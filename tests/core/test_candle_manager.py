"""
Unit Tests for CandleManager and Strict rVOL Linear Extrapolation.
"""
from datetime import datetime
import time
import unittest
import numpy as np
from core.candles.candle_manager import CandleManager
from core.candles.shm_layout import unlink_shm

TEST_SHM = "test_candle_shm"


class TestCandleManager(unittest.TestCase):
    def setUp(self):
        unlink_shm(TEST_SHM)
        self.mgr = CandleManager(
            sub_ipc="ipc:///tmp/nonexistent_test.ipc",
            shm_name=TEST_SHM,
        )

    def tearDown(self):
        self.mgr.stop()
        unlink_shm(TEST_SHM)

    def test_universe_mapping(self):
        self.assertEqual(len(self.mgr.index_to_symbol), 221)
        self.assertIn("RELIANCE", self.mgr.symbol_to_index)
        self.assertIn("NSE:RELIANCE", self.mgr.symbol_to_index)
        idx = self.mgr.symbol_to_index["RELIANCE"]
        self.assertEqual(self.mgr.symbol_to_index["NSE:RELIANCE"], idx)

    def test_bar_synthesis_and_seqlock(self):
        idx = self.mgr.symbol_to_index["RELIANCE"]
        # Simulate ticks at 09:16:00 IST (1 minute into Bar 0)
        dt = datetime(2026, 11, 15, 9, 16, 0)
        t0 = dt.timestamp()

        self.mgr.on_tick("NSE:RELIANCE", ltp=2500.0, vol=1000, ts=t0)
        self.assertEqual(self.mgr.headers[idx]["sequence_id"], 2)  # seq was 0, now 2 (stable)
        self.assertEqual(self.mgr.candles[idx, 0]["open"], 2500.0)
        self.assertEqual(self.mgr.candles[idx, 0]["high"], 2500.0)
        self.assertEqual(self.mgr.candles[idx, 0]["low"], 2500.0)
        self.assertEqual(self.mgr.candles[idx, 0]["close"], 2500.0)

        # Higher tick at 09:16:10
        self.mgr.on_tick("NSE:RELIANCE", ltp=2520.0, vol=2500, ts=t0 + 10)
        self.assertEqual(self.mgr.headers[idx]["sequence_id"], 4)
        self.assertEqual(self.mgr.candles[idx, 0]["high"], 2520.0)
        self.assertEqual(self.mgr.candles[idx, 0]["close"], 2520.0)
        self.assertEqual(self.mgr.candles[idx, 0]["volume"], 1500)

        # Lower tick at 09:16:20
        self.mgr.on_tick("NSE:RELIANCE", ltp=2490.0, vol=3000, ts=t0 + 20)
        self.assertEqual(self.mgr.headers[idx]["sequence_id"], 6)
        self.assertEqual(self.mgr.candles[idx, 0]["low"], 2490.0)
        self.assertEqual(self.mgr.candles[idx, 0]["close"], 2490.0)
        self.assertGreater(self.mgr.candles[idx, 0]["rvol"], 0.0)
        self.assertGreater(self.mgr.candles[idx, 0]["atr"], 0.0)
        self.assertFalse(self.mgr.candles[idx, 0]["is_closed"])

    def test_rvol_linear_extrapolation(self):
        idx = self.mgr.symbol_to_index["RELIANCE"]
        # Set 5m baseline to 5,000 shares (1,000 shares / minute)
        self.mgr.volume_baseline[idx, 0:5] = 1000.0
        t0 = datetime(2026, 11, 15, 9, 15, 30).timestamp()

        # Bar 0, min 0, sec 30: 1,000 shares in 30 seconds
        # 30s extrapolation -> 1,000 * (60/30) = 2,000 shares expected for min 0
        # Expected baseline for min 0 = 1,000 shares
        # Extrapolated rVOL should be 2.0x!
        self.mgr.bar_start_vol[idx] = 0
        self.mgr.candles[idx, 0]["volume"] = 1000
        self.mgr._get_bar_index = lambda ts: (0, 0, 30)

        rvol = self.mgr.calculate_rvol(idx, t0)
        self.assertAlmostEqual(rvol, 2.0, places=1)


if __name__ == "__main__":
    unittest.main()
