#!/usr/bin/env python3
"""
tests/execution/test_live_reconciliation.py
===========================================
Comprehensive unit tests for Live Trading Failure Modes,
Periodic Broker Position Conflation, and Reconciliation Engine in GrowwLiveAdapter:

1. Ghost Position Resolution (Broker == 0, Local > 0) via manual mobile exit or broker RMS.
2. Settlement Quarantine Window (grace period preventing false alarms during order clearing).
3. Orphan Position Adoption (Broker > 0, Local == 0) via network drop or manual entry.
4. Quantity Discrepancy Adjustment (partial fills / partial scale-out).
5. State Persistence & Crash Recovery (`logs/live_positions_state.json`).
6. Session Invalidation (HTTP 401/403) Auto-Freeze Trigger.
7. Timeout Idempotency Guard (verifying order before retrying to prevent duplicate orders).
8. End-of-Day MIS Auto-Squareoff Guard (15:10 IST freeze, 15:15 IST squareoff).
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.superstructures.live import GrowwLiveAdapter, LiveSuperstructure
from core.feed.position_registry import ProtectedPositionRegistry


class MockGrowwReconciliationClient:
    """Mock GrowwAPI client with configurable positions book."""

    EXCHANGE_NSE = "NSE"
    SEGMENT_FNO = "FNO"
    PRODUCT_NRML = "NRML"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_MARKET = "MARKET"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, initial_positions=None):
        self.positions_list = initial_positions or []
        self.placed_orders = []

    def get_positions_for_user(self):
        return {
            "status": "SUCCESS",
            "payload": {
                "net_positions": self.positions_list
            }
        }

    def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        return {
            "status": "SUCCESS",
            "payload": {
                "groww_order_id": f"GW-ORD-{len(self.placed_orders)}",
                "order_status": "FILLED",
                "order_reference_id": kwargs.get("order_reference_id"),
            }
        }

    def modify_order(self, **kwargs):
        return {"status": "SUCCESS", "order_id": kwargs.get("groww_order_id")}

    def cancel_order(self, **kwargs):
        return {"status": "SUCCESS", "order_id": kwargs.get("groww_order_id")}

    def get_order_detail(self, **kwargs):
        ref_id = kwargs.get("order_reference_id")
        for o in self.placed_orders:
            if o.get("order_reference_id") == ref_id:
                return {
                    "groww_order_id": "GW-MATCHED-123",
                    "order_status": "FILLED",
                    "price": o.get("price", 100.0),
                }
        return None


class TestLiveReconciliationAndFailureModes(unittest.TestCase):
    """Test suite covering all live failure modes and synchronization logic."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.temp_dir.name, "live_positions_state.json")
        self.csv_file = os.path.join(self.temp_dir.name, "live_trades.csv")
        self.registry_file = os.path.join(self.temp_dir.name, "active_positions.json")

        # Reset ProtectedPositionRegistry with isolated file
        self.registry = ProtectedPositionRegistry.get_instance(registry_file=self.registry_file)
        self.registry.registry_file = self.registry_file
        self.registry.clear()

    def tearDown(self):
        self.temp_dir.cleanup()
        # Clean any freeze flag created
        if os.path.exists("logs/.freeze_live"):
            try:
                os.remove("logs/.freeze_live")
            except Exception:
                pass

    def test_ghost_position_reconciliation(self):
        """
        Verify Ghost Position Resolution:
        When a position is open locally but broker net quantity is 0 (closed on mobile app or RMS),
        reconciliation must gracefully close local position, update P&L, and unprotect contract.
        """
        # Mock broker returns empty positions
        mock_client = MockGrowwReconciliationClient(initial_positions=[])

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
            brokerage_per_order=20.0,
        )

        # 1. Establish open local position
        pos_key = "TCS_NSE_FO|TCS26NOV3500CE"
        now_ts = time.time() - 30.0  # Entered 30 seconds ago (past quarantine)
        adapter.positions[pos_key] = {
            "order_id": "GW-TCS-001",
            "symbol": "TCS",
            "instrument_token": "NSE_FO|TCS26NOV3500CE",
            "quantity": 175,
            "entry_price": 50.0,
            "entry_time": now_ts,
            "cost": 50.0 * 175,
            "tag": "DYNAMIC_TRIO",
        }
        self.registry.register_open_position(
            contract_key="NSE_FO|TCS26NOV3500CE",
            symbol="TCS",
            contract_symbol="TCS26NOV3500CE",
            mode="live",
            track="DYNAMIC_TRIO",
            trade_id="GW-TCS-001",
            quantity=175,
            entry_price=50.0,
        )

        self.assertIn("NSE_FO|TCS26NOV3500CE", self.registry.get_protected_keys())
        self.assertEqual(len(adapter.get_positions()), 1)

        # 2. Run reconciliation (quarantine = 10s)
        res = adapter.reconcile_broker_positions(quarantine_sec=10.0)

        # 3. Assertions
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["ghost_positions_closed"]), 1)
        self.assertEqual(res["ghost_positions_closed"][0]["symbol"], "TCS26NOV3500CE")

        # Local position must be cleared
        self.assertEqual(len(adapter.get_positions()), 0)
        self.assertEqual(len(adapter.closed_trades), 1)
        self.assertEqual(adapter.closed_trades[0]["reason"], "BROKER_MANUAL_SQUAREOFF")

        # Contract must be unprotected from registry
        self.assertNotIn("NSE_FO|TCS26NOV3500CE", self.registry.get_protected_keys())

    def test_settlement_quarantine_window_prevents_false_alarms(self):
        """
        Verify Settlement Quarantine Window:
        Orders placed within quarantine_sec (e.g. 10s) must NOT be closed as ghost positions
        even if the broker positions book has not yet settled the fill (broker qty == 0).
        """
        mock_client = MockGrowwReconciliationClient(initial_positions=[])

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        # Position entered only 2 seconds ago
        now_ts = time.time() - 2.0
        pos_key = "INFY_NSE_FO|INFY26NOV1800CE"
        adapter.positions[pos_key] = {
            "order_id": "GW-INFY-001",
            "symbol": "INFY",
            "instrument_token": "NSE_FO|INFY26NOV1800CE",
            "quantity": 300,
            "entry_price": 30.0,
            "entry_time": now_ts,
            "tag": "ATM",
        }

        # Run reconciliation with 10s quarantine
        res = adapter.reconcile_broker_positions(quarantine_sec=10.0)

        # Must skip due to quarantine!
        self.assertIn("INFY26NOV1800CE", res["quarantined_positions_skipped"])
        self.assertEqual(len(res["ghost_positions_closed"]), 0)
        # Position is safely retained
        self.assertEqual(len(adapter.get_positions()), 1)

    def test_orphan_position_adoption(self):
        """
        Verify Orphan Position Adoption:
        When the broker holds an open position that local memory has lost (e.g. timeout on placement
        or manual mobile buy), reconciliation must adopt it into tracking and protect its contract.
        """
        # Broker reports open position for RELIANCE
        mock_client = MockGrowwReconciliationClient(initial_positions=[
            {
                "trading_symbol": "RELIANCE26NOV2600CE",
                "quantity": 250,
                "buy_price": 55.0,
                "sell_price": 0.0,
                "segment": "FNO",
            }
        ])

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        # Initially local has 0 positions
        self.assertEqual(len(adapter.get_positions()), 0)

        # Run reconciliation
        res = adapter.reconcile_broker_positions(quarantine_sec=0.0)

        # Assert orphan adopted
        self.assertEqual(len(res["orphan_positions_adopted"]), 1)
        self.assertEqual(res["orphan_positions_adopted"][0]["symbol"], "RELIANCE26NOV2600CE")
        self.assertEqual(res["orphan_positions_adopted"][0]["quantity"], 250)

        # Local state now holds adopted position
        positions = adapter.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "RELIANCE26NOV2600CE")
        self.assertEqual(positions[0]["quantity"], 250)
        self.assertEqual(positions[0]["entry_price"], 55.0)
        self.assertTrue(positions[0].get("adopted"))

        # Contract must be registered in ProtectedPositionRegistry
        self.assertIn("NSE_FO|RELIANCE26NOV2600CE", self.registry.get_protected_keys())

    def test_quantity_discrepancy_adjustment(self):
        """
        Verify Quantity Discrepancy Adjustment:
        When broker reports a different filled quantity (e.g. partial scale out or partial fill),
        local position quantity is synchronized with broker reality.
        """
        mock_client = MockGrowwReconciliationClient(initial_positions=[
            {
                "trading_symbol": "SBIN26NOV800CE",
                "quantity": 750,  # Scaled down from 1500 to 750
                "buy_price": 15.0,
            }
        ])

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        # Local has 1500
        now_ts = time.time() - 20.0
        pos_key = "SBIN_NSE_FO|SBIN26NOV800CE"
        adapter.positions[pos_key] = {
            "order_id": "GW-SBIN-001",
            "symbol": "SBIN",
            "instrument_token": "NSE_FO|SBIN26NOV800CE",
            "quantity": 1500,
            "entry_price": 15.0,
            "entry_time": now_ts,
            "tag": "ATM",
        }

        res = adapter.reconcile_broker_positions(quarantine_sec=10.0)

        self.assertEqual(len(res["quantity_mismatches_adjusted"]), 1)
        self.assertEqual(res["quantity_mismatches_adjusted"][0]["old_quantity"], 1500)
        self.assertEqual(res["quantity_mismatches_adjusted"][0]["new_quantity"], 750)
        self.assertEqual(adapter.positions[pos_key]["quantity"], 750)

    def test_state_persistence_and_crash_recovery(self):
        """
        Verify State Persistence:
        Positions written to logs/live_positions_state.json must survive crash/restart
        and be reloaded seamlessly by a new adapter instance.
        """
        adapter1 = GrowwLiveAdapter(
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=False,
            enable_polling=False,
        )

        # Place simulated fill
        adapter1.place_order(
            symbol="TATAMOTORS",
            instrument_token="NSE_FO|TATAMOTORS26NOV1000CE",
            quantity=550,
            transaction_type="BUY",
            price=25.0,
            market_quote={"ask": 25.0, "bid": 24.5},
        )
        self.assertEqual(len(adapter1.get_positions()), 1)
        self.assertTrue(os.path.exists(self.state_file))

        # Simulate crash/reboot: create fresh adapter2 pointing to the same state_file
        adapter2 = GrowwLiveAdapter(
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=False,
            enable_polling=False,
        )

        # Adapter 2 must have restored the position from disk!
        restored = adapter2.get_positions()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["symbol"], "TATAMOTORS")
        self.assertEqual(restored[0]["quantity"], 550)
        self.assertEqual(restored[0]["entry_price"], 25.0)

    def test_session_expiry_401_triggers_emergency_freeze(self):
        """
        Verify Session Expiration Guard:
        When an HTTP 401/403 or unauthorized error occurs, the adapter must mark
        session inactive and create logs/.freeze_live to block all subsequent entries.
        """
        # Create a mock client whose get_positions raises 401 Unauthorized
        failing_client = MagicMock()
        failing_client.get_positions_for_user.side_effect = Exception("HTTP 401 Unauthorized: token expired")

        adapter = GrowwLiveAdapter(
            access_token="expired_token",
            groww_client=failing_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        res = adapter.reconcile_broker_positions(quarantine_sec=0.0)

        # Assert session marked inactive and freeze flag created
        self.assertFalse(adapter.is_session_active)
        self.assertEqual(res["status"], "AUTH_EXPIRED")
        self.assertTrue(os.path.exists("logs/.freeze_live"))

        # Subsequent orders must be rejected with REJECTED_SESSION_EXPIRED
        ord_res = adapter.place_order(
            symbol="NIFTY",
            instrument_token="NSE_FO|NIFTY26NOV25000CE",
            quantity=50,
            transaction_type="BUY",
            price=100.0,
            market_quote={"ask": 100.0, "bid": 99.0},
        )
        self.assertEqual(ord_res["status"], "REJECTED_SESSION_EXPIRED")

    def test_timeout_idempotency_guard(self):
        """
        Verify Timeout Idempotency Guard:
        When an order placement experiences a socket timeout, the adapter queries
        the broker with the reference_id and resolves the fill without duplicate ordering.
        """
        mock_client = MockGrowwReconciliationClient()
        # Seed placed order in mock client matching our reference ID
        mock_client.placed_orders.append({
            "order_reference_id": "GW-TEST-REF-999",
            "price": 42.0,
        })

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        # Call resolve timed out order directly
        resolved = adapter._resolve_timed_out_order(
            ref_id="GW-TEST-REF-999",
            trading_symbol="RELIANCE26NOV2600CE",
            quantity=250,
            trans_type="BUY",
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["order_id"], "GW-MATCHED-123")
        self.assertTrue(resolved["resolved_from_timeout"])

    def test_eod_mis_auto_squareoff_guard(self):
        """
        Verify End-of-Day RMS Liquidation Guard:
        Simulate 15:15 IST: checks that open intraday positions are squared off
        before broker liquidation penalty kicks in.
        """
        mock_client = MockGrowwReconciliationClient()

        adapter = GrowwLiveAdapter(
            access_token="test_token",
            groww_client=mock_client,
            output_csv=self.csv_file,
            state_file=self.state_file,
            live_confirmed=True,
            enable_polling=False,
        )

        # Open 1 position
        adapter.positions["TCS_NSE_FO|TCS26NOV3500CE"] = {
            "order_id": "GW-EOD-001",
            "symbol": "TCS",
            "instrument_token": "NSE_FO|TCS26NOV3500CE",
            "quantity": 175,
            "entry_price": 50.0,
            "entry_time": time.time() - 3600,
            "tag": "DYNAMIC_TRIO",
        }

        # Construct a timestamp corresponding to 15:15:30 IST
        ist = timezone(timedelta(hours=5, minutes=30))
        simulated_eod_time = datetime(2026, 11, 7, 15, 15, 30, tzinfo=ist).timestamp()

        quotes = {"NSE_FO|TCS26NOV3500CE": {"bid": 52.0, "ask": 52.5, "ltp": 52.0}}
        exited = adapter.check_eod_squareoff(now_ts=simulated_eod_time, quotes_cache=quotes)

        # Assert squareoff order dispatched
        self.assertEqual(len(exited), 1)
        self.assertEqual(len(mock_client.placed_orders), 1)
        self.assertEqual(mock_client.placed_orders[0]["transaction_type"], "SELL")
        self.assertEqual(mock_client.placed_orders[0]["trading_symbol"], "TCS26NOV3500CE")
        self.assertTrue(os.path.exists("logs/.freeze_live"))


if __name__ == "__main__":
    unittest.main()

