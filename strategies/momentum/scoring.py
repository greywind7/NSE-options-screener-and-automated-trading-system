#!/usr/bin/env python3
"""
strategies/momentum/scoring.py
==============================
Scoring Engine 2.0: Dual-Scoring Engine with Truly Unclamped Absolute Alpha.

Features:
1. Relative Setup Score (S_rel in [0, 100]): Bounded continuous 5-pillar score for screening.
2. Unbounded Absolute Score (S_abs): Convex alpha score calculated after screening to capture
   exceptional events (massive RVOL, explosive volume acceleration, extreme tick thrust,
   and macro Nifty regime alignment).
3. UnclampedAlphaScorer: Zero-clamp alpha calculator for Engine 2.0 binary setups.
4. Continuous Exponential Score Decay: Helper for 20-second actionable pool ranking.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def compute_decayed_score(raw_score: float, trigger_ts: float, now_ts: float, max_age_sec: float = 20.0) -> float:
    """
    Computes real-time decayed score using power/exponential decay:
    S_decayed = S_0 * max(0.0, 1.0 - (age / max_age)^1.5)
    """
    age = max(0.0, now_ts - trigger_ts)
    if age >= max_age_sec:
        return 0.0
    factor = max(0.0, 1.0 - math.pow(age / max_age_sec, 1.5))
    return round(raw_score * factor, 2)


class UnclampedAlphaScorer:
    """
    Engine 2.0: Purely unconstrained absolute alpha scoring engine without arbitrary min() ceilings.
    """

    def __init__(
        self,
        vol_burst_mult: float = 15.0,
        pure_rvol_mult: float = 12.0,
        rvol_day_mult: float = 5.0,
        z_vol_mult: float = 3.0,
        sr_clearance_mult: float = 8.0,
        sr_proximity_mult: float = 20.0,
        max_sr_proximity_pts: float = 25.0,
        shallow_retrace_pts: float = 25.0,
        spread_penalty_mult: float = 10.0,
        bear_vol_multiplier: float = 1.25,
    ):
        self.vol_burst_mult = vol_burst_mult
        self.pure_rvol_mult = pure_rvol_mult
        self.rvol_day_mult = rvol_day_mult
        self.z_vol_mult = z_vol_mult
        self.sr_clearance_mult = sr_clearance_mult
        self.sr_proximity_mult = sr_proximity_mult
        self.max_sr_proximity_pts = max_sr_proximity_pts
        self.shallow_retrace_pts = shallow_retrace_pts
        self.spread_penalty_mult = spread_penalty_mult
        self.bear_vol_multiplier = bear_vol_multiplier

    def calculate_alpha(
        self,
        ltp: float,
        direction: str,
        setup_type: str,
        burst_ratio: float,
        rvol_5m: float,
        rvol_cum: float,
        z_vol: float,
        retrace_pct: float,
        tech_indicators: Optional[Dict[str, Any]] = None,
        nifty_stats: Optional[Dict[str, Any]] = None,
        option_spread_pct: float = 0.5,
        has_baseline: bool = True,
    ) -> Dict[str, Any]:
        tech = tech_indicators or {}
        nifty = nifty_stats or {}

        is_bear = direction == "BEAR"
        vol_mult = self.bear_vol_multiplier if is_bear else 1.0

        if setup_type in ("FLAG_BREAKOUT", "FLAG_BREAKDOWN"):
            s_vol_burst = self.vol_burst_mult * max(1.0, burst_ratio) * vol_mult
        else:
            s_vol_burst = self.pure_rvol_mult * max(1.0, rvol_5m) * vol_mult

        s_rvol_base = 0.0
        if has_baseline:
            s_rvol_base = (self.rvol_day_mult * max(0.0, rvol_cum)) + (self.z_vol_mult * max(0.0, z_vol))

        if direction == "BULL":
            clearance_pct = float(tech.get("clearance_pct", 0.0))
        else:
            clearance_pct = float(tech.get("clearance_down_pct", 0.0))
        s_clearance = max(0.0, clearance_pct * self.sr_clearance_mult)

        extension_pct = float(tech.get("extension_pct", 0.05))
        s_proximity = max(0.0, self.max_sr_proximity_pts - (extension_pct * self.sr_proximity_mult))

        s_retrace = 0.0
        if setup_type in ("FLAG_BREAKOUT", "FLAG_BREAKDOWN"):
            if retrace_pct < 50.0:
                s_retrace = (1.0 - (retrace_pct / 50.0)) * self.shallow_retrace_pts

        s_spread = -(max(0.0, option_spread_pct) * self.spread_penalty_mult)

        nifty_regime = nifty.get("regime", "NEUTRAL")
        s_regime = 0.0
        if nifty_regime == "PRO_BULL":
            s_regime = 10.0 if direction == "BULL" else -25.0
        elif nifty_regime == "PRO_BEAR":
            s_regime = 10.0 if direction == "BEAR" else -25.0

        total_alpha = s_vol_burst + s_rvol_base + s_clearance + s_proximity + s_retrace + s_spread + s_regime
        total_alpha = round(max(0.0, total_alpha), 1)

        return {
            "absolute_score": total_alpha,
            "components": {
                "vol_burst": round(s_vol_burst, 1),
                "rvol_base": round(s_rvol_base, 1),
                "clearance": round(s_clearance, 1),
                "proximity": round(s_proximity, 1),
                "retrace": round(s_retrace, 1),
                "spread_penalty": round(s_spread, 1),
                "regime": round(s_regime, 1),
            },
        }


def calculate_setup_score(
    ltp: float,
    high: float,
    low: float,
    close: float,
    volume_stats: Dict[str, Any],
    tech_indicators: Optional[Dict[str, Any]] = None,
    pattern_state: Optional[Dict[str, Any]] = None,
    nifty_stats: Optional[Dict[str, Any]] = None,
    recent_ticks: Optional[List[float]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Computes both the 100-Point Relative Setup Score (S_rel) and Unbounded Absolute Score (S_abs).
    """
    if tech_indicators is None:
        tech_indicators = {}
    if pattern_state is None:
        pattern_state = {}
    if nifty_stats is None:
        nifty_stats = {}
    if recent_ticks is None:
        recent_ticks = []

    rvol_5m = float(volume_stats.get("rvol_5m", 0.0))
    rvol_cum = float(volume_stats.get("rvol_cum", 0.0))
    a_vol = float(volume_stats.get("a_vol", 0.0))
    pace = float(volume_stats.get("pace", 0.0))
    z_vol = float(volume_stats.get("z_vol", 0.0))
    has_baseline = bool(volume_stats.get("has_baseline", True))

    # Day Range & Position
    day_span = max(0.01, high - low)
    eff_span = max(ltp * 0.005, day_span)
    range_pos = max(0.0, min(100.0, ((ltp - low) / day_span) * 100.0)) if day_span > 0 else 50.0

    # Net day change and 5m impulse
    net_chg = ltp - close if close > 0 else 0.0
    pct_chg = (net_chg / close) * 100.0 if close > 0 else 0.0
    p_5m_delta = float(volume_stats.get("p_5m_delta", 0.0))
    p_5m_pct = (p_5m_delta / (ltp - p_5m_delta)) * 100.0 if (ltp - p_5m_delta) > 0 else 0.0

    # Configuration extraction
    cfg = config or {}
    sc_cfg = cfg.get("scoring", cfg)
    gate_cfg = cfg.get("volume_inflation_gate", {})
    regime_cfg = cfg.get("market_regime", {})

    p1_max = float(sc_cfg.get("pillar1_volume_rvol_weight", 25.0))
    p1_rvol_min = float(sc_cfg.get("pillar1_rvol_min", 0.60))
    p1_rvol_max = float(sc_cfg.get("pillar1_rvol_max", 2.50))
    p1_rvol_pts = float(sc_cfg.get("pillar1_rvol_pts", 15.0))
    p1_pace_min = float(sc_cfg.get("pillar1_pace_min", 0.80))
    p1_pace_max = float(sc_cfg.get("pillar1_pace_max", 2.00))
    p1_pace_pts = float(sc_cfg.get("pillar1_pace_pts", 6.0))
    p1_zvol_min = float(sc_cfg.get("pillar1_zvol_min", 0.20))
    p1_zvol_max = float(sc_cfg.get("pillar1_zvol_max", 2.00))
    p1_zvol_pts = float(sc_cfg.get("pillar1_zvol_pts", 4.0))

    p2_max = float(sc_cfg.get("pillar2_consolidation_tightness_weight", 20.0))
    p2_span_max = float(sc_cfg.get("pillar2_coiling_span_max_pct", 2.5))
    p2_span_pts = float(sc_cfg.get("pillar2_span_pts", 12.0))
    p2_coil_bonus = float(sc_cfg.get("pillar2_coiling_pattern_bonus", 8.0))

    p3_max = float(sc_cfg.get("pillar3_directional_momentum_weight", 30.0))
    p3_chg_tgt = float(sc_cfg.get("pillar3_pct_chg_target", 3.0))
    p3_chg_pts = float(sc_cfg.get("pillar3_pct_chg_pts", 10.0))
    p3_impulse_tgt = float(sc_cfg.get("pillar3_impulse_5m_target_pct", 0.50))
    p3_impulse_pts = float(sc_cfg.get("pillar3_impulse_5m_pts", 20.0))

    p4_max = float(sc_cfg.get("pillar4_position_freshness_weight", 15.0))
    p5_max = float(sc_cfg.get("pillar5_micro_thrust_weight", 10.0))
    p5_thrust_eff = float(sc_cfg.get("pillar5_thrust_eff_span_pct", 0.15))

    # Pillar 1: Volume & Participation
    rvol_pts = 0.0
    pace_pts = 0.0
    z_pts = 0.0
    if has_baseline:
        if rvol_5m > p1_rvol_min and p1_rvol_max > p1_rvol_min:
            rvol_pts = min(p1_rvol_pts, ((rvol_5m - p1_rvol_min) / (p1_rvol_max - p1_rvol_min)) * p1_rvol_pts)
        if pace > p1_pace_min and p1_pace_max > p1_pace_min:
            pace_pts = min(p1_pace_pts, ((pace - p1_pace_min) / (p1_pace_max - p1_pace_min)) * p1_pace_pts)
        if z_vol > p1_zvol_min and p1_zvol_max > p1_zvol_min:
            z_pts = min(p1_zvol_pts, ((z_vol - p1_zvol_min) / (p1_zvol_max - p1_zvol_min)) * p1_zvol_pts)

    p1_volume = min(p1_max, rvol_pts + pace_pts + z_pts)

    # Pillar 2: Price Consolidation & Tightness
    p2_consolidation = 0.0
    coiling_span_pct = (day_span / ltp) * 100.0 if ltp > 0 else 0.0
    if coiling_span_pct <= p2_span_max and p2_span_max > 0:
        p2_consolidation += min(p2_span_pts, (1.0 - (coiling_span_pct / p2_span_max)) * p2_span_pts)
    if pattern_state.get("bull_coiling") or pattern_state.get("bear_coiling"):
        p2_consolidation += p2_coil_bonus
    p2_consolidation = min(p2_max, p2_consolidation)

    # Pillar 3: Directional Momentum
    bull_mom = 0.0
    if pct_chg > 0 and p_5m_pct >= 0:
        t1 = min(p3_chg_pts, (pct_chg / p3_chg_tgt) * p3_chg_pts) if p3_chg_tgt > 0 else 0.0
        t2 = min(p3_impulse_pts, (p_5m_pct / p3_impulse_tgt) * p3_impulse_pts) if p3_impulse_tgt > 0 else 0.0
        bull_mom = min(p3_max, t1 + t2)

    bear_mom = 0.0
    if pct_chg < 0 and p_5m_pct <= 0:
        t1 = min(p3_chg_pts, (abs(pct_chg) / p3_chg_tgt) * p3_chg_pts) if p3_chg_tgt > 0 else 0.0
        t2 = min(p3_impulse_pts, (abs(p_5m_pct) / p3_impulse_tgt) * p3_impulse_pts) if p3_impulse_tgt > 0 else 0.0
        bear_mom = min(p3_max, t1 + t2)

    # Pillar 4: Position & Freshness
    bull_pos = min(p4_max, (range_pos / 100.0) * p4_max)
    bear_pos = min(p4_max, ((100.0 - range_pos) / 100.0) * p4_max)

    # Pillar 5: Micro-Tick Velocity & Thrust
    thrust_pts = 0.0
    thrust_velocity = 0.0
    eff_thresh = eff_span * p5_thrust_eff
    if len(recent_ticks) >= 3:
        t1, t2, t3 = recent_ticks[-3], recent_ticks[-2], recent_ticks[-1]
        delta_tick = t3 - t1
        thrust_velocity = abs(delta_tick) / eff_span
        if (t3 > t2 or t2 > t1) and delta_tick > 0:
            thrust_pts = min(p5_max, (delta_tick / eff_thresh) * p5_max) if eff_thresh > 0 else 0.0
        elif (t3 < t2 or t2 < t1) and delta_tick < 0:
            thrust_pts = min(p5_max, (abs(delta_tick) / eff_thresh) * p5_max) if eff_thresh > 0 else 0.0

    raw_bull = p1_volume + p2_consolidation + bull_mom + bull_pos + thrust_pts
    raw_bear = p1_volume + p2_consolidation + bear_mom + bear_pos + thrust_pts

    # Pattern bonuses and clamps
    is_vol_inflated = pattern_state.get("is_vol_inflated", False)
    bull_breakout = pattern_state.get("bull_breakout", False)
    bull_weak_break = pattern_state.get("bull_weak_break", False)
    bear_breakdown = pattern_state.get("bear_breakdown", False)
    bear_weak_break = pattern_state.get("bear_weak_break", False)

    breakout_bonus = float(sc_cfg.get("breakout_bonus", 15.0))
    breakout_min_score = float(sc_cfg.get("breakout_min_score", 70.0))
    bull_breakout_range_min = float(sc_cfg.get("bull_breakout_range_pos_min", 75.0))
    bear_breakdown_range_max = float(sc_cfg.get("bear_breakdown_range_pos_max", 25.0))
    fluff_dock = float(gate_cfg.get("fluff_break_dock", 15.0))
    fluff_ceiling = float(gate_cfg.get("fluff_break_ceiling", 60.0))
    coiling_bonus = float(sc_cfg.get("coiling_bonus", 5.0))
    coiling_min_score = float(sc_cfg.get("coiling_min_score", 50.0))
    breakout_pos_min = float(sc_cfg.get("breakout_range_pos_min", 90.0))
    forming_pos_min = float(sc_cfg.get("forming_range_pos_min", 80.0))
    breakdown_pos_max = float(sc_cfg.get("breakdown_range_pos_max", 10.0))
    cracking_pos_max = float(sc_cfg.get("cracking_range_pos_max", 20.0))

    bull_stage = "NORMAL"
    bear_stage = "NORMAL"

    if bull_breakout and range_pos >= bull_breakout_range_min:
        raw_bull = min(100.0, max(breakout_min_score, raw_bull + breakout_bonus))
        bull_stage = "💥 BULL BREAKOUT"
    elif bull_weak_break or (bull_breakout and range_pos < bull_breakout_range_min):
        raw_bull = min(fluff_ceiling, max(0.0, raw_bull - fluff_dock))
        bull_stage = "👀 WEAK BREAK / NO VOL"
    elif pattern_state.get("bull_coiling"):
        raw_bull = min(100.0, max(coiling_min_score, raw_bull + coiling_bonus))
        bull_stage = "🌀 BULL FLAG"
    elif range_pos >= breakout_pos_min and rvol_5m >= 1.5:
        bull_stage = "🚀 BREAKOUT"
    elif range_pos >= forming_pos_min:
        bull_stage = "⚡ FORMING"

    if bear_breakdown and range_pos <= bear_breakdown_range_max:
        raw_bear = min(100.0, max(breakout_min_score, raw_bear + breakout_bonus))
        bear_stage = "💥 BEAR BREAKDOWN"
    elif bear_weak_break or (bear_breakdown and range_pos > bear_breakdown_range_max):
        raw_bear = min(fluff_ceiling, max(0.0, raw_bear - fluff_dock))
        bear_stage = "👀 WEAK CRACK / NO VOL"
    elif pattern_state.get("bear_coiling"):
        raw_bear = min(100.0, max(coiling_min_score, raw_bear + coiling_bonus))
        bear_stage = "🌀 BEAR FLAG"
    elif range_pos <= breakdown_pos_max and rvol_5m >= 1.5:
        bear_stage = "🩸 BREAKDOWN"
    elif range_pos <= cracking_pos_max:
        bear_stage = "⚡ CRACKING"

    # Nifty market regime bias
    regime_thresh = float(regime_cfg.get("bias_threshold_pct", 0.25))
    pro_bonus = float(regime_cfg.get("pro_bias_bonus", 10.0))
    counter_penalty = float(regime_cfg.get("counter_bias_penalty", -25.0))
    raw_boost = float(regime_cfg.get("raw_score_boost", 5.0))
    raw_suppress_penalty = float(regime_cfg.get("raw_score_suppress_penalty", -15.0))
    raw_suppress_cap = float(regime_cfg.get("raw_score_suppress_cap", 65.0))

    nifty_pct = float(nifty_stats.get("pct_change", 0.0))
    regime = "NEUTRAL"
    bias_bonus_bull = 0.0
    bias_bonus_bear = 0.0

    if nifty_pct >= regime_thresh:
        regime = "PRO_BULL"
        raw_bull = min(100.0, raw_bull + raw_boost)
        raw_bear = min(raw_suppress_cap, max(0.0, raw_bear + raw_suppress_penalty))
        bias_bonus_bull = pro_bonus
        bias_bonus_bear = counter_penalty
    elif nifty_pct <= -regime_thresh:
        regime = "PRO_BEAR"
        raw_bear = min(100.0, raw_bear + raw_boost)
        raw_bull = min(raw_suppress_cap, max(0.0, raw_bull + raw_suppress_penalty))
        bias_bonus_bear = pro_bonus
        bias_bonus_bull = counter_penalty

    # Dominant direction
    if raw_bull >= raw_bear:
        rel_score = round(min(100.0, max(0.0, raw_bull)), 1)
        direction = "BULL"
        stage = bull_stage
        regime_bias_bonus = bias_bonus_bull
    else:
        rel_score = round(min(100.0, max(0.0, raw_bear)), 1)
        direction = "BEAR"
        stage = bear_stage
        regime_bias_bonus = bias_bonus_bear

    # Dual Scoring: Unbounded Absolute Score (S_abs)
    abs_rvol_thresh = float(sc_cfg.get("abs_bonus_rvol_thresh", 2.0))
    abs_rvol_mult = float(sc_cfg.get("abs_bonus_rvol_mult", 10.0))
    abs_accel_thresh = float(sc_cfg.get("abs_bonus_accel_thresh", 1.5))
    abs_accel_mult = float(sc_cfg.get("abs_bonus_accel_mult", 12.0))
    abs_thrust_thresh = float(sc_cfg.get("abs_bonus_thrust_thresh", 0.50))
    abs_thrust_mult = float(sc_cfg.get("abs_bonus_thrust_mult", 15.0))
    abs_zvol_thresh = float(sc_cfg.get("abs_bonus_zvol_thresh", 2.0))
    abs_zvol_mult = float(sc_cfg.get("abs_bonus_zvol_mult", 8.0))
    abs_clearance_max = float(sc_cfg.get("abs_bonus_clearance_max", 10.0))
    abs_clearance_mult = float(sc_cfg.get("abs_bonus_clearance_mult", 2.0))

    bonus_rvol = max(0.0, (rvol_5m - abs_rvol_thresh) * abs_rvol_mult) if has_baseline else 0.0
    bonus_accel = max(0.0, (a_vol - abs_accel_thresh) * abs_accel_mult)
    bonus_thrust = max(0.0, (thrust_velocity - abs_thrust_thresh) * abs_thrust_mult)
    bonus_zvol = max(0.0, (z_vol - abs_zvol_thresh) * abs_zvol_mult) if has_baseline else 0.0

    clearance_pct = float(tech_indicators.get("clearance_pct", 0.0) if direction == "BULL" else tech_indicators.get("clearance_down_pct", 0.0))
    bonus_clearance = min(abs_clearance_max, clearance_pct * abs_clearance_mult)

    abs_score = rel_score + bonus_rvol + bonus_accel + bonus_thrust + bonus_zvol + bonus_clearance + regime_bias_bonus
    abs_score = round(max(0.0, abs_score), 1)

    return {
        "score": rel_score,
        "absolute_score": abs_score,
        "direction": direction,
        "stage": stage,
        "has_baseline": has_baseline,
        "market_regime": regime,
        "nifty_pct": nifty_pct,
        "is_vol_inflated": is_vol_inflated,
        "is_actionable": pattern_state.get("is_actionable", False) or (stage in ("💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN", "🚀 BREAKOUT", "🩸 BREAKDOWN")),
        "pillars": {
            "p1_volume": round(p1_volume, 1),
            "p2_consolidation": round(p2_consolidation, 1),
            "p3_momentum": round(bull_mom if direction == "BULL" else bear_mom, 1),
            "p4_position": round(bull_pos if direction == "BULL" else bear_pos, 1),
            "p5_thrust": round(thrust_pts, 1),
        },
        "bonuses": {
            "rvol": round(bonus_rvol, 1),
            "accel": round(bonus_accel, 1),
            "thrust": round(bonus_thrust, 1),
            "zvol": round(bonus_zvol, 1),
            "clearance": round(bonus_clearance, 1),
            "regime": round(regime_bias_bonus, 1),
        },
    }
