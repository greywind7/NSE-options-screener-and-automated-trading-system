"""
Unit Tests for Universal Multi-Mode Terminal Dashboard.
Verifies alert telemetry, available strike depth, and mode-separated multi-track performance matrices.
"""
from datetime import datetime, timezone
import json
import os
import shutil
import tempfile
import time
import unittest
import zmq

from core.common.ipc_schemas import SetupAlert, ActionableOpportunity
from ui.dashboard import TerminalDashboard


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_dashboard_")
        self.ctx = zmq.Context()
        self.dashboard = TerminalDashboard(
            alerts_ipc="inproc://dummy_dash_alerts",
            rms_ipc="inproc://dummy_dash_rms",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )

    def tearDown(self):
        self.dashboard.stop()
        self.ctx.destroy(linger=0)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_alert_and_strike_rendering(self):
        now = time.time()
        # Set latest alert
        self.dashboard.latest_alert = {
            "symbol": "RELIANCE",
            "archetype": "FLAG_BULL_BREAKOUT",
            "direction": "BULL",
            "trigger_price": 2500.0,
            "rvol": 3.2,
            "clv": 0.88,
            "timestamp": now - 15.0,  # 15 seconds ago
        }

        # Set latest opportunity strikes
        self.dashboard.latest_opp = {
            "symbol": "RELIANCE",
            "strikes": {
                "ATM": {
                    "token": "NSE:RELIANCE26NOV2500CE",
                    "strike": 2500.0,
                    "bid": 20.0,
                    "ask": 20.20,
                    "lot_size": 250,
                },
                "ITM1": {
                    "token": "NSE:RELIANCE26NOV2400CE",
                    "strike": 2400.0,
                    "bid": 85.0,
                    "ask": 85.50,
                    "lot_size": 250,
                },
            },
        }

        rendered = self.dashboard.render(clear=False)
        self.assertIn("RELIANCE", rendered)
        self.assertIn("FLAG_BULL_BREAKOUT", rendered)
        self.assertIn("15s ago", rendered)
        self.assertIn("NSE:RELIANCE26NOV2500CE", rendered)
        self.assertIn("20.20", rendered)
        self.assertIn("[ PAPER EXECUTION MATRIX ]", rendered)
        self.assertIn("[ PAPER-DELAYED EXECUTION MATRIX ]", rendered)
        self.assertIn("[ LIVE BROKER MATRIX ]", rendered)

    def test_journal_scan_and_mode_matrices(self):
        # Create today's session journal directory
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_dir = os.path.join(self.test_dir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        # Write sample journal for paper fast_momentum_ATM
        paper_journal = os.path.join(session_dir, "journal_paper_fast_momentum_ATM.jsonl")
        with open(paper_journal, "w", encoding="utf-8") as f:
            # 1 winning trade (+₹1,500)
            f.write(json.dumps({
                "event": "TRADE_EXIT",
                "mode": "paper",
                "data": {"track": "fast_momentum_ATM", "status": "CLOSED", "realized_pnl": 1500.0}
            }) + "\n")
            # 1 losing trade (-₹500)
            f.write(json.dumps({
                "event": "TRADE_EXIT",
                "mode": "paper",
                "data": {"track": "fast_momentum_ATM", "status": "CLOSED", "realized_pnl": -500.0}
            }) + "\n")

        # Write sample journal for paper_delayed fixntrend
        delayed_journal = os.path.join(session_dir, "journal_paper_delayed_fixntrend.jsonl")
        with open(delayed_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "TRADE_EXIT",
                "mode": "paper_delayed",
                "data": {"track": "fixntrend", "status": "CLOSED", "realized_pnl": 3000.0}
            }) + "\n")

        # Write stale legacy journal that SHOULD be filtered out
        stale_journal = os.path.join(session_dir, "journal_paper_trend_pullback.jsonl")
        with open(stale_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "TRADE_EXIT",
                "mode": "paper",
                "data": {"track": "trend_pullback", "status": "CLOSED", "realized_pnl": 9999.0}
            }) + "\n")

        self.dashboard.scan_journals()
        rendered = self.dashboard.render(clear=False)

        # Verify paper matrix shows fast_momentum_ATM metrics
        self.assertIn("fast_momentum_ATM", rendered)
        self.assertIn("₹+1,000.00" if "1,000" in rendered else "1000", rendered)

        # Verify delayed matrix shows fixntrend metrics
        self.assertIn("fixntrend", rendered)
        self.assertTrue("3,000" in rendered or "3000" in rendered)

        # Verify stale track is completely ignored
        self.assertNotIn("trend_pullback", rendered)

    def test_custom_track_filtering(self):
        dash = TerminalDashboard(
            alerts_ipc="inproc://dummy_dash_alerts",
            rms_ipc="inproc://dummy_dash_rms",
            recordings_dir=self.test_dir,
            tracks=["custom_track_A", "custom_track_B"],
            ctx=self.ctx,
        )
        self.assertEqual(dash.tracks, ["custom_track_A", "custom_track_B"])
        rendered = dash.render(clear=False)
        self.assertIn("custom_track_A", rendered)
        self.assertIn("custom_track_B", rendered)
        self.assertNotIn("fast_momentum_ATM", rendered)
        dash.stop()

    def test_mode_filtering(self):
        # Create dashboard with only paper and paper_delayed
        dash = TerminalDashboard(
            alerts_ipc="inproc://dummy_dash_alerts",
            rms_ipc="inproc://dummy_dash_rms",
            recordings_dir=self.test_dir,
            modes=["paper", "paper_delayed"],
            ctx=self.ctx,
        )
        rendered = dash.render(clear=False)
        self.assertIn("[ PAPER EXECUTION MATRIX ]", rendered)
        self.assertIn("[ PAPER-DELAYED EXECUTION MATRIX ]", rendered)
        self.assertNotIn("[ LIVE BROKER MATRIX ]", rendered)
        dash.stop()


if __name__ == "__main__":
    unittest.main()

