#!/usr/bin/env python3
"""
tests/strategies/test_rolling_dynamic_trio.py
=============================================
Unit tests for:
1. Dynamic 15-minute rolling window capital allocation in DYNAMIC_TRIO.
2. Recently Executed Orders table in dashboard.
3. 1-Lot Option requirement computation and display.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from strategies.momentum.dashboard import (
    render_recently_executed_orders,
    render_scanner_dashboard,
)
from strategies.momentum.option_selector import OptionStrikeResolver
from strategies.momentum.portfolio import MultiTrackPortfolio, TradeRecord


@dataclass
class MockContract:
    instrument_key: str = "NSE_FO|12345"
    trading_symbol: str = "RELIANCE26NOV2500CE"
    underlying_symbol: str = "RELIANCE"
    strike_price: float = 2500.0
    option_type: str = "CE"
    expiry_date: str = "2026-11-26"
    lot_size: int = 250
    tick_size: float = 0.05


class TestRollingDynamicTrio(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.csv_path = os.path.join(self.test_dir, "trades.csv")
        self.portfolio = MultiTrackPortfolio(
            starting_capital=100_000.0,
            output_csv=self.csv_path,
            rolling_lookback_minutes=15,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_rolling_window_considers_only_recent_15m_trades(self):
        now_ts = 100_000.0
        cutoff_15m = now_ts - (15 * 60.0)  # 99,100.0

        # Simulate track OTM had a trade 30 mins ago (+₹5,000)
        old_trade = TradeRecord(
            trade_id="T-OLD",
            strategy_track="OTM",
            symbol="INFY",
            contract_key="K1",
            contract_symbol="S1",
            direction="BULL",
            lots=1,
            quantity=100,
            entry_time=98_000.0,
            entry_price=10.0,
            exit_time=98_500.0,  # Older than 15 mins!
            exit_price=60.0,
            gross_pnl=5000.0,
            net_pnl=4940.0,
            is_closed=True,
        )
        self.portfolio.tracks["OTM"].closed_trades.append(old_trade)
        self.portfolio.tracks["OTM"].net_realized += 4940.0

        # Simulate track TRAIL_SL_ATM had a trade 5 mins ago (+₹2,000)
        recent_trade = TradeRecord(
            trade_id="T-RECENT",
            strategy_track="TRAIL_SL_ATM",
            symbol="TCS",
            contract_key="K2",
            contract_symbol="S2",
            direction="BULL",
            lots=1,
            quantity=100,
            entry_time=99_400.0,
            entry_price=20.0,
            exit_time=99_700.0,  # 5 mins ago (within 15m window!)
            exit_price=40.0,
            gross_pnl=2000.0,
            net_pnl=1940.0,
            is_closed=True,
        )
        self.portfolio.tracks["TRAIL_SL_ATM"].closed_trades.append(recent_trade)
        self.portfolio.tracks["TRAIL_SL_ATM"].net_realized += 1940.0

        # Simulate track ATM had a trade 2 mins ago (+₹3,000)
        recent_atm = TradeRecord(
            trade_id="T-ATM",
            strategy_track="ATM",
            symbol="RELIANCE",
            contract_key="K3",
            contract_symbol="S3",
            direction="BULL",
            lots=1,
            quantity=100,
            entry_time=99_800.0,
            entry_price=10.0,
            exit_time=99_900.0,  # 1.6 mins ago (within 15m window!)
            exit_price=40.0,
            gross_pnl=3000.0,
            net_pnl=2940.0,
            is_closed=True,
        )
        self.portfolio.tracks["ATM"].closed_trades.append(recent_atm)
        self.portfolio.tracks["ATM"].net_realized += 2940.0

        # Update dynamic trio leaderboard at now_ts (100,000.0)
        self.portfolio.update_dynamic_trio_leaderboard(now_ts=now_ts)

        # In the last 15 minutes:
        # ATM = +₹2,940
        # TRAIL_SL_ATM = +₹1,940
        # OTM = ₹0 (trade was 30 mins ago!)
        # Therefore, ATM and TRAIL_SL_ATM MUST rank above OTM in the rolling 15m window!
        champions = self.portfolio.dynamic_trio_champions
        self.assertEqual(champions[0], "ATM")
        self.assertEqual(champions[1], "TRAIL_SL_ATM")

    def test_recently_executed_orders_table_rendering(self):
        mock_contract = MockContract()
        # Open a position
        self.portfolio.open_position(
            symbol="RELIANCE",
            contract=mock_contract,
            direction="BULL",
            underlying_ltp=2500.0,
            option_quote={"ask": 35.0, "bid": 34.0, "ltp": 35.0},
            flag_high=2510.0,
            flag_low=2490.0,
            now_ts=1000.0,
        )

        recent = self.portfolio.get_recent_trades(limit=5)
        self.assertGreaterEqual(len(recent), 1)

        # Render Recently Executed Orders Table
        rendered = render_recently_executed_orders(self.portfolio, quotes_cache={}, max_orders=5)
        self.assertIn("RECENTLY EXECUTED ORDERS", rendered)
        self.assertIn("RELIANCE", rendered)
        self.assertIn("BULL", rendered)

    def test_option_requirement_column_formatting(self):
        resolver = OptionStrikeResolver()
        # Non-F&O equity
        non_fo = resolver.compute_option_requirement("NON_FO_STOCK", 120.0, direction="BULL")
        self.assertFalse(non_fo["has_fo"])
        self.assertEqual(non_fo["raw_str"], "Cash Only")

        # F&O equity
        resolver.options_by_symbol["NIFTY"]["2026-11-26"] = [
            MockContract(instrument_key="NIFTY_CE", trading_symbol="NIFTY26NOV24000CE", strike_price=24000.0, lot_size=25)
        ]
        resolver._loaded = True

        mock_quotes = {"NIFTY_CE": {"ask": 120.0, "bid": 118.0, "ltp": 120.0}}
        fo_res = resolver.compute_option_requirement("NIFTY", 24000.0, direction="BULL", quotes_cache=mock_quotes)
        self.assertTrue(fo_res["has_fo"])
        # 120.0 * 25 = 3000 -> "₹3.0k (₹120)"
        self.assertIn("₹3.0k", fo_res["raw_str"])

    def test_roi_subtracts_current_mtm_drawdown(self):
        """Verify return percentage (ROI %) subtracts open position MTM loss rather than relying on net realized."""
        acct = self.portfolio.tracks["ATM"]
        acct.starting_capital = 100_000.0
        acct.net_realized = 2000.0  # Realized +₹2,000 (+2.0% raw)

        # No open positions: ROI reflects realized
        self.assertEqual(acct.compute_roi(), 2.0)

        # Open position entered at ₹50.0 for 100 qty (cost ₹5,000)
        pos = TradeRecord(
            trade_id="TRD-OPEN-1",
            strategy_track="ATM",
            symbol="INFY",
            contract_key="K-INFY",
            contract_symbol="INFY26NOV1800CE",
            direction="BULL",
            lots=1,
            quantity=100,
            entry_time=1000.0,
            entry_price=50.0,
            peak_price=50.0,
            underlying_entry=1800.0,
            underlying_sl=1780.0,
            current_sl=40.0,
        )
        acct.open_positions["TRD-OPEN-1"] = pos

        # Scenario A: Bid dropped to ₹35.0 (unrealized MTM loss = (35 - 50) * 100 = -₹1,500)
        # Net Return = Realized (2,000) + MTM (-1,500) = +₹500.00
        # ROI % MUST be +0.50%, NOT +2.00%!
        quotes = {"K-INFY": {"bid": 35.0, "ask": 35.5, "ltp": 35.0}}
        mtm = acct.compute_mtm(quotes)
        self.assertEqual(mtm, -1500.0)
        self.assertEqual(acct.compute_roi(quotes), 0.50)
        self.assertEqual(acct.net_roi, 0.50)

        # Scenario B: Bid dropped to ₹20.0 (unrealized MTM loss = -₹3,000)
        # Net Return = 2,000 - 3,000 = -₹1,000.00
        # ROI % MUST be -1.00% (negative return despite positive net realized!)
        quotes_deep = {"K-INFY": {"bid": 20.0, "ask": 20.5, "ltp": 20.0}}
        self.assertEqual(acct.compute_mtm(quotes_deep), -3000.0)
        self.assertEqual(acct.compute_roi(quotes_deep), -1.00)
        self.assertEqual(acct.net_roi, -1.00)

    def test_dynamic_trio_50_50_roi_scoring_penalizes_drawdown(self):
        """
        Verify DYNAMIC_TRIO 50/50 scoring combines P&L and ROI (factoring in open MTM),
        ensuring a strategy with high realized P&L but severe open drawdown is not crowned champion.
        """
        now_ts = 50_000.0
        cutoff = now_ts - (15 * 60.0)

        # Track 1 (OTM): High realized +₹2,000, but open MTM is -₹3,000 (net return: -₹1,000, ROI: -1.0%)
        # Track 2 (TRAIL_SL_ATM): Clean realized +₹800, 0 open MTM (net return: +₹800, ROI: +0.8%)
        # Track 3 (ATM): Clean realized +₹600, 0 open MTM (net return: +₹600, ROI: +0.6%)

        tr1_fix = self.portfolio.tracks["OTM_FIXED_CAP"]
        tr1_cmp = self.portfolio.tracks["OTM"]
        tr2_fix = self.portfolio.tracks["TRAIL_SL_ATM_FIXED_CAP"]
        tr2_cmp = self.portfolio.tracks["TRAIL_SL_ATM"]
        tr3_fix = self.portfolio.tracks["ATM_FIXED_CAP"]
        tr3_cmp = self.portfolio.tracks["ATM"]

        # Track 1 closed trade +₹2,000
        t1 = TradeRecord(
            trade_id="T1", strategy_track="OTM", symbol="A",
            contract_key="K1", contract_symbol="S1", direction="BULL",
            lots=1, quantity=100, entry_time=cutoff + 60, exit_time=cutoff + 120,
            entry_price=10.0, exit_price=30.0, gross_pnl=2000.0, brokerage=60.0,
            net_pnl=1940.0, is_closed=True,
        )
        tr1_fix.closed_trades.append(t1)
        tr1_fix.net_realized = 1940.0
        tr1_cmp.closed_trades.append(t1)
        tr1_cmp.net_realized = 1940.0

        # But Track 1 holds an open position with huge -₹3,000 MTM loss
        pos_loss = TradeRecord(
            trade_id="T1-OPEN", strategy_track="OTM", symbol="A",
            contract_key="K1-OPEN", contract_symbol="S1-OPEN", direction="BULL",
            lots=1, quantity=100, entry_time=cutoff + 180, entry_price=50.0,
            peak_price=50.0, underlying_entry=1000.0, underlying_sl=980.0, current_sl=40.0,
        )
        tr1_fix.open_positions["T1-OPEN"] = pos_loss
        tr1_cmp.open_positions["T1-OPEN"] = pos_loss

        # Track 2 closed trade +₹800, no open positions
        t2 = TradeRecord(
            trade_id="T2", strategy_track="TRAIL_SL_ATM", symbol="B",
            contract_key="K2", contract_symbol="S2", direction="BULL",
            lots=1, quantity=100, entry_time=cutoff + 60, exit_time=cutoff + 120,
            entry_price=10.0, exit_price=18.6, gross_pnl=860.0, brokerage=60.0,
            net_pnl=800.0, is_closed=True,
        )
        tr2_fix.closed_trades.append(t2)
        tr2_fix.net_realized = 800.0
        tr2_cmp.closed_trades.append(t2)
        tr2_cmp.net_realized = 800.0

        # Track 3 closed trade +₹600, no open positions
        t3 = TradeRecord(
            trade_id="T3", strategy_track="ATM", symbol="C",
            contract_key="K3", contract_symbol="S3", direction="BULL",
            lots=1, quantity=100, entry_time=cutoff + 60, exit_time=cutoff + 120,
            entry_price=10.0, exit_price=16.6, gross_pnl=660.0, brokerage=60.0,
            net_pnl=600.0, is_closed=True,
        )
        tr3_fix.closed_trades.append(t3)
        tr3_fix.net_realized = 600.0
        tr3_cmp.closed_trades.append(t3)
        tr3_cmp.net_realized = 600.0

        # Quotes where K1-OPEN is currently down to ₹20 (loss = (20 - 50) * 100 = -₹3,000)
        quotes = {"K1-OPEN": {"bid": 20.0, "ask": 20.5, "ltp": 20.0}}

        self.portfolio.update_dynamic_trio_leaderboard(now_ts=now_ts, quotes_cache=quotes)

        # Because ROI accounts for open MTM, Track 1 (OTM) net return is -₹1,060 (ROI = -1.06%)
        # Track 2 (TRAIL_SL_ATM) is +₹800 (ROI = +0.80%) and Track 3 (ATM) is +₹600 (ROI = +0.60%)
        # Champions must be TRAIL_SL_ATM (#1) and ATM (#2) beating OTM!
        champions = self.portfolio.dynamic_trio_champions
        self.assertEqual(champions[0], "TRAIL_SL_ATM")
        self.assertEqual(champions[1], "ATM")


if __name__ == "__main__":
    unittest.main()
