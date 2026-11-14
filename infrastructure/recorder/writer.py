#!/usr/bin/env python3
"""
infrastructure/recorder/writer.py
=================================
High-throughput, buffered, thread-safe session writer for raw market events.

Features:
- Buffered writes with periodic auto-flush (zero data loss on graceful exit).
- Supports split recording files (market_events.jsonl + option_depth.jsonl)
  or consolidated stream.
- Emits session metadata.json with token manifests and integrity stats.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from infrastructure.recorder.recording_schema import make_session_footer, make_session_header


class SessionWriter:
    """
    Manages physical file persistence for a recording session.
    """

    def __init__(
        self,
        record_dir: str = "recordings",
        session_date: Optional[str] = None,
        split_depth: bool = True,
        flush_interval_sec: float = 2.0,
        buffer_capacity: int = 5000,
    ):
        self.record_dir = record_dir
        if not session_date:
            session_date = datetime.now().strftime("%Y%m%d")
        self.session_date = session_date
        self.split_depth = split_depth
        self.flush_interval_sec = flush_interval_sec
        self.buffer_capacity = buffer_capacity

        # Directory: recordings/YYYYMMDD/
        self.session_path = os.path.join(self.record_dir, self.session_date)
        os.makedirs(self.session_path, exist_ok=True)

        self.events_file_path = os.path.join(self.session_path, "market_events.jsonl")
        self.depth_file_path = os.path.join(self.session_path, "option_depth.jsonl") if split_depth else self.events_file_path
        self.metadata_file_path = os.path.join(self.session_path, "metadata.json")

        # Create backward-compatible convenience symlinks for legacy consumers
        for src_name, target_name in [("market_events.jsonl", "ticks.jsonl"), ("option_depth.jsonl", "options_depth.jsonl")]:
            link_path = os.path.join(self.session_path, target_name)
            if not os.path.exists(link_path):
                try:
                    os.symlink(src_name, link_path)
                except Exception:
                    pass

        self._events_file = open(self.events_file_path, "a", encoding="utf-8")
        self._depth_file = open(self.depth_file_path, "a", encoding="utf-8") if split_depth else self._events_file

        self._buffer: List[Tuple[str, str]] = []  # [(target_file_type, line_json), ...]
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.total_ticks_written = 0
        self.total_depth_written = 0
        self.gaps_detected = 0

    def write_header(self, manifest_summary: Dict[str, Any]) -> None:
        """Write session initialization header."""
        header = make_session_header(self.session_date, manifest_summary)
        line = json.dumps(header) + "\n"
        with self._lock:
            self._events_file.write(line)
            self._events_file.flush()

    def write_tick(self, tick_dict: Dict[str, Any]) -> None:
        """Buffer raw tick event for writing."""
        line = json.dumps(tick_dict) + "\n"
        with self._lock:
            self._buffer.append(("tick", line))
            self.total_ticks_written += 1
            if len(self._buffer) >= self.buffer_capacity or (time.time() - self._last_flush >= self.flush_interval_sec):
                self._flush_internal()

    def write_depth(self, depth_dict: Dict[str, Any]) -> None:
        """Buffer option depth snapshot for writing."""
        line = json.dumps(depth_dict) + "\n"
        with self._lock:
            self._buffer.append(("depth", line))
            self.total_depth_written += 1
            if len(self._buffer) >= self.buffer_capacity or (time.time() - self._last_flush >= self.flush_interval_sec):
                self._flush_internal()

    def flush(self) -> None:
        """Public thread-safe flush."""
        with self._lock:
            self._flush_internal()

    def _flush_internal(self) -> None:
        """Internal flush (must be called under self._lock)."""
        if not self._buffer:
            return

        for target, line in self._buffer:
            if target == "depth" and self.split_depth:
                self._depth_file.write(line)
            else:
                self._events_file.write(line)

        self._events_file.flush()
        if self.split_depth and self._depth_file != self._events_file:
            self._depth_file.flush()

        self._buffer.clear()
        self._last_flush = time.time()

    def close(self) -> None:
        """Flush remaining buffers, write footer, and close files."""
        self.flush()

        # Write session metadata.json
        footer = make_session_footer(
            session_date=self.session_date,
            total_ticks=self.total_ticks_written,
            total_depth_updates=self.total_depth_written,
            gaps_detected=self.gaps_detected,
        )
        try:
            with open(self.metadata_file_path, "w", encoding="utf-8") as f:
                json.dump(footer, f, indent=2)
        except Exception:
            pass

        with self._lock:
            if self._events_file and not self._events_file.closed:
                self._events_file.close()
            if self.split_depth and self._depth_file and not self._depth_file.closed:
                self._depth_file.close()

