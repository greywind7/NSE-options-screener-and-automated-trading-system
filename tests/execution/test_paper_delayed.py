#!/usr/bin/env python3
"""
tests/execution/test_paper_delayed.py
=====================================
Unit tests for PaperDelayedExecutionAdapter:
- Simulated 700ms - 2.5s latency modeling.
- Dynamic quote re-sampling at delayed execution time (slippage capture).
- Full round-trip trade lifecycle with P&L accounting and brokerage friction.
- Non-blocking asynchronous execution vs synchronous simulation.
- Multi-executor integration.
"""

import os
import sys
import tempfile
import time
import unittest

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.multi_executor import create_multi_executor
from execution.paper.paper_delayed_adapter import PaperDelayedExecutionAdapter


class TestPaperDelayedAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.csv_path = os.path.join(self.temp_dir.name, "paper_delayed_trades.csv")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_adapter_initialization(self):
        """Verify initialization and CSV journal setup."""
        adapter = PaperDelayedExecutionAdapter(
            initial_capital=200_000.0,
            brokerage_per_order=25.0,
            output_csv=self.csv_path,
            min_delay_ms=700.0,
            max_delay_ms=2500.0,
        )
        self.assertEqual(adapter.initial_capital, 200_000.0)
        self.assertEqual(adapter.available_capital, 200_000.0)
        self.assertEqual(adapter.brokerage_per_order, 25.0)
        self.assertEqual(adapter.min_delay_ms, 700.0)
        self.assertEqual(adapter.max_delay_ms, 2500.0)
        self.assertTrue(os.path.exists(self.csv_path))

        # Test default parameters when not explicitly passed
        default_adapter = PaperDelayedExecutionAdapter(output_csv=self.csv_path)
        self.assertEqual(default_adapter.min_delay_ms, 700.0)
        self.assertEqual(default_adapter.max_delay_ms, 2500.0)

    def test_dynamic_quote_resampling_on_delayed_fill(self):
        """
        Verify that quotes updated in quotes_cache during the latency window
        are used for the fill price (slippage capture).
        """
        shared_cache = {
            "NSE_FO|NIFTY26NOV25000CE": {"bid": 99.5, "ask": 100.0, "ltp": 100.0}
        }

        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            min_delay_ms=50.0,
            max_delay_ms=100.0,
            quotes_cache=shared_cache,
            async_execution=False,  # Synchronous for deterministic testing
        )

        # Before order executes, underlying surges and quote shifts from 100.0 to 104.5
        # In a real environment, this happens while the timer is running. Here we update cache directly.
        shared_cache["NSE_FO|NIFTY26NOV25000CE"] = {"bid": 104.0, "ask": 104.5, "ltp": 104.5}

        # Place BUY order with initial quote passing stale 100.0
        stale_quote = {"bid": 99.5, "ask": 100.0, "ltp": 100.0}
        res = adapter.place_order(
            symbol="NIFTY",
            instrument_token="NSE_FO|NIFTY26NOV25000CE",
            quantity=50,
            transaction_type="BUY",
            market_quote=stale_quote,
        )

        # Filled price must be the fresh 104.5 Ask from quotes_cache, NOT stale 100.0!
        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(res["fill_price"], 104.5)
        self.assertGreaterEqual(res["delay_ms"], 50.0)

        # Capital reduced by 104.5 * 50 = 5,225
        self.assertEqual(adapter.available_capital, 100_000.0 - 5225.0)
        self.assertEqual(len(adapter.get_positions()), 1)

    def test_round_trip_trade_settlement_and_pnl(self):
        """Verify round-trip trade settlement and portfolio accounting."""
        shared_cache = {
            "NSE_FO|TCS26NOV3500CE": {"bid": 49.5, "ask": 50.0, "ltp": 50.0}
        }

        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            brokerage_per_order=20.0,
            output_csv=self.csv_path,
            min_delay_ms=0.0,
            max_delay_ms=10.0,
            quotes_cache=shared_cache,
            async_execution=False,
        )

        # 1. Buy at Ask 50.0
        adapter.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|TCS26NOV3500CE",
            quantity=175,
            transaction_type="BUY",
            market_quote=shared_cache["NSE_FO|TCS26NOV3500CE"],
        )
        self.assertEqual(len(adapter.get_positions()), 1)

        # Price advances: Bid moves to 60.0
        shared_cache["NSE_FO|TCS26NOV3500CE"] = {"bid": 60.0, "ask": 60.5, "ltp": 60.0}

        # 2. Sell at Bid 60.0
        adapter.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|TCS26NOV3500CE",
            quantity=175,
            transaction_type="SELL",
            market_quote=shared_cache["NSE_FO|TCS26NOV3500CE"],
            tag="TARGET_HIT",
        )

        self.assertEqual(len(adapter.get_positions()), 0)
        self.assertEqual(len(adapter.closed_trades), 1)

        trade = adapter.closed_trades[0]
        # Gross = (60.0 - 50.0) * 175 = 1750.0
        self.assertEqual(trade["gross_pnl"], 1750.0)
        # Brokerage = 20.0 * 2 = 40.0
        self.assertEqual(trade["brokerage"], 40.0)
        self.assertEqual(trade["net_pnl"], 1710.0)

        summary = adapter.get_portfolio_summary()
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["net_realized"], 1710.0)
        self.assertEqual(summary["win_rate"], 100.0)

    def test_asynchronous_delayed_execution(self):
        """
        Verify that with async_execution=True, place_order returns immediately
        with status='OPEN' / 'delayed_in_flight', and fills in background after delay.
        """
        shared_cache = {
            "NSE_FO|RELIANCE26NOV2600CE": {"bid": 54.5, "ask": 55.0, "ltp": 55.0}
        }

        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            min_delay_ms=80.0,
            max_delay_ms=120.0,
            quotes_cache=shared_cache,
            async_execution=True,  # Non-blocking async!
        )

        t0 = time.time()
        res = adapter.place_order(
            symbol="RELIANCE",
            instrument_token="NSE_FO|RELIANCE26NOV2600CE",
            quantity=250,
            transaction_type="BUY",
            market_quote=shared_cache["NSE_FO|RELIANCE26NOV2600CE"],
        )
        t_elapsed = time.time() - t0

        # Must return in under 20ms without blocking!
        self.assertLess(t_elapsed, 0.05)
        self.assertEqual(res["status"], "OPEN")
        self.assertEqual(res["api_status"], "delayed_in_flight")
        oid = res["order_id"]

        # Wait for delay to elapse
        time.sleep(0.20)

        # Order must now be filled!
        st = adapter.get_order_status(oid)
        self.assertEqual(st["status"], "FILLED")
        self.assertEqual(st["fill_price"], 55.0)
        self.assertEqual(len(adapter.get_positions()), 1)

    def test_multi_executor_paper_delayed_factory_integration(self):
        """Verify MultiExecutionManager creates and manages paper_delayed adapter."""
        multi = create_multi_executor("paper,paper_delayed,live", capital=150_000.0, log_dir=self.temp_dir.name)
        self.assertIn("paper", multi.adapters)
        self.assertIn("paper_delayed", multi.adapters)
        self.assertIn("live", multi.adapters)

        # Propagate quotes cache
        test_cache = {"TEST": {"bid": 10.0, "ask": 10.5}}
        multi.set_quotes_cache(test_cache)
        dly_adapter = multi.get_adapter("paper_delayed")
        self.assertIs(dly_adapter.quotes_cache, test_cache)

        # Freeze paper_delayed mode
        multi.freeze_mode("paper_delayed")
        self.assertTrue(multi.is_frozen("paper_delayed"))
        self.assertFalse(multi.is_frozen("paper"))

        # Blocked entry when frozen
        res = multi.place_order(
            symbol="TEST",
            instrument_token="TEST_TOKEN",
            quantity=10,
            transaction_type="BUY",
            market_quote={"ask": 10.5, "bid": 10.0},
        )
        self.assertEqual(res["paper_delayed"]["status"], "BLOCKED")

    def test_no_fallback_to_initial_quote_when_cache_active(self):
        """
        Verify that when quotes_cache is active, the adapter NEVER falls back
        to initial_quote if quotes_cache is missing the contract.
        Order must be rejected cleanly rather than filling against stale initial_quote.
        """
        active_cache = {}  # Cache is active but empty for this contract
        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            min_delay_ms=0.0,
            max_delay_ms=0.0,
            quotes_cache=active_cache,
            async_execution=False,
        )

        stale_quote = {"bid": 9.0, "ask": 9.5, "ltp": 9.5}
        res = adapter.place_order(
            symbol="HDFCAMC",
            instrument_token="NSE_FO|HDFCAMC26NOV2600PE",
            quantity=300,
            transaction_type="BUY",
            market_quote=stale_quote,  # Must NOT be used!
        )

        # Must reject due to missing quotes in active cache
        self.assertEqual(res["status"], "REJECTED_NO_QUOTE")
        self.assertEqual(len(adapter.get_positions()), 0)

    def test_sell_order_failure_preserves_position(self):
        """
        CRITICAL TEST: When quotes_cache is missing quotes for a SELL (liquidation) order,
        the adapter must reject the order (REJECTED_NO_QUOTE) and PRESERVE the position
        in self.positions so it is never dropped or liquidated at ₹0 / fake prices.
        """
        shared_cache = {
            "NSE_FO|SBIN26NOV800CE": {"bid": 24.5, "ask": 25.0, "ltp": 25.0}
        }
        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            min_delay_ms=0.0,
            max_delay_ms=0.0,
            quotes_cache=shared_cache,
            async_execution=False,
        )

        # 1. Successful BUY entry
        res_buy = adapter.place_order(
            symbol="SBIN",
            instrument_token="NSE_FO|SBIN26NOV800CE",
            quantity=1500,
            transaction_type="BUY",
        )
        self.assertEqual(res_buy["status"], "FILLED")
        self.assertEqual(len(adapter.get_positions()), 1)

        # 2. Market feed drops or loses quote for this option
        del shared_cache["NSE_FO|SBIN26NOV800CE"]

        # 3. Attempt SELL order while quote is missing
        res_sell = adapter.place_order(
            symbol="SBIN",
            instrument_token="NSE_FO|SBIN26NOV800CE",
            quantity=1500,
            transaction_type="SELL",
            tag="SL_HIT",
        )

        # Order must be rejected with REJECTED_NO_QUOTE
        self.assertEqual(res_sell["status"], "REJECTED_NO_QUOTE")
        # Position must be PRESERVED in adapter!
        positions = adapter.get_positions()
        self.assertEqual(len(positions), 1, "Position must NOT be silently dropped when sell quote is missing!")
        self.assertEqual(positions[0]["symbol"], "SBIN")

    def test_retry_mechanism_fills_when_quote_arrives(self):
        """
        Verify that retry loop queries quotes_cache multiple times and succeeds
        as soon as a delayed websocket tick arrives during the retry window.
        """
        import threading
        shared_cache = {}
        adapter = PaperDelayedExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=self.csv_path,
            min_delay_ms=0.0,
            max_delay_ms=0.0,
            quotes_cache=shared_cache,
            async_execution=False,
        )

        def delayed_tick_feed():
            time.sleep(0.10)  # Arrives 100ms later during retry loop
            shared_cache["NSE_FO|INFY26NOV1800CE"] = {"bid": 30.0, "ask": 30.5, "ltp": 30.5}

        t = threading.Thread(target=delayed_tick_feed)
        t.start()

        res = adapter.place_order(
            symbol="INFY",
            instrument_token="NSE_FO|INFY26NOV1800CE",
            quantity=400,
            transaction_type="BUY",
        )
        t.join()

        self.assertEqual(res["status"], "FILLED")
        self.assertEqual(res["fill_price"], 30.5)
        self.assertEqual(len(adapter.get_positions()), 1)


if __name__ == "__main__":
    unittest.main()

