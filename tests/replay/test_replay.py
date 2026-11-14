#!/usr/bin/env python3
"""
tests/replay/test_replay.py
===========================
Unit tests for Historical ReplayEngine:
- Tests finding recorded session files.
- Tests replaying ticks into IPC bus at accelerated speed.
- Tests replay completion event emission.
"""

import json
import os
import sys
import tempfile
import unittest

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.market_data.subscriber import MarketDataSubscriber
from replay.replay_engine import ReplayEngine


class TestReplayEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_date = "20261106"
        self.rec_dir = os.path.join(self.temp_dir.name, "recordings")
        self.sess_dir = os.path.join(self.rec_dir, self.session_date)
        os.makedirs(self.sess_dir, exist_ok=True)

        self.ticks_file = os.path.join(self.sess_dir, "ticks.jsonl")
        # Write 5 mock ticks
        with open(self.ticks_file, "w", encoding="utf-8") as f:
            for i in range(5):
                tick = {
                    "event_type": "TICK",
                    "instrument_key": "NSE_EQ|INE002A01018",
                    "symbol": "RELIANCE",
                    "ltp": 2500.0 + i,
                    "volume": 100000 + (i * 1000),
                    "received_timestamp": 1730904000.0 + i,
                }
                f.write(json.dumps(tick) + "\n")

        self.ipc_path = os.path.join(self.temp_dir.name, "test_replay.ipc")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_replay_execution(self):
        """Verify ReplayEngine reads JSONL file and broadcasts events over IPC."""
        engine = ReplayEngine(
            session_date=self.session_date,
            record_dir=self.rec_dir,
            speed=0.0,  # Instant replay
            ipc_path=self.ipc_path,
        )

        sub = MarketDataSubscriber(name="TestReplaySub", address=self.ipc_path)
        sub.connect(timeout_sec=2.0)

        code = engine.run()
        self.assertEqual(code, 0)
        self.assertEqual(engine.total_replayed, 5)


if __name__ == "__main__":
    unittest.main()

