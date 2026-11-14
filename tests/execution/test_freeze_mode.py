#!/usr/bin/env python3
"""
tests/execution/test_freeze_mode.py
===================================
Unit tests for Graceful Mode Freezing ('Stop Live' / 'Stop Mode').
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from execution.base import BaseExecutionAdapter
from execution.multi_executor import MultiExecutionManager
from execution.paper.paper_adapter import PaperExecutionAdapter


class TestFreezeMode(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_logs = os.path.join(self.test_dir, "logs")
        os.makedirs(self.test_logs, exist_ok=True)
        self.paper = PaperExecutionAdapter(
            initial_capital=100_000.0,
            output_csv=os.path.join(self.test_dir, "paper.csv"),
        )
        self.manager = MultiExecutionManager({"paper": self.paper}, log_dir=self.test_logs)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_freeze_blocks_new_entries(self):
        # 1. Unfrozen: BUY order executes normally
        res_open = self.manager.place_order(
            symbol="RELIANCE",
            instrument_token="NSE_FO|12345",
            quantity=250,
            transaction_type="BUY",
            price=30.0,
        )
        self.assertEqual(res_open["paper"]["status"], "FILLED")
        self.assertEqual(len(self.manager.get_positions()["paper"]), 1)

        # 2. Freeze paper mode
        self.manager.freeze_mode("paper")
        self.assertTrue(self.manager.is_frozen("paper"))

        # 3. Place new entry order while frozen -> MUST BE BLOCKED
        res_blocked = self.manager.place_order(
            symbol="TCS",
            instrument_token="NSE_FO|67890",
            quantity=150,
            transaction_type="BUY",
            price=50.0,
        )
        self.assertEqual(res_blocked["paper"]["status"], "BLOCKED")
        self.assertIn("FROZEN", res_blocked["paper"]["reason"])
        # Position count should still be 1 (TCS not entered!)
        self.assertEqual(len(self.manager.get_positions()["paper"]), 1)

        # 4. Exit/Sell order on existing RELIANCE position -> MUST BE PERMITTED
        res_exit = self.manager.place_order(
            symbol="RELIANCE",
            instrument_token="NSE_FO|12345",
            quantity=250,
            transaction_type="SELL",
            price=35.0,
            tag="TARGET_HIT",
        )
        self.assertEqual(res_exit["paper"]["status"], "FILLED")
        # Position successfully closed!
        self.assertEqual(len(self.manager.get_positions()["paper"]), 0)

    def test_unfreeze_resumes_entries(self):
        self.manager.freeze_mode("paper")
        self.assertTrue(self.manager.is_frozen("paper"))
        self.manager.unfreeze_mode("paper")
        self.assertFalse(self.manager.is_frozen("paper"))

        res = self.manager.place_order(
            symbol="INFY",
            instrument_token="NSE_FO|11111",
            quantity=100,
            transaction_type="BUY",
            price=20.0,
        )
        self.assertEqual(res["paper"]["status"], "FILLED")

    def test_check_freeze_flags_file_detection(self):
        logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        flag_file = os.path.join(logs_dir, ".freeze_paper")

        with open(flag_file, "w") as f:
            f.write("frozen\n")

        self.manager.check_freeze_flags(log_dir=logs_dir)
        self.assertTrue(self.manager.is_frozen("paper"))

    def test_freeze_all_then_unfreeze_one_in_memory(self):
        """
        Verify that if we freeze 'all', but then unfreeze ONE mode ('live'),
        'all' is removed from frozen_modes, and the other modes ('paper', 'paper_delayed')
        are added/retained in the frozen list.
        """
        multi_mgr = MultiExecutionManager({
            "live": self.paper,  # reuse paper adapter instance for testing
            "paper": self.paper,
            "paper_delayed": self.paper,
        }, log_dir=self.test_logs)

        # 1. Freeze 'all'
        multi_mgr.freeze_mode("all")
        self.assertTrue(multi_mgr.is_frozen("all"))
        self.assertTrue(multi_mgr.is_frozen("live"))
        self.assertTrue(multi_mgr.is_frozen("paper"))
        self.assertTrue(multi_mgr.is_frozen("paper_delayed"))

        # 2. Unfreeze ONE mode: 'live'
        multi_mgr.unfreeze_mode("live")

        # 3. CRITICAL VERIFICATION:
        # 'all' MUST be removed
        self.assertNotIn("all", multi_mgr.frozen_modes)
        # 'live' MUST NOT be frozen
        self.assertFalse(multi_mgr.is_frozen("live"))
        # The OTHER modes MUST be in frozen_modes
        self.assertIn("paper", multi_mgr.frozen_modes)
        self.assertIn("paper_delayed", multi_mgr.frozen_modes)
        self.assertTrue(multi_mgr.is_frozen("paper"))
        self.assertTrue(multi_mgr.is_frozen("paper_delayed"))

    def test_freeze_all_then_unfreeze_one_disk_flags(self):
        """
        Verify runner CLI flag handling:
        When 'all' is frozen and then 'live' is unfrozen:
        .freeze_all is removed, .freeze_live is removed,
        and .freeze_paper + .freeze_paper_delayed are created/retained.
        """
        import argparse
        from runner import cmd_freeze, cmd_unfreeze

        logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        # Monkeypatch current working directory logs
        orig_cwd = os.getcwd()
        try:
            os.chdir(self.test_dir)

            # 1. Freeze ALL
            cmd_freeze(argparse.Namespace(mode="all"))
            self.assertTrue(os.path.exists("logs/.freeze_all"))
            self.assertTrue(os.path.exists("logs/.freeze_live"))
            self.assertTrue(os.path.exists("logs/.freeze_paper"))
            self.assertTrue(os.path.exists("logs/.freeze_paper_delayed"))

            # 2. Unfreeze LIVE
            cmd_unfreeze(argparse.Namespace(mode="live"))

            # 3. .freeze_all and .freeze_live MUST be removed!
            self.assertFalse(os.path.exists("logs/.freeze_all"), ".freeze_all should be deleted!")
            self.assertFalse(os.path.exists("logs/.freeze_live"), ".freeze_live should be deleted!")

            # 4. The other modes MUST be retained on disk!
            self.assertTrue(os.path.exists("logs/.freeze_paper"), ".freeze_paper must be retained!")
            self.assertTrue(os.path.exists("logs/.freeze_paper_delayed"), ".freeze_paper_delayed must be retained!")

            # 5. Verify MultiExecutionManager check_freeze_flags picks this up
            multi_mgr = MultiExecutionManager({"live": self.paper, "paper": self.paper, "paper_delayed": self.paper})
            multi_mgr.check_freeze_flags(log_dir="logs")

            self.assertFalse(multi_mgr.is_frozen("live"))
            self.assertTrue(multi_mgr.is_frozen("paper"))
            self.assertTrue(multi_mgr.is_frozen("paper_delayed"))
            self.assertFalse(multi_mgr.is_frozen("all"))

        finally:
            os.chdir(orig_cwd)


if __name__ == "__main__":
    unittest.main()
