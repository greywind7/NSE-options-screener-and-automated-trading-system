"""
Canonical operational helpers shared between CLI (`runner.py`) and Web Admin (`web/admin_service.py`).
Avoids duplicating pre-market, post-market, freeze/unfreeze, rest-fallback, reconciliation,
and Core/Strategy process inspection logic.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import shutil
import signal
import tarfile
from typing import Any, Dict, List, Optional, Tuple

from core.common.logging_utils import get_logger
from core.common.time_utils import IST
from scripts.run_distributed import is_pid_alive, load_pids

ALL_EXECUTION_MODES = ("live", "paper", "paper_delayed", "sim")
ALL_PREPARE_STEPS = ("master", "filter", "volume", "manifest", "cleanup")
ALL_POSTMARKET_ACTIONS = ("archive", "prune", "rotate_logs", "report")

_logger = get_logger("OPS")


def load_json_config(path: str) -> Dict[str, Any]:
    """Load JSON configuration file safely."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _logger.error(f"Error reading configuration {path}: {e}")
        return {}


def is_core_rms_running(pids_file: str = "logs/pids.json") -> bool:
    """Check whether Core-tier decoupled RMSService is alive in pids registry."""
    pids = load_pids(pids_file)
    rms_info = pids.get("rms_service")
    if not rms_info:
        return False
    pid = rms_info.get("pid")
    return bool(pid and is_pid_alive(int(pid)))


def get_freeze_status(logs_dir: str = "logs") -> Dict[str, bool]:
    """Inspect on-disk freeze flag files across execution modes."""
    all_frozen = os.path.exists(os.path.join(logs_dir, ".freeze_all"))
    status: Dict[str, bool] = {"all": all_frozen}
    for m in ALL_EXECUTION_MODES:
        status[m] = all_frozen or os.path.exists(os.path.join(logs_dir, f".freeze_{m}"))
    return status


def set_freeze_state(
    mode: str = "all",
    freeze: bool = True,
    logs_dir: str = "logs",
    pids_file: str = "logs/pids.json",
) -> Dict[str, Any]:
    """
    Freeze or unfreeze execution modes via sentinel flag files and deliver
    SIGUSR1/SIGUSR2 to running execution processes.
    """
    mode_norm = (mode or "all").strip().lower()
    if mode_norm == "*":
        mode_norm = "all"
    if mode_norm != "all" and mode_norm not in ALL_EXECUTION_MODES:
        raise ValueError(f"Invalid execution mode '{mode}'. Expected one of all, {', '.join(ALL_EXECUTION_MODES)}.")

    os.makedirs(logs_dir, exist_ok=True)
    created_flags: List[str] = []
    removed_flags: List[str] = []
    retained_frozen: List[str] = []
    errors: List[str] = []

    if freeze:
        if mode_norm == "all":
            all_flag = os.path.join(logs_dir, ".freeze_all")
            with open(all_flag, "w", encoding="utf-8") as f:
                f.write(f"frozen at {datetime.now(tz=IST).isoformat()}\n")
            created_flags.append(all_flag)
            for m in ALL_EXECUTION_MODES:
                flag_file = os.path.join(logs_dir, f".freeze_{m}")
                with open(flag_file, "w", encoding="utf-8") as f:
                    f.write(f"frozen at {datetime.now(tz=IST).isoformat()} via freeze all\n")
                created_flags.append(flag_file)
        else:
            flag_file = os.path.join(logs_dir, f".freeze_{mode_norm}")
            with open(flag_file, "w", encoding="utf-8") as f:
                f.write(f"frozen at {datetime.now(tz=IST).isoformat()}\n")
            created_flags.append(flag_file)
        sig = getattr(signal, "SIGUSR1", None)
    else:
        all_flag = os.path.join(logs_dir, ".freeze_all")
        if mode_norm == "all":
            for cand in [all_flag] + [os.path.join(logs_dir, f".freeze_{m}") for m in ALL_EXECUTION_MODES]:
                if os.path.exists(cand):
                    try:
                        os.remove(cand)
                        removed_flags.append(cand)
                    except Exception as e:
                        errors.append(f"Error removing {cand}: {e}")
        else:
            if os.path.exists(all_flag):
                try:
                    os.remove(all_flag)
                    removed_flags.append(all_flag)
                except Exception as e:
                    errors.append(f"Error removing {all_flag}: {e}")
                for other_m in ALL_EXECUTION_MODES:
                    if other_m != mode_norm:
                        other_flag = os.path.join(logs_dir, f".freeze_{other_m}")
                        if not os.path.exists(other_flag):
                            with open(other_flag, "w", encoding="utf-8") as f:
                                f.write(f"frozen at {datetime.now(tz=IST).isoformat()} (retained after unfreezing {mode_norm})\n")
                        retained_frozen.append(other_m)
            target_flag = os.path.join(logs_dir, f".freeze_{mode_norm}")
            if os.path.exists(target_flag):
                try:
                    os.remove(target_flag)
                    removed_flags.append(target_flag)
                except Exception as e:
                    errors.append(f"Error removing {target_flag}: {e}")
        sig = getattr(signal, "SIGUSR2", None)

    signaled_pids: List[int] = []
    if sig is not None:
        # 1. Legacy strategy.pid
        pid_file = os.path.join(logs_dir, "strategy.pid")
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                if is_pid_alive(pid):
                    os.kill(pid, sig)
                    signaled_pids.append(pid)
            except Exception:
                pass
        # 2. Individual distributed exec processes registered in pids.json
        pids = load_pids(pids_file)
        for role, info in pids.items():
            if not role.startswith("exec_"):
                continue
            if mode_norm != "all" and not role.startswith(f"exec_{mode_norm}_"):
                continue
            pid = info.get("pid")
            if pid and int(pid) not in signaled_pids and is_pid_alive(int(pid)):
                try:
                    os.kill(int(pid), sig)
                    signaled_pids.append(int(pid))
                except Exception:
                    pass

    action_label = "FROZEN" if freeze else "UN-FROZEN"
    if freeze:
        _logger.warning(f"Execution mode {mode_norm.upper()} {action_label} | Signaled PIDs={signaled_pids}")
    else:
        _logger.info(f"Execution mode {mode_norm.upper()} {action_label} | Signaled PIDs={signaled_pids}")

    return {
        "status": "error" if errors else "ok",
        "mode": mode_norm,
        "frozen": freeze,
        "created_flags": created_flags,
        "removed_flags": removed_flags,
        "retained_frozen": retained_frozen,
        "signaled_pids": signaled_pids,
        "freeze_state": get_freeze_status(logs_dir=logs_dir),
        "errors": errors,
    }


