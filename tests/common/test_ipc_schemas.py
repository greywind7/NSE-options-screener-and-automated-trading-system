"""
Unit Tests for ZeroMQ Msgpack Wire Schemas.
"""
import unittest
from core.common.ipc_schemas import (
    EquityTickEvent,
    OptionDepthTickEvent,
    QuoteSnapshotRequest,
    QuoteSnapshotResponse,
    QuoteSubscriptionRequest,
    QuoteSubscriptionResponse,
    QuoteHeartbeat,
    QuoteDepthUpdate,
    TickEvent,
    SetupAlert,
    ActionableOpportunity,
    OrderRequest,
    OrderResult,
    pack_message,
    unpack_message,
)


class TestIPCSchemas(unittest.TestCase):
    def test_tick_event_pack_unpack(self):
        tick = TickEvent(
            token="NSE:RELIANCE",
            symbol="RELIANCE",
            ltp=2500.5,
            bid=2500.4,
            ask=2500.6,
            bid_qty=500,
            ask_qty=1200,
            exchange_timestamp=1700000000.5,
            received_timestamp=1700000000.505,
        )
        packed = pack_message(tick)
        self.assertIsInstance(packed, bytes)
        unpacked = unpack_message(packed)
        self.assertEqual(unpacked["token"], "NSE:RELIANCE")
        self.assertEqual(unpacked["ltp"], 2500.5)
        self.assertEqual(unpacked["ask_qty"], 1200)

    def test_setup_alert_pack_unpack(self):
        alert = SetupAlert(
            alert_id="ALT_001",
            symbol="TATAMOTORS",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=685.20,
            rvol=3.42,
            clv=0.88,
            atr=12.4,
            flag_high=685.0,
            flag_low=678.0,
            timestamp=1700000100.0,
        )
        packed = pack_message(alert)
        unpacked = unpack_message(packed)
        self.assertEqual(unpacked["archetype"], "FLAG_BULL_BREAKOUT")
        self.assertEqual(unpacked["rvol"], 3.42)

    def test_order_request_and_result(self):
        req = OrderRequest(
            order_id="ORD_001",
            mode="paper_delayed",
            track="FastMomentum",
            instrument_token="NSE:TCS26NOV3500CE",
            symbol="TCS",
            transaction_type="BUY",
            order_type="MARKET",
            quantity=100,
            price=18.5,
            trigger_price=18.5,
            timestamp=1700000200.0,
        )
        packed = pack_message(req)
        unpacked = unpack_message(packed)
        self.assertEqual(unpacked["instrument_token"], "NSE:TCS26NOV3500CE")
        self.assertEqual(unpacked["quantity"], 100)

        res = OrderResult(
            order_id="ORD_001",
            status="FILLED",
            fill_price=18.5,
            fill_quantity=100,
            fill_timestamp=1700000200.05,
        )
        packed_res = pack_message(res)
        unpacked_res = unpack_message(packed_res)
        self.assertEqual(unpacked_res["status"], "FILLED")
        self.assertEqual(unpacked_res["fill_price"], 18.5)

    def test_dual_tick_schemas(self):
        eq_tick = EquityTickEvent(
            token="NSE:RELIANCE",
            symbol="RELIANCE",
            ltp=2500.5,
            volume=150000,
            bid=2500.4,
            ask=2500.6,
            exchange_timestamp=1700000000.0,
        )
        packed_eq = pack_message(eq_tick)
        unpacked_eq = unpack_message(packed_eq)
        self.assertEqual(unpacked_eq["token"], "NSE:RELIANCE")
        self.assertEqual(unpacked_eq["volume"], 150000)

        opt_tick = OptionDepthTickEvent(
            token="NSE:RELIANCE26NOV2500CE",
            symbol="RELIANCE",
            ltp=45.0,
            bid=44.9,
            ask=45.1,
            bids=[[44.9, 500, 2]],
            asks=[[45.1, 750, 3]],
        )
        packed_opt = pack_message(opt_tick)
        unpacked_opt = unpack_message(packed_opt)
        self.assertEqual(unpacked_opt["token"], "NSE:RELIANCE26NOV2500CE")
        self.assertEqual(len(unpacked_opt["bids"]), 1)
        self.assertEqual(len(unpacked_opt["asks"]), 1)

    def test_quotes_broker_schemas(self):
        sub_req = QuoteSubscriptionRequest(action="SUBSCRIBE", tokens=["NSE:TCS26NOV3500CE"])
        unpacked_sub = unpack_message(pack_message(sub_req))
        self.assertEqual(unpacked_sub["action"], "SUBSCRIBE")
        self.assertEqual(unpacked_sub["tokens"], ["NSE:TCS26NOV3500CE"])

        hb = QuoteHeartbeat(timestamp=1700000001.0, active_quotes_count=50, subscribed_count=5)
        unpacked_hb = unpack_message(pack_message(hb))
        self.assertEqual(unpacked_hb["type"], "HEARTBEAT")
        self.assertEqual(unpacked_hb["subscribed_count"], 5)


if __name__ == "__main__":
    unittest.main()

