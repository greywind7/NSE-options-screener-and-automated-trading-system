#!/usr/bin/env python3
"""
tests/replay/test_archive_replay.py
===================================
Unit tests for post-market session archiving and transparent .tar.gz replay.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from replay.replay_engine import ReplayEngine
from runner import cmd_post_market


class TestArchiveAndReplay(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.record_dir = os.path.join(self.test_dir, "recordings")
        os.makedirs(self.record_dir, exist_ok=True)
        self.session_date = "20261106"
        self.session_folder = os.path.join(self.record_dir, self.session_date)
        os.makedirs(self.session_folder, exist_ok=True)

        # Create dummy market_events.jsonl
        self.events_file = os.path.join(self.session_folder, "market_events.jsonl")
        self.sample_ticks = [
            {"event_type": "TICK", "instrument_key": "NSE_EQ|INE002A01018", "symbol": "RELIANCE", "ltp": 2500.0, "volume": 1000},
            {"event_type": "TICK", "instrument_key": "NSE_EQ|INE002A01018", "symbol": "RELIANCE", "ltp": 2505.0, "volume": 1200},
            {"event_type": "TICK", "instrument_key": "NSE_EQ|INE040A01034", "symbol": "HDFCBANK", "ltp": 1600.0, "volume": 500},
        ]
        with open(self.events_file, "w", encoding="utf-8") as f:
            for t in self.sample_ticks:
                f.write(json.dumps(t) + "\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_post_market_archive_and_tar_gz_replay(self):
        """Test post-market compression to .tar.gz and streaming replay from archive."""
        class Args:
            date = self.session_date
            record_dir = self.record_dir
            keep_raw = False
            all = False

        # 1. Run post-market archive
        code = cmd_post_market(Args())
        self.assertEqual(code, 0)

        # 2. Verify archive created and raw folder removed
        expected_archive = os.path.join(self.record_dir, f"{self.session_date}.tar.gz")
        self.assertTrue(os.path.exists(expected_archive))
        self.assertGreater(os.path.getsize(expected_archive), 0)
        self.assertFalse(os.path.exists(self.session_folder))  # Raw folder cleaned up

        # 3. Verify ReplayEngine finds archive source
        ipc_path = os.path.join(self.test_dir, "test_ipc.sock")
        engine = ReplayEngine(
            session_date=self.session_date,
            record_dir=self.record_dir,
            ipc_path=ipc_path,
            speed=0.0,
        )
        src_info = engine.find_event_source()
        self.assertIsNotNone(src_info)
        src_path, src_type = src_info
        self.assertEqual(src_type, "archive")
        self.assertEqual(src_path, expected_archive)

        # 4. Stream lines from archive without extracting to disk
        lines = list(engine.stream_lines(src_path, src_type))
        self.assertEqual(len(lines), 3)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[0]["symbol"], "RELIANCE")
        self.assertEqual(parsed[1]["ltp"], 2505.0)
        self.assertEqual(parsed[2]["symbol"], "HDFCBANK")

    def test_post_market_archive_preserves_raw_by_default(self):
        """Test post-market compression preserves uncompressed files when keep_raw is default True."""
        class DefaultArgs:
            date = self.session_date
            record_dir = self.record_dir
            keep_raw = None
            delete_raw = False
            all = False

        code = cmd_post_market(DefaultArgs())
        self.assertEqual(code, 0)

        expected_archive = os.path.join(self.record_dir, f"{self.session_date}.tar.gz")
        self.assertTrue(os.path.exists(expected_archive))
        # Raw folder must remain intact!
        self.assertTrue(os.path.exists(self.session_folder))
        self.assertTrue(os.path.exists(self.events_file))


if __name__ == "__main__":
    unittest.main()
