#!/usr/bin/env python3
"""
tests/execution/test_multi_executor.py
======================================
Unit tests for MultiExecutionManager & Multi-Mode Execution:
- Tests simultaneous broadcasting of orders to Paper and PaperDelayed adapters.
- Tests isolated position tracking and separate trade journal files.
- Tests factory create_multi_executor with various mode parameters ('paper,paper_delayed', 'all').
- Tests portfolio summaries aggregation across all active run modes.
"""

import os
import sys
import tempfile
import unittest

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.multi_executor import MultiExecutionManager, create_multi_executor
from execution.paper.paper_adapter import PaperExecutionAdapter
from execution.paper.paper_delayed_adapter import PaperDelayedExecutionAdapter


class TestMultiExecutionManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.paper_csv = os.path.join(self.temp_dir.name, "paper_trades.csv")
        self.delayed_csv = os.path.join(self.temp_dir.name, "paper_delayed_trades.csv")

        self.paper_adapter = PaperExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.paper_csv,
        )
        self.delayed_adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.delayed_csv,
            min_delay_ms=0.0,
            max_delay_ms=0.0,
        )
        self.manager = MultiExecutionManager({
            "paper": self.paper_adapter,
            "paper_delayed": self.delayed_adapter,
        }, log_dir=self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_simultaneous_order_placement(self):
        """Verify placing an order routes to both Paper and PaperDelayed adapters."""
        quote = {"bid": 45.0, "ask": 46.0, "last_price": 45.5}
        results = self.manager.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|10101",
            quantity=175,
            transaction_type="BUY",
            market_quote=quote,
            tag="TEST_MULTI",
        )

        self.assertIn("paper", results)
        self.assertIn("paper_delayed", results)
        self.assertEqual(results["paper"]["status"], "FILLED")
        self.assertEqual(results["paper_delayed"]["status"], "FILLED")
        self.assertEqual(results["paper"]["fill_price"], 46.0)
        self.assertEqual(results["paper_delayed"]["fill_price"], 46.0)

        # Check positions in both adapters
        positions_by_mode = self.manager.get_positions()
        self.assertEqual(len(positions_by_mode["paper"]), 1)
        self.assertEqual(len(positions_by_mode["paper_delayed"]), 1)
        self.assertEqual(positions_by_mode["paper"][0]["symbol"], "TCS")
        self.assertEqual(positions_by_mode["paper_delayed"][0]["symbol"], "TCS")

    def test_simultaneous_exit_and_isolated_csvs(self):
        """Verify exiting position writes to both independent CSV journals."""
        quote_buy = {"bid": 20.0, "ask": 21.0, "last_price": 20.5}
        self.manager.place_order(
            symbol="INFY",
            instrument_token="NSE_FO|20202",
            quantity=100,
            transaction_type="BUY",
            market_quote=quote_buy,
        )

        quote_sell = {"bid": 25.0, "ask": 26.0, "last_price": 25.5}
        self.manager.place_order(
            symbol="INFY",
            instrument_token="NSE_FO|20202",
            quantity=100,
            transaction_type="SELL",
            market_quote=quote_sell,
        )

        # Positions in both should now be 0
        positions = self.manager.get_positions()
        self.assertEqual(len(positions["paper"]), 0)
        self.assertEqual(len(positions["paper_delayed"]), 0)

        # Both CSV journals should exist and have logged the trade
        self.assertTrue(os.path.exists(self.paper_csv))
        self.assertTrue(os.path.exists(self.delayed_csv))

        with open(self.paper_csv, "r", encoding="utf-8") as f:
            lines_paper = f.readlines()
        with open(self.delayed_csv, "r", encoding="utf-8") as f:
            lines_del = f.readlines()

        self.assertEqual(len(lines_paper), 2)  # Header + 1 trade
        self.assertEqual(len(lines_del), 2)    # Header + 1 trade

    def test_factory_create_multi_executor(self):
        """Verify factory parses comma-separated modes and creates adapters."""
        # Test 1: Single mode
        m1 = create_multi_executor("paper")
        self.assertEqual(m1.active_modes, ["paper"])

        # Test 2: Comma-separated modes
        m2 = create_multi_executor("paper,paper_delayed")
        self.assertIn("paper", m2.active_modes)
        self.assertIn("paper_delayed", m2.active_modes)
        self.assertEqual(len(m2.active_modes), 2)

        # Test 3: 'all' mode
        m3 = create_multi_executor("all")
        self.assertIn("paper", m3.active_modes)
        self.assertIn("paper_delayed", m3.active_modes)
        self.assertIn("live", m3.active_modes)

        # Summaries
        summaries = m2.get_portfolio_summaries()
        self.assertIn("paper", summaries)
        self.assertIn("paper_delayed", summaries)

    def test_factory_reads_execution_json_config(self):
        """Verify factory automatically loads defaults from config/execution.json."""
        # 1. Default mode when empty
        m_default = create_multi_executor("")
        self.assertIn("paper", m_default.active_modes)
        paper_adapter = m_default.get_adapter("paper")
        self.assertEqual(paper_adapter.initial_capital, 250000.0)
        self.assertEqual(paper_adapter.brokerage_per_order, 30.0)

        # 2. Paper delayed loads latency parameters from execution.json
        m_delayed = create_multi_executor("paper_delayed")
        delayed_adapter = m_delayed.get_adapter("paper_delayed")
        self.assertEqual(delayed_adapter.initial_capital, 250000.0)
        self.assertEqual(delayed_adapter.min_delay_ms, 700.0)
        self.assertEqual(delayed_adapter.max_delay_ms, 2500.0)


if __name__ == "__main__":
    unittest.main()

