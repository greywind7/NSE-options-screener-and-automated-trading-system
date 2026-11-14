#!/usr/bin/env python3
"""
tests/strategies/test_scoring_engine_2_0.py
===========================================
Unit & Integration Tests for Scoring Engine 2.0:
1. Binary Flag Detector (Engine A):
   - Pole establishment (ATR, body >= 70%, RVOL >= 2.0x).
   - 50% Pole Retracement Rule (strict invalidation).
   - Linear regression line breakout.
   - 2.0x Volume rate surge.
   - 6th candle cutoff.
2. Binary Pure Breakout Detector (Engine B):
   - Strict volume criteria & session segmentation.
   - ATR displacement & wick rejection.
3. Unclamped Absolute Alpha Scorer:
   - Proportional burst scaling (zero min() caps).
   - S/R Geometry Asymmetry.
   - Shallow retracement quality.
4. Actionable Setup Pool:
   - Continuous exponential score decay.
   - Priority sorting by current decayed score.
   - Hard 20-second eviction.
5. Macro-Regime Dynamic Trio Allocation:
   - Pro-bull, pro-bear, and rangebound regime allocations.
"""

import math
import time
import unittest

from strategies.momentum.actionable_pool import ActionableSetupPool
from strategies.momentum.patterns import (
    BinaryBreakoutDetector,
    BinaryFlagDetector,
    FlagPatternTracker,
    fit_linear_regression,
)
from strategies.momentum.portfolio import MultiTrackPortfolio
from strategies.momentum.scoring import (
    UnclampedAlphaScorer,
    calculate_setup_score,
    compute_decayed_score,
)


