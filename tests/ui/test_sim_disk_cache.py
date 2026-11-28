import json
import os
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from web.alert_service import AlertService
from web.health_service import HealthService
from web.historical_service import HistoricalService
from web.live_service import LiveService
from web.sim_disk_cache import (
    CACHE_MAGIC,
    DEFAULT_SLOT_BYTES,
    HEADER_BYTES,
    HEADER_STRUCT,
    SimDiskCache,
    resolve_track_cache_specs,
)
from web.simulation_service import SimulationService


class TestSimDiskCacheAndPaths(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_sim_cache_")
        self.rec_dir = os.path.join(self.tmp_dir, "recordings")
        self.cache_dir = os.path.join(self.tmp_dir, "cache")
        os.makedirs(self.rec_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fixed_slot_append_and_inplace_cold_overwrite_preserves_neighbors(self):
        cache = SimDiskCache(cache_dir=self.cache_dir, slot_bytes=4096)
        k1 = "a" * 64
        k2 = "b" * 64
        k3 = "c" * 64

        det1 = {
            "trades": [{
                "trade_id": "T1",
                "symbol": "RELIANCE",
                "net_pnl": 500.0,
                "underlying_candles": [{"o": 1, "c": 2}],
                "option_candles": [{"o": 10, "c": 12}],
            }],
            "curve": [{"time": "09:20:00", "net_pnl": 500.0}],
        }
        det2 = {
            "trades": [{"trade_id": "T2", "symbol": "TCS", "net_pnl": -120.0}],
            "curve": [{"time": "09:25:00", "net_pnl": -120.0}],
        }
        det3 = {
            "trades": [{"trade_id": "T3", "symbol": "INFY", "net_pnl": 900.0}],
            "curve": [{"time": "09:30:00", "net_pnl": 900.0}],
        }

        m1 = cache.put("2026-11-19", k1, "paper", "fast_momentum_ATM", {"net_pnl": 500.0, "trades": 1}, det1)
        m2 = cache.put("2026-11-19", k2, "paper_delayed", "fast_momentum_ATM", {"net_pnl": -120.0, "trades": 1}, det2)
        m3 = cache.put("2026-11-19", k3, "paper", "trend_pullback_ITM", {"net_pnl": 900.0, "trades": 1}, det3)

        self.assertEqual(m1["offset"], 0)
        self.assertEqual(m2["offset"], 4096)
        self.assertEqual(m3["offset"], 8192)
        dat_path = os.path.join(self.cache_dir, "2026-11-19.dat")
        self.assertEqual(os.path.getsize(dat_path), 12288)

        # Verify heavy chart arrays stripped on disk
        r1 = cache.get("2026-11-19", k1)
        self.assertIsNotNone(r1)
        self.assertNotIn("underlying_candles", r1["details"]["trades"][0])
        self.assertNotIn("option_candles", r1["details"]["trades"][0])

        # Cold run overwrite on middle slot (k2) must stay in-place at offset 4096
        det2_updated = {
            "trades": [{"trade_id": "T2B", "symbol": "TCS", "net_pnl": 444.0}],
            "curve": [{"time": "09:25:00", "net_pnl": 444.0}],
        }
        m2_cold = cache.put(
            "2026-11-19", k2, "paper_delayed", "fast_momentum_ATM",
            {"net_pnl": 444.0, "trades": 1}, det2_updated, is_cold_run=True,
        )
        self.assertEqual(m2_cold["offset"], 4096)
        self.assertEqual(m2_cold["slot_bytes"], 4096)
        self.assertEqual(m2_cold["cold_run_count"], 1)
        # File size unchanged & neighboring records intact
        self.assertEqual(os.path.getsize(dat_path), 12288)
        self.assertEqual(cache.get("2026-11-19", k1)["matrix"]["net_pnl"], 500.0)
        self.assertEqual(cache.get("2026-11-19", k2)["matrix"]["net_pnl"], 444.0)
        self.assertEqual(cache.get("2026-11-19", k3)["matrix"]["net_pnl"], 900.0)

        # Verify standardized fixed header format directly from raw binary slot
        with open(dat_path, "rb") as f:
            f.seek(4096)
            hdr = HEADER_STRUCT.unpack(f.read(HEADER_BYTES))
        self.assertEqual(hdr[0], CACHE_MAGIC)
        self.assertEqual(hdr[2].rstrip(b"\x00").decode("ascii"), k2)
        self.assertEqual(hdr[3].rstrip(b"\x00").decode("utf-8"), "paper_delayed")
        self.assertEqual(hdr[6], 1)  # cold_run_count
        self.assertEqual(hdr[9], 4096)

    def test_partial_cache_hit_subset_reuse_and_cold_run_modes(self):
        sim_svc = SimulationService(recordings_dir=self.rec_dir, cache_dir=self.cache_dir)
        executed_calls = []

        def fake_worker(queue, result_file, target_date, worker_plan, recordings_dir, precompute_candles=False):
            import pickle
            combined_matrix = {}
            combined_details = {}
            for step in worker_plan:
                sub_params = step["params"]
                modes = step["modes"]
                strat_cfg = sim_svc._build_custom_strategy_cfg(sub_params)
                from common.tracks.factory import get_enabled_track_names
                tracks = get_enabled_track_names(strat_cfg)
                executed_calls.append((tuple(modes), tuple(tracks), dict(sub_params["base_tracks"])))
                for m in modes:
                    combined_matrix.setdefault(m, {})
                    combined_details.setdefault(m, {})
                    for tr in tracks:
                        base_k = tr.rsplit("_", 1)[0]
                        tp = float((sub_params.get("base_tracks") or {}).get(base_k, {}).get("target_pct", 0.1))
                        net = round(1000.0 * tp + (50.0 if m == "paper" else 10.0), 2)
                        combined_matrix[m][tr] = {
                            "trades": 1, "wins": 1, "losses": 0, "win_rate": 100.0,
                            "gross_pnl": net + 60.0, "brokerage": 60.0, "net_pnl": net,
                            "max_dd": 0.0, "max_dd_pct": 0.0, "profit_factor": 99.0,
                        }
                        combined_details[m][tr] = {
                            "trades": [{"trade_id": f"{m}_{tr}", "net_pnl": net}],
                            "curve": [{"time": "09:20:00", "net_pnl": net}],
                        }
            with open(result_file, "wb") as f:
                pickle.dump({"date": target_date, "cached": False, "matrix": combined_matrix, "details": combined_details}, f)
            queue.put({"type": "done", "file": result_file})

        class FakeProc:
            def __init__(self, target, args, daemon=True):
                self._target = target
                self._args = args
                self.exitcode = 0
                self._alive = False

            def start(self):
                self._target(*self._args)

            def is_alive(self):
                return self._alive

        class FakeCtx:
            def Queue(self):
                import queue as qmod
                return qmod.Queue()

            def Process(self, target, args, daemon=True):
                return FakeProc(target, args, daemon=daemon)

        import asyncio

        base_payload = {
            "date": "2026-11-19",
            "global_settings": {
                "modes": ["paper", "paper_delayed"],
                "capital": 250000.0,
                "min_delay_ms": 200.0,
                "max_delay_ms": 500.0,
            },
            "base_tracks": {
                "fast_momentum": {"enabled": True, "modes": ["ATM"], "target_pct": 0.12, "sl_pct": 0.08},
                "trend_pullback": {"enabled": True, "modes": ["ITM"], "target_pct": 0.20, "sl_pct": 0.10},
            },
            "composite_tracks": [],
            "custom_tracks": [],
        }

        async def run_scenario():
            with patch("web.simulation_service.mp.get_context", return_value=FakeCtx()):
                # 1. Cold initial run: runs both tracks across paper & paper_delayed
                res1 = await sim_svc.run_simulation_job(base_payload)
                self.assertEqual(len(executed_calls), 1)
                self.assertEqual(res1["cache_stats"]["hits"], 0)
                self.assertEqual(res1["cache_stats"]["misses"], 4)

                # 2. Subset request (only fast_momentum ATM & only paper): 100% cache hit, 0 worker calls!
                executed_calls.clear()
                sub_payload = json.loads(json.dumps(base_payload))
                sub_payload["global_settings"]["modes"] = ["paper"]
                sub_payload["base_tracks"]["trend_pullback"]["enabled"] = False
                res2 = await sim_svc.run_simulation_job(sub_payload)
                self.assertEqual(len(executed_calls), 0)
                self.assertTrue(res2["cached"])
                self.assertEqual(res2["cache_stats"]["hits"], 1)
                self.assertEqual(list(res2["matrix"]["paper"].keys()), ["fast_momentum_ATM"])

                # 3. Modify ONLY trend_pullback target_pct -> fast_momentum_ATM reused from disk!
                executed_calls.clear()
                mod_payload = json.loads(json.dumps(base_payload))
                mod_payload["base_tracks"]["trend_pullback"]["target_pct"] = 0.35
                res3 = await sim_svc.run_simulation_job(mod_payload)
                self.assertEqual(len(executed_calls), 1)
                self.assertEqual(executed_calls[0][1], ("trend_pullback_ITM",))
                self.assertEqual(res3["cache_stats"]["hits"], 2)   # paper & delayed fast_momentum_ATM
                self.assertEqual(res3["cache_stats"]["misses"], 2) # paper & delayed trend_pullback_ITM

                # 4. Change delayed latency ONLY -> paper reuses 100% disk cache; ONLY paper_delayed re-runs!
                executed_calls.clear()
                del_payload = json.loads(json.dumps(mod_payload))
                del_payload["global_settings"]["max_delay_ms"] = 800.0
                res4 = await sim_svc.run_simulation_job(del_payload)
                self.assertEqual(len(executed_calls), 1)
                self.assertEqual(executed_calls[0][0], ("paper_delayed",))
                self.assertEqual(res4["cache_stats"]["hits"], 2)
                self.assertEqual(res4["cache_stats"]["misses"], 2)

                # 5. Cold run for `paper` only -> re-runs `paper`, reuses `paper_delayed` from cache!
                executed_calls.clear()
                cold_payload = json.loads(json.dumps(del_payload))
                cold_payload["cold_run_modes"] = ["paper"]
                res5 = await sim_svc.run_simulation_job(cold_payload)
                self.assertEqual(len(executed_calls), 1)
                self.assertEqual(executed_calls[0][0], ("paper",))
                self.assertEqual(res5["cache_stats"]["cold_runs"], 2)
                self.assertEqual(res5["cache_stats"]["hits"], 2)

        # Monkeypatch _simulation_worker_process inside simulation_service module
        with patch("web.simulation_service._simulation_worker_process", side_effect=fake_worker):
            asyncio.run(run_scenario())

    def test_reorganized_subdirectories_supported(self):
        logs_dir = os.path.join(self.tmp_dir, "logs")
        os.makedirs(os.path.join(logs_dir, "alerts"), exist_ok=True)
        os.makedirs(os.path.join(logs_dir, "exec_logs"), exist_ok=True)
        os.makedirs(os.path.join(self.rec_dir, "2026-11-28", "journal"), exist_ok=True)

        with open(os.path.join(logs_dir, "alerts", "alerts_2026-11-28.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"symbol": "RELIANCE", "timestamp": 1764300000.0, "score": 8.0}) + "\n")
        with open(os.path.join(logs_dir, "exec_logs", "scoring_engine.log"), "w", encoding="utf-8") as f:
            f.write("2026-11-28 10:00:00 IST [INFO] [SCORING] Heartbeat ok\n")
        with open(os.path.join(self.rec_dir, "2026-11-28", "journal", "journal_paper_fast_momentum_ATM.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "TRADE_EXIT",
                "data": {
                    "trade_id": "T10",
                    "mode": "paper",
                    "track": "fast_momentum_ATM",
                    "symbol": "RELIANCE",
                    "token": "NSE_FO:123",
                    "status": "CLOSED",
                    "entry_price": 10.0,
                    "exit_price": 12.0,
                    "entry_time": 1764300000.0,
                    "exit_time": 1764300300.0,
                    "realized_pnl": 500.0,
                }
            }) + "\n")

        alert_svc = AlertService(logs_dir=logs_dir)
        alerts_res = alert_svc.get_alerts(target_date="2026-11-28")
        self.assertEqual(alerts_res["total_count"], 1)

        health_svc = HealthService(logs_dir=logs_dir, skip_system_probes=True)
        logs_res = health_svc.get_logs(date_str="2026-11-28")
        self.assertGreaterEqual(logs_res["counts"]["matched"], 1)

        hist_svc = HistoricalService(recordings_dir=self.rec_dir, logs_dir=logs_dir)
        dates = hist_svc.get_available_dates()
        self.assertTrue(any(d["date"] == "2026-11-28" and d["has_journals"] for d in dates))
        ov = hist_svc.get_historical_overview("2026-11-28")
        self.assertEqual(ov["modes"]["paper"]["tracks"]["fast_momentum_ATM"]["trades"], 1)

    def test_resolve_specs_respects_disabled_tracks_and_param_overrides_and_appends_new_track(self):
        sim_svc = SimulationService(recordings_dir=self.rec_dir, cache_dir=self.cache_dir)
        cfg = sim_svc.get_default_config("default")
        base_tracks = {
            k: {**v, "enabled": (k == "volatility_expansion")}
            for k, v in cfg["base_tracks"].items()
        }
        p1 = {
            "date": "2026-11-19",
            "track_profile": "default",
            "global_settings": {**cfg["global_settings"], "modes": ["paper"]},
            "base_tracks": base_tracks,
            "composite_tracks": [],
            "custom_tracks": [],
        }
        sc1 = sim_svc._build_custom_strategy_cfg(p1)
        specs1 = resolve_track_cache_specs("2026-11-19", p1, sc1, self.rec_dir)
        self.assertEqual([s["track_name"] for s in specs1], ["volatility_expansion_ATM"])

        # Changing a parameter on the enabled track must produce a distinct cache_key even with track_profile="default"
        p2 = json.loads(json.dumps(p1))
        p2["base_tracks"]["volatility_expansion"]["target_pct"] = 0.35
        sc2 = sim_svc._build_custom_strategy_cfg(p2)
        specs2 = resolve_track_cache_specs("2026-11-19", p2, sc2, self.rec_dir)
        self.assertNotEqual(specs1[0]["cache_key"], specs2[0]["cache_key"])

        # Adding a newly enabled track later keeps original track's key identical for disk reuse + appends new track
        p3 = json.loads(json.dumps(p1))
        p3["base_tracks"]["volatility_expansion_ratchet"]["enabled"] = True
        sc3 = sim_svc._build_custom_strategy_cfg(p3)
        specs3 = resolve_track_cache_specs("2026-11-19", p3, sc3, self.rec_dir)
        self.assertEqual(
            [s["track_name"] for s in specs3],
            ["volatility_expansion_ATM", "volatility_expansion_ratchet_ATM"],
        )
        self.assertEqual(specs3[0]["cache_key"], specs1[0]["cache_key"])
