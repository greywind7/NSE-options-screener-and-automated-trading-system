"""
Unit Tests for Risk Management Service (RMS) & Strike Selection Engine.
Verifies strike resolution, hybrid spread gate (Gap 1 / HIGH-004), and daily loss circuit breaker (HIGH-008).
"""
import time
import unittest
import zmq

from core.common.ipc_schemas import SetupAlert, ActionableOpportunity
from core.rms.rms_service import RMSService, get_strike_step
from core.quotes.quotes_broker import QuotesBroker


class TestRMSService(unittest.TestCase):
    def setUp(self):
        self.ctx = zmq.Context()
        self.alerts_ipc = "inproc://test_rms_alerts"
        self.rms_pub_ipc = "inproc://test_rms_opps"
        self.quotes_router_ipc = "inproc://test_rms_quotes"
        self.hb_ipc = "inproc://test_rms_hb"

        # Start QuotesBroker
        self.broker = QuotesBroker(
            feed_ipc="inproc://dummy_feed",
            router_ipc=self.quotes_router_ipc,
            stream_ipc="inproc://test_rms_stream",
            heartbeat_ipc=self.hb_ipc,
            ctx=self.ctx,
        )

        import threading
        self.broker_thread = threading.Thread(target=self.broker.start, daemon=True)
        self.broker_thread.start()
        time.sleep(0.05)

        # Start RMS Service
        self.rms = RMSService(
            alerts_ipc=self.alerts_ipc,
            rms_pub_ipc=self.rms_pub_ipc,
            quotes_router_ipc=self.quotes_router_ipc,
            max_daily_loss=10000.0,
            universe_csv="data/filtered.csv",
            default_expiry_tag="26NOV",
            ctx=self.ctx,
        )

    def tearDown(self):
        self.rms.stop()
        self.broker.stop()
        self.ctx.destroy(linger=0)

    def test_strike_resolution_and_spread_gate(self):
        # RELIANCE trigger at 2500.0 (step = 100.0, ATM = 2500, ITM1 = 2400, OTM1 = 2600)
        # Inject quotes into broker
        atm_token = "NSE:RELIANCE26NOV2500CE"
        itm_token = "NSE:RELIANCE26NOV2400CE"
        otm_token = "NSE:RELIANCE26NOV2600CE"

        # Tight liquid spread for ATM (ask 20.20, bid 20.00 -> spread 0.20 <= max(0.50, 0.03*20.20=0.60))
        self.broker.on_tick({
            "token": atm_token,
            "symbol": "RELIANCE",
            "ltp": 20.10,
            "bid": 20.00,
            "ask": 20.20,
            "bid_qty": 500,
            "ask_qty": 500,
        })
        # Tight spread for ITM1
        self.broker.on_tick({
            "token": itm_token,
            "symbol": "RELIANCE",
            "ltp": 85.00,
            "bid": 84.50,
            "ask": 85.50,
            "bid_qty": 300,
            "ask_qty": 300,
        })
        # Illiquid wide spread for OTM1 (ask 10.00, bid 6.00 -> spread 4.00 > max(0.50, 0.03*10=0.50))
        self.broker.on_tick({
            "token": otm_token,
            "symbol": "RELIANCE",
            "ltp": 8.00,
            "bid": 6.00,
            "ask": 10.00,
            "bid_qty": 100,
            "ask_qty": 100,
        })

        alert = SetupAlert(
            alert_id="ALT_REL_TEST",
            symbol="RELIANCE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=2500.0,
            rvol=3.0,
            clv=0.85,
            atr=25.0,
            flag_high=2500.0,
            flag_low=2480.0,
            timestamp=time.time(),
        )

        opp = self.rms.process_alert(alert)
        self.assertIsNotNone(opp)
        self.assertEqual(opp.symbol, "RELIANCE")

        # Verify ATM and ITM1 passed the spread gate
        self.assertIn("ATM", opp.strikes)
        self.assertIn("ITM1", opp.strikes)
        self.assertEqual(opp.strikes["ATM"]["ask"], 20.20)
        self.assertEqual(opp.strikes["ATM"]["bid"], 20.00)

        # Gap 1 / HIGH-004 Verification: OTM1 had wide spread of 4.00 and MUST be filtered out!
        self.assertNotIn("OTM1", opp.strikes)

    def test_mode_risk_manager_daily_loss_and_capital(self):
        from core.rms.mode_risk import ModeRiskManager
        mgr = ModeRiskManager(
            mode="paper",
            starting_capital=250000.0,
            daily_loss_limit_inr=10000.0,
            max_capital_per_trade_pct=0.50,
        )
        self.assertEqual(mgr.available_capital, 250000.0)
        self.assertFalse(mgr.is_circuit_breaker_tripped())

        # Commit capital for a trade: ₹50,000
        self.assertTrue(mgr.can_allocate(50000.0))
        self.assertTrue(mgr.commit_capital(50000.0))
        self.assertEqual(mgr.available_capital, 200000.0)

        # Release capital with -₹12,000 loss (exceeding daily loss limit of ₹10,000)
        mgr.release_capital(committed_amount=50000.0, realized_pnl=-12000.0)
        self.assertEqual(mgr.realized_pnl, -12000.0)
        self.assertTrue(mgr.is_circuit_breaker_tripped())
        self.assertFalse(mgr.can_allocate(10000.0))

    def test_options_map_moneyness_resolution_itm2_otm2(self):
        """Verify RMS resolves ATM, ITM1, ITM2, OTM1, OTM2 from options_map without UnboundLocalError."""
        self.rms.options_map["TCS"] = {
            "strikes": {
                "3400.0": {"CE": "NSE_FO|TCS3400CE", "PE": "NSE_FO|TCS3400PE"},
                "3450.0": {"CE": "NSE_FO|TCS3450CE", "PE": "NSE_FO|TCS3450PE"},
                "3500.0": {"CE": "NSE_FO|TCS3500CE", "PE": "NSE_FO|TCS3500PE"},
                "3550.0": {"CE": "NSE_FO|TCS3550CE", "PE": "NSE_FO|TCS3550PE"},
                "3600.0": {"CE": "NSE_FO|TCS3600CE", "PE": "NSE_FO|TCS3600PE"},
            }
        }
        # Inject quotes into broker for all 5 strikes
        for s_val, tok in [
            (3400.0, "NSE:TCS3400CE"),
            (3450.0, "NSE:TCS3450CE"),
            (3500.0, "NSE:TCS3500CE"),
            (3550.0, "NSE:TCS3550CE"),
            (3600.0, "NSE:TCS3600CE"),
        ]:
            self.broker.on_tick({
                "token": tok,
                "symbol": "TCS",
                "ltp": 50.0,
                "bid": 49.8,
                "ask": 50.2,
                "bid_qty": 100,
                "ask_qty": 100,
            })

        # Trigger at 3500.0 (BULL CE): ATM=3500, ITM1=3450, ITM2=3400, OTM1=3550, OTM2=3600
        resolved = self.rms.resolve_strikes("TCS", "BULL", 3500.0)
        self.assertIn("ATM", resolved)
        self.assertIn("ITM1", resolved)
        self.assertIn("ITM2", resolved)
        self.assertIn("OTM1", resolved)
        self.assertIn("OTM2", resolved)
        self.assertEqual(resolved["ATM"]["strike"], 3500.0)
        self.assertEqual(resolved["ITM1"]["strike"], 3450.0)
        self.assertEqual(resolved["ITM2"]["strike"], 3400.0)
        self.assertEqual(resolved["OTM1"]["strike"], 3550.0)
        self.assertEqual(resolved["OTM2"]["strike"], 3600.0)

    def test_dynamic_expiry_tag_and_config_spread_gate(self):
        from datetime import date
        from core.rms.rms_service import get_current_expiry_tag, passes_spread_gate

        self.assertEqual(get_current_expiry_tag(date(2026, 11, 25)), "26NOV")
        self.assertEqual(get_current_expiry_tag(date(2026, 11, 26)), "26NOV")
        self.assertEqual(get_current_expiry_tag(date(2026, 11, 27)), "03DEC")
        self.assertIn("max_spread_pct", self.rms.spread_gate)
        self.assertAlmostEqual(self.rms.spread_gate["max_spread_pct"], 0.03, places=6)

        # Strict percentage gate: ₹0.50 on a ₹10.00 ask is 5% (> 3%) and must be rejected
        self.assertFalse(passes_spread_gate(bid=9.50, ask=10.00, max_spread_pct=0.03))
        self.assertTrue(passes_spread_gate(bid=9.75, ask=10.00, max_spread_pct=0.03))

        # Verify QuotesBroker subscribes & forwards TICK.EQ to underlying equity subscribers
        from core.common.ipc_schemas import pack_message, unpack_message
        stream_sub = self.ctx.socket(zmq.SUB)
        stream_sub.setsockopt(zmq.LINGER, 0)
        stream_sub.setsockopt(zmq.RCVTIMEO, 1000)
        stream_sub.setsockopt(zmq.SUBSCRIBE, b"QUOTE.NSE:RELIANCE")
        stream_sub.connect("inproc://test_rms_stream")
        time.sleep(0.05)
        try:
            client = self.rms.quotes_client
            client.subscribe(["NSE:RELIANCE"], client_id="paper_test")
            self.broker.on_tick({
                "token": "NSE:INE002A01018",
                "symbol": "RELIANCE",
                "ltp": 2512.50,
                "bid": 2512.0,
                "ask": 2513.0,
                "bid_qty": 10,
                "ask_qty": 10,
            })
            frames = stream_sub.recv_multipart()
            self.assertEqual(frames[0], b"QUOTE.NSE:RELIANCE")
            q_msg = unpack_message(frames[1])
            self.assertEqual(q_msg["ltp"], 2512.50)
        finally:
            stream_sub.close(linger=0)

    def test_mode_risk_manager_circuit_breaker_flag(self):
        from core.rms.mode_risk import ModeRiskManager
        mgr_off = ModeRiskManager(
            mode="paper",
            starting_capital=250000.0,
            daily_loss_limit_inr=10000.0,
            enable_circuit_breaker=False,
        )
        mgr_off.release_capital(committed_amount=50000.0, realized_pnl=-15000.0)
        self.assertFalse(mgr_off.is_circuit_breaker_tripped())
        self.assertTrue(mgr_off.can_allocate(10000.0))


if __name__ == "__main__":
    unittest.main()

