"""
Tests for FeedHandler WebSocket half-open socket resilience, startup auth retry,
ProtectedPositionRegistry reconnect refresh, unbuffered Health Tab logging,
and cross-system config single-source-of-truth propagation.
"""
import asyncio
from datetime import datetime
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch
import polars as pl
import zmq

from core.common.logging_utils import get_logger
from core.common.time_utils import IST
from core.config.config_loader import get_config_manager
from core.feed.feed_handler import FeedHandler
from core.feed.position_registry import ProtectedPositionRegistry
from core.feed.proto import MarketDataFeedV3_pb2 as pb
from execution.superstructures.paper import PaperSuperstructure
from execution.superstructures.delayed import DelayedSuperstructure
from execution.superstructures.live import LiveSuperstructure
from simulation.columnar_sim_engine import ColumnarSimEngine, SimExecutionContext
from common.tracks.factory import create_tracks
from web.health_service import HealthService
from web.historical_service import HistoricalService
from web.sim_disk_cache import resolve_track_cache_specs
from web.simulation_service import SimulationService


class _FakeWebSocket:
    """Minimal async WebSocket context manager for deterministic timeout/reconnect tests."""

    def __init__(self, handler, recv_actions):
        self._handler = handler
        self._recv_actions = list(recv_actions)
        self.sent_messages = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    async def send(self, data):
        self.sent_messages.append(json.loads(data.decode("utf-8")))

    async def recv(self):
        if not self._recv_actions:
            self._handler._running = False
            raise ConnectionResetError("mock_ws_eof")
        action = self._recv_actions.pop(0)
        return await action(self._handler)


