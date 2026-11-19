"""
Unit Tests for Web Analytics & Simulation UI.
Verifies Live Telemetry & MTM, Historical Review, and Simulation Lab API services.
"""
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from web.historical_service import HistoricalService
from web.live_service import LiveService
from web.server import AnalyticsWebServer
from web.simulation_service import SimulationService


class TestWebServices(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_web_rec_")
        self.session_date = "2026-11-19"
        self.session_dir = os.path.join(self.test_dir, self.session_date)
        os.makedirs(self.session_dir, exist_ok=True)

        # Create sample journal file with 1 closed trade and 1 open trade
        self.journal_path = os.path.join(self.session_dir, "journal_paper_fast_momentum_ATM.jsonl")
        self.closed_trade = {
            "event": "TRADE_EXIT",
            "mode": "paper",
            "timestamp": 1795061000.0,
            "data": {
                "trade_id": "TRD_TEST_001",
                "mode": "paper",
                "track": "fast_momentum_ATM",
                "symbol": "RELIANCE",
                "token": "NSE:12345",
                "option_type": "CE",
                "strike": 2500.0,
                "lot_size": 250,
                "quantity": 250,
                "entry_price": 50.0,
                "entry_time": 1795060800.0,
                "status": "CLOSED",
                "exit_price": 60.0,
                "exit_time": 1795061000.0,
                "exit_reason": "TARGET_HIT",
                "realized_pnl": 2500.0,
                "metadata": {"brokerage": 60.0, "net_pnl": 2440.0},
            },
        }
        self.open_trade = {
            "event": "TRADE_FILLED",
            "mode": "paper",
            "timestamp": 1795061100.0,
            "data": {
                "trade_id": "TRD_TEST_002",
                "mode": "paper",
                "track": "fast_momentum_ATM",
                "symbol": "TCS",
                "token": "NSE:67890",
                "option_type": "PE",
                "strike": 4000.0,
                "lot_size": 175,
                "quantity": 175,
                "entry_price": 80.0,
                "entry_time": 1795061100.0,
                "status": "OPEN",
                "current_bid": 85.0,
                "metadata": {"brokerage": 60.0},
            },
        }

        with open(self.journal_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(self.closed_trade) + "\n")
            f.write(json.dumps(self.open_trade) + "\n")

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_live_service_scanning_and_mtm(self):
        live_srv = LiveService(recordings_dir=self.test_dir)
        # Inject live quote into LVC for the open trade
        live_srv.lvc["NSE:67890"] = {
            "token": "NSE:67890",
            "symbol": "TCS",
            "ltp": 90.0,
            "bid": 90.0,
            "ask": 91.0,
            "timestamp": time.time(),
        }

        overview = live_srv.scan_live_journals(force=True)
        self.assertEqual(overview["date"], self.session_date)
        self.assertIn("paper", overview["modes"])

        track_stats = overview["modes"]["paper"]["tracks"]["fast_momentum_ATM"]
        self.assertEqual(track_stats["trades"], 2)
        self.assertEqual(track_stats["closed"], 1)
        self.assertEqual(track_stats["open"], 1)
        self.assertEqual(track_stats["wins"], 1)

        # MTM on open trade: (90 - 80) * 175 = 1750.0
        self.assertAlmostEqual(track_stats["mtm_pnl"], 1750.0, places=1)
        # Net PnL = (2500 - 60) realized + 1750 MTM - 30 (open fee) = 4160.0
        self.assertAlmostEqual(track_stats["net_pnl"], 4160.0, places=1)

        # Test track detail
        detail = live_srv.get_track_detail("paper", "fast_momentum_ATM")
        self.assertEqual(detail["track"], "fast_momentum_ATM")
        self.assertEqual(len(detail["trades"]), 2)
        self.assertTrue(len(detail["performance_curve"]) > 0)

    def test_historical_service_overview_and_curve(self):
        hist_srv = HistoricalService(recordings_dir=self.test_dir)
        dates = hist_srv.get_available_dates()
        self.assertEqual(len(dates), 1)
        self.assertEqual(dates[0]["date"], self.session_date)

        overview = hist_srv.get_historical_overview(self.session_date)
        self.assertEqual(overview["date"], self.session_date)
        self.assertIn("paper", overview["modes"])

        detail = hist_srv.get_historical_track_detail(self.session_date, "paper", "fast_momentum_ATM")
        self.assertEqual(detail["mode"], "paper")
        self.assertEqual(len(detail["trades"]), 2)
        self.assertTrue(len(detail["performance_curve"]) > 0)

    def test_simulation_service_config_and_custom_tracks(self):
        sim_srv = SimulationService(recordings_dir=self.test_dir)
        cfg = sim_srv.get_default_config()
        self.assertIn("base_tracks", cfg)
        self.assertIn("fast_momentum", cfg["base_tracks"])
        self.assertIn("trend_pullback", cfg["base_tracks"])
        self.assertIn("fixed_target", cfg["base_tracks"])

        # Test custom track payload builder
        params = {
            "global_settings": {"capital": 300000.0, "brokerage_per_order": 25.0},
            "base_tracks": {
                "fast_momentum": {"enabled": True, "target_pct": 0.20, "sl_pct": 0.08, "modes": ["ITM"]},
            },
            "custom_tracks": [
                {
                    "type": "base",
                    "name": "custom_pullback_ITM",
                    "archetype": "trend_pullback",
                    "mode": "ITM",
                    "target_pct": 0.30,
                    "sl_pct": 0.06,
                }
            ],
        }
        custom_strat = sim_srv._build_custom_strategy_cfg(params)
        momentum = custom_strat["strategies"]["momentum"]
        self.assertEqual(momentum["brokerage_per_order"], 25.0)
        self.assertEqual(len(momentum["enabled_tracks"]), 2)


class TestWebServerEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_web_server_")
        session_dir = os.path.join(self.test_dir, "2026-11-19")
        os.makedirs(session_dir, exist_ok=True)
        self.server_inst = AnalyticsWebServer(host="127.0.0.1", port=0, recordings_dir=self.test_dir)

    async def asyncTearDown(self):
        if hasattr(self, "test_dir") and os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    async def test_html_index_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/", app=self.server_inst.app)
        resp = await self.server_inst.handle_index(req)
        self.assertEqual(resp.status, 200)
        self.assertIn("ANTIGRAVITY", resp.text)
        self.assertIn("Live Telemetry", resp.text)
        self.assertIn("Simulation Lab", resp.text)

    async def test_api_status_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/api/status", app=self.server_inst.app)
        resp = await self.server_inst.handle_api_status(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["status"], "online")

    async def test_api_sim_config_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/api/sim/config", app=self.server_inst.app)
        resp = await self.server_inst.handle_sim_config(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertIn("base_tracks", data)
        self.assertIn("global_settings", data)


if __name__ == "__main__":
    unittest.main()
