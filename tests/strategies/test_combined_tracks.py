#!/usr/bin/env python3
"""
tests/strategies/test_combined_tracks.py
========================================
Unit tests for Combined Allocation Tracks:
1. Equal capital allocation across all constituent tracks (ITM, OTM, ATM, LOW_PREM, FIXED_TARGET, TRAILING_SL).
2. 40K Minimum Threshold Disablement: If capital per constituent < 40,000 INR, track is disabled.
3. Sub-track trade execution and proper sub_track attribution on TradeRecord.
4. Independent risk evaluation honoring each constituent's specific exit rules.
5. Non-compounding vs Compounding capital recycling for combined tracks.
"""

import os
import sys
import unittest
from dataclasses import dataclass

os.environ["TRADING_TEST_MODE"] = "1"

from strategies.momentum.portfolio import (
    ALL_COMBINED_TRACKS,
    ALL_SIM_TRACKS,
    COMBINED_STRATEGY_TRACKS,
    COMBINED_TRACK_CONSTITUENTS,
    MIN_COMBINED_SUBTRACK_CAPITAL,
    MultiTrackPortfolio,
    StrategyTrackAccounting,
    TradeRecord,
)


@dataclass
class MockOptionContract:
    instrument_key: str = "NSE_FO|TCS26NOV3500CE"
    trading_symbol: str = "TCS26NOV3500CE"
    lot_size: int = 100


