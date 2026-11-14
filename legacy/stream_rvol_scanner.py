#!/usr/bin/env python3
"""
stream_rvol_scanner.py
======================
Script 3 in the RVOL Workflow:
Subscribes to live market feeds for the indexed instruments using Upstox API v2,
computes real-time Relative Volume (RVOL) at every tick/snapshot, and filters out
any instruments whose RVOL > 2.0 (or custom threshold).

RVOL Formula:
    RVOL(t) = Today's Cumulative Volume at time (t)
              -------------------------------------------------------------
              30-Day Historical Average Cumulative Volume up to time (t)

Usage:
    # Monitor using default 2.0 threshold:
    python3 stream_rvol_scanner.py --index volume_index.json

    # Show only instruments crossing RVOL > 2.0:
    python3 stream_rvol_scanner.py --index volume_index.json --threshold 2.0 --alerts-only

    # Save triggered alerts to CSV:
    python3 stream_rvol_scanner.py --index volume_index.json --output-alerts rvol_alerts.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from upstox_common import get_access_token, http_get_json, safe_float, safe_int, resolve_file_path

try:
    from bidirectional_engine import (
        FlagPaperPortfolio,
        OptionStrikeResolver,
        calculate_theoretical_option_premium,
        extract_live_option_price,
        load_instruments,
        compute_rsi,
        compute_macd,
        compute_support_resistance_levels,
    )
    BearFlagPaperPortfolio = FlagPaperPortfolio
except ImportError:
    try:
        from bear_flag_engine import (
            BearFlagPaperPortfolio,
            OptionStrikeResolver,
            calculate_theoretical_option_premium,
            extract_live_option_price,
            load_instruments,
            compute_rsi,
            compute_macd,
            compute_support_resistance_levels,
        )
        FlagPaperPortfolio = BearFlagPaperPortfolio
    except ImportError:
        FlagPaperPortfolio = None
        BearFlagPaperPortfolio = None
        OptionStrikeResolver = None
        extract_live_option_price = None
        compute_rsi = None
        compute_macd = None
        compute_support_resistance_levels = None
        calculate_theoretical_option_premium = None
        load_instruments = None


# ---------------------------------------------------------------------------
# Terminal Colors & Formatting
# ---------------------------------------------------------------------------

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BLACK = "\033[30m"
    MAGENTA = "\033[95m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"


# ---------------------------------------------------------------------------
# Mini Block Chart & Sparkline Engine
# ---------------------------------------------------------------------------

_SPARK_BLOCKS = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def make_sparkline(values: List[float], width: int = 8) -> Tuple[str, str]:
    """
    Generate an 8-character Unicode sparkline block chart from rolling values.
    Returns (colored_sparkline_str, direction_symbol).
    """
    if not values:
        return f"{Colors.GRAY}{'─' * width}{Colors.RESET}", f"{Colors.GRAY}→{Colors.RESET}"

    vals = values[-width:]
    if len(vals) < width:
        # Prepend initial value if history is short
        vals = [vals[0]] * (width - len(vals)) + vals

    min_v = min(vals)
    max_v = max(vals)
    v_range = max_v - min_v

    is_up = vals[-1] >= vals[0]
    arrow = f"{Colors.GREEN}↗{Colors.RESET}" if vals[-1] > vals[0] else (f"{Colors.RED}↘{Colors.RESET}" if vals[-1] < vals[0] else f"{Colors.CYAN}→{Colors.RESET}")
    color = Colors.GREEN if is_up else (Colors.RED if vals[-1] < vals[0] else Colors.WHITE)

    if v_range == 0:
        bar = "▄" * width
        return f"{color}{bar}{Colors.RESET}", arrow

    chars = []
    for v in vals:
        ratio = (v - min_v) / v_range
        idx = int(ratio * 7)
        idx = max(0, min(7, idx))
        chars.append(_SPARK_BLOCKS[idx])

    return f"{color}{''.join(chars)}{Colors.RESET}", arrow


def make_rvol_meter(rvol: float, max_scale: float = 3.0, width: int = 6) -> str:
    """
    Generate a mini progress block meter for Relative Volume: [████░░]
    """
    if rvol <= 0:
        return f"{Colors.GRAY}[{'░' * width}]{Colors.RESET}"

    filled = int(min(1.0, rvol / max_scale) * width)
    unfilled = width - filled

    if rvol >= 2.0:
        color = Colors.RED
    elif rvol >= 1.5:
        color = Colors.YELLOW
    else:
        color = Colors.CYAN

    return f"{color}[{'█' * filled}{Colors.GRAY}{'░' * unfilled}{color}]{Colors.RESET}"


def make_range_bar(low: float, high: float, ltp: float, width: int = 7) -> str:
    """
    Generate an intraday position bar showing where LTP is within day's [Low ──●── High].
    Colors green if near HOD (>=85%), red if near LOD (<=15%), cyan in middle.
    """
    if high <= low:
        return f"{Colors.GRAY}[───●───]{Colors.RESET}"
    ratio = max(0.0, min(1.0, (ltp - low) / (high - low)))
    pos = int(ratio * (width - 1))
    chars = ["─"] * width
    chars[pos] = "●"
    bar_str = "".join(chars)
    if ratio >= 0.85:
        return f"{Colors.GREEN}[{bar_str}]{Colors.RESET}"
    elif ratio <= 0.15:
        return f"{Colors.RED}[{bar_str}]{Colors.RESET}"
    else:
        return f"{Colors.CYAN}[{bar_str}]{Colors.RESET}"


def clear_screen() -> None:
    """Clear terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


# ---------------------------------------------------------------------------
# RVOL Math Engine
# ---------------------------------------------------------------------------

