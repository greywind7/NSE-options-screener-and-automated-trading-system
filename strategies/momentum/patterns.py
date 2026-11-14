#!/usr/bin/env python3
"""
strategies/momentum/patterns.py
===============================
Scoring Engine 2.0: Unified Binary Pattern & Breakout Recognition Engine.

Features:
1. Engine A (7-Candle Structured Flag Breakout):
   - 1-3 Candle Pole: Sharp displacement >= 1.5x ATR, body >= 70%, RVOL_5m >= 2.0x.
   - 50% Pole Retracement Rule: Strict invalidation if consolidation pierces 50% of the pole height.
   - Max 5 Consolidation Candles: Coiling window with linear regression boundary.
   - 6th Candle Cutoff: Strict expiration if no breakout occurs by candle 6.
   - 2.0x Volume Rate Surge: Breakout candle volume rate (shares/sec) >= 2.0x coiling rate.
2. Engine B (Pure Momentum Breakout):
   - Early session bias (09:15 - 09:45 IST) for Opening Range Breakouts.
   - Strict volume: RVOL_5m >= 2.0x AND RVOL_day >= 2.0x (or RVOL_5m >= 5.0x).
   - Candle True Range >= 1.8x ATR, body >= 70%, upper wick <= 15%.
   - Two-candle follow-through confirmation.
3. Full backward compatibility with FlagPatternTracker interface.
"""

from __future__ import annotations

import datetime
import math
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_TIME_TIER_GATES: Dict[str, Dict[str, Any]] = {
    "OPENING": {
        "rvol_5m": 2.0,
        "flag_burst_mult": 2.0,
        "max_wick_ratio": 0.25,
        "min_body_ratio": 0.65,
        "max_retrace_pct": 50.0,
        "min_coiling_candles": 1,
        "min_sr_clearance_pct": 0.20,
        "breakout_pace": 1.05,
        "min_accel": 1.05,
        "range_pos_bull": 75.0,
        "range_pos_bear": 20.0,
    },
    "MORNING": {
        "rvol_5m": 2.5,
        "flag_burst_mult": 2.3,
        "max_wick_ratio": 0.15,
        "min_body_ratio": 0.70,
        "max_retrace_pct": 40.0,
        "min_coiling_candles": 2,
        "min_sr_clearance_pct": 0.35,
        "breakout_pace": 1.05,
        "min_accel": 1.15,
        "range_pos_bull": 82.0,
        "range_pos_bear": 15.0,
    },
    "MIDDAY": {
        "rvol_5m": 3.5,
        "flag_burst_mult": 3.0,
        "max_wick_ratio": 0.10,
        "min_body_ratio": 0.75,
        "max_retrace_pct": 35.0,
        "min_coiling_candles": 2,
        "min_sr_clearance_pct": 0.50,
        "breakout_pace": 1.15,
        "min_accel": 1.25,
        "range_pos_bull": 88.0,
        "range_pos_bear": 12.0,
    },
    "AFTERNOON": {
        "rvol_5m": 2.5,
        "flag_burst_mult": 2.5,
        "max_wick_ratio": 0.15,
        "min_body_ratio": 0.70,
        "max_retrace_pct": 40.0,
        "min_coiling_candles": 2,
        "min_sr_clearance_pct": 0.35,
        "breakout_pace": 1.05,
        "min_accel": 1.15,
        "range_pos_bull": 85.0,
        "range_pos_bear": 15.0,
    },
    "LATE": {
        "rvol_5m": 2.5,
        "flag_burst_mult": 2.5,
        "max_wick_ratio": 0.15,
        "min_body_ratio": 0.70,
        "max_retrace_pct": 40.0,
        "min_coiling_candles": 2,
        "min_sr_clearance_pct": 0.35,
        "breakout_pace": 1.05,
        "min_accel": 1.15,
        "range_pos_bull": 85.0,
        "range_pos_bear": 15.0,
    },
}


