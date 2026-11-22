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
import polars as pl

from web.candle_service import CandleService
from web.historical_service import HistoricalService
from web.live_service import LiveService
from web.server import AnalyticsWebServer
from web.simulation_service import SimulationService
from web.alert_service import AlertService


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

        # Create sample equity and option parquet files for candle tests
        eq_df = pl.DataFrame({
            "token": ["NSE:RELIANCE", "NSE:RELIANCE"],
            "symbol": ["RELIANCE", "RELIANCE"],
            "ltp": [2500.0, 2520.0],
            "bid": [2499.0, 2519.0],
            "ask": [2501.0, 2521.0],
            "volume": [1000, 2000],
            "exchange_timestamp": [1795060800.0, 1795060830.0],
        })
        eq_df.write_parquet(os.path.join(self.session_dir, "equity_ticks.parquet"))

        opt_df = pl.DataFrame({
            "token": ["NSE_FO:54321", "NSE_FO:54321"],
            "symbol": ["RELIANCE26NOV2500CE", "RELIANCE26NOV2500CE"],
            "ltp": [50.0, 55.0],
            "bid": [49.5, 54.5],
            "ask": [50.5, 55.5],
            "volume": [100, 250],
            "exchange_timestamp": [1795060800.0, 1795060840.0],
        })
        opt_df.write_parquet(os.path.join(self.session_dir, "option_depth.parquet"))

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
        self.assertIn("start_budget", track_stats)
        self.assertIn("current_budget", track_stats)
        self.assertEqual(track_stats["start_budget"], 250000.0)
        # Available Cash = starting budget (250000) + realized net (2440) - open fee (30) - premium paid (90 * 175 = 15750) = 238410.0
        self.assertAlmostEqual(track_stats["current_budget"], 238410.0, places=1)
        self.assertAlmostEqual(track_stats["total_equity"], 254160.0, places=1)

        # Test track detail
        detail = live_srv.get_track_detail("paper", "fast_momentum_ATM")
        self.assertEqual(detail["track"], "fast_momentum_ATM")
        self.assertEqual(len(detail["trades"]), 2)
        self.assertTrue(len(detail["performance_curve"]) > 0)

        # Verify dual curve pnl options
        curve_pt = detail["performance_curve"][-1]
        self.assertIn("net_pnl", curve_pt)
        self.assertIn("gross_pnl", curve_pt)

        # Verify open trade quote timestamp info
        open_tr = [t for t in detail["trades"] if t["status"] == "OPEN"][0]
        self.assertNotEqual(open_tr["last_quote_time_str"], "-")
        self.assertIsNotNone(open_tr["last_quote_age_sec"])

    def test_candle_service_aggregation(self):
        candle_srv = CandleService(recordings_dir=self.test_dir)
        candles = candle_srv.get_candles(
            target_date=self.session_date,
            token="NSE:RELIANCE",
            symbol="RELIANCE",
            is_option=False,
        )
        self.assertEqual(len(candles), 1)
        c = candles[0]
        self.assertEqual(c["open"], 2500.0)
        self.assertEqual(c["high"], 2520.0)
        self.assertEqual(c["low"], 2500.0)
        self.assertEqual(c["close"], 2520.0)
        self.assertEqual(c["volume"], 1000)

        # Verify option candles
        opt_candles = candle_srv.get_candles(
            target_date=self.session_date,
            token="NSE_FO:54321",
            symbol="RELIANCE26NOV2500CE",
            is_option=True,
        )
        self.assertEqual(len(opt_candles), 1)
        oc = opt_candles[0]
        self.assertEqual(oc["open"], 50.0)
        self.assertEqual(oc["high"], 55.0)
        self.assertEqual(oc["close"], 55.0)

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

    def test_param_hash_deduplication(self):
        sim_srv = SimulationService(recordings_dir=self.test_dir)
        p1 = {"global_settings": {"capital": 250000.0}, "base_tracks": {"fast_momentum": {"target_pct": 0.15}}}
        p2 = {"global_settings": {"capital": 250000.0}, "base_tracks": {"fast_momentum": {"target_pct": 0.15}}}
        p3 = {"global_settings": {"capital": 500000.0}, "base_tracks": {"fast_momentum": {"target_pct": 0.15}}}

        h1 = sim_srv.compute_param_hash("2026-11-19", p1)
        h2 = sim_srv.compute_param_hash("2026-11-19", p2)
        h3 = sim_srv.compute_param_hash("2026-11-19", p3)

        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)

    def test_alert_service_and_caching(self):
        logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        alert_file = os.path.join(logs_dir, "alerts_2026-11-19.jsonl")

        sample_alert = {
            "alert_id": "ALT_123456",
            "symbol": "RELIANCE",
            "direction": "BULL",
            "archetype": "FLAG_BULL_BREAKOUT",
            "trigger_price": 2505.50,
            "rvol": 3.45,
            "clv": 0.82,
            "atr": 15.20,
            "timestamp": 1795060800.0,
        }
        with open(alert_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(sample_alert) + "\n")

        svc = AlertService(recordings_dir=self.test_dir, logs_dir=logs_dir)
        res = svc.get_alerts("2026-11-19")
        self.assertEqual(res["total_count"], 1)
        self.assertEqual(len(res["latest_5"]), 1)
        self.assertEqual(res["latest_5"][0]["symbol"], "RELIANCE")
        self.assertEqual(res["latest_5"][0]["direction"], "BULL")

        # Test candle retrieval with trigger marker
        c_res = svc.get_alert_candles("RELIANCE", "2026-11-19", trigger_price=2505.50, trigger_ts=1795060800.0)
        self.assertEqual(c_res["symbol"], "RELIANCE")
        self.assertEqual(len(c_res["markers"]), 1)
        self.assertEqual(c_res["markers"][0]["type"], "TRIGGER")

    def test_simulation_service_cleanup(self):
        """Verify that simulation service teardown removes all history, caches, and temp SHM."""
        sim_svc = SimulationService(recordings_dir=self.test_dir)
        sim_svc.runs_history["RUN-1"] = {"id": "RUN-1", "results": {"test": 123}}
        sim_svc.cache_by_param_hash["abc"] = {"cached": True}
        sim_svc.cached_date = "2026-11-19"
        sim_svc.cached_equity_df = pl.DataFrame({"a": [1]})
        sim_svc.cached_options_df = pl.DataFrame({"b": [2]})
        sim_svc.is_running = True
        sim_svc.progress_status = "Running"
        sim_svc.latest_results = {"test": 1}

        # Invoke teardown cleanup
        sim_svc.cleanup()

        self.assertFalse(sim_svc.is_running)
        self.assertEqual(sim_svc.progress_status, "Idle")
        self.assertEqual(len(sim_svc.runs_history), 0)
        self.assertEqual(len(sim_svc.cache_by_param_hash), 0)
        self.assertIsNone(sim_svc.cached_equity_df)
        self.assertIsNone(sim_svc.cached_options_df)
        self.assertIsNone(sim_svc.cached_date)
        self.assertIsNone(sim_svc.latest_results)

    def test_simulation_trade_formatting_and_candle_precomputation(self):
        """Verify candle precomputation with candle_svc and safe metadata handling."""
        from unittest.mock import MagicMock
        sim_svc = SimulationService(recordings_dir=self.test_dir)
        sim_svc.cached_date = "2026-11-19"
        sim_svc.cached_equity_df = pl.DataFrame({
            "token": ["NSE:RELIANCE"],
            "symbol": ["RELIANCE"],
            "exchange_timestamp": [1795060800.0],
            "ltp": [2500.0],
            "volume": [100],
        })

        # Mock engine with trades having metadata, None metadata, and missing metadata
        mock_trades = [
            {
                "trade_id": "T1",
                "track": "fast_momentum",
                "symbol": "RELIANCE",
                "token": "NSE_FO:54321",
                "entry_price": 50.0,
                "exit_price": 60.0,
                "entry_time": 1795060800.0,
                "exit_time": 1795061000.0,
                "realized_pnl": 2500.0,
                "metadata": {"underlying_token": "NSE:RELIANCE", "option_symbol": "RELIANCE26NOV2500CE", "brokerage": 60.0},
            },
            {
                "trade_id": "T2",
                "track": "fast_momentum",
                "symbol": "RELIANCE",
                "token": "NSE_FO:54321",
                "entry_price": 50.0,
                "exit_price": 45.0,
                "entry_time": 1795061100.0,
                "exit_time": 1795061200.0,
                "realized_pnl": -1250.0,
                "metadata": None,  # Explicitly None to test safety
            },
        ]

        # Test curve builder
        curve = sim_svc._build_simulation_curve(mock_trades)
        self.assertEqual(len(curve), 3)  # 1 initial baseline + 2 trade points
        self.assertEqual(curve[-1]["gross_pnl"], 1250.0)

        # Test precomputation and trade formatting block directly
        from web.candle_service import CandleService
        candle_svc = CandleService(recordings_dir=self.test_dir)
        precomputed = candle_svc.get_candles(
            target_date="2026-11-19",
            token="NSE:RELIANCE",
            symbol="RELIANCE",
            is_option=False,
            interval_minutes=5,
            source_df=sim_svc.cached_equity_df,
        )
        self.assertIsInstance(precomputed, list)


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
        self.assertIn("Live Alerts", resp.text)
        self.assertIn("view-alerts", resp.text)
        self.assertIn("alert-chart-modal", resp.text)
        self.assertIn("Simulation Lab", resp.text)
        self.assertIn("sim-type-batch-btn", resp.text)
        self.assertIn("sim-runs-list", resp.text)
        self.assertIn("view-sim-results", resp.text)
        self.assertIn("nav-tab-sim-results", resp.text)
        self.assertIn("sr-run-select", resp.text)
        self.assertIn("sr-params-body", resp.text)
        self.assertIn("sr-matrix-tbody", resp.text)

    async def test_sim_results_standalone_page_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/sim/results?run_id=RUN-1", app=self.server_inst.app)
        resp = await self.server_inst.handle_sim_results_page(req)
        self.assertEqual(resp.status, 200)
        self.assertIn("Simulation Report", resp.text)
        self.assertIn("srp-matrix-tbody", resp.text)
        self.assertIn("srp-params-body", resp.text)

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

    async def test_api_chart_candles_route(self):
        from aiohttp.test_utils import make_mocked_request
        # Test endpoint with interval returns 5m candles gracefully
        req = make_mocked_request(
            "GET",
            "/api/chart/candles?date=2026-11-19&symbol=RELIANCE&is_option=0&interval=5",
            app=self.server_inst.app,
        )
        resp = await self.server_inst.handle_chart_candles(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["symbol"], "RELIANCE")
        self.assertEqual(data["interval"], 5)
        self.assertIn("candles", data)
        self.assertIsInstance(data["candles"], list)

    async def test_api_alerts_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/api/alerts?date=2026-11-19", app=self.server_inst.app)
        resp = await self.server_inst.handle_alerts(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertIn("latest_5", data)
        self.assertIn("alerts", data)
        self.assertIn("total_count", data)

    async def test_api_alert_candles_route(self):
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request(
            "GET",
            "/api/alerts/candles?symbol=RELIANCE&date=2026-11-19&trigger_price=2500&trigger_ts=1795060800",
            app=self.server_inst.app,
        )
        resp = await self.server_inst.handle_alert_candles(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.text)
        self.assertEqual(data["symbol"], "RELIANCE")
        self.assertIn("candles", data)
        self.assertIn("markers", data)
        self.assertEqual(len(data["markers"]), 1)
        self.assertEqual(data["markers"][0]["type"], "TRIGGER")

    async def test_api_sim_clear_and_boot_id_route(self):
        from aiohttp.test_utils import make_mocked_request
        # 1. Verify boot id is present in sim status and sim runs
        req_st = make_mocked_request("GET", "/api/sim/status", app=self.server_inst.app)
        resp_st = await self.server_inst.handle_sim_status(req_st)
        self.assertEqual(resp_st.status, 200)
        data_st = json.loads(resp_st.text)
        self.assertIn("server_boot_id", data_st)
        self.assertEqual(data_st["server_boot_id"], self.server_inst.server_boot_id)

        # 2. Add fake run and verify clearing
        self.server_inst.simulation_service.runs_history["RUN-TEST"] = {
            "id": "RUN-TEST",
            "date": "2026-11-19",
            "runtime": 1.2,
        }
        req_runs = make_mocked_request("GET", "/api/sim/runs", app=self.server_inst.app)
        resp_runs = await self.server_inst.handle_sim_runs_list(req_runs)
        self.assertEqual(resp_runs.status, 200)
        data_runs = json.loads(resp_runs.text)
        self.assertEqual(len(data_runs["runs"]), 1)
        self.assertEqual(data_runs["server_boot_id"], self.server_inst.server_boot_id)

        # 3. Call POST /api/sim/clear
        req_clear = make_mocked_request("POST", "/api/sim/clear", app=self.server_inst.app)
        resp_clear = await self.server_inst.handle_sim_clear(req_clear)
        self.assertEqual(resp_clear.status, 200)
        data_clear = json.loads(resp_clear.text)
        self.assertEqual(data_clear["status"], "cleared")
        self.assertEqual(len(self.server_inst.simulation_service.runs_history), 0)

    async def test_api_sim_run_lifecycle_and_concurrency(self):
        """Verify that starting a simulation from the web endpoint does not trigger self-lockout,
        safely reports concurrency, and recovers is_running on completion."""
        from aiohttp.test_utils import make_mocked_request
        from unittest.mock import patch

        sim_svc = self.server_inst.simulation_service

        # Pre-populate deduplication cache so worker process is mocked/cached instantly
        p = {"date": "2026-11-19"}
        param_hash = sim_svc.compute_param_hash("2026-11-19", p)
        sim_svc.cache_by_param_hash[param_hash] = {
            "matrix": {"paper": {"trades": 5, "pnl": 1200.0}},
            "details": {},
        }

        # 1. Start simulation via POST /api/sim/run
        req1 = make_mocked_request("POST", "/api/sim/run", app=self.server_inst.app)
        req1._read_bytes = json.dumps({"date": "2026-11-19", "run_id": "RUN-1"}).encode("utf-8")
        resp1 = await self.server_inst.handle_sim_run(req1)
        self.assertEqual(resp1.status, 200)
        data1 = json.loads(resp1.text)
        self.assertEqual(data1["status"], "started")
        self.assertEqual(data1["run_id"], "RUN-1")

        # 2. Wait for background task to complete and assert no exceptions occurred
        if sim_svc._current_task:
            await sim_svc._current_task
        self.assertFalse(sim_svc.is_running)
        self.assertIsNone(sim_svc.latest_error)

        # 3. Test concurrency guard: when is_running is True, second request returns 409
        sim_svc.is_running = True
        req2 = make_mocked_request("POST", "/api/sim/run", app=self.server_inst.app)
        req2._read_bytes = json.dumps({"date": "2026-11-19", "run_id": "RUN-2"}).encode("utf-8")
        resp2 = await self.server_inst.handle_sim_run(req2)
        self.assertEqual(resp2.status, 409)
        data2 = json.loads(resp2.text)
        self.assertIn("error", data2)

        # Reset is_running and verify clean next start
        sim_svc.is_running = False
        req3 = make_mocked_request("POST", "/api/sim/run", app=self.server_inst.app)
        req3._read_bytes = json.dumps({"date": "2026-11-19", "run_id": "RUN-3"}).encode("utf-8")
        resp3 = await self.server_inst.handle_sim_run(req3)
        self.assertEqual(resp3.status, 200)
        if sim_svc._current_task:
            await sim_svc._current_task
        self.assertFalse(sim_svc.is_running)

    async def test_server_teardown_lifecycle(self):
        # Verify teardown cleans up simulation and live services safely
        self.server_inst.teardown()
        self.assertFalse(self.server_inst.simulation_service.is_running)


if __name__ == "__main__":
    unittest.main()
