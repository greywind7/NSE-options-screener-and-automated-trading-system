"""
Unit Tests for Dual-Stream Market Recorder Service.
"""
import os
import shutil
import tempfile
import time
import unittest
import polars as pl

from core.recorder.market_recorder import MarketRecorder, TICK_SCHEMA


class TestMarketRecorder(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_recorder_")
        self.recorder = MarketRecorder(
            feed_ipc="inproc://test_feed",
            output_dir=self.test_dir,
            batch_size=5,
            flush_interval_sec=10.0,
        )

    def tearDown(self):
        self.recorder.stop()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_dual_stream_record_and_flush(self):
        # 1. Record 4 equity ticks (less than batch_size 5)
        for i in range(4):
            self.recorder.record_tick({
                "token": "NSE_EQ|INE002A01018",
                "symbol": "RELIANCE",
                "ltp": 2500.0 + i,
                "volume": 100 * (i + 1),
                "bid": 2499.5,
                "ask": 2500.5,
                "bid_qty": 50,
                "ask_qty": 60,
                "exchange_timestamp": 1700000000.0 + i,
                "received_timestamp": 1700000000.05 + i,
            })

        self.assertEqual(len(self.recorder._buffer_eq), 4)
        self.assertEqual(len(self.recorder._buffer_opt), 0)

        # 5th equity tick triggers auto-flush of equity buffer
        self.recorder.record_tick({
            "token": "NSE_EQ|INE002A01018",
            "symbol": "RELIANCE",
            "ltp": 2504.0,
            "volume": 500,
            "bid": 2503.5,
            "ask": 2504.5,
            "bid_qty": 50,
            "ask_qty": 60,
            "exchange_timestamp": 1700000004.0,
            "received_timestamp": 1700000004.05,
        })
        self.assertEqual(len(self.recorder._buffer_eq), 0)

        # 2. Record 5 option depth ticks with 5-level bids/asks arrays
        for i in range(5):
            self.recorder.record_tick({
                "token": "NSE_FO|54321",
                "symbol": "RELIANCE26NOV2500CE",
                "ltp": 45.0 + i,
                "volume": 20 * (i + 1),
                "bid": 44.8,
                "ask": 45.2,
                "bid_qty": 250,
                "ask_qty": 500,
                "bids": [[44.8, 250.0, 2.0], [44.5, 500.0, 3.0]],
                "asks": [[45.2, 500.0, 1.0], [45.5, 750.0, 4.0]],
                "exchange_timestamp": 1700000000.0 + i,
                "received_timestamp": 1700000000.05 + i,
            }, topic=b"TICK.OPT")

        self.assertEqual(len(self.recorder._buffer_opt), 0)

        # Find written parquet files
        eq_files = []
        opt_files = []
        for root, _, filenames in os.walk(self.test_dir):
            for f in filenames:
                if f.startswith("equity_ticks") and f.endswith(".parquet"):
                    eq_files.append(os.path.join(root, f))
                elif f.startswith("option_depth") and f.endswith(".parquet"):
                    opt_files.append(os.path.join(root, f))

        self.assertEqual(len(eq_files), 1)
        self.assertEqual(len(opt_files), 1)

        df_eq = pl.read_parquet(eq_files[0])
        self.assertEqual(len(df_eq), 5)
        self.assertEqual(df_eq["symbol"][0], "RELIANCE")
        self.assertEqual(df_eq["ltp"][0], 2500.0)

        df_opt = pl.read_parquet(opt_files[0])
        self.assertEqual(len(df_opt), 5)
        self.assertEqual(df_opt["symbol"][0], "RELIANCE26NOV2500CE")
        self.assertEqual(df_opt["ltp"][0], 45.0)
        self.assertIn("bids", df_opt.columns)
        self.assertIn("asks", df_opt.columns)
        self.assertEqual(len(df_opt["bids"][0]), 2)
        self.assertEqual(len(df_opt["asks"][0]), 2)
        self.assertEqual(df_opt["bids"][0][0][0], 44.8)
        self.assertEqual(df_opt["asks"][0][0][0], 45.2)

        # 3. Verify appending to existing file on subsequent batch (single file, no fragmentation)
        for i in range(5):
            self.recorder.record_tick({
                "token": "NSE_EQ|INE002A01018",
                "symbol": "RELIANCE",
                "ltp": 2510.0 + i,
                "volume": 200 * (i + 1),
                "bid": 2509.5,
                "ask": 2510.5,
                "bid_qty": 50,
                "ask_qty": 60,
                "exchange_timestamp": 1700000010.0 + i,
                "received_timestamp": 1700000010.05 + i,
            })
        self.recorder.flush_equity()

        # Still strictly 1 equity file, now with 10 appended rows
        eq_files_post = [os.path.join(root, f) for root, _, files in os.walk(self.test_dir) for f in files if f.startswith("equity_ticks") and f.endswith(".parquet")]
        self.assertEqual(len(eq_files_post), 1)
        df_eq_appended = pl.read_parquet(eq_files_post[0])
        self.assertEqual(len(df_eq_appended), 10)

    def test_dual_stream_connections_and_routing(self):
        rec = MarketRecorder(
            feed_ipc=None,
            feed_eq_ipc="inproc://feed_eq_test",
            feed_opt_ipc="inproc://feed_opt_test",
            output_dir=self.test_dir,
            batch_size=10,
        )
        self.assertIsNone(rec.feed_ipc)
        self.assertEqual(rec.feed_eq_ipc, "inproc://feed_eq_test")
        self.assertEqual(rec.feed_opt_ipc, "inproc://feed_opt_test")

        # 1. Routing equity tick without explicit topic
        rec.record_tick({
            "token": "NSE_EQ|INE467B01029",
            "symbol": "TCS",
            "ltp": 3500.0,
            "volume": 1000,
            "exchange_timestamp": 1700000000.0,
        })
        self.assertEqual(len(rec._buffer_eq), 1)
        self.assertEqual(len(rec._buffer_opt), 0)

        # 2. Routing option tick by trading symbol without explicit topic
        rec.record_tick({
            "token": "NSE_FO|12345",
            "symbol": "TCS26NOV3500PE",
            "ltp": 85.0,
            "volume": 200,
            "exchange_timestamp": 1700000000.0,
        })
        self.assertEqual(len(rec._buffer_eq), 1)
        self.assertEqual(len(rec._buffer_opt), 1)
        rec.stop()

    def test_raw_tick_debug_mode(self):
        debug_log = os.path.join(self.test_dir, "test_raw_ticks.log")
        rec = MarketRecorder(
            feed_ipc="inproc://test_debug_feed",
            output_dir=self.test_dir,
            debug=True,
            debug_log_path=debug_log,
        )
        rec.record_tick({
            "token": "NSE_EQ|INE002A01018",
            "symbol": "RELIANCE",
            "ltp": 2550.0,
            "volume": 1200,
            "bid": 2549.5,
            "ask": 2550.5,
        }, topic=b"TICK.EQ")
        rec.stop()

        self.assertTrue(os.path.exists(debug_log))
        with open(debug_log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("RELIANCE", lines[0])
        self.assertIn("TICK.EQ", lines[0])


if __name__ == "__main__":
    unittest.main()