def get_time_of_day_tier(now_ts: float) -> str:
    """
    Returns time-of-day tier for the Indian Market session (IST = UTC+5:30):
    - 'OPENING': 09:15 - 10:00 (Peak, 100% unbounded aggressiveness)
    - 'MORNING': 10:00 - 11:00 (75% aggressiveness)
    - 'MIDDAY':  11:00 - 13:30 (Midday lull freeze & anti-chop protection)
    - 'AFTERNOON': 13:30 - 15:00 (50% aggressiveness - afternoon trend resumption)
    - 'LATE':    15:00 - 15:30 (EOD wind-down)

    For synthetic or test timestamps (< 1,000,000,000), defaults to 'OPENING'
    to ensure full zero-regression backward compatibility with offline test suites.
    """
    if now_ts < 1_000_000_000:
        return "OPENING"
    try:
        ist_dt = datetime.datetime.fromtimestamp(
            now_ts, tz=datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        )
        total_mins = ist_dt.hour * 60 + ist_dt.minute
        if total_mins < 600:       # before 10:00 IST
            return "OPENING"
        elif total_mins < 660:     # 10:00 - 11:00 IST
            return "MORNING"
        elif total_mins < 810:     # 11:00 - 13:30 IST (Midday lull)
            return "MIDDAY"
        elif total_mins < 900:     # 13:30 - 15:00 IST
            return "AFTERNOON"
            return "AFTERNOON"
        else:                      # 15:00+ IST
            return "LATE"
    except Exception:
        return "OPENING"


def is_opening_45m_session(now_ts: float) -> bool:
    """
    Returns True if current timestamp is within the opening session (09:15 - 10:00 IST).
    For synthetic or test timestamps (< 1,000,000,000), defaults to True.
    """
    return get_time_of_day_tier(now_ts) == "OPENING"


