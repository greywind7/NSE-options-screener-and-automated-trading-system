"""
High-Speed Bit-for-Bit Tick-by-Tick Multi-Experiment Simulator (45 Variants x 8 Sessions).

Two-phase architecture guaranteeing exact production ColumnarSimEngine parity:
  Phase 1 (per day): Runs single chronological CandleManager pass over equity_ticks,
    evaluates all unique ScoringEngine3 parameter specs at exact 1.0s boundaries,
    resolves strike depth from options_by_token, and caches compact per-day bundle
    (pre-indexed options_by_token + eq_by_sym + unique-spec opportunity streams).
  Phase 2 (per day x experiment): Replays event-driven chronological heap
    (opportunities + per-held-symbol equity ticks + per-held-token option ticks +
     delayed fills + 1s heartbeats) across all 20 tracks in both paper and paper_delayed.
"""
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import glob
import heapq
import json
import os
import pickle
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import polars as pl

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if not os.path.exists(os.path.join(ROOT_DIR, "runner.py")):
    ROOT_DIR = os.getcwd()
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

from common.interfaces import ExecutionContext, TradeState
from common.tracks.factory import create_tracks, get_enabled_track_names
from core.candles.candle_manager import CandleManager
from core.candles.shm_layout import NUM_BARS, NUM_SYMBOLS, unlink_shm
from core.common.ipc_schemas import ActionableOpportunity, OrderRequest, OrderResult, SetupAlert
from core.common.symbol_utils import is_option_contract, normalize_instrument_token, parse_option_details
from core.common.time_utils import IST
from core.config.config_loader import get_config_manager
from core.rms.mode_risk import ModeRiskManager
from core.rms.rms_service import get_strike_step, passes_spread_gate
from simulation.columnar_sim_engine import SimDelayedExecutionContext, SimExecutionContext
from strategies.momentum.opportunity_pool import OpportunityPool
from strategies.momentum.scoring_engine import SymbolMetadata

_DEFAULT_CLEAN_DATES = [
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
    "2026-11-20",
    "2026-11-23",
    "2026-11-25",
    "2026-11-26",
]

CLEAN_DATES = [d.strip() for d in os.environ.get("SIM45_DATES", "").split(",") if d.strip()] or _DEFAULT_CLEAN_DATES
CACHE_DIR = os.environ.get("SIM45_CACHE_DIR") or os.path.join(ROOT_DIR, "data", "sim45_cache")
RESULTS_DIR = os.environ.get("SIM45_RESULTS_DIR") or os.path.join(ROOT_DIR, "docs", "live", "experiments", "nov16_26_45exp")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


@dataclass(frozen=True)
class ScoringSpec:
    elapsed_min_sec: float = 0.0
    flag_rule: str = "raw_ols"       # raw_ols | buf_15 | buf_25 | full_pole
    pure_rule: str = "raw"           # raw | 3bar_035 | 3bar_045 | 3bar_060 | body_025 | body_035
    min_atr_pct: float = 0.0

    @property
    def key(self) -> str:
        return f"el{int(self.elapsed_min_sec)}_fl{self.flag_rule}_pu{self.pure_rule}_atr{int(round(self.min_atr_pct*10000))}"


@dataclass(frozen=True)
class ExpConfig:
    exp_id: str
    group: str
    description: str
    scoring: ScoringSpec
    midday_freeze: bool = False
    entry_freeze_time: str = "15:00"
    max_spread_pct: float = 0.03
    max_per_symbol: Optional[int] = None
    flip_mode: Optional[str] = None     # None | "ban" | "cooldown_30m"
    opt_scale: bool = False
    opt_spread_gate: bool = False


