"""
Simulation Service: Manages configuration composition, dataset caching, and asynchronous simulation execution.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
import polars as pl

from core.common.time_utils import IST
from simulation.columnar_sim_engine import ColumnarSimEngine


class SimulationService:
    def __init__(self, recordings_dir: str = "recordings"):
        self.recordings_dir = recordings_dir
        self.executor = ThreadPoolExecutor(max_workers=2)

        # In-memory parquet cache
        self.cached_date: Optional[str] = None
        self.cached_equity_df: Optional[pl.DataFrame] = None
        self.cached_options_df: Optional[pl.DataFrame] = None

        # Execution state
        self.is_running: bool = False
        self.current_job_id: str = ""
        self.progress_status: str = "Idle"
        self.last_run_time: float = 0.0
        self.latest_results: Optional[Dict[str, Any]] = None
        self.latest_error: Optional[str] = None

    def get_simulation_dates(self) -> List[Dict[str, Any]]:
        """List recording dates that contain valid equity & option recording files."""
        dates = []
        if os.path.exists(self.recordings_dir):
            for entry in sorted(os.listdir(self.recordings_dir), reverse=True):
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

        # Deduplicate
        unique_dates = []
        seen = set()
        for d in dates:
            if d["date"] not in seen:
                seen.add(d["date"])
                unique_dates.append(d)

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
        Run simulation asynchronously in thread pool with custom parameter overrides and composer tracks.
        """
        if self.is_running:
            raise RuntimeError("A simulation is already in progress.")

        self.is_running = True
        self.latest_error = None
        job_id = f"sim_{int(time.time() * 1000)}"
        self.current_job_id = job_id
        start_ts = time.time()

        try:
            target_date = params.get("date") or "2026-11-19"
            loop = asyncio.get_running_loop()

            # Run in worker thread
            results = await loop.run_in_executor(
                self.executor,
                self._execute_simulation_sync,
                target_date,
                params,
            )

            self.latest_results = results
            self.last_run_time = time.time() - start_ts
            self.progress_status = "Completed"
            return results

        except Exception as e:
            self.latest_error = str(e)
            self.progress_status = f"Failed: {e}"
            raise e
        finally:
            self.is_running = False

    def _execute_simulation_sync(self, target_date: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Synchronous execution inside worker thread."""
        self._ensure_cache(target_date)
        self.progress_status = f"Executing simulation across modes for {target_date}..."

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
            cached_equity_df=self.cached_equity_df,
            cached_options_df=self.cached_options_df,
        )

        matrix_results = engine.run_simulation()

        # Format closed trades & curves per mode & track
        trade_details_by_mode: Dict[str, Dict[str, Any]] = {}
        for m in modes:
            trade_details_by_mode[m] = {}
            track_trades: Dict[str, List[Dict[str, Any]]] = {}
            for tr_obj in engine.closed_trades.get(m, []):
                tr_dict = tr_obj.to_dict()
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
                    tr_copy["entry_time_str"] = datetime.fromtimestamp(e_ts, tz=IST).strftime("%H:%M:%S") if e_ts > 0 else "-"
                    tr_copy["exit_time_str"] = datetime.fromtimestamp(x_ts, tz=IST).strftime("%H:%M:%S") if x_ts > 0 else "-"

                    entry_p = float(tr.get("entry_price", 0.0))
                    exit_p = float(tr.get("exit_price") or entry_p)
                    pnl = float(tr.get("realized_pnl", 0.0))
                    ret_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0
                    fee = float(tr.get("metadata", {}).get("brokerage", 2.0 * brokerage))

                    tr_copy["pnl"] = round(pnl, 2)
                    tr_copy["return_pct"] = round(ret_pct * 100.0, 2)
                    tr_copy["brokerage"] = round(fee, 2)
                    tr_copy["net_pnl"] = round(pnl - fee, 2)
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
        """Cumulative realized PnL curve plotted across time."""
        if not trades:
            return []

        closed_trades = [tr for tr in trades if float(tr.get("exit_time") or 0.0) > 0]
        if not closed_trades:
            return []

        closed_sorted = sorted(closed_trades, key=lambda t: float(t["exit_time"]))
        curve = []
        cum_pnl = 0.0

        first_ts = float(closed_sorted[0]["exit_time"])
        curve.append({
            "time": datetime.fromtimestamp(first_ts - 60.0, tz=IST).strftime("%H:%M:%S"),
            "timestamp": round(first_ts - 60.0, 1),
            "pnl": 0.0,
        })

        for tr in closed_sorted:
            x_ts = float(tr["exit_time"])
            net_pnl = float(tr.get("net_pnl", 0.0))
            cum_pnl += net_pnl
            curve.append({
                "time": datetime.fromtimestamp(x_ts, tz=IST).strftime("%H:%M:%S"),
                "timestamp": round(x_ts, 1),
                "pnl": round(cum_pnl, 2),
            })

        return curve
