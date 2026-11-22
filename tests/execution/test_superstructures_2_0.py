"""
Unit Tests for Execution Superstructures (Paper, Delayed, Live).
Verifies single-owner journals (Gap 3), in-memory freeze flag (Gap 5),
and CRIT-007 event-driven delayed execution.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
import zmq

from core.common.ipc_schemas import ActionableOpportunity, OrderRequest
from execution.superstructures.paper import PaperSuperstructure
from execution.superstructures.delayed import DelayedSuperstructure
from execution.superstructures.live import LiveSuperstructure


class TestSuperstructures20(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_superstructures_")
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.destroy(linger=0)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_paper_execution_and_single_owner_journal(self):
        superstructure = PaperSuperstructure(
            track_names=["fast_momentum"],
            rms_ipc="inproc://dummy_rms",
            feed_ipc="inproc://dummy_feed",
            hb_ipc="inproc://dummy_hb",
            quotes_router_ipc="inproc://dummy_quotes",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )

        track = superstructure.tracks["fast_momentum"]

        # Submit an opportunity
        opp = ActionableOpportunity(
            opp_id="OPP_PAPER_1",
            alert_id="ALT_1",
            symbol="RELIANCE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=2500.0,
            strikes={
                "ATM": {
                    "token": "NSE:RELIANCE26NOV2500CE",
                    "strike": 2500.0,
                    "option_type": "CE",
                    "ltp": 25.0,
                    "bid": 24.8,
                    "ask": 25.0,
                    "lot_size": 250,
                }
            },
            timestamp=1794803400.0,  # 10:00 AM IST morning opportunity
        )

        superstructure.on_opportunity(opp)

        # In Paper mode, trade fills immediately!
        self.assertEqual(len(track.active_trades), 1)
        trade = list(track.active_trades.values())[0]
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.entry_price, 25.0)

        # Gap 5 Verification: In-memory atomic freeze flag
        superstructure.freeze()
        self.assertTrue(superstructure.is_frozen())
        self.assertTrue(track.context.is_frozen())

        # Second opportunity while frozen MUST be rejected
        opp2 = ActionableOpportunity(
            opp_id="OPP_PAPER_2",
            alert_id="ALT_2",
            symbol="TCS",
            direction="BULL",
            archetype="PURE_BULL_BREAKOUT",
            trigger_price=3500.0,
            strikes={
                "ATM": {
                    "token": "NSE:TCS26NOV3500CE",
                    "strike": 3500.0,
                    "option_type": "CE",
                    "ltp": 50.0,
                    "bid": 49.8,
                    "ask": 50.0,
                    "lot_size": 175,
                }
            },
            timestamp=1794803400.0,  # 10:00 AM IST morning opportunity
        )
        superstructure.on_opportunity(opp2)
        # Count should still be 1!
        self.assertEqual(len(track.active_trades), 1)

        superstructure.unfreeze()
        self.assertFalse(superstructure.is_frozen())

        # Close superstructure and check Gap 3: single-owner journal
        superstructure.stop()

        journal_files = []
        for root, _, filenames in os.walk(self.test_dir):
            for f in filenames:
                if f.startswith("journal_paper_fast_momentum"):
                    journal_files.append(os.path.join(root, f))

        self.assertEqual(len(journal_files), 1)
        with open(journal_files[0], "r", encoding="utf-8") as jf:
            lines = [json.loads(line) for line in jf if line.strip()]

        self.assertGreaterEqual(len(lines), 1)
        self.assertEqual(lines[0]["mode"], "paper")
        self.assertEqual(lines[0]["data"]["track"], "fast_momentum")

    def test_delayed_execution_crit_007(self):
        superstructure = DelayedSuperstructure(
            track_names=["fast_momentum"],
            delay_sec=2.0,  # 2 second delay
            rms_ipc="inproc://dummy_rms2",
            feed_ipc="inproc://dummy_feed2",
            hb_ipc="inproc://dummy_hb2",
            quotes_router_ipc="inproc://dummy_quotes2",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )

        track = superstructure.tracks["fast_momentum"]

        now = 1000.0
        opp = ActionableOpportunity(
            opp_id="OPP_DELAYED_1",
            alert_id="ALT_D1",
            symbol="INFY",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=1800.0,
            strikes={
                "ATM": {
                    "token": "NSE:INFY26NOV1800CE",
                    "strike": 1800.0,
                    "option_type": "CE",
                    "ltp": 30.0,
                    "bid": 29.8,
                    "ask": 30.0,
                    "lot_size": 400,
                }
            },
            timestamp=now,
        )

        superstructure.on_opportunity(opp)

        # Order was enqueued, NOT filled yet!
        self.assertEqual(len(superstructure._queue), 1)
        trade = list(track.active_trades.values())[0]
        # In base track, pending order is waiting
        self.assertEqual(trade.status, "OPEN")  # Initial state
        self.assertEqual(len(track.closed_trades), 0)

        # Heartbeat at now + 1.0s (less than delay of 2.0s): MUST NOT release!
        superstructure.on_heartbeat(now + 1.0)
        self.assertEqual(len(superstructure._queue), 1)

        # Heartbeat at now + 2.1s (greater than delay of 2.0s): RELEASES and FILLS!
        superstructure.on_heartbeat(now + 2.1)
        self.assertEqual(len(superstructure._queue), 0)
        self.assertEqual(trade.entry_price, 30.0)

        superstructure.stop()

    def test_delayed_random_delay_range(self):
        superstructure = DelayedSuperstructure(
            track_names=["fast_momentum"],
            min_delay_ms=200.0,
            max_delay_ms=500.0,
            rms_ipc="inproc://dummy_rms3",
            feed_ipc="inproc://dummy_feed3",
            hb_ipc="inproc://dummy_hb3",
            quotes_router_ipc="inproc://dummy_quotes3",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )
        self.assertEqual(superstructure.min_delay_ms, 200.0)
        self.assertEqual(superstructure.max_delay_ms, 500.0)

        now = 1000.0
        req = OrderRequest(
            order_id="TEST_RANDOM_DELAY",
            mode="paper_delayed",
            track="fast_momentum",
            instrument_token="NSE:RELIANCE",
            symbol="RELIANCE",
            transaction_type="BUY",
            order_type="MARKET",
            quantity=250,
            price=2500.0,
            trigger_price=2500.0,
            timestamp=now,
        )
        superstructure.submit_order(req)
        self.assertEqual(len(superstructure._queue), 1)
        release_ts, _, _ = superstructure._queue[0]
        self.assertGreaterEqual(release_ts, now + 0.20)
        self.assertLessEqual(release_ts, now + 0.50)
        superstructure.stop()

    def test_delayed_execution_capital_rejection_and_fallback(self):
        superstructure = DelayedSuperstructure(
            track_names=["fast_momentum"],
            delay_sec=1.0,
            rms_ipc="inproc://dummy_rms4",
            feed_ipc="inproc://dummy_feed4",
            hb_ipc="inproc://dummy_hb4",
            quotes_router_ipc="inproc://dummy_quotes4",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )
        track = superstructure.tracks["fast_momentum"]

        now = 1000.0
        req = OrderRequest(
            order_id="TEST_DELAY_CAP",
            mode="paper_delayed",
            track="fast_momentum",
            instrument_token="NSE:INFY",
            symbol="INFY",
            transaction_type="BUY",
            order_type="MARKET",
            quantity=100,
            price=10.0,
            trigger_price=10.0,
            timestamp=now,
        )
        superstructure.submit_order(req)
        self.assertEqual(len(superstructure._queue), 1)

        # Starve track of available cash
        track.allocated_capital = track.committed_capital + 10.0

        # Mock quotes_client returning higher quote price (15.0 vs 10.0 -> capital delta = 500 > 10.0)
        superstructure.quotes_client = type("MockClient", (), {
            "get_quote": lambda self, tok: {"ask": 15.0, "bid": 14.8}
        })()

        # Heartbeat release triggers rejection due to insufficient cash
        superstructure.on_heartbeat(now + 1.5)
        self.assertEqual(len(superstructure._queue), 0)

        superstructure.stop()

    def test_continue_state_resubscription(self):
        from core.common.time_utils import get_market_date_str
        from execution.superstructures.paper import PaperSuperstructure

        today_str = get_market_date_str()
        session_dir = os.path.join(self.test_dir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        journal_file = os.path.join(session_dir, "journal_paper_fast_momentum.jsonl")
        open_trade_event = {
            "timestamp": 1000.0,
            "event": "TRADE_FILLED",
            "data": {
                "trade_id": "RECOVER_TEST_1",
                "symbol": "TCS",
                "token": "NSE:TCS26NOV4000CE",
                "option_type": "CE",
                "strike": 4000.0,
                "lot_size": 175,
                "quantity": 175,
                "entry_price": 50.0,
                "entry_ask": 50.0,
                "entry_time": 1000.0,
                "target_price": 55.0,
                "stop_loss": 45.0,
                "status": "OPEN",
                "track": "fast_momentum",
            }
        }
        with open(journal_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(open_trade_event) + "\n")

        superstructure = PaperSuperstructure(
            track_names=["fast_momentum"],
            rms_ipc="inproc://dummy_rms5",
            feed_ipc="inproc://dummy_feed5",
            hb_ipc="inproc://dummy_hb5",
            quotes_router_ipc="inproc://dummy_quotes5",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
            continue_state=True,
        )

        track = superstructure.tracks["fast_momentum"]
        self.assertIn("RECOVER_TEST_1", track.active_trades)
        self.assertIn("NSE:TCS26NOV4000CE", superstructure._recovered_tokens)
        superstructure.stop()


if __name__ == "__main__":
    unittest.main()

