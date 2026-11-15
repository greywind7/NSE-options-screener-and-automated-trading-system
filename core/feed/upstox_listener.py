"""
core.feed.upstox_listener: Compatibility wrapper for MarketDataListener.
Unified with high-performance ZeroMQ dual-socket FeedHandler.
"""
from core.feed.feed_handler import FeedHandler, DEFAULT_FEED_EQ_IPC, DEFAULT_FEED_OPT_IPC


class MarketDataListener(FeedHandler):
    """
    Compatibility wrapper retaining the MarketDataListener signature,
    powered natively by ZeroMQ FeedHandler.
    """
    def __init__(
        self,
        mode: str = "mock",
        exchange: str = "NSE",
        feed_equity_ipc: str = DEFAULT_FEED_EQ_IPC,
        feed_options_ipc: str = DEFAULT_FEED_OPT_IPC,
        ipc_path: str = DEFAULT_FEED_EQ_IPC,
        access_token: str = None,
        poll_interval: float = 1.0,
        manifest_path: str = None,
        filtered_csv: str = None,
        debug_log: str = "logs/debug.log",
        enable_socket: bool = True,
        max_stream_contracts: int = 1800,
        silent: bool = False,
        allow_rest_fallback: bool = False,
    ):
        super().__init__(
            mode=mode,
            exchange=exchange,
            feed_equity_ipc=feed_equity_ipc or ipc_path,
            feed_options_ipc=feed_options_ipc,
            access_token=access_token,
            manifest_path=manifest_path,
            filtered_csv=filtered_csv,
            max_stream_contracts=max_stream_contracts,
            silent=silent,
            allow_rest_fallback=allow_rest_fallback,
            poll_interval=poll_interval,
        )


__all__ = ["MarketDataListener"]