def set_rest_fallback_state(action: str = "status", logs_dir: str = "logs") -> Dict[str, Any]:
    """Inspect or toggle REST polling fallback sentinel file."""
    act = (action or "status").strip().lower()
    if act not in ("status", "enable", "disable"):
        raise ValueError(f"Invalid rest-fallback action '{action}'.")

    os.makedirs(logs_dir, exist_ok=True)
    flag_file = os.path.join(logs_dir, ".allow_rest_fallback")
    if act == "enable":
        with open(flag_file, "w", encoding="utf-8") as f:
            f.write(f"enabled at {datetime.now(tz=IST).isoformat()}\n")
        _logger.warning(f"REST fallback ENABLED ({flag_file})")
    elif act == "disable":
        if os.path.exists(flag_file):
            os.remove(flag_file)
        _logger.info(f"REST fallback DISABLED ({flag_file})")

    return {
        "status": "ok",
        "action": act,
        "enabled": os.path.exists(flag_file),
        "flag_file": flag_file,
    }


def run_reconcile(confirm_live: bool = True) -> Dict[str, Any]:
    """Run broker position conflation/reconciliation against Groww."""
    from execution.superstructures.live import LiveSuperstructure

    adapter = LiveSuperstructure(live_confirmed=bool(confirm_live), enable_polling=False)
    try:
        res = adapter.reconcile_broker_positions()
        return res
    finally:
        try:
            adapter.stop()
        except Exception:
            pass


