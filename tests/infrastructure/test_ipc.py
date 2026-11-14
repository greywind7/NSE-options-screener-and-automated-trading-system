#!/usr/bin/env python3
"""
tests/infrastructure/test_ipc.py
================================
Unit tests for Local IPC Publisher & Subscriber Architecture:
- Concurrent subscriber broadcast
- Sequence numbering & gap tracking
- Clean connect / disconnect lifecycle
"""

import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.market_data.event_schema import MarketTick, SystemEvent
from infrastructure.market_data.publisher import MarketDataPublisher
from infrastructure.market_data.subscriber import MarketDataSubscriber


class TestIPCPubSub(unittest.TestCase):

    def setUp(self):
        self.pub = MarketDataPublisher(enable_socket=False)
        self.pub.start()

    def tearDown(self):
        self.pub.stop()

    def test_01_single_subscriber_reception(self):
        """Verify single subscriber connects and receives broadcast events."""
        q = queue.Queue()
        self.pub.register_queue(q)

        sub = MarketDataSubscriber(input_queue=q, name="Sub1", auto_reconnect=False)
        self.assertTrue(sub.connect(), "Failed to connect subscriber")

        received = []

        def worker():
            for ev in sub.stream_events(timeout=0.1):
                received.append(ev)
                if len(received) >= 3:
                    break

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        time.sleep(0.05)

        # Publish 3 ticks
        tick1 = MarketTick(instrument_key="NSE_EQ|INE002A01018", symbol="RELIANCE", ltp=2500.0, volume=100000)
        tick2 = MarketTick(instrument_key="NSE_EQ|INE009A01021", symbol="INFY", ltp=1800.0, volume=80000)
        sys_ev = SystemEvent(code="HEARTBEAT", message="Ping")

        self.pub.publish(tick1)
        self.pub.publish(tick2)
        self.pub.publish(sys_ev)

        th.join(timeout=2.0)
        sub.close()

        self.assertEqual(len(received), 3)
        self.assertEqual(received[0]["symbol"], "RELIANCE")
        self.assertEqual(received[0]["sequence_number"], 1)
        self.assertEqual(received[1]["symbol"], "INFY")
        self.assertEqual(received[1]["sequence_number"], 2)
        self.assertEqual(received[2]["code"], "HEARTBEAT")
        self.assertEqual(received[2]["sequence_number"], 3)
        self.assertEqual(sub.gaps_detected, 0)

    def test_02_multiple_concurrent_subscribers(self):
        """Verify multiple independent subscribers receive the same stream concurrently."""
        q1 = queue.Queue()
        q2 = queue.Queue()
        self.pub.register_queue(q1)
        self.pub.register_queue(q2)

        sub1 = MarketDataSubscriber(input_queue=q1, name="Recorder", auto_reconnect=False)
        sub2 = MarketDataSubscriber(input_queue=q2, name="MomentumStrategy", auto_reconnect=False)

        self.assertTrue(sub1.connect())
        self.assertTrue(sub2.connect())

        sub1_events = []
        sub2_events = []

        def worker(sub, out_list):
            for ev in sub.stream_events(timeout=0.1):
                out_list.append(ev)
                if len(out_list) >= 5:
                    break

        t1 = threading.Thread(target=worker, args=(sub1, sub1_events), daemon=True)
        t2 = threading.Thread(target=worker, args=(sub2, sub2_events), daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.05)

        for i in range(1, 6):
            self.pub.publish(MarketTick(instrument_key=f"TICK_{i}", symbol=f"SYM_{i}", ltp=100.0 * i, volume=1000))

        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        sub1.close()
        sub2.close()

        self.assertEqual(len(sub1_events), 5)
        self.assertEqual(len(sub2_events), 5)
        self.assertEqual([e["sequence_number"] for e in sub1_events], [1, 2, 3, 4, 5])
        self.assertEqual([e["sequence_number"] for e in sub2_events], [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