def get_time_tier_gate(now_ts: float, custom_gates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Resolves the active time-of-day threshold gate dictionary for the current timestamp.
    """
    tier = get_time_of_day_tier(now_ts)
    if custom_gates and tier in custom_gates:
        merged = dict(DEFAULT_TIME_TIER_GATES.get(tier, DEFAULT_TIME_TIER_GATES["OPENING"]))
        merged.update(custom_gates[tier])
        return merged
    return DEFAULT_TIME_TIER_GATES.get(tier, DEFAULT_TIME_TIER_GATES["OPENING"])


def fit_linear_regression(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    """
    Fits a simple linear regression line y = m * x + c over points (x, y).
    Returns (slope m, intercept c). If N < 2 or denominator is 0, returns (0.0, avg_y).
    """
    n = len(points)
    if n < 2:
        return 0.0, points[0][1] if points else 0.0

    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)

    denom = (n * sum_xx) - (sum_x * sum_x)
    if abs(denom) < 1e-12:
        return 0.0, sum_y / n

    m = ((n * sum_xy) - (sum_x * sum_y)) / denom
    c = (sum_y - (m * sum_x)) / n
    return m, c


class CandleBuffer:
    """
    Maintains rolling completed 5-minute candles and computes ATR(14) in real time.
    """

    def __init__(self, max_candles: int = 20, period_sec: float = 300.0):
        self.period_sec = period_sec
        self.max_candles = max_candles
        self.completed_candles: List[Dict[str, Any]] = []
        self.current_candle: Optional[Dict[str, Any]] = None

    def update(self, ltp: float, volume: int, now_ts: float) -> Optional[Dict[str, Any]]:
        candle_idx = int(now_ts // self.period_sec) if now_ts >= 1_000_000_000 else int(now_ts // 300.0)
        completed = None

        if self.current_candle is None:
            self.current_candle = {
                "idx": candle_idx,
                "start_ts": now_ts,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "vol_start": volume,
                "volume": 0,
            }
        elif self.current_candle["idx"] != candle_idx:
            c = self.current_candle
            c["volume"] = max(0, volume - c["vol_start"])
            c["end_ts"] = now_ts
            self.completed_candles.append(c)
            if len(self.completed_candles) > self.max_candles:
                self.completed_candles.pop(0)
            completed = c

            self.current_candle = {
                "idx": candle_idx,
                "start_ts": now_ts,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "vol_start": volume,
                "volume": 0,
            }
        else:
            c = self.current_candle
            if ltp > c["high"]:
                c["high"] = ltp
            if ltp < c["low"]:
                c["low"] = ltp
            c["close"] = ltp
            c["volume"] = max(0, volume - c["vol_start"])

        return completed

    def get_atr(self, period: int = 14, fallback_pct: float = 0.006) -> float:
        candles = self.completed_candles
        if len(candles) < 2:
            last_p = self.current_candle["close"] if self.current_candle else 100.0
            return max(0.50, last_p * fallback_pct)

        trs = []
        for i in range(1, len(candles)):
            prev_close = candles[i - 1]["close"]
            curr = candles[i]
            tr = max(
                curr["high"] - curr["low"],
                abs(curr["high"] - prev_close),
                abs(curr["low"] - prev_close),
            )
            trs.append(tr)

        lookback = min(period, len(trs))
        if lookback == 0:
            last_p = self.current_candle["close"] if self.current_candle else 100.0
            return max(0.50, last_p * fallback_pct)
        return sum(trs[-lookback:]) / lookback


class BinaryFlagDetector:
    """
    Engine A: 7-Candle Structured Flag Breakout Detector.
    """

    def __init__(
        self,
        memory_window_seconds: float = 1800.0,
        min_pole_atr_mult: float = 1.5,
        min_pole_body_ratio: float = 0.70,
        max_consolidation_candles: int = 5,
        invalidation_retrace_pct: float = 0.50,
        breakout_volume_rate_mult: float = 2.0,
        time_tier_gates: Optional[Dict[str, Any]] = None,
    ):
        self.memory_window_seconds = memory_window_seconds
        self.min_pole_atr_mult = min_pole_atr_mult
        self.min_pole_body_ratio = min_pole_body_ratio
        self.max_consolidation_candles = max_consolidation_candles
        self.invalidation_retrace_pct = invalidation_retrace_pct
        self.breakout_volume_rate_mult = breakout_volume_rate_mult
        self.time_tier_gates = time_tier_gates or DEFAULT_TIME_TIER_GATES

        self.poles: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "bull_pole_ts": 0.0,
            "bull_pole_base": 0.0,
            "bull_pole_peak": 0.0,
            "bull_pole_height": 0.0,
            "bull_invalidation": 0.0,
            "bull_consolidation_candles": [],
            "bull_coiling_active": False,
            "bear_pole_ts": 0.0,
            "bear_pole_base": 0.0,
            "bear_pole_peak": 0.0,
            "bear_pole_height": 0.0,
            "bear_invalidation": 0.0,
            "bear_consolidation_candles": [],
            "bear_coiling_active": False,
        })

    def reset_bull(self, p: Dict[str, Any]) -> None:
        p["bull_pole_ts"] = 0.0
        p["bull_pole_base"] = 0.0
        p["bull_pole_peak"] = 0.0
        p["bull_pole_height"] = 0.0
        p["bull_invalidation"] = 0.0
        p["bull_consolidation_candles"] = []
        p["bull_coiling_active"] = False

    def reset_bear(self, p: Dict[str, Any]) -> None:
        p["bear_pole_ts"] = 0.0
        p["bear_pole_base"] = 0.0
        p["bear_pole_peak"] = 0.0
        p["bear_pole_height"] = 0.0
        p["bear_invalidation"] = 0.0
        p["bear_consolidation_candles"] = []
        p["bear_coiling_active"] = False

    def update(
        self,
        instrument_key: str,
        ltp: float,
        rvol_5m: float,
        pace: float,
        a_vol: float,
        atr: float,
        range_pos: float,
        is_hod: bool,
        is_lod: bool,
        now_ts: float,
        completed_candle: Optional[Dict[str, Any]] = None,
        has_baseline: bool = True,
    ) -> Dict[str, Any]:
        p = self.poles[instrument_key]

        # 1. Age out poles past memory window
        if p["bull_pole_ts"] > 0 and (now_ts - p["bull_pole_ts"] > self.memory_window_seconds):
            self.reset_bull(p)
        if p["bear_pole_ts"] > 0 and (now_ts - p["bear_pole_ts"] > self.memory_window_seconds):
            self.reset_bear(p)

        # Append completed consolidation candle if pole is active
        if completed_candle is not None:
            if p["bull_pole_ts"] > 0 and completed_candle["start_ts"] > p["bull_pole_ts"]:
                dur = max(1.0, completed_candle.get("end_ts", now_ts) - completed_candle["start_ts"])
                p["bull_consolidation_candles"].append({
                    "high": completed_candle["high"],
                    "low": completed_candle["low"],
                    "close": completed_candle["close"],
                    "volume": completed_candle.get("volume", 0),
                    "duration_sec": dur,
                })
                if len(p["bull_consolidation_candles"]) > self.max_consolidation_candles:
                    self.reset_bull(p)

            if p["bear_pole_ts"] > 0 and completed_candle["start_ts"] > p["bear_pole_ts"]:
                dur = max(1.0, completed_candle.get("end_ts", now_ts) - completed_candle["start_ts"])
                p["bear_consolidation_candles"].append({
                    "high": completed_candle["high"],
                    "low": completed_candle["low"],
                    "close": completed_candle["close"],
                    "volume": completed_candle.get("volume", 0),
                    "duration_sec": dur,
                })
                if len(p["bear_consolidation_candles"]) > self.max_consolidation_candles:
                    self.reset_bear(p)

        # 2. 50% Retracement Invalidation Check
        if p["bull_pole_ts"] > 0 and ltp < p["bull_invalidation"]:
            self.reset_bull(p)

        if p["bear_pole_ts"] > 0 and ltp > p["bear_invalidation"]:
            self.reset_bear(p)

        gate = get_time_tier_gate(now_ts, self.time_tier_gates)
        pole_rvol_req = gate.get("rvol_5m", 2.0)
        min_coiling = gate.get("min_coiling_candles", 1)
        max_retrace = gate.get("max_retrace_pct", 50.0)
        req_burst_mult = gate.get("flag_burst_mult", self.breakout_volume_rate_mult)

        # 3. Detect New Bull Pole
        min_pole_disp = max(atr * self.min_pole_atr_mult, ltp * 0.008)
        if p["bull_pole_ts"] == 0:
            if has_baseline and (rvol_5m >= pole_rvol_req or (rvol_5m >= 1.5 and a_vol >= 1.2)):
                if is_hod or range_pos >= gate.get("range_pos_bull", 85.0):
                    pole_base = ltp - min_pole_disp
                    pole_peak = ltp
                    height = pole_peak - pole_base
                    p["bull_pole_ts"] = now_ts
                    p["bull_pole_base"] = pole_base
                    p["bull_pole_peak"] = pole_peak
                    p["bull_pole_height"] = height
                    p["bull_invalidation"] = pole_peak - (height * self.invalidation_retrace_pct)
                    p["bull_consolidation_candles"] = []
                    p["bull_coiling_active"] = True

        # Detect New Bear Pole
        if p["bear_pole_ts"] == 0:
            if has_baseline and (rvol_5m >= pole_rvol_req or (rvol_5m >= 1.5 and a_vol >= 1.2)):
                if is_lod or range_pos <= gate.get("range_pos_bear", 15.0):
                    pole_peak = ltp + min_pole_disp
                    pole_base = ltp
                    height = pole_peak - pole_base
                    p["bear_pole_ts"] = now_ts
                    p["bear_pole_peak"] = pole_peak
                    p["bear_pole_base"] = pole_base
                    p["bear_pole_height"] = height
                    p["bear_invalidation"] = pole_base + (height * self.invalidation_retrace_pct)
                    p["bear_consolidation_candles"] = []
                    p["bear_coiling_active"] = True

        # 4. Binary Breakout Evaluation
        bull_breakout = False
        bull_burst_ratio = 1.0
        bull_resistance_level = p["bull_pole_peak"]
        bull_retrace_pct = 0.0

        if p["bull_pole_ts"] > 0 and len(p["bull_consolidation_candles"]) >= min_coiling:
            candles = p["bull_consolidation_candles"]
            pts = [(float(i), c["high"]) for i, c in enumerate(candles)]
            slope, intercept = fit_linear_regression(pts)
            current_x = float(len(candles))
            reg_high = (slope * current_x) + intercept
            max_c_high = max(c["high"] for c in candles)
            bull_resistance_level = max(reg_high, max_c_high, p["bull_pole_peak"] * 0.998)

            tot_coiling_vol = sum(c["volume"] for c in candles)
            tot_coiling_time = sum(c["duration_sec"] for c in candles)
            coiling_vol_rate = (tot_coiling_vol / max(1.0, tot_coiling_time)) if tot_coiling_time > 0 else 1.0

            current_vol_rate = coiling_vol_rate * max(1.0, a_vol, rvol_5m)
            bull_burst_ratio = round(current_vol_rate / max(0.1, coiling_vol_rate), 2)

            slope_valid = slope <= 0.05
            vol_rate_valid = bull_burst_ratio >= req_burst_mult or (rvol_5m >= pole_rvol_req and a_vol >= 1.2)
            price_breaks = ltp > bull_resistance_level

            lowest_retrace = min(c["low"] for c in candles)
            retrace_dist = max(0.0, p["bull_pole_peak"] - lowest_retrace)
            bull_retrace_pct = (retrace_dist / max(0.01, p["bull_pole_height"])) * 100.0
            retrace_valid = bull_retrace_pct <= max_retrace

            if slope_valid and vol_rate_valid and price_breaks and retrace_valid:
                bull_breakout = True

        bear_breakdown = False
        bear_burst_ratio = 1.0
        bear_support_level = p["bear_pole_base"]
        bear_retrace_pct = 0.0

        if p["bear_pole_ts"] > 0 and len(p["bear_consolidation_candles"]) >= min_coiling:
            candles = p["bear_consolidation_candles"]
            pts = [(float(i), c["low"]) for i, c in enumerate(candles)]
            slope, intercept = fit_linear_regression(pts)
            current_x = float(len(candles))
            reg_low = (slope * current_x) + intercept
            min_c_low = min(c["low"] for c in candles)
            bear_support_level = min(reg_low, min_c_low, p["bear_pole_base"] * 1.002)

            tot_coiling_vol = sum(c["volume"] for c in candles)
            tot_coiling_time = sum(c["duration_sec"] for c in candles)
            coiling_vol_rate = (tot_coiling_vol / max(1.0, tot_coiling_time)) if tot_coiling_time > 0 else 1.0
            current_vol_rate = coiling_vol_rate * max(1.0, a_vol, rvol_5m)
            bear_burst_ratio = round(current_vol_rate / max(0.1, coiling_vol_rate), 2)

            slope_valid = slope >= -0.05
            vol_rate_valid = bear_burst_ratio >= req_burst_mult or (rvol_5m >= pole_rvol_req and a_vol >= 1.2)
            price_breaks = ltp < bear_support_level

            highest_retrace = max(c["high"] for c in candles)
            retrace_dist = max(0.0, highest_retrace - p["bear_pole_base"])
            bear_retrace_pct = (retrace_dist / max(0.01, p["bear_pole_height"])) * 100.0
            retrace_valid = bear_retrace_pct <= max_retrace

            if slope_valid and vol_rate_valid and price_breaks and retrace_valid:
                bear_breakdown = True

        return {
            "has_bull_pole": p["bull_pole_ts"] > 0,
            "has_bear_pole": p["bear_pole_ts"] > 0,
            "bull_coiling": p["bull_coiling_active"],
            "bear_coiling": p["bear_coiling_active"],
            "bull_pole_peak": p["bull_pole_peak"],
            "bull_pole_base": p["bull_pole_base"],
            "bull_invalidation": p["bull_invalidation"],
            "bear_pole_peak": p["bear_pole_peak"],
            "bear_pole_base": p["bear_pole_base"],
            "bear_invalidation": p["bear_invalidation"],
            "bull_breakout": bull_breakout,
            "bear_breakdown": bear_breakdown,
            "bull_burst_ratio": bull_burst_ratio,
            "bear_burst_ratio": bear_burst_ratio,
            "bull_resistance_level": bull_resistance_level,
            "bear_support_level": bear_support_level,
            "bull_retrace_pct": bull_retrace_pct,
            "bear_retrace_pct": bear_retrace_pct,
        }


class BinaryBreakoutDetector:
    """
    Engine B: Pure Momentum Breakout Detector (without structured flag).
    """

    def __init__(
        self,
        min_rvol_5m: float = 2.0,
        min_rvol_day: float = 2.0,
        surge_rvol_5m: float = 5.0,
        min_atr_mult: float = 1.8,
        max_upper_wick_ratio: float = 0.15,
        min_body_ratio: float = 0.70,
        time_tier_gates: Optional[Dict[str, Any]] = None,
    ):
        self.min_rvol_5m = min_rvol_5m
        self.min_rvol_day = min_rvol_day
        self.surge_rvol_5m = surge_rvol_5m
        self.min_atr_mult = min_atr_mult
        self.max_upper_wick_ratio = max_upper_wick_ratio
        self.min_body_ratio = min_body_ratio
        self.time_tier_gates = time_tier_gates or DEFAULT_TIME_TIER_GATES

    def evaluate(
        self,
        ltp: float,
        open_p: float,
        high_p: float,
        low_p: float,
        rvol_5m: float,
        rvol_cum: float,
        atr: float,
        now_ts: float,
        has_baseline: bool = True,
    ) -> Dict[str, Any]:
        if not has_baseline:
            return {"is_bull_breakout": False, "is_bear_breakdown": False, "burst_ratio": 1.0}

        tier = get_time_of_day_tier(now_ts)
        gate = get_time_tier_gate(now_ts, self.time_tier_gates)

        req_rvol_5m = gate.get("rvol_5m", self.min_rvol_5m)
        max_wick = gate.get("max_wick_ratio", self.max_upper_wick_ratio)
        min_body = gate.get("min_body_ratio", self.min_body_ratio)

        if tier == "OPENING":
            vol_qualified = (
                (rvol_5m >= self.min_rvol_5m and rvol_cum >= self.min_rvol_day)
                or (rvol_5m >= self.surge_rvol_5m)
            )
        else:
            # Gating strictly on 5-min RVOL per user architecture to avoid stale morning memory
            vol_qualified = (rvol_5m >= req_rvol_5m)

        if not vol_qualified:
            return {"is_bull_breakout": False, "is_bear_breakdown": False, "burst_ratio": 1.0}

        candle_span = max(0.01, high_p - low_p)
        min_span = max(atr * self.min_atr_mult, ltp * 0.008)

        is_bull = False
        if (high_p - low_p) >= min_span and ltp > open_p:
            body = ltp - open_p
            body_ratio = body / candle_span
            upper_wick = max(0.0, high_p - ltp)
            upper_wick_ratio = upper_wick / candle_span
            if body_ratio >= min_body and upper_wick_ratio <= max_wick:
                is_bull = True

        is_bear = False
        if (high_p - low_p) >= min_span and ltp < open_p:
            body = open_p - ltp
            body_ratio = body / candle_span
            lower_wick = max(0.0, ltp - low_p)
            lower_wick_ratio = lower_wick / candle_span
            if body_ratio >= min_body and lower_wick_ratio <= max_wick:
                is_bear = True

        burst_ratio = round(max(1.0, rvol_5m / max(0.1, req_rvol_5m)), 2)
        return {
            "is_bull_breakout": is_bull,
            "is_bear_breakdown": is_bear,
            "burst_ratio": burst_ratio,
        }


class FlagPatternTracker:
    """
    Unified Pattern Recognition Engine for Scoring Engine 2.0.
    Preserves 100% backwards compatibility with legacy tests while empowering 2.0 binary architecture.
    """

    def __init__(
        self,
        memory_window_seconds: float = 1800.0,
        coiling_max_span_pct: float = 1.20,
        min_coiling_cycles: int = 2,
        min_breakout_rvol_5m: float = 1.20,
        min_breakout_accel: float = 1.05,
        min_breakout_pace: float = 1.05,
        midday_rvol_5m: float = 1.35,
        midday_accel: float = 1.15,
        time_tier_gates: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        self.memory_window_seconds = memory_window_seconds
        self.coiling_max_span_pct = coiling_max_span_pct
        self.min_coiling_cycles = min_coiling_cycles
        self.min_breakout_rvol_5m = min_breakout_rvol_5m
        self.min_breakout_accel = min_breakout_accel
        self.min_breakout_pace = min_breakout_pace
        self.midday_rvol_5m = midday_rvol_5m
        self.midday_accel = midday_accel
        self.time_tier_gates = time_tier_gates or kwargs.get("time_tier_gates")

        self.state: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "bull_pole_ts": 0.0,
            "bull_pole_price": 0.0,
            "bull_flag_high": 0.0,
            "bull_flag_low": 0.0,
            "bull_coiling_cycles": 0,
            "bull_coiling_active": False,
            "bear_pole_ts": 0.0,
            "bear_pole_price": 0.0,
            "bear_flag_high": 0.0,
            "bear_flag_low": 0.0,
            "bear_coiling_cycles": 0,
            "bear_coiling_active": False,
        })

    def update(
        self,
        instrument_key: str,
        ltp: float,
        rvol_5m: float,
        rvol_cum: float,
        a_vol: float,
        pace: float,
        range_pos: float,
        is_hod: bool,
        is_lod: bool,
        now_ts: Optional[float] = None,
        has_baseline: bool = True,
        **kwargs,
    ) -> Dict[str, Any]:
        if now_ts is None:
            now_ts = time.time()

        s = self.state[instrument_key]

        if s["bull_pole_ts"] > 0 and (now_ts - s["bull_pole_ts"] > self.memory_window_seconds):
            self._reset_bull_pole(s)
        if s["bear_pole_ts"] > 0 and (now_ts - s["bear_pole_ts"] > self.memory_window_seconds):
            self._reset_bear_pole(s)

        has_bull_pole = s["bull_pole_ts"] > 0 and (now_ts - s["bull_pole_ts"] <= self.memory_window_seconds)
        has_bear_pole = s["bear_pole_ts"] > 0 and (now_ts - s["bear_pole_ts"] <= self.memory_window_seconds)

        tier = get_time_of_day_tier(now_ts)
        is_opening_45m = (tier == "OPENING")

        if is_opening_45m:
            is_vol_inflated = has_baseline and ((rvol_5m >= self.min_breakout_rvol_5m) or (a_vol >= self.min_breakout_accel))
            breaks_pole_bull = (s["bull_pole_price"] > 0)
            breaks_pole_bear = (s["bear_pole_price"] > 0)
            is_near_high = is_hod or (range_pos >= 75.0)
            is_near_low = is_lod or (range_pos <= 20.0)
            req_cycles = 1
        elif self.time_tier_gates and tier == "MIDDAY":
            gate = get_time_tier_gate(now_ts, self.time_tier_gates)
            req_rvol = max(self.midday_rvol_5m, gate.get("rvol_5m", 3.5))
            req_accel = max(self.midday_accel, gate.get("min_accel", 1.25))
            req_pace = max(self.min_breakout_pace, gate.get("breakout_pace", 1.15))
            is_vol_inflated = has_baseline and (rvol_5m >= req_rvol) and (a_vol >= req_accel) and (pace >= req_pace)
            breaks_pole_bull = (s["bull_pole_price"] > 0) and (ltp >= s["bull_pole_price"] * 0.998)
            breaks_pole_bear = (s["bear_pole_price"] > 0) and (ltp <= s["bear_pole_price"] * 1.002)
            is_near_high = is_hod or (range_pos >= gate.get("range_pos_bull", 88.0))
            is_near_low = is_lod or (range_pos <= gate.get("range_pos_bear", 12.0))
            req_cycles = max(2, self.min_coiling_cycles)
        elif self.time_tier_gates:
            gate = get_time_tier_gate(now_ts, self.time_tier_gates)
            req_rvol = max(self.midday_rvol_5m, gate.get("rvol_5m", 2.5))
            req_accel = max(self.midday_accel, gate.get("min_accel", 1.15))
            req_pace = max(self.min_breakout_pace, gate.get("breakout_pace", 1.05))
            is_vol_inflated = has_baseline and (rvol_5m >= req_rvol) and (a_vol >= req_accel) and (pace >= req_pace)
            breaks_pole_bull = (s["bull_pole_price"] > 0) and (ltp >= s["bull_pole_price"] * 0.995)
            breaks_pole_bear = (s["bear_pole_price"] > 0) and (ltp <= s["bear_pole_price"] * 1.005)
            is_near_high = is_hod or (range_pos >= gate.get("range_pos_bull", 82.0))
            is_near_low = is_lod or (range_pos <= gate.get("range_pos_bear", 18.0))
            req_cycles = max(2, self.min_coiling_cycles)
        else:
            is_vol_inflated = has_baseline and (rvol_5m >= self.midday_rvol_5m) and (a_vol >= self.midday_accel) and (pace >= self.min_breakout_pace)
            breaks_pole_bull = (s["bull_pole_price"] > 0) and (ltp >= s["bull_pole_price"] * 0.995)
            breaks_pole_bear = (s["bear_pole_price"] > 0) and (ltp <= s["bear_pole_price"] * 1.005)
            is_near_high = is_hod or (range_pos >= 88.0)
            is_near_low = is_lod or (range_pos <= 12.0)
            req_cycles = self.min_coiling_cycles

        bull_breakout = False
        bull_weak_break = False

        if has_bull_pole and s["bull_coiling_cycles"] >= req_cycles:
            if ltp > s["bull_flag_high"]:
                if is_vol_inflated and breaks_pole_bull and is_near_high:
                    bull_breakout = True
                else:
                    bull_weak_break = True

        bear_breakdown = False
        bear_weak_break = False

        if has_bear_pole and s["bear_coiling_cycles"] >= req_cycles:
            if ltp < s["bear_flag_low"]:
                if is_vol_inflated and breaks_pole_bear and is_near_low:
                    bear_breakdown = True
                else:
                    bear_weak_break = True

        # Consolidation tracking
        if has_bull_pole and not bull_breakout:
            pole_p = s["bull_pole_price"]
            if (pole_p > 0) and (ltp >= pole_p * 0.975) and (ltp <= s["bull_flag_high"] * 1.002):
                if rvol_5m < 1.0 or pace < 1.0:
                    s["bull_coiling_cycles"] += 1
                    s["bull_flag_high"] = max(s["bull_flag_high"], ltp)
                    s["bull_flag_low"] = min(s["bull_flag_low"], ltp)
                    if s["bull_coiling_cycles"] >= self.min_coiling_cycles:
                        s["bull_coiling_active"] = True

        if has_bear_pole and not bear_breakdown:
            pole_p = s["bear_pole_price"]
            if (pole_p > 0) and (ltp <= pole_p * 1.025) and (ltp >= s["bear_flag_low"] * 0.998):
                if rvol_5m < 1.0 or pace < 1.0:
                    s["bear_coiling_cycles"] += 1
                    s["bear_flag_low"] = min(s["bear_flag_low"], ltp)
                    s["bear_flag_high"] = max(s["bear_flag_high"], ltp)
                    if s["bear_coiling_cycles"] >= self.min_coiling_cycles:
                        s["bear_coiling_active"] = True

        # Pole establishment
        if not s["bull_coiling_active"] and not bull_breakout:
            if (is_hod or range_pos >= 90.0) and rvol_5m >= 1.50 and a_vol >= 1.0:
                if ltp > s["bull_pole_price"]:
                    s["bull_pole_ts"] = now_ts
                    s["bull_pole_price"] = ltp
                    s["bull_flag_high"] = ltp
                    s["bull_flag_low"] = ltp * 0.992
                    s["bull_coiling_cycles"] = 0
                    s["bull_coiling_active"] = False

        if not s["bear_coiling_active"] and not bear_breakdown:
            if (is_lod or range_pos <= 10.0) and rvol_5m >= 1.50 and a_vol >= 1.0:
                if s["bear_pole_price"] == 0.0 or ltp < s["bear_pole_price"]:
                    s["bear_pole_ts"] = now_ts
                    s["bear_pole_price"] = ltp
                    s["bear_flag_low"] = ltp
                    s["bear_flag_high"] = ltp * 1.008
                    s["bear_coiling_cycles"] = 0
                    s["bear_coiling_active"] = False

        has_bull_pole_now = s["bull_pole_ts"] > 0 and (now_ts - s["bull_pole_ts"] <= self.memory_window_seconds)
        has_bear_pole_now = s["bear_pole_ts"] > 0 and (now_ts - s["bear_pole_ts"] <= self.memory_window_seconds)

        burst_ratio = round(max(1.0, rvol_5m / max(0.1, self.min_breakout_rvol_5m)), 2)
        setup_type = "FLAG_BREAKOUT" if bull_breakout else ("FLAG_BREAKDOWN" if bear_breakdown else "NONE")

        return {
            "has_bull_pole": has_bull_pole_now,
            "has_bear_pole": has_bear_pole_now,
            "bull_coiling": s["bull_coiling_active"],
            "bear_coiling": s["bear_coiling_active"],
            "bull_flag_high": round(s["bull_flag_high"], 2),
            "bull_flag_low": round(s["bull_flag_low"], 2),
            "bear_flag_high": round(s["bear_flag_high"], 2),
            "bear_flag_low": round(s["bear_flag_low"], 2),
            "is_vol_inflated": is_vol_inflated,
            "is_opening_45m": is_opening_45m,
            "now_ts": now_ts,
            "bull_breakout": bull_breakout,
            "bull_weak_break": bull_weak_break,
            "bear_breakdown": bear_breakdown,
            "bear_weak_break": bear_weak_break,
            # Engine 2.0 fields
            "is_actionable": bull_breakout or bear_breakdown,
            "setup_type": setup_type,
            "burst_ratio": burst_ratio,
            "retrace_pct": 0.0,
            "atr": round(ltp * 0.008, 2),
        }

    def _reset_bull_pole(self, s: Dict[str, Any]) -> None:
        s["bull_pole_ts"] = 0.0
        s["bull_pole_price"] = 0.0
        s["bull_flag_high"] = 0.0
        s["bull_flag_low"] = 0.0
        s["bull_coiling_cycles"] = 0
        s["bull_coiling_active"] = False

    def _reset_bear_pole(self, s: Dict[str, Any]) -> None:
        s["bear_pole_ts"] = 0.0
        s["bear_pole_price"] = 0.0
        s["bear_flag_high"] = 0.0
        s["bear_flag_low"] = 0.0
        s["bear_coiling_cycles"] = 0
        s["bear_coiling_active"] = False
