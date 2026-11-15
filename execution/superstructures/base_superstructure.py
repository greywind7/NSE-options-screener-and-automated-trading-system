"""
Base Superstructure: Universal Execution Coordinator.
Implements ExecutionContext, owns single-owner JSONL trade journals (Gap 3),
manages in-memory atomic freeze flag (Gap 5), and communicates with QuotesBroker
via on-demand pub/sub and request-reply caching (eliminates direct feed saturation).
"""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import zmq

from common.interfaces import ExecutionContext, TradeState
from core.common.ipc_schemas import (
    ActionableOpportunity,
    OrderRequest,
    OrderResult,
    unpack_message,
)
from core.quotes.quotes_broker import (
    QuotesBrokerClient,
    DEFAULT_QUOTES_ROUTER_IPC,
    DEFAULT_QUOTES_STREAM_IPC,
)

DEFAULT_RMS_SUB_IPC = "ipc:///tmp/rms_strike_pub.ipc"
DEFAULT_HB_SUB_IPC = "ipc:///tmp/quotes_heartbeat.pub"


class BaseSuperstructure(ExecutionContext, ABC):
    def __init__(
        self,
        mode: str,
        track_names: List[str],
        rms_ipc: Optional[str] = None,
        feed_ipc: Optional[str] = None,
        quotes_stream_ipc: Optional[str] = None,
        hb_ipc: Optional[str] = None,
        quotes_router_ipc: Optional[str] = None,
        recordings_dir: Optional[str] = None,
        ctx: Optional[zmq.Context] = None,
    ):
        from core.config.config_loader import get_config_manager
        from common.tracks.factory import get_enabled_track_names, create_tracks
        self._cfg = get_config_manager()
        ipc = self._cfg.get_ipc_endpoints()
        rec_cfg = self._cfg.get_recorder_config()

        self._mode = mode
        self.track_names = track_names or get_enabled_track_names()
        self.rms_ipc = rms_ipc or ipc.get("rms_strike_ipc", DEFAULT_RMS_SUB_IPC)
        self.quotes_stream_ipc = quotes_stream_ipc or feed_ipc or ipc.get("quotes_stream_ipc", DEFAULT_QUOTES_STREAM_IPC)
        self.quotes_router_ipc = quotes_router_ipc or ipc.get("quotes_router_ipc", DEFAULT_QUOTES_ROUTER_IPC)
        self.recordings_dir = recordings_dir or rec_cfg.get("output_dir", "recordings")
        self.running = False

        # Gap 5: In-memory atomic freeze flag (0-latency check)
        self._is_frozen = False

        # Mode-specific Risk & Capital Management
        from core.rms.mode_risk import ModeRiskManager
        exec_mode_cfg = self._cfg.get_execution_mode_config(self.mode)
        starting_cap = float(exec_mode_cfg.get("starting_capital_per_sim", 250000.0))
        daily_loss = float(exec_mode_cfg.get("daily_loss_limit_inr", 10000.0))
        max_cap_pct = float(exec_mode_cfg.get("max_capital_per_trade_pct", 0.50))
        self.risk_mgr = ModeRiskManager(
            mode=self.mode,
            starting_capital=starting_cap,
            daily_loss_limit_inr=daily_loss,
            max_capital_per_trade_pct=max_cap_pct,
        )

        # Instantiate tracks
        self.tracks: Dict[str, Any] = {}
        self._init_tracks()

        # Single-owner journal file handles: track -> open file object (Gap 3)
        self._journal_files: Dict[str, Any] = {}
        self._init_journals()

        # ZeroMQ context and sockets
        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        # 1. SUB socket for RMS opportunities
        self.rms_sub = self.ctx.socket(zmq.SUB)
        self.rms_sub.setsockopt(zmq.LINGER, 0)
        self.rms_sub.setsockopt(zmq.RCVHWM, 100000)
        self.rms_sub.setsockopt(zmq.SUBSCRIBE, b"OPP.")
        self.rms_sub.connect(self.rms_ipc)

        # 2. SUB socket for QuotesBroker stream (depth ticks & heartbeats)
        self.quotes_sub = self.ctx.socket(zmq.SUB)
        self.quotes_sub.setsockopt(zmq.LINGER, 0)
        self.quotes_sub.setsockopt(zmq.RCVHWM, 200000)
        self.quotes_sub.setsockopt(zmq.SUBSCRIBE, b"QUOTE")
        self.quotes_sub.setsockopt(zmq.SUBSCRIBE, b"HEARTBEAT")
        self.quotes_sub.connect(self.quotes_stream_ipc)

        # 3. QuotesBroker Client for snapshots and dynamic sub/unsub
        client_timeout = 50 if "dummy" in str(self.quotes_router_ipc) else 1000
        self.quotes_client = QuotesBrokerClient(router_ipc=self.quotes_router_ipc, timeout_ms=client_timeout, ctx=self.ctx)

    @property
    def mode(self) -> str:
        return self._mode

    def is_frozen(self) -> bool:
        return self._is_frozen or self.risk_mgr.is_circuit_breaker_tripped()

    def freeze(self) -> None:
        self._is_frozen = True
        print(f"[{self.mode.upper()}_SUPERSTRUCTURE] In-memory execution FROZEN.")

    def unfreeze(self) -> None:
        self._is_frozen = False
        print(f"[{self.mode.upper()}_SUPERSTRUCTURE] In-memory execution UN-FROZEN.")

    def subscribe_quotes(self, tokens: List[str]) -> None:
        """Subscribe to live quotes stream from Quotes Broker with superstructure mode for reference counting."""
        if self.quotes_client:
            self.quotes_client.subscribe(tokens, client_id=self.mode)

    def unsubscribe_quotes(self, tokens: List[str]) -> None:
        """Unsubscribe from live quotes stream from Quotes Broker if contract no longer held in this mode."""
        if not self.quotes_client:
            return
        tokens_to_unsub = []
        for t in tokens:
            still_held = any(
                any(tr.token == t and tr.status == "OPEN" for tr in track.active_trades.values())
                for track in self.tracks.values()
            )
            if not still_held:
                tokens_to_unsub.append(t)
        if tokens_to_unsub:
            self.quotes_client.unsubscribe(tokens_to_unsub, client_id=self.mode)

    def get_quote(self, token: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest snapshot quote from QuotesBroker LVC."""
        if self.quotes_client:
            return self.quotes_client.get_quote(token)
        return None

    def _init_journals(self) -> None:
        """Gap 3: Initialize dedicated single-owner JSONL files per track."""
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_dir = os.path.join(self.recordings_dir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        all_track_keys = set(self.tracks.keys()) | set(self.track_names or [])
        for tr in all_track_keys:
            filename = f"journal_{self.mode}_{tr}.jsonl"
            filepath = os.path.join(session_dir, filename)
            f = open(filepath, "a", encoding="utf-8", buffering=1)  # Line buffered
            self._journal_files[tr] = f

    def record_trade_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Gap 3: Write atomic JSON line to track's single-owner journal."""
        if event_type == "TRADE_EXIT" and "realized_pnl" in data:
            self.risk_mgr.update_pnl(float(data["realized_pnl"]))
            if self.risk_mgr.is_circuit_breaker_tripped():
                self.freeze()

        tr = data.get("track", "")
        f = self._journal_files.get(tr)
        if f:
            record = {
                "event": event_type,
                "mode": self.mode,
                "timestamp": time.time(),
                "data": data,
            }
            f.write(json.dumps(record) + "\n")
            f.flush()

    def _init_tracks(self) -> None:
        """Factory import and instantiation of requested tracks."""
        from common.tracks.factory import create_tracks
        self.tracks = create_tracks(context=self, track_filter=self.track_names)
        for name in self.tracks.keys():
            print(f"[{self.mode.upper()}_SUPERSTRUCTURE] Registered track: {name}")

    def on_opportunity(self, opp: ActionableOpportunity) -> None:
        """Route incoming opportunity to all active tracks."""
        if self.is_frozen():
            return
        for track in self.tracks.values():
            track.on_opportunity(opp)

    def on_tick(self, token: str, ltp: float, bid: float, ask: float, ts: float) -> None:
        """Route market tick to all active tracks."""
        for track in self.tracks.values():
            track.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts)

    def on_heartbeat(self, ts: float) -> None:
        """Hook for timer/delayed queue progression."""
        pass

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> None:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        pass

    def start(self) -> None:
        """Main polling event loop."""
        self.running = True
        print(f"[{self.mode.upper()}_SUPERSTRUCTURE] Active | Tracks: {list(self.tracks.keys())}")

        poller = zmq.Poller()
        poller.register(self.rms_sub, zmq.POLLIN)
        poller.register(self.quotes_sub, zmq.POLLIN)

        while self.running:
            try:
                events = dict(poller.poll(timeout=10))
                # 1. Opportunities from RMS
                if self.rms_sub in events:
                    _, payload = self.rms_sub.recv_multipart()
                    opp_dict = unpack_message(payload)
                    opp = ActionableOpportunity(**opp_dict)
                    self.on_opportunity(opp)

                # 2. Quotes & Heartbeats from QuotesBroker stream
                if self.quotes_sub in events:
                    topic, payload = self.quotes_sub.recv_multipart()
                    data = unpack_message(payload)
                    if topic.startswith(b"QUOTE"):
                        token = data.get("token", "")
                        ltp = float(data.get("ltp", 0.0))
                        bid = float(data.get("bid", ltp))
                        ask = float(data.get("ask", ltp))
                        ts = float(data.get("timestamp") or time.time())
                        self.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts)
                    elif topic == b"HEARTBEAT":
                        self.on_heartbeat(float(data.get("timestamp", time.time())))

            except zmq.ZMQError:
                break
            except Exception as e:
                print(f"[{self.mode.upper()}_SUPERSTRUCTURE] Error: {e}", file=sys.stderr)

    def stop(self) -> None:
        self.running = False
        try:
            if hasattr(self, "rms_sub") and self.rms_sub:
                self.rms_sub.close(linger=0)
            if hasattr(self, "quotes_sub") and self.quotes_sub:
                self.quotes_sub.close(linger=0)
            if hasattr(self, "quotes_client") and self.quotes_client:
                self.quotes_client.close()
            if getattr(self, "_owns_ctx", False) and hasattr(self, "ctx") and self.ctx:
                self.ctx.destroy(linger=0)
            if hasattr(self, "_journal_files"):
                for f in list(self._journal_files.values()):
                    try:
                        f.flush()
                        f.close()
                    except Exception:
                        pass
                self._journal_files.clear()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
