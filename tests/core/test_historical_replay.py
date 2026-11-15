"""
Unit Tests for Historical Replay Ingestor.
"""
import os
import shutil
import tempfile
import time
import unittest
import pyarrow as pa
import pyarrow.parquet as pq
import zmq

from core.feed.historical_replay import HistoricalReplayIngestor
from core.recorder.market_recorder import TICK_SCHEMA
from core.common.ipc_schemas import unpack_message


class TestHistoricalReplay(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_replay_")
        self.parquet_path = os.path.join(self.test_dir, "ticks.parquet")

        # Create sample ticks with explicit historical timestamps
        ticks = [
            {
                "token": "NSE:RELIANCE",
                "symbol": "RELIANCE",
                "ltp": 2500.0,
                "volume": 100,
                "bid": 2499.5,
                "ask": 2500.5,
                "bid_qty": 50,
                "ask_qty": 60,
                "exchange_timestamp": 1700000001.0,
                "received_timestamp": 1700000001.025,
            },
            {
                "token": "NSE:NIFTY26NOV24000CE",
                "symbol": "NIFTY",
                "ltp": 150.0,
                "volume": 250,
                "bid": 149.8,
                "ask": 150.2,
                "bid_qty": 150,
                "ask_qty": 200,
                "exchange_timestamp": 1700000002.0,
                "received_timestamp": 1700000002.015,
            },
        ]
        import polars as pl
        df = pl.DataFrame(ticks)
        df.write_parquet(self.parquet_path)

        self.ctx = zmq.Context()
        self.endpoint = "inproc://test_replay_feed"

    def tearDown(self):
        self.ctx.destroy(linger=0)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_replay_preserves_timestamps_and_topics(self):
        sub = self.ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(self.endpoint)

        replay = HistoricalReplayIngestor(
            data_path=self.parquet_path,
            feed_ipc=self.endpoint,
            speed=0.0,
            ctx=self.ctx,
        )

        # Run replay
        count = replay.run()
        self.assertEqual(count, 2)

        # Receive first tick
        topic1, payload1 = sub.recv_multipart()
        self.assertEqual(topic1, b"TICK.EQ")
        t1 = unpack_message(payload1)
        self.assertEqual(t1["token"], "NSE:RELIANCE")
        # Check HIGH-007: timestamps preserved exactly
        self.assertEqual(t1["exchange_timestamp"], 1700000001.0)
        self.assertEqual(t1["received_timestamp"], 1700000001.025)

        # Receive second tick
        topic2, payload2 = sub.recv_multipart()
        self.assertEqual(topic2, b"TICK.OPT")
        t2 = unpack_message(payload2)
        self.assertEqual(t2["token"], "NSE:NIFTY26NOV24000CE")
        self.assertEqual(t2["exchange_timestamp"], 1700000002.0)
        self.assertEqual(t2["received_timestamp"], 1700000002.015)

        sub.close(linger=0)
        replay.stop()


if __name__ == "__main__":
    unittest.main()
