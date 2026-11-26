"""
Quotes Broker Service: Centralized Ticker Plant & Last Value Cache (LVC).
Maintains latest bid/ask depth for all active option contracts and equities (< 200 KB RAM).
Provides microsecond ZeroMQ ROUTER/DEALER snapshot and subscription request-reply interface.
Publishes streaming depth ticks strictly for active subscribed contracts over ZeroMQ PUB.
Broadcasts 1-second timestamp heartbeats to drive lazy, thread-free delayed execution.
"""
import argparse
from collections import defaultdict
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set
import zmq

from core.common.symbol_utils import is_option_contract, normalize_instrument_token
from core.common.ipc_schemas import (
    QuoteDepthUpdate,
    QuoteHeartbeat,
    QuoteSnapshotResponse,
    QuoteSubscriptionResponse,
    pack_message,
    unpack_message,
)
from core.config.config_loader import get_config_manager

DEFAULT_FEED_IPC = "ipc:///tmp/feed_options.ipc"
DEFAULT_QUOTES_ROUTER_IPC = "ipc:///tmp/quotes_router.ipc"
DEFAULT_QUOTES_STREAM_IPC = "ipc:///tmp/quotes_stream.ipc"
DEFAULT_HEARTBEAT_PUB_IPC = "ipc:///tmp/quotes_heartbeat.pub"