def get_current_time_slot(interval_minutes: int = 1) -> str:
    """
    Get current IST time formatted as 'HH:MM'.
    If interval_minutes == 1, returns exact minute.
    If interval_minutes > 1, rounds down to nearest interval slot (e.g. 5-min slot '09:25').
    """
    now = datetime.now()
    if interval_minutes <= 1:
        return f"{now.hour:02d}:{now.minute:02d}"
    minute = (now.minute // interval_minutes) * interval_minutes
    return f"{now.hour:02d}:{minute:02d}"


def _format_vol_k(v: float) -> str:
    """Format volume numbers concisely: 1,450 -> 1.5k, 1,200,000 -> 1.2M"""
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    elif v >= 1_000:
        return f"{v / 1_000:.1f}k"
    else:
        return f"{int(v)}"


def calculate_cumulative_rvol(
    current_volume: float,
    profile: Dict[str, Any],
    current_time_str: Optional[str] = None,
) -> Tuple[float, float, str]:
    """
    Calculate All-Day Cumulative Relative Volume (RVOL) against 30-day historical time-of-day profile.

    Returns:
        (rvol_value, expected_volume, calculation_mode)
    """
    daily_avg_vol = safe_float(profile.get("daily_avg_volume"), 0.0)
    total_vol = safe_float(profile.get("total_volume"), 0.0)

    # Spot indices (e.g. NSE_INDEX|Nifty Fin Service) or contracts with 0 volume history
    if daily_avg_vol <= 0 or total_vol <= 0:
        return 0.0, 0.0, "no_baseline"

    if current_volume <= 0:
        return 0.0, 0.0, "zero_vol"

    time_slot_cum = profile.get("time_slot_cum_volume", {})
    interval = safe_int(profile.get("interval_minutes"), 1)

    if not current_time_str:
        current_time_str = get_current_time_slot(interval)

    expected_vol = 0.0
    mode = "time_adjusted"

    if time_slot_cum:
        if current_time_str in time_slot_cum:
            expected_vol = safe_float(time_slot_cum[current_time_str], 0.0)
        else:
            earlier_slots = [s for s in sorted(time_slot_cum.keys()) if s <= current_time_str]
            if earlier_slots:
                expected_vol = safe_float(time_slot_cum[earlier_slots[-1]], 0.0)
            else:
                first_slot = sorted(time_slot_cum.keys())[0]
                expected_vol = safe_float(time_slot_cum[first_slot], 0.0)

    if expected_vol <= 0:
        expected_vol = daily_avg_vol
        mode = "daily_avg"

    if expected_vol <= 0:
        return 0.0, 0.0, "no_baseline"

    rvol = current_volume / expected_vol
    return round(rvol, 2), round(expected_vol, 0), mode


# Backward compatibility alias
calculate_rvol = calculate_cumulative_rvol


# ---------------------------------------------------------------------------
# Rolling Multi-Minute Volume Tracker & Relative Inactivity Engine
# ---------------------------------------------------------------------------

class RollingVolumeTracker:
    """
    Tracks rolling 5-minute volume deltas and intra-candle 1-minute volume to:
    1. RVOL_5m: Rolling 5-minute Relative Volume (eliminates 1-minute flash blips).
    2. Pace Ratio: RVOL_5m / max(0.1, RVOL_cum) to measure volume pace acceleration vs decay.
    3. Inactivity Detection: Flags stocks whose volume dried up after morning surge ('💤 INACTIVE').
    4. Coiling Detection: Flags stocks with volume dry-up coiling tightly near HOD ('🌀 COILING').
    """

    def __init__(self):
        # key -> list of (epoch_sec: float, cumulative_vol: int)
        self.history: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        # key -> dict for minute candle boundaries
        self.minute_state: Dict[str, Dict[str, Any]] = {}
        # key -> list of (epoch_sec: float, rvol_5m: float) over last 60 mins for rolling variance
        self.rvol_5m_history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        # key -> list of (epoch_sec: float, ltp: float) for rolling 5m price change ΔP_5m
        self.price_history_5m: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        # key -> dict for tracking extreme high/low timestamps
        self.extreme_tracking: Dict[str, Dict[str, float]] = defaultdict(dict)
        # key -> dict for flag consolidation state and breakout detection
        self.flag_states: Dict[str, Dict[str, Any]] = defaultdict(dict)
        # key -> dict for tracking preceding impulse/pole timestamps
        self.pole_tracking: Dict[str, Dict[str, Any]] = defaultdict(dict)
        # key -> list of closed candle close prices for RSI and MACD
        self.candle_closes: Dict[str, List[float]] = defaultdict(list)

    def update(
        self,
        key: str,
        current_volume: int,
        rvol_cum: float,
        profile: Dict[str, Any],
        range_pos: float,
        ltp: float = 0.0,
        high_price: float = 0.0,
        low_price: float = 0.0,
        prev_close: float = 0.0,
        open_price: float = 0.0,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Record volume snapshot, calculate rolling 5-minute volume & RVOL,
        volume pace decay, candle high/low transitions, and classify activity state.
        """
        if now is None:
            now = datetime.now()

        now_ts = now.timestamp()
        samples = self.history[key]
        samples.append((now_ts, current_volume))

        # Keep last 10 minutes of samples (600 seconds)
        cutoff_ts = now_ts - 600.0
        while len(samples) > 2 and samples[0][0] < cutoff_ts:
            samples.pop(0)

        interval_minutes = safe_int(profile.get("interval_minutes"), 1)
        current_slot = get_current_time_slot(interval_minutes)

        time_slot_avg = profile.get("time_slot_avg_volume", {})
        avg_1m = safe_float(profile.get("avg_1m_volume"), 0.0)
        if avg_1m <= 0:
            avg_1m = safe_float(profile.get("avg_candle_volume"), 0.0)
        avg_5m = safe_float(profile.get("avg_5m_volume"), avg_1m * 5.0)

        # Expected 5-minute volume: sum of last 5 slots from historical index
        expected_5m_vol = avg_5m
        if time_slot_avg:
            all_slots = sorted(time_slot_avg.keys())
            if current_slot in all_slots:
                idx = all_slots.index(current_slot)
                window_slots = all_slots[max(0, idx - 4) : idx + 1]
                expected_5m_vol = sum(safe_float(time_slot_avg.get(s), avg_1m) for s in window_slots)

        # 1. Rolling 5-Minute Window Volume (lookback 300 seconds)
        target_5m_ts = now_ts - 300.0
        ref_sample = samples[0]
        for s in samples:
            if s[0] <= target_5m_ts:
                ref_sample = s
            else:
                break

        delta_5m = max(0, current_volume - ref_sample[1])
        elapsed_5m = max(10.0, now_ts - ref_sample[0])

        # Pro-rate if we have less than 300 seconds of history, with early window confidence damping
        if elapsed_5m < 300.0:
            raw_proj = delta_5m * (300.0 / elapsed_5m)
            if raw_proj > expected_5m_vol and elapsed_5m < 60.0:
                # Confidence damping for early window (<60s): prevents early trade bursts from exploding 30x
                conf = max(0.20, elapsed_5m / 60.0)
                proj_5m_vol = round(expected_5m_vol + (raw_proj - expected_5m_vol) * conf, 0)
            else:
                proj_5m_vol = round(raw_proj, 0)
        else:
            proj_5m_vol = float(delta_5m)

        rvol_5m = round(proj_5m_vol / expected_5m_vol, 2) if expected_5m_vol > 0 else 0.0

        # 2. 1-Minute Active Volume & Candle OHLC Boundaries (intra-candle)
        expected_1m_vol = safe_float(time_slot_avg.get(current_slot), avg_1m)
        if key not in self.minute_state:
            self.minute_state[key] = {
                "slot": current_slot,
                "start_vol": current_volume,
                "start_ts": now_ts,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "prev_high": ltp,
                "prev_low": ltp,
                "prev_vol": 0,
            }
            rvol_1m = 0.0
            proj_1m_vol = 0.0
        else:
            m_state = self.minute_state[key]
            if m_state["slot"] != current_slot:
                # Candle rollover: record previous candle boundaries
                m_state["prev_high"] = m_state.get("high", ltp)
                m_state["prev_low"] = m_state.get("low", ltp)
                closed_c = m_state.get("close", ltp)
                m_state["prev_close"] = closed_c
                if closed_c > 0:
                    self.candle_closes[key].append(closed_c)
                    if len(self.candle_closes[key]) > 60:
                        self.candle_closes[key].pop(0)
                m_state["prev_vol"] = max(0, current_volume - m_state.get("start_vol", current_volume))
                m_state["slot"] = current_slot
                m_state["start_vol"] = current_volume
                m_state["start_ts"] = now_ts
                m_state["open"] = ltp
                m_state["high"] = ltp
                m_state["low"] = ltp
                m_state["close"] = ltp
            else:
                if ltp > 0:
                    m_state["high"] = max(m_state.get("high", ltp), ltp)
                    m_state["low"] = min(m_state.get("low", ltp), ltp)
                    m_state["close"] = ltp

            delta_1m = max(0, current_volume - m_state["start_vol"])
            elapsed_1m = max(5.0, now_ts - m_state["start_ts"])
            frac_1m = min(1.0, elapsed_1m / 60.0)
            proj_1m_vol = round(delta_1m / frac_1m, 0)
            rvol_1m = round(proj_1m_vol / expected_1m_vol, 2) if expected_1m_vol > 0 else 0.0

        # 3. Rolling 5-Minute Price Impulse (ΔP_5m)
        if ltp > 0:
            p_samples = self.price_history_5m[key]
            p_samples.append((now_ts, ltp))
            cutoff_price = now_ts - 600.0
            while len(p_samples) > 2 and p_samples[0][0] < cutoff_price:
                p_samples.pop(0)

            ref_p = p_samples[0][1]
            for ts, p in p_samples:
                if ts <= target_5m_ts:
                    ref_p = p
                else:
                    break
            pct_change_5m = round(((ltp - ref_p) / ref_p) * 100, 2) if ref_p > 0 else 0.0
        else:
            pct_change_5m = 0.0

        # 4. Preceding Impulse / Pole Tracking (Flags require evidence of preceding directional impulse)
        p_track = self.pole_tracking[key]
        if range_pos >= 0.70 and (pct_change_5m >= 0.8 or (rvol_5m >= 1.4 and pct_change_5m >= 0.35)):
            p_track["bull_pole_ts"] = now_ts
            p_track["bull_pole_price"] = ltp

        if range_pos <= 0.30 and (pct_change_5m <= -0.8 or (rvol_5m >= 1.4 and pct_change_5m <= -0.35)):
            p_track["bear_pole_ts"] = now_ts
            p_track["bear_pole_price"] = ltp

        has_bull_pole = bool(now_ts - p_track.get("bull_pole_ts", 0.0) <= 1800.0)
        has_bear_pole = bool(now_ts - p_track.get("bear_pole_ts", 0.0) <= 1800.0)

        # 5. Volume Pace Ratio & Intra-5m Volume Acceleration
        effective_cum = max(0.1, rvol_cum)
        pace_ratio = round(rvol_5m / effective_cum, 2)
        vol_accel = round(rvol_1m / max(0.25, rvol_5m), 2)

        # 5b. Technical Indicators: RSI (14) & MACD (12, 26, 9)
        price_series = list(self.candle_closes[key])
        if ltp > 0:
            price_series.append(ltp)
        rsi = compute_rsi(price_series, period=14)
        macd_line, macd_signal, macd_hist = compute_macd(price_series, fast=12, slow=26, signal=9)

        # 5c. Support & Resistance Clearance & Ceiling Trap Detection
        sr_levels = compute_support_resistance_levels(prev_close, open_price, high_price, low_price, ltp)
        bull_clearance_pct = sr_levels.get("bull_clearance_pct", 1.0)
        bear_clearance_pct = sr_levels.get("bear_clearance_pct", 1.0)
        is_near_resistance = sr_levels.get("is_near_resistance", False)
        is_near_support = sr_levels.get("is_near_support", False)

        # 5d. False Breakout / Dry Extreme Detection (Sideways Market Shield)
        # Prevents micro-tick drift to new highs/lows without volume acceleration from triggering breakouts
        is_dry_high = bool(range_pos >= 0.85 and (vol_accel < 0.80 or rvol_1m < 0.85) and expected_5m_vol > 0)
        is_dry_low = bool(range_pos <= 0.15 and (vol_accel < 0.80 or rvol_1m < 0.85) and expected_5m_vol > 0)
        is_dry_extreme = is_dry_high or is_dry_low

        # 6. Inactivity & Activity State Determination
        # Bull Flag Coiling: Volume dry-up while coiling at HOD, strictly requiring a preceding Bull Pole
        is_bull_coiling = bool(rvol_5m < 0.75 and range_pos >= 0.75 and expected_5m_vol > 0 and has_bull_pole)
        # Bear Flag Coiling: Volume dry-up while pausing at LOD, strictly requiring a preceding Bear Pole
        is_bear_coiling = bool(rvol_5m < 0.75 and range_pos <= 0.25 and expected_5m_vol > 0 and has_bear_pole)
        is_coiling = is_bull_coiling or is_bear_coiling

        # Inactive: Cumulative volume was high earlier, but last 5m volume pace has collapsed
        is_inactive = bool(rvol_cum >= 1.3 and (pace_ratio <= 0.45 or rvol_5m < 0.55) and expected_5m_vol > 0)
        is_dormant = bool(rvol_5m < 0.50 and rvol_cum < 0.70 and expected_5m_vol > 0)

        if is_bull_coiling:
            activity_state = "🌀 BULL FLAG"
        elif is_bear_coiling:
            activity_state = "🚩 BEAR FLAG"
        elif is_dry_high:
            activity_state = "⚠️ DRY HIGH"
        elif is_dry_low:
            activity_state = "⚠️ DRY LOW"
        elif is_inactive:
            activity_state = "💤 INACTIVE"
        elif is_dormant:
            activity_state = "🧊 DORMANT"
        elif rvol_5m >= 2.0:
            activity_state = "🔥 SURGE 5m"
        elif rvol_5m >= 1.2:
            activity_state = "⚡ ACTIVE"
        else:
            activity_state = "NORMAL"

        # 7. Flag Formation & Breakout Detection
        # When a flag has coiled, alert the instant the current candle breaks the previous candle high/low with volume inflation
        f_state = self.flag_states[key]
        m_state = self.minute_state[key]
        prev_candle_h = m_state.get("prev_high", ltp)
        prev_candle_l = m_state.get("prev_low", ltp)
        is_flag_breakout = False
        flag_breakout_type = ""

        # Bull Flag Coiling State Machine
        if is_bull_coiling:
            if f_state.get("state") != "BULL_FLAG":
                f_state["state"] = "BULL_FLAG"
                f_state["flag_high"] = max(ltp, prev_candle_h)
                f_state["flag_low"] = min(ltp, prev_candle_l)
                f_state["coiling_cycles"] = 1
            else:
                f_state["flag_high"] = max(f_state.get("flag_high", ltp), ltp)
                f_state["flag_low"] = min(f_state.get("flag_low", ltp), ltp)
                f_state["coiling_cycles"] = f_state.get("coiling_cycles", 0) + 1

        elif f_state.get("state") == "BULL_FLAG" and f_state.get("coiling_cycles", 0) >= 1:
            flag_ceil = max(f_state.get("flag_high", ltp), prev_candle_h)
            # Volume inflation: 5m RVOL >= 0.80 or 1m RVOL >= 0.95 or Pace >= 0.80
            has_vol_inflation = (rvol_5m >= 0.80 or rvol_1m >= 0.95 or pace_ratio >= 0.80)
            if ltp > flag_ceil and has_vol_inflation and range_pos >= 0.70:
                is_flag_breakout = True
                flag_breakout_type = "BULL"
                f_state["state"] = "BREAKOUT"
                f_state["breakout_ts"] = now_ts
            elif range_pos < 0.60:
                f_state["state"] = "NONE"

        elif f_state.get("state") == "BREAKOUT":
            flag_ceil = max(f_state.get("flag_high", ltp), prev_candle_h)
            # Persist breakout state while price holds above flag boundary (up to 15m)
            if range_pos >= 0.65 and ltp >= flag_ceil * 0.995 and (now_ts - f_state.get("breakout_ts", now_ts) < 900.0):
                is_flag_breakout = True
                flag_breakout_type = "BULL"
            else:
                f_state["state"] = "NONE"

        # Bear Flag Coiling State Machine
        if is_bear_coiling:
            if f_state.get("state") != "BEAR_FLAG":
                f_state["state"] = "BEAR_FLAG"
                f_state["flag_high"] = max(ltp, prev_candle_h)
                f_state["flag_low"] = min(ltp, prev_candle_l)
                f_state["coiling_cycles"] = 1
            else:
                f_state["flag_high"] = max(f_state.get("flag_high", ltp), ltp)
                f_state["flag_low"] = min(f_state.get("flag_low", ltp), ltp)
                f_state["coiling_cycles"] = f_state.get("coiling_cycles", 0) + 1

        elif f_state.get("state") == "BEAR_FLAG" and f_state.get("coiling_cycles", 0) >= 1:
            flag_floor = min(f_state.get("flag_low", ltp), prev_candle_l)
            has_vol_inflation = (rvol_5m >= 0.80 or rvol_1m >= 0.95 or pace_ratio >= 0.80)
            if ltp < flag_floor and has_vol_inflation and range_pos <= 0.30:
                is_flag_breakout = True
                flag_breakout_type = "BEAR"
                f_state["state"] = "BREAKDOWN"
                f_state["breakout_ts"] = now_ts
            elif range_pos > 0.40:
                f_state["state"] = "NONE"

        elif f_state.get("state") == "BREAKDOWN":
            flag_floor = min(f_state.get("flag_low", ltp), prev_candle_l)
            # Persist breakdown state while price holds below flag boundary (up to 15m)
            if range_pos <= 0.35 and ltp <= flag_floor * 1.005 and (now_ts - f_state.get("breakout_ts", now_ts) < 900.0):
                is_flag_breakout = True
                flag_breakout_type = "BEAR"
            else:
                f_state["state"] = "NONE"

        # 8. Rolling 60-Minute Volume Variance & Surge Z-Score (sampled at consistent 30s cadence)
        rvol_samples = self.rvol_5m_history[key]
        if not rvol_samples or (now_ts - rvol_samples[-1][0] >= 30.0):
            rvol_samples.append((now_ts, rvol_5m))
        cutoff_60m = now_ts - 3600.0
        while len(rvol_samples) > 2 and rvol_samples[0][0] < cutoff_60m:
            rvol_samples.pop(0)

        # Require at least 8 samples (>= 4 minutes of history) for a valid baseline
        if len(rvol_samples) >= 8:
            vals = [v for ts, v in rvol_samples]
            mean_v = sum(vals) / len(vals)
            std_v = math.sqrt(sum((v - mean_v) ** 2 for v in vals) / len(vals))
            z_vol = round((rvol_5m - mean_v) / max(0.35, std_v), 2)
        else:
            z_vol = 0.0

        # 9. Extreme High/Low Freshness Tracking
        ext = self.extreme_tracking[key]
        if not ext:
            ext["hod_price"] = high_price if high_price > 0 else ltp
            ext["hod_ts"] = now_ts
            ext["lod_price"] = low_price if low_price > 0 else ltp
            ext["lod_ts"] = now_ts
        else:
            if high_price > ext.get("hod_price", 0.0) or (high_price > 0 and ltp >= high_price * 0.999):
                ext["hod_price"] = max(high_price, ltp)
                ext["hod_ts"] = now_ts
            if (low_price > 0 and low_price < ext.get("lod_price", float("inf"))) or (low_price > 0 and ltp <= low_price * 1.001):
                ext["lod_price"] = min(low_price, ltp)
                ext["lod_ts"] = now_ts

        seconds_since_hod = max(0.0, now_ts - ext.get("hod_ts", now_ts))
        seconds_since_lod = max(0.0, now_ts - ext.get("lod_ts", now_ts))

        return {
            "rvol_5m": rvol_5m,
            "rvol_1m": rvol_1m,
            "proj_5m_vol": proj_5m_vol,
            "expected_5m_vol": round(expected_5m_vol, 0),
            "proj_1m_vol": proj_1m_vol,
            "expected_1m_vol": round(expected_1m_vol, 0),
            "pace_ratio": pace_ratio,
            "z_vol": z_vol,
            "pct_change_5m": pct_change_5m,
            "seconds_since_hod": seconds_since_hod,
            "seconds_since_lod": seconds_since_lod,
            "activity_state": activity_state,
            "is_inactive": is_inactive,
            "is_bull_coiling": is_bull_coiling,
            "is_bear_coiling": is_bear_coiling,
            "is_coiling": is_coiling,
            "is_dormant": is_dormant,
            "has_bull_pole": has_bull_pole,
            "has_bear_pole": has_bear_pole,
            "elapsed_5m": round(elapsed_5m, 1),
            "is_flag_breakout": is_flag_breakout,
            "flag_breakout_type": flag_breakout_type,
            "flag_high": f_state.get("flag_high", ltp),
            "flag_low": f_state.get("flag_low", ltp),
            "prev_candle_high": prev_candle_h,
            "prev_candle_low": prev_candle_l,
            "vol_accel": vol_accel,
            "is_dry_extreme": is_dry_extreme,
            "is_dry_high": is_dry_high,
            "is_dry_low": is_dry_low,
            "rsi": rsi,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "bull_clearance_pct": bull_clearance_pct,
            "bear_clearance_pct": bear_clearance_pct,
            "is_near_resistance": is_near_resistance,
            "is_near_support": is_near_support,
            "resistance_level": sr_levels.get("resistance_level", 0.0),
            "support_level": sr_levels.get("support_level", 0.0),
        }


# Backward compatibility alias
MinuteVolumeTracker = RollingVolumeTracker


# ---------------------------------------------------------------------------
# Continuous Session Data Recorder (for 100% Offline Backtest / Replay)
# ---------------------------------------------------------------------------

class SessionDataRecorder:
    """
    Continuous session data recorder for 100% offline backtesting and replay.
    Records periodic snapshots of universe quotes and immediate setup/trade alerts
    to a JSON Lines file in recordings/session_YYYYMMDD_market.jsonl.
    """

    def __init__(self, record_dir: str = "recordings", session_date: Optional[str] = None):
        self.record_dir = record_dir
        os.makedirs(self.record_dir, exist_ok=True)
        if not session_date:
            session_date = datetime.now().strftime("%Y%m%d")
        self.filepath = os.path.join(self.record_dir, f"session_{session_date}_market.jsonl")
        self.file = open(self.filepath, "a", encoding="utf-8")
        self.last_snapshot_ts = 0.0
        self.snapshot_interval = 5.0  # record universe every 5 seconds

        # Write session start header if new file
        if self.file.tell() == 0:
            header = {
                "type": "SESSION_START",
                "ts": datetime.now().isoformat(),
                "session_date": session_date,
                "version": "2.0",
            }
            self.file.write(json.dumps(header) + "\n")
            self.file.flush()

    def record_snapshot(self, rows: List[Dict[str, Any]], now: Optional[datetime] = None) -> None:
        """Record universe snapshot throttled to snapshot_interval."""
        if now is None:
            now = datetime.now()
        now_ts = now.timestamp()
        if now_ts - self.last_snapshot_ts < self.snapshot_interval:
            return
        self.last_snapshot_ts = now_ts

        # Compact snapshot format: [symbol, ltp, volume, high, low, rvol_5m, rvol_cum, z_vol, score, stage]
        compact_ticks = []
        for r in rows:
            compact_ticks.append([
                r.get("symbol", ""),
                r.get("last_price", 0.0),
                r.get("volume", 0),
                r.get("high", 0.0),
                r.get("low", 0.0),
                r.get("rvol_5m", 0.0),
                r.get("rvol_cum", 0.0),
                r.get("breakout_details", {}).get("z_vol", 0.0),
                r.get("breakout_score", 0),
                r.get("breakout_stage", "NORMAL"),
            ])

        entry = {
            "type": "TICK_SNAPSHOT",
            "ts": now.isoformat(),
            "ticks": compact_ticks,
        }
        self.file.write(json.dumps(entry) + "\n")
        self.file.flush()

    def record_alert(self, alert_data: Dict[str, Any], now: Optional[datetime] = None) -> None:
        """Immediately record breakout/breakdown/flag alert."""
        if now is None:
            now = datetime.now()
        entry = {
            "type": "SETUP_ALERT",
            "ts": now.isoformat(),
            "data": alert_data,
        }
        self.file.write(json.dumps(entry) + "\n")
        self.file.flush()

    def record_trade(self, trade_data: Dict[str, Any], now: Optional[datetime] = None) -> None:
        """Record paper trade entry/exit/rejection."""
        if now is None:
            now = datetime.now()
        entry = {
            "type": "TRADE_EVENT",
            "ts": now.isoformat(),
            "data": trade_data,
        }
        self.file.write(json.dumps(entry) + "\n")
        self.file.flush()

    def close(self) -> None:
        if self.file and not self.file.closed:
            self.file.flush()
            self.file.close()


# ---------------------------------------------------------------------------
# Bidirectional Setup Detection & Scoring Engine (Breakouts, Breakdowns & Flags)
# ---------------------------------------------------------------------------

def calculate_setup_score(
    rvol_5m: float,
    rvol_1m: float,
    rvol_cum: float,
    ltp: float,
    open_price: float,
    high_price: float,
    low_price: float,
    pct_change: float,
    price_history: List[float],
    is_inactive: bool = False,
    mode: str = "",
    z_vol: float = 0.0,
    pct_change_5m: float = 0.0,
    seconds_since_hod: float = 0.0,
    seconds_since_lod: float = 0.0,
    option_spread_pct: float = 0.0,
    is_flag_breakout: bool = False,
    flag_breakout_type: str = "",
    has_bull_pole: bool = True,
    has_bear_pole: bool = True,
    vol_accel: float = 1.0,
    is_dry_extreme: bool = False,
    rsi: float = 50.0,
    macd_hist: float = 0.0,
    bull_clearance_pct: float = 1.0,
    bear_clearance_pct: float = 1.0,
    is_near_resistance: bool = False,
    is_near_support: bool = False,
) -> Tuple[int, str, str, Dict[str, Any]]:
    """
    Detect and score intraday directional setups:
    - Bullish: Breakouts (HOD) & Bull Flags (tight coiling at HOD)
    - Bearish: Breakdowns (LOD) & Bear Flags (tight coiling at LOD)

    100-Point Rebalanced Matrix:
    1. Volume & Variance (max 25 pts): RVOL vs profile (15 pts) + Variance surge Z_vol (10 pts) + Institutional bonus (within 25 cap)
    2. Position & Extreme Freshness (max 20 pts): Base HOD/LOD proximity (10 pts) + Fresh extreme print (10 pts, stagnant > 15m gets 0)
    3. Directional Momentum (max 30 pts): Macro Day % change (10 pts) + Rolling 5m Price Impulse Delta_P_5m (20 pts), with directional alignment check
    4. Micro-Tick Velocity (max 25 pts): Candle color (5 pts) + 5-tick trend thrust (10 pts) + 3-tick acceleration relative to day's range (10 pts, flat ticks get 0)
    Technical Confluence:
    - Intra-5m Volume Acceleration (+5 / -8)
    - S/R Clearance (+5) & Resistance Ceiling Trap Penalty (-12)
    - RSI Momentum & Overbought/Oversold Exhaustion (+3 / -5 / -6)
    - MACD Histogram Expansion & Contraction (+2 / -4)
    Option spread penalty: Docks 15 pts if spread > 3.0%
    Sideways False Breakout Trap: Clamps score < 60 and tags ⚠️ DRY HIGH / ⚠️ DRY LOW if volume acceleration fails.

    Returns:
        (setup_score: int 0-100, stage: str, direction: str, details: dict)
        direction: 'BULL', 'BEAR', or 'NEUTRAL'
        stage: '💥 BULL BREAKOUT', '💥 BEAR BREAKDOWN', '🚀 BREAKOUT', '⚡ FORMING', '🌀 BULL FLAG', '🩸 BREAKDOWN', '⚡ CRACKING', '🚩 BEAR FLAG', '👀 WATCH', '⚠️ DRY HIGH', '⚠️ DRY LOW', 'NORMAL'
    """
    if mode in ("no_baseline", "zero_vol") or ltp <= 0 or high_price <= 0:
        return 0, "NORMAL", "NEUTRAL", {
            "vol_score": 0, "z_vol": 0.0, "pos_score": 0, "mom_score": 0, "tick_score": 0,
            "pct_change_5m": 0.0, "seconds_since_hod": 0.0, "seconds_since_lod": 0.0,
            "range_pos": 50.0, "hod_pct": 0.0, "lod_pct": 0.0, "day_range_pct": 0.0,
            "option_spread_pct": 0.0,
            "is_flag_breakout": False,
            "flag_breakout_type": "",
            "has_bull_pole": has_bull_pole,
            "has_bear_pole": has_bear_pole,
            "vol_accel": 1.0,
            "is_dry_extreme": False,
            "rsi": 50.0,
            "macd_hist": 0.0,
            "clearance_pct": 1.0,
            "is_near_resistance": False,
            "is_near_support": False,
        }

    # -----------------------------------------------------------------------
    # 1. Volume & Variance Surge (max 25 pts)
    # -----------------------------------------------------------------------
    # 1a. Base RVOL vs Historical Baseline (continuous 0.0 to 15.0 pts)
    if rvol_5m < 0.6:
        base_vol_pts = 0.0
    elif rvol_5m < 1.0:
        base_vol_pts = 1.0 + ((rvol_5m - 0.6) / 0.4) * 3.0
    elif rvol_5m < 1.8:
        base_vol_pts = 4.0 + ((rvol_5m - 1.0) / 0.8) * 6.0
    elif rvol_5m <= 3.0:
        base_vol_pts = 10.0 + ((rvol_5m - 1.8) / 1.2) * 5.0
    else:
        base_vol_pts = 15.0

    # 1b. Variance-Based Surge Z_vol (continuous 0.0 to 10.0 pts)
    # Measures standard deviations above rolling 60-min volume mean (immune to 9:16 AM outlier skew)
    if z_vol >= 2.0:
        z_vol_pts = 10.0
    elif z_vol >= 0.2:
        z_vol_pts = ((z_vol - 0.2) / 1.8) * 10.0
    elif z_vol == 0.0 and rvol_5m >= 1.2:
        z_vol_pts = min(8.0, max(2.0, (rvol_5m - 1.0) * 4.0))  # Smooth early session fallback
    else:
        z_vol_pts = 0.0

    vol_score = base_vol_pts + z_vol_pts

    # Institutional session confirmation (+3 pts, capped at 25 pillar ceiling)
    if rvol_cum >= 1.5 and vol_score > 0 and not is_inactive:
        vol_score = min(25.0, vol_score + 3.0)

    # Inactivity Penalty: front-loaded volume with collapsing pace
    if is_inactive:
        vol_score = max(0.0, vol_score - 15.0)

    vol_score = max(0.0, min(25.0, vol_score))

    # -----------------------------------------------------------------------
    # 2. Day Range Position & Extreme Freshness (max 20 pts: 10 base + 10 fresh)
    # -----------------------------------------------------------------------
    day_span = high_price - low_price
    range_pos = (ltp - low_price) / day_span if day_span > 0 else 0.5
    range_pos = max(0.0, min(1.0, range_pos))

    hod_ratio = ltp / high_price if high_price > 0 else 0.0
    lod_ratio = low_price / ltp if ltp > 0 else 0.0

    # Bullish Base Position (continuous 0.0 to 10.0 pts from 50% to 98% of range)
    bull_base_pos = min(10.0, max(0.0, ((range_pos - 0.50) / 0.48) * 10.0))

    # Bullish Extreme Freshness (10 pts): Full 10 pts within 2m, decaying over 15m (900s)
    if seconds_since_hod <= 120:
        bull_fresh_pts = 10.0
    else:
        bull_fresh_pts = min(10.0, max(0.0, 10.0 * (1.0 - (seconds_since_hod - 120.0) / 780.0)))

    bull_pos_score = max(0.0, min(20.0, bull_base_pos + bull_fresh_pts))

    # Bearish Base Position (continuous 0.0 to 10.0 pts from 50% down to 2% of range)
    bear_base_pos = min(10.0, max(0.0, ((0.50 - range_pos) / 0.48) * 10.0))

    # Bearish Extreme Freshness (10 pts): Full 10 pts within 2m, decaying over 15m (900s)
    if seconds_since_lod <= 120:
        bear_fresh_pts = 10.0
    else:
        bear_fresh_pts = min(10.0, max(0.0, 10.0 * (1.0 - (seconds_since_lod - 120.0) / 780.0)))

    bear_pos_score = max(0.0, min(20.0, bear_base_pos + bear_fresh_pts))

    # -----------------------------------------------------------------------
    # 3. Two-Tier Directional Momentum (max 30 pts: 10 macro + 20 rolling 5m impulse)
    # -----------------------------------------------------------------------
    # Auto-infer pct_change_5m from price_history if not explicitly passed
    if pct_change_5m == 0.0 and len(price_history) >= 2:
        pct_change_5m = ((price_history[-1] - price_history[0]) / max(0.01, price_history[0])) * 100.0

    # Tier 1: Macro Day % Change (continuous 0.0 to 10.0 pts, reaching 10 at +/- 3.0%)
    bull_macro_mom = min(10.0, max(0.0, (pct_change / 3.0) * 10.0)) if pct_change > 0 else 0.0
    bear_macro_mom = min(10.0, max(0.0, (abs(pct_change) / 3.0) * 10.0)) if pct_change < 0 else 0.0

    # Tier 2: Rolling 5-Minute Price Impulse Delta P_5m (continuous 0.0 to 20.0 pts, reaching 20 at +/- 0.50%)
    bull_impulse_mom = min(20.0, max(0.0, (pct_change_5m / 0.50) * 20.0)) if pct_change_5m > 0 else 0.0
    bear_impulse_mom = min(20.0, max(0.0, (abs(pct_change_5m) / 0.50) * 20.0)) if pct_change_5m < 0 else 0.0

    bull_mom_score = bull_macro_mom + bull_impulse_mom
    bear_mom_score = bear_macro_mom + bear_impulse_mom

    # Directional Alignment & Counter-Trend Penalties:
    # A setup cannot claim momentum points if macro direction contradicts it
    if pct_change < 0.0:
        bull_mom_score = 0.0
    if pct_change > 0.0:
        bear_mom_score = 0.0

    # If rolling 5m impulse fights the macro trend, apply counter-trend pullback/bounce penalty
    if pct_change_5m <= -0.6:
        bull_mom_score = max(0.0, bull_mom_score - 10.0)
    elif pct_change_5m <= -0.3:
        bull_mom_score = max(0.0, bull_mom_score - 5.0)

    if pct_change_5m >= 0.6:
        bear_mom_score = max(0.0, bear_mom_score - 10.0)
    elif pct_change_5m >= 0.3:
        bear_mom_score = max(0.0, bear_mom_score - 5.0)

    bull_mom_score = max(0.0, min(30.0, bull_mom_score))
    bear_mom_score = max(0.0, min(30.0, bear_mom_score))

    # -----------------------------------------------------------------------
    # 4. Micro-Tick Velocity & Order Flow (max 25 pts)
    # -----------------------------------------------------------------------
    # Meaningful tick movement normalized to day's activity
    effective_range = max(day_span, ltp * 0.005)
    is_green_candle = (open_price > 0 and ltp >= open_price)
    is_red_candle = (open_price > 0 and ltp <= open_price)

    bull_tick_score = 5.0 if is_green_candle else 0.0
    bear_tick_score = 5.0 if is_red_candle else 0.0

    if len(price_history) >= 2:
        recent = price_history[-5:]

        # 4a. 5-Tick Trend Thrust relative to day's activity (max 10 pts, continuous)
        delta_p_trend = recent[-1] - recent[0]
        trend_thrust_ratio = abs(delta_p_trend) / effective_range

        if delta_p_trend > 0:
            thrust_pts = min(10.0, max(1.0, 1.0 + (trend_thrust_ratio / 0.03) * 9.0))
            bull_tick_score += thrust_pts
        elif delta_p_trend < 0:
            thrust_pts = min(10.0, max(1.0, 1.0 + (trend_thrust_ratio / 0.03) * 9.0))
            bear_tick_score += thrust_pts

        # 4c. 3-Tick Acceleration & Order Book Sweep relative to day's activity (max 10 pts, continuous)
        # Strict progression required: at least one strictly greater/less step. Flat ticks get ZERO!
        if len(recent) >= 3:
            t1, t2, t3 = recent[-3], recent[-2], recent[-1]
            delta_p_3 = t3 - t1
            accel_ratio = abs(delta_p_3) / effective_range

            if (t3 > t2 and t2 >= t1) or (t3 >= t2 and t2 > t1):
                accel_pts = min(10.0, max(2.0, 2.0 + (accel_ratio / 0.02) * 8.0))
                bull_tick_score += accel_pts
            elif (t3 < t2 and t2 <= t1) or (t3 <= t2 and t2 < t1):
                accel_pts = min(10.0, max(2.0, 2.0 + (accel_ratio / 0.02) * 8.0))
                bear_tick_score += accel_pts
            # Note: Flat ticks (t1 == t2 == t3) receive 0 acceleration points!

    bull_tick_score = max(0.0, min(25.0, bull_tick_score))
    bear_tick_score = max(0.0, min(25.0, bear_tick_score))

    # -----------------------------------------------------------------------
    # 5. Technical Confluence: S/R Levels, Intra-5m Acceleration, RSI & MACD
    # -----------------------------------------------------------------------
    # 5a. Intra-5m Volume Acceleration (+5 / -8)
    if vol_accel >= 1.0:
        bull_accel_pts = min(5.0, ((vol_accel - 1.0) / 0.3) * 5.0)
    else:
        bull_accel_pts = max(-8.0, ((vol_accel - 1.0) / 0.25) * 8.0)
    bear_accel_pts = bull_accel_pts

    # 5b. Support & Resistance Clearance & Resistance Ceiling Trap (High Weight)
    # Clearance: continuous up to 5 pts; Trap: -12 pts if buying right into resistance wall!
    bull_sr_pts = min(5.0, bull_clearance_pct * 4.0) + (-12.0 if is_near_resistance else 0.0)
    bear_sr_pts = min(5.0, bear_clearance_pct * 4.0) + (-12.0 if is_near_support else 0.0)

    # 5c. RSI Momentum & Overbought/Oversold Exhaustion (Lower Weight)
    if 55.0 <= rsi <= 72.0:
        bull_rsi_pts = 3.0
    elif rsi > 78.0:
        bull_rsi_pts = -5.0
    elif rsi < 45.0:
        bull_rsi_pts = -6.0
    else:
        bull_rsi_pts = 0.0

    if 28.0 <= rsi <= 45.0:
        bear_rsi_pts = 3.0
    elif rsi < 22.0:
        bear_rsi_pts = -5.0
    elif rsi > 55.0:
        bear_rsi_pts = -6.0
    else:
        bear_rsi_pts = 0.0

    # 5d. MACD Histogram Directional Expansion (Lower Weight)
    bull_macd_pts = 2.0 if macd_hist > 0 else (-4.0 if macd_hist < 0 else 0.0)
    bear_macd_pts = 2.0 if macd_hist < 0 else (-4.0 if macd_hist > 0 else 0.0)

    bull_tech_score = bull_accel_pts + bull_sr_pts + bull_rsi_pts + bull_macd_pts
    bear_tech_score = bear_accel_pts + bear_sr_pts + bear_rsi_pts + bear_macd_pts

    # Option Spread Liquidity Penalty
    bull_penalty = 15.0 if (option_spread_pct > 3.0) else 0.0
    bear_penalty = 15.0 if (option_spread_pct > 3.0) else 0.0

    total_bull_score = max(0.0, min(100.0, vol_score + bull_pos_score + bull_mom_score + bull_tick_score + bull_tech_score - bull_penalty))
    total_bear_score = max(0.0, min(100.0, vol_score + bear_pos_score + bear_mom_score + bear_tick_score + bear_tech_score - bear_penalty))

    # Sideways False Breakout Trap Shield:
    # If price drifted to extreme without volume acceleration, clamp score strictly below trigger threshold (<60)
    if is_dry_extreme:
        if range_pos >= 0.50:
            total_bull_score = min(total_bull_score, 58.0)
        else:
            total_bear_score = min(total_bear_score, 58.0)

    day_range_pct = (day_span / low_price * 100) if low_price > 0 else 0.0

    # Bull Flags: Volume dry-up while coiling at HOD, strictly requiring preceding Bull Pole
    is_bull_flag = bool(rvol_5m < 0.70 and range_pos >= 0.85 and not is_inactive and has_bull_pole)
    # Bear Flags: Volume dry-up while pausing at LOD, strictly requiring preceding Bear Pole
    is_bear_flag = bool(rvol_5m < 0.70 and range_pos <= 0.15 and not is_inactive and has_bear_pole)

    # 4. Low-Volume Inactivity / Drift Damping:
    # If both 5-min RVOL and Cumulative Day RVOL are below active thresholds (< 0.85x),
    # the stock lacks institutional participation. Unless it is an active flag pattern,
    # dock 15 points and clamp total score to at most 60 so it stays below the default viewing threshold (67).
    is_active_flag_pattern = is_flag_breakout or is_bull_flag or is_bear_flag
    low_vol_drift = bool(rvol_5m < 0.85 and rvol_cum < 0.85 and not is_active_flag_pattern)
    if low_vol_drift:
        total_bull_score = min(60.0, max(0.0, total_bull_score - 15.0))
        total_bear_score = min(60.0, max(0.0, total_bear_score - 15.0))

    # 0. Dry Extreme Shield: overrides breakout/breakdown if volume acceleration failed
    if is_dry_extreme:
        if range_pos >= 0.50:
            direction = "BULL"
            stage = "⚠️ DRY HIGH"
            total_score = total_bull_score
        else:
            direction = "BEAR"
            stage = "⚠️ DRY LOW"
            total_score = total_bear_score
    # 1. Early Flag Breakouts/Breakdowns (Triggers before macro HOD/LOD breakout when candle pierces flag with volume)
    elif is_flag_breakout and flag_breakout_type == "BULL":
        direction = "BULL"
        stage = "💥 BULL BREAKOUT"
        total_score = min(100.0, max(70.0, total_bull_score + 15.0))
    elif is_flag_breakout and flag_breakout_type == "BEAR":
        direction = "BEAR"
        stage = "💥 BEAR BREAKDOWN"
        total_score = min(100.0, max(70.0, total_bear_score + 15.0))
    # 2. Flag Coiling Stages (Pre-breakout / Pre-breakdown tight consolidation)
    elif is_bull_flag:
        direction = "BULL"
        stage = "🌀 BULL FLAG"
        total_score = min(100.0, max(50.0, total_bull_score + 5.0))
    elif is_bear_flag:
        direction = "BEAR"
        stage = "🚩 BEAR FLAG"
        total_score = min(100.0, max(50.0, total_bear_score + 5.0))
    # 3. Macro Directional Setups
    elif total_bull_score >= total_bear_score and range_pos >= 0.50:
        direction = "BULL"
        total_score = total_bull_score
        # Confirmed breakout requires fresh extreme within last 10 minutes (<= 600s)
        if total_score >= 75 and rvol_5m >= 1.5 and range_pos >= 0.90 and seconds_since_hod <= 600 and not is_inactive:
            stage = "🚀 BREAKOUT"
        elif total_score >= 55 and rvol_5m >= 1.2 and range_pos >= 0.75 and not is_inactive:
            stage = "⚡ FORMING"
        elif total_score >= 40 and not is_inactive:
            stage = "👀 WATCH"
        else:
            stage = "NORMAL"
    elif total_bear_score > total_bull_score and range_pos <= 0.50:
        direction = "BEAR"
        total_score = total_bear_score
        # Confirmed breakdown requires fresh extreme within last 10 minutes (<= 600s)
        if total_score >= 75 and rvol_5m >= 1.5 and range_pos <= 0.10 and seconds_since_lod <= 600 and not is_inactive:
            stage = "🩸 BREAKDOWN"
        elif total_score >= 55 and rvol_5m >= 1.2 and range_pos <= 0.25 and not is_inactive:
            stage = "⚡ CRACKING"
        elif total_score >= 40 and not is_inactive:
            stage = "👀 WATCH"
        else:
            stage = "NORMAL"
    else:
        direction = "NEUTRAL"
        total_score = max(total_bull_score, total_bear_score)
        stage = "NORMAL"

    details = {
        "vol_score": int(round(vol_score)),
        "z_vol": z_vol,
        "pos_score": int(round(bull_pos_score if direction == "BULL" else bear_pos_score)),
        "mom_score": int(round(bull_mom_score if direction == "BULL" else bear_mom_score)),
        "bull_mom_score": int(round(bull_mom_score)),
        "bear_mom_score": int(round(bear_mom_score)),
        "tick_score": int(round(bull_tick_score if direction == "BULL" else bear_tick_score)),
        "pct_change_5m": pct_change_5m,
        "seconds_since_hod": round(seconds_since_hod, 1),
        "seconds_since_lod": round(seconds_since_lod, 1),
        "range_pos": round(range_pos * 100, 1),
        "hod_pct": round(hod_ratio * 100, 2),
        "lod_pct": round(lod_ratio * 100, 2),
        "day_range_pct": round(day_range_pct, 2),
        "option_spread_pct": round(option_spread_pct, 2),
        "is_flag_breakout": is_flag_breakout,
        "flag_breakout_type": flag_breakout_type,
        "has_bull_pole": has_bull_pole,
        "has_bear_pole": has_bear_pole,
        "vol_accel": round(vol_accel, 2),
        "is_dry_extreme": is_dry_extreme,
        "rsi": round(rsi, 1),
        "macd_hist": round(macd_hist, 2),
        "clearance_pct": round(bull_clearance_pct if direction == "BULL" else bear_clearance_pct, 2),
        "is_near_resistance": is_near_resistance,
        "is_near_support": is_near_support,
        "low_vol_drift": low_vol_drift,
    }

    return int(round(total_score)), stage, direction, details


# Backward compatibility alias
calculate_breakout_score = calculate_setup_score


def make_score_badge(score: int) -> str:
    """Format setup score (0-100) as a prominent, easily visible badge."""
    if score >= 75:
        # High conviction breakout/breakdown: Bold bright Green
        return f"{Colors.BOLD}{Colors.GREEN}  {score:>3}  {Colors.RESET}"
    elif score >= 55:
        # Forming / Squeeze: Bold Yellow
        return f"{Colors.BOLD}{Colors.YELLOW}  {score:>3}  {Colors.RESET}"
    elif score >= 40:
        # Watchlist: Cyan
        return f"{Colors.CYAN}  {score:>3}  {Colors.RESET}"
    elif score > 0:
        # Normal / Low momentum: White
        return f"{Colors.WHITE}  {score:>3}  {Colors.RESET}"
    else:
        return f"{Colors.GRAY}    0  {Colors.RESET}"


def make_setup_badge(stage: str, direction: str = "BULL", score: Optional[int] = None) -> str:
    """Format setup stage (Breakouts, Breakdowns, Flags) with colors for the live table."""
    # Backward compatibility if called as make_setup_badge(score, stage, direction)
    if isinstance(stage, (int, float)) and isinstance(direction, str):
        real_stage = direction
        real_dir = score if isinstance(score, str) else "BULL"
        stage = real_stage
        direction = real_dir

    if stage in ("🚀 BREAKOUT", "🚀 BULL BREAKOUT"):
        return f"{Colors.BOLD}{Colors.GREEN} 🚀 BREAKOUT  {Colors.RESET}"
    elif stage in ("🩸 BREAKDOWN", "🩸 BEAR BREAKDOWN"):
        return f"{Colors.BOLD}{Colors.RED} 🩸 BREAKDOWN {Colors.RESET}"
    elif stage == "💥 BULL BREAKOUT":
        return f"{Colors.BOLD}{Colors.GREEN} 💥 BULL BREAK{Colors.RESET}"
    elif stage == "💥 BEAR BREAKDOWN":
        return f"{Colors.BOLD}{Colors.RED} 💥 BEAR BREAK{Colors.RESET}"
    elif stage == "⚡ FORMING":
        return f"{Colors.BOLD}{Colors.YELLOW} ⚡ FORMING   {Colors.RESET}"
    elif stage == "⚡ CRACKING":
        return f"{Colors.BOLD}{Colors.MAGENTA} ⚡ CRACKING  {Colors.RESET}"
    elif stage == "🌀 BULL FLAG":
        return f"{Colors.BOLD}{Colors.CYAN} 🌀 BULL FLAG {Colors.RESET}"
    elif stage == "🚩 BEAR FLAG":
        return f"{Colors.BOLD}{Colors.RED} 🚩 BEAR FLAG {Colors.RESET}"
    elif stage in ("⚠️ DRY HIGH", "⚠️ DRY LOW"):
        return f"{Colors.YELLOW} ⚠️ DRY EXTREM{Colors.RESET}"
    elif stage == "👀 WATCH":
        if direction == "BEAR":
            return f"{Colors.MAGENTA} 👀 WATCH (S) {Colors.RESET}"
        return f"{Colors.CYAN} 👀 WATCH (L) {Colors.RESET}"
    else:
        return f"{Colors.GRAY}  ── NORMAL   {Colors.RESET}"


# Backward compatibility alias
make_breakout_badge = make_setup_badge


def make_activity_badge(state: str) -> str:
    """Format intraday activity/inactivity state with colors for live tables and cards."""
    if state == "🔥 SURGE 5m":
        return f"{Colors.BOLD}{Colors.RED}🔥 SURGE 5m   {Colors.RESET}"
    elif state == "⚡ ACTIVE":
        return f"{Colors.BOLD}{Colors.YELLOW}⚡ ACTIVE     {Colors.RESET}"
    elif state in ("🌀 BULL FLAG", "🌀 COILING"):
        return f"{Colors.BOLD}{Colors.CYAN}🌀 BULL FLAG  {Colors.RESET}"
    elif state == "🚩 BEAR FLAG":
        return f"{Colors.BOLD}{Colors.RED}🚩 BEAR FLAG  {Colors.RESET}"
    elif state == "⚠️ DRY HIGH":
        return f"{Colors.YELLOW}⚠️ DRY HIGH   {Colors.RESET}"
    elif state == "⚠️ DRY LOW":
        return f"{Colors.YELLOW}⚠️ DRY LOW    {Colors.RESET}"
    elif state == "💤 INACTIVE":
        return f"{Colors.GRAY}💤 INACTIVE   {Colors.RESET}"
    elif state == "🧊 DORMANT":
        return f"{Colors.GRAY}🧊 DORMANT    {Colors.RESET}"
    elif state == "INDEX":
        return f"{Colors.GRAY}── INDEX      {Colors.RESET}"
    else:
        return f"{Colors.WHITE}── NORMAL     {Colors.RESET}"


def setup_rank_key(row: Dict[str, Any]) -> Tuple[int, int, float, float, float]:
    """
    Sort key to rank instruments in a combined bidirectional feed:
    1. Confirmed Breakouts (🚀) & Breakdowns (🩸) (weight 4)
    2. Forming Breakouts (⚡ FORMING) & Cracking Breakdowns (⚡ CRACKING) (weight 3)
    3. Flags (🌀 BULL FLAG & 🚩 BEAR FLAG) (weight 2)
    4. Active Surges & Watch list (weight 1)
    5. Normal (weight 0)
    6. Inactive / Dormant (weight -1) - deprioritized to bottom!
    Sub-sorted by:
    - Setup Score (0 - 100)
    - Rolling 5m RVOL
    - Cumulative RVOL
    - Absolute Percentage Change abs(pct_change)
    """
    stage = row.get("breakout_stage", "NORMAL")
    act_state = row.get("activity_state", "NORMAL")
    is_inactive = bool(row.get("is_inactive", False))

    if is_inactive or stage in ("⚠️ DRY HIGH", "⚠️ DRY LOW"):
        weight = -1
    elif stage in ("💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN"):
        weight = 5
    elif stage in ("🚀 BREAKOUT", "🩸 BREAKDOWN"):
        weight = 4
    elif stage in ("⚡ FORMING", "⚡ CRACKING"):
        weight = 3
    elif stage in ("🌀 BULL FLAG", "🚩 BEAR FLAG") or act_state in ("🌀 BULL FLAG", "🚩 BEAR FLAG", "🌀 COILING"):
        weight = 2
    elif "WATCH" in stage or act_state == "🔥 SURGE 5m":
        weight = 1
    else:
        weight = 0

    score = safe_int(row.get("breakout_score"), 0)
    rvol_5m = safe_float(row.get("rvol_5m"), 0.0)
    rvol_cum = safe_float(row.get("rvol_cum"), 0.0)
    abs_pct = abs(safe_float(row.get("pct_change"), 0.0))
    return (weight, score, rvol_5m, rvol_cum, abs_pct)


# Backward compatibility alias
breakout_rank_key = setup_rank_key


# ---------------------------------------------------------------------------
# Live Quote Fetcher (Batch REST API)
# ---------------------------------------------------------------------------

def fetch_live_quotes_batch(
    instrument_keys: List[str],
    access_token: str,
    batch_size: int = 500,
    debug_file: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Fetch live market quotes in batches of up to 500 instruments via:
    GET https://api.upstox.com/v2/market-quote/quotes?instrument_key=KEY1,KEY2...
    """
    results: Dict[str, Dict[str, Any]] = {}

    for i in range(0, len(instrument_keys), batch_size):
        chunk = instrument_keys[i : i + batch_size]
        # Preserve comma delimiter in query param without %2C encoding
        encoded_keys = [urllib.parse.quote(k, safe="") for k in chunk]
        keys_param = ",".join(encoded_keys)
        url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={keys_param}"

        try:
            response = http_get_json(url, access_token, timeout=10, debug_file=debug_file)
            if response.get("status") == "success":
                data = response.get("data", {})
                for k, v in data.items():
                    if not isinstance(v, dict):
                        continue
                    # 1. Raw API key (e.g. 'NSE_EQ:RELIANCE' and 'NSE_EQ|RELIANCE')
                    results[k] = v
                    results[k.replace(":", "|")] = v

                    # 2. Key by instrument_token inside payload (e.g. 'NSE_EQ|INE002A01018')
                    inst_token = v.get("instrument_token")
                    if inst_token:
                        results[inst_token] = v
                        results[inst_token.replace("|", ":")] = v

                    # 3. Key by bare trading symbol (e.g. 'RELIANCE')
                    sym = v.get("symbol")
                    if sym:
                        results[sym] = v
                        results[sym.replace(":", "|")] = v
                        if ":" in sym:
                            results[sym.split(":")[-1]] = v
                        elif "|" in sym:
                            results[sym.split("|")[-1]] = v

                    if ":" in k:
                        results[k.split(":")[-1]] = v
                    elif "|" in k:
                        results[k.split("|")[-1]] = v
        except Exception:
            # Continue on error so temporary network hiccups don't stop the scanner
            pass

    return results


# ---------------------------------------------------------------------------
# Option Cash Requirement Calculator (1-Lot ATM Contract)
# ---------------------------------------------------------------------------

def compute_option_requirement(
    resolver: Optional[Any],
    symbol: str,
    ltp: float,
    direction: str = "BULL",
    quotes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolve 1-lot ATM option cash requirement for an underlying instrument.
    Returns:
        {
            "has_fo": bool,
            "one_lot_cost": float,
            "premium": float,
            "lot_size": int,
            "opt_symbol": str,
            "opt_key": str,
            "display_str": str,
            "raw_str": str,
        }
    """
    if resolver is None or ltp <= 0:
        return {
            "has_fo": False,
            "one_lot_cost": 0.0,
            "premium": 0.0,
            "lot_size": 1,
            "opt_symbol": "",
            "opt_key": "",
            "spread_pct": 0.0,
            "display_str": f"{Colors.GRAY}{'Cash Only':^16}{Colors.RESET}",
            "raw_str": "Cash Only",
        }

    opt_type = "PE" if direction == "BEAR" else "CE"
    resolved = None
    try:
        resolved = resolver.resolve_strikes(symbol, ltp, option_type=opt_type)
    except Exception:
        resolved = None

    if not resolved or not resolved.atm_inst:
        return {
            "has_fo": False,
            "one_lot_cost": 0.0,
            "premium": 0.0,
            "lot_size": 1,
            "opt_symbol": "",
            "opt_key": "",
            "spread_pct": 0.0,
            "display_str": f"{Colors.GRAY}{'Cash Only':^16}{Colors.RESET}",
            "raw_str": "Cash Only",
        }

    atm = resolved.atm_inst
    lot_size = atm.lot_size if (getattr(atm, "lot_size", None) and atm.lot_size > 0) else 1
    opt_key = getattr(atm, "upstox_instrument_key", None) or getattr(atm, "instrument_key", None) or ""
    opt_sym = getattr(atm, "trading_symbol", "") or ""

    premium = 0.0
    spread_pct = 0.0
    if quotes:
        q = (
            quotes.get(opt_key)
            or quotes.get(opt_key.replace("|", ":"))
            or quotes.get(opt_sym)
        )
        if q:
            depth = q.get("depth", {}) if isinstance(q.get("depth"), dict) else {}
            bids = depth.get("buy", [])
            asks = depth.get("sell", [])
            if bids and asks:
                best_bid = float(bids[0].get("price", 0.0))
                best_ask = float(asks[0].get("price", 0.0))
                mid = (best_bid + best_ask) / 2.0
                if mid > 0:
                    spread_pct = round(((best_ask - best_bid) / mid) * 100.0, 2)
            if extract_live_option_price:
                try:
                    premium = extract_live_option_price(q, side="BUY")
                except Exception:
                    premium = 0.0

    if premium <= 0.0 and calculate_theoretical_option_premium:
        try:
            premium = calculate_theoretical_option_premium(
                spot=ltp,
                strike=atm.strike_price,
                expiry_date_str=resolved.expiry_date,
                option_type=opt_type,
            )
        except Exception:
            premium = 0.0

    if premium <= 0.0:
        premium = round(max(0.05, ltp * 0.02), 2)

    one_lot_cost = premium * lot_size

    if one_lot_cost >= 100_000:
        cost_txt = f"₹{one_lot_cost / 100000:.1f}L"
    elif one_lot_cost >= 1_000:
        cost_txt = f"₹{one_lot_cost / 1000:.1f}k"
    else:
        cost_txt = f"₹{one_lot_cost:.0f}"

    prem_txt = f"₹{premium:.1f}" if premium < 100 else f"₹{premium:.0f}"

    if spread_pct > 3.0:
        raw_str = f"{cost_txt} ⚠️{spread_pct:.0f}%"
        display_str = f"{Colors.BOLD}{Colors.RED}{raw_str:^16}{Colors.RESET}"
    else:
        raw_str = f"{cost_txt} ({prem_txt})"
        display_str = f"{Colors.BOLD}{Colors.YELLOW}{raw_str:^16}{Colors.RESET}"

    return {
        "has_fo": True,
        "one_lot_cost": round(one_lot_cost, 2),
        "premium": round(premium, 2),
        "lot_size": lot_size,
        "opt_symbol": opt_sym,
        "opt_key": opt_key,
        "spread_pct": spread_pct,
        "display_str": display_str,
        "raw_str": raw_str,
    }


# ---------------------------------------------------------------------------
# Dashboard & Main Scanner Loop
# ---------------------------------------------------------------------------

def _render_row(
    row: Dict[str, Any],
    threshold: float,
    rvol_mode: str = "5m",
    countdown_str: Optional[str] = None,
) -> str:
    """Format a single table row with colors, sparkline mini-chart, 5m RVOL, day RVOL, pace decay, range bar, score, setup badge, option requirement, and activity/timer."""
    sym = row.get("symbol") or row.get("instrument_key", "")
    ltp = safe_float(row.get("last_price"), 0.0)
    pct_chg = safe_float(row.get("pct_change"), 0.0)
    rvol_5m = safe_float(row.get("rvol_5m"), 0.0)
    rvol_cum = safe_float(row.get("rvol_cum"), 0.0)
    pace_ratio = safe_float(row.get("pace_ratio"), 0.0)
    is_inactive = bool(row.get("is_inactive", False))
    activity_state = row.get("activity_state", "NORMAL")
    mode = row.get("mode", "")
    prices = row.get("price_history", [])
    high = safe_float(row.get("high"), ltp)
    low = safe_float(row.get("low"), ltp)
    score = safe_int(row.get("breakout_score"), 0)
    stage = row.get("breakout_stage", "NORMAL")
    direction = row.get("direction", "NEUTRAL")
    opt_req_display = row.get("opt_req_display") or f"{Colors.GRAY}{'Cash Only':^16}{Colors.RESET}"

    spark_str, arrow = make_sparkline(prices, width=8)
    range_bar = make_range_bar(low, high, ltp, width=7)
    score_badge = make_score_badge(score)
    setup_badge = make_setup_badge(stage, direction=direction)

    if countdown_str:
        last_badge = f"{Colors.BOLD}{Colors.RED}{countdown_str:^14}{Colors.RESET}"
    else:
        last_badge = make_activity_badge(activity_state)

    chg_str = f"{pct_chg:+.2f}%" if pct_chg != 0 else "0.00%"
    chg_color = Colors.GREEN if pct_chg > 0 else (Colors.RED if pct_chg < 0 else Colors.WHITE)

    if mode == "no_baseline":
        rvol_5m_str = f"{Colors.WHITE}   N/A  {Colors.RESET}"
        rvol_cum_str = f"{Colors.WHITE}   N/A  {Colors.RESET}"
        pace_str = f"{Colors.WHITE}   N/A {Colors.RESET}"
        spark_display = f"{Colors.GRAY}────────{Colors.RESET}  "
        score_badge = f"{Colors.GRAY}  N/A  {Colors.RESET}"
        setup_badge = f"{Colors.GRAY}  ── INDEX    {Colors.RESET}"
        if not countdown_str:
            last_badge = f"{Colors.GRAY}── INDEX      {Colors.RESET}"
        range_bar = f"{Colors.GRAY}[───●───]{Colors.RESET}"
    else:
        # 5m RVOL coloring
        if rvol_5m >= threshold:
            rvol_5m_str = f"{Colors.BOLD}{Colors.RED}{rvol_5m:>6.2f}x{Colors.RESET}"
        elif rvol_5m >= 1.5:
            rvol_5m_str = f"{Colors.BOLD}{Colors.YELLOW}{rvol_5m:>6.2f}x{Colors.RESET}"
        elif is_inactive or rvol_5m < 0.55:
            rvol_5m_str = f"{Colors.GRAY}{rvol_5m:>6.2f}x{Colors.RESET}"
        else:
            rvol_5m_str = f"{Colors.WHITE}{rvol_5m:>6.2f}x{Colors.RESET}"

        # Day Cumulative RVOL coloring
        if rvol_cum >= 2.0:
            rvol_cum_str = f"{Colors.RED}{rvol_cum:>6.2f}x{Colors.RESET}"
        elif rvol_cum >= 1.3:
            rvol_cum_str = f"{Colors.YELLOW}{rvol_cum:>6.2f}x{Colors.RESET}"
        else:
            rvol_cum_str = f"{Colors.WHITE}{rvol_cum:>6.2f}x{Colors.RESET}"

        # Pace Ratio coloring (Volume Acceleration vs Decay)
        if pace_ratio >= 1.5:
            pace_str = f"{Colors.BOLD}{Colors.GREEN}{pace_ratio:>5.2f}x{Colors.RESET}"
        elif pace_ratio >= 1.0:
            pace_str = f"{Colors.CYAN}{pace_ratio:>5.2f}x{Colors.RESET}"
        elif pace_ratio <= 0.45 or is_inactive:
            pace_str = f"{Colors.GRAY}{pace_ratio:>5.2f}x{Colors.RESET}"
        else:
            pace_str = f"{Colors.WHITE}{pace_ratio:>5.2f}x{Colors.RESET}"

        spark_display = f"{spark_str} {arrow}"

    return (
        f"{sym[:15]:<15} | "
        f"{ltp:>9,.2f} | "
        f"{chg_color}{chg_str:>7}{Colors.RESET} | "
        f" {spark_display} | "
        f"{rvol_5m_str} | "
        f"{rvol_cum_str} | "
        f"{pace_str} | "
        f"{range_bar} | "
        f"{score_badge} | "
        f"{setup_badge} | "
        f"{opt_req_display} | "
        f"{last_badge}"
    )


def _render_block_card(row: Dict[str, Any], threshold: float, rvol_mode: str = "5m") -> str:
    """Format an instrument as a visual terminal card block with bidirectional analytics."""
    sym = row.get("symbol") or row.get("instrument_key", "")
    ltp = safe_float(row.get("last_price"), 0.0)
    pct_chg = safe_float(row.get("pct_change"), 0.0)
    net_chg = safe_float(row.get("net_change"), 0.0)
    today_vol = safe_int(row.get("volume"), 0)
    exp_cum_vol = safe_float(row.get("expected_volume"), 0.0)
    rvol_5m = safe_float(row.get("rvol_5m"), 0.0)
    rvol_cum = safe_float(row.get("rvol_cum"), 0.0)
    proj_5m_vol = safe_float(row.get("proj_5m_vol"), 0.0)
    exp_5m_vol = safe_float(row.get("expected_5m_vol"), 0.0)
    pace_ratio = safe_float(row.get("pace_ratio"), 0.0)
    is_alert = bool(row.get("is_alert", False))
    is_inactive = bool(row.get("is_inactive", False))
    activity_state = row.get("activity_state", "NORMAL")
    mode = row.get("mode", "")
    prices = row.get("price_history", [])
    high = safe_float(row.get("high"), ltp)
    low = safe_float(row.get("low"), ltp)
    score = safe_int(row.get("breakout_score"), 0)
    stage = row.get("breakout_stage", "NORMAL")
    direction = row.get("direction", "NEUTRAL")
    details = row.get("breakout_details", {})
    opt_req_raw = row.get("opt_req_raw", "Cash Only")

    spark_str, arrow = make_sparkline(prices, width=14)
    rvol_5m_meter = make_rvol_meter(rvol_5m, max_scale=3.0, width=8)
    rvol_cum_meter = make_rvol_meter(rvol_cum, max_scale=3.0, width=8)
    range_bar = make_range_bar(low, high, ltp, width=10)

    chg_color = Colors.GREEN if pct_chg > 0 else (Colors.RED if pct_chg < 0 else Colors.WHITE)

    if stage in ("🚀 BREAKOUT", "🚀 BULL BREAKOUT"):
        border_color = Colors.GREEN
        badge = f"{Colors.BG_GREEN}{Colors.BLACK} 🚀 BULL BREAKOUT {score}/100 {Colors.RESET}"
    elif stage in ("🩸 BREAKDOWN", "🩸 BEAR BREAKDOWN"):
        border_color = Colors.RED
        badge = f"{Colors.BG_RED}{Colors.WHITE} 🩸 BEAR BREAKDOWN {score}/100 {Colors.RESET}"
    elif stage == "💥 BULL BREAKOUT":
        border_color = Colors.GREEN
        badge = f"{Colors.BG_GREEN}{Colors.BLACK} 💥 EARLY BULL BREAK {score}/100 {Colors.RESET}"
    elif stage == "💥 BEAR BREAKDOWN":
        border_color = Colors.RED
        badge = f"{Colors.BG_RED}{Colors.WHITE} 💥 EARLY BEAR BREAK {score}/100 {Colors.RESET}"
    elif stage == "⚡ FORMING":
        border_color = Colors.YELLOW
        badge = f"{Colors.BOLD}{Colors.YELLOW} ⚡ FORMING BULL {score}/100 {Colors.RESET}"
    elif stage == "⚡ CRACKING":
        border_color = Colors.MAGENTA
        badge = f"{Colors.BOLD}{Colors.MAGENTA} ⚡ CRACKING BEAR {score}/100 {Colors.RESET}"
    elif stage == "🌀 BULL FLAG" or activity_state == "🌀 BULL FLAG":
        border_color = Colors.CYAN
        badge = f"{Colors.BOLD}{Colors.CYAN} 🌀 BULL FLAG COIL ({score}/100) {Colors.RESET}"
    elif stage == "🚩 BEAR FLAG" or activity_state == "🚩 BEAR FLAG":
        border_color = Colors.RED
        badge = f"{Colors.BOLD}{Colors.RED} 🚩 BEAR FLAG COIL ({score}/100) {Colors.RESET}"
    elif is_inactive:
        border_color = Colors.GRAY
        badge = f"{Colors.GRAY} 💤 INACTIVE (Pace: {pace_ratio:.2f}x | Day: {rvol_cum:.2f}x) {Colors.RESET}"
    elif is_alert:
        border_color = Colors.RED
        badge = f"{Colors.BG_RED}{Colors.WHITE} 🔥 5m RVOL {rvol_5m:.2f}x ALERT {Colors.RESET}"
    elif rvol_5m >= 1.5:
        border_color = Colors.YELLOW
        badge = f"{Colors.BOLD}{Colors.YELLOW} ⚡ 5m SURGE {rvol_5m:.2f}x {Colors.RESET}"
    elif mode == "no_baseline":
        border_color = Colors.GRAY
        badge = f"{Colors.WHITE} INDEX (NO BASELINE) {Colors.RESET}"
    else:
        border_color = Colors.WHITE
        badge = f"{Colors.WHITE} NORMAL ({score}/100) {Colors.RESET}"

    range_pos_val = details.get("range_pos", 50.0)
    v_pts = details.get("vol_score", 0)
    p_pts = details.get("pos_score", 0)
    m_pts = details.get("mom_score", 0)
    t_pts = details.get("tick_score", 0)

    card = [
        f"{border_color}┌── {Colors.BOLD}{Colors.WHITE}{sym:<14}{Colors.RESET}{border_color} ───────────────────────────── [ {badge} {border_color}]──┐{Colors.RESET}",
        f"{border_color}│{Colors.RESET} LTP: {Colors.BOLD}₹{ltp:>9,.2f}{Colors.RESET} ({chg_color}{net_chg:+.2f} / {pct_chg:+.2f}%{Colors.RESET})    Day Range: ₹{low:>8,.2f} {range_bar} ₹{high:<8,.2f} ({range_pos_val:.1f}% Pos)  {border_color}│{Colors.RESET}",
        f"{border_color}│{Colors.RESET} Rolling 5m: {int(proj_5m_vol):>8,d} proj / {int(exp_5m_vol):>8,d} exp  5m RVOL: {rvol_5m_meter} {Colors.BOLD}{rvol_5m:>5.2f}x{Colors.RESET} (Pace: {pace_ratio:>4.2f}x) {border_color}│{Colors.RESET}",
        f"{border_color}│{Colors.RESET} Today Cum : {today_vol:>8,d} act  / {int(exp_cum_vol):>8,d} exp  Day RVOL: {rvol_cum_meter} {Colors.BOLD}{rvol_cum:>5.2f}x{Colors.RESET} ({activity_state:<10}) {border_color}│{Colors.RESET}",
        f"{border_color}│{Colors.RESET} Setup [{direction:<4}]: Score {Colors.BOLD}{score:>2}/100{Colors.RESET} (Vol: {v_pts}/25, Pos: {p_pts}/20, Mom: {m_pts}/30, Ticks: {t_pts}/25)  Req: {opt_req_raw:<15}{border_color}│{Colors.RESET}",
        f"{border_color}│{Colors.RESET} Live Trend: {spark_str} {arrow} (Last {len(prices[-14:])} ticks)                                            {border_color}│{Colors.RESET}",
        f"{border_color}└─────────────────────────────────────────────────────────────────────────────────┘{Colors.RESET}",
    ]
    return "\n".join(card)


def print_dashboard(
    alerts: List[Dict[str, Any]],
    confirmed_setups: List[Dict[str, Any]],
    forming_setups: List[Dict[str, Any]],
    flag_setups: List[Dict[str, Any]],
    all_rows: List[Dict[str, Any]],
    threshold: float,
    scan_count: int,
    alerts_only: bool = False,
    setups_only: bool = False,
    block_mode: bool = False,
    sort_mode: str = "score",
    rvol_mode: str = "5m",
    direction_filter: str = "both",
    fresh_alerts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Render real-time bidirectional (Bullish & Bearish) setup, RVOL, and 8-second fresh alert dashboard."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    fresh_cnt = len(fresh_alerts) if fresh_alerts else 0
    bull_break_cnt = sum(1 for r in all_rows if r.get("breakout_stage") in ("🚀 BREAKOUT", "💥 BULL BREAKOUT"))
    bear_break_cnt = sum(1 for r in all_rows if r.get("breakout_stage") in ("🩸 BREAKDOWN", "💥 BEAR BREAKDOWN"))
    flag_break_cnt = sum(1 for r in all_rows if r.get("breakout_stage") in ("💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN"))
    forming_cnt = sum(1 for r in all_rows if r.get("breakout_stage") in ("⚡ FORMING", "⚡ CRACKING"))
    flag_cnt = sum(1 for r in all_rows if r.get("breakout_stage") in ("🌀 BULL FLAG", "🚩 BEAR FLAG") or r.get("activity_state") in ("🌀 BULL FLAG", "🚩 BEAR FLAG"))
    inactive_cnt = sum(1 for r in all_rows if r.get("is_inactive") or r.get("is_dormant"))

    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 148}{Colors.RESET}")
    print(
        f"{Colors.BOLD}{Colors.WHITE} UPSTOX BIDIRECTIONAL 5-MIN & DAY RVOL SCANNER (BREAKOUTS, BREAKDOWNS & FLAGS) {Colors.RESET}"
        f" | Time: {Colors.YELLOW}{now_str}{Colors.RESET} | Scan #{scan_count}"
    )
    print(
        f" Monitored: {Colors.BOLD}{len(all_rows)}{Colors.RESET} instruments"
        f" | 🚨 Fresh (<8s): {Colors.BOLD}{Colors.RED if fresh_cnt else Colors.GREEN}{fresh_cnt}{Colors.RESET}"
        f" | 💥 Flag Breaks: {Colors.BOLD}{Colors.GREEN if flag_break_cnt else Colors.WHITE}{flag_break_cnt}{Colors.RESET}"
        f" | 🚀 Breakouts: {Colors.BOLD}{Colors.GREEN}{bull_break_cnt}{Colors.RESET}"
        f" | 🩸 Breakdowns: {Colors.BOLD}{Colors.RED}{bear_break_cnt}{Colors.RESET}"
        f" | ⚡ Forming/Crack: {Colors.BOLD}{Colors.YELLOW}{forming_cnt}{Colors.RESET}"
        f" | 🚩 Flags/Coil: {Colors.BOLD}{Colors.CYAN}{flag_cnt}{Colors.RESET}"
        f" | 🔥 Alerts: {Colors.BOLD}{Colors.RED if alerts else Colors.GREEN}{len(alerts)}{Colors.RESET}"
        f" | 💤 Inactive: {Colors.BOLD}{Colors.GRAY}{inactive_cnt}{Colors.RESET}"
        f" | Filter: {Colors.BOLD}{Colors.YELLOW}{direction_filter.upper()}{Colors.RESET}"
    )
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 148}{Colors.RESET}\n")

    header = (
        f"{'SYMBOL / ASSET':<15} | "
        f"{'LTP (₹)':>9} | "
        f"{'CHG %':>7} | "
        f"{'MINI CHART':^11} | "
        f"{'5m RVOL':>8} | "
        f"{'DAY RVOL':>8} | "
        f"{'PACE':>7} | "
        f"{'RANGE POS':^9} | "
        f"{'SCORE':^7} | "
        f"{'SETUP STAGE':^14} | "
        f"{'1-LOT REQ (ATM)':^16} | "
        f"{'ACTIVITY STATUS':<14}"
    )

    fresh_header = (
        f"{'SYMBOL / ASSET':<15} | "
        f"{'LTP (₹)':>9} | "
        f"{'CHG %':>7} | "
        f"{'MINI CHART':^11} | "
        f"{'5m RVOL':>8} | "
        f"{'DAY RVOL':>8} | "
        f"{'PACE':>7} | "
        f"{'RANGE POS':^9} | "
        f"{'SCORE':^7} | "
        f"{'SETUP STAGE':^14} | "
        f"{'1-LOT REQ (ATM)':^16} | "
        f"{'COUNTDOWN':^14}"
    )

    # 0. Fresh Breakout Alerts Section (< 8s Execution Window)
    if fresh_alerts:
        print(f"{Colors.BG_RED}{Colors.WHITE}{Colors.BOLD} 🚨 FRESH BREAKOUT ALERTS (< 8s WINDOW — ACT INSTANTLY!) {Colors.RESET}")
        print(Colors.BOLD + fresh_header + Colors.RESET)
        print("-" * 148)
        for f_alert in fresh_alerts:
            row = f_alert.get("row_data", {})
            rem_sec = f_alert.get("rem_sec", 0.0)
            countdown_str = f"⏳ {rem_sec:.1f}s left"
            print(_render_row(row, threshold, rvol_mode=rvol_mode, countdown_str=countdown_str))
        print("-" * 148)
        print()

    # 1. Confirmed Setups Section (🚀 Bull Breakouts & 🩸 Bear Breakdowns, including downgraded Flag Breaks)
    if confirmed_setups:
        print(f"{Colors.BOLD}{Colors.GREEN}🎯 CONFIRMED BREAKOUTS & BREAKDOWNS (🚀 HOD Breakouts, 🩸 LOD Breakdowns & 💥 Flag Breaks):{Colors.RESET}")
        if block_mode:
            for row in confirmed_setups:
                print(_render_block_card(row, threshold, rvol_mode=rvol_mode))
                print()
        else:
            print(Colors.BOLD + header + Colors.RESET)
            print("-" * 148)
            for row in confirmed_setups:
                print(_render_row(row, threshold, rvol_mode=rvol_mode))
            print("-" * 148)
    elif setups_only and not fresh_alerts:
        print(f"{Colors.YELLOW}ℹ️  Active Breakouts & Breakdowns: None currently triggered.{Colors.RESET}")

    # 2. Forming Moves & Flag Consolidations Section
    forming_and_flags = forming_setups + [r for r in flag_setups if r not in forming_setups]
    if forming_and_flags:
        print(f"\n{Colors.BOLD}{Colors.YELLOW}⚡ FORMING MOVES & FLAGS (⚡ Bull Forming, ⚡ Bear Cracking, 🌀 Bull Flags & 🚩 Bear Flags):{Colors.RESET}")
        if block_mode:
            for row in forming_and_flags:
                print(_render_block_card(row, threshold, rvol_mode=rvol_mode))
                print()
        else:
            print(Colors.BOLD + header + Colors.RESET)
            print("-" * 148)
            for row in forming_and_flags:
                print(_render_row(row, threshold, rvol_mode=rvol_mode))
            print("-" * 148)

    # 3. Other Active RVOL Alerts Section (RVOL >= threshold, not already shown above)
    displayed_keys = {r["instrument_key"] for r in confirmed_setups + forming_and_flags}
    if fresh_alerts:
        displayed_keys.update(f.get("row_data", {}).get("instrument_key", "") for f in fresh_alerts)
    other_alerts = [r for r in alerts if r["instrument_key"] not in displayed_keys]

    if other_alerts and not setups_only:
        print(f"\n{Colors.BOLD}{Colors.RED}🚨 OTHER HIGH RVOL ALERTS ({rvol_mode.upper()} RVOL >= {threshold:.1f}x):{Colors.RESET}")
        if block_mode:
            for row in other_alerts:
                print(_render_block_card(row, threshold, rvol_mode=rvol_mode))
                print()
        else:
            print(Colors.BOLD + header + Colors.RESET)
            print("-" * 148)
            for row in other_alerts:
                print(_render_row(row, threshold, rvol_mode=rvol_mode))
            print("-" * 148)
    elif not fresh_alerts and not confirmed_setups and not forming_and_flags and not other_alerts:
        print(f"{Colors.YELLOW}ℹ️  Active Setups & Alerts: None currently triggered ({rvol_mode.upper()} RVOL >= {threshold:.1f}x).{Colors.RESET}")

    # 4. Top Monitored Benchmark (if not alerts_only or setups_only)
    if not alerts_only and not setups_only:
        all_displayed = displayed_keys.union({r["instrument_key"] for r in other_alerts})
        top_candidates = [r for r in all_rows if r["instrument_key"] not in all_displayed][:10]
        sort_label = "Setup Score & 5m RVOL" if sort_mode in ("score", "breakout") else ("5m Relative Volume" if sort_mode == "5m_rvol" else ("Day RVOL" if sort_mode == "cum_rvol" else "% Change"))
        print(f"\n{Colors.BOLD}{Colors.WHITE}📊 TOP MONITORED ACTIVE STOCKS (Combined Ranking by {sort_label}):{Colors.RESET}")
        if block_mode:
            for row in top_candidates:
                print(_render_block_card(row, threshold, rvol_mode=rvol_mode))
                print()
        else:
            print(Colors.BOLD + header + Colors.RESET)
            print("-" * 148)
            if top_candidates:
                for row in top_candidates:
                    print(_render_row(row, threshold, rvol_mode=rvol_mode))
            else:
                print(f"  {Colors.YELLOW}No quotes received yet... Waiting for market data.{Colors.RESET}")
            print("-" * 148)

    print(f"Press {Colors.BOLD}Ctrl+C{Colors.RESET} to stop scanner.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream live feeds from Upstox, filter instruments with rolling 5-minute RVOL surges, and rank bidirectional setups (Bullish Breakouts & Bearish Breakdowns/Flags).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan instruments using combined bidirectional ranking (default: Bull & Bear):
  python3 stream_rvol_scanner.py --index volume_index.json

  # Scan ONLY Bearish setups (Breakdowns & Bear Flags):
  python3 stream_rvol_scanner.py --index volume_index.json --direction bear
  python3 stream_rvol_scanner.py --index volume_index.json --bear-only

  # Scan ONLY Bullish setups (Breakouts & Bull Flags):
  python3 stream_rvol_scanner.py --index volume_index.json --bull-only

  # Show ONLY active confirmed setups, forming moves, and flag squeezes:
  python3 stream_rvol_scanner.py --index volume_index.json --setups-only

  # Hide inactive and dormant stocks:
  python3 stream_rvol_scanner.py --index volume_index.json --hide-inactive

  # Visual Block Cards View:
  python3 stream_rvol_scanner.py --index volume_index.json --blocks
        """,
    )

    parser.add_argument("--index", "-i", default="volume_index.json", help="Path to volume_index.json (default: volume_index.json)")
    parser.add_argument("--threshold", "-t", type=float, default=2.0, help="RVOL alert threshold (default: 2.0)")
    parser.add_argument("--direction", "-d", choices=["both", "bull", "bear"], default="both", help="Filter direction: 'both' (default, combined long & short feed), 'bull' (long breakouts & bull flags), or 'bear' (short breakdowns & bear flags)")
    parser.add_argument("--bull-only", action="store_true", help="Shortcut for --direction bull")
    parser.add_argument("--bear-only", action="store_true", help="Shortcut for --direction bear")
    parser.add_argument("--rvol-mode", choices=["5m", "1m", "cum", "either"], default="5m", help="RVOL alert basis: '5m' (default, rolling 5-minute window RVOL), '1m' (intra-minute candle RVOL), 'cum' (all-day cumulative RVOL), or 'either'")
    parser.add_argument("--sort", choices=["score", "breakout", "5m_rvol", "1m_rvol", "cum_rvol", "change"], default="score", help="Ranking order: 'score' (default, prioritizes breakouts, breakdowns & flags), '5m_rvol', '1m_rvol', 'cum_rvol', or 'change'")
    parser.add_argument("--interval", type=int, default=3, help="Polling interval in seconds (default: 3)")
    parser.add_argument("--alerts-only", action="store_true", help="Display only instruments currently crossing the RVOL threshold")
    parser.add_argument("--setups-only", action="store_true", help="Display only instruments actively in a confirmed setup, forming move, or flag consolidation")
    parser.add_argument("--breakouts-only", action="store_true", help="Alias for --setups-only")
    parser.add_argument("--hide-inactive", action="store_true", help="Hide inactive and dormant stocks from the dashboard")
    parser.add_argument("--min-score", type=int, default=67, help="Minimum Setup Score (0-100) to display on tables (default: 67)")
    parser.add_argument("--output-alerts", help="Append triggered alerts to a CSV file (e.g. rvol_alerts.csv)")
    parser.add_argument("--blocks", action="store_true", help="Display instruments in visual terminal block cards with mini charts instead of table")
    parser.add_argument("--paper-trade", action="store_true", help="Enable live automated forward paper-trading of Bear Flag Put Options")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Starting capital for paper trading in INR (default: 100,000)")
    parser.add_argument("--strike-mode", choices=["all", "atm", "itm", "otm", "low_prem", "trail_sl", "fixed_tgt", "combo"], default="all", help="Paper trade strike mode: 'all' (simulates ATM, ITM, OTM, LOW_PREM, TRAIL_SL, FIXED_TGT, COMBO), or specific mode")
    parser.add_argument("--paper-csv", default="logs/paper_trades.csv", help="CSV file path to record paper trades journal (default: logs/paper_trades.csv)")
    parser.add_argument("--max-spread", type=float, default=3.0, help="Maximum option Bid-Ask spread %% allowed for paper entry (default: 3.0%%)")
    parser.add_argument("--max-lots", type=int, default=1, help="Maximum lots allowed per trade setup (default: 1 to avoid capital concentration)")
    parser.add_argument("--brokerage", type=float, default=30.0, help="Brokerage fee per order in INR (default: 30.0 flat, round-trip = 60.0)")
    parser.add_argument("--target-pct", type=float, default=5.0, help="Initial target profit percentage on premium paid (default: 5.0%% + brokerage)")
    parser.add_argument("--fixed-target", type=float, default=750.0, help="Fixed target net gain in INR for dual target simulation (default: 750.0)")
    parser.add_argument("--target-decay-pct", type=float, default=2.5, help="Rolling target floor percentage (default: 2.5%%)")
    parser.add_argument("--breakeven-target-pct", type=float, default=0.5, help="Breakeven target percentage after 30m in red (default: 0.5%%)")
    parser.add_argument("--intermediate-trail-pct", type=float, default=3.5, help="Threshold percentage to activate intermediate trailing floor (default: 3.5%%)")
    parser.add_argument("--intermediate-floor-pct", type=float, default=2.5, help="Base floor percentage for intermediate trailing floor (default: 2.5%%)")
    parser.add_argument("--trailing-step-pct", type=float, default=1.0, help="Trailing distance percentage behind peak for trailing floors (default: 1.0%%)")
    parser.add_argument("--target-runner-seconds", type=float, default=30.0, help="Seconds to let trade run after reaching target with rolling floor (default: 30.0s)")
    parser.add_argument("--r-stop", type=float, default=0.15, help="Option premium stop loss drop fraction (default: 0.15 = 15%% drop)")
    parser.add_argument("--show-positions", action="store_true", help="Display full active positions table in paper trading dashboard (default: False, compact summary)")
    parser.add_argument("--record-dir", default="recordings", help="Directory to save session recordings (default: recordings)")
    parser.add_argument("--no-record", action="store_true", help="Disable continuous market session data recording")
    parser.add_argument("--debug", action="store_true", help="Dump raw API requests and responses to debug.log")
    parser.add_argument("--debug-file", default="logs/debug.log", help="Path to debug log file (default: logs/debug.log)")

    args = parser.parse_args()

    # Handle directional shortcuts
    if args.bull_only:
        args.direction = "bull"
    elif args.bear_only:
        args.direction = "bear"

    if args.breakouts_only:
        args.setups_only = True

    # 1. Resolve and verify volume index exists
    args.index = resolve_file_path(args.index, fallback_dir="data")
    if not os.path.exists(args.index):
        print(f"\nError: Volume index file not found: {args.index}", file=sys.stderr)
        print("Please run Script 2 first to generate the index:", file=sys.stderr)
        print("  python3 tools/build_volume_index.py --input filtered.csv\n", file=sys.stderr)
        sys.exit(1)

    with open(args.index, "r", encoding="utf-8") as f:
        volume_index: Dict[str, Any] = json.load(f)

    if not volume_index:
        print(f"Error: Volume index at {args.index} is empty.", file=sys.stderr)
        sys.exit(1)

    # 2. Setup Debug Logging
    debug_log_path = resolve_file_path(args.debug_file, fallback_dir="logs") if args.debug else None
    if debug_log_path:
        os.makedirs(os.path.dirname(os.path.abspath(debug_log_path)), exist_ok=True)
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\n[SCANNER DEBUG SESSION STARTED: {datetime.now().isoformat()}] Direction={args.direction.upper()} Basis={args.rvol_mode.upper()} Threshold={args.threshold}x\n{'='*70}\n")
        print(f"Debug mode enabled: Raw API responses will be dumped to '{debug_log_path}'")

    # 3. Get access token from .env
    token = get_access_token()

    # 4. Continuous Session Data Recorder
    session_recorder: Optional[SessionDataRecorder] = None
    if not args.no_record:
        session_recorder = SessionDataRecorder(record_dir=args.record_dir)
        print(f"Session Recorder active -> {session_recorder.filepath}")

    instrument_keys = list(volume_index.keys())
    sample_profile = next(iter(volume_index.values()), {})
    idx_interval = sample_profile.get("interval_minutes", 1)

    print(f"Loaded {len(instrument_keys):,} instruments from {args.index} (Candle Interval: {idx_interval}m).")
    print(f"Starting live Bidirectional RVOL scanner (Direction: {args.direction.upper()} | Basis: {args.rvol_mode.upper()} RVOL | Threshold: > {args.threshold:.1f}x)...")
    time.sleep(1)

    scan_count = 0
    logged_alert_keys = set()
    price_histories: Dict[str, List[float]] = defaultdict(list)
    last_tick_times: Dict[str, float] = {}
    rolling_tracker = RollingVolumeTracker()
    fresh_breakout_tracker: Dict[str, Dict[str, Any]] = {}
    downgraded_breakouts: Dict[str, Dict[str, Any]] = {}
    previous_stages: Dict[str, str] = {}
    active_opt_keys: set = set()

    # Initialize OptionStrikeResolver for 1-Lot ATM Premium calculation
    inst_csv = resolve_file_path("instrument.csv", fallback_dir="data")
    strike_resolver: Optional[Any] = None
    if OptionStrikeResolver and os.path.exists(inst_csv):
        try:
            strike_resolver = OptionStrikeResolver(instrument_csv=inst_csv, access_token=token)
        except Exception as e:
            if args.debug:
                print(f"[Resolver Warning] Could not init OptionStrikeResolver: {e}", file=sys.stderr)

    # Initialize Bidirectional Paper Portfolio if requested
    paper_portfolio: Optional[Any] = None
    if args.paper_trade:
        if FlagPaperPortfolio is None and BearFlagPaperPortfolio is None:
            print("Error: bidirectional_engine module could not be loaded for paper trading.", file=sys.stderr)
            sys.exit(1)
        engine_cls = FlagPaperPortfolio or BearFlagPaperPortfolio
        inst_csv = resolve_file_path("instrument.csv", fallback_dir="data")
        inst_mgr = load_instruments(inst_csv) if os.path.exists(inst_csv) and load_instruments else None
        paper_portfolio = engine_cls(
            starting_capital=args.capital,
            direction=args.direction,
            strike_mode=args.strike_mode,
            output_csv=args.paper_csv,
            instrument_csv=inst_csv,
            manager=inst_mgr,
            access_token=token,
            max_spread_pct=args.max_spread,
            max_lots=args.max_lots,
            brokerage_per_order=args.brokerage,
            target_pct=args.target_pct,
            r_stop=args.r_stop,
            target_decay_pct=args.target_decay_pct,
            breakeven_target_pct=args.breakeven_target_pct,
            intermediate_trail_trigger_pct=args.intermediate_trail_pct,
            intermediate_trail_floor_pct=args.intermediate_floor_pct,
            trailing_step_pct=args.trailing_step_pct,
            target_runner_seconds=args.target_runner_seconds,
            fixed_target_gain=args.fixed_target,
            show_positions=args.show_positions,
        )
        print(f"Paper-Trading enabled: Capital=₹{args.capital:,.0f} | Direction={args.direction.upper()} | Strike Mode={args.strike_mode.upper()} | Max Lots={args.max_lots} | Brkr=₹{args.brokerage*2:.0f}/trade | Tgt=Rolling {args.target_pct:.1f}->{args.target_decay_pct:.1f}% (+{args.breakeven_target_pct:.1f}% BE if red >30m) | Runner={args.target_runner_seconds:.0f}s with 1.0% trail | Trail Floor=+{args.intermediate_trail_pct:.1f}%->+{args.intermediate_floor_pct:.1f}% | SL=-{args.r_stop*100:.0f}% (upped 33% if red >30m) | Journal={args.paper_csv}")

    # Setup CSV logger if requested
    alert_csv_writer = None
    csv_file = None
    if args.output_alerts:
        file_exists = os.path.exists(args.output_alerts)
        csv_file = open(args.output_alerts, "a", newline="", encoding="utf-8")
        fieldnames = [
            "timestamp",
            "instrument_key",
            "symbol",
            "direction",
            "last_price",
            "net_change",
            "pct_change",
            "proj_5m_vol",
            "expected_5m_vol",
            "rvol_5m",
            "today_vol",
            "expected_cum_vol",
            "rvol_cum",
            "pace_ratio",
            "activity_state",
            "setup_score",
            "setup_stage",
            "is_inactive",
            "is_coiling",
        ]
        alert_csv_writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if not file_exists:
            alert_csv_writer.writeheader()

    try:
        while True:
            scan_count += 1
            now = datetime.now()
            now_epoch = now.timestamp()
            current_slot = get_current_time_slot(idx_interval)

            # Determine keys to quote: underlying equities + active/pending options if paper-trading
            query_keys = list(instrument_keys)
            if paper_portfolio:
                query_keys.extend(paper_portfolio.get_required_option_keys())
            if active_opt_keys:
                query_keys.extend(list(active_opt_keys))

            # Fetch live market quotes
            quotes = fetch_live_quotes_batch(query_keys, token, debug_file=debug_log_path)

            all_rows: List[Dict[str, Any]] = []
            alerts: List[Dict[str, Any]] = []

            for key in instrument_keys:
                profile = volume_index[key]
                sym = profile.get("symbol") or profile.get("name") or ""
                exch = profile.get("exchange") or "NSE"

                # Multi-fallback: ISIN key ('NSE_EQ|INE...'), colon key, 'NSE_EQ:RELIANCE', or bare 'RELIANCE'
                quote = (
                    quotes.get(key)
                    or quotes.get(key.replace("|", ":"))
                    or (quotes.get(f"{exch}_EQ:{sym}") if sym else None)
                    or (quotes.get(f"{exch}_EQ|{sym}") if sym else None)
                    or (quotes.get(f"{exch}_FO:{sym}") if sym else None)
                    or (quotes.get(f"{exch}_FO|{sym}") if sym else None)
                    or (quotes.get(sym) if sym else None)
                )

                if not quote or not isinstance(quote, dict):
                    continue

                # Safely parse volume
                vol_raw = quote.get("volume")
                if vol_raw is None:
                    vol_raw = quote.get("volume_traded") or (quote.get("ohlc", {}).get("volume") if isinstance(quote.get("ohlc"), dict) else 0)
                today_vol = safe_int(vol_raw, 0)

                # Safely parse LTP
                ltp_raw = quote.get("last_price")
                if ltp_raw is None and isinstance(quote.get("ohlc"), dict):
                    ltp_raw = quote["ohlc"].get("close")
                ltp = safe_float(ltp_raw, 0.0)

                # Safely parse net change and compute true percentage change
                net_chg = safe_float(quote.get("net_change"), 0.0)
                ohlc_dict = quote.get("ohlc", {}) if isinstance(quote.get("ohlc"), dict) else {}
                prev_close = safe_float(ohlc_dict.get("close"), 0.0)
                if prev_close <= 0 and ltp > 0:
                    prev_close = ltp - net_chg
                pct_chg = round((net_chg / prev_close) * 100, 2) if prev_close > 0 else 0.0

                # Track rolling price history for sparklines & mini block charts with feed deduplication
                if ltp > 0:
                    hist = price_histories[key]
                    now_epoch = now.timestamp()
                    if not hist:
                        if prev_close > 0:
                            hist.append(prev_close)
                        o_val = safe_float(ohlc_dict.get("open"), 0.0)
                        if o_val > 0:
                            hist.append(o_val)
                        hist.append(ltp)
                        last_tick_times[key] = now_epoch
                    else:
                        # Deduplicate polling noise: append only on price tick change or >= 15s pause
                        if ltp != hist[-1] or (now_epoch - last_tick_times.get(key, 0.0) >= 15.0):
                            hist.append(ltp)
                            last_tick_times[key] = now_epoch
                            if len(hist) > 16:
                                price_histories[key] = hist[-16:]

                sym = profile.get("symbol") or profile.get("name") or key

                open_val = safe_float(ohlc_dict.get("open"), ltp)
                high_val = safe_float(ohlc_dict.get("high"), ltp)
                low_val = safe_float(ohlc_dict.get("low"), ltp)

                # 1. Calculate All-Day Cumulative RVOL
                rvol_cum, exp_cum_vol, mode = calculate_cumulative_rvol(today_vol, profile, current_time_str=current_slot)

                # Range position for coiling/flag detection
                day_span = high_val - low_val
                range_pos = (ltp - low_val) / day_span if day_span > 0 else 0.5

                # 2. Calculate Rolling 5-Minute Volume, Variance (Z_vol), Impulse & Extreme Freshness
                v_stats = rolling_tracker.update(
                    key=key,
                    current_volume=today_vol,
                    rvol_cum=rvol_cum,
                    profile=profile,
                    range_pos=range_pos,
                    ltp=ltp,
                    high_price=high_val,
                    low_price=low_val,
                    prev_close=prev_close,
                    open_price=open_val,
                    now=datetime.now(),
                )
                rvol_5m = v_stats["rvol_5m"]
                rvol_1m = v_stats["rvol_1m"]
                proj_5m_vol = v_stats["proj_5m_vol"]
                exp_5m_vol = v_stats["expected_5m_vol"]
                proj_1m_vol = v_stats["proj_1m_vol"]
                exp_1m_vol = v_stats["expected_1m_vol"]
                pace_ratio = v_stats["pace_ratio"]
                activity_state = v_stats["activity_state"]
                is_inactive = v_stats["is_inactive"]
                is_coiling = v_stats["is_coiling"]
                is_dormant = v_stats["is_dormant"]
                elapsed_5m = v_stats["elapsed_5m"]

                # 3. Alert determination based on user's chosen rvol_mode
                if args.rvol_mode == "5m":
                    is_alert = (rvol_5m >= args.threshold) and (mode not in ("no_baseline", "zero_vol")) and (exp_5m_vol > 0)
                elif args.rvol_mode == "1m":
                    is_alert = (rvol_1m >= args.threshold) and (mode not in ("no_baseline", "zero_vol")) and (exp_1m_vol > 0)
                elif args.rvol_mode == "cum":
                    is_alert = (rvol_cum >= args.threshold) and (mode not in ("no_baseline", "zero_vol")) and (exp_cum_vol > 0)
                else:  # 'either'
                    is_alert = ((rvol_5m >= args.threshold and exp_5m_vol > 0) or (rvol_cum >= args.threshold and exp_cum_vol > 0)) and (mode not in ("no_baseline", "zero_vol"))

                # Tentative direction for ATM option resolution & spread inspection
                tentative_dir = "BULL" if range_pos >= 0.50 else "BEAR"

                # Pre-calculate Option Requirement & Live Spread Penalty before scoring
                opt_req = compute_option_requirement(
                    resolver=strike_resolver,
                    symbol=sym,
                    ltp=ltp,
                    direction=tentative_dir,
                    quotes=quotes,
                )

                # 4. Bidirectional Setup Score (Bullish Breakouts & Bearish Breakdowns/Flags)
                s_score, s_stage, s_dir, s_details = calculate_setup_score(
                    rvol_5m=rvol_5m,
                    rvol_1m=rvol_1m,
                    rvol_cum=rvol_cum,
                    ltp=ltp,
                    open_price=open_val,
                    high_price=high_val,
                    low_price=low_val,
                    pct_change=pct_chg,
                    price_history=list(price_histories[key]),
                    is_inactive=is_inactive,
                    mode=mode,
                    z_vol=v_stats["z_vol"],
                    pct_change_5m=v_stats["pct_change_5m"],
                    seconds_since_hod=v_stats["seconds_since_hod"],
                    seconds_since_lod=v_stats["seconds_since_lod"],
                    option_spread_pct=opt_req.get("spread_pct", 0.0),
                    is_flag_breakout=v_stats.get("is_flag_breakout", False),
                    flag_breakout_type=v_stats.get("flag_breakout_type", ""),
                    has_bull_pole=v_stats.get("has_bull_pole", True),
                    has_bear_pole=v_stats.get("has_bear_pole", True),
                    vol_accel=v_stats.get("vol_accel", 1.0),
                    is_dry_extreme=v_stats.get("is_dry_extreme", False),
                    rsi=v_stats.get("rsi", 50.0),
                    macd_hist=v_stats.get("macd_hist", 0.0),
                    bull_clearance_pct=v_stats.get("bull_clearance_pct", 1.0),
                    bear_clearance_pct=v_stats.get("bear_clearance_pct", 1.0),
                    is_near_resistance=v_stats.get("is_near_resistance", False),
                    is_near_support=v_stats.get("is_near_support", False),
                )

                # If final direction flipped from tentative, refresh option contract
                if s_dir != tentative_dir and opt_req.get("has_fo"):
                    opt_req = compute_option_requirement(
                        resolver=strike_resolver,
                        symbol=sym,
                        ltp=ltp,
                        direction=s_dir,
                        quotes=quotes,
                    )

                if opt_req.get("opt_key"):
                    active_opt_keys.add(opt_req["opt_key"])
                    if len(active_opt_keys) > 200:
                        active_opt_keys.clear()

                row_data = {
                    "instrument_key": key,
                    "symbol": sym,
                    "direction": s_dir,
                    "last_price": ltp,
                    "net_change": net_chg,
                    "pct_change": pct_chg,
                    "volume": today_vol,
                    "expected_volume": exp_cum_vol,
                    "rvol": rvol_5m,            # Primary real-time metric
                    "rvol_5m": rvol_5m,
                    "rvol_1m": rvol_1m,
                    "rvol_cum": rvol_cum,
                    "proj_5m_vol": proj_5m_vol,
                    "expected_5m_vol": exp_5m_vol,
                    "proj_1m_vol": proj_1m_vol,
                    "expected_1m_vol": exp_1m_vol,
                    "pace_ratio": pace_ratio,
                    "activity_state": activity_state,
                    "is_inactive": is_inactive,
                    "is_bull_coiling": v_stats["is_bull_coiling"],
                    "is_bear_coiling": v_stats["is_bear_coiling"],
                    "is_coiling": is_coiling,
                    "is_dormant": is_dormant,
                    "elapsed_5m": elapsed_5m,
                    "mode": mode,
                    "is_alert": is_alert,
                    "price_history": list(price_histories[key]),
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "breakout_score": s_score,
                    "breakout_stage": s_stage,
                    "breakout_details": s_details,
                    "opt_req_display": opt_req["display_str"],
                    "opt_req_raw": opt_req["raw_str"],
                    "one_lot_cost": opt_req["one_lot_cost"],
                    "opt_premium": opt_req["premium"],
                    "opt_symbol": opt_req["opt_symbol"],
                    "opt_key": opt_req["opt_key"],
                    "has_fo": opt_req["has_fo"],
                }

                all_rows.append(row_data)

                # Track Breakouts & Transition for 8-Second Fresh Breakout Alert
                breakout_stages = {"🚀 BREAKOUT", "🩸 BREAKDOWN", "💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN"}
                prev_stage = previous_stages.get(key, "NORMAL")
                previous_stages[key] = s_stage
                now_ts = time.time()

                if s_stage in breakout_stages and prev_stage not in breakout_stages:
                    # New breakout triggered - start 8-second countdown!
                    fresh_breakout_tracker[key] = {
                        "trigger_ts": now_ts,
                        "symbol": sym,
                        "stage": s_stage,
                        "direction": s_dir,
                        "row_data": row_data,
                    }
                elif key in fresh_breakout_tracker:
                    fresh_breakout_tracker[key]["row_data"] = row_data
                    if s_stage in breakout_stages:
                        fresh_breakout_tracker[key]["stage"] = s_stage

                if is_alert or s_stage in breakout_stages:
                    alerts.append(row_data)
                    if session_recorder:
                        session_recorder.record_alert(row_data)

                    # Log to CSV if enabled
                    if alert_csv_writer and key not in logged_alert_keys:
                        alert_csv_writer.writerow({
                            "timestamp": datetime.now().isoformat(),
                            "instrument_key": key,
                            "symbol": sym,
                            "direction": s_dir,
                            "last_price": ltp,
                            "net_change": net_chg,
                            "pct_change": pct_chg,
                            "proj_5m_vol": proj_5m_vol,
                            "expected_5m_vol": exp_5m_vol,
                            "rvol_5m": rvol_5m,
                            "today_vol": today_vol,
                            "expected_cum_vol": exp_cum_vol,
                            "rvol_cum": rvol_cum,
                            "pace_ratio": pace_ratio,
                            "activity_state": activity_state,
                            "setup_score": s_score,
                            "setup_stage": s_stage,
                            "is_inactive": is_inactive,
                            "is_coiling": is_coiling,
                        })
                        csv_file.flush()
                        logged_alert_keys.add(key)

                # Feed into Flag paper-trading engine
                if paper_portfolio:
                    clearance = v_stats.get("bull_clearance_pct" if s_dir == "BULL" else "bear_clearance_pct", 1.0)
                    paper_portfolio.process_underlying_tick(
                        symbol=sym,
                        ltp=ltp,
                        high=high_val,
                        low=low_val,
                        range_pos=range_pos,
                        rvol_5m=rvol_5m,
                        rvol_cum=rvol_cum,
                        stage=s_stage,
                        score=s_score,
                        now=datetime.now(),
                        vol_accel=v_stats.get("vol_accel", 1.0),
                        is_dry_extreme=v_stats.get("is_dry_extreme", False),
                        clearance_pct=clearance,
                        is_near_resistance=v_stats.get("is_near_resistance", False),
                        is_near_support=v_stats.get("is_near_support", False),
                        rsi=v_stats.get("rsi", 50.0),
                        macd_hist=v_stats.get("macd_hist", 0.0),
                    )

            # Process option quotes & evaluate exits/triggers for paper portfolio
            if paper_portfolio:
                needed_opt_keys = paper_portfolio.get_required_option_keys()
                missing_keys = [k for k in needed_opt_keys if k not in quotes and k.replace("|", ":") not in quotes]
                if missing_keys:
                    fresh_opt_quotes = fetch_live_quotes_batch(missing_keys, token)
                    quotes.update(fresh_opt_quotes)

                paper_portfolio.process_market_quotes(
                    underlying_quotes=quotes,
                    option_quotes=quotes,
                    now=datetime.now(),
                )

            # Record continuous market snapshot to session file
            if session_recorder:
                session_recorder.record_snapshot(all_rows)

            # Optional filter: direction filter (bull or bear only)
            if args.direction == "bull":
                all_rows = [r for r in all_rows if r.get("direction") == "BULL"]
                alerts = [r for r in alerts if r.get("direction") == "BULL"]
            elif args.direction == "bear":
                all_rows = [r for r in all_rows if r.get("direction") == "BEAR"]
                alerts = [r for r in alerts if r.get("direction") == "BEAR"]

            # Optional filter: hide inactive/dormant stocks
            if args.hide_inactive:
                all_rows = [r for r in all_rows if not (r.get("is_inactive") or r.get("is_dormant"))]
                alerts = [r for r in alerts if not (r.get("is_inactive") or r.get("is_dormant"))]

            # Sort instruments based on user ranking preference
            if args.sort in ("score", "breakout"):
                alerts.sort(key=setup_rank_key, reverse=True)
                all_rows.sort(key=setup_rank_key, reverse=True)
            elif args.sort == "5m_rvol":
                alerts.sort(key=lambda x: safe_float(x.get("rvol_5m"), 0.0), reverse=True)
                all_rows.sort(key=lambda x: safe_float(x.get("rvol_5m"), 0.0), reverse=True)
            elif args.sort == "1m_rvol":
                alerts.sort(key=lambda x: safe_float(x.get("rvol_1m"), 0.0), reverse=True)
                all_rows.sort(key=lambda x: safe_float(x.get("rvol_1m"), 0.0), reverse=True)
            elif args.sort == "cum_rvol":
                alerts.sort(key=lambda x: safe_float(x.get("rvol_cum"), 0.0), reverse=True)
                all_rows.sort(key=lambda x: safe_float(x.get("rvol_cum"), 0.0), reverse=True)
            elif args.sort == "change":
                alerts.sort(key=lambda x: abs(safe_float(x.get("pct_change"), 0.0)), reverse=True)
                all_rows.sort(key=lambda x: abs(safe_float(x.get("pct_change"), 0.0)), reverse=True)

            # Evaluate 8-Second Fresh Breakout Alerts (< 8.0s execution window)
            now_ts = time.time()
            active_fresh_alerts: List[Dict[str, Any]] = []
            for k, alert_info in list(fresh_breakout_tracker.items()):
                elapsed = now_ts - alert_info["trigger_ts"]
                if elapsed < 8.0:
                    rem_sec = round(max(0.1, 8.0 - elapsed), 1)
                    alert_info["rem_sec"] = rem_sec
                    active_fresh_alerts.append(alert_info)
                else:
                    # After 8s window: demote to confirmed category
                    fresh_breakout_tracker.pop(k, None)
                    downgraded_breakouts[k] = {
                        "ts": now_ts,
                        "stage": alert_info.get("stage", "🚀 BREAKOUT"),
                        "row_data": alert_info.get("row_data", {}),
                    }

            active_fresh_alerts.sort(key=lambda a: safe_int(a.get("row_data", {}).get("breakout_score"), 0), reverse=True)
            fresh_keys = {a["row_data"]["instrument_key"] for a in active_fresh_alerts}

            # Confirmed setups: contains all confirmed breakouts + downgraded breakouts from the fresh alert window
            confirmed_keys = set()
            confirmed_setups: List[Dict[str, Any]] = []

            for r in all_rows:
                if r.get("breakout_stage") in breakout_stages:
                    confirmed_setups.append(r)
                    confirmed_keys.add(r["instrument_key"])

            # Keep downgraded breakouts in confirmed table so they never disappear
            for k, d_info in list(downgraded_breakouts.items()):
                if now_ts - d_info["ts"] > 900.0:  # 15 minutes persistence
                    downgraded_breakouts.pop(k, None)
                    continue
                if k not in confirmed_keys:
                    matching = [r for r in all_rows if r["instrument_key"] == k]
                    if matching:
                        row_copy = dict(matching[0])
                        if row_copy.get("breakout_stage") not in breakout_stages:
                            row_copy["breakout_stage"] = d_info.get("stage", "🚀 BREAKOUT")
                        confirmed_setups.append(row_copy)
                        confirmed_keys.add(k)

            forming_setups = [r for r in all_rows if r.get("breakout_stage") in ("⚡ FORMING", "⚡ CRACKING")]
            flag_setups = [
                r for r in all_rows
                if r.get("breakout_stage") in ("🌀 BULL FLAG", "🚩 BEAR FLAG")
                or r.get("activity_state") in ("🌀 BULL FLAG", "🚩 BEAR FLAG")
            ]

            # Filter if setups_only requested
            if args.setups_only:
                setup_keys = {r["instrument_key"] for r in confirmed_setups + forming_setups + flag_setups}
                setup_keys.update(fresh_keys)
                all_rows = [r for r in all_rows if r["instrument_key"] in setup_keys]
                alerts = [r for r in alerts if r["instrument_key"] in setup_keys]

            # Filter if min_score specified
            if args.min_score > 0:
                all_rows = [r for r in all_rows if safe_int(r.get("breakout_score"), 0) >= args.min_score]
                alerts = [r for r in alerts if safe_int(r.get("breakout_score"), 0) >= args.min_score]
                confirmed_setups = [r for r in confirmed_setups if safe_int(r.get("breakout_score"), 0) >= args.min_score]
                forming_setups = [r for r in forming_setups if safe_int(r.get("breakout_score"), 0) >= args.min_score]
                flag_setups = [r for r in flag_setups if safe_int(r.get("breakout_score"), 0) >= args.min_score]
                active_fresh_alerts = [a for a in active_fresh_alerts if safe_int(a.get("row_data", {}).get("breakout_score"), 0) >= args.min_score]

            # Clear and render dashboard
            clear_screen()
            print_dashboard(
                alerts=alerts,
                confirmed_setups=confirmed_setups,
                forming_setups=forming_setups,
                flag_setups=flag_setups,
                all_rows=all_rows,
                threshold=args.threshold,
                scan_count=scan_count,
                alerts_only=args.alerts_only,
                setups_only=args.setups_only,
                block_mode=args.blocks,
                sort_mode=args.sort,
                rvol_mode=args.rvol_mode,
                direction_filter=args.direction,
                fresh_alerts=active_fresh_alerts,
            )

            # If paper trading enabled, print the live portfolio dashboard below the scanner
            if paper_portfolio:
                print("\n" + paper_portfolio.render_dashboard())

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nScanner stopped by user.")
    finally:
        if session_recorder:
            session_recorder.close()
            print(f"Session data saved to: {os.path.abspath(session_recorder.filepath)}")
        if csv_file:
            csv_file.close()
        if paper_portfolio:
            print("\n" + paper_portfolio.generate_final_report())
            print(f"Trade Journal recorded to: {os.path.abspath(args.paper_csv)}\n")


if __name__ == "__main__":
    main()
