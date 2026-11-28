import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_TEST_MODE", "1")

from aiohttp.test_utils import make_mocked_request
import zmq

from core.common.ipc_schemas import ActionableOpportunity, SetupAlert, unpack_message
from core.common.logging_utils import get_logger
from core.common.time_utils import IST, get_market_date_str
from core.ops.operations import is_core_rms_running, resolve_tracks_by_mode
from core.rms.rms_service import RMSService
from execution.superstructures.paper import PaperSuperstructure
from web.server import AnalyticsWebServer


class TestStandardLogging(unittest.TestCase):
    def test_ist_formatter_and_warn_normalization(self):
        tmpdir = tempfile.mkdtemp(prefix="log_fmt_test_")
        try:
            log_path = os.path.join(tmpdir, "sample_service.log")
            logger = get_logger("TEST_LOGGER_SVC", log_file=log_path)
            logger.info("Service initialized")
            logger.warning("Low disk warning")
            logger.error("Socket timeout error")

            with open(log_path, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.readlines() if ln.strip()]

            self.assertEqual(len(lines), 3)
            self.assertIn("IST] [INFO] [TEST_LOGGER_SVC] Service initialized", lines[0])
            self.assertIn("IST] [WARN] [TEST_LOGGER_SVC] Low disk warning", lines[1])
            self.assertIn("IST] [ERROR] [TEST_LOGGER_SVC] Socket timeout error", lines[2])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestOriginTrackDeterminismAndQuarantine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="origin_det_test_")
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.term()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_live_composite_records_origin_and_quarantines_ambiguous_recovery(self):
        today_str = get_market_date_str()
        session_dir = os.path.join(self.tmpdir, today_str)
        os.makedirs(session_dir, exist_ok=True)

        # Write one valid composite entry AND one ambiguous legacy entry lacking sub-track origin
        comp_journal = os.path.join(session_dir, "journal_paper_composite_shared_ITM.jsonl")
        valid_evt = {
            "timestamp": 1000.0,
            "event": "TRADE_FILLED",
            "data": {
                "trade_id": "TRD_VALID_1",
                "symbol": "INFY",
                "token": "NSE:INFY26NOV1500CE",
                "option_type": "CE",
                "strike": 1500.0,
                "lot_size": 400,
                "quantity": 400,
                "entry_price": 40.0,
                "entry_ask": 40.0,
                "entry_time": 1000.0,
                "target_price": 46.0,
                "stop_loss": 37.0,
                "status": "OPEN",
                "track": "composite_shared_ITM",
                "origin_track": "fast_momentum_ITM",
                "sub_track_rule": "FastMomentumTrack",
            },
        }
        ambiguous_evt = {
            "timestamp": 1050.0,
            "event": "TRADE_FILLED",
            "data": {
                "trade_id": "TRD_AMBIGUOUS_LEGACY",
                "symbol": "TCS",
                "token": "NSE:TCS26NOV3500CE",
                "option_type": "CE",
                "strike": 3500.0,
                "lot_size": 175,
                "quantity": 175,
                "entry_price": 95.0,
                "entry_ask": 95.0,
                "entry_time": 1050.0,
                "target_price": 115.0,
                "stop_loss": 88.0,
                "status": "OPEN",
                "track": "composite_shared_ITM",
                # missing origin_track
            },
        }
        with open(comp_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps(valid_evt) + "\n")
            f.write(json.dumps(ambiguous_evt) + "\n")

        paper = PaperSuperstructure(
            track_names=["composite_shared_ITM"],
            rms_ipc="inproc://dummy_rms_orig",
            feed_ipc="inproc://dummy_feed_orig",
            hb_ipc="inproc://dummy_hb_orig",
            quotes_router_ipc="inproc://dummy_quotes_orig",
            recordings_dir=self.tmpdir,
            ctx=self.ctx,
            continue_state=True,
        )
        try:
            comp_track = paper.tracks["composite_shared_ITM"]
            self.assertIn("TRD_VALID_1", comp_track.active_trades)
            self.assertEqual(comp_track.active_trades["TRD_VALID_1"].origin_track, "fast_momentum_ITM")
            self.assertEqual(comp_track.active_trades["TRD_VALID_1"].sub_track_rule, "FastMomentumTrack")
            # Ambiguous trade must NOT silently pollute sub_tracks[0]
            self.assertNotIn("TRD_AMBIGUOUS_LEGACY", comp_track.active_trades)
            quarantine_file = os.path.join(session_dir, "journal", "quarantined_legacy_trades.jsonl")
            self.assertTrue(os.path.exists(quarantine_file))
            with open(quarantine_file, "r", encoding="utf-8") as qf:
                q_lines = [json.loads(line) for line in qf if line.strip()]
            self.assertEqual(len(q_lines), 1)
            self.assertEqual(q_lines[0]["trade_id"], "TRD_AMBIGUOUS_LEGACY")
        finally:
            paper.stop()


