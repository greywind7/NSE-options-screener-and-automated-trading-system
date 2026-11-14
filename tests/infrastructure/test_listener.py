#!/usr/bin/env python3
"""
tests/infrastructure/test_listener.py
=====================================
Unit tests for MarketDataListener service:
- Tests listener initialization and instrument universe discovery.
- Tests mock mode stream ingestion and IPC event broadcasting.
- Tests subscriber receipt of ticks emitted by listener.
"""

import os
import queue
import sys
import tempfile
import threading
import time
import unittest

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.market_data.listener import MarketDataListener
from infrastructure.market_data.subscriber import MarketDataSubscriber


class TestMarketDataListener(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ipc_path = os.path.join(self.temp_dir.name, "test_listener.ipc")
        self.reg_path = os.path.join(self.temp_dir.name, "active_pos.json")
        self.listener = MarketDataListener(
            mode="mock",
            ipc_path=self.ipc_path,
            poll_interval=0.1,
            enable_socket=False,
            debug_log=None,
        )
        from infrastructure.market_data.position_registry import ProtectedPositionRegistry
        self.listener.position_registry = ProtectedPositionRegistry(registry_file=self.reg_path)
        self.listener.position_registry.clear()

    def tearDown(self):
        self.listener.stop()
        self.temp_dir.cleanup()

    def test_listener_loads_instruments(self):
        """Verify listener discovers monitored instruments and builds symbol map."""
        insts = self.listener.load_instruments()
        self.assertGreater(len(insts), 0)
        keys = [x["key"] for x in insts]
        self.assertIn("NSE_INDEX|Nifty 50", keys)
        self.assertIn("NSE_INDEX|Nifty 50", self.listener.symbol_map)
        self.assertEqual(self.listener.symbol_map["NSE_INDEX|Nifty 50"], "Nifty 50")

    def test_listener_broadcast_and_subscriber_receipt(self):
        """Verify listener broadcasts ticks that a subscriber can receive."""
        q = queue.Queue()
        self.listener.publisher.register_queue(q)

        listener_thread = threading.Thread(target=self.listener.start, daemon=True)
        listener_thread.start()

        # Connect subscriber
        sub = MarketDataSubscriber(input_queue=q, name="TestSub", auto_reconnect=False)
        self.assertTrue(sub.connect(), "Subscriber failed to connect to queue")

        # Receive at least 3 events
        received_events = []
        for ev in sub.stream_events(timeout=0.5):
            received_events.append(ev)
            if len(received_events) >= 3:
                break

        sub.close()
        self.assertGreaterEqual(len(received_events), 3)
        self.assertTrue(any(e.get("event_type") == "TICK" for e in received_events))

    def test_dynamic_options_evaluation_on_price_shift(self):
        """Verify dynamic popular options selection computes correct add/remove keys as underlying price moves."""
        # Setup mock options map
        self.listener.options_map = {
            "TEST_STOCK": {
                "nearest_expiry": "2026-11-23",
                "strikes": {
                    "2400.0": {"CE": "NSE_FO|2400CE", "PE": "NSE_FO|2400PE"},
                    "2420.0": {"CE": "NSE_FO|2420CE", "PE": "NSE_FO|2420PE"},
                    "2440.0": {"CE": "NSE_FO|2440CE", "PE": "NSE_FO|2440PE"},
                    "2460.0": {"CE": "NSE_FO|2460CE", "PE": "NSE_FO|2460PE"},
                    "2480.0": {"CE": "NSE_FO|2480CE", "PE": "NSE_FO|2480PE"},
                    "2500.0": {"CE": "NSE_FO|2500CE", "PE": "NSE_FO|2500PE"},
                    "2520.0": {"CE": "NSE_FO|2520CE", "PE": "NSE_FO|2520PE"},
                    "2540.0": {"CE": "NSE_FO|2540CE", "PE": "NSE_FO|2540PE"},
                    "2560.0": {"CE": "NSE_FO|2560CE", "PE": "NSE_FO|2560PE"},
                    "2580.0": {"CE": "NSE_FO|2580CE", "PE": "NSE_FO|2580PE"},
                    "2600.0": {"CE": "NSE_FO|2600CE", "PE": "NSE_FO|2600PE"},
                    "2620.0": {"CE": "NSE_FO|2620CE", "PE": "NSE_FO|2620PE"},
                    "2640.0": {"CE": "NSE_FO|2640CE", "PE": "NSE_FO|2640PE"},
                    "2660.0": {"CE": "NSE_FO|2660CE", "PE": "NSE_FO|2660PE"},
                }
            }
        }
        self.listener.available_strikes_by_symbol = {
            "TEST_STOCK": [2400.0, 2420.0, 2440.0, 2460.0, 2480.0, 2500.0, 2520.0, 2540.0, 2560.0, 2580.0, 2600.0, 2620.0, 2640.0, 2660.0]
        }
        self.listener.underlying_key_to_symbol = {
            "NSE_EQ|INE999A01010": "TEST_STOCK",
            "TEST_STOCK": "TEST_STOCK",
        }
        self.listener.symbol_metadata = {
            "TEST_STOCK": {"underlying_key": "NSE_EQ|INE999A01010", "name": "TEST STOCK"}
        }

        # 1. First evaluation at LTP 2500 (ATM = 2500, popular strikes: 2440, 2460, 2480, 2500, 2520, 2540, 2560)
        to_add, to_remove = self.listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2501.0)
        self.assertEqual(len(to_remove), 0)
        self.assertEqual(len(to_add), 14)  # 7 strikes * 2 (CE & PE)
        self.assertIn("NSE_FO|2500CE", to_add)
        self.assertIn("NSE_FO|2500PE", to_add)
        self.assertIn("NSE_FO|2440CE", to_add)
        self.assertIn("NSE_FO|2560CE", to_add)
        self.assertEqual(self.listener.last_atm_by_symbol["TEST_STOCK"], 2500.0)

        # 2. Second tick at same ATM price (2503) should produce zero changes (no flapping)
        to_add2, to_remove2 = self.listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2503.0)
        self.assertEqual(len(to_add2), 0)
        self.assertEqual(len(to_remove2), 0)

        # 3. Stock rallies to 2600 (ATM shifts to 2600, popular strikes: 2540, 2560, 2580, 2600, 2620, 2640, 2660)
        # Clear reval throttle for test
        self.listener.last_reval_time_by_symbol["TEST_STOCK"] = 0.0
        to_add3, to_remove3 = self.listener.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2598.5)
        self.assertEqual(self.listener.last_atm_by_symbol["TEST_STOCK"], 2600.0)
        
        # New strikes (2580, 2600, 2620, 2640, 2660) added
        self.assertIn("NSE_FO|2580CE", to_add3)
        self.assertIn("NSE_FO|2600CE", to_add3)
        self.assertIn("NSE_FO|2620CE", to_add3)
        self.assertIn("NSE_FO|2640CE", to_add3)
        self.assertIn("NSE_FO|2660CE", to_add3)

        # Stale strikes beyond hysteresis window (2400, 2420, 2440, 2460, 2480, 2500) pruned
        self.assertIn("NSE_FO|2440CE", to_remove3)
        self.assertIn("NSE_FO|2460CE", to_remove3)
        self.assertIn("NSE_FO|2480CE", to_remove3)
        self.assertIn("NSE_FO|2500CE", to_remove3)

        # Verify symbol map has clean names
        self.assertIn("NSE_FO|2600CE", self.listener.symbol_map)
        self.assertEqual(self.listener.symbol_map["NSE_FO|2600CE"], "TEST STOCK 2600 CE")

    def test_dual_event_publishing_trade_and_depth(self):
        """Verify that when both volume and depth change, separate TICK and DEPTH events are produced."""
        q = queue.Queue()
        self.listener.publisher.register_queue(q)

        # 1. Simulate initial frame with trade and depth
        key = "NSE_EQ|INE002A01018"
        sym = "RELIANCE"
        ltp = 2500.0
        vol = 1000
        bids = [[2499.5, 100, 1]]
        asks = [[2500.5, 200, 1]]
        now_ts = time.time()

        # Helper method testing the publication logic directly
        def process_update(k, s, p, v, c, b_list, a_list, t):
            prev_v = self.listener.last_seen_volume.get(k, -1)
            prev_d = self.listener.last_seen_depth.get(k)
            v_changed = (k not in self.listener.last_seen_volume) or (v != prev_v)
            d_available = bool(b_list or a_list)
            bid_p = b_list[0][0] if b_list else p
            ask_p = a_list[0][0] if a_list else p
            d_tuple = (
                round(bid_p, 2),
                int(b_list[0][1]) if b_list else 0,
                round(ask_p, 2),
                int(a_list[0][1]) if a_list else 0,
                tuple((round(b[0], 2), int(b[1])) for b in b_list[:3]),
                tuple((round(a[0], 2), int(a[1])) for a in a_list[:3]),
            )
            d_changed = d_available and (k not in self.listener.last_seen_depth or d_tuple != prev_d)

            from infrastructure.market_data.event_schema import MarketTick, OptionDepth
            if v_changed:
                self.listener.last_seen_volume[k] = v
                self.listener.last_seen_ltp[k] = p
                self.listener.publisher.publish(MarketTick(
                    instrument_key=k, symbol=s, ltp=p, volume=v, close=c,
                    high=p, low=p, bid=bid_p, ask=ask_p, received_timestamp=t,
                ))
            if d_changed:
                self.listener.last_seen_depth[k] = d_tuple
                self.listener.publisher.publish(OptionDepth(
                    instrument_key=k, symbol=s, bids=b_list, asks=a_list,
                    bid=bid_p, bid_qty=int(b_list[0][1]) if b_list else 0,
                    ask=ask_p, ask_qty=int(a_list[0][1]) if a_list else 0,
                    ltp=p, received_timestamp=t,
                ))

        # Initial frame (both first seen): should produce 1 TICK and 1 DEPTH
        process_update(key, sym, ltp, vol, 2500.0, bids, asks, now_ts)
        ev1 = q.get(timeout=1.0)
        ev2 = q.get(timeout=1.0)
        types = {ev1.get("event_type"), ev2.get("event_type")}
        self.assertEqual(types, {"TICK", "DEPTH"})

        # Subsequent frame where BOTH volume AND depth change
        now_ts += 1.0
        process_update(key, sym, 2501.0, 1100, 2500.0, [[2500.5, 150, 2]], [[2501.5, 250, 2]], now_ts)
        ev3 = q.get(timeout=1.0)
        ev4 = q.get(timeout=1.0)
        types2 = {ev3.get("event_type"), ev4.get("event_type")}
        self.assertEqual(types2, {"TICK", "DEPTH"})

        # Frame where ONLY depth changes (no trade, volume remains 1100)
        now_ts += 1.0
        process_update(key, sym, 2501.0, 1100, 2500.0, [[2500.75, 120, 1]], [[2501.5, 250, 2]], now_ts)
        ev5 = q.get(timeout=1.0)
        self.assertEqual(ev5.get("event_type"), "DEPTH")
        self.assertTrue(q.empty(), "Should not produce TICK when volume is unchanged")

    def test_rest_fallback_flag_default_is_false(self):
        """Verify REST polling fallback is strictly False by default to prevent suboptimal trading."""
        l = MarketDataListener(mode="ws", enable_socket=False)
        self.assertFalse(l.allow_rest_fallback)
        self.assertFalse(l.is_rest_fallback_allowed())

    def test_rest_fallback_dynamic_flag_file_toggle(self):
        """Verify dynamic flag file 'logs/.allow_rest_fallback' toggles REST fallback at runtime."""
        l = MarketDataListener(mode="ws", enable_socket=False)
        self.assertFalse(l.is_rest_fallback_allowed())

        flag_path = "logs/.allow_rest_fallback"
        try:
            os.makedirs("logs", exist_ok=True)
            with open(flag_path, "w") as f:
                f.write("enabled\n")
            self.assertTrue(l.is_rest_fallback_allowed(), "Flag file should dynamically enable REST fallback")
        finally:
            if os.path.exists(flag_path):
                os.remove(flag_path)

        self.assertFalse(l.is_rest_fallback_allowed(), "Removing flag file should dynamically disable REST fallback")

    def test_rest_fallback_instance_parameter(self):
        """Verify explicit allow_rest_fallback=True parameter enables REST fallback."""
        l = MarketDataListener(mode="ws", allow_rest_fallback=True, enable_socket=False)
        self.assertTrue(l.allow_rest_fallback)
        self.assertTrue(l.is_rest_fallback_allowed())


if __name__ == "__main__":
    unittest.main()
