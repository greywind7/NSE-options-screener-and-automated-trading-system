#!/usr/bin/env python3
"""
tests/simulation/test_simulation_engine.py
==========================================
Unit tests for SimulationEngine date resolution, parameter parsing,
and graceful handling of missing recording sessions.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from simulation.simulation_engine import SimulationEngine, SimulationDummyAdapter


class TestSimulationEngine(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.record_dir = os.path.join(self.test_dir, "recordings")
        self.log_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(self.record_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_date_resolution_single_and_range(self):
        # 1. Single date
        e1 = SimulationEngine(dates=["20261109"], record_dir=self.record_dir, log_dir=self.log_dir)
        self.assertEqual(e1.dates, ["20261109"])

        # 2. Date range
        e2 = SimulationEngine(from_date="20261106", to_date="20261109", record_dir=self.record_dir, log_dir=self.log_dir)
        self.assertEqual(e2.dates, ["20261106", "20261107", "20261108", "20261109"])

        # 3. Default date is today
        e3 = SimulationEngine(record_dir=self.record_dir, log_dir=self.log_dir)
        self.assertEqual(len(e3.dates), 1)
        self.assertEqual(e3.dates[0], datetime.now().strftime("%Y%m%d"))

    def test_time_bounds_resolution(self):
        engine = SimulationEngine(
            dates=["20261109"],
            start_time="09:15",
            end_time="15:15",
            record_dir=self.record_dir,
            log_dir=self.log_dir,
        )
        start_ts, end_ts = engine._resolve_time_bounds("20261109")
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_start = datetime.fromtimestamp(start_ts, tz=ist)
        dt_end = datetime.fromtimestamp(end_ts, tz=ist)

        self.assertEqual(dt_start.strftime("%H:%M"), "09:15")
        self.assertEqual(dt_end.strftime("%H:%M"), "15:15")
        self.assertGreater(end_ts, start_ts)

    def test_missing_recording_handled_gracefully(self):
        engine = SimulationEngine(
            dates=["20261199"],  # Non-existent date
            record_dir=self.record_dir,
            log_dir=self.log_dir,
        )
        res = engine.run_session("20261199")
        self.assertEqual(res.get("status"), "NO_RECORDING")
        self.assertEqual(res.get("closed_trades"), [])

    def test_simulation_dummy_adapter(self):
        adapter = SimulationDummyAdapter("paper")
        res = adapter.place_order(symbol="TCS", quantity=10, transaction_type="BUY")
        self.assertEqual(res.get("status"), "FILLED")
        self.assertTrue(res.get("simulated"))
        self.assertTrue(adapter.cancel_order("ord1"))
        self.assertTrue(adapter.update_stop_loss("TCS", "tok1", 100.0))
        self.assertEqual(adapter.get_positions(), [])


if __name__ == "__main__":
    unittest.main()

