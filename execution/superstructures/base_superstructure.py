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
from core.common.time_utils import get_market_date_str
from core.common.logging_utils import get_logger
from core.quotes.quotes_broker import (
    QuotesBrokerClient,
    DEFAULT_QUOTES_ROUTER_IPC,
    DEFAULT_QUOTES_STREAM_IPC,
)

_logger = get_logger("SUPERSTRUCTURE")

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
        continue_state: bool = False,
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
        self.continue_state = continue_state
        self.running = False

        # Gap 5: In-memory atomic freeze flag (0-latency check)
        self._is_frozen = False

        # Mode-specific Risk & Capital Management
        from core.rms.mode_risk import ModeRiskManager
        risk_cfg = self._cfg.get_risk_config()
        exec_mode_cfg = self._cfg.get_execution_mode_config(self.mode)
        starting_cap = float(exec_mode_cfg.get("starting_capital_per_sim", 250000.0))
        daily_loss = float(risk_cfg.get("daily_loss_limit_inr", exec_mode_cfg.get("daily_loss_limit_inr", 10000.0)))
        max_cap_pct = float(risk_cfg.get("max_capital_allocation_pct", exec_mode_cfg.get("max_capital_per_trade_pct", 0.50)))
        enable_cb = bool(risk_cfg.get("enable_circuit_breaker", False))
        self.risk_mgr = ModeRiskManager(
            mode=self.mode,
            starting_capital=starting_cap,
            daily_loss_limit_inr=daily_loss,
            max_capital_per_trade_pct=max_cap_pct,
            enable_circuit_breaker=enable_cb,
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

        self._pending_delayed_recovery: List[TradeState] = []
        if self.continue_state:
            self._recover_state_from_journals()

    def _recover_state_from_journals(self) -> None:
        """
        Recover active positions, closed trades, and cumulative PnL from journal files on disk.
        Supports BaseTrack, CompositeTrack (routing to originating sub_track), capital restoration,
        and unfilled paper_delayed orders.
        """
        from datetime import datetime
        from common.interfaces import TradeState
        from core.common.symbol_utils import normalize_instrument_token

        today_str = get_market_date_str()
        session_dir = os.path.join(self.recordings_dir, today_str)
        if not os.path.exists(session_dir):
            alt_dir = os.path.join(self.recordings_dir, today_str.replace("-", ""))
            if os.path.exists(alt_dir):
                session_dir = alt_dir
            else:
                return

        IMMUTABLE_FIELDS = {
            "trade_id", "mode", "track", "symbol", "token",
            "option_type", "strike", "lot_size", "quantity",
            "entry_ask", "entry_time", "entry_reason", "origin_track",
        }

        total_open_recovered = 0
        total_closed_recovered = 0
        tokens_to_sub = set()

        for tr_name, track in self.tracks.items():
            filename = f"journal_{self.mode}_{tr_name}.jsonl"
            filepath = os.path.join(session_dir, filename)
            if not os.path.exists(filepath):
                continue

            trades_map: Dict[str, Dict[str, Any]] = {}
            filled_ids = set()
            rejected_ids = set()

            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        event = record.get("event")
                        data = record.get("data", {})
                        trade_id = data.get("trade_id")
                        if not trade_id:
                            continue

                        if event == "TRADE_FILLED":
                            filled_ids.add(trade_id)
                        elif event == "TRADE_REJECTED" or data.get("status") == "REJECTED":
                            rejected_ids.add(trade_id)

                        if trade_id not in trades_map:
                            trades_map[trade_id] = dict(data)
                            if trades_map[trade_id].get("token"):
                                trades_map[trade_id]["token"] = normalize_instrument_token(trades_map[trade_id]["token"])
                        else:
                            tr = trades_map[trade_id]
                            for k, v in data.items():
                                if v is None:
                                    continue
                                if k == "metadata" and isinstance(v, dict):
                                    merged_meta = dict(tr.get("metadata") or {})
                                    merged_meta.update(v)
                                    tr["metadata"] = merged_meta
                                elif k == "entry_price":
                                    if event == "TRADE_FILLED" and float(v) > 0.0:
                                        tr["entry_price"] = float(v)
                                elif k == "entry_time":
                                    if event == "TRADE_FILLED" and float(v) > 0.0:
                                        tr["entry_time"] = float(v)
                                elif k not in IMMUTABLE_FIELDS:
                                    tr[k] = v

                        if event == "TRADE_EXIT" or data.get("status") == "CLOSED":
                            trades_map[trade_id]["status"] = "CLOSED"
                            if data.get("exit_price") is not None:
                                trades_map[trade_id]["exit_price"] = float(data["exit_price"])
                            if data.get("exit_time") is not None:
                                trades_map[trade_id]["exit_time"] = float(data["exit_time"])
                            if data.get("exit_reason"):
                                trades_map[trade_id]["exit_reason"] = data["exit_reason"]
                            if data.get("realized_pnl") is not None:
                                trades_map[trade_id]["realized_pnl"] = float(data["realized_pnl"])
                    except Exception:
                        pass

            for tid, tdict in trades_map.items():
                if tid in rejected_ids or tdict.get("status") == "REJECTED":
                    continue
                try:
                    meta = dict(tdict.get("metadata") or {})
                    is_composite = hasattr(track, "sub_tracks") and bool(getattr(track, "sub_tracks", None))
                    raw_origin = (
                        tdict.get("origin_track")
                        or meta.get("origin_track")
                        or meta.get("sub_track")
                    )
                    origin_track = raw_origin if is_composite else (raw_origin or tr_name)
                    sub_track_rule = (
                        tdict.get("sub_track_rule")
                        or meta.get("sub_track_rule")
                        or (None if is_composite else track.__class__.__name__)
                    )
                    trade = TradeState(
                        trade_id=tid,
                        mode=self.mode,
                        track=tr_name,
                        symbol=tdict.get("symbol", ""),
                        token=normalize_instrument_token(tdict.get("token", "")),
                        option_type=tdict.get("option_type", ""),
                        strike=float(tdict.get("strike", 0.0)),
                        lot_size=int(tdict.get("lot_size", 0)),
                        quantity=int(tdict.get("quantity", 0)),
                        entry_ask=float(tdict.get("entry_ask", 0.0) or tdict.get("entry_price", 0.0)),
                        entry_price=float(tdict.get("entry_price", 0.0)),
                        entry_time=float(tdict.get("entry_time", 0.0)),
                        target_price=float(tdict.get("target_price", 0.0)),
                        stop_loss=float(tdict.get("stop_loss", 0.0)),
                        highest_price=float(tdict.get("highest_price", 0.0)),
                        current_bid=float(tdict.get("current_bid", 0.0)),
                        status=tdict.get("status", "OPEN"),
                        entry_reason=tdict.get("entry_reason"),
                        origin_track=origin_track,
                        sub_track_rule=sub_track_rule,
                        exit_price=float(tdict["exit_price"]) if tdict.get("exit_price") is not None else None,
                        exit_time=float(tdict["exit_time"]) if tdict.get("exit_time") is not None else None,
                        exit_reason=tdict.get("exit_reason"),
                        realized_pnl=float(tdict.get("realized_pnl", 0.0)),
                        ratchet_level=int(tdict.get("ratchet_level", 0)),
                        underlying_entry_price=float(tdict["underlying_entry_price"]) if tdict.get("underlying_entry_price") is not None else None,
                        underlying_exit_price=float(tdict["underlying_exit_price"]) if tdict.get("underlying_exit_price") is not None else None,
                        metadata=meta,
                    )

                    is_filled = (tid in filled_ids) or (self.mode != "paper_delayed")
                    if hasattr(track, "add_recovered_trade"):
                        rec_ok = track.add_recovered_trade(trade, is_filled=is_filled)
                        if rec_ok is False:
                            quarantine_path = os.path.join(session_dir, "quarantined_legacy_trades.jsonl")
                            try:
                                os.makedirs(session_dir, exist_ok=True)
                                with open(quarantine_path, "a", encoding="utf-8") as qf:
                                    qf.write(json.dumps({
                                        "timestamp": time.time(),
                                        "mode": self.mode,
                                        "track": tr_name,
                                        "trade_id": tid,
                                        "origin_track": origin_track,
                                        "reason": "missing_or_invalid_origin_track_for_composite",
                                        "trade": tdict,
                                    }) + "\n")
                            except Exception:
                                pass
                            _logger.error(
                                f"[{self.mode.upper()}] Quarantined ambiguous composite trade {tid} "
                                f"(track='{tr_name}', origin_track={origin_track!r}) -> {quarantine_path}"
                            )
                            continue
                    elif trade.status == "OPEN":
                        track.active_trades[tid] = trade
                    else:
                        track.closed_trades.append(trade)

                    if trade.status == "OPEN":
                        if trade.token:
                            tokens_to_sub.add(trade.token)
                        if trade.symbol:
                            tokens_to_sub.add(trade.symbol)
                            tokens_to_sub.add(f"NSE:{trade.symbol}")
                        total_open_recovered += 1
                        if self.mode == "paper_delayed" and not is_filled:
                            self._pending_delayed_recovery.append(trade)
                        else:
                            try:
                                from core.feed.position_registry import ProtectedPositionRegistry
                                ProtectedPositionRegistry.get_instance().register_open_position(
                                    contract_key=trade.token,
                                    symbol=trade.symbol,
                                    contract_symbol=trade.symbol,
                                    mode=self.mode,
                                    track=tr_name,
                                    trade_id=tid,
                                    quantity=trade.quantity,
                                    entry_price=trade.entry_price,
                                    entry_time=trade.entry_time,
                                )
                            except Exception:
                                pass
                    else:
                        self.risk_mgr.update_pnl(trade.realized_pnl)
                        total_closed_recovered += 1
                except Exception as e:
                    _logger.error(f"[{self.mode.upper()}] Error recovering trade {tid}: {e}")

        if tokens_to_sub:
            self._recovered_tokens = list(tokens_to_sub)
            quotes = self.subscribe_quotes(self._recovered_tokens)
            if quotes:
                for tok, q in quotes.items():
                    bid = float(q.get("bid", 0.0))
                    if bid > 0:
                        for tr in self.tracks.values():
                            for trade in tr.active_trades.values():
                                if trade.token == tok:
                                    trade.current_bid = bid
                                    trade.highest_price = max(trade.highest_price, bid)

        _logger.info(
            f"[{self.mode.upper()}] State recovered (--continue): "
            f"{total_open_recovered} open trades re-activated, {total_closed_recovered} closed trades restored."
        )

    @property
    def client_id(self) -> str:
        track_str = "_".join(sorted(self.track_names)) if self.track_names else "all"
        return f"{self.mode}_{track_str}_{os.getpid()}"

    @property
    def mode(self) -> str:
        return self._mode

    def is_frozen(self) -> bool:
        freeze_all = os.path.join("logs", ".freeze_all")
        freeze_mode = os.path.join("logs", f".freeze_{self.mode}")
        file_frozen = os.path.exists(freeze_all) or os.path.exists(freeze_mode)
        return self._is_frozen or file_frozen or self.risk_mgr.is_circuit_breaker_tripped()

    def freeze(self) -> None:
        self._is_frozen = True
        _logger.warning(f"[{self.mode.upper()}] In-memory execution FROZEN.")

    def unfreeze(self) -> None:
        self._is_frozen = False
        _logger.info(f"[{self.mode.upper()}] In-memory execution UN-FROZEN.")

    def subscribe_quotes(self, tokens: List[str]) -> Dict[str, Any]:
        """Subscribe to live quotes stream from Quotes Broker with superstructure client_id for reference counting."""
        if self.quotes_client:
            return self.quotes_client.subscribe(tokens, client_id=self.client_id)
        return {}

    def unsubscribe_quotes(self, tokens: List[str]) -> None:
        """Unsubscribe from live quotes stream from Quotes Broker if contract no longer held in this track process."""
        if not self.quotes_client:
            return
        tokens_to_unsub = []
        for t in tokens:
            still_held = any(
                any(
                    (tr.token == t or tr.symbol == t or f"NSE:{tr.symbol}" == t)
                    and tr.status == "OPEN"
                    for tr in track.active_trades.values()
                )
                for track in self.tracks.values()
            )
            if not still_held:
                tokens_to_unsub.append(t)
        if tokens_to_unsub:
            self.quotes_client.unsubscribe(tokens_to_unsub, client_id=self.client_id)

    def get_quote(self, token: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest snapshot quote from QuotesBroker LVC."""
        if self.quotes_client:
            return self.quotes_client.get_quote(token)
        return None

    def _init_journals(self) -> None:
        """Gap 3: Initialize dedicated single-owner JSONL files per track."""
        today_str = get_market_date_str()
        session_dir = os.path.join(self.recordings_dir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        all_track_keys = set(self.tracks.keys()) | set(self.track_names or [])
        for tr in all_track_keys:
            filename = f"journal_{self.mode}_{tr}.jsonl"
            filepath = os.path.join(session_dir, filename)
            f = open(filepath, "a", encoding="utf-8", buffering=1)  # Line buffered
            self._journal_files[tr] = f

    def record_trade_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Gap 3: Write atomic JSON line to track's single-owner journal and sync active position protection."""
        if event_type == "TRADE_FILLED":
            try:
                from core.feed.position_registry import ProtectedPositionRegistry
                ProtectedPositionRegistry.get_instance().register_open_position(
                    contract_key=data.get("token", ""),
                    symbol=data.get("symbol", ""),
                    contract_symbol=data.get("symbol", ""),
                    mode=self.mode,
                    track=data.get("track", ""),
                    trade_id=data.get("trade_id", ""),
                    quantity=int(data.get("quantity", 1)),
                    entry_price=float(data.get("entry_price", 0.0)),
                    entry_time=float(data.get("entry_time") or time.time()),
                )
            except Exception:
                pass
        elif event_type == "TRADE_EXIT":
            if "realized_pnl" in data:
                self.risk_mgr.update_pnl(float(data["realized_pnl"]))
                if self.risk_mgr.is_circuit_breaker_tripped():
                    self.freeze()
            try:
                from core.feed.position_registry import ProtectedPositionRegistry
                ProtectedPositionRegistry.get_instance().register_closed_position(
                    contract_key=data.get("token", ""),
                    mode=self.mode,
                    track=data.get("track", ""),
                    trade_id=data.get("trade_id", ""),
                )
            except Exception:
                pass

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
            _logger.info(f"[{self.mode.upper()}] Registered track: {name}")

    def on_opportunity(self, opp: ActionableOpportunity) -> None:
        """Route incoming opportunity to all active tracks."""
        if self.is_frozen():
            return
        for track in self.tracks.values():
            track.on_opportunity(opp)

    def on_tick(
        self,
        token: str,
        ltp: float,
        bid: float,
        ask: float,
        ts: float,
        topic_token: str = "",
        symbol: str = "",
    ) -> None:
        """Route market tick to all active tracks."""
        for track in self.tracks.values():
            if hasattr(track, "on_tick"):
                try:
                    track.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts, topic_token=topic_token, symbol=symbol)
                except TypeError:
                    track.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts)

    def on_heartbeat(self, ts: float) -> None:
        """Hook for timer/delayed queue progression and periodic track state checks."""
        for track in self.tracks.values():
            if hasattr(track, "on_heartbeat"):
                track.on_heartbeat(ts)

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> None:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        pass

    def start(self) -> None:
        """Main polling event loop."""
        import threading
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGUSR1, lambda _s, _f: self.freeze())
                signal.signal(signal.SIGUSR2, lambda _s, _f: self.unfreeze())
            except (ValueError, OSError, AttributeError):
                pass
        self.running = True
        _logger.info(f"[{self.mode.upper()}] Active | Tracks: {list(self.tracks.keys())}")

        # Re-ensure subscriptions to QuotesBroker upon entering loop for recovered positions
        if hasattr(self, "_recovered_tokens") and self._recovered_tokens:
            quotes = self.subscribe_quotes(self._recovered_tokens)
            if quotes:
                now_ts = time.time()
                for tok, q in quotes.items():
                    bid = float(q["bid"]) if q.get("bid") is not None else 0.0
                    ask = float(q["ask"]) if q.get("ask") is not None else 0.0
                    ltp = float(q.get("ltp") or 0.0)
                    q_ts = float(q.get("timestamp") or now_ts)
                    if bid > 0:
                        self.on_tick(token=tok, ltp=ltp, bid=bid, ask=ask, ts=q_ts)
            self.on_heartbeat(time.time())

        poller = zmq.Poller()
        poller.register(self.rms_sub, zmq.POLLIN)
        poller.register(self.quotes_sub, zmq.POLLIN)

        while self.running:
            try:
                events = dict(poller.poll(timeout=10))
                # 1. Opportunities from RMS
                if self.rms_sub in events:
                    rms_frames = self.rms_sub.recv_multipart()
                    if len(rms_frames) >= 2:
                        opp_dict = unpack_message(rms_frames[-1])
                        opp = ActionableOpportunity(**opp_dict)
                        self.on_opportunity(opp)

                # 2. Quotes & Heartbeats from QuotesBroker stream
                if self.quotes_sub in events:
                    q_frames = self.quotes_sub.recv_multipart()
                    if len(q_frames) < 2:
                        continue
                    topic, payload = q_frames[0], q_frames[-1]
                    data = unpack_message(payload)
                    if topic.startswith(b"QUOTE"):
                        topic_token = ""
                        try:
                            topic_str = topic.decode("utf-8")
                            if "." in topic_str:
                                topic_token = topic_str.split(".", 1)[1]
                        except Exception:
                            pass
                        token = data.get("token") or topic_token
                        sym = data.get("symbol", "")
                        ltp = float(data.get("ltp") or 0.0)
                        bid = float(data["bid"]) if data.get("bid") is not None else 0.0
                        ask = float(data["ask"]) if data.get("ask") is not None else 0.0
                        ts = float(data.get("timestamp") or time.time())
                        self.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts, topic_token=topic_token, symbol=sym)
                    elif topic == b"HEARTBEAT":
                        self.on_heartbeat(float(data.get("timestamp", time.time())))

            except zmq.ZMQError:
                break
            except Exception as e:
                _logger.error(f"[{self.mode.upper()}] Loop error: {e}")

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