def build_all_45_experiments() -> List[ExpConfig]:
    """Define the 30 baseline/hypothesis experiments + 15 new sensible combinations."""
    # Exact ScoringSpecs
    S_BASE = ScoringSpec(0.0, "raw_ols", "raw", 0.0)
    S_15S = ScoringSpec(15.0, "raw_ols", "raw", 0.0)
    S_30S = ScoringSpec(30.0, "raw_ols", "raw", 0.0)
    S_ATR30 = ScoringSpec(0.0, "raw_ols", "raw", 0.0030)
    S_3BAR45 = ScoringSpec(0.0, "raw_ols", "3bar_045", 0.0)
    S_BODY25_ATR25 = ScoringSpec(0.0, "raw_ols", "body_025", 0.0025)
    S_BUF15 = ScoringSpec(0.0, "buf_15", "raw", 0.0)
    S_BUF25 = ScoringSpec(0.0, "buf_25", "raw", 0.0)
    S_FULL = ScoringSpec(0.0, "full_pole", "raw", 0.0)

    S_M1 = ScoringSpec(15.0, "buf_15", "raw", 0.0)
    S_M2 = ScoringSpec(15.0, "buf_15", "raw", 0.0030)
    S_M3 = ScoringSpec(15.0, "buf_15", "3bar_045", 0.0025)
    S_M6 = ScoringSpec(15.0, "buf_25", "3bar_045", 0.0030)
    S_M8 = ScoringSpec(15.0, "buf_15", "body_025", 0.0025)

    S_15S_FLAG25 = ScoringSpec(15.0, "buf_25", "raw", 0.0)
    S_15S_FLAG25_3BAR45 = ScoringSpec(15.0, "buf_25", "3bar_045", 0.0)
    S_15S_FLAG25_3BAR35 = ScoringSpec(15.0, "buf_25", "3bar_035", 0.0)
    S_15S_FLAG25_3BAR60 = ScoringSpec(15.0, "buf_25", "3bar_060", 0.0)
    S_15S_FLAG25_ATR20 = ScoringSpec(15.0, "buf_25", "raw", 0.0020)
    S_15S_FLAG25_3BAR45_ATR20 = ScoringSpec(15.0, "buf_25", "3bar_045", 0.0020)
    S_15S_FLAG25_BODY35 = ScoringSpec(15.0, "buf_25", "body_035", 0.0)

    exps = [
        # --- Baseline & 14 Single Hypotheses (15) ---
        ExpConfig("H0_BASELINE", "baseline", "Current Baseline (Unfiltered)", S_BASE),
        ExpConfig("H1_MIDDAY_FREEZE", "single", "Midday Freeze Only (11:30-13:30)", S_BASE, midday_freeze=True),
        ExpConfig("H2a_MIN_ATR_030", "single", "Min 5m ATR >= 0.30%", S_ATR30),
        ExpConfig("H2b_PURE_3BAR_045", "single", "PURE Net 3-Bar Expansion >= 0.45%", S_3BAR45),
        ExpConfig("H2c_BODY_025_ATR_025", "single", "Min ATR >= 0.25% & PURE Body >= 0.25%", S_BODY25_ATR25),
        ExpConfig("H3a_FLAG_BUF_15", "single", "Flag OLS + 15% Buffer (Top 35% of Pole)", S_BUF15),
        ExpConfig("H3b_FLAG_BUF_25", "single", "Flag Trigger >= Pole Top 25%", S_BUF25),
        ExpConfig("H3c_FLAG_FULL_POLE", "single", "Flag Strict Full Pole High/Low (Ref)", S_FULL),
        ExpConfig("H4a_ELAPSED_15S", "single", "Min Candle Elapsed >= 15s", S_15S),
        ExpConfig("H4b_ELAPSED_30S", "single", "Min Candle Elapsed >= 30s (Ref)", S_30S),
        ExpConfig("H5a_CAP_2", "single", "Cap Alerts <= 2 per Symbol/Day", S_BASE, max_per_symbol=2),
        ExpConfig("H5b_CAP_2_NO_FLIP", "single", "Cap <= 2 & Block Opposite Flips", S_BASE, max_per_symbol=2, flip_mode="ban"),
        ExpConfig("H5c_CAP_1", "single", "Cap Alerts <= 1 per Symbol/Day", S_BASE, max_per_symbol=1),
        ExpConfig("H6a_OPT_SCALE", "single", "Option Convexity Target/SL Scaling Only", S_BASE, opt_scale=True),
        ExpConfig("H6b_OPT_SCALE_SPREAD", "single", "Option Convexity Scale + ATR/Spread Gate", S_BASE, opt_scale=True, opt_spread_gate=True),

        # --- 9 Midday-Freeze Mixed Variants ---
        ExpConfig("M1_CORE_CLEAN", "midday_mix", "Midday + 15s + Flag15 + Cap2", S_M1, midday_freeze=True, max_per_symbol=2),
        ExpConfig("M2_BALANCED", "midday_mix", "M1 + Min ATR >= 0.30%", S_M2, midday_freeze=True, max_per_symbol=2),
        ExpConfig("M3_SMART_IMPULSE", "midday_mix", "M1 + 3Bar0.45 + ATR0.25", S_M3, midday_freeze=True, max_per_symbol=2),
        ExpConfig("M4_BALANCED_SCALED", "midday_mix", "M2 + Option Scaling", S_M2, midday_freeze=True, max_per_symbol=2, opt_scale=True),
        ExpConfig("M5_IMPULSE_SCALED", "midday_mix", "M3 + Option Scaling", S_M3, midday_freeze=True, max_per_symbol=2, opt_scale=True),
        ExpConfig("M6_HIGH_CONVICTION", "midday_mix", "Midday + 15s + Flag25 + 3Bar0.45 + ATR0.30 + Cap2NoFlip + Scaled", S_M6, midday_freeze=True, max_per_symbol=2, flip_mode="ban", opt_scale=True),
        ExpConfig("M7_SPREAD_GATE_CORE", "midday_mix", "M1 + NoFlip + OptScaleSpread", S_M1, midday_freeze=True, max_per_symbol=2, flip_mode="ban", opt_scale=True, opt_spread_gate=True),
        ExpConfig("M8_BODY025_SPREAD_SCALED", "midday_mix", "Midday + 15s + Flag15 + ATR0.25 + Body0.25 + Cap2NoFlip + Scaled", S_M8, midday_freeze=True, max_per_symbol=2, flip_mode="ban", opt_scale=True, opt_spread_gate=True),
        ExpConfig("M9_ALL_SIGNALS_SCALED", "midday_mix", "Midday + 15s + Flag15 + OptScaleSpread (No Cap)", S_M1, midday_freeze=True, opt_scale=True, opt_spread_gate=True),

        # --- 6 Non-Midday Winner Combinations ---
        ExpConfig("W1_15S_OPT_SPREAD", "winner", "15s + OptScale + SpreadGate", S_15S, opt_scale=True, opt_spread_gate=True),
        ExpConfig("W2_15S_FLAG25_OPT_SPREAD", "winner", "15s + Flag25 + OptScale + SpreadGate", S_15S_FLAG25, opt_scale=True, opt_spread_gate=True),
        ExpConfig("W3_15S_FLAG25_3BAR045_OPT_SPREAD", "winner", "15s + Flag25 + 3Bar0.45 + OptScale + SpreadGate", S_15S_FLAG25_3BAR45, opt_scale=True, opt_spread_gate=True),
        ExpConfig("W4_3BAR045_OPT_SPREAD", "winner", "PURE 3Bar0.45 + OptScale + SpreadGate", S_3BAR45, opt_scale=True, opt_spread_gate=True),
        ExpConfig("W5_15S_FLAG25_NOFLIP_OPT_SPREAD", "winner", "W2 + Block Opposite-Dir Flips (No Cap)", S_15S_FLAG25, flip_mode="ban", opt_scale=True, opt_spread_gate=True),
        ExpConfig("W6_BODY025_ATR025_OPT_SPREAD", "winner", "Body0.25 + ATR0.25 + OptScale + SpreadGate", S_BODY25_ATR25, opt_scale=True, opt_spread_gate=True),

        # --- 15 New Sensible Extensions (E1..E15) ---
        ExpConfig("E1_15S_FLAG25_SCORING_ONLY", "new_ext", "15s + Flag25 (Pure Scoring, No OptScale/Gate)", S_15S_FLAG25),
        ExpConfig("E2_15S_FLAG25_3BAR045_SCORING", "new_ext", "15s + Flag25 + 3Bar0.45 (Pure Scoring Only)", S_15S_FLAG25_3BAR45),
        ExpConfig("E3_W2_3BAR035", "new_ext", "15s + Flag25 + PURE 3Bar>=0.35% + OptScaleSpread", S_15S_FLAG25_3BAR35, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E4_W2_3BAR060", "new_ext", "15s + Flag25 + PURE 3Bar>=0.60% + OptScaleSpread", S_15S_FLAG25_3BAR60, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E5_W2_ATR020", "new_ext", "W2 + Min 5m ATR >= 0.20%", S_15S_FLAG25_ATR20, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E6_W3_ATR020", "new_ext", "W3 + Min 5m ATR >= 0.20%", S_15S_FLAG25_3BAR45_ATR20, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E7_W2_FLIPCD30", "new_ext", "W2 + 30m Opposite-Flip Cooldown", S_15S_FLAG25, flip_mode="cooldown_30m", opt_scale=True, opt_spread_gate=True),
        ExpConfig("E8_W3_FLIPCD30", "new_ext", "W3 + 30m Opposite-Flip Cooldown", S_15S_FLAG25_3BAR45, flip_mode="cooldown_30m", opt_scale=True, opt_spread_gate=True),
        ExpConfig("E9_W2_CAP3", "new_ext", "W2 + Cap <= 3 per Symbol/Day", S_15S_FLAG25, max_per_symbol=3, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E10_W3_CAP3", "new_ext", "W3 + Cap <= 3 per Symbol/Day", S_15S_FLAG25_3BAR45, max_per_symbol=3, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E11_W2_FREEZE1400", "new_ext", "W2 + Entry Freeze at 14:00 IST", S_15S_FLAG25, entry_freeze_time="14:00", opt_scale=True, opt_spread_gate=True),
        ExpConfig("E12_W3_FREEZE1400", "new_ext", "W3 + Entry Freeze at 14:00 IST", S_15S_FLAG25_3BAR45, entry_freeze_time="14:00", opt_scale=True, opt_spread_gate=True),
        ExpConfig("E13_W2_SPREAD2PCT", "new_ext", "W2 + Tight 2% Option Spread Cap", S_15S_FLAG25, max_spread_pct=0.02, opt_scale=True, opt_spread_gate=True),
        ExpConfig("E14_W2_GATE_ONLY_NOSCALE", "new_ext", "15s + Flag25 + ATR/Spread Gate (No Target/SL Rescale)", S_15S_FLAG25, opt_scale=False, opt_spread_gate=True),
        ExpConfig("E15_15S_FLAG25_BODY035_SCALED", "new_ext", "15s + Flag25 + PURE Body>=0.35% + OptScaleSpread", S_15S_FLAG25_BODY35, opt_scale=True, opt_spread_gate=True),
    ]
    assert len(exps) == 45
    return exps


