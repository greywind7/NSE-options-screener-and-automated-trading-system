#!/usr/bin/env python3
"""
tests/infrastructure/test_position_registry.py
==============================================
Unit tests for ProtectedPositionRegistry:
1. Tests open position registration and tracking across modes/tracks.
2. Tests multi-position retention (contract stays protected until all positions close).
3. Tests cross-process atomic disk persistence and synchronization.
4. Tests integration with MarketDataListener to ensure open positions are never unsubscribed.
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.feed.position_registry import ProtectedPositionRegistry
from core.feed.upstox_listener import MarketDataListener


class TestProtectedPositionRegistry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.reg_file = os.path.join(self.temp_dir.name, "active_positions.json")
        self.registry = ProtectedPositionRegistry(registry_file=self.reg_file)

    def tearDown(self):
        self.registry.clear()
        self.temp_dir.cleanup()

    def test_register_and_unregister_single_position(self):
        """Verify contract is protected on open and unprotected on exit."""
        contract_key = "NSE_FO|RELIANCE26NOV2500CE"
        self.assertEqual(len(self.registry.get_protected_keys()), 0)

        # 1. Register open position
        self.registry.register_open_position(
            contract_key=contract_key,
            symbol="RELIANCE",
            contract_symbol="RELIANCE 2500 CE",
            mode="live",
            track="ATM",
            trade_id="TR_101",
            quantity=250,
            entry_price=45.5,
        )

        protected = self.registry.get_protected_keys()
        self.assertIn("NSE_FO|RELIANCE26NOV2500CE", protected)
        self.assertIn("NSE_FO:RELIANCE26NOV2500CE", protected)

        summary = self.registry.get_protected_summary()
        self.assertIn("NSE_FO|RELIANCE26NOV2500CE", summary)
        self.assertEqual(summary["NSE_FO|RELIANCE26NOV2500CE"]["open_positions_count"], 1)
        self.assertEqual(summary["NSE_FO|RELIANCE26NOV2500CE"]["holders"], ["LIVE:ATM"])

        # 2. Register closed position
        self.registry.register_closed_position(
            contract_key=contract_key,
            mode="live",
            track="ATM",
            trade_id="TR_101",
        )

        self.assertEqual(len(self.registry.get_protected_keys()), 0)

    def test_multi_position_coexistence(self):
        """If two tracks/modes hold the same contract, it remains protected until BOTH exit."""
        contract_key = "NSE_FO|NIFTY26NOV24500CE"

        # Track 1: SIM ATM
        self.registry.register_open_position(
            contract_key=contract_key,
            symbol="NIFTY",
            contract_symbol="NIFTY 24500 CE",
            mode="sim",
            track="ATM",
            trade_id="TR_SIM_1",
        )

        # Track 2: PAPER OTM_MOMENTUM
        self.registry.register_open_position(
            contract_key=contract_key,
            symbol="NIFTY",
            contract_symbol="NIFTY 24500 CE",
            mode="paper",
            track="OTM_MOMENTUM",
            trade_id="TR_PAP_2",
        )

        summary = self.registry.get_protected_summary()
        self.assertEqual(summary["NSE_FO|NIFTY26NOV24500CE"]["open_positions_count"], 2)
        self.assertIn("SIM:ATM", summary["NSE_FO|NIFTY26NOV24500CE"]["holders"])
        self.assertIn("PAPER:OTM_MOMENTUM", summary["NSE_FO|NIFTY26NOV24500CE"]["holders"])

        # Close first position: contract MUST STILL BE PROTECTED!
        self.registry.register_closed_position(
            contract_key=contract_key,
            mode="sim",
            track="ATM",
            trade_id="TR_SIM_1",
        )
        self.assertIn(contract_key, self.registry.get_protected_keys())
        self.assertEqual(self.registry.get_protected_summary()[contract_key]["open_positions_count"], 1)

        # Close second position: now it can be safely pruned
        self.registry.register_closed_position(
            contract_key=contract_key,
            mode="paper",
            track="OTM_MOMENTUM",
            trade_id="TR_PAP_2",
        )
        self.assertNotIn(contract_key, self.registry.get_protected_keys())

    def test_disk_persistence_and_cross_process_sync(self):
        """Verify atomic disk writes allow an independent process instance to read protected keys."""
        contract_key = "NSE_FO|TCS26NOV3500PE"

        self.registry.register_open_position(
            contract_key=contract_key,
            symbol="TCS",
            contract_symbol="TCS 3500 PE",
            mode="paper_delayed",
            track="ITM",
            trade_id="TR_SB_5",
        )

        # Verify disk file exists and contains valid JSON
        self.assertTrue(os.path.exists(self.reg_file))
        with open(self.reg_file, "r") as f:
            data = json.load(f)
        self.assertIn("positions_by_contract", data)
        self.assertIn(contract_key, data["positions_by_contract"])

        # Spawn a simulated second process registry pointing to the same file
        process_2_registry = ProtectedPositionRegistry(registry_file=self.reg_file)
        keys_in_proc2 = process_2_registry.get_protected_keys()
        self.assertIn(contract_key, keys_in_proc2)

    def test_listener_refuses_to_unsubscribe_protected_contract(self):
        """
        Verify that even when the underlying moves drastically and dynamic strike selection
        attempts to prune an option, the listener detects an open position and REFUSES to unsubscribe!
        """
        ipc_path = os.path.join(self.temp_dir.name, "test_prot.ipc")
        listener = MarketDataListener(
            mode="mock",
            feed_equity_ipc="inproc://test_posreg_eq1",
            feed_options_ipc="inproc://test_posreg_opt1",
            ipc_path=ipc_path,
            poll_interval=0.1,
            enable_socket=False,
            debug_log=None,
            silent=True,
        )
        listener.position_registry = self.registry

        # Setup mock options map
        listener.options_map = {
            "TEST_STOCK": {
                "nearest_expiry": "2026-11-23",
                "strikes": {
                    "2400.0": {"CE": "NSE_FO|2400CE", "PE": "NSE_FO|2400PE"},
                    "2420.0": {"CE": "NSE_FO|2420CE", "PE": "NSE_FO|2420PE"},
                    "2440.0": {"CE": "NSE_FO|2440CE", "PE": "NSE_FO|2440PE"},
                    "2460.0": {"CE": "NSE_FO|2460CE", "PE": "NSE_FO|2460PE"},
                    "2480.0": {"CE": "NSE_FO|2480CE", "PE": "NSE_FO|2480PE"},
                    "2500.0": {"CE": "NSE_FO|2500CE", "PE": "NSE_FO|2500PE"},
                    "2520.0": {"CE": "NSE_FO|2520CE", "PE": "NSE_FO|2520PE"},
                    "2540.0": {"CE": "NSE_FO|2540CE", "PE": "NSE_FO|2540PE"},
                    "2560.0": {"CE": "NSE_FO|2560CE", "PE": "NSE_FO|2560PE"},
                    "2580.0": {"CE": "NSE_FO|2580CE", "PE": "NSE_FO|2580PE"},
                    "2600.0": {"CE": "NSE_FO|2600CE", "PE": "NSE_FO|2600PE"},
                }
            }
        }
        listener.available_strikes_by_symbol = {
            "TEST_STOCK": [2400.0, 2420.0, 2440.0, 2460.0, 2480.0, 2500.0, 2520.0, 2540.0, 2560.0, 2580.0, 2600.0]
        }
        listener.underlying_key_to_symbol = {
            "NSE_EQ|INE999A01010": "TEST_STOCK",
            "TEST_STOCK": "TEST_STOCK",
        }
        listener.symbol_metadata = {
            "TEST_STOCK": {"underlying_key": "NSE_EQ|INE999A01010", "name": "TEST STOCK"}
        }

        # 1. Initial ATM evaluation at 2460 (subscribes to strikes around 2460, including 2440CE)
        to_add, to_remove = listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2460.0)
        self.assertIn("NSE_FO|2440CE", listener.active_options_by_symbol["TEST_STOCK"])

        # 2. Strategy enters an active position in NSE_FO|2440CE!
        self.registry.register_open_position(
            contract_key="NSE_FO|2440CE",
            symbol="TEST_STOCK",
            contract_symbol="TEST STOCK 2440 CE",
            mode="live",
            track="ATM",
            trade_id="TR_LIVE_99",
        )

        # 3. Market skyrockets to 2600!
        # Normally 2440CE would be outside ATM+-3 and outside hysteresis (+-5), so normally it would be pruned.
        listener.last_reval_time_by_symbol["TEST_STOCK"] = 0.0
        to_add_rally, to_remove_rally = listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2600.0)

        # CRITICAL ASSERTION: 2440CE MUST NOT BE IN to_remove_rally!
        self.assertNotIn("NSE_FO|2440CE", to_remove_rally, "CRITICAL ERROR: Listener attempted to unsubscribe from an active position!")
        self.assertIn("NSE_FO|2440CE", listener.active_options_by_symbol["TEST_STOCK"], "Protected contract was removed from active subscriptions!")

        # 4. Strategy exits position
        self.registry.register_closed_position(
            contract_key="NSE_FO|2440CE",
            mode="live",
            track="ATM",
            trade_id="TR_LIVE_99",
        )

        # 5. Subsequent re-evaluation can now safely prune it
        listener.last_reval_time_by_symbol["TEST_STOCK"] = 0.0
        to_add_after_exit, to_remove_after_exit = listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2600.0)
        self.assertIn("NSE_FO|2440CE", to_remove_after_exit, "After closing position, contract should be pruned normally.")
        listener.stop()

    def test_dated_positions_and_outdated_purging(self):
        """Verify that positions from previous dates are NOT protected and are cleanly purged."""
        yesterday_ts = time.time() - 86400 * 2  # 2 days ago
        today_ts = time.time()

        old_key = "NSE_FO|OLD26NOV1000CE"
        today_key = "NSE_FO|TODAY26NOV2000CE"

        # Register old position
        self.registry.register_open_position(
            contract_key=old_key,
            symbol="OLD_STOCK",
            contract_symbol="OLD 1000 CE",
            mode="sim",
            track="ATM",
            trade_id="TR_OLD",
            entry_time=yesterday_ts,
        )

        # Register today position
        self.registry.register_open_position(
            contract_key=today_key,
            symbol="TODAY_STOCK",
            contract_symbol="TODAY 2000 CE",
            mode="live",
            track="ATM",
            trade_id="TR_TODAY",
            entry_time=today_ts,
        )

        # get_protected_keys() must strictly return TODAY's key, not the outdated one
        protected = self.registry.get_protected_keys()
        self.assertIn(today_key, protected)
        self.assertNotIn(old_key, protected)

        # purge_outdated_positions() removes the old position from storage
        purged_count = self.registry.purge_outdated_positions()
        self.assertGreaterEqual(purged_count, 1)

        summary = self.registry.get_protected_summary()
        self.assertIn(today_key, summary)
        self.assertNotIn(old_key, summary)

    def test_dynamic_options_equivalent_fall_under_capacity_limit(self):
        """
        Verify that when capacity ceiling (max_stream_contracts) is reached,
        unheld dynamic options take the equivalent fall to make room for new/protected options,
        while protected contracts are NEVER pruned.
        """
        ipc_path = os.path.join(self.temp_dir.name, "test_cap.ipc")
        listener = MarketDataListener(
            mode="mock",
            feed_equity_ipc="inproc://test_posreg_eq2",
            feed_options_ipc="inproc://test_posreg_opt2",
            ipc_path=ipc_path,
            poll_interval=0.1,
            enable_socket=False,
            debug_log=None,
            silent=True,
            max_stream_contracts=6,  # Small limit for testing
        )
        listener.position_registry = self.registry
        listener.underlying_keys = {"NSE_EQ|STOCK_A", "NSE_EQ|STOCK_B"}
        listener.underlying_key_to_symbol = {"NSE_EQ|STOCK_A": "STOCK_A", "NSE_EQ|STOCK_B": "STOCK_B"}

        # Suppose we already have active dynamic options
        listener.active_options_by_symbol["STOCK_A"] = {"NSE_FO|OPT_A_1", "NSE_FO|OPT_A_2"}
        listener.active_options_by_symbol["STOCK_B"] = {"NSE_FO|OPT_B_1", "NSE_FO|OPT_B_2"}
        listener.active_option_keys = {"NSE_FO|OPT_A_1", "NSE_FO|OPT_A_2", "NSE_FO|OPT_B_1", "NSE_FO|OPT_B_2"}
        # Current total: 2 underlyings + 4 options = 6 contracts (at capacity limit)

        # Protect OPT_A_1
        self.registry.register_open_position(
            contract_key="NSE_FO|OPT_A_1",
            symbol="STOCK_A",
            contract_symbol="STOCK_A 100 CE",
            mode="live",
            track="ATM",
            trade_id="TR_1",
        )

        # New options to add: 2 new contracts for STOCK_A
        keys_to_add = {"NSE_FO|OPT_A_3", "NSE_FO|OPT_A_4"}
        protected_keys = self.registry.get_protected_keys()

        # Call _prune_dynamic_options_to_fit
        pruned = listener._prune_dynamic_options_to_fit(keys_to_add, protected_keys, "STOCK_A")

        # Must prune 2 unheld dynamic options (equivalent fall)
        self.assertEqual(len(pruned), 2)
        # Protected contract must NOT be pruned
        self.assertNotIn("NSE_FO|OPT_A_1", pruned)
        # Total projected contracts must not exceed max_stream_contracts (6)
        remaining_options = (listener.active_option_keys - pruned) | keys_to_add
        total_projected = len(listener.underlying_keys) + len(remaining_options)
        self.assertLessEqual(total_projected, listener.max_stream_contracts)
        listener.stop()


if __name__ == "__main__":
    unittest.main()

