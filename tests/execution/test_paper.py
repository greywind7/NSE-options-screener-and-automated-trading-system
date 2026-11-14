#!/usr/bin/env python3
"""
tests/execution/test_paper.py
=============================
Unit tests for PaperExecutionAdapter:
- Tests standalone paper trading with spread fill (BUY at Ask, SELL at Bid)
- Tests capital accounting, P&L calculations, and trade journaling
- Tests portfolio summary queries (equity, realized P&L, open count)
- Tests order modification and cancellation
"""

import os
import sys
import tempfile
import unittest

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.paper.paper_adapter import PaperExecutionAdapter


class TestPaperExecutionAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = os.path.join(self.temp_dir.name, "test_paper_trades.csv")
        self.adapter = PaperExecutionAdapter(
            initial_capital=100_000.0,
            brokerage_per_order=30.0,
            output_csv=self.csv_path,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_paper_buy_and_sell_lifecycle(self):
        """Verify complete paper trade execution with spread fill, PnL and capital update."""
        quote_buy = {"bid": 19.50, "ask": 20.00, "last_price": 19.75}
        buy_order = self.adapter.place_order(
            symbol="HDFCBANK",
            instrument_token="NSE_FO|11111",
            quantity=500,
            transaction_type="BUY",
            market_quote=quote_buy,
            tag="TEST_BUY",
        )

        self.assertEqual(buy_order["status"], "FILLED")
        self.assertEqual(buy_order["fill_price"], 20.00)  # BUY at Ask
        self.assertTrue(buy_order["order_id"].startswith("PAPER-"))

        # Capital reduced by cost (20 * 500 = 10,000)
        self.assertEqual(self.adapter.available_capital, 90_000.0)
        positions = self.adapter.get_positions()
        self.assertEqual(len(positions), 1)

        # Exit position at higher price
        quote_sell = {"bid": 25.00, "ask": 25.50, "last_price": 25.25}
        sell_order = self.adapter.place_order(
            symbol="HDFCBANK",
            instrument_token="NSE_FO|11111",
            quantity=500,
            transaction_type="SELL",
            market_quote=quote_sell,
            tag="TARGET_HIT",
        )

        self.assertEqual(sell_order["status"], "FILLED")
        self.assertEqual(sell_order["fill_price"], 25.00)  # SELL at Bid

        # Positions should now be empty
        self.assertEqual(len(self.adapter.get_positions()), 0)

        # Capital updated: 90,000 + 10,000 (cost) + 2,500 (gross PnL: (25-20)*500) - 60 (brokerage) = 102,440.0
        self.assertEqual(self.adapter.available_capital, 102_440.0)

        # Portfolio Summary
        summary = self.adapter.get_portfolio_summary()
        self.assertEqual(summary["realized_pnl"], 2440.0)
        self.assertEqual(summary["total_equity"], 102_440.0)
        self.assertEqual(summary["closed_trades_count"], 1)

        # Verify CSV log
        self.assertTrue(os.path.exists(self.csv_path))
        with open(self.csv_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("HDFCBANK", lines[1])
        self.assertIn("2440.0", lines[1])

    def test_order_modification_and_cancellation(self):
        """Verify order status queries, modify, and cancel logic."""
        quote = {"bid": 50.0, "ask": 50.5, "last_price": 50.25}
        order = self.adapter.place_order(
            symbol="INFY",
            instrument_token="NSE_FO|22222",
            quantity=100,
            transaction_type="BUY",
            market_quote=quote,
        )
        order_id = order["order_id"]

        mod_res = self.adapter.modify_order(order_id, quantity=200)
        self.assertEqual(mod_res["status"], "success")
        self.assertEqual(self.adapter.get_order_status(order_id)["quantity"], 200)

        cancel_res = self.adapter.cancel_order(order_id)
        self.assertEqual(cancel_res["status"], "success")
        self.assertEqual(self.adapter.get_order_status(order_id)["status"], "CANCELLED")

    def test_paper_sell_order_failure_preserves_position(self):
        """Verify PaperExecutionAdapter preserves position if sell quote is missing."""
        cache = {"NSE_FO|TCS26NOV3500CE": {"bid": 50.0, "ask": 50.5, "ltp": 50.5}}
        adapter = PaperExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            quotes_cache=cache,
        )

        # 1. Buy
        res_buy = adapter.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|TCS26NOV3500CE",
            quantity=175,
            transaction_type="BUY",
        )
        self.assertEqual(res_buy["status"], "FILLED")
        self.assertEqual(len(adapter.get_positions()), 1)

        # 2. Quote drops
        del cache["NSE_FO|TCS26NOV3500CE"]

        # 3. Sell attempt
        res_sell = adapter.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|TCS26NOV3500CE",
            quantity=175,
            transaction_type="SELL",
            tag="SL_EXIT",
        )
        self.assertEqual(res_sell["status"], "REJECTED_NO_QUOTE")
        self.assertEqual(len(adapter.get_positions()), 1, "Position must be preserved!")


if __name__ == "__main__":
    unittest.main()
