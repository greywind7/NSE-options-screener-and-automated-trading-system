"""
Historical Replay Ingestor: Replays recorded parquet tick streams into the system.
Preserves exchange_timestamp and received_timestamp (HIGH-007).
Supports speed multiplier (0 = as fast as possible for sim/tests, 1.0 = real-time).
"""
import argparse
import glob
import os
import signal
import sys
import time
from typing import Any, Dict, Generator, List, Optional
import pyarrow.parquet as pq
import zmq

from core.common.symbol_utils import is_option_contract, normalize_instrument_token
from core.common.ipc_schemas import pack_message

DEFAULT_FEED_IPC = "ipc:///tmp/feed_ticks_pub.ipc"


class HistoricalReplayIngestor:
    def __init__(
        self,
        data_path: str,
        feed_ipc: str = DEFAULT_FEED_IPC,
        speed: float = 0.0,
        ctx: Optional[zmq.Context] = None,
    ):
        self.data_path = data_path
        self.feed_ipc = feed_ipc
        self.speed = speed
        self.running = False

        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.SNDHWM, 500000)
        self.pub.bind(self.feed_ipc)

    def _collect_files(self) -> List[str]:
        if os.path.isfile(self.data_path):
            return [self.data_path]
        if os.path.isdir(self.data_path):
            pq_files = sorted(glob.glob(os.path.join(self.data_path, "**/*.parquet"), recursive=True))
            if pq_files:
                return pq_files
            jsonl_files = sorted(glob.glob(os.path.join(self.data_path, "**/*.jsonl"), recursive=True))
            if jsonl_files:
                return jsonl_files
        return []

    def stream_ticks(self) -> Generator[Dict[str, Any], None, None]:
        files = self._collect_files()
        if not files:
            print(f"[REPLAY] No parquet or jsonl recording files found at: {self.data_path}", file=sys.stderr)
            return

        for filepath in files:
            try:
                import polars as pl
                if filepath.endswith(".parquet"):
                    df = pl.read_parquet(filepath)
                else:
                    df = pl.read_ndjson(filepath)

                if "token" not in df.columns and "instrument_key" in df.columns:
                    df = df.with_columns(pl.col("instrument_key").alias("token"))

                if "exchange_timestamp" in df.columns:
                    df = df.sort("exchange_timestamp")

                for row in df.iter_rows(named=True):
                    token = normalize_instrument_token(str(row.get("token", "")))
                    symbol = str(row.get("symbol", ""))
                    ltp = float(row.get("ltp", 0.0))
                    vol = int(row.get("volume", 0))
                    bid = float(row.get("bid", ltp))
                    ask = float(row.get("ask", ltp))
                    bid_qty = int(row.get("bid_qty", 0))
                    ask_qty = int(row.get("ask_qty", 0))
                    bids = row.get("bids") or []
                    asks = row.get("asks") or []
                    # HIGH-007: Preserve recorded timestamps
                    ex_ts = float(row.get("exchange_timestamp", 0.0))
                    rec_ts = float(row.get("received_timestamp", ex_ts))

                    yield {
                        "token": token,
                        "symbol": symbol,
                        "ltp": ltp,
                        "volume": vol,
                        "bid": bid,
                        "ask": ask,
                        "bid_qty": bid_qty,
                        "ask_qty": ask_qty,
                        "bids": bids,
                        "asks": asks,
                        "exchange_timestamp": ex_ts,
                        "received_timestamp": rec_ts,
                    }
            except Exception as e:
                print(f"[REPLAY] Error reading file {filepath}: {e}", file=sys.stderr)

    def run(self) -> int:
        """Stream ticks to PUB socket. Returns total ticks published."""
        self.running = True
        print(f"[REPLAY] Starting playback from: {self.data_path} -> {self.feed_ipc} (Speed={self.speed})")
        time.sleep(0.1)  # Allow subscriber discovery

        published_count = 0
        last_ex_ts: Optional[float] = None
        start_real = time.time()

        for tick in self.stream_ticks():
            if not self.running:
                break

            token = tick["token"]
            topic = b"TICK.OPT" if is_option_contract(token) else b"TICK.EQ"
            payload = pack_message(tick)
            self.pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
            published_count += 1

            if self.speed > 0:
                current_ex_ts = tick.get("exchange_timestamp", 0.0)
                if last_ex_ts is not None and current_ex_ts > last_ex_ts:
                    delay = (current_ex_ts - last_ex_ts) / self.speed
                    if delay > 0 and delay < 10.0:  # Cap max gap sleep at 10s
                        time.sleep(delay)
                last_ex_ts = current_ex_ts

        elapsed = time.time() - start_real
        rate = published_count / max(elapsed, 0.001)
        print(f"[REPLAY] Completed playback: {published_count} ticks in {elapsed:.2f}s ({rate:.0f} ticks/s)")
        return published_count

    def stop(self) -> None:
        self.running = False
        try:
            self.pub.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        print("[REPLAY] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Historical Replay Ingestor")
    parser.add_argument("--data-path", required=True, help="Path to recording parquet file or directory")
    parser.add_argument("--feed-ipc", default=DEFAULT_FEED_IPC, help="ZeroMQ feed PUB address")
    parser.add_argument("--speed", type=float, default=0.0, help="Replay speed multiplier (0 = unlimited)")
    args = parser.parse_args()

    replay = HistoricalReplayIngestor(data_path=args.data_path, feed_ipc=args.feed_ipc, speed=args.speed)

    def sig_handler(sig, frame):
        replay.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    replay.run()


if __name__ == "__main__":
    main()