def resolve_tracks_by_mode(
    modes: List[str],
    tracks_by_mode: Optional[Dict[str, List[str]]] = None,
    tracks_spec: Optional[str] = None,
    strategy_config: str = "config/strategies.json",
) -> Dict[str, List[str]]:
    """
    Resolve explicit per-mode track lists cleanly.
    Supports granular `tracks_by_mode` mapping ({mode: [tracks...]}) or unified CLI spec.
    """
    from common.tracks.factory import get_enabled_track_names

    strat_cfg = load_json_config(strategy_config) if strategy_config else None
    strat_arg = strat_cfg if strat_cfg else None

    resolved: Dict[str, List[str]] = {}
    if tracks_by_mode:
        default_tracks = get_enabled_track_names(strategy_cfg=strat_arg)
        for m in modes:
            mode_tracks = tracks_by_mode.get(m)
            if mode_tracks is None:
                continue
            cleaned = [str(t).strip() for t in mode_tracks if str(t).strip()]
            if cleaned == ["ALL"]:
                resolved[m] = list(default_tracks)
            else:
                resolved[m] = cleaned
        return resolved

    spec = (tracks_spec or "").strip()
    if not spec or spec.upper() == "ALL":
        default_tracks = get_enabled_track_names(strategy_cfg=strat_arg)
    elif spec.lower() in ("v1", "v2", "default") or os.path.exists(os.path.join("config", "tracks", f"{spec.lower()}.json")):
        default_tracks = get_enabled_track_names(strategy_cfg=strat_arg, track_profile=spec.lower())
    else:
        default_tracks = [t.strip() for t in spec.split(",") if t.strip()]

    for m in modes:
        resolved[m] = list(default_tracks)
    return resolved


