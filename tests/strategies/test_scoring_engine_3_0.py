"""
Unit Tests for Alpha Scoring Engine 3.0.
Verifies physical invariants: 50% Retracement Check first, Pure Impulse Thrust, CLV >= 0.75, rVOL >= 2.0x,
and priority dispatch sorted by rVOL descending.
"""
from datetime import datetime
import os
import time
import unittest
import numpy as np

from core.candles.shm_layout import (
    CANDLE_DTYPE,
    SEQLOCK_DTYPE,
    NUM_SYMBOLS,
    NUM_BARS,
    create_or_attach_shm,
    get_shm_views,
    unlink_shm,
)
from strategies.momentum.scoring_engine import ScoringEngine3


class TestScoringEngine3(unittest.TestCase):
    def setUp(self):
        self.shm_name = "test_scoring_shm"
        self.shm = create_or_attach_shm(name=self.shm_name, create=True)
        self.headers, self.candles = get_shm_views(self.shm)
        self.headers[:] = 0
        self.candles[:] = 0

        self.engine = ScoringEngine3(
            shm_name=self.shm_name,
            pool=None,
            universe_csv="data/filtered.csv",
            enable_zmq=False,
            persist_alerts=False,
        )

    def tearDown(self):
        self.engine.stop()
        self.headers = None
        self.candles = None
        self.shm.close()
        unlink_shm(self.shm_name)
        for p in ["logs/alerts_2026-11-15.jsonl"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def _make_sim_timestamp(self, bar_idx: int, sec_offset: int = 60) -> float:
        """Create timestamp aligned to 09:15 IST market hours."""
        dt_base = datetime(2026, 11, 15, 9, 15, 0)
        base_ts = dt_base.timestamp()
        return base_ts + bar_idx * 300.0 + sec_offset

    def _set_bar(
        self,
        sym_idx: int,
        bar_idx: int,
        ts: float,
        o: float,
        h: float,
        l: float,
        c: float,
        v: int,
        rvol: float = 1.0,
        atr: float = 10.0,
        is_closed: bool = True,
    ) -> None:
        bar = self.candles[sym_idx, bar_idx]
        bar["timestamp"] = int(ts)
        bar["open"] = float(o)
        bar["high"] = float(h)
        bar["low"] = float(l)
        bar["close"] = float(c)
        bar["volume"] = int(v)
        bar["rvol"] = float(rvol)
        bar["atr"] = float(atr)
        bar["is_closed"] = bool(is_closed)

    def test_flag_breakout_50_pct_retracement_invariant(self):
        # Symbol 0 = ABB
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]

        bar_idx = 4
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Setup 4 closed bars for ABB:
        # Bar 0: Pole (Low 5000, High 5200, Close 5180, Volume 10000)
        self._set_bar(sym_idx, 0, now_ts - 1200, 5000.0, 5200.0, 5000.0, 5180.0, 10000, atr=60.0)
        # Bar 1: Contraction 1 (High 5190, Low 5150, Close 5160, Volume 2000) -> Retracement (5200-5150)/200 = 25% <= 50%
        self._set_bar(sym_idx, 1, now_ts - 900, 5180.0, 5190.0, 5150.0, 5160.0, 2000, atr=60.0)
        # Bar 2: Contraction 2 (High 5175, Low 5140, Close 5150, Volume 1800) -> Retracement (5200-5140)/200 = 30% <= 50%
        self._set_bar(sym_idx, 2, now_ts - 600, 5160.0, 5175.0, 5140.0, 5150.0, 1800, atr=60.0)
        # Bar 3: Contraction 3 (High 5165, Low 5145, Close 5160, Volume 1500)
        self._set_bar(sym_idx, 3, now_ts - 300, 5150.0, 5165.0, 5145.0, 5160.0, 1500, atr=60.0)

        # Active Bar 4: Breakout bar!
        # Volume 3000 -> rVOL = 3.0 >= 2.0!
        # Close 5210 > regression line, High 5215, Low 5155 -> CLV = (5210-5155)/(5215-5155) = 55/60 = 0.916 >= 0.75
        self._set_bar(sym_idx, 4, now_ts, 5160.0, 5215.0, 5155.0, 5210.0, 3000, rvol=3.0, atr=60.0, is_closed=False)

        # Trigger evaluation
        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].symbol, symbol)
        self.assertEqual(alerts[0].archetype, "FLAG_BULL_BREAKOUT")
        self.assertGreaterEqual(alerts[0].clv, 0.75)
        self.assertGreaterEqual(alerts[0].rvol, 2.0)

        # Now test 50% Retracement Invariant Failure:
        # If Bar 1 dropped to 5050 (Retracement (5200-5050)/200 = 75% > 50%) -> MUST REJECT!
        self.candles[sym_idx, 1]["low"] = 5050.0
        self.engine.meta[symbol].status = None
        self.engine.meta[symbol].status_bar_idx = -1
        self.engine.meta[symbol].last_evaluated_ts = 0.0

        alerts_failed = self.engine.evaluate_universe(now_ts=now_ts)
        flag_alerts = [a for a in alerts_failed if a.archetype == "FLAG_BULL_BREAKOUT"]
        self.assertEqual(len(flag_alerts), 0)

    def test_pure_impulse_breakout_and_priority_dispatch(self):
        # Symbol 1 and Symbol 2
        sym1 = self.engine.index_to_symbol[1]
        sym2 = self.engine.index_to_symbol[2]

        bar_idx = 4
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # 3 base bars for sym1 (ATR=10, High=1000)
        for b in range(1, 4):
            self._set_bar(1, b, now_ts - (4 - b) * 300, 995.0, 1000.0, 990.0, 998.0, 1000, atr=10.0)

        # Active Bar for sym1:
        # Vol = 2500 -> rVOL = 2.5 >= 2.0
        # Thrust = 1025 - 998 = 27 >= 2.0 * baseline_thrust
        # CLV = (1024 - 998) / (1025 - 998) = 26/27 = 0.963 >= 0.75
        self._set_bar(1, 4, now_ts, 998.0, 1025.0, 998.0, 1024.0, 2500, rvol=2.5, atr=10.0, is_closed=False)

        # 3 base bars for sym2 (ATR=15, High=2000)
        for b in range(1, 4):
            self._set_bar(2, b, now_ts - (4 - b) * 300, 1990.0, 2000.0, 1985.0, 1995.0, 1500, atr=15.0)

        # Active Bar for sym2:
        # Vol = 4800 -> rVOL = 4.8 >= 2.0 (Higher than sym1!)
        # Thrust = 2050 - 1995 = 55 >= 2.0 * baseline_thrust
        # CLV = (2048 - 1995) / (2050 - 1995) = 53/55 = 0.963 >= 0.75
        self._set_bar(2, 4, now_ts, 1995.0, 2050.0, 1995.0, 2048.0, 4800, rvol=4.8, atr=15.0, is_closed=False)

        # Trigger evaluation
        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        self.assertEqual(len(alerts), 2)

        # Priority Dispatch Invariant: Sorted by rVOL descending!
        self.assertEqual(alerts[0].symbol, sym2)  # rVOL = 4.8
        self.assertAlmostEqual(alerts[0].rvol, 4.8, places=2)
        self.assertEqual(alerts[1].symbol, sym1)  # rVOL = 2.5
        self.assertAlmostEqual(alerts[1].rvol, 2.5, places=2)

    def test_dynamic_pole_search_intermediate_pole(self):
        """Test dynamic pole detection when pole starts mid-window (e.g. index 2 in 6-candle history)."""
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]

        bar_idx = 6
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Bar 0, 1: Flat/Choppy pre-pole noise
        self._set_bar(sym_idx, 0, now_ts - 1800, 5000.0, 5010.0, 4990.0, 5000.0, 1000, atr=50.0)
        self._set_bar(sym_idx, 1, now_ts - 1500, 5000.0, 5015.0, 4995.0, 5005.0, 1000, atr=50.0)

        # Bar 2, 3: Dynamic 2-candle high-volume impulse pole (p_start=2, pole_len=2)
        self._set_bar(sym_idx, 2, now_ts - 1200, 5005.0, 5120.0, 5005.0, 5110.0, 12000, atr=50.0)
        self._set_bar(sym_idx, 3, now_ts - 900, 5110.0, 5200.0, 5110.0, 5190.0, 15000, atr=50.0)

        # Bar 4, 5: Low-volume contraction (avg vol = 1900 << pole avg vol 13500)
        self._set_bar(sym_idx, 4, now_ts - 600, 5190.0, 5195.0, 5150.0, 5160.0, 2000, atr=50.0)
        self._set_bar(sym_idx, 5, now_ts - 300, 5160.0, 5175.0, 5140.0, 5155.0, 1800, atr=50.0)

        # Bar 6: Active Breakout Bar
        self._set_bar(sym_idx, 6, now_ts, 5155.0, 5215.0, 5155.0, 5210.0, 3500, rvol=3.5, atr=50.0, is_closed=False)

        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].symbol, symbol)
        self.assertEqual(alerts[0].archetype, "FLAG_BULL_BREAKOUT")
        self.assertEqual(alerts[0].flag_high, 5200.0)
        self.assertEqual(alerts[0].flag_low, 5140.0)
        self.assertGreaterEqual(alerts[0].clv, 0.75)

    def test_min_pole_pct_threshold_filtering(self):
        """Verify that min_pole_pct correctly filters out poles smaller than threshold."""
        # Test 1: Explicit min_pole_pct initialization & normalization
        engine_high_pole = ScoringEngine3(
            shm_name=self.shm_name,
            min_pole_pct=0.08,  # 8.0% minimum pole
            enable_zmq=False,
            persist_alerts=False,
        )
        self.assertEqual(engine_high_pole.min_pole_pct, 0.08)

        engine_pct_normalized = ScoringEngine3(
            shm_name=self.shm_name,
            min_pole_pct=5.0,  # 5.0% passed as percentage (normalized to 0.05)
            enable_zmq=False,
            persist_alerts=False,
        )
        self.assertEqual(engine_pct_normalized.min_pole_pct, 0.05)

        # Set up a 3.9% pole on symbol 0
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 5
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)
        # Bar 1: Pole low=5000, high=5195 -> pole_pct = 195/5000 = 3.9%
        self._set_bar(sym_idx, 1, now_ts - 1200, 5000.0, 5195.0, 5000.0, 5190.0, 10000, atr=50.0)
        self._set_bar(sym_idx, 2, now_ts - 900, 5190.0, 5195.0, 5150.0, 5160.0, 1500, atr=50.0)
        self._set_bar(sym_idx, 3, now_ts - 600, 5160.0, 5175.0, 5140.0, 5155.0, 1200, atr=50.0)
        self._set_bar(sym_idx, 4, now_ts - 300, 5155.0, 5170.0, 5145.0, 5150.0, 1100, atr=50.0)
        self._set_bar(sym_idx, 5, now_ts, 5150.0, 5210.0, 5150.0, 5205.0, 3000, rvol=3.0, atr=50.0, is_closed=False)

        # Engine with 0.5% threshold accepts the 3.9% pole
        engine_low = ScoringEngine3(
            shm_name=self.shm_name,
            min_pole_pct=0.005,
            enable_zmq=False,
            persist_alerts=False,
        )
        alert_low = engine_low._check_flag_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.0, 50.0, now_ts
        )
        self.assertIsNotNone(alert_low)
        self.assertEqual(alert_low.archetype, "FLAG_BULL_BREAKOUT")

        # Engine with 5.0% threshold rejects the 3.9% pole
        engine_high = ScoringEngine3(
            shm_name=self.shm_name,
            min_pole_pct=0.05,
            enable_zmq=False,
            persist_alerts=False,
        )
        alert_high = engine_high._check_flag_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.0, 50.0, now_ts
        )
        self.assertIsNone(alert_high)

    def test_pure_breakout_thrust_real_body_calculation(self):
        """Verify that pure breakout thrust measures absolute real body abs(close - open)."""
        sym_idx = 1
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 4
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # 3 Prior bars are RED candles with significant 10-point body drops:
        # open=1000, high=1001, low=988, close=990 -> abs(close - open) = 10.0
        # Under the old formula (high - open), thrust was only 1.0 (severely depressed).
        for b in range(1, 4):
            self._set_bar(sym_idx, b, now_ts - (4 - b) * 300, 1000.0, 1001.0, 988.0, 990.0, 1000, atr=10.0)

        # Baseline thrust must be max(avg_body=10.0, 0.35 * 10 = 3.5) = 10.0.
        # Required thrust for 2.0x is >= 20.0 points.

        # Case 1: Weak green expansion (body = 12.0 points, close=1002 > combined_high=1001).
        # Thrust 12.0 < 20.0 -> Must be REJECTED (under old code, high-open=14 >= 2.0 * 3.5 would falsely trigger).
        self._set_bar(sym_idx, 4, now_ts, 990.0, 1004.0, 990.0, 1002.0, 3000, rvol=3.0, atr=10.0, is_closed=False)
        alert_weak = self.engine._check_pure_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.0, 10.0, now_ts
        )
        self.assertIsNone(alert_weak)

        # Case 2: Strong green expansion (body = 22.0 points, open=990, close=1012, high=1013).
        # Thrust 22.0 >= 20.0 -> Must TRIGGER.
        self._set_bar(sym_idx, 4, now_ts, 990.0, 1013.0, 990.0, 1012.0, 3000, rvol=3.0, atr=10.0, is_closed=False)
        alert_strong = self.engine._check_pure_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.0, 10.0, now_ts
        )
        self.assertIsNotNone(alert_strong)
        self.assertEqual(alert_strong.archetype, "PURE_BULL_BREAKOUT")

        # Case 3: Red shooting star with big upper wick (open=990, high=1020, close=988).
        # Net body is negative (close < open) -> Must NEVER trigger Bull Impulse.
        self._set_bar(sym_idx, 4, now_ts, 990.0, 1020.0, 985.0, 988.0, 3000, rvol=3.0, atr=10.0, is_closed=False)
        alert_red_wick = self.engine._check_pure_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.0, 10.0, now_ts
        )
        self.assertIsNone(alert_red_wick)

    def test_pole_trimming_contraction_candle(self):
        """
        Verify that a low-volume contraction candle mistakenly grouped into an initial 3-candle
        pole is trimmed from the pole and transferred into contraction, enabling valid flag trigger.
        """
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 4
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Bear Flag scenario:
        # Bar 0: Red thrust 1 (vol 10000)
        self._set_bar(sym_idx, 0, now_ts - 1200, 5000.0, 5005.0, 4900.0, 4910.0, 10000, atr=50.0)
        # Bar 1: Red thrust 2 (vol 12000)
        self._set_bar(sym_idx, 1, now_ts - 900, 4910.0, 4915.0, 4820.0, 4830.0, 12000, atr=50.0)
        # Bar 2: Red contraction candle (vol 2000) - Lower volume than pole candles
        self._set_bar(sym_idx, 2, now_ts - 600, 4830.0, 4840.0, 4810.0, 4820.0, 2000, atr=50.0)
        # Bar 3: Contraction 2 (vol 1800)
        self._set_bar(sym_idx, 3, now_ts - 300, 4820.0, 4835.0, 4815.0, 4830.0, 1800, atr=50.0)

        # Active Bar 4: Breakdown bar
        # Low 4780, High 4835, Close 4785 -> CLV bear = (4835 - 4785) / (4835 - 4780) = 50/55 = 0.909 >= 0.75
        self._set_bar(sym_idx, 4, now_ts, 4830.0, 4835.0, 4780.0, 4785.0, 3500, rvol=3.5, atr=50.0, is_closed=False)

        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].symbol, symbol)
        self.assertEqual(alerts[0].archetype, "FLAG_BEAR_BREAKDOWN")
        self.assertEqual(alerts[0].flag_low, 4820.0)  # Low of trimmed pole (Bar 0-1)

    def test_pole_expansion_earlier_side(self):
        """
        Verify that backward expansion captures an earlier thrust candle if it significantly increases
        pole height (>= 33%) without substantial volume degradation.
        """
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 5
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Bar 0: Noise
        self._set_bar(sym_idx, 0, now_ts - 1500, 5000.0, 5005.0, 4995.0, 5000.0, 1000, atr=50.0)
        # Bar 1: Strong green impulse 1 (height 100, vol 10000)
        self._set_bar(sym_idx, 1, now_ts - 1200, 5000.0, 5100.0, 5000.0, 5095.0, 10000, atr=50.0)
        # Bar 2: Strong green impulse 2 (height 105, vol 11000)
        self._set_bar(sym_idx, 2, now_ts - 900, 5095.0, 5200.0, 5095.0, 5190.0, 11000, atr=50.0)
        # Bar 3: Contraction 1 (vol 2000)
        self._set_bar(sym_idx, 3, now_ts - 600, 5190.0, 5195.0, 5150.0, 5160.0, 2000, atr=50.0)
        # Bar 4: Contraction 2 (vol 1800)
        self._set_bar(sym_idx, 4, now_ts - 300, 5160.0, 5175.0, 5145.0, 5150.0, 1800, atr=50.0)

        # Active Bar 5: Bull Breakout
        self._set_bar(sym_idx, 5, now_ts, 5150.0, 5215.0, 5150.0, 5210.0, 3500, rvol=3.5, atr=50.0, is_closed=False)

        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].symbol, symbol)
        self.assertEqual(alerts[0].archetype, "FLAG_BULL_BREAKOUT")
        # Confirms pole was expanded to include Bar 1 (flag_high = 5200.0)
        self.assertEqual(alerts[0].flag_high, 5200.0)

    def test_pole_volume_less_than_2x_contraction_rejected(self):
        """Verify that a flag setup is strictly rejected if pole average volume is < 2.0x contraction average volume."""
        sym_idx = 0
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 3
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Bar 0: Pole with weak volume (3000)
        self._set_bar(sym_idx, 0, now_ts - 900, 5000.0, 5200.0, 5000.0, 5180.0, 3000, atr=50.0)
        # Bar 1: Contraction 1 (vol 2000)
        self._set_bar(sym_idx, 1, now_ts - 600, 5180.0, 5190.0, 5150.0, 5160.0, 2000, atr=50.0)
        # Bar 2: Contraction 2 (vol 2000) -> contr_vol = 2000. Ratio = 3000 / 2000 = 1.5x (< 2.0x)
        self._set_bar(sym_idx, 2, now_ts - 300, 5160.0, 5175.0, 5140.0, 5150.0, 2000, atr=50.0)

        # Active Bar 3: Breakout
        self._set_bar(sym_idx, 3, now_ts, 5150.0, 5215.0, 5150.0, 5210.0, 3500, rvol=3.5, atr=50.0, is_closed=False)

        # Rejected because 1.5x < 2.0x threshold
        alert_rejected = self.engine._check_flag_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.5, 50.0, now_ts
        )
        self.assertIsNone(alert_rejected)

        # Now increase Bar 0 volume to 5000 -> Ratio = 5000 / 2000 = 2.5x (>= 2.0x) -> Must TRIGGER!
        self.candles[sym_idx, 0]["volume"] = 5000
        alert_accepted = self.engine._check_flag_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 3.5, 50.0, now_ts
        )
        self.assertIsNotNone(alert_accepted)
        self.assertEqual(alert_accepted.archetype, "FLAG_BULL_BREAKOUT")

    def test_pole_within_last_5_candles_blocks_pure_breakout(self):
        """Verify that if a pole was formed within the last 5 candles, pure impulse breakouts are blocked."""
        sym_idx = 1
        symbol = self.engine.index_to_symbol[sym_idx]
        bar_idx = 4
        now_ts = self._make_sim_timestamp(bar_idx, sec_offset=60)

        # Bar 0: Noise
        self._set_bar(sym_idx, 0, now_ts - 1200, 1000.0, 1002.0, 998.0, 1000.0, 1000, rvol=1.0, atr=10.0)
        # Bar 1: Institutional Pole (Height 50 pts = 5%, rvol 4.0 >= 2.0)
        self._set_bar(sym_idx, 1, now_ts - 900, 1000.0, 1050.0, 1000.0, 1048.0, 8000, rvol=4.0, atr=10.0)
        # Bar 2: Contraction pause
        self._set_bar(sym_idx, 2, now_ts - 600, 1048.0, 1052.0, 1040.0, 1042.0, 1000, rvol=1.0, atr=10.0)
        # Bar 3: Contraction pause
        self._set_bar(sym_idx, 3, now_ts - 300, 1042.0, 1045.0, 1038.0, 1040.0, 900, rvol=0.9, atr=10.0)

        # Active Bar 4: Attempts Pure Breakout (breaks above combined high 1052)
        self._set_bar(sym_idx, 4, now_ts, 1040.0, 1060.0, 1040.0, 1058.0, 4000, rvol=4.0, atr=10.0, is_closed=False)

        # Bar 1 was a pole within the last 5 bars (bar_idx 4 - 1 = 3 < 5). Pure breakout MUST be blocked!
        alert_blocked = self.engine._check_pure_breakout(
            symbol, sym_idx, bar_idx, self.candles[sym_idx, bar_idx], 4.0, 10.0, now_ts
        )
        self.assertIsNone(alert_blocked)

        alerts = self.engine.evaluate_universe(now_ts=now_ts)
        pure_alerts = [a for a in alerts if "PURE" in a.archetype]
        self.assertEqual(len(pure_alerts), 0)

        # Now test that when pole is older than 5 bars (e.g. pole at bar 1, evaluating at bar 7), pure breakout succeeds
        bar_idx_far = 7
        now_ts_far = self._make_sim_timestamp(bar_idx_far, sec_offset=60)
        # Bars 2, 3, 4, 5, 6: Base bars without pole characteristics (rvol=1.0, tight range)
        for b in range(2, 7):
            self._set_bar(sym_idx, b, now_ts_far - (7 - b) * 300, 1040.0, 1045.0, 1038.0, 1042.0, 1000, rvol=1.0, atr=10.0)

        # Active Bar 7: Pure breakout from bars 4..6 base
        self._set_bar(sym_idx, 7, now_ts_far, 1042.0, 1070.0, 1042.0, 1068.0, 4000, rvol=4.0, atr=10.0, is_closed=False)

        alert_allowed = self.engine._check_pure_breakout(
            symbol, sym_idx, bar_idx_far, self.candles[sym_idx, bar_idx_far], 4.0, 10.0, now_ts_far
        )
        self.assertIsNotNone(alert_allowed)
        self.assertEqual(alert_allowed.archetype, "PURE_BULL_BREAKOUT")


if __name__ == "__main__":
    unittest.main()
