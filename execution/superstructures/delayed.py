"""
Delayed Superstructure: Thread-Free Event-Driven Delayed Execution (CRIT-007).
Maintains priority release queue triggered by QuotesBroker 1-second heartbeats and incoming ticks,
executing delayed fills with zero OS sleep threads.
"""
import heapq
import random
import time
from typing import Any, Dict, List, Optional, Tuple
import zmq

from core.common.ipc_schemas import OrderRequest, OrderResult
from execution.superstructures.base_superstructure import (
    BaseSuperstructure,
    DEFAULT_HB_SUB_IPC,
    DEFAULT_RMS_SUB_IPC,
    DEFAULT_QUOTES_ROUTER_IPC,
    DEFAULT_QUOTES_STREAM_IPC,
)


class DelayedSuperstructure(BaseSuperstructure):
    def __init__(
        self,
        track_names: List[str],
        delay_sec: Optional[float] = None,
        min_delay_ms: Optional[float] = None,
        max_delay_ms: Optional[float] = None,
        rms_ipc: Optional[str] = None,
        feed_ipc: Optional[str] = None,
        quotes_stream_ipc: Optional[str] = None,
        hb_ipc: Optional[str] = None,
        quotes_router_ipc: Optional[str] = None,
        recordings_dir: Optional[str] = None,
        ctx: Optional[zmq.Context] = None,
    ):
        super().__init__(
            mode="paper_delayed",
            track_names=track_names,
            rms_ipc=rms_ipc,
            feed_ipc=feed_ipc,
            quotes_stream_ipc=quotes_stream_ipc,
            hb_ipc=hb_ipc,
            quotes_router_ipc=quotes_router_ipc,
            recordings_dir=recordings_dir,
            ctx=ctx,
        )
        delayed_cfg = self._cfg.get_execution_mode_config("paper_delayed")
        if delay_sec is not None:
            self.min_delay_ms = float(delay_sec) * 1000.0
            self.max_delay_ms = float(delay_sec) * 1000.0
        else:
            self.min_delay_ms = float(min_delay_ms if min_delay_ms is not None else delayed_cfg.get("min_delay_ms", 10000.0))
            self.max_delay_ms = float(max_delay_ms if max_delay_ms is not None else delayed_cfg.get("max_delay_ms", 60000.0))
        self.delay_sec = self.max_delay_ms / 1000.0

        # Priority queue for pending orders: (release_timestamp, order_counter, OrderRequest)
        self._order_counter = 0
        self._queue: List[Tuple[float, int, OrderRequest]] = []

    def submit_order(self, request: OrderRequest) -> None:
        """Enqueue order for release after random delay between min_delay_ms and max_delay_ms."""
        delay_sec = random.uniform(self.min_delay_ms, self.max_delay_ms) / 1000.0
        release_ts = (request.timestamp or time.time()) + delay_sec
        self._order_counter += 1
        heapq.heappush(self._queue, (release_ts, self._order_counter, request))

    def cancel_order(self, order_id: str) -> None:
        self._queue = [item for item in self._queue if item[2].order_id != order_id]
        heapq.heapify(self._queue)

    def _process_queue(self, current_ts: float) -> None:
        """Process all queued orders whose release_timestamp <= current_ts."""
        while self._queue and self._queue[0][0] <= current_ts:
            release_ts, _, req = heapq.heappop(self._queue)

            # Query executable quote at release time
            q = self.quotes_client.get_quote(req.instrument_token)
            if q:
                fill_price = float(q.get("ask" if req.transaction_type == "BUY" else "bid") or req.price)
            else:
                fill_price = req.price

            result = OrderResult(
                order_id=req.order_id,
                status="FILLED",
                fill_price=fill_price,
                fill_quantity=req.quantity,
                fill_timestamp=release_ts,
            )

            track = self.tracks.get(req.track)
            if track:
                track.on_order_result(result)

    def on_heartbeat(self, ts: float) -> None:
        """CRIT-007: QuotesBroker 1s heartbeat drives delayed release queue progression and trade timers."""
        self._process_queue(ts)
        super().on_heartbeat(ts)

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
        """Market tick progresses queue and evaluates active trades."""
        self._process_queue(ts)
        super().on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts, topic_token=topic_token, symbol=symbol)
