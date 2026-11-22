"""
Simulation Service: Manages configuration composition, dataset caching, and asynchronous simulation execution.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import multiprocessing as mp
import os
import pickle
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import polars as pl

from core.common.time_utils import IST
from simulation.columnar_sim_engine import ColumnarSimEngine
from web.candle_service import CandleService


def _safe_format_ts(ts: Optional[float]) -> str:
    """Format Unix timestamp to HH:MM:SS string safely handling zeroes and invalid inputs."""
    if ts is None or ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts), tz=IST).strftime("%H:%M:%S")
    except Exception:
        return "-"


def _simulation_worker_process(
    queue: Any,
    result_file: str,
    target_date: str,
    params: Dict[str, Any],
    recordings_dir: str,
) -> None:
    """
    Independent worker process entrypoint.
    Executes in a separate OS process with its own memory space and GIL.
    Ensures web server event loop remains completely responsive.
    """
    try:
        def progress_cb(status: str) -> None:
            try:
                queue.put_nowait({"type": "progress", "status": status})
            except Exception:
                pass

        sim_svc = SimulationService(recordings_dir=recordings_dir)
        results = sim_svc._execute_simulation_sync(
            target_date=target_date,
            params=params,
            progress_cb=progress_cb,
        )

        with open(result_file, "wb") as f:
            pickle.dump(results, f, protocol=pickle.HIGHEST_PROTOCOL)

        try:
            queue.put({"type": "done", "file": result_file}, timeout=2.0)
            time.sleep(0.05)
        except Exception:
            pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            queue.put({"type": "error", "error": str(e), "traceback": tb}, timeout=2.0)
            time.sleep(0.05)
        except Exception:
            pass


class SimulationService:
    def __init__(self, recordings_dir: str = "recordings"):
        self.recordings_dir = recordings_dir
        self.executor = ThreadPoolExecutor(max_workers=2)
        self._current_process: Optional[mp.Process] = None
        self._current_task: Optional[asyncio.Task] = None
        self.candle_service = CandleService(recordings_dir=recordings_dir)

        # In-memory parquet cache
        self.cached_date: Optional[str] = None
        self.cached_equity_df: Optional[pl.DataFrame] = None
        self.cached_options_df: Optional[pl.DataFrame] = None

        # Execution state
        self._is_running: bool = False
        self.current_job_id: str = ""
        self.progress_status: str = "Idle"
        self.last_run_time: float = 0.0
        self.latest_results: Optional[Dict[str, Any]] = None
        self.latest_params: Optional[Dict[str, Any]] = None
        self.latest_error: Optional[str] = None
        self.runs_history: Dict[str, Dict[str, Any]] = {}
        # Parameter hash deduplication cache: param_hash -> results dict
        self.cache_by_param_hash: Dict[str, Dict[str, Any]] = {}

    @property
    def is_running(self) -> bool:
        if self._is_running:
            # Auto-heal stale is_running state if task has finished and worker process is no longer alive
            task_done = self._current_task is not None and self._current_task.done()
            proc_dead = self._current_process is None or not self._current_process.is_alive()
            if task_done and proc_dead:
                self._is_running = False
        return self._is_running

    @is_running.setter
    def is_running(self, val: bool) -> None:
        self._is_running = bool(val)

    def cleanup(self) -> None:
        """
        Teardown all simulation state, worker threads, temp files, and cached run data.
        Ensures complete reset on server shutdown or queue clear.
        """
        self.is_running = False
        self.progress_status = "Idle"
        self.current_job_id = ""
        self.latest_results = None
        self.latest_params = None
        self.latest_error = None
        self.last_run_time = 0.0

        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        self._current_task = None

        # Terminate running worker process if active
        if self._current_process and self._current_process.is_alive():
            try:
                self._current_process.terminate()
                self._current_process.join(timeout=0.5)
                if self._current_process.is_alive():
                    self._current_process.kill()
            except Exception:
                pass
        self._current_process = None

        # Clear in-memory datasets and history
        self.runs_history.clear()
        self.cache_by_param_hash.clear()
        self.cached_equity_df = None
        self.cached_options_df = None
        self.cached_date = None

        # Safely terminate executor worker threads
        if self.executor:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self.executor = ThreadPoolExecutor(max_workers=2)

        # Clean up shared memory segments and temporary simulation files
        self._cleanup_temp_files_and_shm()

    def _cleanup_temp_files_and_shm(self) -> None:
        """Remove simulation shared memory segments and lingering temp files."""
        from core.candles.shm_layout import unlink_shm
        for shm_name in ["shm_sim_ohlc", "shm_scoring_ohlc"]:
            try:
                unlink_shm(shm_name)
            except Exception:
                pass

        # Clean up any lingering temporary shm files and result pickles
        try:
            import glob
            import tempfile
            patterns = [
                os.path.join(tempfile.gettempdir(), "*sim*.shm"),
                os.path.join(tempfile.gettempdir(), "*scoring*.shm"),
                os.path.join(tempfile.gettempdir(), "sim_res_*.pkl"),
                "/tmp/*sim*.shm",
                "/tmp/*scoring*.shm",
                "/tmp/sim_res_*.pkl",
            ]
            for pat in patterns:
                for fpath in glob.glob(pat):
                    try:
                        if os.path.exists(fpath):
                            os.remove(fpath)
                    except Exception:
                        pass
        except Exception:
            pass

    def compute_param_hash(self, target_date: str, params: Dict[str, Any]) -> str:
        """Compute deterministic SHA256 hash of simulation parameters for deduplication."""
        norm_params = {
            "date": target_date,
            "global_settings": params.get("global_settings", {}),
            "base_tracks": params.get("base_tracks", {}),
            "composite_tracks": params.get("composite_tracks", []),
            "custom_tracks": params.get("custom_tracks", []),
        }
        canonical_str = json.dumps(norm_params, sort_keys=True)
        return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored simulation run details by ID, or latest run if blank/latest."""
        if run_id in self.runs_history:
            return self.runs_history[run_id]
        if run_id in ("latest", "current", "") and self.latest_results:
            return {
                "id": self.current_job_id or "RUN-latest",
                "date": self.latest_results.get("date", ""),
                "params": self.latest_params or {},
                "results": self.latest_results,
                "runtime": round(self.last_run_time, 2),
                "timestamp": datetime.now(IST).strftime("%H:%M:%S"),
            }
        return None

    def get_simulation_dates(self) -> List[Dict[str, Any]]:
        """List recording dates that contain valid equity & option recording files (chronological order)."""
        dates = []
        if os.path.exists(self.recordings_dir):
            for entry in sorted(os.listdir(self.recordings_dir), reverse=False):
                full_path = os.path.join(self.recordings_dir, entry)
                if not os.path.isdir(full_path):
                    continue
                # Normalize date
                norm_date = entry
                if len(entry) == 8 and entry.isdigit():
                    norm_date = f"{entry[:4]}-{entry[4:6]}-{entry[6:8]}"
                elif not ("-" in entry and len(entry) == 10):
                    continue

                has_eq = False
                has_opt = False
                files = os.listdir(full_path)
                for f in files:
                    if "equity_ticks" in f or "market_events" in f:
                        has_eq = True
                    if "option_depth" in f:
                        has_opt = True

                if has_eq or has_opt:
                    dates.append({
                        "date": norm_date,
                        "raw_dir": entry,
                        "is_cached": (self.cached_date == norm_date),
                    })

        # Deduplicate and sort chronologically (ascending)
        unique_dates = []
        seen = set()
        for d in dates:
            if d["date"] not in seen:
                seen.add(d["date"])
                unique_dates.append(d)

        unique_dates.sort(key=lambda d: d["date"])
        return unique_dates

    def get_default_config(self) -> Dict[str, Any]:
        """Load default parameters from config/strategies.json and system.json."""
        strat_path = "config/strategies.json"
        raw_cfg = {}
        if os.path.exists(strat_path):
            with open(strat_path, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)

        momentum = raw_cfg.get("strategies", {}).get("momentum", {})
        tracks_cfg = momentum.get("tracks", {})
        enabled_list = momentum.get("enabled_tracks", [])

        # Extract base tracks configuration
        base_tracks = {
            "fast_momentum": {
                "target_pct": float(tracks_cfg.get("fast_momentum", {}).get("target_pct", 0.15)),
                "sl_pct": float(tracks_cfg.get("fast_momentum", {}).get("sl_pct", 0.05)),
                "target_decay_sec": int(tracks_cfg.get("fast_momentum", {}).get("target_decay_sec", 900)),
                "decay_floor_pct": float(tracks_cfg.get("fast_momentum", {}).get("decay_floor_pct", 0.025)),
                "stagnation_timeout_sec": int(tracks_cfg.get("fast_momentum", {}).get("stagnation_timeout_sec", 1800)),
                "breakeven_trigger_pct": float(tracks_cfg.get("fast_momentum", {}).get("breakeven_trigger_pct", 0.025)),
                "min_pole_pct": float(tracks_cfg.get("fast_momentum", {}).get("min_pole_pct", 0.005)),
                "modes": ["ITM", "ATM", "OTM"],
                "enabled": True,
            },
            "trend_pullback": {
                "target_pct": float(tracks_cfg.get("trend_pullback", {}).get("target_pct", 0.25)),
                "sl_pct": float(tracks_cfg.get("trend_pullback", {}).get("sl_pct", 0.05)),
                "trailing_step_pct": float(tracks_cfg.get("trend_pullback", {}).get("trailing_step_pct", 0.01)),
                "timeout_sec": float(tracks_cfg.get("trend_pullback", {}).get("timeout_sec", 3600.0)),
                "breakeven_trigger_pct": float(tracks_cfg.get("trend_pullback", {}).get("breakeven_trigger_pct", 0.025)),
                "modes": ["ITM", "ATM", "OTM"],
                "enabled": True,
            },
            "fixed_target": {
                "target_pct": float(tracks_cfg.get("fixed_target", {}).get("target_pct", 0.15)),
                "sl_pct": float(tracks_cfg.get("fixed_target", {}).get("sl_pct", 0.05)),
                "fixed_target_inr": float(tracks_cfg.get("fixed_target", {}).get("fixed_target_inr", 750.0)),
                "timeout_sec": float(tracks_cfg.get("fixed_target", {}).get("timeout_sec", 1800.0)),
                "breakeven_trigger_pct": float(tracks_cfg.get("fixed_target", {}).get("breakeven_trigger_pct", 0.025)),
                "modes": ["ITM", "ATM", "OTM"],
                "enabled": True,
            },
        }

        # Extract composite tracks
        composite_tracks = []
        for entry in enabled_list:
            if isinstance(entry, dict) and entry.get("type") == "composite":
                composite_tracks.append({
                    "name": entry.get("name"),
                    "shared_pool": bool(entry.get("shared_pool", True)),
                    "composition": entry.get("composition", []),
                    "enabled": True,
                })

        available_dates = self.get_simulation_dates()
        default_date = available_dates[0]["date"] if available_dates else "2026-11-19"

        return {
            "selected_date": default_date,
            "cached_date": self.cached_date,
            "available_dates": available_dates,
            "global_settings": {
                "capital": 250000.0,
                "modes": ["paper", "paper_delayed"],
                "min_delay_ms": 200.0,
                "max_delay_ms": 500.0,
                "brokerage_per_order": float(momentum.get("brokerage_per_order", 30.0)),
                "min_pole_pct": float(momentum.get("scoring_engine_3_0", {}).get("min_pole_pct", 0.005)),
                "entry_freeze_time": momentum.get("entry_freeze_time", "15:00"),
                "eod_squareoff_time": momentum.get("eod_squareoff_time", "15:15"),
            },
            "base_tracks": base_tracks,
            "composite_tracks": composite_tracks,
            "custom_tracks": [],
        }

    def _resolve_data_path(self, target_date: str) -> str:
        """Resolve actual folder path for date."""
        cand_hyphen = os.path.join(self.recordings_dir, target_date)
        if os.path.exists(cand_hyphen):
            return cand_hyphen
        cand_flat = os.path.join(self.recordings_dir, target_date.replace("-", ""))
        if os.path.exists(cand_flat):
            return cand_flat
        return cand_hyphen

    def _ensure_cache(self, target_date: str) -> None:
        """Load and cache parquet data for target date if not already cached."""
        if self.cached_date == target_date and self.cached_equity_df is not None:
            return

        self.progress_status = f"Ingesting and caching market dataset for {target_date}..."
        data_path = self._resolve_data_path(target_date)

        temp_engine = ColumnarSimEngine(
            data_path=data_path,
            session_date=target_date,
            modes=["paper"],
        )
        df_eq, df_opt = temp_engine.load_data()

        self.cached_date = target_date
        self.cached_equity_df = df_eq
        self.cached_options_df = df_opt

    async def run_simulation_job(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run simulation asynchronously in a dedicated worker process with custom parameter overrides.
        Process isolation guarantees 0% GIL contention with the web server event loop.
        Supports instant parameter-hash deduplication for identical configurations.
        """
        cur_task = asyncio.current_task()
        if self._is_running:
            # Check if another task or worker process is actively running
            other_task_active = (
                self._current_task is not None
                and not self._current_task.done()
                and self._current_task is not cur_task
            )
            proc_active = self._current_process is not None and self._current_process.is_alive()
            if other_task_active or proc_active:
                raise RuntimeError("A simulation is already in progress.")

        self._current_task = cur_task
        self.is_running = True
        result_file = None
        process = None
        queue = None

        try:
            # Canonicalize target date to YYYY-MM-DD
            raw_date = params.get("date") or "2026-11-19"
            if len(raw_date) == 8 and raw_date.isdigit():
                target_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            else:
                target_date = raw_date

            self.latest_error = None
            # Use passed run_id if available, else sequential RUN-N
            req_id = params.get("run_id")
            job_id = req_id if req_id else f"RUN-{len(self.runs_history) + 1}"
            self.current_job_id = job_id
            self.latest_results = None
            self.progress_status = "Initializing simulation..."
            start_ts = time.time()
            param_hash = self.compute_param_hash(target_date, params)

            # Check parameter-hash deduplication cache
            if param_hash in self.cache_by_param_hash:
                cached_res = self.cache_by_param_hash[param_hash]
                self.latest_results = cached_res
                self.latest_params = params
                self.last_run_time = 0.01
                self.progress_status = "Completed (Cached)"
                self.runs_history[job_id] = {
                    "id": job_id,
                    "date": target_date,
                    "params": params,
                    "results": cached_res,
                    "runtime": 0.01,
                    "cached": True,
                    "timestamp": datetime.now(IST).strftime("%H:%M:%S"),
                }
                return cached_res

            temp_fd, result_file = tempfile.mkstemp(prefix=f"sim_res_{job_id}_", suffix=".pkl")
            os.close(temp_fd)

            ctx = mp.get_context("spawn")
            queue = ctx.Queue()

            process = ctx.Process(
                target=_simulation_worker_process,
                args=(queue, result_file, target_date, params, self.recordings_dir),
                daemon=True,
            )
            self._current_process = process
            process.start()

            results = None
            while process.is_alive() or not queue.empty():
                while True:
                    try:
                        msg = queue.get_nowait()
                    except Exception:
                        break

                    mtype = msg.get("type")
                    if mtype == "progress":
                        self.progress_status = msg.get("status", self.progress_status)
                    elif mtype == "error":
                        err = msg.get("error", "Simulation worker error")
                        raise RuntimeError(err)
                    elif mtype == "done":
                        res_path = msg.get("file", result_file)
                        if os.path.exists(res_path):
                            with open(res_path, "rb") as f:
                                results = pickle.load(f)
                            try:
                                os.remove(res_path)
                            except Exception:
                                pass

                if results is not None:
                    break

                if not process.is_alive():
                    if process.exitcode != 0 and results is None:
                        raise RuntimeError(f"Simulation worker process crashed with exit code {process.exitcode}")
                    break

                await asyncio.sleep(0.1)

            if results is None and result_file and os.path.exists(result_file):
                try:
                    with open(result_file, "rb") as f:
                        results = pickle.load(f)
                    os.remove(result_file)
                except Exception:
                    pass

            if results is None:
                raise RuntimeError("Simulation finished without producing results.")

            # Store in deduplication cache
            self.cache_by_param_hash[param_hash] = results

            self.latest_results = results
            self.latest_params = params
            self.last_run_time = time.time() - start_ts
            self.progress_status = "Completed"

            # Save in history for multi-run inspection and clean tab views
            self.runs_history[job_id] = {
                "id": job_id,
                "date": target_date,
                "params": params,
                "results": results,
                "runtime": round(self.last_run_time, 2),
                "timestamp": datetime.now(IST).strftime("%H:%M:%S"),
            }
            return results

        except Exception as e:
            self.latest_error = str(e)
            self.progress_status = f"Failed: {e}"
            print(f"[SIMULATION_SERVICE] Simulation job error for {job_id}: {e}")
            return {"error": str(e), "status": "failed"}
        finally:
            if process and process.is_alive():
                try:
                    process.terminate()
                    process.join(timeout=0.5)
                    if process.is_alive():
                        process.kill()
                except Exception:
                    pass
            self._current_process = None
            if result_file and os.path.exists(result_file):
                try:
                    os.remove(result_file)
                except Exception:
                    pass
            if queue:
                try:
                    queue.close()
                    queue.cancel_join_thread()
                except Exception:
                    pass
            self.is_running = False
            if self._current_task is cur_task:
                self._current_task = None

    def _execute_simulation_sync(
        self,
        target_date: str,
        params: Dict[str, Any],
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Synchronous execution inside dedicated worker process."""
        if progress_cb:
            progress_cb(f"Ingesting and caching market dataset for {target_date}...")
        self._ensure_cache(target_date)

        msg = f"Executing simulation across modes for {target_date}..."
        self.progress_status = msg
        if progress_cb:
            progress_cb(msg)

        data_path = self._resolve_data_path(target_date)
        glob = params.get("global_settings", {})
        modes = glob.get("modes") or ["paper", "paper_delayed"]
        capital = float(glob.get("capital", 250000.0))
        min_delay_ms = float(glob.get("min_delay_ms", 200.0))
        max_delay_ms = float(glob.get("max_delay_ms", 500.0))
        brokerage = float(glob.get("brokerage_per_order", 30.0))
        min_pole_pct = float(glob.get("min_pole_pct", 0.005))
        freeze_time = glob.get("entry_freeze_time", "15:00")
        squareoff_time = glob.get("eod_squareoff_time", "15:15")

        # Compose strategy configuration dictionary
        strategy_cfg = self._build_custom_strategy_cfg(params)

        engine = ColumnarSimEngine(
            data_path=data_path,
            modes=modes,
            capital=capital,
            min_delay_ms=min_delay_ms,
            max_delay_ms=max_delay_ms,
            brokerage_per_order=brokerage,
            min_pole_pct=min_pole_pct,
            entry_freeze_time=freeze_time,
            eod_squareoff_time=squareoff_time,
            session_date=target_date,
            strategy_cfg=strategy_cfg,
            shm_name=f"shm_sim_ohlc_{os.getpid()}",
            cached_equity_df=self.cached_equity_df,
            cached_options_df=self.cached_options_df,
        )

        try:
            matrix_results = engine.run_simulation()
        finally:
            from core.candles.shm_layout import unlink_shm
            unlink_shm(engine.shm_name)

        if progress_cb:
            progress_cb("Precomputing chart candles for closed trades...")

        # Precompute candles ONCE for unique underlyings and options across all closed trades
        unique_underlyings = set()
        unique_options = set()
        for m in modes:
            for tr_obj in engine.closed_trades.get(m, []):
                tr_dict = tr_obj.to_dict() if hasattr(tr_obj, "to_dict") else tr_obj
                tr_sym = str(tr_dict.get("symbol", "")).strip()
                tr_tok = str(tr_dict.get("token", "")).strip()
                meta = tr_dict.get("metadata") or {}
                u_tok = str(meta.get("underlying_token", "") or f"NSE:{tr_sym}").strip()
                opt_sym = str(meta.get("option_symbol", "") or tr_sym).strip()
                if tr_sym:
                    unique_underlyings.add((u_tok, tr_sym))
                if tr_tok:
                    unique_options.add((tr_tok, opt_sym))

        candle_svc = CandleService(recordings_dir=self.recordings_dir)

        precomputed_underlyings: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for u_tok, tr_sym in unique_underlyings:
            try:
                precomputed_underlyings[(u_tok, tr_sym)] = candle_svc.get_candles(
                    target_date=target_date,
                    token=u_tok,
                    symbol=tr_sym,
                    is_option=False,
                    interval_minutes=5,
                    source_df=self.cached_equity_df,
                )
            except Exception:
                precomputed_underlyings[(u_tok, tr_sym)] = []

        precomputed_options: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for tr_tok, opt_sym in unique_options:
            try:
                precomputed_options[(tr_tok, opt_sym)] = candle_svc.get_candles(
                    target_date=target_date,
                    token=tr_tok,
                    symbol=opt_sym,
                    is_option=True,
                    interval_minutes=5,
                    source_df=self.cached_options_df,
                )
            except Exception:
                precomputed_options[(tr_tok, opt_sym)] = []

        # Format closed trades & curves per mode & track
        trade_details_by_mode: Dict[str, Dict[str, Any]] = {}
        for m in modes:
            trade_details_by_mode[m] = {}
            track_trades: Dict[str, List[Dict[str, Any]]] = {}
            for tr_obj in engine.closed_trades.get(m, []):
                tr_dict = tr_obj.to_dict() if hasattr(tr_obj, "to_dict") else tr_obj
                tr_name = tr_dict.get("track", "unknown")
                if tr_name not in track_trades:
                    track_trades[tr_name] = []
                track_trades[tr_name].append(tr_dict)

            for tr_name, tr_list in track_trades.items():
                formatted_trades = []
                for tr in sorted(tr_list, key=lambda t: float(t.get("entry_time") or 0.0)):
                    tr_copy = dict(tr)
                    e_ts = float(tr.get("entry_time") or 0.0)
                    x_ts = float(tr.get("exit_time") or 0.0)
                    tr_copy["entry_time_str"] = _safe_format_ts(e_ts)
                    tr_copy["exit_time_str"] = _safe_format_ts(x_ts)

                    entry_p = float(tr.get("entry_price", 0.0))
                    exit_p = float(tr.get("exit_price") or entry_p)
                    pnl = float(tr.get("realized_pnl", 0.0))
                    ret_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0
                    tr_meta = tr.get("metadata") or {}
                    fee = float(tr_meta.get("brokerage", 2.0 * brokerage))

                    tr_copy["pnl"] = round(pnl, 2)
                    tr_copy["return_pct"] = round(ret_pct * 100.0, 2)
                    tr_copy["brokerage"] = round(fee, 2)
                    tr_copy["net_pnl"] = round(pnl - fee, 2)
                    tr_copy["date"] = target_date

                    u_entry = (
                        tr.get("underlying_entry_price")
                        or tr_meta.get("underlying_entry_price")
                        or tr_meta.get("trigger_price")
                        or tr.get("trigger_price")
                    )
                    if u_entry is not None:
                        tr_copy["underlying_entry_price"] = float(u_entry)
                        tr_copy["trigger_price"] = float(u_entry)

                    # Format clean entry reason / archetype
                    raw_entry = (
                        tr.get("entry_reason")
                        or tr_meta.get("archetype")
                        or tr_meta.get("entry_reason")
                        or tr.get("trigger")
                        or "Breakout"
                    )
                    arch_map = {
                        "FLAG_BULL_BREAKOUT": "Flag Breakout",
                        "FLAG_BEAR_BREAKDOWN": "Flag Breakdown",
                        "PURE_BULL_BREAKOUT": "Pure Breakout",
                        "PURE_BEAR_BREAKDOWN": "Pure Breakdown",
                        "BULL_FLAG": "Flag Breakout",
                        "BEAR_FLAG": "Flag Breakdown",
                        "PURE_BREAKOUT": "Pure Breakout",
                        "PURE_BREAKDOWN": "Pure Breakdown",
                        "MOMENTUM_BREAKOUT": "Breakout",
                    }
                    disp_entry = arch_map.get(str(raw_entry), str(raw_entry))
                    tr_copy["entry_reason"] = disp_entry
                    tr_copy["raw_entry_reason"] = str(raw_entry)
                    tr_copy["trigger"] = disp_entry

                    # Attach pre-built 5-minute candles
                    tr_sym = str(tr.get("symbol", "")).strip()
                    tr_tok = str(tr.get("token", "")).strip()
                    u_tok = str(tr_meta.get("underlying_token", "") or f"NSE:{tr_sym}").strip()
                    opt_sym = str(tr_meta.get("option_symbol", "") or tr_sym).strip()

                    tr_copy["underlying_candles"] = precomputed_underlyings.get((u_tok, tr_sym), [])
                    tr_copy["option_candles"] = precomputed_options.get((tr_tok, opt_sym), [])

                    formatted_trades.append(tr_copy)

                curve = self._build_simulation_curve(formatted_trades)
                trade_details_by_mode[m][tr_name] = {
                    "trades": formatted_trades,
                    "curve": curve,
                }

        return {
            "date": target_date,
            "cached": True,
            "matrix": matrix_results,
            "details": trade_details_by_mode,
        }

    def _build_custom_strategy_cfg(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Construct strategy.json payload from client parameter overrides and composition."""
        base_tracks = params.get("base_tracks", {})
        custom_tracks = params.get("custom_tracks", [])
        composite_tracks = params.get("composite_tracks", [])

        enabled_spec: List[Dict[str, Any]] = []
        track_params_dict: Dict[str, Any] = {}

        # 1. Base tracks
        for base_name, b_cfg in base_tracks.items():
            if not b_cfg.get("enabled", True):
                continue
            modes = b_cfg.get("modes", ["ATM"])
            enabled_spec.append({
                "type": "base",
                "name": base_name,
                "modes": modes,
            })
            # Clean copy of numeric params
            p = dict(b_cfg)
            p.pop("modes", None)
            p.pop("enabled", None)
            track_params_dict[base_name] = p

        # 2. Custom tracks (composed by user)
        for c in custom_tracks:
            c_type = c.get("type", "base")
            c_name = c.get("name")
            if not c_name:
                continue

            if c_type == "base":
                arch = c.get("archetype", "fast_momentum")
                mode = c.get("mode", "ATM")
                enabled_spec.append({
                    "type": "base",
                    "name": arch,
                    "modes": [mode],
                })
                # If specific overrides given for custom track
                custom_p = dict(track_params_dict.get(arch, {}))
                for k in ["target_pct", "sl_pct", "trailing_step_pct", "timeout_sec", "fixed_target_inr"]:
                    if k in c and c[k] is not None:
                        custom_p[k] = float(c[k])
                track_params_dict[c_name] = custom_p
            elif c_type == "composite":
                enabled_spec.append({
                    "type": "composite",
                    "name": c_name,
                    "shared_pool": bool(c.get("shared_pool", True)),
                    "capital": float(c.get("capital", 250000.0)),
                    "composition": c.get("composition", []),
                })

        # 3. Enabled composites
        for comp in composite_tracks:
            if comp.get("enabled", True):
                enabled_spec.append({
                    "type": "composite",
                    "name": comp.get("name"),
                    "shared_pool": comp.get("shared_pool", True),
                    "composition": comp.get("composition", []),
                })

        glob = params.get("global_settings", {})

        return {
            "strategies": {
                "momentum": {
                    "enabled_tracks": enabled_spec,
                    "tracks": track_params_dict,
                    "brokerage_per_order": float(glob.get("brokerage_per_order", 30.0)),
                    "entry_freeze_time": glob.get("entry_freeze_time", "15:00"),
                    "eod_squareoff_time": glob.get("eod_squareoff_time", "15:15"),
                }
            }
        }

    def _build_simulation_curve(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Cumulative realized PnL curve plotted across time with net and gross values."""
        if not trades:
            return []

        closed_trades = [tr for tr in trades if float(tr.get("exit_time") or 0.0) > 0]
        if not closed_trades:
            return []

        closed_sorted = sorted(closed_trades, key=lambda t: float(t.get("exit_time") or 0.0))
        curve = []
        cum_net = 0.0
        cum_gross = 0.0

        first_ts = float(closed_sorted[0].get("exit_time") or 0.0)
        curve.append({
            "time": _safe_format_ts(first_ts - 60.0),
            "timestamp": round(first_ts - 60.0, 1),
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "pnl": 0.0,
        })

        for tr in closed_sorted:
            x_ts = float(tr.get("exit_time") or 0.0)
            net_pnl = float(tr.get("net_pnl", 0.0))
            gross_pnl = float(tr.get("pnl", 0.0) or tr.get("realized_pnl", 0.0))
            cum_net += net_pnl
            cum_gross += gross_pnl
            curve.append({
                "time": _safe_format_ts(x_ts),
                "timestamp": round(x_ts, 1),
                "net_pnl": round(cum_net, 2),
                "gross_pnl": round(cum_gross, 2),
                "pnl": round(cum_net, 2),
            })

        return curve
