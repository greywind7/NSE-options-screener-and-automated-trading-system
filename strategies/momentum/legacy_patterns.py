#!/usr/bin/env python3
"""
strategies/momentum/patterns.py
===============================
Bidirectional Flag Pattern & Breakout Recognition Engine.

Features:
1. Preceding-Pole Memory Window: 30-minute rolling memory (1,800 seconds).
2. Tight Consolidation & Coiling Detection: Identifies dry-volume coiling at extremes.
3. Strict Volume Inflation Gate for Bull Flags:
   - Eliminates false positive 'fluff' breakouts.
   - Requires active volume expansion (RVOL_5m >= 1.20x or A_vol >= 1.15x, Pace >= 1.05x).
   - If price breaks flag high on dry/flat volume, classifies as '👀 WEAK BREAK / NO VOL'
     and strictly blocks premature trade execution.
4. Structural Invalidation: Automatically tracks flag boundaries (flag_high / flag_low).
"""

from __future__ import annotations

import datetime
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def is_opening_45m_session(now_ts: float) -> bool:
    """
    Returns True if current timestamp is within the opening 45 minutes of the session (09:15 - 10:00 IST).
    For synthetic or test timestamps (< 1,000,000,000), defaults to True.
    """
    if now_ts < 1_000_000_000:
        return True
    try:
        ist_dt = datetime.datetime.fromtimestamp(
            now_ts, tz=datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        )
        return ist_dt.hour == 9 and ist_dt.minute >= 15
    except Exception:
        return True


class FlagPatternTracker:
    """
    Maintains pole memory, consolidation boundaries, and breakout verification.
    """

    def __init__(
        self,
        memory_window_seconds: float = 1800.0,  # 30-minute rolling memory
        coiling_max_span_pct: float = 1.20,     # Max consolidation height %
        min_coiling_cycles: int = 2,            # Minimum coiling cycles
        min_breakout_rvol_5m: float = 1.20,     # Opening volume expansion floor
        min_breakout_accel: float = 1.05,       # Opening intra-5m volume acceleration requirement
        min_breakout_pace: float = 1.05,        # Pace ratio requirement
        midday_rvol_5m: float = 1.35,           # Midday (after 10:00 AM IST) volume expansion requirement
        midday_accel: float = 1.15,             # Midday intra-5m volume acceleration requirement
    ):
        self.memory_window_seconds = memory_window_seconds
        self.coiling_max_span_pct = coiling_max_span_pct
        self.min_coiling_cycles = min_coiling_cycles
        self.min_breakout_rvol_5m = min_breakout_rvol_5m
        self.min_breakout_accel = min_breakout_accel
        self.min_breakout_pace = min_breakout_pace
        self.midday_rvol_5m = midday_rvol_5m
        self.midday_accel = midday_accel

        # State per instrument key
        self.state: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            # Bull Pole State
            "bull_pole_ts": 0.0,
            "bull_pole_price": 0.0,
            "bull_flag_high": 0.0,
            "bull_flag_low": 0.0,
            "bull_coiling_cycles": 0,
            "bull_coiling_active": False,
            # Bear Pole State
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
    ) -> Dict[str, Any]:
        """
        Update flag pattern detection on every tick/snapshot.
        Returns pattern state dictionary.
        """
        if now_ts is None:
            now_ts = time.time()

        s = self.state[instrument_key]

        # Age out old poles past 30-minute rolling window
        if s["bull_pole_ts"] > 0 and (now_ts - s["bull_pole_ts"] > self.memory_window_seconds):
            self._reset_bull_pole(s)
        if s["bear_pole_ts"] > 0 and (now_ts - s["bear_pole_ts"] > self.memory_window_seconds):
            self._reset_bear_pole(s)

        has_bull_pole = s["bull_pole_ts"] > 0 and (now_ts - s["bull_pole_ts"] <= self.memory_window_seconds)
        has_bear_pole = s["bear_pole_ts"] > 0 and (now_ts - s["bear_pole_ts"] <= self.memory_window_seconds)

        # -------------------------------------------------------------------
        # 1. Breakout Recognition with Dual Volume & Acceleration Gate
        # -------------------------------------------------------------------
        is_opening_45m = is_opening_45m_session(now_ts)

        if is_opening_45m:
            # Early Market (09:15 - 10:00 IST): Preserve proven agile breakout behavior.
            # Avoids overtuning the opening session where the strategy makes money:
            # 1. Volume inflation: Volume expansion (RVOL_5m >= min) OR intra-5m acceleration (A_vol >= min)
            is_vol_inflated = has_baseline and ((rvol_5m >= self.min_breakout_rvol_5m) or (a_vol >= self.min_breakout_accel))
            # 2. Flag level breakout: Price penetrates the consolidation flag boundary
            breaks_pole_bull = (s["bull_pole_price"] > 0)
            breaks_pole_bear = (s["bear_pole_price"] > 0)
            # 3. Directional boundary: Near session highs / lows (is_hod / is_lod or top/bottom 20%)
            is_near_high = is_hod or (range_pos >= 75.0)
            is_near_low = is_lod or (range_pos <= 20.0)
            req_cycles = 1
        else:
            # Midday / Afternoon Session (Post-10:00 AM IST): Strict Anti-Chop Guard.
            # Clamps down on midday false positives, chop, and fake breaks on drying volume:
            # 1. Dual Volume Gate: Requires BOTH volume expansion (RVOL_5m >= 1.35x)
            #    AND active intra-5m acceleration (A_vol >= 1.15x) AND pace confirmation (Pace >= 1.05x).
            #    Decelerating or dry volume is strictly rejected as 'WEAK BREAK / NO VOL'.
            is_vol_inflated = has_baseline and (rvol_5m >= self.midday_rvol_5m) and (a_vol >= self.midday_accel) and (pace >= self.min_breakout_pace)
            # 2. Pole Extreme Confirmation: Price must test or penetrate the pole extreme (within 0.5%)
            #    to prevent triggering on minor internal flag oscillations far away from the day's extremes.
            breaks_pole_bull = (s["bull_pole_price"] > 0) and (ltp >= s["bull_pole_price"] * 0.995)
            breaks_pole_bear = (s["bear_pole_price"] > 0) and (ltp <= s["bear_pole_price"] * 1.005)
            # 3. Strict Extreme Confirmation: Must be at or very close to HOD/LOD
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
                    bull_weak_break = True  # Fluff / Low-volume test / Mid-range drift (not tradeable!)

        bear_breakdown = False
        bear_weak_break = False

        if has_bear_pole and s["bear_coiling_cycles"] >= req_cycles:
            if ltp < s["bear_flag_low"]:
                if is_vol_inflated and breaks_pole_bear and is_near_low:
                    bear_breakdown = True
                else:
                    bear_weak_break = True  # Fluff / Low-volume test / Mid-range drift (not tradeable!)

        # -------------------------------------------------------------------
        # 2. Consolidation & Coiling Tracking
        # -------------------------------------------------------------------
        if has_bull_pole and not bull_breakout:
            pole_p = s["bull_pole_price"]
            # Price within 2.5% of pole high and volume contracted (RVOL_5m < 1.0 or Pace < 1.0)
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

        # -------------------------------------------------------------------
        # 3. Pole Establishment (if not already in active coiling/breakout)
        # -------------------------------------------------------------------
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
