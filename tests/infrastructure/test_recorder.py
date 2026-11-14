#!/usr/bin/env python3
"""
tests/infrastructure/test_recorder.py
=====================================
Unit tests for Independent Market Data Recorder:
- Buffered JSONL event writes
- Split market_events and option_depth persistence
- Metadata header and footer integrity
"""

import json
import os
import queue
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.market_data.event_schema import MarketTick, OptionDepth
from infrastructure.market_data.subscriber import MarketDataSubscriber
from infrastructure.recorder.recorder import MarketRecorder
from infrastructure.recorder.writer import SessionWriter


TEST_RECORD_DIR = "/tmp/test_recordings"


class TestMarketRecorder(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_RECORD_DIR):
            shutil.rmtree(TEST_RECORD_DIR)
        os.makedirs(TEST_RECORD_DIR, exist_ok=True)

    def tearDown(self):
        if os.path.exists(TEST_RECORD_DIR):
            shutil.rmtree(TEST_RECORD_DIR)

    def test_01_session_writer_buffering_and_flush(self):
        """Verify session writer writes ticks and depth to separate files and closes cleanly."""
        writer = SessionWriter(
            record_dir=TEST_RECORD_DIR,
            session_date="20261106",
            split_depth=True,
            flush_interval_sec=10.0,
            buffer_capacity=100,
        )

        writer.write_header({"underlyings": 213, "options": 2967})

        tick1 = MarketTick(instrument_key="NSE_EQ|INE002A01018", symbol="RELIANCE", ltp=2500.0, volume=50000).to_dict()
        tick2 = MarketTick(instrument_key="NSE_EQ|INE009A01021", symbol="INFY", ltp=1800.0, volume=30000).to_dict()
        depth1 = OptionDepth(instrument_key="NSE_FO|66509", symbol="TCS26NOV3500CE", bids=[[45.0, 150, 2]], asks=[[45.5, 300, 3]]).to_dict()

        writer.write_tick(tick1)
        writer.write_tick(tick2)
        writer.write_depth(depth1)

        # Force flush
        writer.flush()
        writer.close()

        # Verify files exist
        events_path = os.path.join(TEST_RECORD_DIR, "20261106", "market_events.jsonl")
        depth_path = os.path.join(TEST_RECORD_DIR, "20261106", "option_depth.jsonl")
        meta_path = os.path.join(TEST_RECORD_DIR, "20261106", "metadata.json")

        self.assertTrue(os.path.exists(events_path), "market_events.jsonl missing")
        self.assertTrue(os.path.exists(depth_path), "option_depth.jsonl missing")
        self.assertTrue(os.path.exists(meta_path), "metadata.json missing")

        # Read events file
        with open(events_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 3)  # SESSION_START + 2 ticks
        self.assertEqual(lines[0]["event_type"], "SESSION_START")
        self.assertEqual(lines[1]["symbol"], "RELIANCE")
        self.assertEqual(lines[2]["symbol"], "INFY")

        # Read depth file
        with open(depth_path, "r", encoding="utf-8") as f:
            depth_lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(depth_lines), 1)
        self.assertEqual(depth_lines[0]["symbol"], "TCS26NOV3500CE")
        self.assertEqual(depth_lines[0]["bids"][0][0], 45.0)

        # Read metadata
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        self.assertEqual(meta["stats"]["total_ticks"], 2)
        self.assertEqual(meta["stats"]["total_depth_updates"], 1)
        self.assertEqual(meta["stats"]["gaps_detected"], 0)

    def test_02_recorder_queue_stream(self):
        """Verify MarketRecorder consumes from subscriber queue in background thread."""
        q = queue.Queue()
        sub = MarketDataSubscriber(input_queue=q, name="TestRecorderSub")
        sub.connect()

        recorder = MarketRecorder(
            record_dir=TEST_RECORD_DIR,
            session_date="20261106",
            subscriber=sub,
            manifest_summary={"test": True},
        )
        recorder.start()

        # Send 3 events via queue
        q.put(MarketTick(instrument_key="K1", symbol="S1", ltp=10.0, volume=100).to_dict())
        q.put(MarketTick(instrument_key="K2", symbol="S2", ltp=20.0, volume=200).to_dict())
        q.put(OptionDepth(instrument_key="K3", symbol="OPT3", bids=[[1.0, 10, 1]], asks=[[1.2, 20, 1]]).to_dict())

        time.sleep(0.15)
        recorder.stop()

        meta_path = os.path.join(TEST_RECORD_DIR, "20261106", "metadata.json")
        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        self.assertEqual(meta["stats"]["total_ticks"], 2)
        self.assertEqual(meta["stats"]["total_depth_updates"], 1)


if __name__ == "__main__":
    unittest.main()