class TestCombinedAllocationTracks(unittest.TestCase):

    def test_01_constituents_definition(self):
        """Verify constituent tracks for each combined family."""
        self.assertEqual(COMBINED_TRACK_CONSTITUENTS["COMBINED_ITM"], ["ITM", "TRAIL_SL_ITM", "FIXED_TGT_ITM"])
        self.assertEqual(COMBINED_TRACK_CONSTITUENTS["COMBINED_OTM"], ["OTM", "TRAIL_SL_OTM", "FIXED_TGT_OTM"])
        self.assertEqual(COMBINED_TRACK_CONSTITUENTS["COMBINED_ATM"], ["ATM", "TRAIL_SL_ATM", "FIXED_TGT_ATM"])
        self.assertEqual(
            COMBINED_TRACK_CONSTITUENTS["COMBINED_FIXED_TARGET"],
            ["FIXED_TGT_ATM", "FIXED_TGT_ITM", "FIXED_TGT_OTM"],
        )
        self.assertEqual(
            COMBINED_TRACK_CONSTITUENTS["COMBINED_TRAILING_SL"],
            ["TRAIL_SL_ATM", "TRAIL_SL_ITM", "TRAIL_SL_OTM"],
        )
        self.assertNotIn("COMBINED_LOW_PREM", COMBINED_TRACK_CONSTITUENTS)

    def test_02_below_40k_disablement(self):
        """Verify that when capital per constituent < 40k, the combined track is disabled."""
        # 100k starting capital:
        # COMBINED_ITM: 100k / 3 = 33.3k < 40k -> disabled!
        # COMBINED_ATM: 100k / 3 = 33.3k < 40k -> disabled!
        port_low = MultiTrackPortfolio(
            starting_capital=100_000.0,
            track_filter="COMBINED",
            output_csv="/tmp/test_combined_low.csv",
        )
        self.assertEqual(len(port_low.tracks), 0, "All combined tracks must be disabled if sub-track cap < 40k")

        # Even with ALL filter, combined tracks must not be enabled when capital < 40k
        port_all_low = MultiTrackPortfolio(
            starting_capital=100_000.0,
            track_filter="ALL",
            output_csv="/tmp/test_combined_all_low.csv",
        )
        for ct in COMBINED_STRATEGY_TRACKS:
            self.assertNotIn(ct, port_all_low.tracks)

    def test_03_above_40k_equal_allocation(self):
        """Verify equal capital allocation when starting capital is sufficient (>= 40k per constituent)."""
        # 240k starting capital:
        # ITM (3 tracks): 80k each >= 40k -> Active
        # ATM (3 tracks): 80k each >= 40k -> Active
        port = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM",
            output_csv="/tmp/test_combined_itm.csv",
        )
        self.assertIn("COMBINED_ITM", port.tracks)
        acct = port.tracks["COMBINED_ITM"]
        self.assertEqual(len(acct.constituents), 3)
        self.assertEqual(acct.starting_capital, 240_000.0)
        self.assertEqual(acct.cash, 240_000.0)

        expected_sub_cap = 240_000.0 / 3.0
        for c in acct.constituents:
            self.assertEqual(acct.sub_starting_capital[c], expected_sub_cap)
            self.assertEqual(acct.sub_cash[c], expected_sub_cap)

    def test_04_trade_execution_across_subtracks(self):
        """Verify opening positions creates separate trade records for each constituent sub-track."""
        port = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM",
            output_csv="/tmp/test_combined_exec.csv",
        )
        c = MockOptionContract()
        quotes = {
            c.instrument_key: {"bid": 100.0, "ask": 100.0, "last_price": 100.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }
        contracts_by_mode = {
            "ITM": c,
        }

        opened = port.open_position(
            symbol="TCS",
            contract=c,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes,
        )

        # 3 constituent tracks in COMBINED_ITM: ITM, TRAIL_SL_ITM, FIXED_TGT_ITM
        self.assertEqual(len(opened), 3)
        acct = port.tracks["COMBINED_ITM"]
        self.assertEqual(len(acct.open_positions), 3)

        # Cost per trade = ask (100) * lot_size (100) = 10,000 INR
        # Sub-track starting cash = 240,000 / 3 = 80,000 INR
        # After entry, each sub_cash should be 80,000 - 10,000 = 70,000 INR
        for sub_t in acct.constituents:
            self.assertEqual(acct.sub_cash[sub_t], 70_000.0)
        self.assertEqual(acct.cash, 210_000.0)

        # Verify sub_track tags on TradeRecords
        sub_tracks_opened = {t.sub_track for t in opened}
        self.assertEqual(sub_tracks_opened, {"ITM", "TRAIL_SL_ITM", "FIXED_TGT_ITM"})

    def test_05_subtrack_specific_risk_evaluation(self):
        """Verify that each constituent trade in a combined track executes its specific exit rules."""
        port = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM",
            output_csv="/tmp/test_combined_risk.csv",
        )
        c = MockOptionContract()
        quotes = {
            c.instrument_key: {"bid": 100.0, "ask": 100.0, "last_price": 100.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }
        contracts_by_mode = {"ITM": c}

        port.open_position(
            symbol="TCS",
            contract=c,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes,
        )

        acct = port.tracks["COMBINED_ITM"]
        self.assertEqual(len(acct.open_positions), 3)

        # Fast forward: 650 seconds (10.8 minutes) elapsed at unchanged price (₹100.00)
        # FIXED_TGT_ITM has a 10-minute timeout (600s), so it must exit on FIXED_TARGET_TIMEOUT
        # The other 2 tracks (ITM, TRAIL_SL_ITM) have a 30m timeout so they remain OPEN
        closed_10m = port.update_positions(quotes, now_ts=1650.0)
        self.assertEqual(len(closed_10m), 1)
        self.assertEqual(closed_10m[0].sub_track, "FIXED_TGT_ITM")
        self.assertIn("FIXED_TARGET_TIMEOUT", closed_10m[0].exit_reason)

        # FIXED_TGT_ITM cash should be refunded back to its sub_cash
        self.assertEqual(len(acct.open_positions), 2)
        self.assertEqual(acct.sub_cash["FIXED_TGT_ITM"], 80_000.0)  # 100 * 100 = 10,000 returned

    def test_06_non_compounding_fixed_cap_combined(self):
        """Verify non-compounding COMBINED_*_FIXED_CAP tracks cap sub_cash at initial tranche capital."""
        port = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM_FIXED_CAP",
            output_csv="/tmp/test_combined_fixed_cap.csv",
        )
        self.assertIn("COMBINED_ITM_FIXED_CAP", port.tracks)
        acct = port.tracks["COMBINED_ITM_FIXED_CAP"]
        self.assertFalse(acct.allow_compounding)

        c = MockOptionContract()
        quotes = {
            c.instrument_key: {"bid": 20.0, "ask": 20.0, "last_price": 20.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }
        contracts_by_mode = {"ITM": c}

        port.open_position(
            symbol="TCS",
            contract=c,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes,
        )

        # Price rallies to ₹25.00 (+25% gain)
        quotes[c.instrument_key]["bid"] = 25.0
        quotes[c.instrument_key]["ask"] = 25.5
        closed = port.update_positions(quotes, now_ts=1050.0)
        self.assertGreater(len(closed), 0)

        # In non-compounding mode, sub_cash cannot exceed sub_starting_capital (80,000 INR)
        for st in acct.constituents:
            self.assertLessEqual(acct.sub_cash[st], 80_000.0)
        self.assertLessEqual(acct.cash, 240_000.0)

    def test_07_duplicate_order_diversification_to_next_itm(self):
        """Verify that when sub-tracks in a combined track would select duplicate contracts, they diversify to the next ITM strike."""
        from unittest.mock import MagicMock
        resolver = MagicMock()
        c_itm1 = MockOptionContract(instrument_key="NSE_FO|TCS_3480CE", trading_symbol="TCS_3480CE")
        c_itm2 = MockOptionContract(instrument_key="NSE_FO|TCS_3460CE", trading_symbol="TCS_3460CE")
        c_itm3 = MockOptionContract(instrument_key="NSE_FO|TCS_3440CE", trading_symbol="TCS_3440CE")

        # Sequentially return deeper ITM strikes when called
        resolver.resolve_next_itm_strike.side_effect = [c_itm2, c_itm3]

        port = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM",
            output_csv="/tmp/test_combined_dupe.csv",
            option_resolver=resolver,
        )
        quotes = {
            c_itm1.instrument_key: {"bid": 100.0, "ask": 100.0, "last_price": 100.0, "timestamp": 1000.0},
            c_itm2.instrument_key: {"bid": 120.0, "ask": 120.0, "last_price": 120.0, "timestamp": 1000.0},
            c_itm3.instrument_key: {"bid": 140.0, "ask": 140.0, "last_price": 140.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }

        # All sub-tracks initially map to c_itm1
        contracts_by_mode = {"ITM": c_itm1}

        opened = port.open_position(
            symbol="TCS",
            contract=c_itm1,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c_itm1.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes,
            option_resolver=resolver,
        )

        self.assertEqual(len(opened), 3)
        contract_keys = [t.contract_key for t in opened]
        # Verify 3 distinct contracts were opened (no duplicates!)
        self.assertEqual(len(set(contract_keys)), 3)
        self.assertIn("NSE_FO|TCS_3480CE", contract_keys)
        self.assertIn("NSE_FO|TCS_3460CE", contract_keys)
        self.assertIn("NSE_FO|TCS_3440CE", contract_keys)

    def test_08_combined_star_and_recursive_strike_diversification(self):
        """Verify COMBINED_STAR ensemble combining Combined Trail SL + Combined ITM with recursive strike diversification."""
        # 1. Verify constituent composition (5 sub-tracks with overlapping TRAIL_SL_ITM deduplicated)
        self.assertIn("COMBINED_STAR", COMBINED_TRACK_CONSTITUENTS)
        self.assertEqual(
            COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"],
            ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "TRAIL_SL_OTM", "ITM", "FIXED_TGT_ITM"],
        )
        self.assertEqual(
            COMBINED_TRACK_CONSTITUENTS["COMBINED_ITM_TRAILING_SL"],
            COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"],
        )

        from unittest.mock import MagicMock
        resolver = MagicMock()
        c_atm = MockOptionContract(instrument_key="NSE_FO|TCS_3500CE", trading_symbol="TCS_3500CE")
        c_otm = MockOptionContract(instrument_key="NSE_FO|TCS_3520CE", trading_symbol="TCS_3520CE")
        c_itm1 = MockOptionContract(instrument_key="NSE_FO|TCS_3480CE", trading_symbol="TCS_3480CE")
        c_itm2 = MockOptionContract(instrument_key="NSE_FO|TCS_3460CE", trading_symbol="TCS_3460CE")
        c_itm3 = MockOptionContract(instrument_key="NSE_FO|TCS_3440CE", trading_symbol="TCS_3440CE")

        # Recursive resolver side effects for repeated ITM conflicts
        resolver.resolve_next_itm_strike.side_effect = [c_itm2, c_itm3]

        port = MultiTrackPortfolio(
            starting_capital=250_000.0,
            track_filter="COMBINED_STAR",
            output_csv="/tmp/test_combined_star.csv",
            option_resolver=resolver,
        )
        self.assertIn("COMBINED_STAR", port.tracks)
        acct = port.tracks["COMBINED_STAR"]
        self.assertEqual(len(acct.constituents), 5)

        quotes = {
            c_atm.instrument_key: {"bid": 50.0, "ask": 50.0, "last_price": 50.0, "timestamp": 1000.0},
            c_otm.instrument_key: {"bid": 25.0, "ask": 25.0, "last_price": 25.0, "timestamp": 1000.0},
            c_itm1.instrument_key: {"bid": 80.0, "ask": 80.0, "last_price": 80.0, "timestamp": 1000.0},
            c_itm2.instrument_key: {"bid": 100.0, "ask": 100.0, "last_price": 100.0, "timestamp": 1000.0},
            c_itm3.instrument_key: {"bid": 120.0, "ask": 120.0, "last_price": 120.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }

        contracts_by_mode = {
            "ATM": c_atm,
            "OTM": c_otm,
            "ITM": c_itm1,
        }

        opened = port.open_position(
            symbol="TCS",
            contract=c_atm,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c_atm.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes,
            option_resolver=resolver,
        )

        self.assertEqual(len(opened), 5, "All 5 sub-tracks in COMBINED_STAR must execute orders")
        contract_keys = [t.contract_key for t in opened]
        # Verify all 5 contracts are strictly distinct
        self.assertEqual(len(set(contract_keys)), 5)
        self.assertIn(c_itm1.instrument_key, contract_keys)
        self.assertIn(c_atm.instrument_key, contract_keys)
        self.assertIn(c_otm.instrument_key, contract_keys)
        self.assertIn(c_itm2.instrument_key, contract_keys)
        self.assertIn(c_itm3.instrument_key, contract_keys)

    def test_09_shared_full_pool_vs_separate_pools(self):
        """
        Verify:
        1. Base combined tracks (e.g. COMBINED_ITM) retain isolated separate sub-track pools.
        2. Shared combined tracks (e.g. COMBINED_ITM_SHARED, COMBINED_STAR) share the full cash pool.
        """
        c_expensive = MockOptionContract(instrument_key="NSE_FO|EXPENSIVE", trading_symbol="EXPENSIVE", lot_size=1000)
        quotes = {
            c_expensive.instrument_key: {"bid": 100.0, "ask": 100.0, "last_price": 100.0, "timestamp": 1000.0},
            "TCS": {"last_price": 3500.0},
        }

        # Case 1: Separate Pools (COMBINED_ITM)
        # Starting capital = 240,000 INR -> 3 constituents get 80,000 INR each.
        # Contract cost = 100 * 1000 = 100,000 INR > 80,000 INR.
        # Trade MUST be blocked by sub-track isolated limit!
        port_sep = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM",
            output_csv="/tmp/test_sep_pool.csv",
        )
        acct_sep = port_sep.tracks["COMBINED_ITM"]
        self.assertFalse(acct_sep.is_shared_pool, "COMBINED_ITM must be separate pools")
        opened_sep = port_sep.open_position(
            symbol="TCS",
            contract=c_expensive,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c_expensive.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode={"ITM": c_expensive},
            quotes_cache=quotes,
        )
        self.assertEqual(len(opened_sep), 0, "Separate pools must reject trade exceeding sub-track partition (80k < 100k)")
        self.assertEqual(acct_sep.cash, 240_000.0)

        # Case 2: Shared Full Pool (COMBINED_ITM_SHARED)
        # Total cash = 240,000 INR shared across all constituents without individual silos.
        # Two 100k trades execute (100k + 100k = 200k), leaving 40k in shared pool!
        port_shared = MultiTrackPortfolio(
            starting_capital=240_000.0,
            track_filter="COMBINED_ITM_SHARED",
            output_csv="/tmp/test_shared_pool.csv",
        )
        acct_shared = port_shared.tracks["COMBINED_ITM_SHARED"]
        self.assertTrue(acct_shared.is_shared_pool, "COMBINED_ITM_SHARED must be shared pool")
        opened_shared = port_shared.open_position(
            symbol="TCS",
            contract=c_expensive,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes[c_expensive.instrument_key],
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=1000.0,
            contracts_by_mode={"ITM": c_expensive},
            quotes_cache=quotes,
        )
        self.assertEqual(len(opened_shared), 2, "Shared pool allows trades up to total available cash")
        self.assertEqual(acct_shared.cash, 40_000.0)


if __name__ == "__main__":
    unittest.main()
