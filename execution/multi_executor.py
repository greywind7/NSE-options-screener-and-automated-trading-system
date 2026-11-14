#!/usr/bin/env python3
"""
execution/multi_executor.py
===========================
Multi-Execution Coordinator:
Orchestrates simultaneous order routing across multiple execution modes:
- Paper Trading: Zero-broker offline simulation (logs/paper_trades.csv)
- Paper Delayed: Realistic 700ms-2.5s gateway latency with dynamic quote re-sampling (logs/paper_delayed_trades.csv)
- Live: Production Broker API v3 with safety gate (logs/live_trades.csv)

Enables real-time side-by-side comparison of execution latency, fill quality,
brokerage friction, and P&L across all modes in the strategy dashboard.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from execution.base import BaseExecutionAdapter
from execution.live.live_adapter import LiveExecutionAdapter
from execution.paper.paper_adapter import PaperExecutionAdapter
from execution.paper.paper_delayed_adapter import PaperDelayedExecutionAdapter
from infrastructure.common.time_utils import is_quote_fresh, normalize_time_str


class MultiExecutionManager:
    """
    Coordinates and broadcasts execution actions across multiple execution adapters.
    """

    def __init__(
        self,
        adapters: Optional[Dict[str, BaseExecutionAdapter]] = None,
        enforce_market_hours: bool = False,
        market_open_time: str = "09:15",
        market_close_time: str = "15:30",
        eod_freeze_time: str = "15:15",
        eod_square_off_time: str = "15:20",
        log_dir: str = "logs",
    ):
        self.adapters: Dict[str, BaseExecutionAdapter] = adapters or {}
        self.frozen_modes: Set[str] = set()
        self.enforce_market_hours = enforce_market_hours
        self.market_open_time = normalize_time_str(market_open_time, "09:15")
        self.market_close_time = normalize_time_str(market_close_time, "15:30")
        self.eod_freeze_time = normalize_time_str(eod_freeze_time, "15:15")
        self.eod_square_off_time = normalize_time_str(eod_square_off_time, "15:20")
        self.log_dir = log_dir

    def add_adapter(self, name: str, adapter: BaseExecutionAdapter) -> None:
        self.adapters[name.lower()] = adapter

    def get_adapter(self, name: str) -> Optional[BaseExecutionAdapter]:
        return self.adapters.get(name.lower())

    def set_quotes_cache(self, quotes_cache: Dict[str, Any]) -> None:
        """Propagate live quotes cache to adapters that support real-time re-sampling."""
        for adapter in self.adapters.values():
            if hasattr(adapter, "set_quotes_cache"):
                adapter.set_quotes_cache(quotes_cache)

    def freeze_mode(self, mode: str) -> None:
        """
        Freeze a specific execution mode (e.g. 'live', 'paper', 'paper_delayed', or 'all').
        Blocks any new entry orders from executing while allowing active positions
        to gradually exit according to targets and stop-losses.
        """
        m = mode.strip().lower()
        if m in ("all", "*"):
            self.frozen_modes.add("all")
            if self.adapters:
                self.frozen_modes.update(self.adapters.keys())
            else:
                self.frozen_modes.update(["live", "paper", "paper_delayed"])
        else:
            self.frozen_modes.add(m)
            # If all individual adapter modes are frozen, also mark 'all'
            all_known = set(self.adapters.keys()) if self.adapters else {"live", "paper", "paper_delayed"}
            if all_known and all_known.issubset(self.frozen_modes):
                self.frozen_modes.add("all")

    def unfreeze_mode(self, mode: str) -> None:
        """
        Unfreeze an execution mode to resume regular order entry.
        CRITICAL: If 'all' is currently frozen, unfreezing one mode (e.g. 'live')
        removes 'all' from the frozen list, keeps all OTHER modes in the frozen list,
        and unfreezes only the requested mode.
        """
        m = mode.strip().lower()
        if m in ("all", "*"):
            self.frozen_modes.clear()
        else:
            # If 'all' was frozen, unfreezing one mode breaks the 'all' umbrella:
            # 1. 'all' is removed from frozen_modes.
            # 2. All other known/active modes are explicitly added to frozen_modes.
            # 3. The specified mode is unfreezed (removed from frozen_modes).
            if "all" in self.frozen_modes:
                self.frozen_modes.discard("all")
                all_known = set(self.adapters.keys()) if self.adapters else {"live", "paper", "paper_delayed"}
                for other_mode in all_known:
                    if other_mode != m:
                        self.frozen_modes.add(other_mode)
            self.frozen_modes.discard(m)

    def is_frozen(self, mode: str) -> bool:
        """Check if an execution mode is frozen for new position entries."""
        m = mode.strip().lower()
        if "all" in self.frozen_modes or m in self.frozen_modes:
            return True
        # Direct disk verification as immediate defense-in-depth
        log_dir = getattr(self, "log_dir", "logs")
        if os.path.exists(os.path.join(log_dir, ".freeze_all")) or os.path.exists(os.path.join(log_dir, f".freeze_{m}")):
            return True
        if m in ("paper", "paper_delayed", "sim") and os.path.exists(os.path.join(log_dir, ".freeze_sim")):
            return True
        return False

    def check_freeze_flags(self, log_dir: Optional[str] = None) -> Set[str]:
        """
        Check for external freeze flag files created via CLI or file touch:
        e.g. logs/.freeze_live, logs/.freeze_paper, logs/.freeze_paper_delayed, logs/.freeze_all.
        Synchronizes internal in-memory frozen_modes with disk state:
        - If .freeze_all exists -> freezes 'all' and all sub-modes.
        - If .freeze_all is absent -> unfreezes 'all' and syncs individual modes.
        - If an individual .freeze_<mode> file was deleted -> unfreezes that mode.
        """
        if log_dir is None:
            log_dir = getattr(self, "log_dir", "logs")
        if not os.path.exists(log_dir):
            return self.frozen_modes

        all_flag = os.path.join(log_dir, ".freeze_all")
        known_modes = set(self.adapters.keys()) | {"live", "paper", "paper_delayed", "sim"}

        if os.path.exists(all_flag):
            self.freeze_mode("all")
            return self.frozen_modes

        # If .freeze_all is NOT on disk, ensure 'all' is removed from in-memory set
        if "all" in self.frozen_modes:
            self.frozen_modes.discard("all")

        # Synchronize individual flags
        for mode in known_modes:
            flag_file = os.path.join(log_dir, f".freeze_{mode}")
            if os.path.exists(flag_file):
                self.frozen_modes.add(mode)
            else:
                # Flag file was deleted -> unfreeze mode
                self.frozen_modes.discard(mode)

        return self.frozen_modes

    @property
    def active_modes(self) -> List[str]:
        return list(self.adapters.keys())

    def place_order(
        self,
        symbol: str,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        product: str = "I",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: Optional[str] = None,
        market_quote: Optional[Dict[str, Any]] = None,
        stop_loss_price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """Broadcast order placement across all active execution adapters."""
        results = {}
        is_entry = transaction_type.upper() in ("BUY", "ENTRY")

        # Market Hours & CAS Auction Guard
        if is_entry and getattr(self, "enforce_market_hours", False):
            now_ts = time.time()
            ist = timezone(timedelta(hours=5, minutes=30))
            dt_ist = datetime.fromtimestamp(now_ts, tz=ist)
            if dt_ist.year >= 2020:
                cur_hm = dt_ist.strftime("%H:%M")
                open_t = getattr(self, "market_open_time", "09:15")
                freeze_t = getattr(self, "eod_freeze_time", "15:15")
                close_t = getattr(self, "market_close_time", "15:30")

                if dt_ist.weekday() >= 5:
                    return {
                        m: {"status": "BLOCKED", "reason": f"Market closed (Weekend: {dt_ist.strftime('%A')})"}
                        for m in self.adapters.keys()
                    }
                if cur_hm < open_t:
                    return {
                        m: {"status": "BLOCKED", "reason": f"Pre-market Call Auction Session (CAS active until {open_t} IST)"}
                        for m in self.adapters.keys()
                    }
                if cur_hm >= close_t:
                    return {
                        m: {"status": "BLOCKED", "reason": f"Market closed (after {close_t} IST)"}
                        for m in self.adapters.keys()
                    }
                if cur_hm >= freeze_t:
                    return {
                        m: {"status": "BLOCKED", "reason": f"Intraday entry cutoff {freeze_t} IST reached"}
                        for m in self.adapters.keys()
                    }

        # Staleness Gate Guard on entry orders
        if is_entry and market_quote:
            ref_now = kwargs.get("now_ts") or time.time()
            is_fresh, quote_age = is_quote_fresh(market_quote, now_ts=ref_now, max_age_sec=30.0)
            if not is_fresh:
                return {
                    m: {"status": "BLOCKED", "reason": f"Market quote is STALE ({quote_age:.1f}s > 30.0s threshold)"}
                    for m in self.adapters.keys()
                }

        for mode, adapter in self.adapters.items():
            if is_entry and self.is_frozen(mode):
                results[mode] = {
                    "status": "BLOCKED",
                    "reason": f"Execution mode '{mode.upper()}' is FROZEN (no new entries allowed; closing positions only)",
                }
                continue

            try:
                res = adapter.place_order(
                    symbol=symbol,
                    instrument_token=instrument_token,
                    quantity=quantity,
                    transaction_type=transaction_type,
                    order_type=order_type,
                    product=product,
                    price=price,
                    trigger_price=trigger_price,
                    tag=tag,
                    market_quote=market_quote,
                    stop_loss_price=stop_loss_price,
                    **kwargs,
                )
                results[mode] = res
            except Exception as e:
                results[mode] = {"status": "ERROR", "error": str(e)}
        return results

    def update_stop_loss(
        self,
        symbol: str,
        instrument_token: str,
        stop_loss_price: float,
    ) -> Dict[str, Dict[str, Any]]:
        """Broadcast stop-loss price update (ratchet) across all supporting execution adapters."""
        results = {}
        for mode, adapter in self.adapters.items():
            if hasattr(adapter, "update_stop_loss"):
                try:
                    res = adapter.update_stop_loss(
                        symbol=symbol,
                        instrument_token=instrument_token,
                        new_stop_loss_price=stop_loss_price,
                    )
                    results[mode] = res
                except Exception as e:
                    results[mode] = {"status": "ERROR", "error": str(e)}
        return results

    def modify_order(
        self,
        orders_by_mode: Dict[str, str],
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        order_type: str = "LIMIT",
        validity: str = "DAY",
        disclosed_quantity: int = 0,
    ) -> Dict[str, Dict[str, Any]]:
        """Broadcast order modification across respective adapter order IDs."""
        results = {}
        for mode, order_id in orders_by_mode.items():
            adapter = self.adapters.get(mode.lower())
            if adapter and order_id:
                try:
                    res = adapter.modify_order(
                        order_id=order_id,
                        quantity=quantity,
                        price=price,
                        trigger_price=trigger_price,
                        order_type=order_type,
                        validity=validity,
                        disclosed_quantity=disclosed_quantity,
                    )
                    results[mode] = res
                except Exception as e:
                    results[mode] = {"status": "ERROR", "error": str(e)}
        return results

    def cancel_order(self, orders_by_mode: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        """Broadcast order cancellation across respective adapter order IDs."""
        results = {}
        for mode, order_id in orders_by_mode.items():
            adapter = self.adapters.get(mode.lower())
            if adapter and order_id:
                try:
                    res = adapter.cancel_order(order_id=order_id)
                    results[mode] = res
                except Exception as e:
                    results[mode] = {"status": "ERROR", "error": str(e)}
        return results

    def get_positions(self) -> Dict[str, List[Dict[str, Any]]]:
        """Retrieve open positions mapped by mode."""
        return {mode: adapter.get_positions() for mode, adapter in self.adapters.items()}

    def get_portfolio_summaries(self, quotes_cache: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
        """Retrieve portfolio accounting summaries mapped by mode."""
        summaries = {}
        for mode, adapter in self.adapters.items():
            if hasattr(adapter, "get_portfolio_summary"):
                try:
                    s = adapter.get_portfolio_summary(quotes_cache=quotes_cache)
                except TypeError:
                    s = adapter.get_portfolio_summary()
                s["mode"] = mode.upper()
                s["frozen"] = self.is_frozen(mode)
                summaries[mode] = s
            else:
                pos = adapter.get_positions()
                summaries[mode] = {
                    "mode": mode.upper(),
                    "frozen": self.is_frozen(mode),
                    "open_positions": len(pos),
                    "initial_capital": getattr(adapter, "initial_capital", 100_000.0),
                    "available_capital": getattr(adapter, "available_capital", 100_000.0),
                    "net_realized": 0.0,
                    "net_roi": 0.0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                }
        return summaries

    def reconcile_broker_positions(
        self,
        quarantine_sec: float = 10.0,
        quotes_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Trigger broker position reconciliation on the live adapter.
        Detects ghost positions, adopted orphan positions, and adjusts partial fills.
        """
        live_adapter = self.adapters.get("live")
        if live_adapter and hasattr(live_adapter, "reconcile_broker_positions"):
            return live_adapter.reconcile_broker_positions(quarantine_sec=quarantine_sec, quotes_cache=quotes_cache)
        return {"status": "SKIPPED", "reason": "No live adapter registered"}

    def check_eod_squareoff(
        self,
        now_ts: Optional[float] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        eod_time: str = "15:20",
        freeze_time: str = "15:15",
    ) -> List[Dict[str, Any]]:
        """
        Check End-of-Day RMS auto-squareoff guard across all registered adapters (live, paper, paper_delayed):
        - Freezes new entries at freeze_time (default 15:15 IST).
        - Closes intraday open positions at eod_time (default 15:20 IST).
        """
        ts = now_ts or time.time()
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime.fromtimestamp(ts, tz=ist)

        target_open = getattr(self, "market_open_time", "09:15")
        target_freeze = freeze_time or getattr(self, "eod_freeze_time", "15:15")
        target_eod = eod_time or getattr(self, "eod_square_off_time", "15:20")

        if dt_ist.year >= 2020:
            cur_hm = dt_ist.strftime("%H:%M")
            if cur_hm < target_open:
                # Pre-market CAS: Freeze entries so no orders can execute
                self.freeze_mode("all")
            elif cur_hm >= target_freeze or cur_hm >= target_eod:
                # EOD Freeze / Squareoff
                self.freeze_mode("all")
            elif "all" in self.frozen_modes and not os.path.exists("logs/.freeze_all"):
                # Lift pre-market freeze upon continuous session opening (09:15 to 15:15 IST)
                self.unfreeze_mode("all")

        all_exited = []
        for name, adapter in self.adapters.items():
            if hasattr(adapter, "check_eod_squareoff"):
                try:
                    res = adapter.check_eod_squareoff(
                        now_ts=ts,
                        quotes_cache=quotes_cache,
                        eod_time=target_eod,
                        freeze_time=target_freeze,
                    )
                    if isinstance(res, list):
                        all_exited.extend(res)
                except TypeError:
                    res = adapter.check_eod_squareoff(now_ts=ts, quotes_cache=quotes_cache)
                    if isinstance(res, list):
                        all_exited.extend(res)
                except Exception as e:
                    print(f"⚠️ [EOD GUARD] Error checking EOD squareoff for adapter {name}: {e}")

        return all_exited

    def shutdown(self) -> None:
        """Shutdown all registered execution adapters."""
        for adapter in self.adapters.values():
            try:
                adapter.shutdown()
            except Exception:
                pass