class TestFeedHandlerResilience(unittest.TestCase):
    def setUp(self):
        self.ctx = zmq.Context()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.reg_path = os.path.join(self.temp_dir.name, "active_pos.json")
        self.status_path = os.path.join(self.temp_dir.name, "feed_status.json")
        self.eq_ipc = "inproc://test_resil_eq"
        self.opt_ipc = "inproc://test_resil_opt"
        self.feed = FeedHandler(
            mode="ws",
            feed_equity_ipc=self.eq_ipc,
            feed_options_ipc=self.opt_ipc,
            silent=True,
            ctx=self.ctx,
            ws_ping_interval=20.0,
            ws_ping_timeout=10.0,
            ws_recv_timeout_market=0.05,
            ws_recv_timeout_offhours=0.15,
            feed_status_file=self.status_path,
        )
        self.feed.position_registry = ProtectedPositionRegistry(registry_file=self.reg_path)
        self.feed.position_registry.clear()

    def tearDown(self):
        self.feed.stop()
        self.ctx.term()
        self.temp_dir.cleanup()

    def test_market_hours_watchdog_timeout_selection(self):
        """Watchdog uses tight market-hours timeout inside NSE session and relaxed off-hours timeout outside."""
        mkt_dt = datetime(2026, 11, 27, 10, 30, 0, tzinfo=IST)
        off_dt = datetime(2026, 11, 27, 20, 15, 0, tzinfo=IST)
        self.assertAlmostEqual(self.feed._get_ws_recv_watchdog_timeout(mkt_dt), 0.05)
        self.assertAlmostEqual(self.feed._get_ws_recv_watchdog_timeout(off_dt), 0.15)

        cfg = get_config_manager()
        feed_cfg = cfg.system.get("system", {}).get("feed", {})
        self.assertEqual(float(feed_cfg.get("ws_ping_interval_sec", 0)), 20.0)
        self.assertEqual(float(feed_cfg.get("ws_ping_timeout_sec", 0)), 10.0)

    def test_half_open_timeout_startup_retry_and_protected_refresh(self):
        """
        Verify:
        1. Startup auth transient failures retry inside ws_connection_worker without fatal exit.
        2. websockets.connect receives configured ping_interval=20, ping_timeout=10.
        3. Half-open socket triggers asyncio.TimeoutError watchdog -> closes & reconnects.
        4. New ProtectedPositionRegistry keys added mid-outage are subscribed on reconnect.
        5. Incoming tick clears stale state and emits unbuffered WARN + recovery INFO logs.
        """
        log_path = os.path.join(self.temp_dir.name, "exec_logs", "feed_handler.log")
        test_logger = get_logger("FEED_HANDLER", log_file=log_path)

        # Build valid protobuf equity tick frame
        resp = pb.FeedResponse()
        resp.feeds["NSE_EQ|INE002A01018"].ltpc.ltp = 2510.5
        tick_bytes = resp.SerializeToString()

        auth_calls = {"n": 0}

        def mock_fetch_url():
            auth_calls["n"] += 1
            if auth_calls["n"] <= 2:
                return None
            return f"wss://mock.upstox.test/v3/{auth_calls['n']}"

        async def hang_then_protect(handler):
            # Register an open option position WHILE socket is disconnected/hung
            handler.position_registry.register_open_position(
                contract_key="NSE_FO|RELIANCE26NOV2500CE",
                symbol="RELIANCE",
                contract_symbol="RELIANCE 2500 CE",
                mode="paper",
                track="fast_momentum",
                trade_id="T_RECONN_1",
                quantity=250,
                entry_price=42.0,
            )
            await asyncio.sleep(0.25)  # exceeds ws_recv_timeout=0.05 -> watchdog triggers!
            return b""

        async def yield_tick_and_stop(handler):
            # Yield one valid tick then terminate loop cleanly
            async def stop_soon():
                await asyncio.sleep(0.02)
                handler._running = False

            asyncio.create_task(stop_soon())
            return tick_bytes

        ws_instances = []
        connect_kwargs = []

        def fake_connect(ws_url, **kwargs):
            connect_kwargs.append(kwargs)
            idx = len(ws_instances)
            actions = [hang_then_protect] if idx == 0 else [yield_tick_and_stop]
            ws = _FakeWebSocket(self.feed, actions)
            ws_instances.append(ws)
            return ws

        self.feed.connections[0].underlying_keys.add("NSE_EQ|INE002A01018")
        self.feed.connections[0].subscribed_keys.add("NSE_EQ|INE002A01018")
        self.feed.symbol_map["NSE_EQ|INE002A01018"] = "RELIANCE"
        self.feed.underlying_key_to_symbol["NSE_EQ|INE002A01018"] = "RELIANCE"
        self.feed.symbol_to_conn["RELIANCE"] = 0
        self.feed._register_key_to_conn("NSE_EQ|INE002A01018", 0)
        self.feed._running = True

        # Make backoff sleep near-instant during unit test while still keeping timeout wait_for at 0.05s
        orig_sleep = asyncio.sleep

        async def fast_backoff_sleep(delay, *args, **kwargs):
            await orig_sleep(0.005 if delay >= 0.9 else delay)

        with patch.object(self.feed, "_fetch_authorized_ws_url", side_effect=mock_fetch_url), \
             patch.object(self.feed, "_get_ws_recv_watchdog_timeout", return_value=0.05), \
             patch("core.feed.feed_handler.websockets.connect", side_effect=fake_connect), \
             patch("core.feed.feed_handler.asyncio.sleep", side_effect=fast_backoff_sleep):
            asyncio.run(self.feed._ws_connection_worker(0))

        self.assertGreaterEqual(auth_calls["n"], 4)
        self.assertEqual(len(connect_kwargs), 2)
        self.assertEqual(connect_kwargs[0]["ping_interval"], 20.0)
        self.assertEqual(connect_kwargs[0]["ping_timeout"], 10.0)

        conn0 = self.feed.connections[0]
        self.assertEqual(conn0.timeout_count, 1)
        self.assertEqual(conn0.reconnect_count, 1)
        self.assertFalse(conn0.is_stale)
        self.assertGreaterEqual(conn0.total_ticks_received, 1)

        # Second WS connection must have refreshed ProtectedPositionRegistry and subscribed the new contract
        reconn_sub = ws_instances[1].sent_messages[0]["data"]["instrumentKeys"]
        self.assertIn("NSE_FO|RELIANCE26NOV2500CE", reconn_sub)

        # HealthService should immediately see today's WARN timeout logs without manual flush
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        hs = HealthService(logs_dir=self.temp_dir.name, skip_system_probes=True)
        status = hs.get_status(target_date=today_str)
        self.assertEqual(status["overall_status"], "YELLOW")
        self.assertGreaterEqual(status["summary"]["today_warns"], 2)  # 2 auth retries + 1 watchdog timeout

        # Clean up rotating file handler we added to FEED_HANDLER logger for test isolation
        for h in list(test_logger.handlers):
            if isinstance(h, logging.FileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == os.path.abspath(log_path):
                h.close()
                test_logger.removeHandler(h)

    def test_rest_fallback_triggered_after_repeated_auth_failures(self):
        """When allow_rest_fallback=True and WS auth repeatedly fails, failover activates cleanly."""
        self.feed.allow_rest_fallback = True
        self.feed.max_auth_retries_before_fallback = 2
        self.feed._running = True
        poll_called = {"flag": False}

        def fake_poll():
            poll_called["flag"] = True
            self.feed._running = False

        orig_sleep = asyncio.sleep

        async def fast_sleep(_delay, *args, **kwargs):
            await orig_sleep(0.002)

        with patch.object(self.feed, "_fetch_authorized_ws_url", return_value=None), \
             patch.object(self.feed, "_run_poll_stream", side_effect=fake_poll), \
             patch("core.feed.feed_handler.asyncio.sleep", side_effect=fast_sleep):
            self.feed._run_websocket_stream()

        self.assertTrue(poll_called["flag"])


class TestConfigPropagationConsistency(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.term()
        self.temp_dir.cleanup()

    def test_superstructures_and_tracks_respect_capital_override(self):
        """Paper, Delayed, Live superstructures & create_tracks must honor explicit starting_capital."""
        custom_cap = 175000.0
        for cls, kw in (
            (PaperSuperstructure, {"starting_capital": custom_cap}),
            (DelayedSuperstructure, {"starting_capital": custom_cap}),
            (LiveSuperstructure, {"initial_capital": custom_cap, "enable_polling": False}),
        ):
            s = cls(
                track_names=["fast_momentum", "composite_shared_ATM"],
                rms_ipc=f"inproc://rms_{cls.__name__}",
                feed_ipc=f"inproc://feed_{cls.__name__}",
                quotes_stream_ipc=f"inproc://qstream_{cls.__name__}",
                quotes_router_ipc=f"inproc://qrouter_{cls.__name__}",
                hb_ipc=f"inproc://hb_{cls.__name__}",
                recordings_dir=self.temp_dir.name,
                ctx=self.ctx,
                **kw,
            )
            try:
                self.assertAlmostEqual(s.risk_mgr.starting_capital, custom_cap)
                self.assertAlmostEqual(s.tracks["fast_momentum"].allocated_capital, custom_cap)
                self.assertAlmostEqual(s.tracks["composite_shared_ATM"].total_capital, custom_cap)
                if hasattr(s, "initial_capital"):
                    self.assertAlmostEqual(s.initial_capital, custom_cap)
            finally:
                s.stop()

        # create_tracks directly via SimExecutionContext also inherits context capital
        sim_ctx = SimExecutionContext(mode="paper", starting_capital=140000.0)
        tracks = create_tracks(sim_ctx, track_filter=["fast_momentum_ATM", "composite_shared_ATM"])
        self.assertAlmostEqual(tracks["fast_momentum_ATM"].allocated_capital, 140000.0)
        self.assertAlmostEqual(tracks["composite_shared_ATM"].total_capital, 140000.0)

    def test_direct_option_sim_and_zero_overrides_respected(self):
        """ColumnarSimEngine direct option fallback & track init must honor custom capital and 0.0 brokerage."""
        engine = ColumnarSimEngine(
            data_path=self.temp_dir.name,
            tracks=["fast_momentum_ATM", "adaptive_regime_ratchet_ATM"],
            modes=["paper", "paper_delayed"],
            capital=190000.0,
            brokerage_per_order=0.0,
            min_delay_ms=0.0,
            max_delay_ms=0.0,
        )
        self.assertEqual(engine.brokerage_per_order, 0.0)
        self.assertEqual(engine.min_delay_ms, 0.0)
        self.assertEqual(engine.max_delay_ms, 0.0)

        engine.df_options = pl.DataFrame({
            "token": ["NSE_FO|TESTCE"] * 12,
            "exchange_timestamp": [1700000000.0 + i for i in range(12)],
            "ask": [100.0] * 12,
            "bid": [100.0] + [130.0] * 11,
            "ltp": [100.0] * 12,
        })
        res = engine._run_direct_option_simulation()
        self.assertEqual(res["paper"]["fast_momentum_ATM"]["start_capital"], 190000.0)
        self.assertEqual(res["paper"]["adaptive_regime_ratchet_ATM"]["start_capital"], 190000.0)
        self.assertEqual(res["paper_delayed"]["adaptive_regime_ratchet_ATM"]["start_capital"], 190000.0)
        self.assertEqual(res["paper"]["fast_momentum_ATM"]["total_brokerage"], 0.0)

    def test_sim_and_historical_services_respect_config_and_zero_overrides(self):
        """SimulationService and HistoricalService must propagate capital & zero overrides cleanly."""
        sim_svc = SimulationService(recordings_dir=self.temp_dir.name)
        custom_params = {
            "global_settings": {
                "capital": 160000.0,
                "brokerage_per_order": 0.0,
                "min_delay_ms": 0.0,
                "max_delay_ms": 0.0,
                "daily_loss_limit_inr": 0.0,
            },
            "base_tracks": {
                "fast_momentum": {"enabled": True, "modes": ["ATM"], "target_pct": 0.15, "sl_pct": 0.05},
            },
            "composite_tracks": [
                {"enabled": True, "name": "composite_shared_all", "shared_pool": True, "composition": ["fast_momentum_ATM"]},
            ],
            "custom_tracks": [],
        }
        strat_cfg = sim_svc._build_custom_strategy_cfg(custom_params)
        mom_cfg = strat_cfg["strategies"]["momentum"]
        self.assertEqual(mom_cfg["brokerage_per_order"], 0.0)
        comp_entry = next(e for e in mom_cfg["enabled_tracks"] if e.get("name") == "composite_shared_all")
        self.assertEqual(comp_entry["capital"], 160000.0)

        from web.sim_disk_cache import _normalize_global_settings_for_mode
        specs = resolve_track_cache_specs("2026-11-27", custom_params, strat_cfg, recordings_dir=self.temp_dir.name)
        delayed_comp = next(s for s in specs if s["mode"] == "paper_delayed" and s["track_name"] == "composite_shared_all")
        self.assertTrue(bool(delayed_comp.get("cache_key")))
        mode_glob = _normalize_global_settings_for_mode("paper_delayed", custom_params)
        self.assertEqual(mode_glob["capital"], 160000.0)
        self.assertEqual(mode_glob["brokerage_per_order"], 0.0)
        self.assertEqual(mode_glob["min_delay_ms"], 0.0)
        self.assertEqual(mode_glob["daily_loss_limit_inr"], 0.0)

        hist_svc = HistoricalService(recordings_dir=self.temp_dir.name)
        with patch.object(
            get_config_manager(),
            "get_execution_mode_config",
            return_value={"starting_capital_per_sim": 325000.0},
        ):
            stats = hist_svc._compute_historical_track_stats("paper", "fast_momentum", [])
        self.assertEqual(stats["start_budget"], 325000.0)


if __name__ == "__main__":
    unittest.main()
