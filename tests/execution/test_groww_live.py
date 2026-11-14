#!/usr/bin/env python3
"""
tests/execution/test_groww_live.py
==================================
Unit tests for Groww Live Execution Adapter targeting Groww Trading API (`growwapi`).
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.live.live_adapter import (
    GrowwLiveAdapter,
    LiveExecutionAdapter,
    generate_groww_access_token,
)


class MockGrowwAPIClient:
    """Mock GrowwAPI client mimicking the official growwapi SDK."""

    EXCHANGE_NSE = "NSE"
    EXCHANGE_BSE = "BSE"
    SEGMENT_CASH = "CASH"
    SEGMENT_FNO = "FNO"
    PRODUCT_NRML = "NRML"
    PRODUCT_MIS = "MIS"
    PRODUCT_CNC = "CNC"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_STOP_LOSS = "SL"
    ORDER_TYPE_STOP_LOSS_MARKET = "SL_M"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    VALIDITY_DAY = "DAY"
    SMART_ORDER_TYPE_OCO = "OCO"

    def __init__(self):
        self.placed_orders = []
        self.smart_orders = []
        self.modified_orders = []
        self.cancelled_orders = []

    def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        order_num = len(self.placed_orders)
        return {
            "status": "SUCCESS",
            "payload": {
                "groww_order_id": f"GROWW-{order_num}",
                "order_status": "OPEN",
                "order_reference_id": kwargs.get("order_reference_id"),
            },
        }

    def create_smart_order(self, **kwargs):
        self.smart_orders.append(kwargs)
        return {
            "status": "SUCCESS",
            "payload": {
                "smart_order_id": "SMART-OCO-987654",
                "reference_id": kwargs.get("reference_id"),
            },
        }

    def modify_order(self, **kwargs):
        self.modified_orders.append(kwargs)
        return {"status": "SUCCESS", "order_id": kwargs.get("groww_order_id")}

    def cancel_order(self, **kwargs):
        self.cancelled_orders.append(kwargs)
        return {"status": "SUCCESS", "order_id": kwargs.get("groww_order_id")}

    def get_order_detail(self, **kwargs):
        return {
            "status": "SUCCESS",
            "payload": {
                "groww_order_id": kwargs.get("groww_order_id"),
                "order_status": "FILLED",
                "average_fill_price": 52.5,
                "filled_quantity": 250,
            },
        }

    def get_order_status(self, **kwargs):
        return {
            "status": "SUCCESS",
            "payload": {
                "groww_order_id": kwargs.get("groww_order_id"),
                "order_status": "FILLED",
            },
        }


class TestGrowwLiveAdapter(unittest.TestCase):
    """Test suite for GrowwLiveAdapter implementation."""

    def test_groww_adapter_initialization(self):
        """Verify initialization and backward compatibility alias."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            adapter = GrowwLiveAdapter(
                access_token="test_token_123",
                output_csv=csv_path,
                brokerage_per_order=20.0,
                live_confirmed=False,
                initial_capital=150_000.0,
                enable_polling=False,
            )
            self.assertEqual(adapter.initial_capital, 150_000.0)
            self.assertFalse(adapter.live_confirmed)
            self.assertTrue(os.path.exists(csv_path))
            self.assertIs(LiveExecutionAdapter, GrowwLiveAdapter)

    def test_groww_adapter_dry_run_safety(self):
        """Verify that when live_confirmed is False, adapter executes in dry-run mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            adapter = GrowwLiveAdapter(
                output_csv=csv_path,
                live_confirmed=False,
                enable_polling=False,
            )

            # Buy order in dry-run
            res = adapter.place_order(
                symbol="NSE_INDEX|Nifty 50",
                instrument_token="NSE_FO|NIFTY26NOV24500CE",
                quantity=50,
                transaction_type="BUY",
                order_type="MARKET",
                price=120.0,
                tag="DYNAMIC_TRIO",
                market_quote={"bid": 119.5, "ask": 120.5, "ltp": 120.0},
            )

            self.assertEqual(res.get("status"), "FILLED")
            self.assertTrue(res.get("simulated"))
            self.assertIn("live_confirmed=False", res.get("reason", ""))

            # Verify active position recorded
            positions = adapter.get_positions()
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0]["symbol"], "NSE_INDEX|Nifty 50")
            self.assertEqual(positions[0]["quantity"], 50)

    def test_groww_adapter_official_sdk_client_integration(self):
        """Verify that GrowwLiveAdapter routes order calls directly to the GrowwAPI client object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            mock_client = MockGrowwAPIClient()

            adapter = GrowwLiveAdapter(
                access_token="valid_token_xyz",
                groww_client=mock_client,
                output_csv=csv_path,
                live_confirmed=True,  # LIVE CONFIRMED
                enable_polling=False,
            )

            # 1. Place BUY order with resting Stop-Loss
            res = adapter.place_order(
                symbol="RELIANCE",
                instrument_token="NSE_FO|RELIANCE26NOV2600CE",
                quantity=250,
                transaction_type="BUY",
                order_type="LIMIT",
                price=50.0,
                stop_loss_price=45.0,
            )

            self.assertEqual(res.get("status"), "OPEN")
            self.assertEqual(res.get("order_id"), "GROWW-1")
            self.assertEqual(res.get("sl_order_id"), "GROWW-2")

            # Verify mock GrowwAPI received the place_order call for BUY entry
            self.assertEqual(len(mock_client.placed_orders), 2)
            buy_args = mock_client.placed_orders[0]
            self.assertEqual(buy_args["trading_symbol"], "RELIANCE26NOV2600CE")
            self.assertEqual(buy_args["quantity"], 250)
            self.assertEqual(buy_args["exchange"], "NSE")
            self.assertEqual(buy_args["segment"], "FNO")
            self.assertEqual(buy_args["product"], "NRML")
            self.assertEqual(buy_args["order_type"], "LIMIT")
            self.assertEqual(buy_args["transaction_type"], "BUY")
            self.assertEqual(buy_args["price"], 50.0)

            # Verify mock GrowwAPI received resting Stop-Loss SELL order
            sl_args = mock_client.placed_orders[1]
            self.assertEqual(sl_args["trading_symbol"], "RELIANCE26NOV2600CE")
            self.assertEqual(sl_args["quantity"], 250)
            self.assertEqual(sl_args["transaction_type"], "SELL")
            self.assertEqual(sl_args["order_type"], "SL")
            self.assertEqual(sl_args["trigger_price"], 45.0)

            # 2. Modify resting SL order (Trailing Stop Loss ratchet)
            mod_sl = adapter.update_stop_loss("RELIANCE", "NSE_FO|RELIANCE26NOV2600CE", 47.0)
            self.assertEqual(mod_sl.get("status"), "SUCCESS")
            self.assertEqual(len(mock_client.modified_orders), 1)
            self.assertEqual(mock_client.modified_orders[0]["groww_order_id"], "GROWW-2")
            self.assertEqual(mock_client.modified_orders[0]["trigger_price"], 47.0)

            # 3. Execution Exit: modifies resting SL order to MARKET order
            exit_res = adapter.place_order(
                symbol="RELIANCE",
                instrument_token="NSE_FO|RELIANCE26NOV2600CE",
                quantity=250,
                transaction_type="SELL",
                order_type="MARKET",
                market_quote={"bid": 52.0, "ask": 52.5, "ltp": 52.0},
            )
            self.assertEqual(exit_res.get("status"), "FILLED")
            self.assertEqual(exit_res.get("order_type"), "MARKET")
            self.assertEqual(len(adapter.get_positions()), 0)
            self.assertEqual(len(adapter.closed_trades), 1)
            # Verify SL order was modified to MARKET at broker level
            self.assertEqual(len(mock_client.modified_orders), 2)
            self.assertEqual(mock_client.modified_orders[1]["groww_order_id"], "GROWW-2")
            self.assertEqual(mock_client.modified_orders[1]["order_type"], "MARKET")

            # 4. Modify order via SDK directly
            mod_res = adapter.modify_order("GROWW-1", quantity=500, price=51.0)
            self.assertEqual(mod_res.get("status"), "SUCCESS")
            self.assertEqual(len(mock_client.modified_orders), 3)
            self.assertEqual(mock_client.modified_orders[2]["groww_order_id"], "GROWW-1")
            self.assertEqual(mock_client.modified_orders[2]["quantity"], 500)

            # 5. Poll order status via SDK
            st_res = adapter.poll_order_status("GROWW-1")
            self.assertEqual(st_res.get("status"), "FILLED")
            self.assertEqual(st_res.get("fill_price"), 52.5)

            # 6. Cancel order via SDK
            canc_res = adapter.cancel_order("GROWW-1")
            self.assertEqual(canc_res.get("status"), "SUCCESS")
            self.assertEqual(len(mock_client.cancelled_orders), 1)
            self.assertEqual(mock_client.cancelled_orders[0]["groww_order_id"], "GROWW-1")

    def test_groww_adapter_lifecycle_order_status_modify_cancel(self):
        """Verify get_order_status, modify_order, and cancel_order contracts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            adapter = GrowwLiveAdapter(
                output_csv=csv_path,
                live_confirmed=False,
                enable_polling=False,
            )

            # Submit order
            res = adapter.place_order(
                symbol="RELIANCE",
                instrument_token="NSE_FO|RELIANCE26NOV2600CE",
                quantity=250,
                transaction_type="BUY",
                price=50.0,
            )
            oid = res["order_id"]

            # 1. get_order_status
            st = adapter.get_order_status(oid)
            self.assertEqual(st.get("status"), "FILLED")
            self.assertEqual(st.get("quantity"), 250)

            # 2. modify_order
            mod_res = adapter.modify_order(oid, quantity=500, price=52.0)
            self.assertEqual(mod_res.get("status"), "success")
            st_mod = adapter.get_order_status(oid)
            self.assertEqual(st_mod.get("quantity"), 500)

            # 3. cancel_order
            canc_res = adapter.cancel_order(oid)
            self.assertEqual(canc_res.get("status"), "success")
            st_canc = adapter.get_order_status(oid)
            self.assertEqual(st_canc.get("status"), "CANCELLED")

    def test_groww_adapter_exit_and_pnl_summary(self):
        """Verify round-trip trade settlement, brokerage, and summary calculation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            adapter = GrowwLiveAdapter(
                output_csv=csv_path,
                brokerage_per_order=20.0,
                live_confirmed=False,
                initial_capital=100_000.0,
                enable_polling=False,
            )

            # 1. Entry Buy
            adapter.place_order(
                symbol="TCS",
                instrument_token="NSE_FO|TCS26NOV3500CE",
                quantity=175,
                transaction_type="BUY",
                price=50.0,
                market_quote={"ask": 50.0, "bid": 49.5},
            )
            self.assertEqual(len(adapter.get_positions()), 1)

            # 2. Exit Sell at profit
            adapter.place_order(
                symbol="TCS",
                instrument_token="NSE_FO|TCS26NOV3500CE",
                quantity=175,
                transaction_type="SELL",
                price=60.0,
                market_quote={"ask": 60.5, "bid": 60.0},
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
            self.assertEqual(summary["losses"], 0)
            self.assertEqual(summary["win_rate"], 100.0)
            self.assertEqual(summary["net_realized"], 1710.0)

    def test_groww_adapter_polling_lifecycle(self):
        """Verify background polling thread lifecycle and shutdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "live_trades.csv")
            adapter = GrowwLiveAdapter(
                output_csv=csv_path,
                live_confirmed=False,
                enable_polling=True,
                poll_interval_sec=0.1,
            )
            self.assertTrue(adapter._polling_running)
            self.assertIsNotNone(adapter._poll_thread)
            self.assertTrue(adapter._poll_thread.is_alive())

            # Shutdown cleanly
            adapter.shutdown()
            self.assertFalse(adapter._polling_running)
            self.assertFalse(adapter._poll_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
