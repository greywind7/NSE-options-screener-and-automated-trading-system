#!/usr/bin/env python3
"""
strategies/momentum/strategy.py
===============================
Bidirectional Momentum Strategy Package:
Coordinates Scanner, Indicators, Scoring, Patterns, Option Strike Selection,
Risk Engine, 16 Simulation Tracks, and DYNAMIC_TRIO Ensemble Allocator.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from infrastructure.market_data.subscriber import MarketDataSubscriber
from infrastructure.pnl.pnl_aggregator import PnLAggregator, get_global_pnl_aggregator
from strategies.base import BaseStrategy
from strategies.momentum.dashboard import (
    Colors,
    clear_screen,
    render_performance_matrix,
    render_positions_summary,
    render_scanner_dashboard,
)
from strategies.momentum.actionable_pool import ActionableSetupPool
from strategies.momentum.indicators import compute_macd, compute_rsi, compute_support_resistance_levels
from strategies.momentum.option_selector import OptionStrikeResolver
from strategies.momentum.patterns import (
    FlagPatternTracker,
    get_time_of_day_tier,
    get_time_tier_gate,
    is_opening_45m_session,
)
from strategies.momentum.portfolio import MultiTrackPortfolio
from strategies.momentum.risk import normalize_time_str
from strategies.momentum.scanner import RollingVolumeTracker
from strategies.momentum.scoring import calculate_setup_score


class MomentumStrategyRunner(BaseStrategy):
    """
    Live / Simulation execution coordinator for the Bidirectional Momentum Strategy.
    """

    def __init__(
        self,
        mode: str = "paper",
        capital: Optional[float] = None,
        min_score: Optional[int] = None,
        min_trade_score: Optional[int] = None,
        show_positions: bool = False,
        track_filter: Optional[str] = None,
        view_mode: str = "all",
        volume_index_path: str = "data/index.json",
        manifest_path: Optional[str] = None,
        subscriber: Optional[MarketDataSubscriber] = None,
        execution_adapter: Optional[Any] = None,
        config_path: str = "config/strategies.json",
        execution_config_path: str = "config/execution.json",
        strategy_config: Optional[Dict[str, Any]] = None,
        execution_config: Optional[Dict[str, Any]] = None,
        dynamic_lookback: Optional[int] = None,
        eod_square_off_time: Optional[str] = None,
        eod_freeze_time: Optional[str] = None,
        is_mcx: bool = False,
        market_open_time: Optional[str] = None,
        market_close_time: Optional[str] = None,
        enable_dashboard: bool = True,
        start_time: Optional[str] = None,
        backfill: bool = True,
    ):
        from execution.multi_executor import MultiExecutionManager, create_multi_executor

        self.enable_dashboard = enable_dashboard
        self.start_time_str = start_time
        self.backfill = backfill
        self.is_backfilling = False

        # 1. Unify and resolve Strategy & Execution configurations
        if strategy_config is not None:
            self.strat_cfg = strategy_config
        elif os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    self.strat_cfg = json.load(f)
            except Exception:
                self.strat_cfg = {}
        else:
            self.strat_cfg = {}

        if execution_config is not None:
            self.exec_cfg = execution_config
        elif os.path.exists(execution_config_path):
            try:
                with open(execution_config_path, "r", encoding="utf-8") as f:
                    self.exec_cfg = json.load(f)
            except Exception:
                self.exec_cfg = {}
        else:
            self.exec_cfg = {}

        self.mom_cfg = self.strat_cfg.get("strategies", {}).get("momentum", {})
        r_cfg = self.mom_cfg.get("risk", {})
        regime_cfg = self.mom_cfg.get("market_regime", {})
        vol_gate_cfg = self.mom_cfg.get("volume_inflation_gate", {})
        pattern_cfg = self.mom_cfg.get("flag_patterns", vol_gate_cfg)
        alloc_cfg = self.mom_cfg.get("ensemble_allocator", {})
        options_cfg = self.mom_cfg.get("options", {})

        is_mcx = is_mcx or ("mcx" in (volume_index_path or "").lower()) or ("mcx" in (manifest_path or "").lower())
        eod_sq = eod_square_off_time or r_cfg.get("eod_square_off_time")
        eod_frz = eod_freeze_time or r_cfg.get("eod_freeze_time")
        m_open = market_open_time or r_cfg.get("market_open_time")
        m_close = market_close_time or r_cfg.get("market_close_time")

        self.is_mcx = is_mcx
        self.market_open_time = normalize_time_str(m_open or ("09:00" if is_mcx else "09:15"), "09:00" if is_mcx else "09:15")
        self.market_close_time = normalize_time_str(m_close or ("23:30" if is_mcx else "15:30"), "23:30" if is_mcx else "15:30")
        self.eod_square_off_time = normalize_time_str(eod_sq or ("23:25" if is_mcx else "15:20"), "23:25" if is_mcx else "15:20")
        self.eod_freeze_time = normalize_time_str(eod_frz or ("23:15" if is_mcx else "15:15"), "23:15" if is_mcx else "15:15")
        self._last_market_session = ""

        if not mode:
            mode = self.exec_cfg.get("execution", {}).get("default_mode", "paper")

        primary_m = mode.split(",")[0].strip().lower()
        m_cfg = self.exec_cfg.get("execution", {}).get(primary_m, {})

        if capital is None:
            candidates_cap = [
                m_cfg.get("starting_capital_per_sim"),
                m_cfg.get("max_capital_limit_inr"),
                m_cfg.get("capital_limit"),
                m_cfg.get("limits"),
                self.exec_cfg.get("execution", {}).get("starting_capital_per_sim"),
                self.exec_cfg.get("execution", {}).get("max_capital_limit_inr"),
                self.exec_cfg.get("execution", {}).get("capital_limit"),
                self.exec_cfg.get("execution", {}).get("limits"),
                self.exec_cfg.get("execution", {}).get("paper_delayed", {}).get("starting_capital_per_sim") if "delayed" in primary_m else None,
                self.exec_cfg.get("execution", {}).get("paper", {}).get("starting_capital_per_sim"),
            ]
            val_cap = next((c for c in candidates_cap if c is not None), 100000.0)
            capital = float(val_cap)

        if min_score is None:
            min_score = int(
                self.mom_cfg.get("minimum_view_score")
                or self.mom_cfg.get("min_view_score")
                or self.mom_cfg.get("min_score")
                or 67
            )
        if min_trade_score is None:
            min_trade_score = int(
                self.mom_cfg.get("minimum_trade_score")
                or self.mom_cfg.get("min_trade_score")
                or 70
            )

        min_score = min_score if min_score is not None else 67
        min_trade_score = min_trade_score if min_trade_score is not None else 70

        self.mode = mode
        self.capital = capital
        self.min_score = min_score
        self.min_trade_score = min_trade_score
        self.show_positions = show_positions
        self.track_filter = track_filter
        self.view_mode = view_mode.lower() if view_mode else "all"
        self.volume_index_path = volume_index_path
        self.manifest_path = manifest_path
        self.subscriber = subscriber or MarketDataSubscriber(name="MomentumStrategy")

        if execution_adapter is not None:
            if isinstance(execution_adapter, MultiExecutionManager):
                self.multi_executor = execution_adapter
            else:
                self.multi_executor = MultiExecutionManager({self.mode.lower(): execution_adapter})
            self.execution_adapter = execution_adapter
        else:
            self.multi_executor = create_multi_executor(
                modes_input=self.mode,
                capital=capital,
                config_path=execution_config_path,
                exec_config=self.exec_cfg,
                enforce_market_hours=True,
                market_open_time=self.market_open_time,
                market_close_time=self.market_close_time,
                eod_freeze_time=self.eod_freeze_time,
                eod_square_off_time=self.eod_square_off_time,
            )
            self.execution_adapter = (
                self.multi_executor.get_adapter(self.mode.lower())
                or self.multi_executor.get_adapter("paper")
                or self.multi_executor.get_adapter("paper_delayed")
                or list(self.multi_executor.adapters.values())[0]
            )

        self.volume_tracker = RollingVolumeTracker(
            nifty_key=regime_cfg.get("index_key", "NSE_INDEX|Nifty 50"),
            bias_threshold_pct=float(regime_cfg.get("bias_threshold_pct", 0.25)),
        )
        tod_cfg = (
            self.mom_cfg.get("time_of_day_gates")
            or self.mom_cfg.get("engine_2_0", {}).get("time_of_day_gates")
        )
        self.time_tier_gates = tod_cfg

        self.pattern_tracker = FlagPatternTracker(
            memory_window_seconds=float(pattern_cfg.get("memory_window_seconds", 1800.0)),
            coiling_max_span_pct=float(pattern_cfg.get("coiling_max_span_pct", 1.20)),
            min_coiling_cycles=int(pattern_cfg.get("min_coiling_cycles", 2)),
            min_breakout_rvol_5m=float(pattern_cfg.get("min_breakout_rvol_5m", 1.20)),
            min_breakout_accel=float(pattern_cfg.get("min_breakout_accel", 1.05)),
            min_breakout_pace=float(pattern_cfg.get("min_breakout_pace", 1.05)),
            midday_rvol_5m=float(pattern_cfg.get("midday_rvol_5m", 1.35)),
            midday_accel=float(pattern_cfg.get("midday_accel", 1.15)),
            time_tier_gates=self.time_tier_gates,
        )
        self.option_resolver = OptionStrikeResolver(
            manifest_path=manifest_path,
            max_spread_pct=float(options_cfg.get("max_spread_pct", 3.0)),
            min_depth_lots=int(options_cfg.get("min_depth_lots", 1)),
        )
        self.pnl_aggregator = get_global_pnl_aggregator()

        # Scoring Engine 2.0: Actionable candidate pool with continuous exponential score decay & 20s eviction
        self.actionable_pool = ActionableSetupPool(
            expiry_seconds=float(self.mom_cfg.get("actionable_expiry_seconds", 20.0))
        )
        self.is_warming_up = False

        rolling_lookback = 15
        if dynamic_lookback is not None:
            rolling_lookback = int(dynamic_lookback)
        else:
            rolling_lookback = int(alloc_cfg.get("rolling_lookback_minutes", 15))

        session_date = getattr(self, "session_date", None) or datetime.now().strftime("%Y%m%d")
        rec_session_dir = os.path.join("recordings", session_date)
        try:
            os.makedirs(rec_session_dir, exist_ok=True)
        except Exception:
            pass

        output_csv = m_cfg.get("output_csv", os.path.join(rec_session_dir, "paper_trades.csv"))
        if "{date}" in output_csv:
            output_csv = output_csv.replace("{date}", session_date)

        # Initialize per-mode simulation portfolios for all active execution modes
        self.portfolios: Dict[str, MultiTrackPortfolio] = {}
        for m_name in self.multi_executor.active_modes:
            norm_m = m_name.lower()
            if norm_m == "paper":
                p_csv = os.path.join(rec_session_dir, "paper_trades.csv")
                min_del = 0.0
                max_del = 0.0
                allow_all_cmp = False
            elif norm_m in ("paper_delayed", "paper-delayed"):
                p_csv = os.path.join(rec_session_dir, "paper_delayed_trades.csv")
                min_del = float(m_cfg.get("min_delay_ms", 200))
                max_del = float(m_cfg.get("max_delay_ms", 500))
                allow_all_cmp = False
            elif norm_m == "live":
                p_csv = os.path.join(rec_session_dir, "live_trades.csv")
                min_del = 0.0
                max_del = 0.0
                allow_all_cmp = True
            else:
                p_csv = os.path.join(rec_session_dir, f"{norm_m}_trades.csv")
                min_del = 0.0
                max_del = 0.0
                allow_all_cmp = False

            self.portfolios[norm_m] = MultiTrackPortfolio(
                starting_capital=capital,
                output_csv=p_csv,
                show_positions=show_positions,
                track_filter=track_filter,
                rolling_lookback_minutes=rolling_lookback,
                pnl_aggregator=self.pnl_aggregator,
                mode=norm_m,
                min_delay_ms=min_del,
                max_delay_ms=max_del,
                allow_compounding_on_all=allow_all_cmp,
                eod_square_off_time=self.eod_square_off_time,
                eod_freeze_time=self.eod_freeze_time,
                is_mcx=self.is_mcx,
                market_open_time=self.market_open_time,
                market_close_time=self.market_close_time,
                risk_config=r_cfg,
                allocator_config=alloc_cfg,
                option_resolver=self.option_resolver,
            )

        # Primary portfolio alias for backwards compatibility
        self.portfolio = (
            self.portfolios.get("paper")
            or self.portfolios.get("paper_delayed")
            or list(self.portfolios.values())[0]
        )

        self.volume_index: Dict[str, Any] = {}
        self.instrument_name_map: Dict[str, str] = {}
        self.quotes_cache: Dict[str, Any] = {}
        self.recent_ticks: Dict[str, List[float]] = {}
        self.nifty_stats: Dict[str, Any] = {"regime": "NEUTRAL", "pct_change": 0.0}
        self.underlying_keys: Set[str] = set()
        self.option_keys: Set[str] = set()
        self.session_high: Dict[str, float] = {}
        self.session_low: Dict[str, float] = {}

        self._running = False
        self._last_dashboard_render = 0.0
        self.fresh_alerts: Dict[str, Dict[str, Any]] = {}
        self.confirmed_setups: List[Dict[str, Any]] = []
        self.monitored_assets: Dict[str, Dict[str, Any]] = {}

    @property
    def designated_execution_track(self) -> str:
        """
        Determine the single designated track for physical broker execution (live/paper/paper_delayed).
        If user ran with --track ATM -> 'ATM'
        If user ran with --track DYNAMIC_TRIO -> 'DYNAMIC_TRIO'
        If user ran with --track ALL -> 'DYNAMIC_TRIO' (ensemble champion) or 'ATM'
        """
        tf = (self.track_filter or "").upper()
        if tf in ("ALL", "*", ""):
            return "DYNAMIC_TRIO" if hasattr(self, "portfolio") and "DYNAMIC_TRIO" in self.portfolio.tracks else "ATM"
        if tf in ("TRIO", "DYNAMIC_TRIO"):
            return "DYNAMIC_TRIO"
        if hasattr(self, "portfolio") and tf in self.portfolio.tracks:
            return tf
        if hasattr(self, "portfolio"):
            for t in self.portfolio.tracks:
                if tf in t:
                    return t
        return "ATM"

    def set_warming_up(self, is_warming: bool) -> None:
        """Toggle warm-up mode (updates volume, candles, and pattern trackers without executing trades)."""
        self.is_warming_up = is_warming

    def backfill_from_recordings(
        self,
        session_date: Optional[str] = None,
        start_time_str: Optional[str] = None,
    ) -> int:
        """
        Backfills indicators, caches, and simulates strategy state from today's session recording.
        Ticks before start_time_str are ingested in warm-up mode (updating indicators/candles/RVOL).
        Ticks from start_time_str onward simulate strategy trade execution up to current time.
        Suppresses broker order placement during backfill.
        """
        import io
        import tarfile
        from datetime import timezone, timedelta

        if not session_date:
            session_date = datetime.now().strftime("%Y%m%d")

        rec_dir = os.path.join("recordings", session_date)
        events_path = os.path.join(rec_dir, "market_events.jsonl")
        ticks_path = os.path.join(rec_dir, "ticks.jsonl")
        alt_events_path = os.path.join(rec_dir, "events.jsonl")
        tar_path = os.path.join("recordings", f"{session_date}.tar.gz")

        src_info = None
        if os.path.exists(events_path) and os.path.getsize(events_path) > 0:
            src_info = (events_path, "file")
        elif os.path.exists(ticks_path) and os.path.getsize(ticks_path) > 0:
            src_info = (ticks_path, "file")
        elif os.path.exists(alt_events_path) and os.path.getsize(alt_events_path) > 0:
            src_info = (alt_events_path, "file")
        elif os.path.exists(tar_path) and os.path.getsize(tar_path) > 0:
            src_info = (tar_path, "archive")

        if not src_info:
            print(f"ℹ️ [BACKFILL] No prior recording found for date {session_date} in 'recordings/'. Starting fresh live stream.")
            return 0

        # Resolve start_epoch for simulation threshold
        st_str = normalize_time_str(start_time_str or self.market_open_time or "09:15", "09:15")
        try:
            ist = timezone(timedelta(hours=5, minutes=30))
            y, m, d = int(session_date[:4]), int(session_date[4:6]), int(session_date[6:8])
            sh, sm = map(int, st_str.split(":")[:2])
            start_ts = datetime(y, m, d, sh, sm, 0, tzinfo=ist).timestamp()
        except Exception:
            start_ts = 0.0

        print(f"⚡ [BACKFILL] Ingesting recorded ticks from {session_date} ({src_info[0]})...")
        print(f"   • Ticks < {st_str} IST: Warm-up mode (populating candles, RVOL, pattern indicators)")
        print(f"   • Ticks >= {st_str} IST: Simulating trade execution (restoring active positions & P&L)")

        def _read_events(info):
            path, kind = info
            if kind == "file":
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                yield json.loads(line)
                            except Exception:
                                pass
            elif kind == "archive":
                with tarfile.open(path, "r:gz") as tar:
                    for m in tar.getmembers():
                        if ("market_events" in m.name.lower() or "ticks" in m.name.lower()) and m.name.endswith(".jsonl"):
                            f = tar.extractfile(m)
                            if f:
                                tf = io.TextIOWrapper(f, encoding="utf-8")
                                for line in tf:
                                    line = line.strip()
                                    if line:
                                        try:
                                            yield json.loads(line)
                                        except Exception:
                                            pass
                            break

        self.is_backfilling = True
        total_events = 0
        warmup_events = 0
        sim_events = 0

        try:
            for evt in _read_events(src_info):
                ts = float(evt.get("received_timestamp") or evt.get("exchange_timestamp") or 0.0)
                if ts <= 0:
                    continue
                if start_ts > 0 and ts < start_ts:
                    if not self.is_warming_up:
                        self.set_warming_up(True)
                    warmup_events += 1
                else:
                    if self.is_warming_up:
                        self.set_warming_up(False)
                    sim_events += 1

                self.on_market_event(evt)
                total_events += 1
        finally:
            self.is_backfilling = False
            self.set_warming_up(False)

        # Reconcile broker positions for live execution if active
        live_adapter = self.multi_executor.get_adapter("live")
        if live_adapter and hasattr(live_adapter, "reconcile_broker_positions"):
            try:
                live_adapter.reconcile_broker_positions()
            except Exception:
                pass

        print(f"✓ [BACKFILL COMPLETE] Processed {total_events:,} events ({warmup_events:,} warm-up, {sim_events:,} simulated execution). State fully primed!\n")
        return total_events

    def warmup_from_recording(self, session_date: str, target_ts: float) -> int:
        """
        Replays recorded market events up to target_ts in warm-up mode
        to restore indicators, candle buffers, and flag pole states upon mid-day strategy restarts.
        """
        st_hm = datetime.fromtimestamp(target_ts).strftime("%H:%M") if target_ts > 0 else "09:15"
        return self.backfill_from_recordings(session_date=session_date, start_time_str=st_hm)

    def _dispatch_execution_exits(self, closed_trades: List[Any]) -> None:
        """
        Dispatches exits through multi_executor ONLY for trades belonging to the designated execution track.
        Prevents firing duplicate or phantom broker orders when unrelated simulation tracks exit!
        """
        if not closed_trades or getattr(self, "is_backfilling", False):
            return
        exec_track = self.designated_execution_track
        for ct in closed_trades:
            if getattr(ct, "strategy_track", "") == exec_track:
                # Resolve freshest quote across all key formats (: vs | and display symbol)
                ck = ct.contract_key
                csym = getattr(ct, "contract_symbol", "")
                q = (
                    self.quotes_cache.get(ck)
                    or self.quotes_cache.get(ck.replace(":", "|"))
                    or self.quotes_cache.get(ck.replace("|", ":"))
                    or (self.quotes_cache.get(csym) if csym else None)
                    or {}
                )
                self.multi_executor.place_order(
                    symbol=ct.symbol,
                    instrument_token=ct.contract_key,
                    quantity=ct.quantity,
                    transaction_type="SELL",
                    order_type="MARKET",
                    tag=ct.strategy_track,
                    market_quote=q,
                )

    def _dispatch_stop_loss_updates(self, portfolio: Any) -> None:
        """
        Broadcast trailing stop-loss ratchet updates to multi_executor for the designated execution track.
        """
        if getattr(self, "is_backfilling", False):
            return
        exec_track = self.designated_execution_track
        acct = portfolio.tracks.get(exec_track)
        if not acct:
            return

        for pos in acct.open_positions.values():
            cur_sl = getattr(pos, "current_sl", 0.0)
            last_sl = getattr(pos, "_last_broadcast_sl", 0.0)
            if cur_sl > last_sl + 0.05:
                pos._last_broadcast_sl = cur_sl
                self.multi_executor.update_stop_loss(
                    symbol=pos.symbol,
                    instrument_token=pos.contract_key,
                    stop_loss_price=cur_sl,
                )

    def _is_option_contract(self, key: str, event: Dict[str, Any], display_name: str = "") -> bool:
        """
        Determine if an instrument is an option contract (CE/PE) vs an underlying asset.
        Option ticks feed quotes_cache for live fills/MTM/risk exits, but do not enter the scanner.
        """
        # 1. Direct key membership
        if key in self.option_keys:
            return True
        if key in self.underlying_keys:
            return False

        # 2. Check event attributes
        itype = (event.get("instrument_type") or event.get("option_type") or "").strip().upper()
        if itype in ("CE", "PE"):
            return True

        # 3. Check symbol / display name / key patterns (e.g. 'COPPER 1380 CE', 'ABB24NOV5400CE', etc.)
        for txt in (display_name, event.get("symbol", ""), key):
            if txt:
                txt_clean = txt.strip().upper()
                if txt_clean.endswith(" CE") or txt_clean.endswith(" PE"):
                    return True
                if txt_clean.endswith("_CE") or txt_clean.endswith("_PE"):
                    return True
                if re.search(r'\d+\.?\d*\s*(CE|PE)$', txt_clean):
                    return True

        return False

    def initialize(self, context: Optional[Dict[str, Any]] = None) -> None:
        """Load volume index baseline and option chains."""
        print("  Initializing Momentum Strategy Package...")
        if os.path.exists(self.volume_index_path):
            with open(self.volume_index_path, "r", encoding="utf-8") as f:
                self.volume_index = json.load(f)
            if len(self.volume_index) > 0:
                print(f"  ✓ Loaded 30-day Volume Index ({len(self.volume_index):,} instruments).")
                for vk in self.volume_index.keys():
                    self.underlying_keys.add(vk)
                    self.underlying_keys.add(vk.replace(":", "|"))
                    self.underlying_keys.add(vk.replace("|", ":"))
            elif "mcx" in self.volume_index_path.lower():
                print("  ✓ MCX Commodities mode: real-time dynamic volume & momentum tracking active.")
            else:
                print("  ℹ️ Volume Index baseline empty. Real-time dynamic volume tracking active.")

        # Load instrument name mappings and underlying/option keys from manifest files
        manifest_files = [self.manifest_path, "data/subscription_manifest_mcx.json", "data/subscription_manifest.json"]
        for mf in manifest_files:
            if mf and os.path.exists(mf):
                try:
                    with open(mf, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    n_map = m_data.get("instrument_name_map", {})
                    for k, v in n_map.items():
                        self.instrument_name_map[k] = v
                        self.instrument_name_map[k.replace(":", "|")] = v
                        self.instrument_name_map[k.replace("|", ":")] = v

                    # Track underlying keys
                    for uk in m_data.get("underlying_keys", []):
                        self.underlying_keys.add(uk)
                        self.underlying_keys.add(uk.replace(":", "|"))
                        self.underlying_keys.add(uk.replace("|", ":"))

                    for u in m_data.get("streamed_underlyings", []):
                        uk = u.get("instrument_key", "")
                        un = u.get("name") or u.get("symbol", "")
                        if uk:
                            self.underlying_keys.add(uk)
                            self.underlying_keys.add(uk.replace(":", "|"))
                            self.underlying_keys.add(uk.replace("|", ":"))
                        if uk and un:
                            self.instrument_name_map[uk] = un
                            self.instrument_name_map[uk.replace(":", "|")] = un
                            self.instrument_name_map[uk.replace("|", ":")] = un

                    # Track option keys
                    for ok in m_data.get("popular_option_keys", []):
                        self.option_keys.add(ok)
                        self.option_keys.add(ok.replace(":", "|"))
                        self.option_keys.add(ok.replace("|", ":"))

                    for opt in m_data.get("streamed_popular_options", []):
                        ok = opt.get("instrument_key", "")
                        if ok:
                            self.option_keys.add(ok)
                            self.option_keys.add(ok.replace(":", "|"))
                            self.option_keys.add(ok.replace("|", ":"))

                    for batch in m_data.get("background_poll_batches", []):
                        for ok in batch:
                            self.option_keys.add(ok)
                            self.option_keys.add(ok.replace(":", "|"))
                            self.option_keys.add(ok.replace("|", ":"))
                except Exception:
                    pass

        # Also load from filtered CSVs if any keys remain unmapped
        import csv
        for csv_f in ("data/filtered_mcx.csv", "data/filtered.csv"):
            if os.path.exists(csv_f):
                try:
                    with open(csv_f, "r", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            rk = row.get("upstox_instrument_key") or row.get("instrument_key") or ""
                            rn = row.get("name") or row.get("trading_symbol") or ""
                            if rk:
                                self.underlying_keys.add(rk)
                                self.underlying_keys.add(rk.replace(":", "|"))
                                self.underlying_keys.add(rk.replace("|", ":"))
                            if rk and rn:
                                self.instrument_name_map.setdefault(rk, rn)
                                self.instrument_name_map.setdefault(rk.replace(":", "|"), rn)
                                self.instrument_name_map.setdefault(rk.replace("|", ":"), rn)
                except Exception:
                    pass

        self.option_resolver.load()
        self.multi_executor.set_quotes_cache(self.quotes_cache)
        print(f"  ✓ Loaded Option Strike Resolver ({len(self.option_resolver.options_by_symbol):,} underlying chains).")
        print(f"  ✓ Loaded Instrument Name Map ({len(self.instrument_name_map):,} clean display names).")
        print(f"  ✓ Monitored Underlying Universe: {len(self.underlying_keys) // 3 if self.underlying_keys else len(self.volume_index):,} assets | {len(self.option_keys) // 3 if self.option_keys else 0:,} tracked option contracts.")
        print(f"  ✓ DYNAMIC_TRIO seeded with champions: {self.portfolio.dynamic_trio_champions}")
        active_modes_str = ", ".join([m.upper() for m in self.multi_executor.active_modes])
        print(f"  ✓ Multi-Execution Adapters Active: [{active_modes_str}]")

    def on_market_event(self, event: Dict[str, Any]) -> None:
        """Process incoming market tick or depth."""
        etype = event.get("event_type", "TICK")

        if etype == "DEPTH":
            key = event.get("instrument_key", "")
            if not key:
                return

            clean_name = (
                self.instrument_name_map.get(key)
                or self.instrument_name_map.get(key.replace(":", "|"))
                or self.instrument_name_map.get(key.replace("|", ":"))
            )
            display_name = clean_name or event.get("symbol", "") or key.split("|")[-1]

            bid = float(event.get("bid") or 0.0)
            ask = float(event.get("ask") or 0.0)
            if bid <= 0 and event.get("bids"):
                bid = float(event["bids"][0][0])
            if ask <= 0 and event.get("asks"):
                ask = float(event["asks"][0][0])

            # Update cache with latest top bid & ask from market depth update
            cached = self.quotes_cache.get(key, {})
            merged = dict(cached)
            merged.update(event)
            if bid > 0:
                merged["bid"] = bid
            if ask > 0:
                merged["ask"] = ask
            self.quotes_cache[key] = merged
            self.quotes_cache[key.replace(":", "|")] = merged
            self.quotes_cache[key.replace("|", ":")] = merged
            if display_name:
                self.quotes_cache[display_name] = merged

            # If any open position holds this contract, update trailing SL / target checks against live bid
            now_ts = float(event.get("received_timestamp") or time.time())
            for m_name, port in self.portfolios.items():
                if port.is_instrument_held(key, display_name):
                    closed_trades = port.update_positions(self.quotes_cache, now_ts=now_ts)
                    if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
                        self._dispatch_execution_exits(closed_trades)
            return

        if etype != "TICK":
            return

        key = event.get("instrument_key", "")
        sym = event.get("symbol", "")
        ltp = float(event.get("ltp") or 0.0)
        vol = int(event.get("volume") or 0)
        close = float(event.get("close") or 0.0)
        high = float(event.get("high") or ltp)
        low = float(event.get("low") or ltp)
        now_ts = float(event.get("received_timestamp") or time.time())

        if not key or ltp <= 0:
            return

        # Resolve clean human-readable symbol (never display raw token numbers)
        clean_name = (
            self.instrument_name_map.get(key)
            or self.instrument_name_map.get(key.replace(":", "|"))
            or self.instrument_name_map.get(key.replace("|", ":"))
        )
        display_name = clean_name or sym or key.split("|")[-1]
        if not sym or sym.isdigit() or (key and sym == key.split("|")[-1]):
            sym = display_name

        # 1. Update Quotes Cache ALWAYS (crucial: real-time option ticks feed this cache!)
        self.quotes_cache[key] = event
        self.quotes_cache[key.replace(":", "|")] = event
        self.quotes_cache[key.replace("|", ":")] = event
        if display_name:
            self.quotes_cache[display_name] = event

        # 2. Check if Nifty 50 Index tick
        if "Nifty 50" in key or "Nifty 50" in sym or sym == "NIFTY":
            self.nifty_stats = self.volume_tracker.update_nifty(ltp, close)
            for p in self.portfolios.values():
                p.nifty_stats = self.nifty_stats
            return

        # 3. Check if this is an OPTION contract
        # Options are derivative execution instruments, NOT scanner assets!
        # Their ticks provide real-time pricing for spread fills, MTM, and trailing SL/TGT exits.
        if self._is_option_contract(key, event, display_name):
            # Immediately evaluate open positions holding this contract for live risk exits
            for m_name, port in self.portfolios.items():
                if port.is_instrument_held(key, display_name):
                    closed_trades = port.update_positions(self.quotes_cache, now_ts=now_ts)
                    if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
                        self._dispatch_execution_exits(closed_trades)
                        self._dispatch_stop_loss_updates(port)
            return

        # 4. Underlying Asset Processing (Equities / Commodity Futures)
        # Track tick history
        if key not in self.recent_ticks:
            self.recent_ticks[key] = []
        self.recent_ticks[key].append(ltp)
        if len(self.recent_ticks[key]) > 50:
            self.recent_ticks[key].pop(0)

        # 3. Update Rolling Volume Metrics
        profile = self.volume_index.get(key, {})
        v_stats = self.volume_tracker.update(key, vol, ltp, profile, now_ts)

        # 4. Update Cumulative Session Extremes & Flag Pattern Tracking
        cur_h = max(high, ltp)
        cur_l = min(low, ltp) if low > 0 else ltp

        if key not in self.session_high or self.session_high[key] <= 0:
            self.session_high[key] = cur_h
        else:
            self.session_high[key] = max(self.session_high[key], cur_h)

        if key not in self.session_low or self.session_low[key] <= 0:
            self.session_low[key] = cur_l
        else:
            if cur_l > 0:
                self.session_low[key] = min(self.session_low[key], cur_l)

        eff_high = self.session_high[key]
        eff_low = self.session_low[key]
        day_span = max(0.01, eff_high - eff_low)
        range_pos = ((ltp - eff_low) / day_span) * 100.0 if day_span > 0 else 50.0

        # Only declare is_hod / is_lod if day has a meaningful range (> 0.1% opening, > 0.2% midday)
        is_opening = is_opening_45m_session(now_ts)
        min_span = (ltp * 0.001) if is_opening else (ltp * 0.002)
        has_meaningful_range = day_span >= min_span
        is_hod = (ltp >= eff_high - (ltp * 0.0005)) and has_meaningful_range
        is_lod = (ltp <= eff_low + (ltp * 0.0005)) and has_meaningful_range

        p_state = self.pattern_tracker.update(
            instrument_key=key,
            ltp=ltp,
            rvol_5m=v_stats["rvol_5m"],
            rvol_cum=v_stats["rvol_cum"],
            a_vol=v_stats["a_vol"],
            pace=v_stats["pace"],
            range_pos=range_pos,
            is_hod=is_hod,
            is_lod=is_lod,
            now_ts=now_ts,
            has_baseline=v_stats.get("has_baseline", True),
        )

        # 5. Technical Indicators
        ticks = self.recent_ticks[key]
        rsi = compute_rsi(ticks, period=14)
        macd = compute_macd(ticks)
        sr = compute_support_resistance_levels(eff_high, eff_low, close, ltp)

        # 6. Calculate Dual Scores (Relative 100-pt + Unbounded Absolute Score)
        score_res = calculate_setup_score(
            ltp=ltp,
            high=eff_high,
            low=eff_low,
            close=close,
            volume_stats=v_stats,
            tech_indicators=sr,
            pattern_state=p_state,
            nifty_stats=self.nifty_stats,
            recent_ticks=ticks,
            config=self.mom_cfg,
        )

        rel_score = score_res["score"]
        abs_score = score_res["absolute_score"]
        stage = score_res["stage"]
        direction = score_res["direction"]
        chg_pct = ((ltp - close) / close) * 100.0 if close > 0 else 0.0

        # Always update live monitored asset state for real-time dashboard tracking
        self.monitored_assets[key] = {
            "symbol": display_name,
            "key": key,
            "ltp": ltp,
            "chg": chg_pct,
            "rvol_5m": v_stats["rvol_5m"],
            "rvol_cum": v_stats["rvol_cum"],
            "pace": v_stats["pace"],
            "score": rel_score,
            "abs_score": abs_score,
            "stage": stage,
            "direction": direction,
            "range_pos": range_pos,
            "last_update": now_ts,
        }

        # 7. Check for Breakout Execution (Two-Stage Funnel)
        is_breakout = stage in ("💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN", "🚀 BREAKOUT", "🩸 BREAKDOWN")
        
        # Can execute if full breakout meets min_trade_score, OR if min_trade_score is relaxed for testing (< 60)
        can_trade = (is_breakout and rel_score >= self.min_trade_score) or (
            self.min_trade_score < 60 and rel_score >= self.min_trade_score
        )

        # Check S/R clearance gate against active time-of-day tier
        gate = get_time_tier_gate(now_ts, self.time_tier_gates)
        min_clearance = float(gate.get("min_sr_clearance_pct", 0.20))
        if direction == "BULL":
            cand_clearance = float(sr.get("clearance_pct", 5.0))
            is_sr_blocked = (cand_clearance < min_clearance) and not sr.get("hod_breached", False)
        else:
            cand_clearance = float(sr.get("clearance_down_pct", 5.0))
            is_sr_blocked = (cand_clearance < min_clearance) and not sr.get("lod_breached", False)

        if is_sr_blocked and self.min_trade_score >= 60:
            can_trade = False

        # Check entry freeze (e.g. 11:00 - 13:30 IST midday lull freeze or EOD freeze)
        entry_allowed, _ = self.portfolio.risk_manager.is_entry_allowed(now_ts)
        if not entry_allowed and self.min_trade_score >= 60:
            can_trade = False

        # Track confirmed setups meeting viewing threshold
        opt_req_str = ""
        if rel_score >= self.min_score or can_trade:
            opt_req_res = self.option_resolver.compute_option_requirement(
                display_name, ltp, direction=direction, quotes_cache=self.quotes_cache, now_ts=now_ts
            )
            opt_req_str = opt_req_res["raw_str"]

        if rel_score >= self.min_score:
            setup_entry = {
                "symbol": display_name,
                "key": key,
                "ltp": ltp,
                "chg": chg_pct,
                "rvol_5m": v_stats["rvol_5m"],
                "rvol_cum": v_stats["rvol_cum"],
                "pace": v_stats["pace"],
                "score": rel_score,
                "abs_score": abs_score,
                "stage": stage,
                "direction": direction,
                "opt_req": opt_req_str,
                "timestamp": now_ts,
            }
            # Update confirmed list (deduplicated by key/symbol)
            self.confirmed_setups = [s for s in self.confirmed_setups if s["symbol"] != setup_entry["symbol"] and s.get("key") != key]
            self.confirmed_setups.append(setup_entry)
            self.confirmed_setups.sort(key=lambda s: s.get("abs_score", 0.0), reverse=True)
            if len(self.confirmed_setups) > 25:
                self.confirmed_setups.pop()

        if can_trade:
            # If direction is not resolved, determine from price vs close
            if direction not in ("BULL", "BEAR"):
                direction = "BULL" if ltp >= close else "BEAR"

            # Register fresh alert if not already active
            if display_name not in self.fresh_alerts:
                chg_pct = ((ltp - close) / close) * 100.0 if close > 0 else 0.0
                trade_stage = stage if is_breakout else f"⚡ {direction} (Score: {rel_score:.0f})"
                self.fresh_alerts[display_name] = {
                    "symbol": display_name,
                    "key": key,
                    "ltp": ltp,
                    "chg": chg_pct,
                    "rvol_5m": v_stats["rvol_5m"],
                    "rvol_cum": v_stats["rvol_cum"],
                    "pace": v_stats["pace"],
                    "score": rel_score,
                    "abs_score": abs_score,
                    "stage": trade_stage,
                    "opt_req": opt_req_str,
                    "alert_time": now_ts,
                }

            # Add to Scoring Engine 2.0 Actionable Pool
            if not getattr(self, "is_warming_up", False):
                self.actionable_pool.add_candidate(
                    key=key,
                    symbol=display_name,
                    ltp=ltp,
                    direction=direction,
                    stage=stage,
                    setup_type=p_state.get("setup_type", "BREAKOUT"),
                    raw_score=abs_score,
                    now_ts=now_ts,
                    extra_data={
                        "sym": sym,
                        "p_state": p_state,
                    },
                )

        # Stage 2: Process actionable setup pool auction (highest decayed score candidate wins)
        if not getattr(self, "is_warming_up", False):
            sorted_candidates = self.actionable_pool.get_sorted_candidates(now_ts)
            if sorted_candidates:
                top_cand = sorted_candidates[0]
                trade_opened = self._execute_candidate(top_cand, now_ts)
                if trade_opened:
                    self.actionable_pool.remove(top_cand["key"])

        # 8. Update Active Positions & Risk Exits across all mode portfolios
        for m_name, port in self.portfolios.items():
            closed_trades = port.update_positions(self.quotes_cache, now_ts=now_ts)
            if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
                self._dispatch_execution_exits(closed_trades)
                self._dispatch_stop_loss_updates(port)

    def _execute_candidate(self, cand: Dict[str, Any], now_ts: float) -> bool:
        """
        Executes an actionable candidate from the pool across active simulation portfolios.
        Returns True if at least one trade was opened, False otherwise.
        """
        key = cand["key"]
        display_name = cand["symbol"]
        ltp = cand["ltp"]
        direction = cand["direction"]
        stage = cand["stage"]
        extra = cand.get("extra", {})
        sym = extra.get("sym", display_name)
        p_state = extra.get("p_state", {})

        trade_sym = display_name or sym
        contracts_by_mode = {}
        for smode in ("ATM", "ITM", "OTM", "LOW_PREM", "LOW_PREM_ITM", "LOW_PREM_OTM"):
            c = self.option_resolver.resolve_strike(
                symbol=display_name,
                underlying_ltp=ltp,
                direction=direction,
                strike_mode=smode,
                quotes_cache=self.quotes_cache,
                now_ts=now_ts,
                max_quote_age_sec=30.0,
            )
            if not c and sym:
                c = self.option_resolver.resolve_strike(
                    symbol=sym,
                    underlying_ltp=ltp,
                    direction=direction,
                    strike_mode=smode,
                    quotes_cache=self.quotes_cache,
                    now_ts=now_ts,
                    max_quote_age_sec=30.0,
                )
            if not c and key:
                c = self.option_resolver.resolve_strike(
                    symbol=key,
                    underlying_ltp=ltp,
                    direction=direction,
                    strike_mode=smode,
                    quotes_cache=self.quotes_cache,
                    now_ts=now_ts,
                    max_quote_age_sec=30.0,
                )
            if c:
                contracts_by_mode[smode] = c

        primary_contract = contracts_by_mode.get("ATM") or (list(contracts_by_mode.values())[0] if contracts_by_mode else None)
        entry_allowed, block_reason = self.portfolio.risk_manager.is_entry_allowed(now_ts)

        # Check freeze state from disk flags
        self.multi_executor.check_freeze_flags()
        if (
            self.multi_executor.is_frozen("all")
            or self.multi_executor.is_frozen(self.mode)
            or self.portfolio.is_frozen()
        ):
            entry_allowed = False
            block_reason = f"Execution mode {self.mode.upper()} is FROZEN"

        if primary_contract and entry_allowed:
            f_high = p_state.get("bull_flag_high", ltp * 1.01)
            f_low = p_state.get("bull_flag_low", ltp * 0.99)
            primary_opened = []

            for m_name, port in self.portfolios.items():
                if (
                    self.multi_executor.is_frozen("all")
                    or self.multi_executor.is_frozen(m_name)
                    or port.is_frozen()
                ):
                    continue

                if not port.is_position_active(trade_sym) and not port.is_position_active(sym):
                    opened = port.open_position(
                        symbol=trade_sym,
                        contract=primary_contract,
                        direction=direction,
                        underlying_ltp=ltp,
                        option_quote=self.quotes_cache.get(primary_contract.instrument_key, {}),
                        flag_high=f_high,
                        flag_low=f_low,
                        now_ts=now_ts,
                        contracts_by_mode=contracts_by_mode,
                        quotes_cache=self.quotes_cache,
                        option_resolver=self.option_resolver,
                    )
                    if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
                        primary_opened = opened

            exec_track = self.designated_execution_track
            target_trade = next((t for t in primary_opened if t.strategy_track == exec_track), primary_opened[0] if primary_opened else None)
            if target_trade and not getattr(self, "is_backfilling", False):
                if not self.multi_executor.is_frozen("all") and not self.multi_executor.is_frozen(self.mode):
                    order_res = self.multi_executor.place_order(
                        symbol=target_trade.symbol,
                        instrument_token=target_trade.contract_key,
                        quantity=target_trade.quantity,
                        transaction_type="BUY",
                        order_type="MARKET",
                        tag=target_trade.strategy_track,
                        market_quote=self.quotes_cache.get(target_trade.contract_key, {}),
                        stop_loss_price=target_trade.current_sl,
                    )
                    # Synchronize entry price with confirmed broker fill price for precision target & SL computation
                    if isinstance(order_res, dict) and order_res.get("fill_price"):
                        conf_p = float(order_res.get("fill_price") or 0.0)
                        if conf_p > 0:
                            target_trade.entry_price = conf_p
                            target_trade.peak_price = conf_p
                            target_trade.current_sl = round(conf_p * (1.0 - (self.portfolio.risk_manager.option_sl_pct / 100.0)), 2)
            return True if primary_opened else False
        return False

    def on_timer(self, timestamp: float) -> None:
        """Periodic timer event: updates dashboard rendering every second."""
        # Check market session state and log transitions (e.g. CAS auction -> Continuous open at 09:15)
        session, sess_desc = self.portfolio.risk_manager.get_market_session(timestamp)
        if session != self._last_market_session:
            if self._last_market_session:
                if session == "NORMAL_TRADING":
                    print(f"\n🔔 [MARKET OPEN] {self.market_open_time}:00 IST reached. Continuous trading session online.\n")
                elif session == "PRE_MARKET_CAS":
                    print(f"\n🔔 [PRE-MARKET CAS] Call Auction Session active until {self.market_open_time} IST. Orders blocked.\n")
                elif session == "ENTRY_FREEZE":
                    print(f"\n⏰ [ENTRY FREEZE] {self.eod_freeze_time} IST reached. New entries frozen.\n")
                elif session == "EOD_SQUAREOFF":
                    print(f"\n🚪 [EOD SQUAREOFF] {self.eod_square_off_time} IST reached. Auto-liquidating open positions.\n")
                elif session == "POST_MARKET":
                    print(f"\n💤 [MARKET CLOSED] {self.market_close_time} IST reached. Market session ended.\n")
            self._last_market_session = session

        # Check external freeze flags (e.g. runner.py stop-live or logs/.freeze_*)
        self.multi_executor.check_freeze_flags()

        # Check EOD RMS liquidation guard across all adapters (live, paper, paper_delayed)
        self.multi_executor.check_eod_squareoff(
            now_ts=timestamp,
            quotes_cache=self.quotes_cache,
            eod_time=self.eod_square_off_time,
            freeze_time=self.eod_freeze_time,
        )

        # Enforce EOD squareoff across all simulation portfolios (33 tracks per active mode)
        for m_name, port in self.portfolios.items():
            port_closed = port.check_eod_squareoff(self.quotes_cache, now_ts=timestamp)
            if port_closed and (m_name == "paper" or m_name == list(self.portfolios.keys())[0]):
                self._dispatch_execution_exits(port_closed)

        # Update positions and process mature delayed orders across all portfolios
        for m_name, port in self.portfolios.items():
            closed_trades = port.update_positions(self.quotes_cache, now_ts=timestamp)
            if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
                self._dispatch_execution_exits(closed_trades)

        # Scoring Engine 2.0: Purge expired candidates (> 20.0s) from actionable pool
        if hasattr(self, "actionable_pool"):
            self.actionable_pool.purge_expired(timestamp)

        # Dynamically refresh Dynamic Trio macro-regime leaderboard across all mode portfolios
        for port in self.portfolios.values():
            port.update_dynamic_trio_leaderboard(
                now_ts=timestamp,
                quotes_cache=self.quotes_cache,
                nifty_stats=self.nifty_stats,
            )

        if not getattr(self, "enable_dashboard", True):
            return

        if timestamp - self._last_dashboard_render >= 1.0:
            self._last_dashboard_render = timestamp

            active_alerts = []
            for k, a in list(self.fresh_alerts.items()):
                rem = 8.0 - (timestamp - a["alert_time"])
                if rem > 0:
                    a["countdown_str"] = f"⏳ {rem:.1f}s left"
                    active_alerts.append(a)
                else:
                    del self.fresh_alerts[k]

            # Clear and render full live dashboard
            view = render_scanner_dashboard(
                monitored_count=len(self.monitored_assets),
                nifty_stats=self.nifty_stats,
                fresh_alerts=active_alerts,
                confirmed_setups=self.confirmed_setups,
                portfolio=self.portfolio,
                quotes_cache=self.quotes_cache,
                mode=self.mode,
                min_score=self.min_score,
                show_positions=self.show_positions,
                view_mode=self.view_mode,
                multi_executor=self.multi_executor,
                monitored_assets=list(self.monitored_assets.values()),
                portfolios_by_mode=self.portfolios,
            )
            clear_screen()
            print(view)

    def on_order_update(self, order_update: Dict[str, Any]) -> None:
        pass

    def shutdown(self) -> None:
        self._running = False
        pid_file = "logs/strategy.pid"
        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except Exception:
                pass
        self.subscriber.close()
        self.multi_executor.shutdown()

    def run(self) -> int:
        """Run the strategy against the local market data feed."""
        self.initialize()
        if not self.subscriber.connect(timeout_sec=3.0):
            print("\n" + "=" * 80)
            print("❌ CANNOT CONNECT TO LOCAL MARKET DATA FEED!")
            print(f"   Target IPC Socket: {self.subscriber.address}")
            print("=" * 80)
            print("👉 The Market Data Listener service is not running.")
            print("   In a separate terminal, launch the listener first:")
            print("       python3 runner.py listen")
            print("   (Or to test offline with synthetic demo ticks, run: python3 runner.py listen --mode mock)")
            print("=" * 80 + "\n")
            return 1

        # Register PID and OS signal handlers for cross-terminal freeze control
        os.makedirs("logs", exist_ok=True)
        pid_file = "logs/strategy.pid"
        try:
            with open(pid_file, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        import signal

        def _on_sigusr1(signum, frame):
            self.multi_executor.check_freeze_flags()
            print(f"\n❄️ [SIGNAL RECEIVED] Freeze synchronized: {self.multi_executor.frozen_modes}\n")

        def _on_sigusr2(signum, frame):
            self.multi_executor.check_freeze_flags()
            print(f"\n⚡ [SIGNAL RECEIVED] Freeze synchronized: {self.multi_executor.frozen_modes}\n")

        try:
            signal.signal(signal.SIGUSR1, _on_sigusr1)
            signal.signal(signal.SIGUSR2, _on_sigusr2)
        except (AttributeError, ValueError):
            pass

        self._running = True
        print(f"⚡ Momentum Strategy active in {self.mode.upper()} mode. Listening for market ticks...")

        # Backfill today's recorded ticks from market open (or configured start_time)
        if getattr(self, "backfill", True):
            today_str = datetime.now().strftime("%Y%m%d")
            b_start = self.start_time_str or self.market_open_time or "09:15"
            self.backfill_from_recordings(today_str, b_start)

        # Render initial dashboard frame immediately
        self.on_timer(time.time())
        try:
            for event in self.subscriber.stream_events(timeout=0.2, yield_heartbeat=True):
                if not self._running:
                    break
                self.on_market_event(event)
                self.on_timer(time.time())
        except KeyboardInterrupt:
            self.shutdown()
        finally:
            self.shutdown()
            print("\nStrategy stopped.")
        return 0

