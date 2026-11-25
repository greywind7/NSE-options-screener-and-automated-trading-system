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

    def test_dual_connection_partitioning_disjoint_sets(self):
        """Verify deterministic underlying sharding across dual connections with 100% disjoint subscription sets."""
        insts = self.feed.load_instruments()
        self.assertEqual(self.feed.num_connections, 2)
        self.assertEqual(len(self.feed.connections), 2)

        conn0 = self.feed.connections[0]
        conn1 = self.feed.connections[1]

        # Both connections must have subscribed keys
        self.assertGreater(len(conn0.subscribed_keys), 0)
        self.assertGreater(len(conn1.subscribed_keys), 0)

        # Invariant 1: Underlyings are 100% disjoint
        und_intersection = conn0.underlying_symbols & conn1.underlying_symbols
        self.assertEqual(und_intersection, set(), f"Underlyings overlap between connections: {und_intersection}")

        # Invariant 2: Subscriptions are 100% disjoint (no cross-connection duplicates)
        sub_intersection = conn0.subscribed_keys & conn1.subscribed_keys
        self.assertEqual(sub_intersection, set(), f"Subscribed keys overlap between connections: {sub_intersection}")

        # Invariant 3: Active options are 100% disjoint
        opt_intersection = conn0.active_option_keys & conn1.active_option_keys
        self.assertEqual(opt_intersection, set(), f"Option keys overlap between connections: {opt_intersection}")

        # Invariant 4: Key routing matches assigned connection
        for k in list(conn0.subscribed_keys)[:20]:
            self.assertEqual(self.feed.get_connection_for_key(k), 0)
        for k in list(conn1.subscribed_keys)[:20]:
            self.assertEqual(self.feed.get_connection_for_key(k), 1)

        # Diagnostic stats check
        stats = self.feed.get_connection_stats()
        self.assertEqual(len(stats), 2)
        self.assertEqual(stats[0]["conn_id"], 0)
        self.assertEqual(stats[1]["conn_id"], 1)

    def test_dual_connection_dynamic_atm_shift_and_isolation(self):
        """Verify dynamic ATM strikes (+-3 CE/PE) evaluate and isolate cleanly to their assigned connection."""
        self.feed.symbol_to_conn["STOCK_C0"] = 0
        self.feed.connections[0].underlying_symbols.add("STOCK_C0")
        self.feed.symbol_to_conn["STOCK_C1"] = 1
        self.feed.connections[1].underlying_symbols.add("STOCK_C1")

        strikes = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0]
        make_strikes_dict = lambda sym: {
            f"{s:.1f}": {"CE": f"NSE_FO|{sym}_{int(s)}CE", "PE": f"NSE_FO|{sym}_{int(s)}PE"}
            for s in strikes
        }

        self.feed.options_map["STOCK_C0"] = {"strikes": make_strikes_dict("STOCK_C0")}
        self.feed.options_map["STOCK_C1"] = {"strikes": make_strikes_dict("STOCK_C1")}
        self.feed.available_strikes_by_symbol["STOCK_C0"] = strikes
        self.feed.available_strikes_by_symbol["STOCK_C1"] = strikes
        self.feed.underlying_key_to_symbol["NSE_EQ|STOCK_C0"] = "STOCK_C0"
        self.feed.underlying_key_to_symbol["NSE_EQ|STOCK_C1"] = "STOCK_C1"

        # 1. Price of STOCK_C0 is 140 -> ATM is 140 -> +-3 strikes: 110..170 (7 strikes, 14 contracts)
        to_add_0, to_remove_0 = self.feed.evaluate_dynamic_options("NSE_EQ|STOCK_C0", 140.0)
        self.assertEqual(len(to_add_0), 14)  # 7 strikes * 2 (CE + PE)
        self.assertEqual(len(to_remove_0), 0)

        # Verify all added options are on Connection 0
        for opt in to_add_0:
            self.assertIn(opt, self.feed.connections[0].active_option_keys)
            self.assertIn(opt, self.feed.connections[0].subscribed_keys)
            self.assertEqual(self.feed.get_connection_for_key(opt), 0)
            # Connection 1 must not have these
            self.assertNotIn(opt, self.feed.connections[1].active_option_keys)

        # 2. Price of STOCK_C1 is 150 -> ATM is 150 -> +-3 strikes: 120..180 (7 strikes, 14 contracts)
        to_add_1, to_remove_1 = self.feed.evaluate_dynamic_options("NSE_EQ|STOCK_C1", 150.0)
        self.assertEqual(len(to_add_1), 14)

        # Verify all added options are on Connection 1
        for opt in to_add_1:
            self.assertIn(opt, self.feed.connections[1].active_option_keys)
            self.assertIn(opt, self.feed.connections[1].subscribed_keys)
            self.assertEqual(self.feed.get_connection_for_key(opt), 1)
            # Connection 0 must not have these
            self.assertNotIn(opt, self.feed.connections[0].active_option_keys)

        # 3. Register open position for STOCK_C1 (150 CE)
        opt_c1_150ce = "NSE_FO|STOCK_C1_150CE"
        self.feed.position_registry.register_open_position(
            contract_key=opt_c1_150ce,
            symbol="STOCK_C1",
            contract_symbol="STOCK_C1 150 CE",
            mode="live",
            track="ATM",
            trade_id="TR_C1",
        )

        # 4. STOCK_C1 crashes to 100 -> ATM is 100 -> 150CE is way outside ATM+-3 and outside hysteresis
        self.feed.last_reval_time_by_symbol["STOCK_C1"] = 0.0
        to_add_crash, to_remove_crash = self.feed.evaluate_dynamic_options("NSE_EQ|STOCK_C1", 100.0)

        # Protected position guard must prevent unsubscription of 150CE on Connection 1
        self.assertNotIn(opt_c1_150ce, to_remove_crash)
        self.assertIn(opt_c1_150ce, self.feed.connections[1].active_option_keys)

    def test_thread_safe_concurrent_publishing(self):
        """Verify thread-safe ZeroMQ publishing under concurrent multi-connection traffic."""
        time.sleep(0.15)
        published_eq = []
        published_opt = []
        num_threads = 4
        ticks_per_thread = 25

        def worker(thread_idx: int):
            for i in range(ticks_per_thread):
                now = time.time()
                # Publish equity tick
                self.feed.publish_tick(
                    token=f"NSE_EQ|TEST_{thread_idx}_{i}",
                    symbol=f"STOCK_{thread_idx}",
                    ltp=100.0 + i,
                    exchange_ts=now,
                    received_ts=now,
                    conn_id=thread_idx % 2,
                )
                # Publish option tick
                self.feed.publish_tick(
                    token=f"NSE_FO|OPT_{thread_idx}_{i}_CE",
                    symbol=f"STOCK_{thread_idx} {100+i} CE",
                    ltp=10.0 + i,
                    bids=[[10.0, 50, 1]],
                    asks=[[10.5, 50, 1]],
                    exchange_ts=now,
                    received_ts=now,
                    conn_id=thread_idx % 2,
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_total = num_threads * ticks_per_thread
        self.assertEqual(self.feed.total_eq_published, expected_total)
        self.assertEqual(self.feed.total_opt_published, expected_total)

        # Receive all ticks and verify zero frame corruption
        for _ in range(expected_total):
            topic, payload = self.sub_eq.recv_multipart()
            self.assertEqual(topic, b"TICK.EQ")
            event = unpack_message(payload)
            self.assertIn("STOCK_", event["symbol"])

        for _ in range(expected_total):
            topic, payload = self.sub_opt.recv_multipart()
            self.assertEqual(topic, b"TICK.OPT")
            event = unpack_message(payload)
            self.assertIn("OPT_", event["token"])
            self.assertEqual(len(event["bids"]), 1)

    def test_deterministic_crc32_sharding_stability(self):
        """Verify CRC32-based sharding is stable across restarts and adding/removing symbols."""
        syms = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN", "ICICIBANK", "BHARTIARTL"]
        initial_map = {s: self.feed.get_connection_for_symbol(s) for s in syms}

        # NIFTY variants must always pin to Connection 0
        self.assertEqual(self.feed.get_connection_for_symbol("NIFTY"), 0)
        self.assertEqual(self.feed.get_connection_for_symbol("NIFTY 50"), 0)

        # Create a brand new FeedHandler instance and verify identical shard assignments
        new_feed = FeedHandler(
            mode="mock",
            feed_equity_ipc="inproc://test_feed_eq2",
            feed_options_ipc="inproc://test_feed_opt2",
            num_connections=2,
            silent=True,
            ctx=self.ctx,
        )
        try:
            for s in syms:
                self.assertEqual(new_feed.get_connection_for_symbol(s), initial_map[s],
                                 f"Sharding drift detected for symbol {s}")
            # Add a new symbol in the middle
            new_feed.get_connection_for_symbol("NEWSTOCK")
            # Existing symbols must NOT change connection
            for s in syms:
                self.assertEqual(new_feed.get_connection_for_symbol(s), initial_map[s],
                                 f"Adding new symbol shifted shard for {s}")
        finally:
            new_feed.stop()

    def test_capacity_pruning_cleans_other_symbols_sets(self):
        """Verify capacity pruning removes unheld contracts from other symbols' active sets (BUG-006 fix)."""
        conn = self.feed.connections[0]
        conn.max_contracts = 6  # very tight limit to force pruning
        conn.underlying_keys = {"NSE_EQ|STOCK_A", "NSE_EQ|STOCK_B"}
        # STOCK_A has 2 options
        conn.active_options_by_symbol["STOCK_A"] = {"NSE_FO|STOCK_A_100CE", "NSE_FO|STOCK_A_100PE"}
        conn.active_option_keys = {"NSE_FO|STOCK_A_100CE", "NSE_FO|STOCK_A_100PE"}
        conn.subscribed_keys = set(conn.underlying_keys) | set(conn.active_option_keys)
        self.feed.options_map["STOCK_B"] = {
            "strikes": {
                "200.0": {"CE": "NSE_FO|STOCK_B_200CE", "PE": "NSE_FO|STOCK_B_200PE"},
                "210.0": {"CE": "NSE_FO|STOCK_B_210CE", "PE": "NSE_FO|STOCK_B_210PE"},
            }
        }
        self.feed.available_strikes_by_symbol["STOCK_B"] = [200.0, 210.0]
        self.feed.underlying_key_to_symbol["NSE_EQ|STOCK_B"] = "STOCK_B"
        self.feed.symbol_to_conn["STOCK_B"] = 0

        # Adding 4 options for STOCK_B: 2 (underlyings) + 2 (A options) + 4 (new B options) = 8 > max 6
        to_add, to_remove = self.feed.evaluate_dynamic_options("NSE_EQ|STOCK_B", 200.0, conn_id=0)
        self.assertGreater(len(to_add), 0)
        # Pruning must have removed keys from STOCK_A
        self.assertGreater(len(to_remove), 0)
        for pruned_key in to_remove:
            self.assertNotIn(pruned_key, conn.active_options_by_symbol["STOCK_A"],
                             f"Pruned key {pruned_key} still present in STOCK_A options set!")


if __name__ == "__main__":
    unittest.main()
