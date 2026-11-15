"""
Unit tests for Core Market Data FeedHandler (Spec 2.0).
Verifies:
1. Universe discovery and symbol mapping.
2. Dual ZeroMQ stream separation:
   - Equity ticks broadcast on feed_equity_ipc (TICK.EQ)
   - Option depth ticks broadcast on feed_options_ipc (TICK.OPT)
3. Dynamic popular options ATM selection and Active Position Protection.
4. Clean shutdown and socket cleanup.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
import zmq

from core.common.ipc_schemas import unpack_message
from core.feed.feed_handler import FeedHandler
from core.feed.position_registry import ProtectedPositionRegistry


class TestFeedHandler(unittest.TestCase):
    def setUp(self):
        self.ctx = zmq.Context()
        self.eq_ipc = "inproc://test_feed_eq"
        self.opt_ipc = "inproc://test_feed_opt"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.reg_path = os.path.join(self.temp_dir.name, "active_pos.json")

        self.feed = FeedHandler(
            mode="mock",
            feed_equity_ipc=self.eq_ipc,
            feed_options_ipc=self.opt_ipc,
            poll_interval=0.05,
            silent=True,
            ctx=self.ctx,
        )
        self.feed.position_registry = ProtectedPositionRegistry(registry_file=self.reg_path)
        self.feed.position_registry.clear()

        # ZeroMQ Test Subscriber
        self.sub_eq = self.ctx.socket(zmq.SUB)
        self.sub_eq.setsockopt(zmq.LINGER, 0)
        self.sub_eq.setsockopt(zmq.SUBSCRIBE, b"TICK.EQ")
        self.sub_eq.connect(self.eq_ipc)

        self.sub_opt = self.ctx.socket(zmq.SUB)
        self.sub_opt.setsockopt(zmq.LINGER, 0)
        self.sub_opt.setsockopt(zmq.SUBSCRIBE, b"TICK.OPT")
        self.sub_opt.connect(self.opt_ipc)

    def tearDown(self):
        self.feed.stop()
        self.sub_eq.close(0)
        self.sub_opt.close(0)
        self.ctx.term()
        self.temp_dir.cleanup()

    def test_instrument_discovery(self):
        """Verify feed discovers monitored instruments and builds symbol map."""
        insts = self.feed.load_instruments()
        self.assertGreater(len(insts), 0)
        keys = [x["key"] for x in insts]
        self.assertIn("NSE_INDEX|Nifty 50", keys)
        self.assertIn("NSE_INDEX|Nifty 50", self.feed.symbol_map)

    def test_dual_socket_separation(self):
        """Verify equity ticks route to feed_equity_ipc and option ticks route to feed_options_ipc."""
        # Allow ZeroMQ subscription handshake to settle (slow joiner guard)
        time.sleep(0.15)

        # Explicitly publish an equity tick and an option tick
        now = time.time()
        self.feed.publish_tick(
            token="NSE_EQ|INE002A01018",
            symbol="RELIANCE",
            ltp=2500.0,
            bid=2499.5,
            ask=2500.5,
            exchange_ts=now,
            received_ts=now,
        )

        self.feed.publish_tick(
            token="NSE_FO|RELIANCE26NOV2500CE",
            symbol="RELIANCE 2500 CE",
            ltp=45.0,
            bid=44.5,
            ask=45.5,
            bids=[[44.5, 250, 1]],
            asks=[[45.5, 250, 1]],
            exchange_ts=now,
            received_ts=now,
        )

        # 1. Equity socket must receive the equity tick
        eq_topic, eq_payload = self.sub_eq.recv_multipart()
        self.assertEqual(eq_topic, b"TICK.EQ")
        eq_tick = unpack_message(eq_payload)
        self.assertIn("RELIANCE", eq_tick.get("symbol", ""))

        # 2. Options socket must receive the option depth tick with 5-level arrays preserved
        opt_topic, opt_payload = self.sub_opt.recv_multipart()
        self.assertEqual(opt_topic, b"TICK.OPT")
        opt_tick = unpack_message(opt_payload)
        self.assertIn("2500", opt_tick.get("token", ""))
        self.assertEqual(len(opt_tick.get("bids", [])), 1)

    def test_dynamic_popular_options_and_position_protection(self):
        """Verify dynamic ATM strike re-evaluation and Active Position Protection Guard."""
        self.feed.options_map = {
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
                }
            }
        }
        self.feed.available_strikes_by_symbol = {
            "TEST_STOCK": [2400.0, 2420.0, 2440.0, 2460.0, 2480.0, 2500.0, 2520.0, 2540.0, 2560.0, 2580.0, 2600.0]
        }
        self.feed.underlying_key_to_symbol["NSE_EQ|INE999A01010"] = "TEST_STOCK"
        self.feed.symbol_map["NSE_EQ|INE999A01010"] = "TEST_STOCK"

        # 1. Price at 2450 -> ATM = 2440 or 2460
        to_add, to_remove = self.feed.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2450.0)
        self.assertGreater(len(to_add), 0)
        self.assertIn("NSE_FO|2440CE", to_add)

        # 2. Register open position for 2440CE
        self.feed.position_registry.register_open_position(
            contract_key="NSE_FO|2440CE",
            symbol="TEST_STOCK",
            contract_symbol="TEST STOCK 2440 CE",
            mode="live",
            track="ATM",
            trade_id="TR_101",
        )

        # 3. Price rallies to 2600 (far from 2440)
        self.feed.last_reval_time_by_symbol["TEST_STOCK"] = 0.0
        to_add_2, to_remove_2 = self.feed.evaluate_dynamic_options("NSE_EQ|INE999A01010", 2600.0)

        # Protection Invariant: 2440CE must NEVER be in to_remove_2!
        self.assertNotIn("NSE_FO|2440CE", to_remove_2)
        self.assertIn("NSE_FO|2440CE", self.feed.active_options_by_symbol["TEST_STOCK"])


if __name__ == "__main__":
    unittest.main()
