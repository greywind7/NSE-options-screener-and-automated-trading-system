#!/usr/bin/env python3
"""
infrastructure/market_data/subscriber.py
========================================
Local IPC Subscriber client.
Consumes normalized events from either an in-memory/multiprocessing Queue
or a Unix domain / TCP socket connection.

Features:
- Stream generator yielding normalized MarketTick and OptionDepth events.
- Automatic sequence gap tracking.
- Lag tracking: measures receipt-to-processing latency.
- Resilient auto-reconnect with backoff.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import time
from multiprocessing.connection import Client
from typing import Any, Callable, Dict, Iterator, Optional, Union

from infrastructure.market_data.publisher import DEFAULT_AUTHKEY, DEFAULT_FEED_FILE, DEFAULT_IPC_PATH


class MarketDataSubscriber:
    """
    Consumer client for receiving normalized market events.
    Supports direct Queue subscription, Unix/TCP domain Socket connection,
    or transparent live feed-file tailing when socket permissions are restricted.
    """

    def __init__(
        self,
        address: Optional[str] = DEFAULT_IPC_PATH,
        authkey: bytes = DEFAULT_AUTHKEY,
        input_queue: Optional[Any] = None,
        feed_file_path: Optional[str] = DEFAULT_FEED_FILE,
        name: str = "Consumer",
        auto_reconnect: bool = True,
    ):
        self.address = address
        self.authkey = authkey
        self.input_queue = input_queue
        self.feed_file_path = feed_file_path
        self.name = name
        self.auto_reconnect = auto_reconnect
        self._conn: Optional[Any] = None
        self._file_stream: Optional[Any] = None
        self._running = False
        self._last_sequence = 0
        self.gaps_detected = 0
        self.total_received = 0
        self.total_lag_ms = 0.0

    def connect(self, timeout_sec: float = 5.0, retry_interval: float = 0.5) -> bool:
        """Connect to either input queue, socket publisher, or live feed file."""
        if self.input_queue is not None:
            self._running = True
            return True

        start_t = time.time()
        family = "AF_UNIX" if (isinstance(self.address, str) and not ":" in self.address and not isinstance(self.address, tuple)) else "AF_INET"

        while time.time() - start_t < timeout_sec:
            # 1. Try socket connection
            if self.address:
                try:
                    self._conn = Client(self.address, family=family, authkey=self.authkey)
                    self._running = True
                    return True
                except Exception:
                    pass

            # 2. Try live feed file fallback
            if self.feed_file_path and os.path.exists(self.feed_file_path):
                try:
                    mtime = os.path.getmtime(self.feed_file_path)
                    # Consider active if modified in the last 30 seconds
                    if time.time() - mtime < 30.0:
                        self._file_stream = open(self.feed_file_path, "r", encoding="utf-8")
                        self._file_stream.seek(0, os.SEEK_END)
                        self._running = True
                        return True
                except Exception:
                    pass

            time.sleep(retry_interval)

        return False

    def stream_events(self, timeout: float = 0.5, yield_heartbeat: bool = False) -> Iterator[Dict[str, Any]]:
        """
        Generator yielding incoming events.
        If yield_heartbeat=True, yields a HEARTBEAT event when timeout elapses with no incoming tick,
        allowing consumers (like live strategy dashboards) to keep timer loops ticking during idle periods.
        """
        self._running = True
        while self._running:
            # 1. Direct Queue reception
            if self.input_queue is not None:
                try:
                    event = self.input_queue.get(timeout=timeout)
                    self._process_event(event)
                    yield event
                except queue.Empty:
                    if yield_heartbeat:
                        yield {"event_type": "HEARTBEAT", "timestamp": time.time()}
                    continue
                except Exception:
                    continue

            # 2. Socket connection reception
            elif self._conn is not None:
                try:
                    if self._conn.poll(timeout):
                        event = self._conn.recv()
                        self._process_event(event)
                        yield event
                    else:
                        if yield_heartbeat:
                            yield {"event_type": "HEARTBEAT", "timestamp": time.time()}
                except (EOFError, BrokenPipeError, ConnectionResetError):
                    self._conn = None
                    if not self.auto_reconnect or not self._running:
                        break
                    time.sleep(0.5)
                except Exception:
                    self._conn = None
                    if not self.auto_reconnect or not self._running:
                        break
                    time.sleep(0.5)

            # 3. Live feed file stream reception
            elif self._file_stream is not None:
                line = self._file_stream.readline()
                if line and line.strip():
                    try:
                        event = json.loads(line.strip())
                        self._process_event(event)
                        yield event
                    except Exception:
                        pass
                else:
                    time.sleep(0.05)
                    if yield_heartbeat:
                        yield {"event_type": "HEARTBEAT", "timestamp": time.time()}
            else:
                if self.auto_reconnect and (self.address or self.feed_file_path):
                    if not self.connect(timeout_sec=1.0):
                        time.sleep(0.5)
                        continue
                else:
                    break

    def _process_event(self, event: Dict[str, Any]) -> None:
        """Track sequence numbers, gap detection, and processing latency."""
        if not event or event.get("event_type") == "HEARTBEAT":
            return
        self.total_received += 1
        seq = event.get("sequence_number", 0)
        if self._last_sequence > 0 and seq > self._last_sequence + 1:
            gap_size = seq - self._last_sequence - 1
            self.gaps_detected += gap_size
        self._last_sequence = seq

        rcv_ts = event.get("received_timestamp") or event.get("timestamp") or 0.0
        if rcv_ts > 0:
            lag = (time.time() - rcv_ts) * 1000.0
            self.total_lag_ms += max(0.0, lag)

    def close(self) -> None:
        """Close subscriber connection and open streams."""
        self._running = False
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._file_stream:
            try:
                self._file_stream.close()
            except Exception:
                pass
            self._file_stream = None


if __name__ == "__main__":
    sub = MarketDataSubscriber(name="CLI_Test_Sub")
    print("Connecting to Market Data Publisher...")
    if not sub.connect(timeout_sec=3.0):
        print("Could not connect to publisher.")
        sys.exit(1)

    print("Connected. Listening for events...")
    try:
        for ev in sub.stream_events():
            print(f"[{ev.get('sequence_number')}] {ev.get('event_type')} {ev.get('symbol')} LTP={ev.get('ltp')}")
    except KeyboardInterrupt:
        sub.close()
        print("\nSubscriber disconnected.")
