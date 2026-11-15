"""
Unit Tests for Quotes Broker Service & QuotesBrokerClient.
Verifies LVC cache, request-reply snapshots, and on-demand pub/sub streaming.
"""
import time
import unittest
import zmq

from core.common.ipc_schemas import unpack_message
from core.quotes.quotes_broker import QuotesBroker, QuotesBrokerClient

TEST_FEED_IPC = "inproc://test_feed_ipc"
TEST_ROUTER_IPC = "inproc://test_quotes_router"
TEST_STREAM_IPC = "inproc://test_quotes_stream"
TEST_HB_IPC = "inproc://test_quotes_hb"


class TestQuotesBroker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = zmq.Context()
        cls.broker = QuotesBroker(
            feed_ipc=TEST_FEED_IPC,
            router_ipc=TEST_ROUTER_IPC,
            stream_ipc=TEST_STREAM_IPC,
            heartbeat_ipc=TEST_HB_IPC,
            ctx=cls.ctx,
        )
        import threading
        cls.broker_thread = threading.Thread(target=cls.broker.start, daemon=True)
        cls.broker_thread.start()
        time.sleep(0.1)

        cls.client = QuotesBrokerClient(router_ipc=TEST_ROUTER_IPC, ctx=cls.ctx)

        # Setup subscriber to quotes_stream
        cls.stream_sub = cls.ctx.socket(zmq.SUB)
        cls.stream_sub.setsockopt(zmq.LINGER, 0)
        cls.stream_sub.setsockopt(zmq.RCVTIMEO, 500)
        cls.stream_sub.setsockopt(zmq.SUBSCRIBE, b"QUOTE")
        cls.stream_sub.connect(TEST_STREAM_IPC)
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.stream_sub.close(linger=0)
        cls.client.close()
        cls.broker.stop()
        cls.ctx.term()

    def test_lvc_update_and_query(self):
        # Inject tick directly into broker with 5-level depth
        self.broker.on_tick({
            "token": "NSE:TCS26NOV3500CE",
            "symbol": "TCS",
            "ltp": 18.5,
            "bid": 18.4,
            "ask": 18.6,
            "bid_qty": 300,
            "ask_qty": 450,
            "bids": [[18.4, 300.0, 2.0], [18.3, 600.0, 3.0]],
            "asks": [[18.6, 450.0, 1.0], [18.7, 900.0, 4.0]],
            "exchange_timestamp": 1700000000.0,
        })

        # Query via client with best_depth=True (default for 1-lot fast trading)
        q = self.client.get_quote("NSE:TCS26NOV3500CE", best_depth=True)
        self.assertIsNotNone(q)
        self.assertEqual(q["ltp"], 18.5)
        self.assertEqual(q["bid"], 18.4)
        self.assertEqual(q["ask"], 18.6)
        self.assertEqual(q["bid_qty"], 300)
        self.assertEqual(q["ask_qty"], 450)
        self.assertNotIn("bids", q)
        self.assertNotIn("asks", q)

        # Query via client with best_depth=False (preserves full 5-level depth for sim/expansion)
        q_full = self.client.get_quote("NSE:TCS26NOV3500CE", best_depth=False)
        self.assertIsNotNone(q_full)
        self.assertIn("bids", q_full)
        self.assertIn("asks", q_full)
        self.assertEqual(len(q_full["bids"]), 2)
        self.assertEqual(len(q_full["asks"]), 2)
        self.assertEqual(q_full["bids"][0][0], 18.4)
        self.assertEqual(q_full["asks"][0][0], 18.6)

        # Batch query with best_depth=False
        batch = self.client.get_quotes(["NSE:TCS26NOV3500CE", "NSE:NONEXISTENT"], best_depth=False)
        self.assertIn("NSE:TCS26NOV3500CE", batch)
        self.assertNotIn("NSE:NONEXISTENT", batch)
        self.assertEqual(len(batch["NSE:TCS26NOV3500CE"]["bids"]), 2)

    def test_pubsub_subscription_and_selective_streaming(self):
        token_sub = "NSE:RELIANCE26NOV2500CE"
        token_unsub = "NSE:INFY26NOV1800PE"

        # Initially, neither is subscribed
        self.assertNotIn(token_sub, self.broker.subscribed_tokens)

        # Subscribe token_sub via client
        snap = self.client.subscribe([token_sub])
        self.assertIn(token_sub, self.broker.subscribed_tokens)

        # Send tick for subscribed token -> MUST be published to stream
        self.broker.on_tick({
            "token": token_sub,
            "symbol": "RELIANCE",
            "ltp": 55.0,
            "bid": 54.8,
            "ask": 55.2,
            "bid_qty": 250,
            "ask_qty": 250,
            "exchange_timestamp": 1700000005.0,
        })

        topic, payload = self.stream_sub.recv_multipart()
        self.assertTrue(topic.startswith(b"QUOTE."))
        data = unpack_message(payload)
        self.assertEqual(data["token"], token_sub)
        self.assertEqual(data["ltp"], 55.0)

        # Send tick for unsubscribed token -> MUST NOT be received
        self.broker.on_tick({
            "token": token_unsub,
            "symbol": "INFY",
            "ltp": 30.0,
            "bid": 29.8,
            "ask": 30.2,
            "exchange_timestamp": 1700000006.0,
        })
        with self.assertRaises(zmq.Again):
            self.stream_sub.recv_multipart()

        # Unsubscribe token_sub
        success = self.client.unsubscribe([token_sub])
        self.assertTrue(success)
        self.assertNotIn(token_sub, self.broker.subscribed_tokens)

    def test_multi_client_reference_counting(self):
        """Verify that multiple superstructures (paper, paper_delayed, live) share subscriptions safely via ref-counting."""
        shared_token = "NSE:HDFCBANK26NOV1600CE"

        # Initially not subscribed
        self.assertEqual(self.broker.get_ref_count(shared_token), 0)
        self.assertNotIn(shared_token, self.broker.subscribed_tokens)

        # 1. Paper mode subscribes
        self.client.subscribe([shared_token], client_id="paper")
        self.assertEqual(self.broker.get_ref_count(shared_token), 1)
        self.assertIn(shared_token, self.broker.subscribed_tokens)

        # 2. Paper mode calls subscribe again (idempotent; must not inflate ref count)
        self.client.subscribe([shared_token], client_id="paper")
        self.assertEqual(self.broker.get_ref_count(shared_token), 1)

        # 3. Paper Delayed mode subscribes 30 seconds later to the same strike
        self.client.subscribe([shared_token], client_id="paper_delayed")
        self.assertEqual(self.broker.get_ref_count(shared_token), 2)
        self.assertIn(shared_token, self.broker.subscribed_tokens)

        # 4. Live mode also enters
        self.client.subscribe([shared_token], client_id="live")
        self.assertEqual(self.broker.get_ref_count(shared_token), 3)

        # 5. Paper exits trade and unsubs -> Ref count drops to 2, contract STILL STREAMING
        self.client.unsubscribe([shared_token], client_id="paper")
        self.assertEqual(self.broker.get_ref_count(shared_token), 2)
        self.assertIn(shared_token, self.broker.subscribed_tokens)

        # Send tick: must still be published since paper_delayed & live are still active
        self.broker.on_tick({
            "token": shared_token,
            "symbol": "HDFCBANK",
            "ltp": 42.0,
            "bid": 41.5,
            "ask": 42.5,
            "exchange_timestamp": 1700000010.0,
        })
        topic, payload = self.stream_sub.recv_multipart()
        self.assertEqual(topic, f"QUOTE.{shared_token}".encode("utf-8"))
        data = unpack_message(payload)
        self.assertEqual(data["ltp"], 42.0)

        # 6. Paper Delayed exits trade -> Ref count drops to 1, contract STILL STREAMING for live
        self.client.unsubscribe([shared_token], client_id="paper_delayed")
        self.assertEqual(self.broker.get_ref_count(shared_token), 1)
        self.assertIn(shared_token, self.broker.subscribed_tokens)

        # 7. Live exits trade -> Ref count drops to 0, contract STOPPED
        self.client.unsubscribe([shared_token], client_id="live")
        self.assertEqual(self.broker.get_ref_count(shared_token), 0)
        self.assertNotIn(shared_token, self.broker.subscribed_tokens)

        # Send tick after 0 ref count -> Must NOT be published
        self.broker.on_tick({
            "token": shared_token,
            "symbol": "HDFCBANK",
            "ltp": 43.0,
            "bid": 42.5,
            "ask": 43.5,
            "exchange_timestamp": 1700000015.0,
        })
        with self.assertRaises(zmq.Again):
            self.stream_sub.recv_multipart()


if __name__ == "__main__":
    unittest.main()