def run_prepare_steps(
    steps: Optional[List[str]] = None,
    reset: bool = False,
    session_date: Optional[str] = None,
    exchange: str = "NSE",
    volume_days: int = 30,
    data_dir: str = "data",
    logs_dir: str = "logs",
) -> Dict[str, Any]:
    """
    Execute selected pre-market preparation steps (`master`, `filter`, `volume`, `manifest`, `cleanup`).
    If `steps` is None/empty, executes the full pipeline in dependency order.
    """
    from core.instruments.master_fetcher import run_master_fetcher
    from core.instruments.instrument_filter import run_instrument_filter
    from core.volume.volume_index import run_volume_indexer
    from core.instruments.subscription_manifest import run_manifest_generator
    from core.instruments.cleanup import prune_old_preparation_files
    from core.auth.upstox_auth import prune_debug_log

    target_date = session_date or datetime.now(tz=IST).strftime("%Y%m%d")
    target_exchange = (exchange or "NSE").strip().upper()
    is_mcx = target_exchange == "MCX"

    selected = [s.strip().lower() for s in (steps or list(ALL_PREPARE_STEPS)) if str(s).strip()]
    invalid = [s for s in selected if s not in ALL_PREPARE_STEPS]
    if invalid:
        raise ValueError(f"Unknown pre-market step(s): {', '.join(invalid)}")

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    sys_cfg = load_json_config("config/system.json")
    inst_cfg = load_json_config("config/instruments.json")

    index_dir = os.path.join(data_dir, "index")
    manifest_dir = os.path.join(data_dir, "manifest")
    exec_logs_dir = os.path.join(logs_dir, "exec_logs")
    os.makedirs(index_dir, exist_ok=True)
    os.makedirs(manifest_dir, exist_ok=True)
    os.makedirs(exec_logs_dir, exist_ok=True)

    dated_master = os.path.join(data_dir, f"instrument_{target_date}.csv")
    canonical_master = (
        inst_cfg.get("instruments", {}).get("paths", {}).get("instrument_master_csv")
        if data_dir == "data"
        else None
    ) or os.path.join(data_dir, "instrument.csv")

    if is_mcx:
        dated_filtered = os.path.join(data_dir, f"filtered_mcx_{target_date}.csv")
        dated_index = os.path.join(index_dir, f"index_mcx_{target_date}.json")
        dated_manifest = os.path.join(manifest_dir, f"subscription_manifest_mcx_{target_date}.json")
        canonical_filtered = os.path.join(data_dir, "filtered_mcx.csv")
        canonical_index = os.path.join(index_dir, "index_mcx.json")
        canonical_manifest = os.path.join(manifest_dir, "subscription_manifest_mcx.json")
    else:
        dated_filtered = os.path.join(data_dir, f"filtered_{target_date}.csv")
        dated_index = os.path.join(index_dir, f"index_{target_date}.json")
        dated_manifest = os.path.join(manifest_dir, f"subscription_manifest_{target_date}.json")
        canonical_filtered = (
            inst_cfg.get("instruments", {}).get("paths", {}).get("filtered_universe_csv")
            if data_dir == "data"
            else None
        ) or os.path.join(data_dir, "filtered.csv")
        canonical_index = (
            inst_cfg.get("instruments", {}).get("paths", {}).get("volume_index_json")
            if data_dir == "data"
            else None
        ) or os.path.join(index_dir, "index.json")
        canonical_manifest = (
            inst_cfg.get("instruments", {}).get("paths", {}).get("subscription_manifest_json")
            if data_dir == "data"
            else None
        ) or os.path.join(manifest_dir, "subscription_manifest.json")

    def sync_copy(src: str, dst: str) -> None:
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            shutil.copy2(src, dst)

    def resolve_existing_or_canonical(dated_p: str, canon_p: str, min_bytes: int = 1) -> Optional[str]:
        if os.path.exists(dated_p) and os.path.getsize(dated_p) >= min_bytes:
            return dated_p
        if os.path.exists(canon_p) and os.path.getsize(canon_p) >= min_bytes:
            sync_copy(canon_p, dated_p)
            return dated_p
        return None

    _logger.info(f"Pre-market start | Date={target_date} | Exchange={target_exchange} | Reset={reset} | Steps={selected}")
    step_results: Dict[str, Any] = {}
    errors: List[str] = []

    master_path = resolve_existing_or_canonical(dated_master, canonical_master, min_bytes=100_000) if not reset else None
    filtered_path = resolve_existing_or_canonical(dated_filtered, canonical_filtered, min_bytes=1000) if not reset else None
    volume_path = resolve_existing_or_canonical(dated_index, canonical_index, min_bytes=100) if not reset else None
    manifest_path = None

    if "master" in selected:
        if not reset and master_path:
            _logger.info(f"[master] Reusing cached instrument master: {master_path}")
        else:
            master_path = run_master_fetcher(force_download=reset, target_csv=dated_master)
        if not master_path or not os.path.exists(master_path):
            msg = "Step 'master' failed: instrument master not available."
            _logger.error(msg)
            errors.append(msg)
            step_results["master"] = {"status": "error", "error": msg}
            return {"status": "error", "session_date": target_date, "exchange": target_exchange, "steps": step_results, "errors": errors}
        sync_copy(master_path, canonical_master)
        step_results["master"] = {"status": "ok", "path": master_path, "canonical": canonical_master}

    if "filter" in selected:
        src_master = master_path or (dated_master if os.path.exists(dated_master) else canonical_master)
        if not reset and filtered_path:
            _logger.info(f"[filter] Reusing cached filtered universe: {filtered_path}")
        else:
            filtered_path = run_instrument_filter(
                master_csv=src_master,
                output_csv=dated_filtered,
                target_exchange=target_exchange,
            )
        if not filtered_path or not os.path.exists(filtered_path):
            msg = f"Step 'filter' failed for exchange {target_exchange}."
            _logger.error(msg)
            errors.append(msg)
            step_results["filter"] = {"status": "error", "error": msg}
            return {"status": "error", "session_date": target_date, "exchange": target_exchange, "steps": step_results, "errors": errors}
        sync_copy(filtered_path, canonical_filtered)
        step_results["filter"] = {"status": "ok", "path": filtered_path, "canonical": canonical_filtered}

    if "volume" in selected:
        src_filtered = filtered_path or (dated_filtered if os.path.exists(dated_filtered) else canonical_filtered)
        if not reset and not os.path.exists(dated_index) and os.path.exists(canonical_index):
            sync_copy(canonical_index, dated_index)
        volume_path = run_volume_indexer(
            instruments_csv=src_filtered,
            output_json=dated_index,
            days=int(volume_days),
            skip_if_exists=not reset,
        )
        if not volume_path or not os.path.exists(volume_path):
            msg = "Step 'volume' failed: volume index build failed."
            _logger.error(msg)
            errors.append(msg)
            step_results["volume"] = {"status": "error", "error": msg}
            return {"status": "error", "session_date": target_date, "exchange": target_exchange, "steps": step_results, "errors": errors}
        sync_copy(volume_path, canonical_index)
        step_results["volume"] = {"status": "ok", "path": volume_path, "canonical": canonical_index}

    if "manifest" in selected:
        src_master = master_path or (dated_master if os.path.exists(dated_master) else canonical_master)
        src_filtered = filtered_path or (dated_filtered if os.path.exists(dated_filtered) else canonical_filtered)
        src_index = volume_path or (dated_index if os.path.exists(dated_index) else canonical_index)
        pop_strikes = inst_cfg.get("instruments", {}).get("options_subscription", {}).get("popular_strikes_above_atm", 3)
        max_opt_und = (
            sys_cfg.get("system", {}).get("manifest", {}).get("max_stream_underlyings_with_options")
            or sys_cfg.get("system", {}).get("feed", {}).get("max_stream_underlyings_with_options", 110)
        )
        manifest_path = resolve_existing_or_canonical(dated_manifest, canonical_manifest, min_bytes=10_000) if not reset else None
        if not reset and manifest_path:
            _logger.info(f"[manifest] Reusing cached subscription manifest: {manifest_path}")
        else:
            manifest_path = run_manifest_generator(
                filtered_csv=src_filtered,
                volume_index_json=src_index,
                master_csv=src_master,
                output_json=dated_manifest,
                popular_strikes=pop_strikes,
                max_stream_underlyings_with_options=int(max_opt_und),
                session_date=target_date,
            )
        if not manifest_path or not os.path.exists(manifest_path):
            msg = "Step 'manifest' failed: subscription manifest generation failed."
            _logger.error(msg)
            errors.append(msg)
            step_results["manifest"] = {"status": "error", "error": msg}
            return {"status": "error", "session_date": target_date, "exchange": target_exchange, "steps": step_results, "errors": errors}
        sync_copy(manifest_path, canonical_manifest)
        step_results["manifest"] = {"status": "ok", "path": manifest_path, "canonical": canonical_manifest}

    if "cleanup" in selected:
        try:
            pruned = prune_old_preparation_files(keep_session_date=target_date, data_dir=data_dir)
            prune_debug_log(os.path.join(exec_logs_dir, "debug.log"))
            step_results["cleanup"] = {
                "status": "ok",
                "pruned_count": len(pruned),
                "freed_mb": round(sum(sz for _, sz in pruned) / (1024 * 1024), 2),
            }
        except Exception as e:
            _logger.warning(f"[cleanup] Maintenance warning: {e}")
            step_results["cleanup"] = {"status": "warning", "error": str(e)}

    _logger.info(f"Pre-market completed ({target_exchange} | Date={target_date} | Steps={list(step_results.keys())})")
    return {
        "status": "ok",
        "session_date": target_date,
        "exchange": target_exchange,
        "reset": reset,
        "steps": step_results,
        "errors": errors,
    }