def create_multi_executor(
    modes_input: str | List[str],
    capital: Optional[float] = None,
    brokerage_per_order: Optional[float] = None,
    live_confirmed: bool = False,
    access_token: Optional[str] = None,
    config_path: str = "config/execution.json",
    log_dir: str = "logs",
    exec_config: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> MultiExecutionManager:
    """
    Factory creating a MultiExecutionManager from comma-separated string or list.
    Reads defaults from config/execution.json or in-memory exec_config when parameters are not explicitly passed.
    Supports:
      - 'paper'
      - 'paper_delayed'
      - 'live'
      - 'all' (expands to paper, paper_delayed, live)
      - comma-separated combinations: 'paper,live', 'paper,paper_delayed', etc.
    """
    exec_cfg: Dict[str, Any] = {}
    if exec_config is not None:
        exec_cfg = exec_config.get("execution", exec_config)
    elif os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                exec_cfg = data.get("execution", {})
        except Exception:
            pass

    if isinstance(modes_input, str):
        raw_list = [m.strip().lower() for m in modes_input.split(",") if m.strip()]
    else:
        raw_list = [str(m).strip().lower() for m in modes_input if str(m).strip()]

    target_modes: Set[str] = set()
    for m in raw_list:
        if m in ("all", "both"):
            target_modes.update(["paper", "paper_delayed", "live"])
        elif m in ("paper", "paper_delayed", "paper-delayed", "live", "sim"):
            norm_m = "paper_delayed" if m in ("paper_delayed", "paper-delayed") else m
            target_modes.add(norm_m)

    if not target_modes:
        default_m = exec_cfg.get("default_mode", "paper").lower()
        target_modes.add(default_m)

    adapters: Dict[str, BaseExecutionAdapter] = {}

    enforce_hours = kwargs.get("enforce_market_hours", exec_cfg.get("enforce_market_hours", False))
    m_open = normalize_time_str(kwargs.get("market_open_time", exec_cfg.get("market_open_time", "09:15")), "09:15")
    m_close = normalize_time_str(kwargs.get("market_close_time", exec_cfg.get("market_close_time", "15:30")), "15:30")
    e_freeze = normalize_time_str(kwargs.get("eod_freeze_time", exec_cfg.get("eod_freeze_time", "15:15")), "15:15")
    e_sq = normalize_time_str(kwargs.get("eod_square_off_time", exec_cfg.get("eod_square_off_time", "15:20")), "15:20")

    session_date = kwargs.get("session_date") or datetime.now().strftime("%Y%m%d")
    rec_session_dir = os.path.join("recordings", session_date)
    try:
        os.makedirs(rec_session_dir, exist_ok=True)
    except Exception:
        pass

    def _resolve_csv(raw_path: Optional[str], default_filename: str) -> str:
        if not raw_path or raw_path.startswith("logs/"):
            return os.path.join(rec_session_dir, default_filename)
        if "{date}" in raw_path:
            return raw_path.replace("{date}", session_date)
        return raw_path

    if "paper" in target_modes or "sim" in target_modes:
        p_cfg = exec_cfg.get("paper", {})
        p_cap = capital if capital is not None else float(p_cfg.get("starting_capital_per_sim", 100_000.0))
        p_brk = brokerage_per_order if brokerage_per_order is not None else float(p_cfg.get("brokerage_per_order", 30.0))
        p_raw = kwargs.get("output_csv", kwargs.get("paper_csv", p_cfg.get("output_csv")))
        p_csv = _resolve_csv(p_raw, "paper_trades.csv")
        adapters["paper"] = PaperExecutionAdapter(
            initial_capital=p_cap,
            brokerage_per_order=p_brk,
            output_csv=p_csv,
            enforce_market_hours=enforce_hours,
            market_open_time=m_open,
            market_close_time=m_close,
            eod_freeze_time=e_freeze,
            eod_square_off_time=e_sq,
        )

    if "paper_delayed" in target_modes:
        pd_cfg = exec_cfg.get("paper_delayed", {})
        pd_cap = capital if capital is not None else float(pd_cfg.get("starting_capital_per_sim", 100_000.0))
        pd_brk = brokerage_per_order if brokerage_per_order is not None else float(pd_cfg.get("brokerage_per_order", 30.0))
        pd_raw = kwargs.get("output_csv", kwargs.get("delayed_csv", pd_cfg.get("output_csv")))
        pd_csv = _resolve_csv(pd_raw, "paper_delayed_trades.csv")
        min_del = float(kwargs.get("min_delay_ms", pd_cfg.get("min_delay_ms", 200.0)))
        max_del = float(kwargs.get("max_delay_ms", pd_cfg.get("max_delay_ms", 500.0)))
        adapters["paper_delayed"] = PaperDelayedExecutionAdapter(
            initial_capital=pd_cap,
            brokerage_per_order=pd_brk,
            output_csv=pd_csv,
            min_delay_ms=min_del,
            max_delay_ms=max_del,
            enforce_market_hours=enforce_hours,
            market_open_time=m_open,
            market_close_time=m_close,
            eod_freeze_time=e_freeze,
            eod_square_off_time=e_sq,
        )

    if "live" in target_modes:
        l_cfg = exec_cfg.get("live", {})
        l_cap = capital if capital is not None else float(l_cfg.get("max_capital_limit_inr", l_cfg.get("starting_capital_per_sim", 100_000.0)))
        l_brk = brokerage_per_order if brokerage_per_order is not None else float(l_cfg.get("brokerage_per_order", 30.0))
        l_raw = kwargs.get("output_csv", kwargs.get("live_csv", l_cfg.get("output_csv")))
        l_csv = _resolve_csv(l_raw, "live_trades.csv")
        adapters["live"] = LiveExecutionAdapter(
            access_token=access_token,
            output_csv=l_csv,
            brokerage_per_order=l_brk,
            live_confirmed=live_confirmed,
            initial_capital=l_cap,
            enforce_market_hours=enforce_hours,
            market_open_time=m_open,
            market_close_time=m_close,
            eod_freeze_time=e_freeze,
            eod_square_off_time=e_sq,
        )

    return MultiExecutionManager(
        adapters=adapters,
        enforce_market_hours=enforce_hours,
        market_open_time=m_open,
        market_close_time=m_close,
        eod_freeze_time=e_freeze,
        eod_square_off_time=e_sq,
        log_dir=log_dir,
    )

