"""
Market Recorder Service: Dual-Stream Zero-Loss Tick & Option Depth Archival Engine.
Subscribes to ZeroMQ feed pub (TICK.EQ and TICK.OPT), batches ticks independently,
and flushes to two separate columnar Parquet files:
- Equity Underlyings: recordings/{date}/equity_ticks_{ts}_{part}.parquet
- Option Depth Ticks: recordings/{date}/option_depth_{ts}_{part}.parquet
"""
import argparse
import atexit
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
import zmq

from core.common.ipc_schemas import unpack_message
from core.common.logging_utils import get_logger
from core.common.symbol_utils import is_option_contract
from core.common.time_utils import get_market_date_str

logger = get_logger("MARKET_RECORDER")

DEFAULT_FEED_EQ_IPC = "ipc:///tmp/feed_equity.ipc"
DEFAULT_FEED_OPT_IPC = "ipc:///tmp/feed_options.ipc"
DEFAULT_FEED_IPC = "ipc:///tmp/feed_equity.ipc"
DEFAULT_OUTPUT_DIR = "recordings"

TICK_SCHEMA = pa.schema([
    ("token", pa.string()),
    ("symbol", pa.string()),
    ("ltp", pa.float64()),
    ("volume", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("bid_qty", pa.int64()),
    ("ask_qty", pa.int64()),
    ("exchange_timestamp", pa.float64()),
    ("received_timestamp", pa.float64()),
])


class MarketRecorder:
    def __init__(
        self,
        feed_ipc: Optional[str] = None,
        feed_eq_ipc: Optional[str] = None,
        feed_opt_ipc: Optional[str] = None,
        output_dir: Optional[str] = None,
        batch_size: Optional[int] = None,
        flush_interval_sec: Optional[float] = None,
        debug: Optional[bool] = None,
        debug_log_path: Optional[str] = None,
        ctx: Optional[zmq.Context] = None,
    ):
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        sys_cfg = cfg.system.get("system", {})
        rec_cfg = sys_cfg.get("recorder", {})
        ipc_cfg = sys_cfg.get("ipc", {})

        self.feed_ipc = feed_ipc
        self.feed_eq_ipc = feed_eq_ipc or ipc_cfg.get("feed_equity_ipc", DEFAULT_FEED_EQ_IPC)
        self.feed_opt_ipc = feed_opt_ipc or ipc_cfg.get("feed_options_ipc", DEFAULT_FEED_OPT_IPC)
        self.output_dir = output_dir or rec_cfg.get("output_dir", DEFAULT_OUTPUT_DIR)
        self.batch_size = int(batch_size or rec_cfg.get("batch_size", 5000))
        self.flush_interval_sec = float(flush_interval_sec or rec_cfg.get("flush_interval_sec", 300.0))
        self.debug = debug if debug is not None else rec_cfg.get("debug", False)
        self.debug_log_path = debug_log_path or rec_cfg.get("debug_log_path", "logs/recorder_ticks.log")
        self._debug_file = None
        if self.debug:
            os.makedirs(os.path.dirname(self.debug_log_path) or ".", exist_ok=True)
            self._debug_file = open(self.debug_log_path, "a", encoding="utf-8")
        self.running = False

        # Dual buffers for equity underlyings and option depth ticks
        self._buffer_eq: List[Dict[str, Any]] = []
        self._buffer_opt: List[Dict[str, Any]] = []
        self._part_counter_eq = 0
        self._part_counter_opt = 0

        self._last_flush_time = time.time()

        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.RCVHWM, 200000)
        # Subscribe to both equity and option depth streams
        self.sub.setsockopt(zmq.SUBSCRIBE, b"TICK.")
        if self.feed_ipc:
            self.sub.connect(self.feed_ipc)
        else:
            self.sub.connect(self.feed_eq_ipc)
            self.sub.connect(self.feed_opt_ipc)

        atexit.register(self.flush)

    def _normalize_record(self, tick: Dict[str, Any], is_opt: bool = False) -> Dict[str, Any]:
        rec = {
            "token": str(tick.get("token", "")),
            "symbol": str(tick.get("symbol", "")),
            "ltp": float(tick.get("ltp", 0.0)),
            "volume": int(tick.get("volume", 0)),
            "bid": float(tick.get("bid", 0.0)),
            "ask": float(tick.get("ask", 0.0)),
            "bid_qty": int(tick.get("bid_qty", 0)),
            "ask_qty": int(tick.get("ask_qty", 0)),
            "exchange_timestamp": float(tick.get("exchange_timestamp", 0.0)),
            "received_timestamp": float(tick.get("received_timestamp", time.time())),
        }
        if is_opt:
            bids = tick.get("bids") or []
            asks = tick.get("asks") or []
            rec["bids"] = [[float(v) for v in lvl] for lvl in bids if isinstance(lvl, (list, tuple))]
            rec["asks"] = [[float(v) for v in lvl] for lvl in asks if isinstance(lvl, (list, tuple))]
        return rec

    def record_tick(self, tick: Dict[str, Any], topic: Optional[bytes] = None) -> None:
        """Route and buffer incoming tick into equity or option depth buffers."""
        if self.debug and self._debug_file:
            try:
                top_str = topic.decode("utf-8", errors="ignore") if isinstance(topic, bytes) else str(topic or "")
                self._debug_file.write(json.dumps({"topic": top_str, "tick": tick}) + "\n")
                self._debug_file.flush()
            except Exception:
                pass

        token = str(tick.get("token", ""))
        symbol = str(tick.get("symbol", ""))
        is_opt = (topic == b"TICK.OPT") if topic else (is_option_contract(token) or is_option_contract(symbol))

        rec = self._normalize_record(tick, is_opt=is_opt)
        if is_opt:
            self._buffer_opt.append(rec)
            if len(self._buffer_opt) >= self.batch_size:
                self.flush_option_depth()
        else:
            self._buffer_eq.append(rec)
            if len(self._buffer_eq) >= self.batch_size:
                self.flush_equity()

    def flush_equity(self) -> Optional[str]:
        """Flush equity ticks buffer to recordings/{date}/equity_ticks.parquet, appending to existing file."""
        if not self._buffer_eq:
            return None

        batch = self._buffer_eq
        self._buffer_eq = []

        first_ts = batch[0].get("exchange_timestamp") or batch[0].get("received_timestamp") or time.time()
        date_str = get_market_date_str(first_ts)
        partition_dir = os.path.join(self.output_dir, date_str)
        os.makedirs(partition_dir, exist_ok=True)

        filepath = os.path.join(partition_dir, "equity_ticks.parquet")
        temp_filepath = os.path.join(partition_dir, "equity_ticks.parquet.tmp")

        df_new = pl.DataFrame(batch)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            try:
                df_existing = pl.read_parquet(filepath)
                df_combined = pl.concat([df_existing, df_new])
                df_combined.write_parquet(temp_filepath, compression="snappy")
                os.replace(temp_filepath, filepath)
            except Exception:
                df_new.write_parquet(temp_filepath, compression="snappy")
                os.replace(temp_filepath, filepath)
        else:
            df_new.write_parquet(temp_filepath, compression="snappy")
            os.replace(temp_filepath, filepath)

        return filepath

    def flush_option_depth(self) -> Optional[str]:
        """Flush option depth ticks buffer to recordings/{date}/option_depth.parquet with 5-level depth, appending to existing file."""
        if not self._buffer_opt:
            return None

        batch = self._buffer_opt
        self._buffer_opt = []

        first_ts = batch[0].get("exchange_timestamp") or batch[0].get("received_timestamp") or time.time()
        date_str = get_market_date_str(first_ts)
        partition_dir = os.path.join(self.output_dir, date_str)
        os.makedirs(partition_dir, exist_ok=True)

        filepath = os.path.join(partition_dir, "option_depth.parquet")
        temp_filepath = os.path.join(partition_dir, "option_depth.parquet.tmp")

        depth_schema = {
            "token": pl.String,
            "symbol": pl.String,
            "ltp": pl.Float64,
            "volume": pl.Int64,
            "bid": pl.Float64,
            "ask": pl.Float64,
            "bid_qty": pl.Int64,
            "ask_qty": pl.Int64,
            "bids": pl.List(pl.List(pl.Float64)),
            "asks": pl.List(pl.List(pl.Float64)),
            "exchange_timestamp": pl.Float64,
            "received_timestamp": pl.Float64,
        }
        df_new = pl.DataFrame(batch, schema=depth_schema)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            try:
                df_existing = pl.read_parquet(filepath)
                df_combined = pl.concat([df_existing, df_new])
                df_combined.write_parquet(temp_filepath, compression="snappy")
                os.replace(temp_filepath, filepath)
            except Exception:
                df_new.write_parquet(temp_filepath, compression="snappy")
                os.replace(temp_filepath, filepath)
        else:
            df_new.write_parquet(temp_filepath, compression="snappy")
            os.replace(temp_filepath, filepath)

        return filepath

    def flush(self) -> Dict[str, Optional[str]]:
        """Flush both equity and option depth buffers."""
        self._last_flush_time = time.time()
        f_eq = self.flush_equity()
        f_opt = self.flush_option_depth()
        return {"equity": f_eq, "option_depth": f_opt}

    def start(self) -> None:
        """Run dual-stream subscriber loop."""
        self.running = True
        feed_info = self.feed_ipc if self.feed_ipc else f"EQ={self.feed_eq_ipc}, OPT={self.feed_opt_ipc}"
        logger.info(f"Active (Dual Stream) | Feed: {feed_info} | Dest: {self.output_dir}")

        poller = zmq.Poller()
        poller.register(self.sub, zmq.POLLIN)

        try:
            while self.running:
                try:
                    events = dict(poller.poll(timeout=100))
                    if self.sub in events:
                        topic, payload = self.sub.recv_multipart()
                        tick = unpack_message(payload)
                        self.record_tick(tick, topic=topic)

                    # Time-based flush
                    if time.time() - self._last_flush_time >= self.flush_interval_sec:
                        self.flush()

                except zmq.ZMQError:
                    break
                except Exception as e:
                    logger.error(f"Recorder loop error: {e}")
        finally:
            self.flush()

    def stop(self) -> None:
        """Stop and flush both buffers cleanly."""
        self.running = False
        try:
            self.flush()
        except Exception as e:
            logger.error(f"Error flushing on stop: {e}")
        try:
            self.sub.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        if self._debug_file:
            try:
                self._debug_file.close()
            except Exception:
                pass
            self._debug_file = None
        logger.info("Stopped cleanly and flushed (Dual Streams).")


