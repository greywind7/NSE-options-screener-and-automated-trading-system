#!/usr/bin/env python3
"""
tests/strategies/test_time_of_day_gates.py
=========================================
Unit tests for the Time-of-Day Multi-Tier Gating Matrix across:
- OPENING (09:15 - 10:00 IST, peak 100% aggressiveness)
- MORNING (10:00 - 11:00 IST, 75% aggressiveness)
- MIDDAY (11:00 - 13:00 IST, 25% aggressiveness - strict anti-chop/lull gate)
- AFTERNOON (13:00 - 15:00 IST, 50% aggressiveness)
- LATE (15:00 - 15:30 IST)
"""

import datetime
import unittest

from strategies.momentum.patterns import (
    DEFAULT_TIME_TIER_GATES,
    BinaryBreakoutDetector,
    BinaryFlagDetector,
    FlagPatternTracker,
    get_time_of_day_tier,
    get_time_tier_gate,
)


def make_ist_timestamp(hour: int, minute: int, second: int = 0) -> float:
    """Helper to create a deterministic UTC epoch timestamp for given IST time (2026-11-09)."""
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    dt = datetime.datetime(2026, 11, 9, hour, minute, second, tzinfo=ist_tz)
    return dt.timestamp()


class TestTimeOfDayGates(unittest.TestCase):
    def test_tier_classification_and_backward_compatibility(self):
        """Test time tier resolution across all market hours and synthetic timestamps."""
        # Synthetic timestamps (< 1e9) must default to OPENING for test safety
        self.assertEqual(get_time_of_day_tier(0.0), "OPENING")
        self.assertEqual(get_time_of_day_tier(500.0), "OPENING")
        self.assertEqual(get_time_of_day_tier(999_999_999.0), "OPENING")

        # Live IST timestamps
        ts_0930 = make_ist_timestamp(9, 30)
        self.assertEqual(get_time_of_day_tier(ts_0930), "OPENING")

        ts_1030 = make_ist_timestamp(10, 30)
        self.assertEqual(get_time_of_day_tier(ts_1030), "MORNING")

        ts_1130 = make_ist_timestamp(11, 30)
        self.assertEqual(get_time_of_day_tier(ts_1130), "MIDDAY")

        ts_1245 = make_ist_timestamp(12, 45)
        self.assertEqual(get_time_of_day_tier(ts_1245), "MIDDAY")

        ts_1330 = make_ist_timestamp(13, 30)
        self.assertEqual(get_time_of_day_tier(ts_1330), "AFTERNOON")

        ts_1510 = make_ist_timestamp(15, 10)
        self.assertEqual(get_time_of_day_tier(ts_1510), "LATE")

    def test_engine_a_flag_midday_tightening(self):
        """
        Verify Engine A (BinaryFlagDetector):
        - In OPENING (09:30): 1 candle coiling and 50% retrace passes.
        - In MIDDAY (11:30): 1 candle coiling is blocked (requires >= 2 bars).
        - In MIDDAY (11:30): 45% retracement is blocked (max 35% allowed in lull).
        - In MIDDAY (11:30): 2 bars, 20% retrace, 3.2x burst passes.
        """
        det = BinaryFlagDetector()
        ts_open = make_ist_timestamp(9, 30)
        ts_mid = make_ist_timestamp(11, 30)

        # 1. Opening: establish pole and coil for 1 candle
        det.update(
            instrument_key="TEST_ASSET",
            ltp=100.0,
            rvol_5m=2.5,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=90.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_open,
        )
        c1 = {"start_ts": ts_open + 10, "end_ts": ts_open + 310, "high": 100.2, "low": 99.5, "close": 100.0, "volume": 1000}
        res_open = det.update(
            instrument_key="TEST_ASSET",
            ltp=100.5,  # price breaks out
            rvol_5m=2.2,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_open + 320,
            completed_candle=c1,
        )
        self.assertTrue(res_open["bull_breakout"], "Expected breakout at OPENING with 1 coiling candle")

        # 2. Midday with 1 coiling candle -> blocked (needs 2)
        det_mid = BinaryFlagDetector()
        det_mid.update(
            instrument_key="TEST_MID",
            ltp=100.0,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=90.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid,
        )
        c_mid_1 = {"start_ts": ts_mid + 10, "end_ts": ts_mid + 310, "high": 100.1, "low": 99.8, "close": 100.0, "volume": 1000}
        res_mid_1c = det_mid.update(
            instrument_key="TEST_MID",
            ltp=100.5,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid + 320,
            completed_candle=c_mid_1,
        )
        self.assertFalse(res_mid_1c["bull_breakout"], "Expected 1 coiling candle to be rejected in MIDDAY lull")

        # 3. Midday with 2 candles but deep retrace (45% > 35% cap) -> blocked
        c_mid_2_deep = {"start_ts": ts_mid + 320, "end_ts": ts_mid + 620, "high": 100.1, "low": 99.3, "close": 99.5, "volume": 1000}
        res_mid_deep = det_mid.update(
            instrument_key="TEST_MID",
            ltp=100.5,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid + 630,
            completed_candle=c_mid_2_deep,
        )
        self.assertFalse(res_mid_deep["bull_breakout"], "Expected deep retracement (45%) to be rejected in MIDDAY lull")

        # 4. Midday with 2 tight candles (retrace <= 35%) and high surge -> PASS
        det_mid_ok = BinaryFlagDetector()
        det_mid_ok.update(
            instrument_key="TEST_MID_OK",
            ltp=100.0,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=90.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid,
        )
        c_ok_1 = {"start_ts": ts_mid + 10, "end_ts": ts_mid + 310, "high": 100.1, "low": 99.8, "close": 100.0, "volume": 1000}
        det_mid_ok.update(
            instrument_key="TEST_MID_OK",
            ltp=100.0,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=90.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid + 320,
            completed_candle=c_ok_1,
        )
        c_ok_2 = {"start_ts": ts_mid + 330, "end_ts": ts_mid + 630, "high": 100.0, "low": 99.85, "close": 100.0, "volume": 1000}
        res_ok = det_mid_ok.update(
            instrument_key="TEST_MID_OK",
            ltp=100.5,
            rvol_5m=3.8,
            pace=1.5,
            a_vol=1.5,
            atr=1.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid + 640,
            completed_candle=c_ok_2,
        )
        self.assertTrue(res_ok["bull_breakout"], "Expected tight 2-candle flag breakout with high RVOL to pass in MIDDAY")

    def test_engine_b_pure_breakout_time_tier_filtering(self):
        """
        Verify Engine B (BinaryBreakoutDetector):
        - In OPENING (09:30): RVOL_5m = 2.2x, upper wick = 20% passes (wick <= 25% allowed).
        - In MIDDAY (11:30): RVOL_5m = 2.2x is blocked (requires >= 3.5x).
        - In MIDDAY (11:30): RVOL_5m = 3.8x, upper wick = 14% is blocked (midday wick <= 10%).
        - In MIDDAY (11:30): RVOL_5m = 3.8x, upper wick = 8%, body = 80% passes cleanly!
        """
        det = BinaryBreakoutDetector()
        ts_open = make_ist_timestamp(9, 30)
        ts_mid = make_ist_timestamp(11, 30)

        # 1. Opening: RVOL 2.2x, 20% wick (span=2.0, open=100, high=102, low=100, ltp=101.6 -> body=1.6 (80%), wick=0.4 (20%))
        res_open = det.evaluate(
            ltp=101.6,
            open_p=100.0,
            high_p=102.0,
            low_p=100.0,
            rvol_5m=2.2,
            rvol_cum=2.2,
            atr=1.0,
            now_ts=ts_open,
        )
        self.assertTrue(res_open["is_bull_breakout"], "Expected 20% wick to be accepted during OPENING volatility")

        # 2. Midday: RVOL 2.2x -> blocked by 3.5x RVOL gate
        res_mid_rvol = det.evaluate(
            ltp=101.6,
            open_p=100.0,
            high_p=102.0,
            low_p=100.0,
            rvol_5m=2.2,
            rvol_cum=2.2,
            atr=1.0,
            now_ts=ts_mid,
        )
        self.assertFalse(res_mid_rvol["is_bull_breakout"], "Expected RVOL 2.2x to be rejected during MIDDAY lull (requires 3.5x)")

        # 3. Midday: RVOL 3.8x, but upper wick 14% -> blocked by 10% wick filter
        # span=2.0, open=100, high=102, low=100, ltp=101.72 -> wick=0.28 (14%)
        res_mid_wick = det.evaluate(
            ltp=101.72,
            open_p=100.0,
            high_p=102.0,
            low_p=100.0,
            rvol_5m=3.8,
            rvol_cum=2.0,
            atr=1.0,
            now_ts=ts_mid,
        )
        self.assertFalse(res_mid_wick["is_bull_breakout"], "Expected 14% wick to be rejected during MIDDAY lull (requires <= 10%)")

        # 4. Midday: RVOL 3.8x, upper wick 6%, body 85% -> PASS
        # span=2.0, open=100, high=102, low=100, ltp=101.88 -> wick=0.12 (6%), body=1.88 (94%)
        res_mid_pass = det.evaluate(
            ltp=101.88,
            open_p=100.0,
            high_p=102.0,
            low_p=100.0,
            rvol_5m=3.8,
            rvol_cum=2.0,
            atr=1.0,
            now_ts=ts_mid,
        )
        self.assertTrue(res_mid_pass["is_bull_breakout"], "Expected pristine midday breakout (RVOL 3.8x, wick 6%) to pass")

    def test_flag_pattern_tracker_unified_time_tiers(self):
        """
        Verify FlagPatternTracker across time tiers:
        - In MIDDAY (11:30): requires higher volume and minimum coiling cycles.
        """
        tracker = FlagPatternTracker()
        ts_open = make_ist_timestamp(9, 30)
        ts_mid = make_ist_timestamp(11, 30)

        # Baseline pole at opening
        p_open = tracker.update(
            instrument_key="INFY",
            ltp=100.0,
            rvol_5m=2.0,
            rvol_cum=2.0,
            a_vol=1.2,
            pace=1.2,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_open,
        )
        self.assertTrue(p_open["has_bull_pole"])

        # Midday evaluation requires rvol >= 3.5
        p_mid = tracker.update(
            instrument_key="INFY_MID",
            ltp=100.0,
            rvol_5m=2.0,
            rvol_cum=2.0,
            a_vol=1.1,
            pace=1.1,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=ts_mid,
        )
        # INFY_MID pole is established, but won't trigger breakout unless RVOL >= 3.5
        self.assertFalse(p_mid["bull_breakout"])


    def test_sr_clearance_time_tier_gating(self):
        """
        Verify S/R clearance gating:
        - In OPENING (09:30): min_sr_clearance is 0.20% (0.25% clearance passes).
        - In MIDDAY (11:30): min_sr_clearance is 0.50% (0.25% clearance is blocked).
        - In MIDDAY (11:30): clearance >= 0.50% (or HOD breached) passes.
        """
        from strategies.momentum.indicators import compute_support_resistance_levels

        gate_open = get_time_tier_gate(make_ist_timestamp(9, 30))
        gate_mid = get_time_tier_gate(make_ist_timestamp(11, 30))

        self.assertEqual(gate_open["min_sr_clearance_pct"], 0.20)
        self.assertEqual(gate_mid["min_sr_clearance_pct"], 0.50)

        # Setup where next resistance is at 100.40 (clearance = 0.40% from 100.0)
        # high=100.40, low=99.0, close=100.0, ltp=100.0 -> pivot=99.80, r1=100.60, next_res=100.40
        sr = compute_support_resistance_levels(high=100.40, low=99.0, close=100.0, ltp=100.0)
        cand_clearance = sr["clearance_pct"]  # 0.40%
        self.assertAlmostEqual(cand_clearance, 0.40, places=2)
        self.assertFalse(sr["hod_breached"])

        # Check against OPENING gate (min 0.20%): 0.40% >= 0.20% -> PASS
        is_blocked_open = (cand_clearance < gate_open["min_sr_clearance_pct"]) and not sr["hod_breached"]
        self.assertFalse(is_blocked_open, "Expected 0.40% clearance to pass OPENING gate (min 0.20%)")

        # Check against MIDDAY gate (min 0.50%): 0.40% < 0.50% -> BLOCKED
        is_blocked_mid = (cand_clearance < gate_mid["min_sr_clearance_pct"]) and not sr["hod_breached"]
        self.assertTrue(is_blocked_mid, "Expected 0.40% clearance to be blocked in MIDDAY lull (min 0.50%)")

        # When HOD is breached, open blue sky -> NOT blocked in midday
        sr_hod = compute_support_resistance_levels(high=100.0, low=99.0, close=99.5, ltp=100.01)
        self.assertTrue(sr_hod["hod_breached"])
        is_blocked_hod = (sr_hod["clearance_pct"] < gate_mid["min_sr_clearance_pct"]) and not sr_hod["hod_breached"]
        self.assertFalse(is_blocked_hod, "Expected HOD breach to pass cleanly in MIDDAY")


    def test_midday_freeze_risk_guard(self):
        """
        Verify RiskManager entry freeze between 11:00 and 13:30 IST:
        - 10:30 IST: entry allowed (True)
        - 11:30 IST: entry blocked (False, MIDDAY_LULL_FREEZE)
        - 13:15 IST: entry blocked (False, MIDDAY_LULL_FREEZE)
        - 13:45 IST: entry allowed (True)
        - Synthetic timestamps (< 1e9): entry allowed (True)
        """
        from strategies.momentum.risk import RiskManager
        rm = RiskManager(
            midday_freeze_start="11:00",
            midday_freeze_end="13:30",
            midday_freeze_enabled=True,
        )

        # 10:30 IST
        allowed_1030, _ = rm.is_entry_allowed(make_ist_timestamp(10, 30))
        self.assertTrue(allowed_1030)

        # 11:30 IST
        allowed_1130, reason_1130 = rm.is_entry_allowed(make_ist_timestamp(11, 30))
        self.assertFalse(allowed_1130)
        self.assertIn("MIDDAY_LULL_FREEZE", reason_1130)

        # 13:15 IST
        allowed_1315, reason_1315 = rm.is_entry_allowed(make_ist_timestamp(13, 15))
        self.assertFalse(allowed_1315)
        self.assertIn("MIDDAY_LULL_FREEZE", reason_1315)

        # 13:45 IST
        allowed_1345, _ = rm.is_entry_allowed(make_ist_timestamp(13, 45))
        self.assertTrue(allowed_1345)

        # Synthetic timestamp (< 1e9)
        allowed_synth, _ = rm.is_entry_allowed(500.0)
        self.assertTrue(allowed_synth)


if __name__ == "__main__":
    unittest.main()
