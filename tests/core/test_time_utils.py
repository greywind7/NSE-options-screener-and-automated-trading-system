#!/usr/bin/env python3
"""
tests/core/test_time_utils.py
Tests for time normalization and market session string comparison guarantees.
"""

import unittest
from datetime import datetime, timezone, timedelta
from core.common.time_utils import normalize_time_str
from core.config.trading_schedule import MarketSchedule


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

    def test_trading_schedule_market_session_with_unpadded_input(self):
        sched = MarketSchedule(
            exchange="NSE",
            market_open_time=normalize_time_str("9:15"),
            market_close_time=normalize_time_str("15:30"),
            midday_freeze_start="11:30",
            midday_freeze_end="13:00",
            live_entry_freeze_time=normalize_time_str("15:15"),
            live_eod_squareoff_time=normalize_time_str("15:20"),
            paper_eod_squareoff_time="15:25",
        )
        self.assertEqual(sched.market_open_time, "09:15")
        self.assertEqual(sched.live_entry_freeze_time, "15:15")
        self.assertEqual(sched.live_eod_squareoff_time, "15:20")

        self.assertFalse(sched.is_market_open("09:10"))
        self.assertTrue(sched.is_market_open("09:30"))
        self.assertTrue(sched.is_live_entry_frozen("15:16"))
        self.assertFalse(sched.is_live_entry_frozen("10:00"))
