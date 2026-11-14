#!/usr/bin/env python3
"""
replay/replay_engine.py
=======================
Historical Market Session Replay Subsystem:
Reads recorded market ticks and depth events from recordings/YYYYMMDD/
or compressed recordings/YYYYMMDD.tar.gz archives, and broadcasts them
over local IPC bus with configurable speed multipliers.

Allows strategies to execute against historical tick data with zero code modification.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from infrastructure.market_data.event_schema import MarketTick, OptionDepth, SystemEvent
from infrastructure.market_data.publisher import MarketDataPublisher


class ReplayEngine:
    """
    Deterministic historical replay engine.
    Streams recorded JSONL events over local IPC transport, supporting
    both uncompressed folders and .tar.gz compressed session archives.
    """

    def __init__(
        self,
        session_date: str,
        strategy_name: str = "momentum",
        record_dir: str = "recordings",
        speed: float = 1.0,
        ipc_path: str = "/tmp/upstox_market_feed.ipc",
    ):
        self.session_date = session_date
        self.strategy_name = strategy_name
        self.record_dir = record_dir
        self.speed = max(0.0, float(speed))
        self.ipc_path = ipc_path

        self.session_dir = os.path.join(self.record_dir, self.session_date)
        self.ticks_file = os.path.join(self.session_dir, "ticks.jsonl")
        self.events_file = os.path.join(self.session_dir, "market_events.jsonl")
        self.archive_file = os.path.join(self.record_dir, f"{self.session_date}.tar.gz")
        self.legacy_jsonl = os.path.join(self.record_dir, f"session_{self.session_date}_market.jsonl")
        self.legacy_archive = os.path.join(self.record_dir, f"session_{self.session_date}_market.tar.gz")

        self.publisher = MarketDataPublisher(address=self.ipc_path)
        self.total_replayed = 0

    def find_event_source(self) -> Optional[Tuple[str, str]]:
        """
        Locates the event source for this session date.
        Returns (path, 'file') or (path, 'archive') or None.
        """
        # 1. Uncompressed folder files
        if os.path.exists(self.ticks_file) and os.path.getsize(self.ticks_file) > 0:
            return (self.ticks_file, "file")
        if os.path.exists(self.events_file) and os.path.getsize(self.events_file) > 0:
            return (self.events_file, "file")

        # 2. Compressed tar.gz session archive
        if os.path.exists(self.archive_file) and os.path.getsize(self.archive_file) > 0:
            return (self.archive_file, "archive")

        # 3. Legacy file formats
        if os.path.exists(self.legacy_jsonl) and os.path.getsize(self.legacy_jsonl) > 0:
            return (self.legacy_jsonl, "file")
        if os.path.exists(self.legacy_archive) and os.path.getsize(self.legacy_archive) > 0:
            return (self.legacy_archive, "archive")

        return None

    def stream_lines(self, src: str, src_type: str) -> Iterator[str]:
        """
        Yields raw text lines either from a plain text file or directly
        decompressed on-the-fly from a .tar.gz archive without disk extraction.
        """
        if src_type == "file":
            with open(src, "r", encoding="utf-8") as f:
                for line in f:
                    yield line
        elif src_type == "archive":
            with tarfile.open(src, "r:gz") as tar:
                # Find the target event stream member inside tar
                target_member = None
                members = tar.getmembers()
                for m in members:
                    if m.name.endswith("market_events.jsonl") or m.name.endswith("ticks.jsonl"):
                        target_member = m
                        break
                if not target_member:
                    jsonl_members = [m for m in members if m.name.endswith(".jsonl")]
                    if jsonl_members:
                        target_member = max(jsonl_members, key=lambda m: m.size)

                if target_member:
                    f = tar.extractfile(target_member)
                    if f:
                        text_f = io.TextIOWrapper(f, encoding="utf-8")
                        for line in text_f:
                            yield line

    def run(self) -> int:
        source_info = self.find_event_source()
        if not source_info:
            print(f"❌ No recording found for date '{self.session_date}' in '{self.record_dir}'.", file=sys.stderr)
            print(f"   Checked: {self.events_file}, {self.archive_file}, {self.legacy_jsonl}", file=sys.stderr)
            return 1

        src, src_type = source_info
        size_kb = os.path.getsize(src) / 1024
        print(f"✓ Replay Source : {src} [{src_type.upper()}] ({size_kb:,.1f} KB)")
        print(f"✓ Target IPC Bus: {self.ipc_path}")
        print(f"✓ Replay Speed  : {self.speed}x {'(instant)' if self.speed == 0 else ''}")
        print("  Starting playback...\n")

        self.publisher.publish(SystemEvent(
            code="REPLAY_STARTED",
            message=f"Replaying session {self.session_date} from {src_type} at {self.speed}x",
        ))

        last_event_ts: Optional[float] = None
        last_wall_ts: Optional[float] = None

        for line in self.stream_lines(src, src_type):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                evt_type = data.get("event_type", "TICK")

                # Handle pacing
                evt_ts = float(data.get("received_timestamp") or data.get("timestamp") or time.time())
                if self.speed > 0 and last_event_ts is not None and last_wall_ts is not None:
                    time_delta = evt_ts - last_event_ts
                    if 0 < time_delta < 10.0:  # Valid time delta
                        sleep_duration = time_delta / self.speed
                        time.sleep(sleep_duration)

                last_event_ts = evt_ts
                last_wall_ts = time.time()

                if evt_type == "TICK":
                    tick = MarketTick(
                        instrument_key=data.get("instrument_key", ""),
                        symbol=data.get("symbol", ""),
                        ltp=float(data.get("ltp") or 0.0),
                        volume=int(data.get("volume") or 0),
                        close=float(data.get("close") or 0.0),
                        high=float(data.get("high") or 0.0),
                        low=float(data.get("low") or 0.0),
                        bid=float(data.get("bid") or 0.0),
                        ask=float(data.get("ask") or 0.0),
                        received_timestamp=time.time(),
                    )
                    self.publisher.publish(tick)
                elif evt_type == "DEPTH":
                    depth = OptionDepth(
                        instrument_key=data.get("instrument_key", ""),
                        symbol=data.get("symbol", ""),
                        bids=data.get("bids", []),
                        asks=data.get("asks", []),
                        received_timestamp=time.time(),
                    )
                    self.publisher.publish(depth)
                else:
                    self.publisher.publish_dict(data)

                self.total_replayed += 1

            except Exception:
                pass

        self.publisher.publish(SystemEvent(
            code="REPLAY_COMPLETED",
            message=f"Session {self.session_date} replayed ({self.total_replayed:,} events)",
        ))

        print(f"\n✅ Replay Complete: Published {self.total_replayed:,} events over {self.ipc_path}.")
        return 0