class QuotesBroker:
    def __init__(
        self,
        feed_ipc: Optional[str] = None,
        router_ipc: Optional[str] = None,
        stream_ipc: Optional[str] = None,
        heartbeat_ipc: Optional[str] = None,
        ctx: Optional[zmq.Context] = None,
        feed_equity_ipc: Optional[str] = None,
    ):
        cfg = get_config_manager()
        ipc_endpoints = cfg.get_ipc_endpoints()

        self.feed_ipc = feed_ipc or ipc_endpoints.get("feed_options_ipc", DEFAULT_FEED_IPC)
        if feed_equity_ipc is not None:
            self.feed_equity_ipc = feed_equity_ipc
        elif feed_ipc and str(feed_ipc).startswith("inproc://"):
            self.feed_equity_ipc = None
        else:
            self.feed_equity_ipc = ipc_endpoints.get("feed_equity_ipc", "ipc:///tmp/feed_equity.ipc")
        self.router_ipc = router_ipc or ipc_endpoints.get("quotes_router_ipc", DEFAULT_QUOTES_ROUTER_IPC)
        self.stream_ipc = stream_ipc or ipc_endpoints.get("quotes_stream_ipc", DEFAULT_QUOTES_STREAM_IPC)
        self.heartbeat_ipc = heartbeat_ipc or ipc_endpoints.get("heartbeat_ipc", DEFAULT_HEARTBEAT_PUB_IPC)
        self.running = False

        # In-memory Last Value Cache (LVC): token -> quote dict
        self._lvc: Dict[str, Dict[str, Any]] = {}

        # Set of active contract tokens currently subscribed by execution superstructures/tracks
        self.subscribed_tokens: Set[str] = set()

        # Multi-client reference counting: token -> set of active subscriber client IDs (e.g. {"paper", "paper_delayed", "live"})
        self.subscribers_by_token: Dict[str, Set[str]] = defaultdict(set)

        # ZeroMQ Context
        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        # 1. Subscriber to live option depth & equity ticks from FeedHandler
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.RCVHWM, 100000)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"TICK.OPT")
        self.sub.connect(self.feed_ipc)
        if self.feed_equity_ipc:
            self.sub.setsockopt(zmq.SUBSCRIBE, b"TICK.EQ")
            if self.feed_equity_ipc != self.feed_ipc:
                self.sub.connect(self.feed_equity_ipc)

        # 2. ROUTER socket for snapshot and sub/unsub commands
        self.router = self.ctx.socket(zmq.ROUTER)
        self.router.setsockopt(zmq.LINGER, 0)
        self.router.setsockopt(zmq.SNDHWM, 100000)
        self.router.setsockopt(zmq.RCVHWM, 100000)
        self.router.bind(self.router_ipc)

        # 3. PUB socket for streaming depth updates to subscribed superstructures/tracks
        self.stream_pub = self.ctx.socket(zmq.PUB)
        self.stream_pub.setsockopt(zmq.LINGER, 0)
        self.stream_pub.setsockopt(zmq.SNDHWM, 200000)
        self.stream_pub.bind(self.stream_ipc)

        # 4. Optional separate PUB socket for backward-compatible heartbeat consumers
        self._distinct_hb = (self.heartbeat_ipc != self.stream_ipc)
        if self._distinct_hb:
            self.hb_pub = self.ctx.socket(zmq.PUB)
            self.hb_pub.setsockopt(zmq.LINGER, 0)
            self.hb_pub.bind(self.heartbeat_ipc)
        else:
            self.hb_pub = self.stream_pub

    def _publish_stream(self, topic: bytes, payload: bytes) -> None:
        """Publish a 2-frame message on stream_pub without allowing HWM backpressure to kill the loop."""
        try:
            self.stream_pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
        except zmq.Again:
            pass

    def on_tick(self, tick: Dict[str, Any]) -> None:
        """Update LVC with latest quote and forward to stream_pub if contract is subscribed."""
        token = normalize_instrument_token(tick.get("token", ""))
        if not token:
            return

        ltp = float(tick.get("ltp") or 0.0)
        raw_bid = tick.get("bid")
        raw_ask = tick.get("ask")
        bid = float(raw_bid) if raw_bid is not None else 0.0
        ask = float(raw_ask) if raw_ask is not None else 0.0
        ts = float(tick.get("exchange_timestamp") or tick.get("received_timestamp") or time.time())
        bids = [[float(v) for v in lvl] for lvl in tick.get("bids", []) if isinstance(lvl, (list, tuple))]
        asks = [[float(v) for v in lvl] for lvl in tick.get("asks", []) if isinstance(lvl, (list, tuple))]

        quote_data = {
            "token": token,
            "symbol": tick.get("symbol", ""),
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "bid_qty": int(tick.get("bid_qty", 0)),
            "ask_qty": int(tick.get("ask_qty", 0)),
            "bids": bids,
            "asks": asks,
            "timestamp": ts,
        }
        sym = tick.get("symbol", "")
        is_opt = is_option_contract(token) or is_option_contract(sym)
        self._lvc[token] = quote_data
        # Only index/stream under sym alias for genuine equity ticks (or option trading symbols),
        # preventing mock/synthetic option ticks with symbol='RELIANCE' from polluting underlying equity quotes.
        allow_sym_alias = bool(sym) and ((not is_opt) or is_option_contract(sym))
        if allow_sym_alias:
            norm_sym = normalize_instrument_token(sym)
            self._lvc[norm_sym] = quote_data
            if ":" not in sym and not is_opt:
                self._lvc[f"NSE:{sym}"] = quote_data

        # If this contract or underlying is currently subscribed by superstructures/tracks, broadcast update
        if token in self.subscribed_tokens:
            payload = pack_message(quote_data)
            topic = f"QUOTE.{token}".encode("utf-8")
            self._publish_stream(topic, payload)

        if allow_sym_alias:
            norm_sym = normalize_instrument_token(sym)
            if norm_sym != token and norm_sym in self.subscribed_tokens:
                payload = pack_message(quote_data)
                topic = f"QUOTE.{norm_sym}".encode("utf-8")
                self._publish_stream(topic, payload)
            elif not is_opt and ":" not in sym:
                nse_sym = f"NSE:{sym}".upper()
                if nse_sym != token and nse_sym in self.subscribed_tokens:
                    payload = pack_message(quote_data)
                    topic = f"QUOTE.{nse_sym}".encode("utf-8")
                    self._publish_stream(topic, payload)

    def _format_quote(self, q: Dict[str, Any], best_depth: bool) -> Dict[str, Any]:
        if best_depth:
            return {
                "token": q["token"],
                "symbol": q["symbol"],
                "ltp": q["ltp"],
                "bid": q["bid"],
                "ask": q["ask"],
                "bid_qty": q["bid_qty"],
                "ask_qty": q["ask_qty"],
                "timestamp": q["timestamp"],
            }
        return q

    def _handle_request(self) -> bool:
        """Handle incoming ROUTER snapshot or subscription request. Returns True if a request was processed."""
        try:
            frames = self.router.recv_multipart(flags=zmq.NOBLOCK)
            if len(frames) < 3 or frames[1] != b"":
                return True
            client_id = frames[0]
            # frame 1 is validated empty delimiter
            payload = unpack_message(frames[2])
            action = payload.get("action", "")
            best_depth = payload.get("best_depth", True)

            if action in ("GET_QUOTES", "GET_SNAPSHOT"):
                tokens = payload.get("tokens", [])
                if action == "GET_QUOTES" and tokens:
                    result = {}
                    for t in tokens:
                        norm = normalize_instrument_token(t)
                        if norm in self._lvc:
                            result[norm] = self._format_quote(self._lvc[norm], best_depth=best_depth)
                else:
                    result = {k: self._format_quote(v, best_depth=best_depth) for k, v in self._lvc.items()}
                reply_payload = pack_message({"status": "SUCCESS", "quotes": result})

            elif action == "SUBSCRIBE":
                tokens = payload.get("tokens", [])
                sub_id = str(payload.get("client_id") or payload.get("subscriber_id") or client_id.hex())
                quotes_snap = {}
                for t in tokens:
                    norm = normalize_instrument_token(t)
                    if norm:
                        self.subscribers_by_token[norm].add(sub_id)
                        self.subscribed_tokens.add(norm)
                        if norm in self._lvc:
                            q_formatted = self._format_quote(self._lvc[norm], best_depth=best_depth)
                            quotes_snap[norm] = q_formatted
                            # Also publish snapshot immediately to stream so subscriber has prevailing depth
                            topic = f"QUOTE.{norm}".encode("utf-8")
                            self._publish_stream(topic, pack_message(self._lvc[norm]))

                ref_counts = {t: len(self.subscribers_by_token[t]) for t in self.subscribed_tokens}
                reply_payload = pack_message({
                    "status": "SUCCESS",
                    "subscribed_tokens": list(self.subscribed_tokens),
                    "ref_counts": ref_counts,
                    "quotes": quotes_snap,
                })

            elif action == "UNSUBSCRIBE":
                tokens = payload.get("tokens", [])
                sub_id = str(payload.get("client_id") or payload.get("subscriber_id") or "")
                for t in tokens:
                    norm = normalize_instrument_token(t)
                    if norm in self.subscribers_by_token:
                        if sub_id:
                            self.subscribers_by_token[norm].discard(sub_id)
                        else:
                            # Legacy caller without client_id: decrement by popping one anonymous/first subscriber
                            if self.subscribers_by_token[norm]:
                                any_sub = next(iter(self.subscribers_by_token[norm]))
                                self.subscribers_by_token[norm].discard(any_sub)

                        # When reference count drops to zero, stop publishing updates for this contract
                        if not self.subscribers_by_token[norm]:
                            self.subscribers_by_token.pop(norm, None)
                            self.subscribed_tokens.discard(norm)

                ref_counts = {t: len(self.subscribers_by_token[t]) for t in self.subscribed_tokens}
                reply_payload = pack_message({
                    "status": "SUCCESS",
                    "subscribed_tokens": list(self.subscribed_tokens),
                    "ref_counts": ref_counts,
                })

            else:
                reply_payload = pack_message({"status": "ERROR", "error": f"Unknown action: {action}"})

            self.router.send_multipart([client_id, b"", reply_payload], flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False
        except Exception as e:
            print(f"[QUOTES_BROKER] Request error: {e}", file=sys.stderr)
            return True

    def _emit_heartbeat(self, now: Optional[float] = None) -> None:
        """Emit a single 1-second heartbeat from the owning event-loop thread."""
        ts = now if now is not None else time.time()
        hb_data = {
            "type": "HEARTBEAT",
            "timestamp": ts,
            "active_quotes_count": len(self._lvc),
            "subscribed_count": len(self.subscribed_tokens),
        }
        payload = pack_message(hb_data)
        self._publish_stream(b"HEARTBEAT", payload)
        if self._distinct_hb:
            try:
                self.hb_pub.send_multipart([b"HEARTBEAT", payload], flags=zmq.NOBLOCK)
            except zmq.Again:
                pass

    def get_ref_count(self, token: str) -> int:
        """Return the current active subscriber reference count for a contract token."""
        norm = normalize_instrument_token(token)
        return len(self.subscribers_by_token.get(norm, set()))

    def start(self) -> None:
        """Run the QuotesBroker single-threaded ZMQ event loop (keeps all sockets single-owner)."""
        self.running = True
        print(
            f"[QUOTES_BROKER] Active | Feed SUB: {self.feed_ipc} | ROUTER: {self.router_ipc} "
            f"| STREAM PUB: {self.stream_ipc} | Heartbeat: {self.heartbeat_ipc}",
            flush=True,
        )

        poller = zmq.Poller()
        poller.register(self.sub, zmq.POLLIN)
        poller.register(self.router, zmq.POLLIN)
        last_hb_ts = time.time()

        while self.running:
            try:
                events = dict(poller.poll(timeout=10))
                if self.sub in events:
                    for _ in range(256):
                        try:
                            frames = self.sub.recv_multipart(flags=zmq.NOBLOCK)
                        except zmq.Again:
                            break
                        if len(frames) >= 2:
                            tick = unpack_message(frames[-1])
                            self.on_tick(tick)
                        if not self.running:
                            break

                if self.router in events:
                    for _ in range(64):
                        if not self._handle_request() or not self.running:
                            break

                now = time.time()
                if now - last_hb_ts >= 1.0:
                    self._emit_heartbeat(now)
                    last_hb_ts = now
            except zmq.ZMQError:
                break
            except Exception as e:
                print(f"[QUOTES_BROKER] Loop error: {e}", file=sys.stderr, flush=True)

    def stop(self) -> None:
        self.running = False
        try:
            self.sub.close(linger=0)
            self.router.close(linger=0)
            self.stream_pub.close(linger=0)
            if self._distinct_hb:
                self.hb_pub.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        print("[QUOTES_BROKER] Stopped cleanly.")


class QuotesBrokerClient:
    """Client for querying and managing subscriptions with QuotesBroker over ZeroMQ DEALER socket."""
    def __init__(
        self,
        router_ipc: Optional[str] = None,
        timeout_ms: int = 1000,
        ctx: Optional[zmq.Context] = None,
    ):
        cfg = get_config_manager()
        self.router_ipc = router_ipc or cfg.get_ipc_endpoints().get("quotes_router_ipc", DEFAULT_QUOTES_ROUTER_IPC)
        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()
        self.socket = self.ctx.socket(zmq.DEALER)
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self.socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
        self.socket.connect(self.router_ipc)

    def get_quotes(self, tokens: List[str], best_depth: bool = True) -> Dict[str, Dict[str, Any]]:
        """Query multiple quotes atomically. Pass best_depth=False to retrieve full 5-level bids/asks arrays."""
        norm_tokens = [normalize_instrument_token(t) for t in tokens]
        req = pack_message({"action": "GET_QUOTES", "tokens": norm_tokens, "best_depth": best_depth})
        try:
            self.socket.send_multipart([b"", req])
            frames = self.socket.recv_multipart()
            if len(frames) >= 2:
                resp = unpack_message(frames[1])
                return resp.get("quotes", {})
        except Exception:
            pass
        return {}

    def get_quote(self, token: str, best_depth: bool = True) -> Optional[Dict[str, Any]]:
        """Query single quote. Pass best_depth=False to retrieve full 5-level bids/asks arrays."""
        res = self.get_quotes([token], best_depth=best_depth)
        norm = normalize_instrument_token(token)
        return res.get(norm)

    def subscribe(self, tokens: List[str], client_id: Optional[str] = None, best_depth: bool = True) -> Dict[str, Any]:
        """
        Request QuotesBroker to start streaming depth ticks for tokens over quotes_stream.ipc.
        Pass client_id (e.g. 'paper', 'paper_delayed', 'live') to participate in reference counting.
        Returns immediate snapshot dictionary of available quotes.
        """
        norm_tokens = [normalize_instrument_token(t) for t in tokens]
        payload = {"action": "SUBSCRIBE", "tokens": norm_tokens, "best_depth": best_depth}
        if client_id:
            payload["client_id"] = str(client_id)
        req = pack_message(payload)
        try:
            self.socket.send_multipart([b"", req])
            frames = self.socket.recv_multipart()
            if len(frames) >= 2:
                resp = unpack_message(frames[1])
                return resp.get("quotes", {})
        except Exception:
            pass
        return {}

    def unsubscribe(self, tokens: List[str], client_id: Optional[str] = None) -> bool:
        """
        Request QuotesBroker to decrement subscription reference count for tokens.
        When all subscribers have unsubscribed (ref count == 0), streaming stops.
        """
        norm_tokens = [normalize_instrument_token(t) for t in tokens]
        payload = {"action": "UNSUBSCRIBE", "tokens": norm_tokens}
        if client_id:
            payload["client_id"] = str(client_id)
        req = pack_message(payload)
        try:
            self.socket.send_multipart([b"", req])
            frames = self.socket.recv_multipart()
            if len(frames) >= 2:
                resp = unpack_message(frames[1])
                return resp.get("status") == "SUCCESS"
        except Exception:
            pass
        return False

    def close(self) -> None:
        try:
            self.socket.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Quotes Broker Service")
    parser.add_argument("--feed-ipc", default=DEFAULT_FEED_IPC, help="ZeroMQ options feed address")
    parser.add_argument("--feed-equity-ipc", default=None, help="ZeroMQ equity feed address")
    parser.add_argument("--router-ipc", default=DEFAULT_QUOTES_ROUTER_IPC, help="ROUTER address")
    parser.add_argument("--stream-ipc", default=DEFAULT_QUOTES_STREAM_IPC, help="STREAM PUB address")
    parser.add_argument("--heartbeat-ipc", default=DEFAULT_HEARTBEAT_PUB_IPC, help="Heartbeat PUB address")
    args = parser.parse_args()

    broker = QuotesBroker(
        feed_ipc=args.feed_ipc,
        router_ipc=args.router_ipc,
        stream_ipc=args.stream_ipc,
        heartbeat_ipc=args.heartbeat_ipc,
        feed_equity_ipc=args.feed_equity_ipc,
    )

    def sig_handler(sig, frame):
        broker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    broker.start()


if __name__ == "__main__":
    main()