def main():
    parser = argparse.ArgumentParser(description="Market Recorder Service (Dual-Stream)")
    parser.add_argument("--feed-ipc", default=None, help="Feed PUB endpoint (default: None for dual-stream)")
    parser.add_argument("--feed-equity-ipc", default=None, help="Feed Equity PUB endpoint")
    parser.add_argument("--feed-options-ipc", default=None, help="Feed Options PUB endpoint")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Recordings root dir")
    parser.add_argument("--batch-size", type=int, default=5000, help="Flush batch size")
    parser.add_argument("--flush-interval", type=float, default=300.0, help="Flush interval seconds (default: 300.0 / 5 mins)")
    parser.add_argument("--debug", action="store_true", help="Dump raw ticks to log file")
    parser.add_argument("--debug-log-path", default=None, help="Path to dump raw ticks")
    args = parser.parse_args()

    recorder = MarketRecorder(
        feed_ipc=args.feed_ipc,
        feed_eq_ipc=args.feed_equity_ipc,
        feed_opt_ipc=args.feed_options_ipc,
        output_dir=args.output_dir,
        batch_size=args.batch_size,
        flush_interval_sec=args.flush_interval,
        debug=args.debug if args.debug else None,
        debug_log_path=args.debug_log_path,
    )

    def sig_handler(sig, frame):
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    recorder.start()


if __name__ == "__main__":
    main()
