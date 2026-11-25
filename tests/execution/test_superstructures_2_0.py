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
        self.assertEqual(track.committed_capital, 50.0 * 175)
        superstructure.stop()

    def test_continue_composite_track_and_delayed_recovery(self):
        from core.common.time_utils import get_market_date_str
        today_str = get_market_date_str()
        session_dir = os.path.join(self.test_dir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        # 1. CompositeTrack recovery: open trade + closed trade
        comp_journal = os.path.join(session_dir, "journal_paper_composite_shared_ITM.jsonl")
        closed_evt = {
            "timestamp": 1000.0,
            "event": "TRADE_EXIT",
            "data": {
                "trade_id": "COMP_CLOSED_1",
                "symbol": "INFY",
                "token": "NSE:INFY26NOV1500CE",
                "option_type": "CE",
                "strike": 1500.0,
                "lot_size": 400,
                "quantity": 400,
                "entry_price": 40.0,
                "entry_ask": 40.0,
                "entry_time": 900.0,
                "exit_price": 45.0,
                "exit_time": 1000.0,
                "exit_reason": "TARGET_HIT",
                "realized_pnl": 2000.0,
                "status": "CLOSED",
                "track": "composite_shared_ITM",
                "origin_track": "fast_momentum_ITM",
            },
        }
        open_evt = {
            "timestamp": 1100.0,
            "event": "TRADE_FILLED",
            "data": {
                "trade_id": "COMP_OPEN_1",
                "symbol": "TCS",
                "token": "NSE:TCS26NOV3500CE",
                "option_type": "CE",
                "strike": 3500.0,
                "lot_size": 175,
                "quantity": 175,
                "entry_price": 100.0,
                "entry_ask": 100.0,
                "entry_time": 1100.0,
                "target_price": 125.0,
                "stop_loss": 95.0,
                "status": "OPEN",
                "track": "composite_shared_ITM",
                "origin_track": "trend_pullback_ITM",
            },
        }
        with open(comp_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps(closed_evt) + "\n")
            f.write(json.dumps(open_evt) + "\n")

        paper_comp = PaperSuperstructure(
            track_names=["composite_shared_ITM"],
            rms_ipc="inproc://dummy_rms_comp",
            feed_ipc="inproc://dummy_feed_comp",
            hb_ipc="inproc://dummy_hb_comp",
            quotes_router_ipc="inproc://dummy_quotes_comp",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
            continue_state=True,
        )
        comp_track = paper_comp.tracks["composite_shared_ITM"]
        self.assertIn("COMP_OPEN_1", comp_track.active_trades)
        self.assertEqual(len(comp_track.closed_trades), 1)
        self.assertEqual(comp_track.committed_capital, 17500.0)
        self.assertEqual(comp_track.realized_pnl, 2000.0)

        # Simulate tick below -5% SL (bid = 90.4 <= 95.0) -> must exit via trend_pullback_ITM sub-track
        paper_comp.on_tick("NSE:TCS26NOV3500CE", ltp=90.5, bid=90.4, ask=90.8, ts=1200.0)
        self.assertEqual(len(comp_track.active_trades), 0)
        self.assertEqual(len(comp_track.closed_trades), 2)
        self.assertEqual(comp_track.committed_capital, 0.0)
        paper_comp.stop()

        # 2. DelayedSuperstructure recovery of unfilled TRADE_ENTRY_SUBMITTED
        del_journal = os.path.join(session_dir, "journal_paper_delayed_fast_momentum.jsonl")
        sub_evt = {
            "timestamp": 1000.0,
            "event": "TRADE_ENTRY_SUBMITTED",
            "data": {
                "trade_id": "DEL_UNFILLED_1",
                "symbol": "SBIN",
                "token": "NSE:SBIN26NOV800CE",
                "option_type": "CE",
                "strike": 800.0,
                "lot_size": 750,
                "quantity": 750,
                "entry_price": 20.0,
                "entry_ask": 20.0,
                "entry_time": 1000.0,
                "target_price": 23.0,
                "stop_loss": 19.0,
                "status": "OPEN",
                "track": "fast_momentum",
            },
        }
        with open(del_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps(sub_evt) + "\n")

        delayed = DelayedSuperstructure(
            track_names=["fast_momentum"],
            delay_sec=1.0,
            rms_ipc="inproc://dummy_rms_del_rec",
            feed_ipc="inproc://dummy_feed_del_rec",
            hb_ipc="inproc://dummy_hb_del_rec",
            quotes_router_ipc="inproc://dummy_quotes_del_rec",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
            continue_state=True,
        )
        self.assertEqual(len(delayed._queue), 1)
        self.assertIn("DEL_UNFILLED_1", delayed.tracks["fast_momentum"].active_trades)
        delayed.stop()


if __name__ == "__main__":
    unittest.main()