def run_post_market(
    actions: Optional[List[str]] = None,
    session_date: Optional[str] = None,
    record_dir: str = "recordings",
    keep_raw: bool = True,
    archive_all: bool = False,
    data_dir: str = "data",
    logs_dir: str = "logs",
) -> Dict[str, Any]:
    """
    Execute selected post-market housekeeping actions (`archive`, `prune`, `rotate_logs`, `report`).
    """
    from core.instruments.cleanup import prune_old_preparation_files
    from core.auth.upstox_auth import prune_debug_log

    target_date = session_date or datetime.now(tz=IST).strftime("%Y%m%d")
    selected = [a.strip().lower() for a in (actions or ["archive", "prune", "rotate_logs"]) if str(a).strip()]
    invalid = [a for a in selected if a not in ALL_POSTMARKET_ACTIONS]
    if invalid:
        raise ValueError(f"Unknown post-market action(s): {', '.join(invalid)}")

    _logger.info(f"Post-market start | Date={target_date} | Actions={selected} | KeepRaw={keep_raw} | All={archive_all}")
    action_results: Dict[str, Any] = {}
    errors: List[str] = []

    if "archive" in selected:
        if not os.path.exists(record_dir):
            msg = f"Recording directory not found: {record_dir}"
            _logger.error(msg)
            errors.append(msg)
            action_results["archive"] = {"status": "error", "error": msg}
        else:
            targets_to_archive: List[Tuple[str, str, str]] = []
            if archive_all:
                for item in sorted(os.listdir(record_dir)):
                    item_path = os.path.join(record_dir, item)
                    if os.path.isdir(item_path) and re.match(r"^\d{8}$", item):
                        targets_to_archive.append(("dir", item, item_path))
                    elif os.path.isfile(item_path) and re.match(r"^session_\d{8}_market\.jsonl$", item):
                        dt = item.split("_")[1]
                        targets_to_archive.append(("legacy_file", dt, item_path))
            else:
                folder_path = os.path.join(record_dir, target_date)
                legacy_file = os.path.join(record_dir, f"session_{target_date}_market.jsonl")
                if os.path.isdir(folder_path):
                    targets_to_archive.append(("dir", target_date, folder_path))
                elif os.path.isfile(legacy_file):
                    targets_to_archive.append(("legacy_file", target_date, legacy_file))

            if not targets_to_archive and not archive_all:
                archive_path = os.path.join(record_dir, f"{target_date}.tar.gz")
                if os.path.exists(archive_path):
                    _logger.info(f"Session {target_date} already archived at {archive_path}")
                    action_results["archive"] = {
                        "status": "already_archived",
                        "archive_path": archive_path,
                        "sessions_processed": 0,
                    }
                else:
                    msg = f"No uncompressed recording found for session date '{target_date}' in '{record_dir}'."
                    _logger.warning(msg)
                    errors.append(msg)
                    action_results["archive"] = {"status": "error", "error": msg}
            else:
                total_raw_bytes = 0
                total_comp_bytes = 0
                archives_created: List[str] = []
                for t_type, dt, t_path in targets_to_archive:
                    if t_type == "dir":
                        archive_path = os.path.join(record_dir, f"{dt}.tar.gz")
                        raw_bytes = sum(
                            os.path.getsize(os.path.join(root, f))
                            for root, _, files in os.walk(t_path)
                            for f in files
                        )
                        with tarfile.open(archive_path, "w:gz") as tar:
                            tar.add(t_path, arcname=dt)
                        comp_bytes = os.path.getsize(archive_path)
                        total_raw_bytes += raw_bytes
                        total_comp_bytes += comp_bytes
                        archives_created.append(archive_path)
                        if not keep_raw:
                            shutil.rmtree(t_path)
                    else:
                        archive_path = os.path.join(record_dir, f"session_{dt}_market.tar.gz")
                        raw_bytes = os.path.getsize(t_path)
                        with tarfile.open(archive_path, "w:gz") as tar:
                            tar.add(t_path, arcname=os.path.basename(t_path))
                        comp_bytes = os.path.getsize(archive_path)
                        total_raw_bytes += raw_bytes
                        total_comp_bytes += comp_bytes
                        archives_created.append(archive_path)
                        if not keep_raw:
                            os.remove(t_path)
                action_results["archive"] = {
                    "status": "ok",
                    "sessions_processed": len(targets_to_archive),
                    "archives_created": archives_created,
                    "raw_mb": round(total_raw_bytes / (1024 * 1024), 2),
                    "archive_mb": round(total_comp_bytes / (1024 * 1024), 2),
                    "saved_mb": round((total_raw_bytes - total_comp_bytes) / (1024 * 1024), 2),
                    "keep_raw": keep_raw,
                }

    if "prune" in selected:
        try:
            pruned = prune_old_preparation_files(keep_session_date=target_date, data_dir=data_dir)
            action_results["prune"] = {
                "status": "ok",
                "pruned_count": len(pruned),
                "freed_mb": round(sum(sz for _, sz in pruned) / (1024 * 1024), 2),
            }
        except Exception as e:
            _logger.warning(f"Prune step failed: {e}")
            action_results["prune"] = {"status": "error", "error": str(e)}
            errors.append(str(e))

    if "rotate_logs" in selected:
        try:
            dbg_path = os.path.join(logs_dir, "exec_logs", "debug.log")
            if not os.path.exists(dbg_path) and os.path.exists(os.path.join(logs_dir, "debug.log")):
                dbg_path = os.path.join(logs_dir, "debug.log")
            prune_debug_log(dbg_path)
            action_results["rotate_logs"] = {"status": "ok"}
        except Exception as e:
            _logger.warning(f"Log rotation failed: {e}")
            action_results["rotate_logs"] = {"status": "error", "error": str(e)}
            errors.append(str(e))

    if "report" in selected:
        try:
            from web.historical_service import HistoricalService
            canonical_date = (
                f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
                if len(target_date) == 8 and target_date.isdigit()
                else target_date
            )
            hs = HistoricalService(recordings_dir=record_dir)
            overview = hs.get_overview(target_date=canonical_date)
            action_results["report"] = {
                "status": "ok",
                "date": canonical_date,
                "summary": overview.get("summary", {}),
                "modes": list((overview.get("modes") or {}).keys()),
            }
        except Exception as e:
            _logger.warning(f"EOD report summary failed: {e}")
            action_results["report"] = {"status": "error", "error": str(e)}
            errors.append(str(e))

    overall_status = "error" if (errors and len(errors) == len(selected)) else ("partial" if errors else "ok")
    return {
        "status": overall_status,
        "session_date": target_date,
        "actions": action_results,
        "errors": errors,
    }
