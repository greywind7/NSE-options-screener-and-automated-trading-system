"""
Unit Tests for Opportunity Pool & Quarantine Gatekeeper.
"""
import os
import time
import unittest
import zmq

from core.common.ipc_schemas import SetupAlert, unpack_message
from strategies.momentum.opportunity_pool import OpportunityPool


class TestOpportunityPool(unittest.TestCase):
    def setUp(self):
        self.ctx = zmq.Context()
        self.endpoint = "inproc://test_pool_pub"
        self.pool = OpportunityPool(
            pub_ipc=self.endpoint,
            cooldown_sec=1.0,  # 1 second for fast test
            ctx=self.ctx,
            persist_alerts=False,
        )

        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")
        self.sub.connect(self.endpoint)
        time.sleep(0.05)

    def tearDown(self):
        self.sub.close(linger=0)
        self.pool.stop()
        self.ctx.destroy(linger=0)
        for p in ["logs/alerts_1970-01-01.jsonl"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def test_non_blocking_cooldown_and_dispatch(self):
        alert_reliance = SetupAlert(
            alert_id="ALT_REL_1",
            symbol="RELIANCE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=2500.0,
            rvol=3.2,
            clv=0.85,
            atr=25.0,
            flag_high=2500.0,
            flag_low=2480.0,
            timestamp=1000.0,
        )

        # 1. First alert for RELIANCE is accepted
        ok1 = self.pool.submit_alert(alert_reliance)
        self.assertTrue(ok1)

        # 2. Duplicate alert for RELIANCE within cooldown is quarantined (returns False)
        alert_reliance_dup = SetupAlert(
            alert_id="ALT_REL_2",
            symbol="RELIANCE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=2502.0,
            rvol=3.4,
            clv=0.88,
            atr=25.0,
            flag_high=2500.0,
            flag_low=2480.0,
            timestamp=1000.5,
        )
        ok2 = self.pool.submit_alert(alert_reliance_dup)
        self.assertFalse(ok2)  # Quarantined

        # 3. Gap 2 Verification: Alert for DIFFERENT symbol (TCS) is NOT blocked!
        alert_tcs = SetupAlert(
            alert_id="ALT_TCS_1",
            symbol="TCS",
            direction="BULL",
            archetype="PURE_BULL_BREAKOUT",
            trigger_price=3500.0,
            rvol=4.1,
            clv=0.92,
            atr=30.0,
            flag_high=3500.0,
            flag_low=3470.0,
            timestamp=1000.6,
        )
        ok3 = self.pool.submit_alert(alert_tcs)
        self.assertTrue(ok3)  # Not blocked by RELIANCE cooldown!

        # Verify messages arrived at subscriber
        topic1, payload1 = self.sub.recv_multipart()
        self.assertEqual(topic1, b"ALERT.RELIANCE")
        msg1 = unpack_message(payload1)
        self.assertEqual(msg1["alert_id"], "ALT_REL_1")

        topic2, payload2 = self.sub.recv_multipart()
        self.assertEqual(topic2, b"ALERT.TCS")
        msg2 = unpack_message(payload2)
        self.assertEqual(msg2["alert_id"], "ALT_TCS_1")


if __name__ == "__main__":
    unittest.main()

