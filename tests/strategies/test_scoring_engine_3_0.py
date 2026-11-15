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
        )

    def tearDown(self):
        self.engine.stop()
        self.shm.close()
        unlink_shm(self.shm_name)

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


if __name__ == "__main__":
    unittest.main()