class CustomScoringEvaluator:
    """Exact mirror of ScoringEngine3 matching ScoringSpec filters without modifying production code."""

    def __init__(self, spec: ScoringSpec, candles: np.ndarray, headers: np.ndarray, index_to_symbol: List[str]):
        self.spec = spec
        self.candles = candles
        self.headers = headers
        self.index_to_symbol = index_to_symbol
        self.min_volume_thrust_mult = 2.0
        self.min_clv = 0.75
        self.invalidation_retrace_pct = 0.50
        self.min_pole_pct = 0.005
        self.max_consolidation_candles = 5
        self.quarantine_cooldown_sec = 5.0
        self.meta: Dict[str, SymbolMetadata] = {s: SymbolMetadata() for s in index_to_symbol}
        self.last_alert_ts_by_sym: Dict[str, float] = {}

    def _has_recent_pole(self, sym_idx: int, bar_idx: int, n: int = 5) -> bool:
        start_bar = max(0, bar_idx - n)
        raw_bars = self.candles[sym_idx, start_bar:bar_idx]
        bars = [b for b in raw_bars if b["open"] > 0]
        n_bars = len(bars)
        if n_bars < 1:
            return False
        for length in (3, 2, 1):
            for i in range(n_bars - length + 1):
                slice_b = bars[i : i + length]
                all_green = all(float(b["close"]) >= float(b["open"]) for b in slice_b)
                all_red = all(float(b["close"]) <= float(b["open"]) for b in slice_b)
                if not (all_green or all_red):
                    continue
                low = float(min(b["low"] for b in slice_b))
                high = float(max(b["high"] for b in slice_b))
                height = high - low
                if low <= 0.0 or height <= 0.01 or (height / low) < self.min_pole_pct:
                    continue
                if any(float(b["rvol"]) >= self.min_volume_thrust_mult for b in slice_b):
                    return True
        return False

    def _check_flag_breakout(
        self, symbol: str, idx: int, bar_idx: int, c: np.void, rvol: float, atr: float, now_ts: float
    ) -> Optional[SetupAlert]:
        if bar_idx < 3:
            return None
        lookback = min(7, bar_idx)
        start_idx = bar_idx - lookback
        raw_hist = self.candles[idx, start_idx:bar_idx]
        hist_tuples = [(start_idx + offset, b) for offset, b in enumerate(raw_hist) if b["open"] > 0]
        n_hist = len(hist_tuples)
        if n_hist < 3:
            return None
        hist_candles = [t[1] for t in hist_tuples]
        hist_bar_indices = [t[0] for t in hist_tuples]
        evaluated_pairs = set()

        for p_start in range(n_hist - 3, -1, -1):
            for pole_len in (3, 2, 1):
                p_end = p_start + pole_len
                if p_end > n_hist - 2:
                    continue
                init_pole = hist_candles[p_start:p_end]
                is_bull_cand = all(float(b["close"]) >= float(b["open"]) for b in init_pole)
                is_bear_cand = all(float(b["close"]) <= float(b["open"]) for b in init_pole)
                if not (is_bull_cand or is_bear_cand):
                    continue
                init_low = float(min(b["low"] for b in init_pole))
                init_high = float(max(b["high"] for b in init_pole))
                init_height = init_high - init_low
                if init_height <= 0.01 or init_low <= 0.0 or (init_height / init_low) < self.min_pole_pct:
                    continue

                cur_direction = "BULL" if is_bull_cand else "BEAR"
                cur_p_start, cur_p_end = p_start, p_end

                while (cur_p_end - cur_p_start) < 3 and cur_p_start > 0:
                    prev_bar = hist_candles[cur_p_start - 1]
                    if cur_direction == "BULL" and not (float(prev_bar["close"]) >= float(prev_bar["open"])):
                        break
                    if cur_direction == "BEAR" and not (float(prev_bar["close"]) <= float(prev_bar["open"])):
                        break
                    cur_pole = hist_candles[cur_p_start:cur_p_end]
                    cur_low = float(min(b["low"] for b in cur_pole))
                    cur_high = float(max(b["high"] for b in cur_pole))
                    cur_height = cur_high - cur_low
                    cur_avg_vol = float(np.mean([float(b["volume"]) for b in cur_pole]))
                    test_pole = hist_candles[cur_p_start - 1 : cur_p_end]
                    test_height = float(max(b["high"] for b in test_pole)) - float(min(b["low"] for b in test_pole))
                    test_avg_vol = float(np.mean([float(b["volume"]) for b in test_pole]))
                    if (test_height - cur_height) / max(0.01, cur_height) < 0.33 or test_avg_vol < 0.90 * cur_avg_vol:
                        break
                    cur_p_start -= 1

                while (cur_p_end - cur_p_start) > 1:
                    cur_pole = hist_candles[cur_p_start:cur_p_end]
                    cur_avg_vol = float(np.mean([float(b["volume"]) for b in cur_pole]))
                    trimmed_pole = hist_candles[cur_p_start : cur_p_end - 1]
                    t_low = float(min(b["low"] for b in trimmed_pole))
                    t_high = float(max(b["high"] for b in trimmed_pole))
                    t_h = t_high - t_low
                    if t_low <= 0.0 or t_h <= 0.01 or (t_h / t_low) < self.min_pole_pct:
                        break
                    if float(np.mean([float(b["volume"]) for b in trimmed_pole])) > cur_avg_vol:
                        cur_p_end -= 1
                    else:
                        break

                if (cur_p_start, cur_p_end) in evaluated_pairs:
                    continue
                evaluated_pairs.add((cur_p_start, cur_p_end))

                pole = hist_candles[cur_p_start:cur_p_end]
                contraction = hist_candles[cur_p_end:]
                if len(contraction) < 2 or len(contraction) > self.max_consolidation_candles:
                    continue
                pole_vol = float(np.mean([float(b["volume"]) for b in pole]))
                contr_vol = float(np.mean([float(b["volume"]) for b in contraction]))
                if pole_vol < 2.0 * contr_vol:
                    continue

                pole_end_bar = hist_bar_indices[cur_p_end - 1]
                self.meta[symbol].record_pole(pole_end_bar)

                pole_low = float(min(b["low"] for b in pole))
                pole_high = float(max(b["high"] for b in pole))
                pole_height = pole_high - pole_low
                if pole_height <= 0.01 or pole_low <= 0.0:
                    continue

                f_rule = self.spec.flag_rule
                close_p = float(c["close"])

                if cur_direction == "BULL":
                    contr_low = float(min(b["low"] for b in contraction))
                    retracement = (pole_high - contr_low) / pole_height
                    if retracement <= self.invalidation_retrace_pct and contr_low >= pole_low:
                        xs = np.arange(len(contraction), dtype=np.float64)
                        ys = np.array([float(b["high"]) for b in contraction], dtype=np.float64)
                        m, c_val = np.polyfit(xs, ys, 1)
                        proj_trigger = float(m * len(contraction) + c_val)
                        req_trig = proj_trigger
                        if f_rule == "buf_15":
                            req_trig = max(proj_trigger, pole_high - 0.35 * pole_height)
                        elif f_rule == "buf_25":
                            req_trig = max(proj_trigger, pole_high - 0.25 * pole_height)
                        elif f_rule == "full_pole":
                            req_trig = max(proj_trigger, pole_high)

                        avg_contr_rov = float(np.mean([float(b["volume"]) / 300.0 for b in contraction]))
                        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
                        curr_rov = float(c["volume"]) / curr_elapsed
                        clv = float(close_p - float(c["low"])) / max(0.01, float(c["high"] - c["low"]))
                        if close_p >= req_trig and curr_rov >= self.min_volume_thrust_mult * avg_contr_rov and clv >= self.min_clv:
                            return SetupAlert(
                                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                                symbol=symbol, direction="BULL", archetype="FLAG_BULL_BREAKOUT",
                                trigger_price=close_p, rvol=rvol, clv=clv, atr=atr,
                                flag_high=pole_high, flag_low=contr_low, timestamp=now_ts,
                                pole_high=pole_high, pole_low=pole_low,
                            )

                if cur_direction == "BEAR":
                    contr_high = float(max(b["high"] for b in contraction))
                    bear_retracement = (contr_high - pole_low) / pole_height
                    if bear_retracement <= self.invalidation_retrace_pct and contr_high <= pole_high:
                        xs = np.arange(len(contraction), dtype=np.float64)
                        ys = np.array([float(b["low"]) for b in contraction], dtype=np.float64)
                        m, c_val = np.polyfit(xs, ys, 1)
                        proj_trigger = float(m * len(contraction) + c_val)
                        req_trig = proj_trigger
                        if f_rule == "buf_15":
                            req_trig = min(proj_trigger, pole_low + 0.35 * pole_height)
                        elif f_rule == "buf_25":
                            req_trig = min(proj_trigger, pole_low + 0.25 * pole_height)
                        elif f_rule == "full_pole":
                            req_trig = min(proj_trigger, pole_low)

                        avg_contr_rov = float(np.mean([float(b["volume"]) / 300.0 for b in contraction]))
                        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
                        curr_rov = float(c["volume"]) / curr_elapsed
                        clv_bear = float(float(c["high"]) - close_p) / max(0.01, float(c["high"] - c["low"]))
                        if close_p <= req_trig and curr_rov >= self.min_volume_thrust_mult * avg_contr_rov and clv_bear >= self.min_clv:
                            return SetupAlert(
                                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                                symbol=symbol, direction="BEAR", archetype="FLAG_BEAR_BREAKDOWN",
                                trigger_price=close_p, rvol=rvol, clv=clv_bear, atr=atr,
                                flag_high=contr_high, flag_low=pole_low, timestamp=now_ts,
                                pole_high=pole_high, pole_low=pole_low,
                            )
        return None

    def _check_pure_breakout(
        self, symbol: str, idx: int, bar_idx: int, c: np.void, rvol: float, atr: float, now_ts: float
    ) -> Optional[SetupAlert]:
        if bar_idx < 3:
            return None
        if self._has_recent_pole(idx, bar_idx, 5) or self.meta[symbol].has_pole_in_last_n_bars(5, bar_idx):
            return None
        prev = self.candles[idx, (bar_idx - 3) : bar_idx]
        if len(prev) < 3 or any(b["open"] <= 0 for b in prev):
            return None

        close_p = float(c["close"])
        open_p = float(c["open"])
        p_rule = self.spec.pure_rule

        # Optional body floor
        if p_rule.startswith("body_"):
            thresh = float(p_rule.split("_")[1]) / 10000.0
            if open_p <= 0.0 or (abs(close_p - open_p) / open_p) < thresh:
                return None

        combined_high = float(max(b["high"] for b in prev))
        combined_low = float(min(b["low"] for b in prev))
        avg_thrust = float(sum(abs(float(b["close"]) - float(b["open"])) for b in prev)) / 3.0
        baseline_thrust = max(avg_thrust, 0.35 * atr)
        curr_thrust = close_p - open_p
        avg_rov = float(sum(b["volume"] / 300.0 for b in prev)) / 3.0
        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
        curr_rov = float(c["volume"]) / curr_elapsed
        clv = float(close_p - float(c["low"])) / max(0.01, float(c["high"] - c["low"]))

        exp_thresh = 0.0
        if p_rule.startswith("3bar_"):
            exp_thresh = float(p_rule.split("_")[1]) / 10000.0

        # Bull Impulse
        if (
            close_p > combined_high
            and curr_thrust >= self.min_volume_thrust_mult * baseline_thrust
            and curr_rov >= self.min_volume_thrust_mult * avg_rov
            and clv >= self.min_clv
        ):
            if exp_thresh > 0.0:
                if combined_low <= 0.0 or ((close_p - combined_low) / combined_low) < exp_thresh:
                    return None
            return SetupAlert(
                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                symbol=symbol, direction="BULL", archetype="PURE_BULL_BREAKOUT",
                trigger_price=close_p, rvol=rvol, clv=clv, atr=atr,
                flag_high=combined_high, flag_low=combined_low, timestamp=now_ts,
                pole_high=combined_high, pole_low=combined_low,
            )

        # Bear Impulse
        curr_bear_thrust = open_p - close_p
        clv_bear = float(float(c["high"]) - close_p) / max(0.01, float(c["high"] - c["low"]))
        if (
            close_p < combined_low
            and curr_bear_thrust >= self.min_volume_thrust_mult * baseline_thrust
            and curr_rov >= self.min_volume_thrust_mult * avg_rov
            and clv_bear >= self.min_clv
        ):
            if exp_thresh > 0.0:
                if combined_high <= 0.0 or ((combined_high - close_p) / combined_high) < exp_thresh:
                    return None
            return SetupAlert(
                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                symbol=symbol, direction="BEAR", archetype="PURE_BEAR_BREAKDOWN",
                trigger_price=close_p, rvol=rvol, clv=clv_bear, atr=atr,
                flag_high=combined_high, flag_low=combined_low, timestamp=now_ts,
                pole_high=combined_high, pole_low=combined_low,
            )
        return None

    def evaluate_symbol(self, idx: int, symbol: str, bar_idx: int, candle: np.void, now_ts: float) -> Optional[SetupAlert]:
        m = self.meta[symbol]
        c_ts = float(candle["timestamp"])
        if c_ts == 0.0 or c_ts == m.last_evaluated_ts:
            return None
        if m.status is not None and m.status_bar_idx == bar_idx:
            return None
        if self.spec.elapsed_min_sec > 0.0 and (now_ts - c_ts) < self.spec.elapsed_min_sec:
            return None

        rvol = float(candle["rvol"])
        if rvol < self.min_volume_thrust_mult:
            return None

        raw_atr = float(candle["atr"])
        atr = raw_atr if raw_atr > 0.0 else max(1.0, float(candle["high"] - candle["low"]))
        close_p = float(candle["close"])
        if self.spec.min_atr_pct > 0.0:
            if close_p <= 0.0 or (atr / close_p) < self.spec.min_atr_pct:
                return None

        flag_alert = self._check_flag_breakout(symbol, idx, bar_idx, candle, rvol, atr, now_ts)
        if flag_alert:
            m.status = flag_alert.archetype
            m.status_bar_idx = bar_idx
            m.last_evaluated_ts = c_ts
            m.record_breakout(bar_idx)
            last_t = self.last_alert_ts_by_sym.get(symbol, 0.0)
            if (now_ts - last_t) >= self.quarantine_cooldown_sec:
                self.last_alert_ts_by_sym[symbol] = now_ts
                return flag_alert
            return None

        if (
            m.has_breakout_in_last_n_bars(3, bar_idx)
            or m.has_pole_in_last_n_bars(5, bar_idx)
            or self._has_recent_pole(idx, bar_idx, 5)
        ):
            m.last_evaluated_ts = c_ts
            return None

        pure_alert = self._check_pure_breakout(symbol, idx, bar_idx, candle, rvol, atr, now_ts)
        if pure_alert:
            m.status = pure_alert.archetype
            m.status_bar_idx = bar_idx
            m.last_evaluated_ts = c_ts
            m.record_breakout(bar_idx)
            last_t = self.last_alert_ts_by_sym.get(symbol, 0.0)
            if (now_ts - last_t) >= self.quarantine_cooldown_sec:
                self.last_alert_ts_by_sym[symbol] = now_ts
                return pure_alert
            return None
        return None