class TestScoringEngine2(unittest.TestCase):

    def test_linear_regression_fitting(self):
        # Downward sloping resistance points: (0, 100), (1, 99), (2, 98)
        pts = [(0.0, 100.0), (1.0, 99.0), (2.0, 98.0)]
        slope, intercept = fit_linear_regression(pts)
        self.assertAlmostEqual(slope, -1.0, places=2)
        self.assertAlmostEqual(intercept, 100.0, places=2)

    def test_binary_flag_pole_and_50pct_invalidation(self):
        detector = BinaryFlagDetector(min_pole_atr_mult=1.5, invalidation_retrace_pct=0.50)
        # Establish bull pole at 100.0 with ATR=2.0 (min disp = 3.0, pole_base = 97.0, height = 3.0)
        # 50% invalidation line = 100.0 - (0.50 * 3.0) = 98.50
        res = detector.update(
            instrument_key="TEST_KEY",
            ltp=100.0,
            rvol_5m=2.5,
            pace=1.3,
            a_vol=1.2,
            atr=2.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=1000.0,
            has_baseline=True,
        )
        self.assertTrue(res["has_bull_pole"])
        self.assertEqual(res["bull_pole_peak"], 100.0)
        self.assertAlmostEqual(res["bull_invalidation"], 98.50, places=2)

        # Retracement to 99.0 (> 98.50) should preserve pole
        res2 = detector.update(
            instrument_key="TEST_KEY",
            ltp=99.0,
            rvol_5m=0.8,
            pace=0.8,
            a_vol=0.7,
            atr=2.0,
            range_pos=90.0,
            is_hod=False,
            is_lod=False,
            now_ts=1060.0,
            has_baseline=True,
        )
        self.assertTrue(res2["has_bull_pole"])

        # Deep retracement to 98.20 (< 98.50 invalidation level) -> MUST INVALIDATE POLE!
        res3 = detector.update(
            instrument_key="TEST_KEY",
            ltp=98.20,
            rvol_5m=0.7,
            pace=0.7,
            a_vol=0.7,
            atr=2.0,
            range_pos=70.0,
            is_hod=False,
            is_lod=False,
            now_ts=1120.0,
            has_baseline=True,
        )
        self.assertFalse(res3["has_bull_pole"])

    def test_binary_flag_breakout_on_volume_surge(self):
        detector = BinaryFlagDetector()
        # 1. Establish Bull Pole at 100.0
        detector.update(
            instrument_key="FLAG_SURGE",
            ltp=100.0,
            rvol_5m=2.5,
            pace=1.3,
            a_vol=1.2,
            atr=2.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=1000.0,
            has_baseline=True,
        )

        # 2. Add consolidation candle: High=99.5, Low=98.8, Volume=1,000 shares over 300s
        completed_candle = {
            "start_ts": 1001.0,
            "end_ts": 1301.0,
            "high": 99.5,
            "low": 98.8,
            "close": 99.2,
            "volume": 1000,
        }
        res_coil = detector.update(
            instrument_key="FLAG_SURGE",
            ltp=99.2,
            rvol_5m=0.8,
            pace=0.7,
            a_vol=0.7,
            atr=2.0,
            range_pos=88.0,
            is_hod=False,
            is_lod=False,
            now_ts=1302.0,
            completed_candle=completed_candle,
            has_baseline=True,
        )
        self.assertFalse(res_coil["bull_breakout"])

        # 3. Breakout tick: Price pierces 100.2 (> 99.5 resistance) on RVOL 2.5 and a_vol 2.2 (Surge >= 2x)
        res_break = detector.update(
            instrument_key="FLAG_SURGE",
            ltp=100.2,
            rvol_5m=2.5,
            pace=1.5,
            a_vol=2.2,
            atr=2.0,
            range_pos=96.0,
            is_hod=True,
            is_lod=False,
            now_ts=1330.0,
            has_baseline=True,
        )
        self.assertTrue(res_break["bull_breakout"])

    def test_binary_flag_6th_candle_cutoff(self):
        detector = BinaryFlagDetector(max_consolidation_candles=5)
        detector.update(
            instrument_key="CUTOFF_TEST",
            ltp=100.0,
            rvol_5m=2.5,
            pace=1.3,
            a_vol=1.2,
            atr=2.0,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=1000.0,
            has_baseline=True,
        )

        # Add 5 consolidation candles (within limit)
        for i in range(5):
            c = {
                "start_ts": 1000.0 + (i * 300.0),
                "end_ts": 1000.0 + ((i + 1) * 300.0),
                "high": 99.5,
                "low": 99.0,
                "close": 99.2,
                "volume": 500,
            }
            res = detector.update(
                instrument_key="CUTOFF_TEST",
                ltp=99.2,
                rvol_5m=0.8,
                pace=0.8,
                a_vol=0.8,
                atr=2.0,
                range_pos=85.0,
                is_hod=False,
                is_lod=False,
                now_ts=1000.0 + ((i + 1) * 300.0),
                completed_candle=c,
                has_baseline=True,
            )
        self.assertTrue(res["has_bull_pole"])

        # Add 6th consolidation candle -> Hard cutoff, pole must expire!
        c6 = {
            "start_ts": 2800.0,
            "end_ts": 3100.0,
            "high": 99.4,
            "low": 99.1,
            "close": 99.3,
            "volume": 500,
        }
        res6 = detector.update(
            instrument_key="CUTOFF_TEST",
            ltp=99.3,
            rvol_5m=0.8,
            pace=0.8,
            a_vol=0.8,
            atr=2.0,
            range_pos=85.0,
            is_hod=False,
            is_lod=False,
            now_ts=3101.0,
            completed_candle=c6,
            has_baseline=True,
        )
        self.assertFalse(res6["has_bull_pole"])

    def test_binary_pure_breakout_detector(self):
        pure_detector = BinaryBreakoutDetector()
        # In early session (1000s in test mode):
        # Candle TR = 4.0 (ATR=2.0 -> TR = 2.0x ATR >= 1.8x ATR)
        # Open=100, High=104, Low=100, LTP=103.8 (Body=3.8/4.0 = 95% >= 70%, Upper wick=0.2/4.0 = 5% <= 15%)
        # RVOL 5m = 2.4, RVOL day = 2.1
        res = pure_detector.evaluate(
            ltp=103.8,
            open_p=100.0,
            high_p=104.0,
            low_p=100.0,
            rvol_5m=2.4,
            rvol_cum=2.1,
            atr=2.0,
            now_ts=1000.0,
            has_baseline=True,
        )
        self.assertTrue(res["is_bull_breakout"])
        self.assertFalse(res["is_bear_breakdown"])

        # Rejection: Upper wick too long (sellers rejected high)
        # High=104, LTP=101.5 (Upper wick = 2.5 / 4.0 = 62.5% > 15%)
        res_wick = pure_detector.evaluate(
            ltp=101.5,
            open_p=100.0,
            high_p=104.0,
            low_p=100.0,
            rvol_5m=2.4,
            rvol_cum=2.1,
            atr=2.0,
            now_ts=1000.0,
            has_baseline=True,
        )
        self.assertFalse(res_wick["is_bull_breakout"])

    def test_unclamped_alpha_scorer_zero_min_ceilings(self):
        scorer = UnclampedAlphaScorer()
        # Setup with massive 6x volume burst, 3% blue sky clearance, tight 10% retrace, tight 0.5% spread
        res = scorer.calculate_alpha(
            ltp=100.0,
            direction="BULL",
            setup_type="FLAG_BREAKOUT",
            burst_ratio=6.0,
            rvol_5m=4.0,
            rvol_cum=3.0,
            z_vol=2.5,
            retrace_pct=10.0,
            tech_indicators={"clearance_pct": 3.0, "extension_pct": 0.05},
            nifty_stats={"regime": "PRO_BULL"},
            option_spread_pct=0.5,
            has_baseline=True,
        )
        # vol_burst: 15 * 6.0 = 90.0
        # rvol_base: 5*3.0 + 3*2.5 = 22.5
        # clearance: 3.0 * 8 = 24.0
        # proximity: 25 - 0.05*20 = 24.0
        # retrace: (1 - 10/50) * 25 = 20.0
        # spread: -5.0
        # regime: +10.0
        # Total = 90 + 22.5 + 24 + 24 + 20 - 5 + 10 = 185.5
        score = res["absolute_score"]
        self.assertGreater(score, 150.0)
        self.assertEqual(res["components"]["vol_burst"], 90.0)

    def test_actionable_pool_continuous_decay_and_20s_eviction(self):
        pool = ActionableSetupPool(expiry_seconds=20.0, decay_power=1.5)
        # Add Candidate A at T=100.0 with S_0=100.0
        pool.add_candidate(
            key="CAND_A",
            symbol="STOCK_A",
            ltp=100.0,
            direction="BULL",
            stage="💥 BULL BREAKOUT",
            setup_type="FLAG_BREAKOUT",
            raw_score=100.0,
            now_ts=100.0,
        )

        # At T=100.0: Decayed score should be 100.0
        sorted_0 = pool.get_sorted_candidates(now_ts=100.0)
        self.assertEqual(len(sorted_0), 1)
        self.assertEqual(sorted_0[0]["decayed_score"], 100.0)

        # At T=110.0 (10s elapsed, half life): Score decayed to ~64.6
        sorted_10 = pool.get_sorted_candidates(now_ts=110.0)
        self.assertAlmostEqual(sorted_10[0]["decayed_score"], 64.64, delta=1.0)

        # Add Candidate B at T=110.0 with fresh S_0=80.0
        pool.add_candidate(
            key="CAND_B",
            symbol="STOCK_B",
            ltp=200.0,
            direction="BULL",
            stage="💥 BULL BREAKOUT",
            setup_type="FLAG_BREAKOUT",
            raw_score=80.0,
            now_ts=110.0,
        )

        # At T=112.0:
        # Candidate A age=12s -> score ~53
        # Candidate B age=2s -> score ~77
        # CAND_B MUST RANK ABOVE CAND_A because CAND_B is fresh!
        sorted_12 = pool.get_sorted_candidates(now_ts=112.0)
        self.assertEqual(sorted_12[0]["key"], "CAND_B")
        self.assertEqual(sorted_12[1]["key"], "CAND_A")

        # At T=121.0 (Candidate A age=21s > 20s):
        # CAND_A MUST BE PURGED from the pool!
        sorted_21 = pool.get_sorted_candidates(now_ts=121.0)
        self.assertEqual(len(sorted_21), 1)
        self.assertEqual(sorted_21[0]["key"], "CAND_B")

    def test_dynamic_trio_macro_regime_allocation(self):
        portfolio = MultiTrackPortfolio()
        # 1. Pro-Bull Regime (Nifty >= +0.35%)
        portfolio.update_dynamic_trio_leaderboard(
            now_ts=1000.0,
            nifty_stats={"regime": "PRO_BULL", "pct_change": 0.45},
        )
        self.assertEqual(portfolio.dynamic_trio_champions, ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "ITM"])
        self.assertEqual(portfolio.dynamic_trio_weights["TRAIL_SL_ITM"], 0.50)
        self.assertEqual(portfolio.dynamic_trio_weights["TRAIL_SL_ATM"], 0.30)
        self.assertEqual(portfolio.dynamic_trio_weights["ITM"], 0.20)

        # 2. Pro-Bear Regime (Nifty <= -0.35%)
        portfolio.update_dynamic_trio_leaderboard(
            now_ts=2000.0,
            nifty_stats={"regime": "PRO_BEAR", "pct_change": -0.50},
        )
        self.assertEqual(portfolio.dynamic_trio_champions, ["TRAIL_SL_ITM", "ITM", "TRAIL_SL_ATM"])
        self.assertEqual(portfolio.dynamic_trio_weights["TRAIL_SL_ITM"], 0.50)
        self.assertEqual(portfolio.dynamic_trio_weights["ITM"], 0.30)
        self.assertEqual(portfolio.dynamic_trio_weights["TRAIL_SL_ATM"], 0.20)


if __name__ == "__main__":
    unittest.main()

