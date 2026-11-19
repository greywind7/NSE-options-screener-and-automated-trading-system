"""
Paper Superstructure: Instantaneous Simulated Execution Engine.
Fills orders immediately against latest Ask/Bid quotes with zero artificial lag.
"""
from typing import List, Optional
import time
import zmq

from core.common.ipc_schemas import OrderRequest, OrderResult
from execution.superstructures.base_superstructure import (
    BaseSuperstructure,
    DEFAULT_HB_SUB_IPC,
    DEFAULT_RMS_SUB_IPC,
    DEFAULT_QUOTES_ROUTER_IPC,
    DEFAULT_QUOTES_STREAM_IPC,
)


class PaperSuperstructure(BaseSuperstructure):
    def __init__(
        self,
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
        super().__init__(
            mode="paper",
            track_names=track_names,
            rms_ipc=rms_ipc,
            feed_ipc=feed_ipc,
            quotes_stream_ipc=quotes_stream_ipc,
            hb_ipc=hb_ipc,
            quotes_router_ipc=quotes_router_ipc,
            recordings_dir=recordings_dir,
            ctx=ctx,
            continue_state=continue_state,
        )

    def submit_order(self, request: OrderRequest) -> None:
        """Instantaneous simulated fill."""
        fill_price = request.price
        fill_qty = request.quantity

        result = OrderResult(
            order_id=request.order_id,
            status="FILLED",
            fill_price=fill_price,
            fill_quantity=fill_qty,
            fill_timestamp=time.time(),
        )

        track = self.tracks.get(request.track)
        if track:
            track.on_order_result(result)

    def cancel_order(self, order_id: str) -> None:
        pass