def _get_option_quote_fast(options_by_token: Dict[str, Dict[str, np.ndarray]], token: str, timestamp: float) -> Optional[Dict[str, Any]]:
    norm = normalize_instrument_token(token)
    data = options_by_token.get(norm)
    if not data or len(data["timestamps"]) == 0:
        return None
    ts_arr = data["timestamps"]
    idx = int(np.searchsorted(ts_arr, timestamp, side="right") - 1)
    if idx < 0:
        if len(ts_arr) > 0 and abs(ts_arr[0] - timestamp) <= 10.0:
            idx = 0
        else:
            return None
    return {
        "token": norm,
        "bid": float(data["bids"][idx]),
        "ask": float(data["asks"][idx]),
        "ltp": float(data["ltps"][idx]),
        "bid_qty": int(data["bid_qtys"][idx]),
        "ask_qty": int(data["ask_qtys"][idx]),
        "timestamp": float(ts_arr[idx]),
    }


def _resolve_raw_strikes(
    options_catalog: Dict[str, Dict[str, List[Tuple[float, str]]]],
    options_by_token: Dict[str, Dict[str, np.ndarray]],
    lot_sizes: Dict[str, int],
    symbol: str,
    direction: str,
    trigger_price: float,
    timestamp: float,
) -> Dict[str, Any]:
    """Resolve all near-money strikes with valid 0 < bid < ask depth (spread filtered in Phase 2)."""
    und = symbol.upper()
    otype = "CE" if direction.upper() == "BULL" else "PE"
    available = options_catalog.get(und, {}).get(otype, [])
    if not available:
        return {}
    strikes = [item[0] for item in available]
    idx_atm = int(np.argmin([abs(s - trigger_price) for s in strikes]))
    atm_strike, atm_token = available[idx_atm]
    step = get_strike_step(trigger_price)
    if abs(atm_strike - trigger_price) > max(step * 1.5, trigger_price * 0.02):
        return {}

    itm1_idx = max(0, idx_atm - 1) if otype == "CE" else min(len(available) - 1, idx_atm + 1)
    itm2_idx = max(0, idx_atm - 2) if otype == "CE" else min(len(available) - 1, idx_atm + 2)
    otm1_idx = min(len(available) - 1, idx_atm + 1) if otype == "CE" else max(0, idx_atm - 1)
    otm2_idx = min(len(available) - 1, idx_atm + 2) if otype == "CE" else max(0, idx_atm - 2)

    candidates = {
        "ATM": (atm_strike, atm_token),
        "ITM": available[itm1_idx],
        "ITM1": available[itm1_idx],
        "ITM2": available[itm2_idx],
        "OTM": available[otm1_idx],
        "OTM1": available[otm1_idx],
        "OTM2": available[otm2_idx],
    }
    lot_size = lot_sizes.get(und, 250)
    raw = {}
    for role, (strike_val, tok) in candidates.items():
        q = _get_option_quote_fast(options_by_token, tok, timestamp)
        if not q or q["ask"] <= 0.0 or q["bid"] <= 0.0 or q["bid"] >= q["ask"]:
            continue
        raw[role] = {
            "token": tok, "strike": strike_val, "option_type": otype,
            "ltp": q["ltp"], "bid": q["bid"], "ask": q["ask"],
            "bid_qty": q["bid_qty"], "ask_qty": q["ask_qty"], "lot_size": lot_size,
        }
    return raw