class TestCoreRMSDecouplingAndRouting(unittest.TestCase):
    def setUp(self):
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.term()

    def test_strategy_id_topic_prefix_and_metadata_propagation(self):
        rms = RMSService(
            alerts_ipc="inproc://test_alerts_decoupled",
            rms_pub_ipc="inproc://test_rms_pub_decoupled",
            quotes_router_ipc="inproc://test_quotes_router_decoupled",
            ctx=self.ctx,
        )
        sub = self.ctx.socket(zmq.SUB)
        sub.connect("inproc://test_rms_pub_decoupled")
        sub.setsockopt(zmq.SUBSCRIBE, b"OPP.")

        try:
            # Mock strike resolution cleanly so no live QuotesBroker is required
            fake_strikes = {
                "ATM": {
                    "token": "NSE_FO|99999",
                    "symbol": "INFY26NOV1500CE",
                    "strike": 1500.0,
                    "option_type": "CE",
                    "ltp": 50.0,
                    "bid": 49.5,
                    "ask": 50.0,
                    "bid_qty": 100,
                    "ask_qty": 100,
                    "lot_size": 400,
                }
            }
            rms.resolve_strikes = MagicMock(return_value=fake_strikes)

            alert = SetupAlert(
                alert_id="ALT_TEST_1",
                symbol="INFY",
                direction="BULL",
                archetype="FLAG_BULL_BREAKOUT",
                trigger_price=1502.0,
                rvol=2.1,
                clv=0.85,
                atr=12.5,
                flag_high=1501.0,
                flag_low=1490.0,
                timestamp=1700000000.0,
                strategy_id="custom_strat_alpha",
                strategy_tags={"regime": "TREND_UP"},
            )
            opp = rms.process_alert(alert)
            self.assertIsNotNone(opp)
            self.assertEqual(opp.strategy_id, "custom_strat_alpha")
            self.assertEqual(opp.strategy_tags.get("regime"), "TREND_UP")

            frames = sub.recv_multipart()
            self.assertEqual(frames[0], b"OPP.custom_strat_alpha.INFY")
            payload = unpack_message(frames[-1])
            self.assertEqual(payload["strategy_id"], "custom_strat_alpha")
        finally:
            sub.close(linger=0)
            rms.stop()

    def test_core_rms_running_helper(self):
        tmpdir = tempfile.mkdtemp(prefix="core_rms_chk_")
        try:
            pids_path = os.path.join(tmpdir, "pids.json")
            self.assertFalse(is_core_rms_running(pids_path))
            with open(pids_path, "w", encoding="utf-8") as f:
                json.dump({"rms_service": {"pid": os.getpid(), "tier": "CORE"}}, f)
            self.assertTrue(is_core_rms_running(pids_path))
            with open(pids_path, "w", encoding="utf-8") as f:
                json.dump({"rms_service": {"pid": 99999999, "tier": "CORE"}}, f)
            self.assertFalse(is_core_rms_running(pids_path))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestAdminAndHealthAPIs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="admin_health_test_")
        self.logs_dir = os.path.join(self.tmpdir, "logs")
        self.rec_dir = os.path.join(self.tmpdir, "recordings")
        self.data_dir = os.path.join(self.tmpdir, "data")
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.rec_dir, exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        self.env_path = os.path.join(self.tmpdir, ".env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("UPSTOX_ACCESS_TOKEN=secret_upstox_token_1234\n")
            f.write("GROWW_ACCESS_TOKEN=secret_groww_token_9876\n")
            f.write("GROWW_API_KEY=key_abcd\n")

        self.pids_file = os.path.join(self.logs_dir, "pids.json")
        self.server = AnalyticsWebServer(
            recordings_dir=self.rec_dir,
            logs_dir=self.logs_dir,
            pids_file=self.pids_file,
        )
        self.server.admin_service.data_dir = self.data_dir
        self.server.admin_service.env_path = self.env_path

    def tearDown(self):
        self.server.teardown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_admin_config_masking_and_granular_strategy_launch(self):
        # 1. GET /api/admin/config masks secrets and returns steps/services/modes/tracks
        req_cfg = make_mocked_request("GET", "/api/admin/config", app=self.server.app)
        resp_cfg = await self.server.handle_admin_config(req_cfg)
        self.assertEqual(resp_cfg.status, 200)
        cfg = json.loads(resp_cfg.text)
        self.assertEqual(cfg["status"], "ok")
        self.assertGreaterEqual(len(cfg["premarket_steps"]), 5)
        self.assertGreaterEqual(len(cfg["core_services"]), 5)
        self.assertEqual(cfg["modes"], ["paper", "paper_delayed", "live"])
        track_names = [t["name"] for t in cfg["tracks"]]
        self.assertIn("fast_momentum_ATM", track_names)
        self.assertIn("structural_convexity_ATM", track_names)
        self.assertIn("composite_shared_ITM", track_names)
        # Bare config templates without moneyness must NOT be exposed as runnable tracks
        self.assertNotIn("fast_momentum", track_names)
        self.assertNotIn("gamma_runner", track_names)
        self.assertNotIn("composite", track_names)

        # Raw secret values must never appear anywhere in /api/admin/config
        self.assertNotIn("secret_upstox_token_1234", resp_cfg.text)
        self.assertNotIn("secret_groww_token_9876", resp_cfg.text)

        # 2. Simulate running Core RMS in pids.json
        with open(self.pids_file, "w", encoding="utf-8") as f:
            json.dump({"rms_service": {"pid": os.getpid(), "tier": "CORE", "log_file": os.path.join(self.logs_dir, "rms_service.log")}}, f)

        spawned_calls = []

        class _FakeProc:
            def __init__(self, pid):
                self.pid = pid

        def fake_spawn(cmd, role, log_file=None, env=None, tier=None, pids_file=None):
            fake_pid = 40000 + len(spawned_calls)
            spawned_calls.append((role, cmd, tier))
            return _FakeProc(fake_pid)

        # 3. Granular per-mode track launch (must attach to Core RMS & NOT spawn duplicate RMS!)
        with patch("web.admin_service.spawn_process", side_effect=fake_spawn):
            req_strat = make_mocked_request("POST", "/api/admin/strategy/start", app=self.server.app)
            req_strat._read_bytes = json.dumps({
                "modes": ["paper", "paper_delayed"],
                "tracks_by_mode": {
                    "paper": ["fast_momentum_ATM", "composite_shared_ITM"],
                    "paper_delayed": ["trend_pullback_ITM"],
                },
                "continue_state": True,
            }).encode("utf-8")
            resp_strat = await self.server.handle_admin_strategy_start(req_strat)
            self.assertEqual(resp_strat.status, 200)
            strat_data = json.loads(resp_strat.text)
            self.assertTrue(strat_data["core_rms_attached"])
            spawned_roles = [r for r, _, _ in spawned_calls]
            self.assertIn("scoring_engine", spawned_roles)
            self.assertNotIn("rms_service", spawned_roles)
            self.assertIn("exec_paper_fast_momentum_ATM", spawned_roles)
            self.assertIn("exec_paper_composite_shared_ITM", spawned_roles)
            self.assertIn("exec_paper_delayed_trend_pullback_ITM", spawned_roles)
            self.assertEqual(len(spawned_roles), 4)

            # 4. LIVE launch without typed CONFIRM LIVE must fail (400)
            req_live_fail = make_mocked_request("POST", "/api/admin/strategy/start", app=self.server.app)
            req_live_fail._read_bytes = json.dumps({
                "modes": ["live"],
                "tracks_by_mode": {"live": ["fast_momentum_ATM"]},
                "confirm_live": True,
                "confirm_live_phrase": "wrong",
            }).encode("utf-8")
            resp_live_fail = await self.server.handle_admin_strategy_start(req_live_fail)
            self.assertEqual(resp_live_fail.status, 400)

        # 5. Freeze / Unfreeze & Stop-Live safety + Audit log verification
        req_frz = make_mocked_request("POST", "/api/admin/action", app=self.server.app)
        req_frz._read_bytes = json.dumps({"action": "freeze", "params": {"mode": "paper"}}).encode("utf-8")
        resp_frz = await self.server.handle_admin_action(req_frz)
        self.assertEqual(resp_frz.status, 200)
        self.assertTrue(os.path.exists(os.path.join(self.logs_dir, ".freeze_paper")))

        req_unf = make_mocked_request("POST", "/api/admin/action", app=self.server.app)
        req_unf._read_bytes = json.dumps({"action": "unfreeze", "params": {"mode": "paper"}}).encode("utf-8")
        resp_unf = await self.server.handle_admin_action(req_unf)
        self.assertEqual(resp_unf.status, 200)
        self.assertFalse(os.path.exists(os.path.join(self.logs_dir, ".freeze_paper")))

        req_sl_bad = make_mocked_request("POST", "/api/admin/action", app=self.server.app)
        req_sl_bad._read_bytes = json.dumps({"action": "stop_live", "params": {"confirm_phrase": "no"}}).encode("utf-8")
        resp_sl_bad = await self.server.handle_admin_action(req_sl_bad)
        self.assertEqual(resp_sl_bad.status, 400)

        audit_rows = self.server.admin_service.get_recent_audit(limit=20)
        actions_logged = [a["action"] for a in audit_rows]
        self.assertIn("start_strategies", actions_logged)
        self.assertIn("freeze", actions_logged)
        self.assertIn("unfreeze", actions_logged)

    async def test_health_red_on_error_and_day_log_filtering(self):
        today_str = datetime_now_ist_date()
        with open(self.pids_file, "w", encoding="utf-8") as f:
            json.dump({
                "feed_handler": {"pid": os.getpid(), "tier": "CORE", "log_file": os.path.join(self.logs_dir, "feed_handler.log")}
            }, f)

        feed_log = os.path.join(self.logs_dir, "feed_handler.log")
        with open(feed_log, "w", encoding="utf-8") as f:
            # Yesterday error should NOT mark today RED
            f.write("[2020-01-01 10:00:00.000 IST] [ERROR] [FEED_HANDLER] Old historical error\n")
            f.write(f"[{today_str} 09:15:01.000 IST] [INFO] [FEED_HANDLER] WebSocket connected cleanly\n")

        # 1. Clean today logs -> GREEN
        req_h1 = make_mocked_request("GET", f"/api/health/status?date={today_str}", app=self.server.app)
        resp_h1 = await self.server.handle_health_status(req_h1)
        data_h1 = json.loads(resp_h1.text)
        self.assertEqual(data_h1["overall_status"], "GREEN")

        # 2. Add WARN today -> YELLOW
        with open(feed_log, "a", encoding="utf-8") as f:
            f.write(f"[{today_str} 09:30:00.000 IST] [WARN] [FEED_HANDLER] Latency spike detected\n")
        resp_h2 = await self.server.handle_health_status(req_h1)
        data_h2 = json.loads(resp_h2.text)
        self.assertEqual(data_h2["overall_status"], "YELLOW")

        # 3. Add ERROR today -> Immediate RED!
        with open(feed_log, "a", encoding="utf-8") as f:
            f.write(f"[{today_str} 09:45:00.123 IST] [ERROR] [FEED_HANDLER] Protobuf decode failure on key NSE_EQ|123\n")
        resp_h3 = await self.server.handle_health_status(req_h1)
        data_h3 = json.loads(resp_h3.text)
        self.assertEqual(data_h3["overall_status"], "RED")
        self.assertEqual(data_h3["summary"]["today_errors"], 1)
        self.assertEqual(len(data_h3["recent_errors"]), 1)

        # 4. Log Inspector filtering by date, level=ERROR, query
        req_logs = make_mocked_request(
            "GET",
            f"/api/health/logs?date={today_str}&file=feed_handler.log&level=ERROR&q=Protobuf",
            app=self.server.app,
        )
        resp_logs = await self.server.handle_health_logs(req_logs)
        data_logs = json.loads(resp_logs.text)
        self.assertEqual(len(data_logs["entries"]), 1)
        self.assertIn("Protobuf decode failure", data_logs["entries"][0]["message"])

        # 5. HTML exposes Admin & Health tabs, vertical sidebar, activity dropdown, nav badge, & Origin column
        req_idx = make_mocked_request("GET", "/", app=self.server.app)
        resp_idx = await self.server.handle_index(req_idx)
        self.assertEqual(resp_idx.status, 200)
        self.assertIn("sidebar-toggle-btn", resp_idx.text)
        self.assertIn("sidebar-nav", resp_idx.text)
        self.assertIn("admin-activity-select", resp_idx.text)
        self.assertIn("view-admin", resp_idx.text)
        self.assertIn("view-health", resp_idx.text)
        self.assertIn("nav-health-badge", resp_idx.text)
        self.assertIn("Origin / Rule", resp_idx.text)

        # 6. SHM mmap backing detection strictly gated by CandleManager liveness
        from web.health_service import HealthService
        shm_test_name = "test_health_probe_shm"
        shm_test_path = os.path.join("/tmp", f"{shm_test_name}.shm")
        try:
            with open(shm_test_path, "wb") as sf:
                sf.write(b"\x00" * 1024)
            hs = HealthService(
                logs_dir=self.logs_dir,
                pids_file=self.pids_file,
                ipc_paths=[],
                shm_name=shm_test_name,
                skip_system_probes=False,
            )
            # When CandleManager is inactive, stale SHM must be reported inactive and cleaned
            probe_offline = hs._probe_ipc_and_shm(candle_manager_alive=False)
            self.assertFalse(probe_offline["shm"]["active"])
            self.assertEqual(probe_offline["shm"]["backend"], "stale_cleared")

            # Recreate backing file and verify active status when CandleManager is alive
            with open(shm_test_path, "wb") as sf:
                sf.write(b"\x00" * 1024)
            probe_live = hs._probe_ipc_and_shm(candle_manager_alive=True)
            self.assertTrue(probe_live["shm"]["active"])
            self.assertEqual(probe_live["shm"]["backend"], "mmap_tmp")
        finally:
            if os.path.exists(shm_test_path):
                os.remove(shm_test_path)


def datetime_now_ist_date() -> str:
    from datetime import datetime
    return datetime.now(tz=IST).strftime("%Y-%m-%d")


if __name__ == "__main__":
    unittest.main()
