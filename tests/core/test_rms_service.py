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


if __name__ == "__main__":
    unittest.main()

