#!/usr/bin/env python3
"""
infrastructure/recorder/recorder.py
===================================
Independent Market Data Recorder Service.

Subscribes to the central market data stream and captures 100% of incoming ticks
and option depth snapshots unconditionally (strategy-agnostic).

Usage:
    python3 -m infrastructure.recorder.recorder
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from infrastructure.market_data.subscriber import MarketDataSubscriber
from infrastructure.recorder.writer import SessionWriter


class MarketRecorder:
    """
    Dedicated background recording engine.
    """

    def __init__(
        self,
        record_dir: str = "recordings",
        session_date: Optional[str] = None,
        subscriber: Optional[MarketDataSubscriber] = None,
        manifest_summary: Optional[Dict[str, Any]] = None,
        split_depth: bool = True,
    ):
        self.record_dir = record_dir
        if not session_date:
            session_date = datetime.now().strftime("%Y%m%d")
        self.session_date = session_date
        self.subscriber = subscriber or MarketDataSubscriber(name="MarketRecorder")
        self.writer = SessionWriter(record_dir=record_dir, session_date=session_date, split_depth=split_depth)
        self.manifest_summary = manifest_summary or {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start recording in a background thread."""
        if self._running:
            return
        self._running = True

        self.writer.write_header(self.manifest_summary)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="MarketRecorderLoop")
        self._thread.start()

    def _run_loop(self) -> None:
        """Main event consumer loop."""
        if not self.subscriber.connect(timeout_sec=5.0):
            print(f"⚠️ [MarketRecorder] Failed to connect subscriber. Retrying in background...")

        for event in self.subscriber.stream_events():
            if not self._running:
                break

            etype = event.get("event_type", "TICK")
            if etype == "TICK":
                self.writer.write_tick(event)
            elif etype == "DEPTH":
                self.writer.write_depth(event)

            self.writer.gaps_detected = self.subscriber.gaps_detected

    def stop(self) -> None:
        """Stop recording and flush files cleanly."""
        self._running = False
        self.subscriber.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self.writer.close()


def main():
    parser = argparse.ArgumentParser(description="Independent Market Data Recorder")
    parser.add_argument("--record-dir", default="recordings", help="Output directory")
    parser.add_argument("--date", default=None, help="Session date YYYYMMDD")
    args = parser.parse_args()

    recorder = MarketRecorder(record_dir=args.record_dir, session_date=args.date)

    def sig_handler(sig, frame):
        print("\nStopping recorder gracefully...")
        recorder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    print(f"Starting Market Data Recorder for session '{recorder.session_date}'...")
    recorder.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        recorder.stop()


if __name__ == "__main__":
    main()

