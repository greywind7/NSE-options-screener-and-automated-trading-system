#!/usr/bin/env python3
"""
strategies/momentum/portfolio.py
================================
Multi-Track Simulation Portfolio & Dynamic Ensemble Capital Allocator:
1. 16 Isolated Simulation Tracks:
   - Baseline: ATM, ITM, OTM
   - LOW_PREM (Lowest Liquid Option)
   - TRAIL_SL: TRAIL_SL_ATM, TRAIL_SL_ITM, TRAIL_SL_OTM
   - FIXED_TGT: FIXED_TGT_ATM, FIXED_TGT_ITM, FIXED_TGT_OTM
   - COMBO: COMBO_ATM, COMBO_ITM, COMBO_OTM
2. DYNAMIC_TRIO Ensemble Allocator:
   - Divides capital into 3 equal tranches.
   - Day Start: Automatically seeds with the 3 most successful strategies based on the last 7 days of paper trade history.
   - Intraday Rotation: Dynamically rotates capital allocation into today's top 3 performing tracks as trades close.
3. Sandbox Execution Fill Model:
   - Instant fill execution assuming paying the spread (Buy at Ask, Sell at Bid).
   - Flat brokerage deduction (₹30 per order = ₹60 round-trip).
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.common.time_utils import is_quote_fresh, normalize_time_str
from strategies.momentum.risk import RiskManager


CORE_STRATEGY_TRACKS = [
    # 1. Baseline Strikes
    "ATM",
    "ITM",
    "OTM",
    # 2. Trailing Stop Loss Variations
    "TRAIL_SL_ATM",
    "TRAIL_SL_ITM",
    "TRAIL_SL_OTM",
    # 3. Fixed Target Variations
    "FIXED_TGT_ATM",
    "FIXED_TGT_ITM",
    "FIXED_TGT_OTM",
]

FIXED_CAP_SUFFIX = "_FIXED_CAP"

# Base Combined Allocation Tracks (Separate Pools: each sub-track has its isolated cash partition)
BASE_COMBINED_TRACK_CONSTITUENTS: Dict[str, List[str]] = {
    "COMBINED_ATM": ["ATM", "TRAIL_SL_ATM", "FIXED_TGT_ATM"],
    "COMBINED_ITM": ["ITM", "TRAIL_SL_ITM", "FIXED_TGT_ITM"],
    "COMBINED_OTM": ["OTM", "TRAIL_SL_OTM", "FIXED_TGT_OTM"],
    "COMBINED_FIXED_TARGET": ["FIXED_TGT_ATM", "FIXED_TGT_ITM", "FIXED_TGT_OTM"],
    "COMBINED_TRAILING_SL": ["TRAIL_SL_ATM", "TRAIL_SL_ITM", "TRAIL_SL_OTM"],
}

# Shared Pool Combined Tracks (Unified Pool: sub-tracks draw from a shared combined cash pool)
SHARED_COMBINED_TRACK_CONSTITUENTS: Dict[str, List[str]] = {
    # Star Performer Ensemble: Combined Trail Stoploss + Combined ITM (overlapping tracks de-duplicated)
    "COMBINED_STAR": ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "TRAIL_SL_OTM", "ITM", "FIXED_TGT_ITM"],
    "COMBINED_ATM_SHARED": ["ATM", "TRAIL_SL_ATM", "FIXED_TGT_ATM"],
    "COMBINED_ITM_SHARED": ["ITM", "TRAIL_SL_ITM", "FIXED_TGT_ITM"],
    "COMBINED_OTM_SHARED": ["OTM", "TRAIL_SL_OTM", "FIXED_TGT_OTM"],
    "COMBINED_FIXED_TARGET_SHARED": ["FIXED_TGT_ATM", "FIXED_TGT_ITM", "FIXED_TGT_OTM"],
    "COMBINED_TRAILING_SL_SHARED": ["TRAIL_SL_ATM", "TRAIL_SL_ITM", "TRAIL_SL_OTM"],
}

SHARED_POOL_TRACKS: Set[str] = set(SHARED_COMBINED_TRACK_CONSTITUENTS.keys())

COMBINED_TRACK_CONSTITUENTS: Dict[str, List[str]] = {}
COMBINED_TRACK_CONSTITUENTS.update(BASE_COMBINED_TRACK_CONSTITUENTS)
COMBINED_TRACK_CONSTITUENTS.update(SHARED_COMBINED_TRACK_CONSTITUENTS)

# Short alias mapping for Base Tracks (Separate Pools)
COMBINED_TRACK_CONSTITUENTS["COMBINED_FIXED_TGT"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_FIXED_TARGET"]
COMBINED_TRACK_CONSTITUENTS["COMBINED_TRAIL_SL"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_TRAILING_SL"]

# Short alias mapping for Star Performer
COMBINED_TRACK_CONSTITUENTS["COMBINED_ITM_TRAILING_SL"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"]
COMBINED_TRACK_CONSTITUENTS["COMBINED_ITM_TRAIL"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"]
COMBINED_TRACK_CONSTITUENTS["COMBINED_TRAIL_ITM"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"]
COMBINED_TRACK_CONSTITUENTS["STAR"] = COMBINED_TRACK_CONSTITUENTS["COMBINED_STAR"]
SHARED_POOL_TRACKS.update(["COMBINED_ITM_TRAILING_SL", "COMBINED_ITM_TRAIL", "COMBINED_TRAIL_ITM", "STAR"])

# Aliases for Shared Pool Tracks
for k in list(BASE_COMBINED_TRACK_CONSTITUENTS.keys()):
    shared_key = f"{k}_SHARED"
    COMBINED_TRACK_CONSTITUENTS[shared_key] = list(BASE_COMBINED_TRACK_CONSTITUENTS[k])
    SHARED_POOL_TRACKS.add(shared_key)
    COMBINED_TRACK_CONSTITUENTS[f"SHARED_{k}"] = COMBINED_TRACK_CONSTITUENTS[shared_key]
    SHARED_POOL_TRACKS.add(f"SHARED_{k}")
    short_k = k.replace("COMBINED_", "")
    COMBINED_TRACK_CONSTITUENTS[f"SHARED_{short_k}"] = COMBINED_TRACK_CONSTITUENTS[shared_key]
    SHARED_POOL_TRACKS.add(f"SHARED_{short_k}")
    COMBINED_TRACK_CONSTITUENTS[f"{short_k}_SHARED"] = COMBINED_TRACK_CONSTITUENTS[shared_key]
    SHARED_POOL_TRACKS.add(f"{short_k}_SHARED")

# Also map fixed capital non-compounding variants (kept intact for optional explicit activation)
for k, v in list(COMBINED_TRACK_CONSTITUENTS.items()):
    COMBINED_TRACK_CONSTITUENTS[f"{k}{FIXED_CAP_SUFFIX}"] = [
        f"{t}{FIXED_CAP_SUFFIX}" if not t.endswith(FIXED_CAP_SUFFIX) else t for t in v
    ]

BASE_COMBINED_TRACKS = list(BASE_COMBINED_TRACK_CONSTITUENTS.keys())
SHARED_COMBINED_TRACKS = list(SHARED_COMBINED_TRACK_CONSTITUENTS.keys())
COMBINED_STRATEGY_TRACKS = BASE_COMBINED_TRACKS + SHARED_COMBINED_TRACKS

# Fixed capital tracks kept intact in definition for explicit user requests or future re-activation
ALL_FIXED_CAP_TRACKS = (
    [f"{t}{FIXED_CAP_SUFFIX}" for t in CORE_STRATEGY_TRACKS]
    + [f"{t}{FIXED_CAP_SUFFIX}" for t in COMBINED_STRATEGY_TRACKS]
)

# Active execution tracks: Compounding Core Tracks + DYNAMIC_TRIO (Fixed-cap removed from default execution)
CORE_SIM_TRACKS = CORE_STRATEGY_TRACKS + ["DYNAMIC_TRIO"]

# Full simulation matrix of tracks (including fixed-cap definitions for unit test suites and explicit requests)
ALL_SIM_TRACKS = (
    CORE_STRATEGY_TRACKS
    + [f"{t}{FIXED_CAP_SUFFIX}" for t in CORE_STRATEGY_TRACKS]
    + ["DYNAMIC_TRIO"]
)

ALL_COMBINED_TRACKS = (
    COMBINED_STRATEGY_TRACKS
    + [f"{t}{FIXED_CAP_SUFFIX}" for t in COMBINED_STRATEGY_TRACKS]
)

# Minimum starting capital per sub-track required to enable a combined allocation track
MIN_COMBINED_SUBTRACK_CAPITAL = 40_000.0

TRACK_STRIKE_MODE: Dict[str, str] = {
    "ATM": "ATM",
    "ITM": "ITM",
    "OTM": "OTM",
    "TRAIL_SL_ATM": "ATM",
    "TRAIL_SL_ITM": "ITM",
    "TRAIL_SL_OTM": "OTM",
    "FIXED_TGT_ATM": "ATM",
    "FIXED_TGT_ITM": "ITM",
    "FIXED_TGT_OTM": "OTM",
    "DYNAMIC_TRIO": "ATM",
}
# Map fixed-capital non-compounding variants to their respective option strike selection modes
for t in CORE_STRATEGY_TRACKS:
    TRACK_STRIKE_MODE[f"{t}{FIXED_CAP_SUFFIX}"] = TRACK_STRIKE_MODE[t]
for t in COMBINED_TRACK_CONSTITUENTS.keys():
    TRACK_STRIKE_MODE[t] = "ATM"


@dataclass
class TradeRecord:
    trade_id: str
    strategy_track: str
    symbol: str
    contract_key: str
    contract_symbol: str
    direction: str
    lots: int
    quantity: int
    entry_time: float
    entry_price: float
    exit_time: float = 0.0
    exit_price: float = 0.0
    gross_pnl: float = 0.0
    brokerage: float = 60.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    exit_reason: str = ""
    is_closed: bool = False
    peak_price: float = 0.0
    underlying_entry: float = 0.0
    underlying_sl: float = 0.0
    current_sl: float = 0.0
    sub_track: str = ""

    def compute_mtm(self, quotes_cache: Optional[Dict[str, Any]] = None) -> float:
        """Compute open mark-to-market P&L using best bid from market quotes cache."""
        if not quotes_cache:
            return 0.0
        ck = self.contract_key or ""
        q = (
            quotes_cache.get(ck)
            or quotes_cache.get(ck.replace(":", "|"))
            or quotes_cache.get(ck.replace("|", ":"))
            or (quotes_cache.get(self.contract_symbol) if self.contract_symbol else None)
            or {}
        )
        bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or self.entry_price)
        return (bid - self.entry_price) * self.quantity


class StrategyTrackAccounting:
    """Accounting ledger for one isolated strategy track (including combined allocation tracks)."""

    def __init__(
        self,
        name: str,
        starting_capital: float = 100_000.0,
        allow_compounding: bool = True,
        constituents: Optional[List[str]] = None,
        is_shared_pool: bool = False,
    ):
        self.name = name
        self.starting_capital = starting_capital
        self.allow_compounding = allow_compounding
        self.is_shared_pool = is_shared_pool
        self.cash = starting_capital
        self.gross_realized = 0.0
        self.brokerage_paid = 0.0
        self.net_realized = 0.0
        self.wins = 0
        self.losses = 0
        self.open_positions: Dict[str, TradeRecord] = {}
        self.closed_trades: List[TradeRecord] = []
        self.constituents: List[str] = list(constituents) if constituents else []
        if self.constituents:
            n = len(self.constituents)
            sub_cap = starting_capital / n if n > 0 else starting_capital
            self.sub_starting_capital: Dict[str, float] = {c: sub_cap for c in self.constituents}
            self.sub_cash: Dict[str, float] = {c: sub_cap for c in self.constituents}
        else:
            self.sub_starting_capital = {}
            self.sub_cash = {}

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades) * 100.0 if self.total_trades > 0 else 0.0

    def compute_mtm(self, quotes_cache: Optional[Dict[str, Any]] = None) -> float:
        """Compute open mark-to-market (MTM) liquidation value across all active positions."""
        mtm = 0.0
        if quotes_cache:
            for pos in self.open_positions.values():
                ck = pos.contract_key or ""
                q = (
                    quotes_cache.get(ck)
                    or quotes_cache.get(ck.replace(":", "|"))
                    or quotes_cache.get(ck.replace("|", ":"))
                    or (quotes_cache.get(pos.contract_symbol) if pos.contract_symbol else None)
                    or {}
                )
                # Bid price for liquidation
                bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or pos.entry_price)
                mtm += (bid - pos.entry_price) * pos.quantity
            self.last_mtm = round(mtm, 2)
        elif not self.open_positions:
            self.last_mtm = 0.0
        return getattr(self, "last_mtm", 0.0)

    def compute_roi(self, quotes_cache: Optional[Dict[str, Any]] = None) -> float:
        """
        Compute total Net Return on Investment (ROI %) factoring in open position MTM:
        Total Net Return = Net Realized P&L + Current Open MTM.
        ROI % = (Total Net Return / Starting Capital) * 100.0.
        """
        mtm = self.compute_mtm(quotes_cache) if quotes_cache is not None else getattr(self, "last_mtm", 0.0)
        total_pnl = self.net_realized + mtm
        return round((total_pnl / self.starting_capital) * 100.0, 2) if self.starting_capital > 0 else 0.0

    @property
    def net_roi(self) -> float:
        """Return percentage factoring in current open MTM."""
        return self.compute_roi()

    def compute_rolling_pnl(self, cutoff_ts: float, quotes_cache: Optional[Dict[str, Any]] = None) -> float:
        """
        Compute total net P&L over a rolling lookback window:
        Sum of net realized P&L from trades closed at or after cutoff_ts + current open MTM.
        """
        rolling_realized = sum(t.net_pnl for t in self.closed_trades if t.exit_time >= cutoff_ts)
        mtm = self.compute_mtm(quotes_cache or {})
        return round(rolling_realized + mtm, 2)

    def compute_rolling_roi(self, cutoff_ts: float, quotes_cache: Optional[Dict[str, Any]] = None) -> float:
        """
        Compute net ROI % over the rolling lookback window:
        Rolling ROI % = (Rolling Net P&L including open MTM / Starting Capital) * 100.0.
        """
        rolling_pnl = self.compute_rolling_pnl(cutoff_ts, quotes_cache)
        return round((rolling_pnl / self.starting_capital) * 100.0, 2) if self.starting_capital > 0 else 0.0


class MultiTrackPortfolio:
    """
    Manages all 16 isolated strategy tracks plus the DYNAMIC_TRIO ensemble allocator.
    """

    def __init__(
        self,
        starting_capital: float = 100_000.0,
        output_csv: str = "logs/paper_trades.csv",
        active_tracks: Optional[List[str]] = None,
        track_filter: Optional[str] = None,
        show_positions: bool = False,
        rolling_lookback_minutes: int = 15,
        pnl_aggregator: Optional[Any] = None,
        mode: str = "paper",
        min_delay_ms: float = 200.0,
        max_delay_ms: float = 500.0,
        allow_compounding_on_all: bool = False,
        eod_square_off_time: str = "15:20",
        eod_freeze_time: str = "15:15",
        is_mcx: bool = False,
        market_open_time: str = "09:15",
        market_close_time: str = "15:30",
        freeze_dir: Optional[str] = None,
        risk_config: Optional[Dict[str, Any]] = None,
        allocator_config: Optional[Dict[str, Any]] = None,
        option_resolver: Optional[Any] = None,
    ):
        is_default_workspace_path = (
            output_csv.startswith("logs/")
            or output_csv.startswith("recordings/")
            or output_csv in ("logs/paper_trades.csv", "logs/paper_delayed_trades.csv", "logs/live_trades.csv")
        )
        if is_default_workspace_path and (os.environ.get("TRADING_TEST_MODE") == "1" or "unittest" in sys.modules):
            import tempfile, atexit
            output_csv = os.path.join(tempfile.gettempdir(), f"test_port_trades_{os.getpid()}_{id(self)}.csv")
            try:
                atexit.register(lambda: os.path.exists(output_csv) and os.remove(output_csv))
            except Exception:
                pass

        self.starting_capital = starting_capital
        self.output_csv = output_csv
        self.freeze_dir = freeze_dir
        self.show_positions = show_positions
        self.rolling_lookback_minutes = rolling_lookback_minutes
        self.pnl_aggregator = pnl_aggregator
        self.mode = mode.lower()
        self.min_delay_ms = float(min_delay_ms)
        self.max_delay_ms = float(max_delay_ms)
        self.allow_compounding_on_all = allow_compounding_on_all
        self.eod_square_off_time = normalize_time_str(eod_square_off_time, "23:25" if is_mcx else "15:20")
        self.eod_freeze_time = normalize_time_str(eod_freeze_time, "23:15" if is_mcx else "15:15")
        self.is_mcx = is_mcx
        self.market_open_time = normalize_time_str(market_open_time, "09:00" if is_mcx else "09:15")
        self.market_close_time = normalize_time_str(market_close_time, "23:30" if is_mcx else "15:30")
        self.pending_orders: List[Dict[str, Any]] = []

        if active_tracks:
            candidate_tracks = list(active_tracks)
        elif track_filter:
            tokens = [p.strip().upper() for p in track_filter.split(",") if p.strip()]
            all_possible = ALL_SIM_TRACKS + ALL_COMBINED_TRACKS + ALL_FIXED_CAP_TRACKS
            matched = set()
            include_fixed = any("FIXED_CAP" in tok or "FX" in tok for tok in tokens)
            for tf in tokens:
                if tf in ("ALL", "*"):
                    # Compounding core + dynamic trio + combined tracks (excluding fixed cap from default execution)
                    matched.update(CORE_SIM_TRACKS + COMBINED_STRATEGY_TRACKS)
                    if include_fixed:
                        matched.update(ALL_FIXED_CAP_TRACKS)
                elif tf in ("CORE", "CORE_ALL"):
                    matched.update(CORE_STRATEGY_TRACKS)
                    if include_fixed:
                        matched.update(f"{t}{FIXED_CAP_SUFFIX}" for t in CORE_STRATEGY_TRACKS)
                elif tf in ("COMBINED", "COMBO", "COMBINED_ALL"):
                    matched.update(COMBINED_STRATEGY_TRACKS)
                    if include_fixed:
                        matched.update(f"{t}{FIXED_CAP_SUFFIX}" for t in COMBINED_STRATEGY_TRACKS)
                elif tf in ("BASE_COMBINED", "COMBINED_BASE", "COMBINED_SEPARATE"):
                    matched.update(BASE_COMBINED_TRACKS)
                elif tf in ("SHARED", "SHARED_ALL", "COMBINED_SHARED"):
                    matched.update(SHARED_COMBINED_TRACKS)
                elif tf in ("FIXED_CAP", "FIXED_CAPITAL", "FX", "ALL_FIXED"):
                    matched.update(ALL_FIXED_CAP_TRACKS)
                elif tf in ("DYNAMIC_TRIO", "TRIO"):
                    # In active execution, Dynamic Trio evaluates core compounding tracks
                    matched.update(CORE_SIM_TRACKS)
                    if include_fixed:
                        matched.update(f"{t}{FIXED_CAP_SUFFIX}" for t in CORE_STRATEGY_TRACKS)
                elif tf in ("ATM", "ITM", "OTM"):
                    active_pool = CORE_SIM_TRACKS + COMBINED_STRATEGY_TRACKS
                    matched.update(t for t in active_pool if tf in t and (include_fixed or not t.endswith(FIXED_CAP_SUFFIX)))
                elif tf in ("TRAIL_SL", "TRAILING"):
                    active_pool = CORE_SIM_TRACKS + COMBINED_STRATEGY_TRACKS
                    matched.update(t for t in active_pool if ("TRAIL_SL" in t or "TRAILING" in t) and (include_fixed or not t.endswith(FIXED_CAP_SUFFIX)))
                elif tf in ("FIXED_TGT", "FIXED", "FIXED_TARGET"):
                    active_pool = CORE_SIM_TRACKS + COMBINED_STRATEGY_TRACKS
                    matched.update(t for t in active_pool if ("FIXED_TGT" in t or "FIXED_TARGET" in t) and (include_fixed or not t.endswith(FIXED_CAP_SUFFIX)))
                elif tf in all_possible or tf in COMBINED_TRACK_CONSTITUENTS:
                    matched.add(tf)
                else:
                    sub_match = [t for t in all_possible if tf in t and (include_fixed or not t.endswith(FIXED_CAP_SUFFIX))]
                    if sub_match:
                        matched.update(sub_match)
                    else:
                        matched.update(CORE_SIM_TRACKS)
            candidate_tracks = [t for t in all_possible if t in matched]
        else:
            candidate_tracks = ALL_SIM_TRACKS

        # Filter out combined tracks if capital per sub-track < 40k
        filtered_tracks = []
        for t in candidate_tracks:
            constituents = COMBINED_TRACK_CONSTITUENTS.get(t)
            if constituents:
                sub_cap = starting_capital / len(constituents)
                if sub_cap < MIN_COMBINED_SUBTRACK_CAPITAL:
                    # User constraint: "If by dividing capital equally makes available capital to any one track fall below 40K, just disable it."
                    continue
            filtered_tracks.append(t)

        self.tracks_to_run = filtered_tracks

        def _check_is_shared(t_name: str) -> bool:
            clean = t_name.replace(FIXED_CAP_SUFFIX, "")
            return (
                clean in SHARED_POOL_TRACKS
                or "_SHARED" in clean
                or clean.startswith("SHARED_")
                or clean == "COMBINED_STAR"
                or clean.startswith("COMBINED_STAR")
            )

        self.tracks: Dict[str, StrategyTrackAccounting] = {
            t: StrategyTrackAccounting(
                name=t,
                starting_capital=starting_capital,
                allow_compounding=True if allow_compounding_on_all else (not t.endswith(FIXED_CAP_SUFFIX)),
                constituents=COMBINED_TRACK_CONSTITUENTS.get(t),
                is_shared_pool=_check_is_shared(t),
            )
            for t in self.tracks_to_run
        }

        self.risk_config = risk_config or {}
        self.allocator_config = allocator_config or {}
        self.option_resolver = option_resolver

        r_cfg = self.risk_config
        self.risk_manager = RiskManager(
            option_sl_pct=float(r_cfg.get("option_stop_loss_pct", r_cfg.get("option_sl_pct", 7.5))),
            initial_target_pct=float(r_cfg.get("initial_target_pct", 15.0)),
            decay_floor_pct=float(r_cfg.get("target_decay_floor_pct", r_cfg.get("decay_floor_pct", 2.5))),
            target_decay_sec=float(r_cfg.get("target_decay_sec", float(r_cfg.get("target_decay_minutes", 15.0)) * 60.0)),
            stagnation_timeout_sec=float(r_cfg.get("stagnation_timeout_sec", float(r_cfg.get("stagnation_timeout_minutes", 30.0)) * 60.0)),
            step_size_pct=float(r_cfg.get("step_size_pct", 2.5)),
            step_sl_pct=float(r_cfg.get("step_sl_pct", 2.5)),
            trail_breakeven_trigger_pct=float(r_cfg.get("trail_breakeven_trigger_pct", 2.5)),
            trailing_step_pct=float(r_cfg.get("trailing_step_pct", 1.0)),
            fixed_target_inr=float(r_cfg.get("fixed_target_inr", 750.0)),
            fixed_target_timeout_sec=float(r_cfg.get("fixed_target_timeout_sec", float(r_cfg.get("fixed_target_timeout_minutes", 10.0)) * 60.0)),
            fixed_target_min_5pct_val=float(r_cfg.get("fixed_target_min_5pct_val", 500.0)),
            broker_max_order_modifications=int(r_cfg.get("broker_max_order_modifications", 25)),
            breakeven_target_pct=float(r_cfg.get("breakeven_target_pct", 0.5)),
            brokerage_per_order=float(r_cfg.get("brokerage_per_order", 30.0)),
            underlying_breathing_room_pct=float(r_cfg.get("underlying_breathing_room_pct", 0.80)),
            eod_square_off_time=eod_square_off_time,
            eod_freeze_time=eod_freeze_time,
            is_mcx=is_mcx,
            market_open_time=market_open_time,
            market_close_time=market_close_time,
            midday_freeze_start=str(r_cfg.get("midday_freeze_start", "11:00")),
            midday_freeze_end=str(r_cfg.get("midday_freeze_end", "13:30")),
            midday_freeze_enabled=bool(r_cfg.get("midday_freeze_enabled", True)),
            max_option_spread_pct=float(r_cfg.get("max_option_spread_pct", r_cfg.get("max_spread_pct", 3.0))),
        )
        self._session_id = datetime.now().strftime("%H%M%S")
        self._trade_counter = 0

        # Dynamic Trio Leaderboard Configuration & State
        a_cfg = self.allocator_config
        self.dynamic_trio_tranches = int(a_cfg.get("tranches", 3))
        self.dynamic_trio_lookback_days = int(a_cfg.get("historical_lookback_days", 7))
        self.rolling_lookback_minutes = int(a_cfg.get("rolling_lookback_minutes", rolling_lookback_minutes or 10))
        self.dynamic_trio_roi_weight = float(a_cfg.get("roi_weight", 0.50))
        self.dynamic_trio_winrate_weight = float(a_cfg.get("winrate_weight", 0.50))
        self.dynamic_trio_fixed_cap_blend = float(a_cfg.get("fixed_cap_blend_weight", 2.0 / 3.0))
        self.dynamic_trio_compounding_blend = float(a_cfg.get("compounding_blend_weight", 1.0 / 3.0))
        self.dynamic_trio_diversification_floor = float(a_cfg.get("diversification_floor_pct", 0.15))
        self.dynamic_trio_prop_weight = float(a_cfg.get("blend_proportional_weight", 0.70))
        self.dynamic_trio_equal_weight = float(a_cfg.get("blend_equal_weight", 0.30))

        self.dynamic_trio_champions: List[str] = []
        self.dynamic_trio_weights: Dict[str, float] = {}
        self._initialize_dynamic_trio()

    def _initialize_dynamic_trio(self) -> None:
        """
        Seed DYNAMIC_TRIO with the most successful strategy tracks based on past week's paper trade journal.
        Evaluates performance using Fixed-Cap + Compounding blended scoring.
        """
        historical_stats = self._analyze_historical_journal(days=self.dynamic_trio_lookback_days)
        if len(historical_stats) >= self.dynamic_trio_tranches:
            sorted_strats = sorted(historical_stats.keys(), key=lambda s: historical_stats[s]["eval_score"], reverse=True)
            self.dynamic_trio_champions = sorted_strats[:self.dynamic_trio_tranches]
        else:
            self.dynamic_trio_champions = ["TRAIL_SL_ATM", "ATM", "FIXED_TGT_ATM"][:self.dynamic_trio_tranches]

        n = len(self.dynamic_trio_champions)
        eq_w = round(1.0 / n, 4) if n > 0 else 0.0
        self.dynamic_trio_weights = {t: eq_w for t in self.dynamic_trio_champions}

    def _analyze_historical_journal(self, days: int = 7) -> Dict[str, Dict[str, float]]:
        """
        Parse dated recordings/{date}/paper_trades.csv and historical journals to calculate
        past performance per candidate base track using the 2/3 Non-Compounding (Fixed Cap)
        + 1/3 Compounding evaluation scoring.
        """
        candidate_files: List[str] = []
        if os.path.exists(self.output_csv):
            candidate_files.append(self.output_csv)

        # Discover dated CSVs across recordings/
        base_csv_name = os.path.basename(self.output_csv)
        if os.path.exists("recordings"):
            for folder in sorted(os.listdir("recordings"), reverse=True):
                p = os.path.join("recordings", folder, base_csv_name)
                if os.path.exists(p) and p not in candidate_files:
                    candidate_files.append(p)

        # Legacy fallbacks in logs/
        for leg in ("logs/paper_trades.csv", "logs/paper_delayed_trades.csv"):
            if os.path.exists(leg) and leg not in candidate_files:
                candidate_files.append(leg)

        if not candidate_files:
            return {}

        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        strat_pnl: Dict[str, float] = defaultdict(float)
        strat_wins: Dict[str, int] = defaultdict(int)
        strat_trades: Dict[str, int] = defaultdict(int)

        for c_file in candidate_files:
            try:
                with open(c_file, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        ts_str = r.get("entry_time", "") or r.get("timestamp", "")
                        if ts_str and ts_str[:10] < cutoff_date:
                            continue

                        strat = (r.get("strategy_track") or r.get("strike_sim") or "").strip()
                        if not strat or strat == "DYNAMIC_TRIO":
                            continue

                        try:
                            net_p = float(r.get("net_pnl") or 0.0)
                            strat_pnl[strat] += net_p
                            strat_trades[strat] += 1
                            if net_p > 0:
                                strat_wins[strat] += 1
                        except (ValueError, TypeError):
                            pass
            except Exception:
                pass

        base_strats = set()
        for s in strat_pnl.keys():
            base_name = s[:-len(FIXED_CAP_SUFFIX)] if s.endswith(FIXED_CAP_SUFFIX) else s
            base_strats.add(base_name)

        result = {}
        for base in base_strats:
            fix_name = f"{base}{FIXED_CAP_SUFFIX}"
            pnl_fix = strat_pnl.get(fix_name, 0.0)
            pnl_cmp = strat_pnl.get(base, 0.0)

            has_fix = fix_name in strat_pnl
            has_cmp = base in strat_pnl

            fix_w = self.dynamic_trio_fixed_cap_blend
            cmp_w = self.dynamic_trio_compounding_blend
            roi_w = self.dynamic_trio_roi_weight
            pnl_w = 1.0 - roi_w

            if has_fix and has_cmp:
                pnl_eval = fix_w * pnl_fix + cmp_w * pnl_cmp
            elif has_fix:
                pnl_eval = pnl_fix
            else:
                pnl_eval = pnl_cmp

            roi_fix = (pnl_fix / self.starting_capital) * 100.0 if self.starting_capital > 0 else 0.0
            roi_cmp = (pnl_cmp / self.starting_capital) * 100.0 if self.starting_capital > 0 else 0.0
            if has_fix and has_cmp:
                roi_eval = fix_w * roi_fix + cmp_w * roi_cmp
            elif has_fix:
                roi_eval = roi_fix
            else:
                roi_eval = roi_cmp

            # P&L + ROI Composite Evaluation Score
            roi_equiv_pnl = roi_eval * (self.starting_capital / 100.0)
            eval_score = pnl_w * pnl_eval + roi_w * roi_equiv_pnl

            tot = strat_trades.get(base, 0) + strat_trades.get(fix_name, 0)
            tot_w = strat_wins.get(base, 0) + strat_wins.get(fix_name, 0)
            wr = (tot_w / tot * 100.0) if tot > 0 else 0.0
            result[base] = {
                "eval_score": round(eval_score, 2),
                "pnl_score": round(pnl_eval, 2),
                "roi": round(roi_eval, 2),
                "win_rate": wr,
                "trades": tot,
            }
        return result

    def update_dynamic_trio_leaderboard(
        self,
        now_ts: Optional[float] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        nifty_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Dynamically adjust the 3 champions and compute proportional fund allocation weights.
        In Scoring Engine 2.0:
        1. When macro market regime is active (Nifty 50 trend >= +0.35% or <= -0.35%), allocates
           based on index trend, market breadth, and volatility:
           - PRO_BULL: ['TRAIL_SL_ATM' (40%), 'ATM' (35%), 'TRAIL_SL_OTM' (25%)]
           - PRO_BEAR: ['TRAIL_SL_ITM' (40%), 'ITM' (35%), 'TRAIL_SL_ATM' (25%)]
        2. In neutral or fallback regime, evaluates based on a 50% P&L + 50% ROI composite score
           over the rolling lookback window.
        """
        if now_ts is None:
            now_ts = time.time()
        # Avoid redundant recalculations if called within 5 seconds unless forced
        if getattr(self, "_last_trio_update_ts", 0.0) > 0 and (now_ts - self._last_trio_update_ts < 5.0):
            return
        self._last_trio_update_ts = now_ts

        # -------------------------------------------------------------
        # Engine 2.0 Macro Regime Allocation
        # -------------------------------------------------------------
        if nifty_stats:
            regime = str(nifty_stats.get("regime", "NEUTRAL")).upper()
            nifty_pct = float(nifty_stats.get("pct_change", 0.0))

            if regime == "PRO_BULL" or nifty_pct >= 0.35:
                top_bull = [t for t in ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "ITM"] if t in self.tracks]
                if top_bull:
                    self.dynamic_trio_champions = top_bull
                    if len(top_bull) == 3:
                        self.dynamic_trio_weights = {top_bull[0]: 0.50, top_bull[1]: 0.30, top_bull[2]: 0.20}
                    else:
                        eq = round(1.0 / len(top_bull), 4)
                        self.dynamic_trio_weights = {t: eq for t in top_bull}
                    return
            elif regime == "PRO_BEAR" or nifty_pct <= -0.35:
                top_bear = [t for t in ["TRAIL_SL_ITM", "ITM", "TRAIL_SL_ATM"] if t in self.tracks]
                if top_bear:
                    self.dynamic_trio_champions = top_bear
                    if len(top_bear) == 3:
                        self.dynamic_trio_weights = {top_bear[0]: 0.50, top_bear[1]: 0.30, top_bear[2]: 0.20}
                    else:
                        eq = round(1.0 / len(top_bear), 4)
                        self.dynamic_trio_weights = {t: eq for t in top_bear}
                    return

        cutoff_ts = now_ts - (self.rolling_lookback_minutes * 60.0)

        # Base candidate strategies to evaluate
        candidates = [s for s in CORE_STRATEGY_TRACKS if s in self.tracks or f"{s}{FIXED_CAP_SUFFIX}" in self.tracks]
        if not candidates:
            candidates = [t for t in self.tracks if t != "DYNAMIC_TRIO" and not t.endswith(FIXED_CAP_SUFFIX)]
        if not candidates:
            candidates = [t for t in self.tracks if t != "DYNAMIC_TRIO"]

        rolling_scores: Dict[str, float] = {}
        cumulative_scores: Dict[str, float] = {}

        for strat in candidates:
            t_cmp = self.tracks.get(strat)
            t_fix = self.tracks.get(f"{strat}{FIXED_CAP_SUFFIX}")

            # 1. Net P&L (Rolling & Cumulative, including open MTM)
            pnl_cmp = t_cmp.compute_rolling_pnl(cutoff_ts, quotes_cache) if t_cmp else 0.0
            pnl_fix = t_fix.compute_rolling_pnl(cutoff_ts, quotes_cache) if t_fix else 0.0

            cum_cmp = t_cmp.net_realized if t_cmp else 0.0
            cum_fix = t_fix.net_realized if t_fix else 0.0

            if t_fix is not None and t_cmp is not None:
                r_pnl = (2.0 / 3.0) * pnl_fix + (1.0 / 3.0) * pnl_cmp
                c_pnl = (2.0 / 3.0) * cum_fix + (1.0 / 3.0) * cum_cmp
            elif t_fix is not None:
                r_pnl = pnl_fix
                c_pnl = cum_fix
            else:
                r_pnl = pnl_cmp
                c_pnl = cum_cmp

            # 2. ROI Metric (Rolling & Cumulative, factoring in open position MTM)
            roi_cmp = t_cmp.compute_roi(quotes_cache) if t_cmp else 0.0
            roi_fix = t_fix.compute_roi(quotes_cache) if t_fix else 0.0

            rolling_roi_cmp = t_cmp.compute_rolling_roi(cutoff_ts, quotes_cache) if t_cmp else 0.0
            rolling_roi_fix = t_fix.compute_rolling_roi(cutoff_ts, quotes_cache) if t_fix else 0.0

            fix_w = self.dynamic_trio_fixed_cap_blend
            cmp_w = self.dynamic_trio_compounding_blend
            roi_w = self.dynamic_trio_roi_weight
            pnl_w = 1.0 - roi_w

            if t_fix is not None and t_cmp is not None:
                r_roi = fix_w * rolling_roi_fix + cmp_w * rolling_roi_cmp
                c_roi = fix_w * roi_fix + cmp_w * roi_cmp
            elif t_fix is not None:
                r_roi = rolling_roi_fix
                c_roi = roi_fix
            else:
                r_roi = rolling_roi_cmp
                c_roi = roi_cmp

            # 3. P&L + ROI Composite Evaluation Score
            r_roi_val = r_roi * (self.starting_capital / 100.0)
            c_roi_val = c_roi * (self.starting_capital / 100.0)

            combined_rolling = pnl_w * r_pnl + roi_w * r_roi_val
            combined_cum = pnl_w * c_pnl + roi_w * c_roi_val

            rolling_scores[strat] = round(combined_rolling, 2)
            cumulative_scores[strat] = round(combined_cum, 2)

        # Check if any candidate has activity in rolling lookback window
        has_activity = any(abs(v) > 0.01 for v in rolling_scores.values())

        if has_activity:
            ranked = sorted(
                candidates,
                key=lambda s: (rolling_scores.get(s, 0.0), cumulative_scores.get(s, 0.0)),
                reverse=True,
            )
        else:
            ranked = sorted(
                candidates,
                key=lambda s: cumulative_scores.get(s, 0.0),
                reverse=True,
            )

        top_n = ranked[:self.dynamic_trio_tranches]
        self.dynamic_trio_champions = top_n

        # Calculate proportional allocation weights based on relative evaluation scores
        scores = [max(0.0, float(rolling_scores.get(t, 0.0))) for t in top_n]
        tot_score = sum(scores)
        n_tranches = len(top_n)

        if tot_score > 0.0 and n_tranches > 0:
            raw_w = [s / tot_score for s in scores]
            eq_w = 1.0 / n_tranches
            blended = [self.dynamic_trio_prop_weight * w + self.dynamic_trio_equal_weight * eq_w for w in raw_w]
            norm_factor = sum(blended)
            weights = [round(b / norm_factor, 4) for b in blended]
        else:
            eq_val = round(1.0 / n_tranches, 4) if n_tranches > 0 else 0.0
            weights = [eq_val] * n_tranches
            if weights:
                weights[-1] = round(1.0 - sum(weights[:-1]), 4)

        self.dynamic_trio_weights = {top_n[i]: weights[i] for i in range(len(top_n))}

    def is_position_active(self, symbol: str) -> bool:
        """Check if any track currently holds an active open position or pending order for this symbol."""
        for acct in self.tracks.values():
            if any(p.symbol == symbol for p in acct.open_positions.values()):
                return True
        if hasattr(self, "pending_orders"):
            if any(p.get("symbol") == symbol for p in self.pending_orders):
                return True
        return False

    def is_instrument_held(self, contract_key: str, symbol: str = "") -> bool:
        """Check if any active open position holds this option contract or underlying symbol."""
        if not hasattr(self, "_active_held_keys"):
            self._rebuild_held_keys()
        if contract_key in self._active_held_keys:
            return True
        if symbol and symbol in self._active_held_keys:
            return True
        clean_k = contract_key.replace(":", "|")
        if clean_k in self._active_held_keys:
            return True
        clean_k2 = contract_key.replace("|", ":")
        if clean_k2 in self._active_held_keys:
            return True
        return False

    def _rebuild_held_keys(self) -> None:
        """Rebuild set of active contract keys and symbols for O(1) membership check."""
        s = set()
        for acct in self.tracks.values():
            for pos in acct.open_positions.values():
                if pos.contract_key:
                    s.add(pos.contract_key)
                    s.add(pos.contract_key.replace(":", "|"))
                    s.add(pos.contract_key.replace("|", ":"))
                if pos.contract_symbol:
                    s.add(pos.contract_symbol)
                if pos.symbol:
                    s.add(pos.symbol)
        if getattr(self, "pending_orders", None):
            for po in self.pending_orders:
                s.add(po.get("symbol", ""))
                c = po.get("contract")
                if c:
                    ck = getattr(c, "instrument_key", "")
                    if ck:
                        s.add(ck)
                        s.add(ck.replace(":", "|"))
                        s.add(ck.replace("|", ":"))
        self._active_held_keys = s

    def freeze(self) -> None:
        """Programmatically freeze this portfolio from new entries."""
        self._is_frozen = True

    def unfreeze(self) -> None:
        """Programmatically unfreeze this portfolio."""
        self._is_frozen = False

    def is_frozen(self) -> bool:
        """Check if this portfolio's execution mode is frozen from new entries."""
        if getattr(self, "_is_frozen", False):
            return True
        freeze_dirs = ["logs"]
        if getattr(self, "freeze_dir", None):
            freeze_dirs.append(self.freeze_dir)
        if self.output_csv and os.path.dirname(self.output_csv):
            freeze_dirs.append(os.path.dirname(self.output_csv))

        mode_str = getattr(self, "mode", "paper").lower()
        for fdir in set(freeze_dirs):
            if os.path.exists(os.path.join(fdir, ".freeze_all")):
                return True
            if os.path.exists(os.path.join(fdir, f".freeze_{mode_str}")):
                return True
        return False

    def _execute_open_trades(
        self,
        symbol: str,
        contract: Any,
        direction: str,
        underlying_ltp: float,
        option_quote: Dict[str, Any],
        flag_high: float,
        flag_low: float,
        now_ts: float,
        contracts_by_mode: Optional[Dict[str, Any]] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        option_resolver: Optional[Any] = None,
    ) -> List[TradeRecord]:
        opened_trades = []
        default_ask = float(option_quote.get("ask") or option_quote.get("ltp") or option_quote.get("last_price") or 0.0)

        for track_name, acct in self.tracks.items():
            if track_name == "DYNAMIC_TRIO":
                continue  # Managed by ensemble mirroring

            if getattr(acct, "constituents", None):
                # Combined Allocation Track: all constituent sub-tracks share the full combined account pool
                # Initialize used_contract_keys with contracts already OPEN in this portfolio for this symbol
                existing_open_keys = {
                    pos.contract_key for pos in acct.open_positions.values() if pos.symbol == symbol and pos.contract_key
                } | {
                    pos.contract_symbol for pos in acct.open_positions.values() if pos.symbol == symbol and pos.contract_symbol
                }
                used_contract_keys = set(existing_open_keys)
                resolver = option_resolver or self.option_resolver

                for sub_track in acct.constituents:
                    s_clean = sub_track.replace(FIXED_CAP_SUFFIX, "")
                    mode = TRACK_STRIKE_MODE.get(s_clean, "ATM")
                    track_contract = (contracts_by_mode or {}).get(mode, contract)

                    # Recursive Strike Diversification Loop:
                    # If duplicate contract detected (already open or taken by preceding sub-track),
                    # recursively resolve the next ITM strike. Also advances if quote is unquoted, stale, or wide spread.
                    valid_contract = None
                    valid_quote = None
                    valid_ask = 0.0

                    while track_contract is not None:
                        t_key = getattr(track_contract, "instrument_key", "")
                        t_sym = getattr(track_contract, "trading_symbol", "")

                        if (t_key in used_contract_keys or t_sym in used_contract_keys) and resolver:
                            alt_c = resolver.resolve_next_itm_strike(
                                symbol=symbol,
                                underlying_ltp=underlying_ltp,
                                direction=direction,
                                exclude_contract_keys=used_contract_keys,
                            )
                            if alt_c and alt_c != track_contract:
                                track_contract = alt_c
                                continue
                            else:
                                track_contract = None
                                break

                        # Structural safety guard: Contract underlying must strictly match the position symbol!
                        c_und = getattr(track_contract, "underlying_symbol", None)
                        if isinstance(c_und, str) and isinstance(symbol, str):
                            c_u = c_und.upper()
                            s_u = symbol.upper()
                            if c_u != s_u and s_u not in c_u and c_u not in s_u:
                                track_contract = None
                                break

                        t_quote = {}
                        if quotes_cache and t_key:
                            t_quote = (
                                quotes_cache.get(t_key)
                                or quotes_cache.get(t_key.replace(":", "|"))
                                or quotes_cache.get(t_key.replace("|", ":"))
                                or (quotes_cache.get(t_sym) if t_sym else None)
                                or {}
                            )
                        elif track_contract == contract:
                            t_quote = option_quote or {}

                        c_ask = float(t_quote.get("ask") or 0.0)
                        if c_ask <= 0:
                            c_ask = float(t_quote.get("last_price") or t_quote.get("ltp") or 0.0)

                        if c_ask <= 0:
                            if t_key: used_contract_keys.add(t_key)
                            if t_sym: used_contract_keys.add(t_sym)
                            if resolver and ("ITM" in sub_track or mode == "ITM"):
                                continue
                            track_contract = None
                            break

                        is_fresh, _ = is_quote_fresh(t_quote, now_ts=now_ts, max_age_sec=30.0)
                        if not is_fresh:
                            if t_key: used_contract_keys.add(t_key)
                            if t_sym: used_contract_keys.add(t_sym)
                            if resolver and ("ITM" in sub_track or mode == "ITM"):
                                continue
                            track_contract = None
                            break

                        c_bid = float(t_quote.get("bid") or 0.0)
                        spread_ok, _ = self.risk_manager.check_spread_allowed(ask=c_ask, bid=c_bid)
                        if not spread_ok:
                            if t_key: used_contract_keys.add(t_key)
                            if t_sym: used_contract_keys.add(t_sym)
                            if resolver and ("ITM" in sub_track or mode == "ITM"):
                                continue
                            track_contract = None
                            break

                        # Candidate contract is unique, quoted, fresh, and liquid!
                        valid_contract = track_contract
                        valid_quote = t_quote
                        valid_ask = c_ask
                        break

                    if not valid_contract:
                        continue

                    track_contract = valid_contract
                    track_quote = valid_quote
                    ask = valid_ask
                    t_key = getattr(track_contract, "instrument_key", "")
                    t_sym = getattr(track_contract, "trading_symbol", "")

                    if t_key:
                        used_contract_keys.add(t_key)
                    if t_sym:
                        used_contract_keys.add(t_sym)

                    lot_size = getattr(track_contract, "lot_size", 1)
                    req_cost = ask * lot_size

                    if "FIXED_TGT" in sub_track or "FIXED_TARGET" in sub_track:
                        five_pct_val = 0.05 * req_cost
                        if five_pct_val < self.risk_manager.fixed_target_min_5pct_val:
                            continue

                    # Check capital pool availability:
                    if getattr(acct, "is_shared_pool", False):
                        # SHARED FULL POOL:
                        # All constituent sub-tracks draw directly from the shared combined account cash pool!
                        if acct.cash < req_cost:
                            continue
                    else:
                        # SEPARATE POOLS:
                        # Constituent sub-track has its own isolated sub_cash partition!
                        sub_c = acct.sub_cash.get(sub_track, 0.0)
                        if sub_c < req_cost or acct.cash < req_cost:
                            continue

                    self._trade_counter += 1
                    trade_id = f"TRD-{track_name}_{sub_track}-{symbol}-{self._session_id}-{self._trade_counter:04d}"

                    und_sl = self.risk_manager.compute_underlying_stop(underlying_ltp, direction, flag_high, flag_low)
                    opt_sl = ask * (1.0 - (self.risk_manager.option_sl_pct / 100.0))

                    contract_key = getattr(track_contract, "instrument_key", f"NSE_FO|{symbol}_{mode}")
                    contract_sym = getattr(track_contract, "trading_symbol", f"{symbol}_{mode}")

                    trade = TradeRecord(
                        trade_id=trade_id,
                        strategy_track=track_name,
                        symbol=symbol,
                        contract_key=contract_key,
                        contract_symbol=contract_sym,
                        direction=direction,
                        lots=1,
                        quantity=lot_size,
                        entry_time=now_ts,
                        entry_price=ask,
                        peak_price=ask,
                        underlying_entry=underlying_ltp,
                        underlying_sl=und_sl,
                        current_sl=opt_sl,
                        sub_track=sub_track,
                    )

                    acct.cash -= req_cost
                    if sub_track in acct.sub_cash:
                        acct.sub_cash[sub_track] -= req_cost
                    acct.open_positions[trade_id] = trade
                    opened_trades.append(trade)

                    if self.pnl_aggregator:
                        try:
                            self.pnl_aggregator.record_trade_open(
                                trade_id=trade_id,
                                strategy="momentum",
                                track=track_name,
                                mode=self.mode,
                                symbol=symbol,
                                contract_key=contract_key,
                                contract_symbol=contract_sym,
                                direction=direction,
                                quantity=lot_size,
                                entry_price=ask,
                                entry_time=now_ts,
                                peak_price=ask,
                                current_sl=opt_sl,
                                underlying_entry=underlying_ltp,
                                underlying_sl=und_sl,
                                brokerage=30.0,
                            )
                        except Exception:
                            pass
                continue

            mode = TRACK_STRIKE_MODE.get(track_name, "ATM")
            track_contract = (contracts_by_mode or {}).get(mode, contract)

            # Strict safety guard: Contract underlying must strictly match the position symbol!
            c_und = getattr(track_contract, "underlying_symbol", None)
            if isinstance(c_und, str) and isinstance(symbol, str):
                c_u = c_und.upper()
                s_u = symbol.upper()
                if c_u != s_u and s_u not in c_u and c_u not in s_u:
                    continue

            # Resolve quote specifically for this track's contract key
            t_key = getattr(track_contract, "instrument_key", "")
            t_sym = getattr(track_contract, "trading_symbol", "")

            track_quote = {}
            if quotes_cache and t_key:
                track_quote = (
                    quotes_cache.get(t_key)
                    or quotes_cache.get(t_key.replace(":", "|"))
                    or quotes_cache.get(t_key.replace("|", ":"))
                    or (quotes_cache.get(t_sym) if t_sym else None)
                    or {}
                )
            elif track_contract == contract:
                track_quote = option_quote or {}

            # Strict market data enforcement: Pay Best Ask (instant spread fill). Fall back to LTP if Ask unquoted.
            ask = float(track_quote.get("ask") or 0.0)
            if ask <= 0:
                ask = float(track_quote.get("last_price") or track_quote.get("ltp") or 0.0)

            # ZERO TOLERANCE FOR HALLUCINATED / SYNTHETIC PRICES:
            # If real market quote is missing (ask <= 0), NEVER fabricate an arbitrary multiplier of the underlying stock.
            # Skip opening position for this track cleanly.
            if ask <= 0:
                continue

            # -------------------------------------------------------------
            # STALENESS GATE (30-SECOND HARD THRESHOLD):
            # Quotes older than 30 seconds are strictly invalid for entry execution
            # across ALL modes (Simulation, Paper, Paper-Delayed, Sandbox, Live).
            # Prevents trading on obsolete cache states when contracts are pruned or inactive.
            # -------------------------------------------------------------
            is_fresh, quote_age = is_quote_fresh(track_quote, now_ts=now_ts, max_age_sec=30.0)
            if not is_fresh:
                continue

            # -------------------------------------------------------------
            # OPTION SPREAD LIQUIDITY GATE:
            # Rejects illiquid contracts where the bid-ask spread is too wide
            # (e.g. spread > 3.0% or spread >= 50% of the stop loss buffer).
            # Prevents instant stop-loss liquidation on fill.
            # -------------------------------------------------------------
            bid = float(track_quote.get("bid") or 0.0)
            spread_ok, spread_reason = self.risk_manager.check_spread_allowed(ask=ask, bid=bid)
            if not spread_ok:
                continue

            lot_size = getattr(track_contract, "lot_size", 1)
            req_cost = ask * lot_size

            # FIXED_TGT filter: "dont select the strike if the 5% is less than 500"
            if "FIXED_TGT" in track_name:
                five_pct_val = 0.05 * req_cost
                if five_pct_val < self.risk_manager.fixed_target_min_5pct_val:
                    continue

            if acct.cash < req_cost:
                continue

            self._trade_counter += 1
            trade_id = f"TRD-{track_name}-{symbol}-{self._session_id}-{self._trade_counter:04d}"

            und_sl = self.risk_manager.compute_underlying_stop(underlying_ltp, direction, flag_high, flag_low)
            opt_sl = ask * (1.0 - (self.risk_manager.option_sl_pct / 100.0))

            contract_key = getattr(track_contract, "instrument_key", f"NSE_FO|{symbol}_{mode}")
            contract_sym = getattr(track_contract, "trading_symbol", f"{symbol}_{mode}")

            trade = TradeRecord(
                trade_id=trade_id,
                strategy_track=track_name,
                symbol=symbol,
                contract_key=contract_key,
                contract_symbol=contract_sym,
                direction=direction,
                lots=1,
                quantity=lot_size,
                entry_time=now_ts,
                entry_price=ask,
                peak_price=ask,
                underlying_entry=underlying_ltp,
                underlying_sl=und_sl,
                current_sl=opt_sl,
            )

            acct.cash -= req_cost
            acct.open_positions[trade_id] = trade
            opened_trades.append(trade)

            if self.pnl_aggregator:
                try:
                    self.pnl_aggregator.record_trade_open(
                        trade_id=trade_id,
                        strategy="momentum",
                        track=track_name,
                        mode=self.mode,
                        symbol=symbol,
                        contract_key=contract_key,
                        contract_symbol=contract_sym,
                        direction=direction,
                        quantity=lot_size,
                        entry_price=ask,
                        entry_time=now_ts,
                        peak_price=ask,
                        current_sl=opt_sl,
                        underlying_entry=underlying_ltp,
                        underlying_sl=und_sl,
                        brokerage=30.0,
                    )
                except Exception:
                    pass

        # Mirror trade for DYNAMIC_TRIO ensemble across champion tracks
        dt_acct = self.tracks.get("DYNAMIC_TRIO")
        if dt_acct and opened_trades:
            matching = [
                t for t in opened_trades
                if (t.strategy_track in self.dynamic_trio_champions or
                    t.strategy_track.replace(FIXED_CAP_SUFFIX, "") in self.dynamic_trio_champions)
            ]
            if matching:
                matching.sort(key=lambda t: 1 if t.strategy_track.endswith(FIXED_CAP_SUFFIX) else 0)
                champ_trade = matching[0]
                champ_base = champ_trade.strategy_track.replace(FIXED_CAP_SUFFIX, "")
                t_weight = self.dynamic_trio_weights.get(champ_base, self.dynamic_trio_weights.get(champ_trade.strategy_track, 1.0 / 3.0))
                tranche_cap = dt_acct.cash * t_weight
                req_cost = champ_trade.entry_price * champ_trade.quantity
                if tranche_cap >= req_cost:
                    self._trade_counter += 1
                    dt_id = f"TRD-DYNAMIC_TRIO-{symbol}-{self._session_id}-{self._trade_counter:04d}"
                    dt_trade = TradeRecord(
                        trade_id=dt_id,
                        strategy_track="DYNAMIC_TRIO",
                        symbol=symbol,
                        contract_key=champ_trade.contract_key,
                        contract_symbol=champ_trade.contract_symbol,
                        direction=direction,
                        lots=1,
                        quantity=champ_trade.quantity,
                        entry_time=now_ts,
                        entry_price=champ_trade.entry_price,
                        peak_price=champ_trade.peak_price,
                        underlying_entry=underlying_ltp,
                        underlying_sl=champ_trade.underlying_sl,
                        current_sl=champ_trade.current_sl,
                    )
                    dt_acct.cash -= req_cost
                    dt_acct.open_positions[dt_id] = dt_trade
                    opened_trades.append(dt_trade)

                    if self.pnl_aggregator:
                        try:
                            self.pnl_aggregator.record_trade_open(
                                trade_id=dt_id,
                                strategy="momentum",
                                track="DYNAMIC_TRIO",
                                mode=self.mode,
                                symbol=symbol,
                                contract_key=champ_trade.contract_key,
                                contract_symbol=champ_trade.contract_symbol,
                                direction=direction,
                                quantity=champ_trade.quantity,
                                entry_price=champ_trade.entry_price,
                                entry_time=now_ts,
                                peak_price=champ_trade.peak_price,
                                current_sl=champ_trade.current_sl,
                                underlying_entry=underlying_ltp,
                                underlying_sl=champ_trade.underlying_sl,
                                brokerage=30.0,
                            )
                        except Exception:
                            pass

        if opened_trades:
            self._rebuild_held_keys()

        return opened_trades

    def open_position(
        self,
        symbol: str,
        contract: Any,
        direction: str,
        underlying_ltp: float,
        option_quote: Dict[str, Any],
        flag_high: float,
        flag_low: float,
        now_ts: Optional[float] = None,
        contracts_by_mode: Optional[Dict[str, Any]] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        option_resolver: Optional[Any] = None,
    ) -> List[TradeRecord]:
        """
        Open simultaneous positions across all active strategy tracks.
        Executes instant spread fill (Buy at Ask) or queues for delayed fill if latency enabled.
        """
        if now_ts is None:
            now_ts = time.time()

        # Market Hours & Entry Freeze Guard: Block new position entries outside normal trading session (e.g. CAS auction 09:00-09:15)
        allowed, reason = self.risk_manager.is_entry_allowed(now_ts)
        if not allowed:
            return []

        # External Execution Mode Freeze Guard: Block new entries if portfolio mode or 'all' is frozen
        if self.is_frozen():
            return []

        if self.min_delay_ms > 0:
            is_fresh, _ = is_quote_fresh(option_quote, now_ts=now_ts, max_age_sec=30.0)
            if not is_fresh and option_quote:
                return []
            import random
            delay_ms = random.uniform(self.min_delay_ms, self.max_delay_ms)
            self.pending_orders.append({
                "symbol": symbol,
                "contract": contract,
                "direction": direction,
                "underlying_ltp": underlying_ltp,
                "option_quote": option_quote,
                "flag_high": flag_high,
                "flag_low": flag_low,
                "now_ts": now_ts,
                "target_fill_ts": now_ts + (delay_ms / 1000.0),
                "delay_ms": delay_ms,
                "contracts_by_mode": contracts_by_mode,
            })
            return []

        return self._execute_open_trades(
            symbol=symbol,
            contract=contract,
            direction=direction,
            underlying_ltp=underlying_ltp,
            option_quote=option_quote,
            flag_high=flag_high,
            flag_low=flag_low,
            now_ts=now_ts,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes_cache,
            option_resolver=option_resolver or self.option_resolver,
        )

    def update_positions(
        self,
        quotes_cache: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> List[TradeRecord]:
        """
        Evaluate active positions against risk rules and execute fills at Bid.
        Also processes mature pending delayed orders.
        """
        if now_ts is None:
            now_ts = time.time()

        # If market is closed, outside continuous hours, EOD freeze/squareoff is active, or mode is frozen: discard pending latency orders
        if getattr(self, "pending_orders", None) and (not self.risk_manager.is_market_open(now_ts) or self.risk_manager.is_eod_freeze(now_ts) or self.risk_manager.is_eod_squareoff(now_ts) or self.is_frozen()):
            self.pending_orders.clear()

        # 1. Process any mature pending orders (for latency simulation)
        if getattr(self, "pending_orders", None):
            mature = [p for p in self.pending_orders if now_ts >= p["target_fill_ts"]]
            self.pending_orders = [p for p in self.pending_orders if now_ts < p["target_fill_ts"]]
            for p in mature:
                self._execute_open_trades(
                    symbol=p["symbol"],
                    contract=p["contract"],
                    direction=p["direction"],
                    underlying_ltp=p["underlying_ltp"],
                    option_quote=p["option_quote"],
                    flag_high=p["flag_high"],
                    flag_low=p["flag_low"],
                    now_ts=now_ts,
                    contracts_by_mode=p["contracts_by_mode"],
                    quotes_cache=quotes_cache,
                )

        closed_trades = []

        # Fast exit if no open positions in any track
        if not any(acct.open_positions for acct in self.tracks.values()):
            return closed_trades

        for track_name, acct in self.tracks.items():
            to_close = []
            for tid, pos in acct.open_positions.items():
                ck = pos.contract_key or ""
                opt_q = (
                    quotes_cache.get(ck)
                    or quotes_cache.get(ck.replace(":", "|"))
                    or quotes_cache.get(ck.replace("|", ":"))
                    or (quotes_cache.get(pos.contract_symbol) if pos.contract_symbol else None)
                    or {}
                )
                und_q = quotes_cache.get(pos.symbol, {})

                # Pay spread: liquidate at Best Bid. If bid unquoted, use last_price / ltp
                bid = float(opt_q.get("bid") or 0.0)
                if bid <= 0:
                    bid = float(opt_q.get("last_price") or opt_q.get("ltp") or 0.0)
                if bid <= 0:
                    # No real market quote available for this option contract yet in quotes_cache.
                    # Do NOT fabricate prices or assume entry price; skip exit evaluation until real tick arrives.
                    continue
                und_p = float(und_q.get("last_price") or und_q.get("ltp") or pos.underlying_entry)

                pos_dict = {
                    "entry_price": pos.entry_price,
                    "quantity": pos.quantity,
                    "direction": pos.direction,
                    "entry_time": pos.entry_time,
                    "peak_price": pos.peak_price,
                    "current_sl": pos.current_sl,
                    "underlying_sl": pos.underlying_sl,
                    "strategy_type": getattr(pos, "sub_track", "") or track_name,
                }

                should_exit, reason = self.risk_manager.evaluate_exit(pos_dict, bid, und_p, now_ts)
                pos.current_sl = pos_dict["current_sl"]
                pos.peak_price = pos_dict["peak_price"]

                if should_exit:
                    pos.exit_time = now_ts
                    pos.exit_price = bid
                    pos.exit_reason = reason
                    pos.gross_pnl = (bid - pos.entry_price) * pos.quantity
                    pos.brokerage = 60.0  # ₹30 in + ₹30 out
                    pos.net_pnl = pos.gross_pnl - pos.brokerage
                    pos.return_pct = ((bid - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
                    pos.is_closed = True
                    to_close.append((tid, pos))

            for tid, pos in to_close:
                del acct.open_positions[tid]
                acct.closed_trades.append(pos)
                if getattr(acct, "constituents", None) and getattr(pos, "sub_track", ""):
                    st = pos.sub_track
                    if getattr(acct, "is_shared_pool", False):
                        # Shared Full Pool:
                        if acct.allow_compounding:
                            acct.cash += pos.exit_price * pos.quantity
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                        else:
                            open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                            max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                            acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                    else:
                        # Separate Pools (Isolated per constituent):
                        if acct.allow_compounding:
                            acct.cash += pos.exit_price * pos.quantity
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                        else:
                            open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                            max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                            acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                            if st in acct.sub_cash:
                                sub_cap = acct.sub_starting_capital.get(st, acct.starting_capital / len(acct.constituents))
                                open_sub = sum(p.entry_price * p.quantity for p in acct.open_positions.values() if getattr(p, "sub_track", "") == st)
                                acct.sub_cash[st] = min(max(0.0, sub_cap - open_sub), acct.sub_cash.get(st, 0.0) + (pos.exit_price * pos.quantity))
                elif acct.allow_compounding:
                    acct.cash += pos.exit_price * pos.quantity
                else:
                    # Non-compounding: trading capital pool is capped at starting_capital at max.
                    # Profits are logged to net_realized/ROI, but cannot expand active trading purchasing power.
                    open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                    max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                    acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                acct.gross_realized += pos.gross_pnl
                acct.brokerage_paid += pos.brokerage
                acct.net_realized += pos.net_pnl
                if pos.net_pnl > 0:
                    acct.wins += 1
                else:
                    acct.losses += 1
                closed_trades.append(pos)
                self._log_trade_csv(pos)

                if self.pnl_aggregator:
                    try:
                        self.pnl_aggregator.record_trade_exit(
                            trade_id=pos.trade_id,
                            exit_price=pos.exit_price,
                            exit_time=pos.exit_time,
                            exit_reason=pos.exit_reason,
                            gross_pnl=pos.gross_pnl,
                            net_pnl=pos.net_pnl,
                            brokerage=pos.brokerage,
                            strategy="momentum",
                            track=pos.strategy_track,
                            mode=self.mode,
                            symbol=pos.symbol,
                            contract_key=pos.contract_key,
                            contract_symbol=pos.contract_symbol,
                            direction=pos.direction,
                            quantity=pos.quantity,
                            entry_price=pos.entry_price,
                            entry_time=pos.entry_time,
                        )
                    except Exception:
                        pass

        # After evaluating positions, update Dynamic Trio leaderboard across rolling window
        self.update_dynamic_trio_leaderboard(now_ts=now_ts, quotes_cache=quotes_cache, nifty_stats=getattr(self, "nifty_stats", None))

        if closed_trades:
            self._rebuild_held_keys()

        return closed_trades

    def check_eod_squareoff(
        self,
        quotes_cache: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> List[TradeRecord]:
        """
        Enforce EOD RMS auto-squareoff across all simulation tracks.
        Closes all open positions at Best Bid if current time >= eod_square_off_time.
        """
        if now_ts is None:
            now_ts = time.time()

        if not self.risk_manager.is_eod_squareoff(now_ts):
            return []

        # Cancel any pending latency orders
        if hasattr(self, "pending_orders"):
            self.pending_orders.clear()

        # First pass: normal position update (evaluate_exit will flag EOD_MIS_SQUAREOFF)
        closed = self.update_positions(quotes_cache, now_ts=now_ts)

        # Safety fallback: if any open position still remains, resolve freshest quote or mark absent
        for track_name, acct in self.tracks.items():
            if not acct.open_positions:
                continue
            to_force = list(acct.open_positions.items())
            for tid, pos in to_force:
                ck = pos.contract_key or ""
                opt_q = (
                    quotes_cache.get(ck)
                    or quotes_cache.get(ck.replace(":", "|"))
                    or quotes_cache.get(ck.replace("|", ":"))
                    or (quotes_cache.get(pos.contract_symbol) if pos.contract_symbol else None)
                    or {}
                )
                exit_p = float(opt_q.get("bid") or opt_q.get("last_price") or opt_q.get("ltp") or 0.0)
                if exit_p <= 0:
                    # STRICT ZERO TOLERANCE FOR HALLUCINATED / SYNTHETIC PRICES:
                    # Never liquidate at pos.entry_price to fabricate a 0.0% P&L!
                    # Report error and halt liquidation for unquoted contract.
                    print(
                        f"🚨 [RMS LIQUIDATION BLOCKED] Position {pos.trade_id} ({pos.contract_symbol or ck}) "
                        f"cannot be closed: No verified market quote (bid/ltp) in quotes_cache at {now_ts}. "
                        f"Halting liquidation to avoid fabricated exit prices.",
                        file=sys.stderr,
                    )
                    continue

                pos.exit_time = now_ts
                pos.exit_price = round(exit_p, 2)
                reason_detail = f"Forced @ ₹{exit_p:.2f}"
                pos.exit_reason = f"EOD_MIS_SQUAREOFF ({self.eod_square_off_time} IST Market Close - {reason_detail})"
                pos.gross_pnl = (pos.exit_price - pos.entry_price) * pos.quantity
                pos.brokerage = 60.0
                pos.net_pnl = pos.gross_pnl - pos.brokerage
                pos.return_pct = round(((pos.exit_price - pos.entry_price) / pos.entry_price) * 100.0, 2) if pos.entry_price > 0 else 0.0
                pos.is_closed = True

                del acct.open_positions[tid]
                acct.closed_trades.append(pos)
                if getattr(acct, "constituents", None) and getattr(pos, "sub_track", ""):
                    st = pos.sub_track
                    if getattr(acct, "is_shared_pool", False):
                        # Shared Full Pool:
                        if acct.allow_compounding:
                            acct.cash += pos.exit_price * pos.quantity
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                        else:
                            open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                            max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                            acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                    else:
                        # Separate Pools (Isolated per constituent):
                        if acct.allow_compounding:
                            acct.cash += pos.exit_price * pos.quantity
                            if st in acct.sub_cash:
                                acct.sub_cash[st] += pos.exit_price * pos.quantity
                        else:
                            open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                            max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                            acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                            if st in acct.sub_cash:
                                sub_cap = acct.sub_starting_capital.get(st, acct.starting_capital / len(acct.constituents))
                                open_sub = sum(p.entry_price * p.quantity for p in acct.open_positions.values() if getattr(p, "sub_track", "") == st)
                                acct.sub_cash[st] = min(max(0.0, sub_cap - open_sub), acct.sub_cash.get(st, 0.0) + (pos.exit_price * pos.quantity))
                elif acct.allow_compounding:
                    acct.cash += pos.exit_price * pos.quantity
                else:
                    open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                    max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                    acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))
                acct.gross_realized += pos.gross_pnl
                acct.brokerage_paid += pos.brokerage
                acct.net_realized += pos.net_pnl
                acct.losses += 1
                closed.append(pos)
                self._log_trade_csv(pos)
                if self.pnl_aggregator:
                    try:
                        self.pnl_aggregator.record_trade_exit(
                            trade_id=pos.trade_id,
                            exit_price=pos.exit_price,
                            exit_time=pos.exit_time,
                            exit_reason=pos.exit_reason,
                            gross_pnl=pos.gross_pnl,
                            net_pnl=pos.net_pnl,
                            brokerage=pos.brokerage,
                            strategy="momentum",
                            track=pos.strategy_track,
                            mode=self.mode,
                            symbol=pos.symbol,
                        )
                    except Exception:
                        pass
        return closed

    def get_recent_trades(self, limit: int = 5) -> List[TradeRecord]:
        """
        Retrieve the latest executed trades across all tracks (combining active open and closed trades).
        Sorted newest first.
        """
        all_trades: List[TradeRecord] = []
        for acct in self.tracks.values():
            all_trades.extend(acct.open_positions.values())
            all_trades.extend(acct.closed_trades)

        all_trades.sort(key=lambda t: (t.exit_time if t.is_closed else t.entry_time, t.trade_id), reverse=True)

        # Deduplicate trades by trade_id if any duplicate instances
        seen = set()
        deduped = []
        for t in all_trades:
            if t.trade_id not in seen:
                seen.add(t.trade_id)
                deduped.append(t)
            if len(deduped) >= limit:
                break
        return deduped

    def _log_trade_csv(self, t: TradeRecord) -> None:
        """Append trade record to paper_trades.csv."""
        os.makedirs(os.path.dirname(os.path.abspath(self.output_csv)), exist_ok=True)
        file_exists = os.path.exists(self.output_csv)

        fieldnames = [
            "trade_id", "strategy_track", "symbol", "contract_symbol", "direction",
            "quantity", "entry_time", "entry_price", "exit_time", "exit_price",
            "gross_pnl", "brokerage", "net_pnl", "return_pct", "exit_reason",
        ]

        with open(self.output_csv, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "trade_id": t.trade_id,
                "strategy_track": t.strategy_track,
                "symbol": t.symbol,
                "contract_symbol": t.contract_symbol,
                "direction": t.direction,
                "quantity": t.quantity,
                "entry_time": datetime.fromtimestamp(t.entry_time).strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": round(t.entry_price, 2),
                "exit_time": datetime.fromtimestamp(t.exit_time).strftime("%Y-%m-%d %H:%M:%S"),
                "exit_price": round(t.exit_price, 2),
                "gross_pnl": round(t.gross_pnl, 2),
                "brokerage": round(t.brokerage, 2),
                "net_pnl": round(t.net_pnl, 2),
                "return_pct": round(t.return_pct, 2),
                "exit_reason": t.exit_reason,
            })