def build_day_bundle(date_str: str) -> str:
    """Phase 1: Build and cache day bundle containing indexed options, eq_by_sym, and opportunities for all unique ScoringSpecs."""
    cache_path = os.path.join(CACHE_DIR, f"bundle_{date_str}.pkl")
    if os.path.exists(cache_path):
        print(f"[CACHE_HIT] {date_str}: {cache_path}", flush=True)
        return cache_path

    t0 = time.time()
    d_nodash = date_str.replace("-", "")
    data_dir = os.path.join("recordings", date_str)
    eq_files = sorted(glob.glob(os.path.join(data_dir, "*equity_ticks*.parquet")))
    opt_files = sorted(glob.glob(os.path.join(data_dir, "*option_depth*.parquet")))
    baseline_file = f"data/index_{d_nodash}.json" if os.path.exists(f"data/index_{d_nodash}.json") else "data/index.json"
    universe_csv = f"data/filtered_{d_nodash}.csv" if os.path.exists(f"data/filtered_{d_nodash}.csv") else "data/filtered.csv"

    print(f"[PHASE1_START] {date_str} | baseline={baseline_file}", flush=True)

    df_eq = pl.read_parquet(eq_files)
    if "token" not in df_eq.columns and "instrument_key" in df_eq.columns:
        df_eq = df_eq.with_columns(pl.col("instrument_key").alias("token"))
    df_eq = df_eq.sort("exchange_timestamp")

    df_opt = pl.read_parquet(opt_files)
    if "token" not in df_opt.columns and "instrument_key" in df_opt.columns:
        df_opt = df_opt.with_columns(pl.col("instrument_key").alias("token"))
    df_opt = df_opt.sort(["token", "exchange_timestamp"])

    # Load manifest & lot sizes
    token_map: Dict[str, Tuple[str, float, str]] = {}
    for mp in [f"data/subscription_manifest_{d_nodash}.json", "data/subscription_manifest.json"]:
        if os.path.exists(mp):
            with open(mp, "r", encoding="utf-8") as f:
                mdata = json.load(f)
            for und, udata in mdata.get("options_map", {}).items():
                for s_str, opt_pair in udata.get("strikes", {}).items():
                    try:
                        sv = float(s_str)
                        for otype, ikey in opt_pair.items():
                            token_map[normalize_instrument_token(ikey)] = (und.upper(), sv, otype.upper())
                    except Exception:
                        pass
            for opt in mdata.get("streamed_popular_options", []):
                norm = normalize_instrument_token(opt.get("instrument_key", ""))
                if norm and norm not in token_map:
                    d = parse_option_details(opt.get("trading_symbol") or opt.get("name"))
                    if d:
                        token_map[norm] = d
            break

    lot_sizes: Dict[str, int] = {}
    if os.path.exists(universe_csv):
        import csv
        with open(universe_csv, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = row.get("trading_symbol") or row.get("symbol") or row.get("name")
                ls = row.get("lot_size")
                if sym and ls:
                    lot_sizes[sym.upper()] = int(ls)

    options_by_token: Dict[str, Dict[str, np.ndarray]] = {}
    options_catalog: Dict[str, Dict[str, List[Tuple[float, str]]]] = {}
    partitions = df_opt.partition_by("token", as_dict=True)
    for tok_t, sub_df in partitions.items():
        tok = tok_t[0] if isinstance(tok_t, tuple) else tok_t
        norm = normalize_instrument_token(tok)
        ts = sub_df["exchange_timestamp"].to_numpy().astype(np.float64)
        bids = sub_df["bid"].to_numpy().astype(np.float64)
        asks = sub_df["ask"].to_numpy().astype(np.float64)
        ltps = sub_df["ltp"].to_numpy().astype(np.float64) if "ltp" in sub_df.columns else bids
        bqs = sub_df["bid_qty"].to_numpy().astype(np.int64) if "bid_qty" in sub_df.columns else np.zeros(len(ts), dtype=np.int64)
        aqs = sub_df["ask_qty"].to_numpy().astype(np.int64) if "ask_qty" in sub_df.columns else np.zeros(len(ts), dtype=np.int64)
        if len(ts) > 1 and not bool(np.all(ts[:-1] <= ts[1:])):
            ord_idx = np.argsort(ts, kind="mergesort")
            ts, bids, asks, ltps, bqs, aqs = ts[ord_idx], bids[ord_idx], asks[ord_idx], ltps[ord_idx], bqs[ord_idx], aqs[ord_idx]
        options_by_token[norm] = {
            "timestamps": ts, "bids": bids, "asks": asks, "ltps": ltps, "bid_qtys": bqs, "ask_qtys": aqs,
        }
        details = parse_option_details(norm) or token_map.get(norm)
        if not details and "symbol" in sub_df.columns and len(sub_df) > 0:
            details = parse_option_details(sub_df["symbol"][0])
        if details:
            und, sv, ot = details
            und = und.upper()
            options_catalog.setdefault(und, {"CE": [], "PE": []})
            if ot in ("CE", "PE"):
                options_catalog[und][ot].append((sv, norm))
    for und in options_catalog:
        for ot in ("CE", "PE"):
            options_catalog[und][ot].sort(key=lambda x: x[0])
    del df_opt, partitions

    # Init CandleManager & unique ScoringEvaluators
    shm_name = f"tmp_phase1_{date_str}_{os.getpid()}"
    unlink_shm(shm_name)
    cm = CandleManager(shm_name=shm_name, universe_csv=universe_csv, baseline_file=baseline_file, enable_zmq=False)

    exps = build_all_45_experiments()
    unique_specs: Dict[str, ScoringSpec] = {}
    for e in exps:
        unique_specs[e.scoring.key] = e.scoring
    evaluators: Dict[str, CustomScoringEvaluator] = {
        sk: CustomScoringEvaluator(sp, cm.candles, cm.headers, cm.index_to_symbol)
        for sk, sp in unique_specs.items()
    }

    ts_arr = df_eq["exchange_timestamp"].to_numpy().astype(np.float64)
    tokens = df_eq["token"].to_list()
    ltps = df_eq["ltp"].to_numpy().astype(np.float64)
    vols = df_eq["volume"].to_numpy().astype(np.int64)
    total_eq = len(ts_arr)
    last_ts = float(ts_arr[-1]) if total_eq > 0 else 0.0

    # Pre-index eq_by_sym for fast Phase 2 event-driven underlying playback
    unique_toks = set(tokens)
    tok_to_idx: Dict[str, int] = {}
    for tk in unique_toks:
        sk = normalize_instrument_token(tk)
        rs = sk.split(":", 1)[1] if ":" in sk else sk
        sidx = cm.symbol_to_index.get(sk)
        if sidx is None:
            sidx = cm.symbol_to_index.get(rs)
        if sidx is not None:
            tok_to_idx[tk] = sidx

    # Build per-canonical-symbol index lists once
    eq_idx_arr = np.fromiter((tok_to_idx.get(tk, -1) for tk in tokens), dtype=np.int32, count=total_eq)
    eq_by_sym: Dict[str, Dict[str, np.ndarray]] = {}
    for sidx, sym in enumerate(cm.index_to_symbol):
        mask = (eq_idx_arr == sidx)
        if np.any(mask):
            eq_by_sym[sym] = {"timestamps": ts_arr[mask], "ltps": ltps[mask]}

    # Vectorized exact IST bar_idx lookup matching datetime.fromtimestamp(ts, tz=IST)
    sec_in_day = (ts_arr + 19800.0).astype(np.int64) % 86400
    elapsed_vec = sec_in_day - 33300  # 09:15 = 33300s
    bar_idx_vec = np.where(elapsed_vec < 0, 0, np.minimum(NUM_BARS - 1, elapsed_vec // 300)).astype(np.int32)
    min_off_vec = np.where(elapsed_vec < 0, 0, np.minimum(4, (elapsed_vec % 300) // 60)).astype(np.int32)

    opps_by_spec: Dict[str, List[ActionableOpportunity]] = {sk: [] for sk in unique_specs}
    raw_strike_cache: Dict[Tuple[float, str, str], Dict[str, Any]] = {}

    last_scoring_eval_ts = 0.0
    for i in range(total_eq):
        t_curr = float(ts_arr[i])
        cm.on_tick(token=tokens[i], ltp=float(ltps[i]), vol=int(vols[i]), ts=t_curr)

        if (t_curr - last_scoring_eval_ts) >= 1.0:
            last_scoring_eval_ts = t_curr
            b_idx = int(bar_idx_vec[i])
            sec_d = int(sec_in_day[i])
            if sec_d < 54000:  # < 15:00 IST (hard cutoff before entry_freeze_time 15:00)
                active_rows = []
                for sidx, sym in enumerate(cm.index_to_symbol):
                    candle = cm.candles[sidx, b_idx]
                    if candle["timestamp"] == 0 or candle["rvol"] < 2.0:
                        continue
                    active_rows.append((sidx, sym, candle.copy()))

                if active_rows:
                    for sk, ev in evaluators.items():
                        alerts_sk = []
                        for sidx, sym, candle in active_rows:
                            al = ev.evaluate_symbol(sidx, sym, b_idx, candle, t_curr)
                            if al is not None:
                                alerts_sk.append(al)
                        if alerts_sk:
                            alerts_sk.sort(key=lambda a: a.rvol, reverse=True)
                            for al in alerts_sk:
                                ck = (t_curr, al.symbol, al.direction)
                                if ck not in raw_strike_cache:
                                    raw_strike_cache[ck] = _resolve_raw_strikes(
                                        options_catalog, options_by_token, lot_sizes,
                                        al.symbol, al.direction, al.trigger_price, t_curr,
                                    )
                                raw_st = raw_strike_cache[ck]
                                if raw_st:
                                    opp = ActionableOpportunity(
                                        opp_id=f"OPP_SIM_{al.symbol}_{int(t_curr*1000)}",
                                        alert_id=al.alert_id,
                                        symbol=al.symbol,
                                        direction=al.direction,
                                        archetype=al.archetype,
                                        trigger_price=al.trigger_price,
                                        strikes=raw_st,
                                        timestamp=t_curr,
                                        flag_high=al.flag_high,
                                        flag_low=al.flag_low,
                                        pole_high=al.pole_high,
                                        pole_low=al.pole_low,
                                        atr=al.atr,
                                        rvol=al.rvol,
                                        clv=al.clv,
                                    )
                                    opps_by_spec[sk].append(opp)

    try:
        cm.stop()
        unlink_shm(shm_name)
    except Exception:
        pass

    bundle = {
        "date": date_str,
        "last_ts": last_ts,
        "options_by_token": options_by_token,
        "eq_by_sym": eq_by_sym,
        "opps_by_spec": opps_by_spec,
        "total_eq": total_eq,
    }
    with open(cache_path, "wb") as f:
        pickle.dump(bundle, f, protocol=pickle.HIGHEST_PROTOCOL)
    elapsed = time.time() - t0
    base_opps = len(opps_by_spec[ExpConfig("H0", "", "", ScoringSpec()).scoring.key])
    print(f"[PHASE1_DONE] {date_str} in {elapsed:.1f}s | H0 raw opps={base_opps}", flush=True)
    return cache_path


def _apply_exp_filters_to_opp(
    opp: ActionableOpportunity,
    exp: ExpConfig,
    sym_counts: Dict[str, int],
    sym_last_dir: Dict[str, Tuple[str, float]],
    freeze_minutes: int,
) -> Optional[ActionableOpportunity]:
    """Filter raw opp by experiment execution gates & spread pct without mutating cached bundle."""
    ts = float(opp.timestamp)
    sec_d = (int(ts) + 19800) % 86400
    curr_minutes = sec_d // 60
    if curr_minutes >= freeze_minutes:
        return None
    if exp.midday_freeze and (11 * 60 + 30) <= curr_minutes < (13 * 60 + 30):
        return None

    # Spread gate (3% or 2%) + optional ATR/Spread gate (H6b)
    filtered_strikes = {}
    spot = float(opp.trigger_price or 0.0)
    atr = float(opp.atr or 0.0)
    for role, st in opp.strikes.items():
        bid = float(st["bid"])
        ask = float(st["ask"])
        if not passes_spread_gate(bid, ask, max_spread_pct=exp.max_spread_pct):
            continue
        if exp.opt_spread_gate and spot > 0.0 and ask > 0.0:
            prem_pct = ask / spot
            spread_pct = (ask - bid) / ask
            exp_opt_move = (0.5 * atr / spot) / max(1e-6, prem_pct)
            if exp_opt_move < 2.0 * spread_pct:
                continue
        filtered_strikes[role] = st

    if not filtered_strikes:
        return None
    if "ITM1" in filtered_strikes and "ITM" not in filtered_strikes:
        filtered_strikes["ITM"] = filtered_strikes["ITM1"]
    if "OTM1" in filtered_strikes and "OTM" not in filtered_strikes:
        filtered_strikes["OTM"] = filtered_strikes["OTM1"]

    sym = opp.symbol
    if exp.max_per_symbol is not None and sym_counts.get(sym, 0) >= exp.max_per_symbol:
        return None

    if exp.flip_mode == "ban":
        prev = sym_last_dir.get(sym)
        if prev is not None and prev[0] != opp.direction:
            return None
    elif exp.flip_mode == "cooldown_30m":
        prev = sym_last_dir.get(sym)
        if prev is not None and prev[0] != opp.direction and (ts - prev[1]) < 1800.0:
            return None

    sym_counts[sym] = sym_counts.get(sym, 0) + 1
    sym_last_dir[sym] = (opp.direction, ts)

    return ActionableOpportunity(
        opp_id=opp.opp_id, alert_id=opp.alert_id, symbol=opp.symbol,
        direction=opp.direction, archetype=opp.archetype,
        trigger_price=opp.trigger_price, strikes=filtered_strikes,
        timestamp=opp.timestamp, flag_high=opp.flag_high, flag_low=opp.flag_low,
        pole_high=opp.pole_high, pole_low=opp.pole_low,
        atr=opp.atr, rvol=opp.rvol, clv=opp.clv,
    )


def _scale_trade_convexity(trade: TradeState, opp: ActionableOpportunity, exp: ExpConfig) -> bool:
    """Apply H6a Option Convexity Target/SL scaling cleanly to newly opened trade. Returns False if SL invalid."""
    if not exp.opt_scale:
        return True
    spot = float(opp.trigger_price or 0.0)
    ask = float(trade.entry_ask or 0.0)
    bid = float(trade.current_bid or 0.0)
    if spot <= 0.0 or ask <= 0.0:
        return True
    prem_pct = ask / spot
    spread_pct = (ask - bid) / ask if bid > 0 else 0.02
    scale = max(0.45, min(1.25, 0.015 / max(1e-6, prem_pct)))
    orig_t = float(trade.metadata.get("target_pct", 0.15))
    orig_s = float(trade.metadata.get("sl_pct", 0.05))
    new_t = orig_t * scale
    sl_floor = max(0.035, min(0.12, spread_pct * 2.5))
    sl_scaled = min(orig_s * 1.25, orig_s * max(0.75, scale))
    new_s = max(sl_floor, sl_scaled)
    new_sl_price = ask * (1.0 - new_s)
    if bid <= new_sl_price:
        return False
    trade.metadata["target_pct"] = new_t
    trade.metadata["sl_pct"] = new_s
    trade.target_price = ask * (1.0 + new_t)
    trade.stop_loss = new_sl_price
    return True


def run_single_experiment_day(bundle: Dict[str, Any], exp: ExpConfig, modes: List[str] = ["paper", "paper_delayed"]) -> Dict[str, Any]:
    """Phase 2: Event-driven tick-by-tick simulation using cached options_by_token + eq_by_sym + opps."""
    random.seed(42)  # Deterministic delayed noise per day+exp
    options_by_token = bundle["options_by_token"]
    eq_by_sym = bundle["eq_by_sym"]
    raw_opps = bundle["opps_by_spec"][exp.scoring.key]
    last_ts = bundle["last_ts"]

    from common.tracks.base_track import _parse_time_to_minutes
    freeze_minutes = _parse_time_to_minutes(exp.entry_freeze_time, 15 * 60)

    cfg = get_config_manager()
    track_names = get_enabled_track_names()
    tracks_by_mode: Dict[str, Dict[str, Any]] = {}
    ctx_delayed: Optional[SimDelayedExecutionContext] = None
    for m in modes:
        if m == "paper_delayed":
            ctx_delayed = SimDelayedExecutionContext(min_delay_ms=200.0, max_delay_ms=500.0, enable_circuit_breaker=False)
            tracks_by_mode[m] = create_tracks(ctx_delayed, track_filter=track_names, entry_freeze_time=exp.entry_freeze_time, eod_squareoff_time="15:15")
        else:
            ctx = SimExecutionContext(mode=m, enable_circuit_breaker=False)
            tracks_by_mode[m] = create_tracks(ctx, track_filter=track_names, entry_freeze_time=exp.entry_freeze_time, eod_squareoff_time="15:15")

    sym_counts: Dict[str, int] = {}
    sym_last_dir: Dict[str, Tuple[str, float]] = {}
    accepted_opps: List[ActionableOpportunity] = []
    for raw_opp in raw_opps:
        f_opp = _apply_exp_filters_to_opp(raw_opp, exp, sym_counts, sym_last_dir, freeze_minutes)
        if f_opp is not None:
            accepted_opps.append(f_opp)

    open_trades_by_token: Dict[str, List[Tuple[str, str, Any, TradeState]]] = {}
    open_trades_by_symbol: Dict[str, List[Tuple[str, str, Any, TradeState]]] = {}
    open_tracks: Dict[Tuple[str, str], Any] = {}
    opt_heap: List[Tuple[float, str, int]] = []
    opt_cursor: Dict[str, int] = {}
    eq_heap: List[Tuple[float, str, int]] = []
    eq_cursor: Dict[str, int] = {}
    delayed_heap: List[Tuple[float, int, OrderRequest]] = []
    pending_delayed_meta: Dict[str, Tuple[str, str, Any, TradeState]] = {}
    latest_equity_ltp: Dict[str, float] = {}
    delay_seq = 0
    next_hb_ts: Optional[float] = None

    def _prune() -> None:
        nonlocal next_hb_ts
        for tk in list(open_trades_by_token.keys()):
            alive = [e for e in open_trades_by_token[tk] if e[3].status == "OPEN"]
            if alive:
                open_trades_by_token[tk] = alive
            else:
                open_trades_by_token.pop(tk, None)
                opt_cursor.pop(tk, None)
        for sk in list(open_trades_by_symbol.keys()):
            alive = [e for e in open_trades_by_symbol[sk] if e[3].status == "OPEN"]
            if alive:
                open_trades_by_symbol[sk] = alive
            else:
                open_trades_by_symbol.pop(sk, None)
                eq_cursor.pop(sk, None)
        for k in list(open_tracks.keys()):
            if not any(t.status == "OPEN" for t in open_tracks[k].active_trades.values()):
                open_tracks.pop(k, None)
        if not open_tracks:
            next_hb_ts = None

    def _resolve_owner_track(tr_obj: Any, trade: TradeState) -> Any:
        if hasattr(tr_obj, "sub_tracks"):
            for st in tr_obj.sub_tracks:
                if trade.trade_id in st.active_trades:
                    return st
        return tr_obj

    def _reg_trade(m_name: str, tr_name: str, tr_obj: Any, trade: TradeState, fill_ts: float) -> None:
        nonlocal next_hb_ts
        if trade.status != "OPEN":
            return
        owner = _resolve_owner_track(tr_obj, trade)
        ntok = normalize_instrument_token(trade.token)
        sym_u = (trade.symbol or "").upper()
        open_trades_by_token.setdefault(ntok, []).append((m_name, tr_name, owner, trade))
        if sym_u:
            open_trades_by_symbol.setdefault(sym_u, []).append((m_name, tr_name, owner, trade))
        open_tracks[(m_name, tr_name)] = tr_obj
        if next_hb_ts is None:
            next_hb_ts = fill_ts + 1.0
        if ntok not in opt_cursor:
            od = options_by_token.get(ntok)
            if od is not None and len(od["timestamps"]) > 0:
                idx = int(np.searchsorted(od["timestamps"], fill_ts, side="right"))
                if idx < len(od["timestamps"]):
                    opt_cursor[ntok] = idx
                    heapq.heappush(opt_heap, (float(od["timestamps"][idx]), ntok, idx))
        if sym_u and sym_u not in eq_cursor:
            ed = eq_by_sym.get(sym_u)
            if ed is not None and len(ed["timestamps"]) > 0:
                idx_e = int(np.searchsorted(ed["timestamps"], fill_ts, side="right"))
                if idx_e < len(ed["timestamps"]):
                    eq_cursor[sym_u] = idx_e
                    heapq.heappush(eq_heap, (float(ed["timestamps"][idx_e]), sym_u, idx_e))

    def _flush(limit_ts: float) -> None:
        nonlocal next_hb_ts
        INF = float("inf")
        while True:
            nd = delayed_heap[0][0] if delayed_heap else INF
            ne = eq_heap[0][0] if eq_heap else INF
            no = opt_heap[0][0] if opt_heap else INF
            nh = next_hb_ts if (next_hb_ts is not None and open_tracks) else INF
            ev_ts = min(nd, ne, no, nh)
            if ev_ts > limit_ts:
                break

            if nd == ev_ts:
                rel_ts, _, req = heapq.heappop(delayed_heap)
                opt_q = _get_option_quote_fast(options_by_token, req.instrument_token, rel_ts)
                meta = pending_delayed_meta.pop(req.order_id, None)
                track = meta[2] if meta else tracks_by_mode.get("paper_delayed", {}).get(req.track)
                if opt_q and opt_q["ask"] > 0:
                    fp = opt_q["ask"]
                    cd = (fp - req.price) * req.quantity
                    if track and cd > 0 and track.available_capital < cd:
                        res = OrderResult(req.order_id, "REJECTED", 0.0, 0, rel_ts)
                    else:
                        if track:
                            if cd > 0:
                                track.commit_capital(cd)
                            elif cd < 0:
                                track.release_capital(abs(cd), 0.0)
                        res = OrderResult(req.order_id, "FILLED", fp, req.quantity, rel_ts)
                else:
                    res = OrderResult(req.order_id, "REJECTED", 0.0, 0, rel_ts)
                if track:
                    track.on_order_result(res)
                    if res.status == "FILLED" and meta is not None and meta[3].status == "OPEN":
                        _reg_trade(meta[0], meta[1], track, meta[3], rel_ts)
            elif ne == ev_ts:
                e_ts, sym_u, e_idx = heapq.heappop(eq_heap)
                ed = eq_by_sym.get(sym_u)
                entries = open_trades_by_symbol.get(sym_u)
                if not entries or ed is None:
                    eq_cursor.pop(sym_u, None)
                    continue
                e_ltp = float(ed["ltps"][e_idx])
                latest_equity_ltp[sym_u] = e_ltp
                had_c = False
                if e_ltp > 0.0:
                    for _, _, owner, tr in list(entries):
                        if tr.status != "OPEN":
                            had_c = True
                            continue
                        if tr.entry_time and e_ts <= tr.entry_time:
                            continue
                        tr.metadata["underlying_ltp"] = e_ltp
                        tr.metadata["underlying_last_ts"] = e_ts
                        if hasattr(owner, "on_underlying_tick"):
                            owner.on_underlying_tick(tr, ltp=e_ltp, ts=e_ts)
                            if tr.status != "OPEN":
                                had_c = True
                if had_c:
                    _prune()
                if sym_u in open_trades_by_symbol:
                    nxt = e_idx + 1
                    if nxt < len(ed["timestamps"]):
                        eq_cursor[sym_u] = nxt
                        heapq.heappush(eq_heap, (float(ed["timestamps"][nxt]), sym_u, nxt))
                    else:
                        eq_cursor.pop(sym_u, None)
            elif no == ev_ts:
                o_ts, ntok, o_idx = heapq.heappop(opt_heap)
                entries = open_trades_by_token.get(ntok)
                if not entries:
                    opt_cursor.pop(ntok, None)
                    continue
                od = options_by_token[ntok]
                bid, ask, ltp = float(od["bids"][o_idx]), float(od["asks"][o_idx]), float(od["ltps"][o_idx])
                had_c = False
                if bid > 0.0:
                    for _, _, owner, tr in list(entries):
                        if tr.status != "OPEN":
                            had_c = True
                            continue
                        if tr.entry_time and o_ts <= tr.entry_time:
                            continue
                        if tr.symbol in latest_equity_ltp:
                            tr.metadata["underlying_ltp"] = latest_equity_ltp[tr.symbol]
                        tr.current_bid = bid
                        if bid > tr.highest_price:
                            tr.highest_price = bid
                        be = owner.breakeven_trigger_pct
                        if be and be > 0.0 and tr.entry_price > 0.0:
                            if ((tr.highest_price - tr.entry_price) / tr.entry_price) >= be - 1e-6:
                                if tr.stop_loss < tr.entry_price:
                                    tr.stop_loss = tr.entry_price
                                    tr.metadata["breakeven_triggered"] = True
                        reason = owner.evaluate_exit_rules(tr, ltp, bid, ask, o_ts)
                        if reason:
                            owner.exit_trade(tr, exit_price=bid, reason=reason, ts=o_ts)
                            had_c = True
                if had_c:
                    _prune()
                if ntok in open_trades_by_token:
                    nxt = o_idx + 1
                    if nxt < len(od["timestamps"]):
                        opt_cursor[ntok] = nxt
                        heapq.heappush(opt_heap, (float(od["timestamps"][nxt]), ntok, nxt))
                    else:
                        opt_cursor.pop(ntok, None)
            else:
                hb_ts = nh
                next_hb_ts = hb_ts + 1.0
                had_c = False
                for tobj in list(open_tracks.values()):
                    b4 = len(tobj.active_trades)
                    tobj.on_heartbeat(hb_ts)
                    if len(tobj.active_trades) < b4:
                        had_c = True
                if had_c:
                    _prune()

    for opp in accepted_opps:
        t_opp = float(opp.timestamp)
        _flush(t_opp)
        for m_name, tdict in tracks_by_mode.items():
            for tr_name, tr_obj in tdict.items():
                nt = tr_obj.on_opportunity(opp)
                if nt is None:
                    continue
                if exp.opt_scale and not _scale_trade_convexity(nt, opp, exp):
                    # Cancel & revert capital commit cleanly if scaled SL invalidated
                    nt.status = "REJECTED"
                    committed = nt.entry_ask * nt.quantity
                    tr_obj.release_capital(committed, 0.0)
                    tr_obj.active_trades.pop(nt.trade_id, None)
                    if hasattr(tr_obj, "_pending_orders"):
                        tr_obj._pending_orders.pop(f"ORD_{nt.trade_id}", None)
                    if m_name == "paper_delayed" and ctx_delayed is not None:
                        ctx_delayed.cancel_order(f"ORD_{nt.trade_id}")
                    continue
                if m_name == "paper_delayed" and ctx_delayed is not None:
                    while ctx_delayed.pending_orders:
                        rel_ts, req = ctx_delayed.pending_orders.pop(0)
                        delay_seq += 1
                        pending_delayed_meta[req.order_id] = (m_name, tr_name, tr_obj, nt)
                        heapq.heappush(delayed_heap, (rel_ts, delay_seq, req))
                else:
                    _reg_trade(m_name, tr_name, tr_obj, nt, t_opp)

    _flush(last_ts)

    # EOD Squareoff
    for m_name, tdict in tracks_by_mode.items():
        for tr_name, tr_obj in tdict.items():
            for trade in list(tr_obj.active_trades.values()):
                if trade.status == "OPEN":
                    oq = _get_option_quote_fast(options_by_token, trade.token, last_ts)
                    bid = oq["bid"] if (oq and oq["bid"] > 0) else (trade.current_bid if trade.current_bid > 0 else 0.0)
                    if hasattr(tr_obj, "exit_trade"):
                        tr_obj.exit_trade(trade, exit_price=bid, reason="EOD_SQUAREOFF", ts=last_ts)

    # Aggregate results per mode & track
    out_modes: Dict[str, Dict[str, Any]] = {}
    for m_name in modes:
        m_res: Dict[str, Any] = {}
        for tr_name, tr_obj in tracks_by_mode[m_name].items():
            ct = tr_obj.closed_trades
            wins = bes = losses = 0
            gross = brok = net = 0.0
            for t in ct:
                tb = float(t.metadata.get("brokerage", 60.0))
                gp = float(t.realized_pnl)
                npnl = gp - tb
                gross += gp
                brok += tb
                net += npnl
                pct = (t.exit_price - t.entry_price) / t.entry_price if t.entry_price > 0 else 0.0
                if pct > 0.025:
                    wins += 1
                elif pct >= -1e-6:
                    bes += 1
                else:
                    losses += 1
            m_res[tr_name] = {
                "trades": len(ct), "wins": wins, "bes": bes, "losses": losses,
                "gross_pnl": round(gross, 2), "brokerage": round(brok, 2), "net_pnl": round(net, 2),
            }
        out_modes[m_name] = m_res
    return {"exp_id": exp.exp_id, "date": bundle["date"], "opps": len(accepted_opps), "modes": out_modes}


def run_day_all_experiments(date_str: str) -> str:
    """Build bundle (if needed) and run all 45 experiments for one day, saving day summary JSON."""
    out_path = os.path.join(RESULTS_DIR, f"results_{date_str}.json")
    if os.path.exists(out_path):
        print(f"[DAY_SKIP] {date_str} already computed: {out_path}", flush=True)
        return out_path
    bundle_path = build_day_bundle(date_str)
    with open(bundle_path, "rb") as f:
        bundle = pickle.load(f)
    exps = build_all_45_experiments()
    day_results = {}
    t0 = time.time()
    for idx, exp in enumerate(exps, 1):
        day_results[exp.exp_id] = run_single_experiment_day(bundle, exp, modes=["paper", "paper_delayed"])
        if idx % 15 == 0:
            print(f"[{date_str}] {idx}/45 exps done ({time.time() - t0:.1f}s)", flush=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(day_results, f)
    print(f"[DAY_DONE] {date_str}: 45 exps finished in {time.time() - t0:.1f}s", flush=True)
    return out_path


def verify_parity_nov26() -> None:
    """Run Nov 26 Phase 1 + H0_BASELINE paper mode and verify exact parity with nov26_tick_sim_paper.json."""
    t0 = time.time()
    bpath = build_day_bundle("2026-11-26")
    with open(bpath, "rb") as f:
        bundle = pickle.load(f)
    exps = build_all_45_experiments()
    res = run_single_experiment_day(bundle, exps[0], modes=["paper"])
    tr_map = res["modes"]["paper"]
    tot_tr = sum(v["trades"] for v in tr_map.values())
    tot_gross = sum(v["gross_pnl"] for v in tr_map.values())
    tot_net = sum(v["net_pnl"] for v in tr_map.values())
    print(f"[PARITY_CHECK] 2026-11-26 H0 paper: elapsed={time.time()-t0:.2f}s | opps={res['opps']} trades={tot_tr} gross={tot_gross:.2f} net={tot_net:.2f}", flush=True)
    gt_path = os.path.join(RESULTS_DIR, "nov26_tick_sim_paper.json")
    if os.path.exists(gt_path):
        gt = json.load(open(gt_path))
        print(f"[GROUND_TRUTH] trades={gt['tot_trades']} gross={gt['tot_gross']} net={gt['tot_net']}", flush=True)
        for k in ["composite_shared_ATM", "structural_convexity_ATM", "volatility_expansion_ATM", "fast_momentum_OTM"]:
            print(f"  {k}: harness={tr_map[k]['trades']}/{tr_map[k]['gross_pnl']:.2f} vs gt={gt['tracks'][k]['trades']}/{gt['tracks'][k]['gross_pnl']:.2f}", flush=True)


def run_day_chunk(date_str: str, start_idx: int, end_idx: int) -> str:
    """Run a slice [start_idx:end_idx] of the 45 experiments on cached date bundle."""
    out_path = os.path.join(RESULTS_DIR, f"chunk_{date_str}_{start_idx}_{end_idx}.json")
    if os.path.exists(out_path):
        return out_path
    bundle_path = os.path.join(CACHE_DIR, f"bundle_{date_str}.pkl")
    with open(bundle_path, "rb") as f:
        bundle = pickle.load(f)
    exps = build_all_45_experiments()[start_idx:end_idx]
    t0 = time.time()
    chunk_res = {}
    for exp in exps:
        chunk_res[exp.exp_id] = run_single_experiment_day(bundle, exp, modes=["paper", "paper_delayed"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(chunk_res, f)
    print(f"[CHUNK_DONE] {date_str} [{start_idx}:{end_idx}] in {time.time() - t0:.1f}s", flush=True)
    return out_path


def run_all_split(p1_workers: int = 3, p2_workers: int = 5) -> None:
    """Run Phase 1 safely on p1_workers, then Phase 2 across p2_workers in 15-exp chunks."""
    t_start = time.time()
    # Order biggest/uncached cleanly so Nov 25 runs first paired with smaller days
    p1_order = list(reversed(CLEAN_DATES))
    print(f"[PHASE1_POOL] dates={len(p1_order)} workers={p1_workers}", flush=True)
    with ProcessPoolExecutor(max_workers=p1_workers) as ex:
        futs = {ex.submit(build_day_bundle, d): d for d in p1_order}
        for fut in as_completed(futs):
            d = futs[fut]
            fut.result()
    print(f"[PHASE1_ALL_DONE] elapsed={time.time() - t_start:.1f}s", flush=True)

    t_p2 = time.time()
    chunks = []
    for d in CLEAN_DATES:
        if os.path.exists(os.path.join(RESULTS_DIR, f"results_{d}.json")):
            continue
        for s, e in [(0, 15), (15, 30), (30, 45)]:
            chunks.append((d, s, e))
    print(f"[PHASE2_POOL] chunks={len(chunks)} workers={p2_workers}", flush=True)
    with ProcessPoolExecutor(max_workers=p2_workers) as ex:
        futs = {ex.submit(run_day_chunk, d, s, e): (d, s, e) for d, s, e in chunks}
        for fut in as_completed(futs):
            fut.result()

    # Merge per-day chunks into results_{date}.json
    for d in CLEAN_DATES:
        out_path = os.path.join(RESULTS_DIR, f"results_{d}.json")
        if os.path.exists(out_path):
            continue
        merged = {}
        for s, e in [(0, 15), (15, 30), (30, 45)]:
            cpath = os.path.join(RESULTS_DIR, f"chunk_{d}_{s}_{e}.json")
            with open(cpath, "r", encoding="utf-8") as cf:
                merged.update(json.load(cf))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(merged, f)
    print(f"[ALL_DONE] Phase2={time.time() - t_p2:.1f}s | Total={time.time() - t_start:.1f}s", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "parity"
    if mode == "parity":
        verify_parity_nov26()
    elif mode == "all":
        p1_w = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        p2_w = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        run_all_split(p1_workers=p1_w, p2_workers=p2_w)
