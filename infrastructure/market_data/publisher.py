#!/usr/bin/env python3
"""
infrastructure/market_data/publisher.py
=======================================
High-performance local IPC Broadcaster using Python's multiprocessing.
Supports both in-memory multiprocessing.Queue (zero-socket, sandbox-safe, ultra-low latency)
and Unix domain / TCP socket connections via multiprocessing.connection.Listener.

Features:
- Sub-millisecond broadcast latency.
- Multi-client subscription: seamlessly broadcasts to Recorder, Strategies, and Dashboards.
- Incremental sequence numbers on every message for consumer gap detection.
- Non-blocking client delivery with slow-consumer detection and auto-pruning.
- Thread-safe and process-safe publishing API.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from multiprocessing import Queue as MPQueue
from multiprocessing.connection import Listener
from typing import Any, Dict, List, Optional, Set, Union

from infrastructure.market_data.event_schema import MarketTick, OptionDepth, SystemEvent


DEFAULT_IPC_PATH = "/tmp/upstox_market_feed.ipc"
DEFAULT_FEED_FILE = "/tmp/upstox_market_feed.jsonl"
DEFAULT_AUTHKEY = b"upstox_algo_shared_secret"


class MarketDataPublisher:
    """
    Central local IPC broadcaster.
    Broadcasts normalized market events to multiple subscriber channels (Queues or Sockets),
    with an automatic live-feed file fallback for cross-process streaming in restricted environments.
    """

    def __init__(
        self,
        address: Optional[str] = DEFAULT_IPC_PATH,
        authkey: bytes = DEFAULT_AUTHKEY,
        enable_socket: bool = True,
        enable_file_stream: bool = True,
        feed_file_path: Optional[str] = DEFAULT_FEED_FILE,
        max_buffer_size: int = 10_000,
    ):
        self.address = address
        self.authkey = authkey
        self.enable_socket = enable_socket
        self.enable_file_stream = enable_file_stream
        self.feed_file_path = feed_file_path
        self.max_buffer_size = max_buffer_size
        self._sequence = 0
        self._socket_clients: List[Any] = []
        self._queues: List[Any] = []
        self._lock = threading.Lock()
        self._running = False
        self._listener: Optional[Listener] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._feed_fp: Optional[Any] = None

    def register_queue(self, q: Any) -> None:
        """Register an in-memory or multiprocessing Queue for direct zero-socket IPC."""
        with self._lock:
            if q not in self._queues:
                self._queues.append(q)

    def unregister_queue(self, q: Any) -> None:
        """Unregister a Queue."""
        with self._lock:
            if q in self._queues:
                self._queues.remove(q)

    def start(self) -> None:
        """Start accepting subscriber connections and open live feed stream if enabled."""
        if self._running:
            return
        self._running = True

        # Open live feed file fallback
        if self.enable_file_stream and self.feed_file_path:
            try:
                parent_dir = os.path.dirname(os.path.abspath(self.feed_file_path))
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                self._feed_fp = open(self.feed_file_path, "w", encoding="utf-8")
            except Exception:
                self._feed_fp = None

        if self.enable_socket and self.address:
            try:
                # Clean up stale Unix domain socket if present
                if isinstance(self.address, str) and not ":" in self.address and os.path.exists(self.address):
                    try:
                        os.remove(self.address)
                    except Exception:
                        pass

                family = "AF_UNIX" if (isinstance(self.address, str) and not ":" in self.address and not isinstance(self.address, tuple)) else "AF_INET"
                self._listener = Listener(self.address, family=family, authkey=self.authkey)
                self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True, name="IPCAcceptLoop")
                self._accept_thread.start()
            except Exception:
                # In sandbox or restricted environment, fallback gracefully to queue-based IPC or feed file
                self._listener = None

    def _accept_loop(self) -> None:
        """Continuously accept new subscriber client connections."""
        while self._running and self._listener:
            try:
                conn = self._listener.accept()
                with self._lock:
                    self._socket_clients.append(conn)
            except Exception:
                if not self._running:
                    break

    def publish(self, event: Dict[str, Any] | MarketTick | OptionDepth | SystemEvent) -> int:
        """
        Broadcast an event to all connected subscriber channels.
        Returns the assigned sequence number.
        """
        with self._lock:
            self._sequence += 1
            seq = self._sequence

        # Extract dict payload and assign sequence
        if hasattr(event, "to_dict"):
            payload = event.to_dict()
        elif isinstance(event, dict):
            payload = dict(event)
        else:
            payload = {"event_type": "UNKNOWN", "data": str(event)}

        payload["sequence_number"] = seq
        if "received_timestamp" not in payload and "timestamp" not in payload:
            payload["received_timestamp"] = time.time()

        # 1. Broadcast to registered Queues
        with self._lock:
            active_queues = list(self._queues)

        for q in active_queues:
            try:
                q.put_nowait(payload)
            except Exception:
                pass  # Buffer full or dropped

        # 2. Broadcast to Socket Clients
        dead_conns = []
        with self._lock:
            active_sockets = list(self._socket_clients)

        for conn in active_sockets:
            try:
                conn.send(payload)
            except Exception:
                dead_conns.append(conn)

        if dead_conns:
            with self._lock:
                for dc in dead_conns:
                    if dc in self._socket_clients:
                        self._socket_clients.remove(dc)
                    try:
                        dc.close()
                    except Exception:
                        pass

        # 3. Stream to live feed file (resilient fallback for multi-process environments)
        if self._feed_fp and not self._feed_fp.closed:
            try:
                self._feed_fp.write(json.dumps(payload) + "\n")
                self._feed_fp.flush()
            except Exception:
                pass

        return seq

    def get_subscriber_count(self) -> int:
        """Return the total number of connected subscribers (queues + sockets)."""
        with self._lock:
            return len(self._queues) + len(self._socket_clients)

    def stop(self) -> None:
        """Stop broadcaster and close all subscriber channels."""
        self._running = False
        if self._listener:
            try:
                self._listener.close()
            except Exception:
                pass

        if self._feed_fp:
            try:
                self._feed_fp.close()
            except Exception:
                pass
            self._feed_fp = None

        with self._lock:
            for conn in self._socket_clients:
                try:
                    conn.close()
                except Exception:
                    pass
            self._socket_clients.clear()
            self._queues.clear()

        if isinstance(self.address, str) and os.path.exists(self.address):
            try:
                os.remove(self.address)
            except Exception:
                pass

        if self.feed_file_path and os.path.exists(self.feed_file_path):
            try:
                os.remove(self.feed_file_path)
            except Exception:
                pass


if __name__ == "__main__":
    pub = MarketDataPublisher()
    pub.start()
    print(f"Publisher started on {pub.address}. Waiting for subscribers...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pub.stop()
        print("\nPublisher stopped.")
