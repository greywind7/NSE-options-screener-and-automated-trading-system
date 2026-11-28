"""
Admin Service: Control plane for Pre-Market Preparation, Core Tier Processes,
Per-Mode Granular Strategy Launches, Post-Market Housekeeping, Broker Auth,
Risk Freeze / Unfreeze, and Immutable Audit Logging.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

from core.auth.groww_auth import generate_groww_access_token
from core.auth.upstox_auth import load_env
from core.common.logging_utils import get_logger
from core.common.time_utils import IST
from core.ops.operations import (
    ALL_EXECUTION_MODES,
    ALL_POSTMARKET_ACTIONS,
    ALL_PREPARE_STEPS,
    get_freeze_status,
    is_core_rms_running,
    load_json_config,
    resolve_tracks_by_mode,
    run_post_market,
    run_prepare_steps,
    run_reconcile,
    set_freeze_state,
    set_rest_fallback_state,
)
from scripts.run_distributed import is_pid_alive, kill_roles, load_pids, spawn_process

_logger = get_logger("ADMIN_SERVICE")

CORE_SERVICES = [
    {
        "id": "feed_handler",
        "label": "Feed Handler",
        "description": "Upstox Dual WebSocket / Mock Market Data Feed",
        "module": "core.feed.feed_handler",
    },
    {
        "id": "candle_manager",
        "label": "Candle Manager",
        "description": "Shared Memory OHLCV Bar Builder (shm_ohlc_221)",
        "module": "core.candles.candle_manager",
    },
    {
        "id": "quotes_broker",
        "label": "Quotes Broker",
        "description": "LVC Option Chain Quotes & Heartbeat Router",
        "module": "core.quotes.quotes_broker",
    },
    {
        "id": "rms_service",
        "label": "RMS Service (Core)",
        "description": "Strike Selection, Liquidity Gate & Strategy-Aware PUB",
        "module": "core.rms.rms_service",
    },
    {
        "id": "market_recorder",
        "label": "Market Recorder",
        "description": "Tick-by-Tick Parquet Session Archival",
        "module": "core.recorder.market_recorder",
    },
]

PREMARKET_STEP_META = [
    {"id": "master", "label": "Instrument Master", "description": "Download / validate complete Upstox instrument CSV"},
    {"id": "filter", "label": "F&O Universe Filter", "description": "Filter active F&O equities (NSE) or commodities (MCX)"},
    {"id": "volume", "label": "30-Day Volume Index", "description": "Fetch historical 1m baselines for RVOL scoring"},
    {"id": "manifest", "label": "Subscription Manifest", "description": "Partition WS keys, option chains & REST batches"},
    {"id": "cleanup", "label": "Maintenance Cleanup", "description": "Prune stale CSVs & rotate debug logs"},
]

POSTMARKET_ACTION_META = [
    {"id": "archive", "label": "Compress Session (.tar.gz)", "description": "Archive recordings directory into compressed tarball"},
    {"id": "prune", "label": "Prune Stale Prep CSVs", "description": "Remove older dated instrument CSVs (preserves indices)"},
    {"id": "rotate_logs", "label": "Rotate Debug Logs", "description": "Trim/rotate debug logs safely"},
    {"id": "report", "label": "EOD Journal Summary", "description": "Aggregate session performance metrics across journals"},
]


def _mask_secret(val: Optional[str]) -> Dict[str, Any]:
    s = (val or "").strip()
    if not s:
        return {"configured": False, "masked": "Not Set", "length": 0}
    tail = s[-4:] if len(s) >= 4 else "*" * len(s)
    return {
        "configured": True,
        "masked": f"••••{tail}",
        "length": len(s),
    }


def _update_env_key(env_path: str, key: str, value: str) -> None:
    lines: List[str] = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


class AdminService:
    """Backend operational coordinator for Web UI Admin tab."""

    def __init__(
        self,
        logs_dir: str = "logs",
        pids_file: Optional[str] = None,
        recordings_dir: str = "recordings",
        data_dir: str = "data",
        env_path: str = ".env",
        config_path: str = "config/strategies.json",
    ):
        self.logs_dir = logs_dir
        self.pids_file = pids_file or os.path.join(logs_dir, "pids.json")
        self.recordings_dir = recordings_dir
        self.data_dir = data_dir
        self.env_path = env_path
        self.config_path = config_path
        self.audit_log_path = os.path.join(self.logs_dir, "admin_audit.jsonl")
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin_ops")

    def _python_bin(self) -> str:
        venv_py = os.path.abspath(".venv/bin/python")
        if os.path.exists(venv_py) and os.access(venv_py, os.X_OK):
            return venv_py
        return sys.executable

    def _append_audit(self, action: str, status: str, detail: Dict[str, Any]) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.now(tz=IST).isoformat(),
            "action": action,
            "status": status,
            "detail": detail,
        }
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(self.audit_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            _logger.error(f"Failed writing admin audit log: {e}")
        return entry

    def get_recent_audit(self, limit: int = 40) -> List[Dict[str, Any]]:
        if not os.path.exists(self.audit_log_path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with open(self.audit_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            return []
        return list(reversed(rows[-max(1, limit):]))

    def _discover_tracks(self) -> Dict[str, Any]:
        from common.tracks.factory import get_enabled_track_names, load_track_profile

        strat_cfg = load_json_config(self.config_path)
        try:
            default_enabled = get_enabled_track_names(strategy_cfg=strat_cfg or None)
        except Exception:
            default_enabled = ["fast_momentum_ATM", "trend_pullback_ITM", "fixed_target_ATM", "composite_shared_ITM"]

        all_track_ids: List[str] = []
        track_meta: Dict[str, Dict[str, Any]] = {}

        def _infer_moneyness(name: str) -> str:
            for suffix in ("ITM2", "ITM1", "OTM2", "OTM1", "ITM", "ATM", "OTM"):
                if name.endswith(f"_{suffix}"):
                    return suffix
            return "ATM"

        def _add_track(name: str, t_type: str = "single", moneyness: Optional[str] = None, source: str = "default") -> None:
            if name not in all_track_ids:
                all_track_ids.append(name)
            track_meta[name] = {
                "name": name,
                "enabled_default": name in default_enabled,
                "type": t_type,
                "moneyness": moneyness or _infer_moneyness(name),
                "source": source,
            }

        # Only register concrete runnable tracks from enabled_tracks specs (skip bare parameter templates)
        for name in default_enabled:
            _add_track(name, "composite" if name.startswith("composite") else "single", None, "default")

        for prof in ("default", "v1", "v2"):
            pdata = load_track_profile(prof)
            if not isinstance(pdata, dict):
                continue
            spec = pdata.get("enabled_tracks") or pdata.get("momentum", {}).get("enabled_tracks") or []
            for entry in spec:
                if not isinstance(entry, dict):
                    continue
                t_type = str(entry.get("type", "base")).lower()
                t_name = entry.get("name")
                if not t_name:
                    continue
                if t_type == "composite":
                    _add_track(t_name, "composite", None, f"profile:{prof}")
                else:
                    modes = entry.get("modes") or []
                    for m in modes:
                        full_name = f"{t_name}_{m}"
                        _add_track(full_name, "single", str(m), f"profile:{prof}")

        return {
            "default_enabled": default_enabled,
            "tracks": [track_meta[t] for t in all_track_ids],
        }

    def get_config(self) -> Dict[str, Any]:
        """Return complete capabilities, current state flags, auth status, and running processes."""
        pids = load_pids(self.pids_file)
        running_procs: Dict[str, Any] = {}
        for role, info in pids.items():
            pid = info.get("pid")
            alive = bool(pid and is_pid_alive(int(pid)))
            running_procs[role] = {
                "role": role,
                "pid": pid,
                "alive": alive,
                "tier": info.get("tier") or "SYSTEM",
                "started_at": info.get("started_at"),
                "log_file": info.get("log_file"),
            }

        env_file_vars = load_env(self.env_path) if os.path.exists(self.env_path) else {}
        upstox_tok = os.environ.get("UPSTOX_ACCESS_TOKEN") or env_file_vars.get("UPSTOX_ACCESS_TOKEN")
        groww_tok = os.environ.get("GROWW_ACCESS_TOKEN") or env_file_vars.get("GROWW_ACCESS_TOKEN")
        groww_key = os.environ.get("GROWW_API_KEY") or env_file_vars.get("GROWW_API_KEY")

        track_catalog = self._discover_tracks()
        rest_state = set_rest_fallback_state(action="status", logs_dir=self.logs_dir)

        return {
            "session_date": datetime.now(tz=IST).strftime("%Y%m%d"),
            "premarket_steps": PREMARKET_STEP_META,
            "postmarket_actions": POSTMARKET_ACTION_META,
            "core_services": CORE_SERVICES,
            "modes": ["paper", "paper_delayed", "live"],
            "default_tracks": track_catalog["default_enabled"],
            "tracks": track_catalog["tracks"],
            "core_rms_running": is_core_rms_running(self.pids_file),
            "running_processes": running_procs,
            "freeze_state": get_freeze_status(logs_dir=self.logs_dir),
            "rest_fallback_enabled": bool(rest_state.get("enabled")),
            "auth": {
                "upstox_access_token": _mask_secret(upstox_tok),
                "groww_access_token": _mask_secret(groww_tok),
                "groww_api_key": _mask_secret(groww_key),
            },
            "recent_audit": self.get_recent_audit(limit=30),
        }

    async def execute_premarket(
        self,
        steps: Optional[List[str]] = None,
        reset: bool = False,
        session_date: Optional[str] = None,
        exchange: str = "NSE",
        volume_days: int = 30,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            self._executor,
            lambda: run_prepare_steps(
                steps=steps or list(ALL_PREPARE_STEPS),
                reset=bool(reset),
                session_date=session_date,
                exchange=exchange,
                volume_days=int(volume_days),
                data_dir=self.data_dir,
                logs_dir=self.logs_dir,
            ),
        )
        audit = self._append_audit(
            "premarket",
            res.get("status", "ok"),
            {"steps": steps or list(ALL_PREPARE_STEPS), "exchange": exchange, "reset": reset, "session_date": res.get("session_date")},
        )
        res["audit"] = audit
        return res

    async def execute_postmarket(
        self,
        actions: Optional[List[str]] = None,
        session_date: Optional[str] = None,
        keep_raw: bool = True,
        archive_all: bool = False,
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            self._executor,
            lambda: run_post_market(
                actions=actions or ["archive", "prune", "rotate_logs"],
                session_date=session_date,
                record_dir=self.recordings_dir,
                keep_raw=bool(keep_raw),
                archive_all=bool(archive_all),
                data_dir=self.data_dir,
                logs_dir=self.logs_dir,
            ),
        )
        audit = self._append_audit(
            "postmarket",
            res.get("status", "ok"),
            {"actions": actions, "session_date": res.get("session_date"), "keep_raw": keep_raw, "archive_all": archive_all},
        )
        res["audit"] = audit
        return res

    def start_core(
        self,
        services: Optional[List[str]] = None,
        feed_mode: str = "ws",
        debug: bool = False,
    ) -> Dict[str, Any]:
        valid_ids = {s["id"]: s for s in CORE_SERVICES}
        selected = [s.strip() for s in (services or [x["id"] for x in CORE_SERVICES if x["id"] != "market_recorder"]) if s.strip()]
        invalid = [s for s in selected if s not in valid_ids]
        if invalid:
            raise ValueError(f"Unknown core service(s): {', '.join(invalid)}")
        if feed_mode not in ("ws", "mock"):
            raise ValueError(f"Invalid feed_mode '{feed_mode}'. Expected 'ws' or 'mock'.")

        py_bin = self._python_bin()
        pids = load_pids(self.pids_file)
        spawned: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for svc in selected:
            existing = pids.get(svc)
            if existing and existing.get("pid") and is_pid_alive(int(existing["pid"])):
                skipped.append({"role": svc, "pid": existing["pid"], "reason": "already_running"})
                continue

            if svc == "feed_handler":
                cmd = [py_bin, "-m", "core.feed.feed_handler", "--mode", feed_mode]
            elif svc == "candle_manager":
                cmd = [py_bin, "-m", "core.candles.candle_manager"]
            elif svc == "quotes_broker":
                cmd = [py_bin, "-m", "core.quotes.quotes_broker"]
            elif svc == "rms_service":
                cmd = [py_bin, "-m", "core.rms.rms_service"]
            elif svc == "market_recorder":
                cmd = [py_bin, "-m", "core.recorder.market_recorder"]
                if debug:
                    cmd.append("--debug")
            else:
                continue

            exec_logs_dir = os.path.join(self.logs_dir, "exec_logs")
            os.makedirs(exec_logs_dir, exist_ok=True)
            log_path = os.path.join(exec_logs_dir, f"{svc}.log")
            proc = spawn_process(cmd, svc, log_file=log_path, tier="CORE", pids_file=self.pids_file)
            _logger.info(f"Launched Core service '{svc}' (PID={proc.pid})")
            spawned.append({"role": svc, "pid": proc.pid, "log_file": log_path})

        audit = self._append_audit(
            "start_core",
            "ok",
            {"services": selected, "feed_mode": feed_mode, "spawned": [s["role"] for s in spawned], "skipped": [s["role"] for s in skipped]},
        )
        return {"status": "ok", "spawned": spawned, "skipped": skipped, "audit": audit}

    def stop_core(self, services: Optional[List[str]] = None) -> Dict[str, Any]:
        from core.candles.shm_layout import SHM_NAME, SHM_SIGNAL_DETACH, signal_shm_consumers, unlink_shm
        pids = load_pids(self.pids_file)
        if services:
            targets = [s for s in services if s in pids]
        else:
            core_ids = {s["id"] for s in CORE_SERVICES}
            targets = [r for r, inf in pids.items() if r in core_ids or inf.get("tier") == "CORE"]
        stopping_candle = "candle_manager" in targets or (not services and "candle_manager" in pids)
        if stopping_candle:
            signaled = signal_shm_consumers(SHM_SIGNAL_DETACH, pids_file=self.pids_file)
            if signaled:
                _logger.info(f"Signaled SHM consumers {signaled} to detach before stopping candle_manager.")
        killed = kill_roles(targets, pids_file=self.pids_file) if targets else 0
        if stopping_candle or (not services):
            unlink_shm(SHM_NAME)
        audit = self._append_audit("stop_core", "ok", {"targets": targets, "killed": killed})
        return {"status": "ok", "targets": targets, "killed": killed, "audit": audit}

    def start_strategies(
        self,
        modes: Optional[List[str]] = None,
        tracks_by_mode: Optional[Dict[str, List[str]]] = None,
        tracks: Optional[List[str]] = None,
        continue_state: bool = False,
        confirm_live: bool = False,
        confirm_live_phrase: str = "",
        capital: Optional[float] = None,
        eod_freeze_time: Optional[str] = None,
        eod_time: Optional[str] = None,
        spawn_scoring: bool = True,
        allow_fallback_rms: bool = True,
    ) -> Dict[str, Any]:
        """
        Launch strategy scoring engine + granular per-mode track execution processes.
        Enforces strict confirmation if 'live' mode is selected.
        """
        mode_list = [str(m).strip().lower() for m in (modes or (list(tracks_by_mode.keys()) if tracks_by_mode else ["paper"])) if str(m).strip()]
        invalid_modes = [m for m in mode_list if m not in ("paper", "paper_delayed", "live")]
        if invalid_modes:
            raise ValueError(f"Unsupported execution mode(s): {', '.join(invalid_modes)}")
        if not mode_list:
            raise ValueError("Select at least one execution mode.")

        if tracks_by_mode:
            resolved_map = resolve_tracks_by_mode(mode_list, tracks_by_mode=tracks_by_mode, strategy_config=self.config_path)
        else:
            spec = ",".join(tracks) if tracks else "ALL"
            resolved_map = resolve_tracks_by_mode(mode_list, tracks_spec=spec, strategy_config=self.config_path)

        total_selected_tracks = sum(len(v) for v in resolved_map.values())
        if total_selected_tracks == 0:
            raise ValueError("Select at least one track inside the chosen execution mode(s).")

        live_tracks = resolved_map.get("live", [])
        if "live" in mode_list and live_tracks:
            if not confirm_live or confirm_live_phrase.strip().upper() != "CONFIRM LIVE":
                raise ValueError("Launching LIVE mode requires checking confirmation and typing 'CONFIRM LIVE'.")

        py_bin = self._python_bin()
        pids = load_pids(self.pids_file)
        spawned: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        exec_logs_dir = os.path.join(self.logs_dir, "exec_logs")
        os.makedirs(exec_logs_dir, exist_ok=True)

        # 1. Scoring Engine
        if spawn_scoring:
            sc_info = pids.get("scoring_engine")
            if sc_info and sc_info.get("pid") and is_pid_alive(int(sc_info["pid"])):
                skipped.append({"role": "scoring_engine", "pid": sc_info["pid"], "reason": "already_running"})
            else:
                sc_log = os.path.join(exec_logs_dir, "scoring_engine.log")
                p_sc = spawn_process(
                    [py_bin, "-m", "strategies.momentum.scoring_engine"],
                    "scoring_engine",
                    log_file=sc_log,
                    tier="STRATEGY",
                    pids_file=self.pids_file,
                )
                spawned.append({"role": "scoring_engine", "pid": p_sc.pid, "log_file": sc_log})

        # 2. RMS: Attach to Core RMS if alive, otherwise optional fallback
        attached_rms = is_core_rms_running(self.pids_file)
        if not attached_rms and allow_fallback_rms:
            rms_log = os.path.join(exec_logs_dir, "rms_service.log")
            p_rms = spawn_process(
                [py_bin, "-m", "core.rms.rms_service"],
                "rms_service",
                log_file=rms_log,
                tier="CORE",
                pids_file=self.pids_file,
            )
            spawned.append({"role": "rms_service", "pid": p_rms.pid, "log_file": rms_log})

        # 3. Granular Execution Processes (mode x track)
        for m in mode_list:
            cls_mod = "paper" if m == "paper" else "delayed" if m == "paper_delayed" else "live"
            cls_name = "PaperSuperstructure" if m == "paper" else "DelayedSuperstructure" if m == "paper_delayed" else "LiveSuperstructure"
            for tr in resolved_map.get(m, []):
                role = f"exec_{m}_{tr}"
                ex_info = pids.get(role)
                if ex_info and ex_info.get("pid") and is_pid_alive(int(ex_info["pid"])):
                    skipped.append({"role": role, "pid": ex_info["pid"], "reason": "already_running"})
                    continue

                extra_kwargs = ""
                if m == "live":
                    extra_kwargs += f", live_confirmed={bool(confirm_live)}"
                    if capital is not None:
                        extra_kwargs += f", initial_capital={float(capital)}"
                    if eod_freeze_time:
                        extra_kwargs += f", eod_freeze_time={repr(str(eod_freeze_time))}"
                    if eod_time:
                        extra_kwargs += f", eod_square_off_time={repr(str(eod_time))}"
                elif capital is not None:
                    extra_kwargs += f", initial_capital={float(capital)}"

                exec_code = (
                    f"from execution.superstructures.{cls_mod} import {cls_name}; "
                    f"s = {cls_name}(track_names=[{repr(tr)}], continue_state={bool(continue_state)}{extra_kwargs}); "
                    f"s.start()"
                )
                log_path = os.path.join(exec_logs_dir, f"{role}.log")
                p_ex = spawn_process([py_bin, "-c", exec_code], role, log_file=log_path, tier="EXECUTION", pids_file=self.pids_file)
                spawned.append({"role": role, "mode": m, "track": tr, "pid": p_ex.pid, "log_file": log_path})

        _logger.info(
            f"Strategy launch | Modes={mode_list} | CoreRMSAttached={attached_rms} | "
            f"Spawned={len(spawned)} | Skipped={len(skipped)}"
        )
        audit = self._append_audit(
            "start_strategies",
            "ok",
            {"modes": mode_list, "tracks_by_mode": resolved_map, "continue_state": continue_state, "core_rms_attached": attached_rms, "spawned": [s["role"] for s in spawned]},
        )
        return {
            "status": "ok",
            "core_rms_attached": attached_rms,
            "tracks_by_mode": resolved_map,
            "spawned": spawned,
            "skipped": skipped,
            "audit": audit,
        }

    def stop_strategies(self, modes: Optional[List[str]] = None, include_scoring: bool = True) -> Dict[str, Any]:
        pids = load_pids(self.pids_file)
        targets: List[str] = []
        mode_set = {m.lower() for m in modes} if modes else None
        for role, inf in pids.items():
            if role == "scoring_engine" and include_scoring:
                targets.append(role)
            elif role.startswith("exec_"):
                if mode_set is None or any(role.startswith(f"exec_{m}_") for m in mode_set):
                    targets.append(role)
        killed = kill_roles(targets, pids_file=self.pids_file) if targets else 0
        audit = self._append_audit("stop_strategies", "ok", {"targets": targets, "killed": killed})
        return {"status": "ok", "targets": targets, "killed": killed, "audit": audit}

    def stop_process(self, role: str) -> Dict[str, Any]:
        r = (role or "").strip()
        if not r:
            raise ValueError("Process role is required.")
        killed = kill_roles([r], pids_file=self.pids_file)
        audit = self._append_audit("stop_process", "ok", {"role": r, "killed": killed})
        return {"status": "ok", "role": r, "killed": killed, "audit": audit}

    async def perform_action(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        act = (action or "").strip().lower()
        p = params or {}

        if act == "freeze":
            res = set_freeze_state(mode=p.get("mode", "all"), freeze=True, logs_dir=self.logs_dir, pids_file=self.pids_file)
            res["audit"] = self._append_audit("freeze", res.get("status", "ok"), {"mode": res.get("mode")})
            return res

        if act == "unfreeze":
            res = set_freeze_state(mode=p.get("mode", "all"), freeze=False, logs_dir=self.logs_dir, pids_file=self.pids_file)
            res["audit"] = self._append_audit("unfreeze", res.get("status", "ok"), {"mode": res.get("mode")})
            return res

        if act == "stop_live":
            phrase = str(p.get("confirm_phrase", "")).strip().upper()
            if phrase != "STOP LIVE":
                raise ValueError("Emergency Live Stop requires typing 'STOP LIVE'.")
            res = set_freeze_state(mode="live", freeze=True, logs_dir=self.logs_dir, pids_file=self.pids_file)
            killed_live = 0
            if p.get("stop_processes", False):
                pids = load_pids(self.pids_file)
                live_roles = [r for r in pids.keys() if r.startswith("exec_live_")]
                killed_live = kill_roles(live_roles, pids_file=self.pids_file) if live_roles else 0
            res["killed_live_processes"] = killed_live
            res["audit"] = self._append_audit("stop_live", res.get("status", "ok"), {"killed_live_processes": killed_live})
            return res

        if act == "rest_fallback":
            state = p.get("state", "status")
            res = set_rest_fallback_state(action=state, logs_dir=self.logs_dir)
            res["audit"] = self._append_audit("rest_fallback", "ok", {"action": state, "enabled": res.get("enabled")})
            return res

        if act == "reconcile":
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(self._executor, lambda: run_reconcile(confirm_live=True))
            res["audit"] = self._append_audit("reconcile", str(res.get("status", "ok")).lower(), {"local": res.get("local_positions_count"), "broker": res.get("broker_positions_count")})
            return res

        if act == "refresh_groww_token":
            totp = str(p.get("totp", "")).strip()
            env_vars = load_env(self.env_path) if os.path.exists(self.env_path) else {}
            api_key = str(p.get("api_key") or os.environ.get("GROWW_API_KEY") or env_vars.get("GROWW_API_KEY") or "").strip()
            if not api_key or not totp:
                raise ValueError("Both GROWW_API_KEY (in .env) and a 6-digit TOTP code are required.")
            loop = asyncio.get_running_loop()
            tok_res = await loop.run_in_executor(self._executor, lambda: generate_groww_access_token(api_key=api_key, totp=totp))
            if not tok_res or not tok_res.get("access_token"):
                err = tok_res.get("error") if isinstance(tok_res, dict) else "Failed to generate Groww token"
                self._append_audit("refresh_groww_token", "error", {"error": str(err)})
                raise ValueError(str(err))
            new_token = str(tok_res["access_token"])
            os.environ["GROWW_ACCESS_TOKEN"] = new_token
            _update_env_key(self.env_path, "GROWW_ACCESS_TOKEN", new_token)
            masked = _mask_secret(new_token)
            audit = self._append_audit("refresh_groww_token", "ok", {"token_masked": masked["masked"]})
            return {"status": "ok", "auth_groww_access_token": masked, "audit": audit}

        if act == "stop_all":
            pids = load_pids(self.pids_file)
            all_roles = list(pids.keys())
            killed = kill_roles(None, pids_file=self.pids_file)
            audit = self._append_audit("stop_all", "ok", {"targets": all_roles, "killed": killed})
            return {"status": "ok", "killed": killed, "audit": audit}

        raise ValueError(f"Unknown admin action '{action}'.")
