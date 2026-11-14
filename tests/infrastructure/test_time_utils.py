#!/usr/bin/env python3
"""
tests/infrastructure/test_time_utils.py
Tests for time normalization and market session string comparison guarantees.
"""

import unittest
from datetime import datetime, timezone, timedelta
from infrastructure.common.time_utils import normalize_time_str
from strategies.momentum.risk import RiskManager


class TestTimeUtils(unittest.TestCase):
    def test_normalize_time_str(self):
        self.assertEqual(normalize_time_str("9:15"), "09:15")
        self.assertEqual(normalize_time_str("09:15"), "09:15")
        self.assertEqual(normalize_time_str("9:15:00"), "09:15")
        self.assertEqual(normalize_time_str("9"), "09:00")
        self.assertEqual(normalize_time_str("15:15"), "15:15")
        self.assertEqual(normalize_time_str("11:00"), "11:00")
        self.assertEqual(normalize_time_str(None, "09:15"), "09:15")
        self.assertEqual(normalize_time_str("", "09:15"), "09:15")
        self.assertEqual(normalize_time_str("invalid", "09:15"), "09:15")

    def test_risk_manager_market_session_with_unpadded_input(self):
        # Passing '9:15' should be safely normalized to '09:15'
        rm = RiskManager(market_open_time="9:15", eod_freeze_time="15:15", eod_square_off_time="15:20")
        self.assertEqual(rm.market_open_time, "09:15")
        self.assertEqual(rm.eod_freeze_time, "15:15")
        self.assertEqual(rm.eod_square_off_time, "15:20")

        ist = timezone(timedelta(hours=5, minutes=30))
        # 09:10 IST on a Wednesday (2026-11-11)
        dt_pre = datetime(2026, 11, 11, 9, 10, 0, tzinfo=ist).timestamp()
        sess, desc = rm.get_market_session(dt_pre)
        self.assertEqual(sess, "PRE_MARKET_CAS")
        allowed, _ = rm.is_entry_allowed(dt_pre)
        self.assertFalse(allowed)

        # 09:30 IST on a Wednesday
        dt_open = datetime(2026, 11, 11, 9, 30, 0, tzinfo=ist).timestamp()
        sess, desc = rm.get_market_session(dt_open)
        self.assertEqual(sess, "NORMAL_TRADING")
        allowed, _ = rm.is_entry_allowed(dt_open)
        self.assertTrue(allowed)

        # 10:45 IST on a Wednesday
        dt_mid = datetime(2026, 11, 11, 10, 45, 0, tzinfo=ist).timestamp()
        sess, desc = rm.get_market_session(dt_mid)
        self.assertEqual(sess, "NORMAL_TRADING")
        allowed, _ = rm.is_entry_allowed(dt_mid)
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()

