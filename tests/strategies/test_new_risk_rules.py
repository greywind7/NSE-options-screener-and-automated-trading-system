#!/usr/bin/env python3
"""
tests/strategies/test_new_risk_rules.py
======================================
Tests for updated risk and exit engine:
1. 7.5% default Stop Loss (configurable).
2. 15.0% rolling target decaying to 2.5% over 15 mins with 30m square-off.
3. Stepped SL for main & low premium tracks: +2.5% value -> +2.5% SL ratchet.
4. Trailing SL tracks: breakeven (SL=0%) at +2.5% and ratchet from there.
5. Fixed target: no rolling target, square off after 10m, skip strike if 5% < 500.
6. Dynamic Trio 10-minute rolling window.
7. Groww 25 order modifications: cancel and place fresh SL order.
"""

import unittest
from unittest.mock import MagicMock
from strategies.momentum.risk import RiskManager
from strategies.momentum.portfolio import MultiTrackPortfolio, TradeRecord
from execution.live.live_adapter import LiveExecutionAdapter


class TestNewRiskRules(unittest.TestCase):

    def test_01_default_risk_parameters(self):
        rm = RiskManager()
        self.assertEqual(rm.option_sl_pct, 7.5)
        self.assertEqual(rm.initial_target_pct, 15.0)
        self.assertEqual(rm.decay_floor_pct, 2.5)
        self.assertEqual(rm.target_decay_sec, 900.0)  # 15 mins
        self.assertEqual(rm.stagnation_timeout_sec, 1800.0)  # 30 mins
        self.assertEqual(rm.step_size_pct, 2.5)
        self.assertEqual(rm.step_sl_pct, 2.5)
        self.assertEqual(rm.trail_breakeven_trigger_pct, 2.5)
        self.assertEqual(rm.fixed_target_inr, 750.0)
        self.assertEqual(rm.fixed_target_timeout_sec, 600.0)  # 10 mins
        self.assertEqual(rm.fixed_target_min_5pct_val, 500.0)
        self.assertEqual(rm.broker_max_order_modifications, 25)

    def test_02_stepped_stop_loss_main_tracks(self):
        rm = RiskManager()
        # Entry at ₹100. Base SL = ₹92.50 (-7.5%)
        pos = {
            "entry_price": 100.0,
            "quantity": 10,
            "direction": "BULL",
            "entry_time": 1000.0,
            "peak_price": 100.0,
            "current_sl": 92.50,
            "strategy_type": "ATM",
        }

        # Case A: Price drops to 92.50 -> STOP LOSS hit (-7.5%)
        exit_flag, reason = rm.evaluate_exit(pos, current_option_price=92.50, current_underlying_price=1000.0, now_ts=1010.0)
        self.assertTrue(exit_flag)
        self.assertIn("STOP_LOSS", reason)

        # Case B: Price rises to 102.50 (+2.5% value increase).
        # SL should update by 2.5% (-7.5% + 2.5% = -5.0% -> ₹95.00)
        pos["current_sl"] = 92.50
        pos["peak_price"] = 100.0
        exit_flag, _ = rm.evaluate_exit(pos, current_option_price=102.50, current_underlying_price=1000.0, now_ts=1010.0)
        self.assertFalse(exit_flag)
        self.assertAlmostEqual(pos["current_sl"], 95.00, places=2)

        # Case C: Price rises to 105.00 (+5.0% value increase).
        # SL should update to -2.5% -> ₹97.50
        exit_flag, _ = rm.evaluate_exit(pos, current_option_price=105.00, current_underlying_price=1000.0, now_ts=1020.0)
        self.assertFalse(exit_flag)
        self.assertAlmostEqual(pos["current_sl"], 97.50, places=2)

        # Case D: Price rises to 107.50 (+7.5% value increase).
        # SL should update to 0.0% (Breakeven -> ₹100.00)
        exit_flag, _ = rm.evaluate_exit(pos, current_option_price=107.50, current_underlying_price=1000.0, now_ts=1030.0)
        self.assertFalse(exit_flag)
        self.assertAlmostEqual(pos["current_sl"], 100.00, places=2)

    def test_03_trailing_stop_loss_tracks(self):
        rm = RiskManager()
        # Entry at ₹100. Initial SL is -7.5% = ₹92.50
        pos = {
            "entry_price": 100.0,
            "quantity": 10,
            "direction": "BULL",
            "entry_time": 1000.0,
            "peak_price": 100.0,
            "current_sl": 92.50,
            "strategy_type": "TRAIL_SL_ATM",
        }

        # Case A: Advance to +2.0% (< +2.5%). SL stays at 92.50
        rm.evaluate_exit(pos, current_option_price=102.0, current_underlying_price=1000.0, now_ts=1010.0)
        self.assertEqual(pos["current_sl"], 92.50)

        # Case B: Reaches +2.5% (₹102.50). Stoploss becomes 0 (Breakeven = ₹100.00)
        rm.evaluate_exit(pos, current_option_price=102.50, current_underlying_price=1000.0, now_ts=1020.0)
        self.assertAlmostEqual(pos["current_sl"], 100.00, places=2)

        # Case C: Advances to +5.0% (₹105.00). Ratchets from there (+₹2.50 -> ₹102.50)
        rm.evaluate_exit(pos, current_option_price=105.00, current_underlying_price=1000.0, now_ts=1030.0)
        self.assertAlmostEqual(pos["current_sl"], 102.50, places=2)

        # Case D: Price drops back to ₹102.00 -> Hit Trailing SL!
        exit_flag, reason = rm.evaluate_exit(pos, current_option_price=102.00, current_underlying_price=1000.0, now_ts=1040.0)
        self.assertTrue(exit_flag)
        self.assertIn("TRAILING_STOP_LOSS", reason)

    def test_04_rolling_target_and_30m_timeout(self):
        rm = RiskManager()
        # Entry at ₹100, qty 10. Brokerage = ₹60 total (₹6/share).
        pos = {
            "entry_price": 100.0,
            "quantity": 10,
            "direction": "BULL",
            "entry_time": 1000.0,
            "peak_price": 100.0,
            "current_sl": 92.50,
            "strategy_type": "ATM",
        }

        # At t=0: Target is 15% + ₹6 = ₹121.00
        exit_flag, _ = rm.evaluate_exit(pos, current_option_price=115.00, current_underlying_price=1000.0, now_ts=1000.0)
        self.assertFalse(exit_flag)

        # At t=15m (900s): Target has decayed to 2.5% + ₹6 = ₹108.50
        exit_flag, reason = rm.evaluate_exit(pos, current_option_price=108.60, current_underlying_price=1000.0, now_ts=1900.0)
        self.assertTrue(exit_flag)
        self.assertIn("TARGET", reason)

        # At t=30m (1800s): Stagnation timeout exit if trade stagnates near entry without hitting target or SL
        pos_stagnant = {
            "entry_price": 100.0,
            "quantity": 10,
            "direction": "BULL",
            "entry_time": 1000.0,
            "peak_price": 100.50,
            "current_sl": 92.50,
            "strategy_type": "ATM",
        }
        exit_flag, reason = rm.evaluate_exit(pos_stagnant, current_option_price=100.50, current_underlying_price=1000.0, now_ts=2801.0)
        self.assertTrue(exit_flag)
        self.assertIn("STAGNATION_TIMEOUT", reason)

    def test_05_fixed_target_rules(self):
        rm = RiskManager()
        pos = {
            "entry_price": 100.0,
            "quantity": 10,
            "direction": "BULL",
            "entry_time": 1000.0,
            "peak_price": 100.0,
            "current_sl": 92.50,
            "strategy_type": "FIXED_TGT_ATM",
        }

        # Fixed target of ₹750 net.
        # Gross needed = 750 + 60 = 810 -> Price = 100 + 81 = 181.00 (or 15% initial target)
        # At +5m: price ₹103.00 (net P&L = ₹30 - 60 = -₹30) -> No exit
        exit_flag, _ = rm.evaluate_exit(pos, current_option_price=103.0, current_underlying_price=1000.0, now_ts=1300.0)
        self.assertFalse(exit_flag)

        # At 10m (600s): Unresolved -> FIXED_TARGET_TIMEOUT exit!
        exit_flag, reason = rm.evaluate_exit(pos, current_option_price=103.0, current_underlying_price=1000.0, now_ts=1601.0)
        self.assertTrue(exit_flag)
        self.assertIn("FIXED_TARGET_TIMEOUT", reason)

    def test_06_fixed_target_strike_filter_5pct_less_than_500(self):
        port = MultiTrackPortfolio(starting_capital=100000.0, active_tracks=["ATM", "FIXED_TGT_ATM"])
        # Mock contract 1: ask = 5.0, lot_size = 500 -> Cost = 2,500. 5% of 2500 = 125 < 500!
        # Should NOT open in FIXED_TGT_ATM!
        c_small = MagicMock()
        c_small.instrument_key = "NSE_FO|SMALL_CE"
        c_small.trading_symbol = "SMALL_CE"
        c_small.lot_size = 500

        trades = port._execute_open_trades(
            symbol="SMALL",
            contract=c_small,
            direction="BULL",
            underlying_ltp=100.0,
            option_quote={"ask": 5.0, "bid": 4.95, "received_timestamp": 1000.0},
            flag_high=101.0,
            flag_low=99.0,
            now_ts=1000.0,
        )
        tracks_opened = [t.strategy_track for t in trades]
        self.assertIn("ATM", tracks_opened)
        self.assertNotIn("FIXED_TGT_ATM", tracks_opened)

        # Mock contract 2: ask = 30.0, lot_size = 500 -> Cost = 15,000. 5% of 15000 = 750 >= 500!
        # Should open in FIXED_TGT_ATM!
        c_large = MagicMock()
        c_large.instrument_key = "NSE_FO|LARGE_CE"
        c_large.trading_symbol = "LARGE_CE"
        c_large.lot_size = 500

        port.tracks["ATM"].open_positions.clear()
        port.tracks["FIXED_TGT_ATM"].open_positions.clear()
        trades2 = port._execute_open_trades(
            symbol="LARGE",
            contract=c_large,
            direction="BULL",
            underlying_ltp=100.0,
            option_quote={"ask": 30.0, "bid": 29.5, "received_timestamp": 1000.0},
            flag_high=101.0,
            flag_low=99.0,
            now_ts=1000.0,
        )
        tracks_opened2 = [t.strategy_track for t in trades2]
        self.assertIn("ATM", tracks_opened2)
        self.assertIn("FIXED_TGT_ATM", tracks_opened2)

    def test_07_dynamic_trio_10m_rolling_window(self):
        port = MultiTrackPortfolio(
            starting_capital=100000.0,
            allocator_config={"rolling_lookback_minutes": 10},
        )
        self.assertEqual(port.rolling_lookback_minutes, 10)

    def test_08_groww_25_order_modifications_cancel_and_replace(self):
        adapter = LiveExecutionAdapter(live_confirmed=False)
        adapter.broker_max_order_modifications = 25
        # Setup mock active position
        pos_key = "TEST_NSE_FO|TEST"
        adapter.positions[pos_key] = {
            "symbol": "TEST",
            "instrument_token": "NSE_FO|TEST",
            "quantity": 100,
            "entry_price": 100.0,
            "sl_order_id": "ORD-SL-001",
            "current_sl": 90.0,
            "modification_count": 24,
        }
        adapter.orders["ORD-SL-001"] = {"order_id": "ORD-SL-001", "trigger_price": 90.0, "status": "OPEN"}

        # 25th modification: modify normally
        res1 = adapter.update_stop_loss("TEST", "NSE_FO|TEST", new_stop_loss_price=92.0)
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(adapter.positions[pos_key]["modification_count"], 25)

        # 26th modification (exceeds 25): Cancel order and place fresh SL order!
        res2 = adapter.update_stop_loss("TEST", "NSE_FO|TEST", new_stop_loss_price=94.0)
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(res2["action"], "CANCEL_AND_REPLACE")
        self.assertEqual(adapter.positions[pos_key]["modification_count"], 0)

    def test_09_option_spread_liquidity_gate(self):
        """Verify that options with spread > 3.0% or unquoted bids (e.g. BOSCHLTD 7.59% spread) are strictly rejected before entry."""
        port = MultiTrackPortfolio(
            starting_capital=100000.0,
            track_filter="ATM",
        )
        c = MagicMock()
        c.instrument_key = "NSE_FO|BOSCHLTD_49500_PE"
        c.trading_symbol = "BOSCHLTD_49500_PE"
        c.lot_size = 25

        # 1. BOSCHLTD Wide Spread Case (Ask ₹1129.55, Bid ₹1043.80 -> 7.59% spread)
        wide_quote = {"ask": 1129.55, "bid": 1043.80, "ltp": 1080.0, "received_timestamp": 1000.0}
        trades_wide = port._execute_open_trades(
            symbol="BOSCHLTD",
            contract=c,
            direction="BEAR",
            underlying_ltp=49700.0,
            option_quote=wide_quote,
            flag_high=49800.0,
            flag_low=49600.0,
            now_ts=1000.0,
        )
        self.assertEqual(len(trades_wide), 0, "Wide spread option (>3.0%) must be strictly rejected!")

        # 2. Unquoted Bid Case (Ask ₹50.0, Bid ₹0.0 -> no market exit liquidity)
        unquoted_quote = {"ask": 50.0, "bid": 0.0, "received_timestamp": 1000.0}
        trades_unquoted = port._execute_open_trades(
            symbol="TEST",
            contract=c,
            direction="BULL",
            underlying_ltp=2500.0,
            option_quote=unquoted_quote,
            flag_high=2510.0,
            flag_low=2490.0,
            now_ts=1000.0,
        )
        self.assertEqual(len(trades_unquoted), 0, "Unquoted bid option must be strictly rejected!")

        # 3. Liquid Option Case (Ask ₹50.0, Bid ₹49.50 -> 1.0% spread <= 3.0%)
        liquid_quote = {"ask": 50.0, "bid": 49.50, "ltp": 49.80, "received_timestamp": 1000.0}
        trades_liquid = port._execute_open_trades(
            symbol="TEST",
            contract=c,
            direction="BULL",
            underlying_ltp=2500.0,
            option_quote=liquid_quote,
            flag_high=2510.0,
            flag_low=2490.0,
            now_ts=1000.0,
        )
        self.assertGreater(len(trades_liquid), 0, "Liquid option (<=3.0% spread) must execute cleanly!")
        self.assertEqual(trades_liquid[0].entry_price, 50.0)


if __name__ == "__main__":
    unittest.main()
