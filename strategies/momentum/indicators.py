#!/usr/bin/env python3
"""
strategies/momentum/indicators.py
=================================
Technical Indicator Engine (Pure Python Standard Library):
1. Relative Strength Index (RSI 14) with Wilder's exponential smoothing.
2. Moving Average Convergence Divergence (MACD 12, 26, 9) with histogram contraction tracking.
3. Classical Pivot Support & Resistance (Pivot, R1, R2, S1, S2, HOD, LOD).
4. Structural Clearance calculation (runway to next major overhead/underlying barrier).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def compute_rsi(prices: List[float], period: int = 14) -> float:
    """
    Compute RSI using Wilder's smoothed moving average.
    Returns 50.0 if insufficient data.
    """
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = prices[i] - prices[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = abs(diff) if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def compute_macd(
    prices: List[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> Dict[str, Any]:
    """
    Compute MACD line, signal line, and histogram.
    Also tracks histogram contraction (momentum slowing/reversing).
    """
    if len(prices) < slow_period + signal_period:
        return {
            "macd": 0.0,
            "signal": 0.0,
            "hist": 0.0,
            "prev_hist": 0.0,
            "contracting": False,
        }

    # Fast EMA
    k_fast = 2.0 / (fast_period + 1.0)
    ema_fast = sum(prices[:fast_period]) / fast_period
    fast_emas = [ema_fast]
    for p in prices[fast_period:]:
        ema_fast = p * k_fast + ema_fast * (1.0 - k_fast)
        fast_emas.append(ema_fast)

    # Slow EMA
    k_slow = 2.0 / (slow_period + 1.0)
    ema_slow = sum(prices[:slow_period]) / slow_period
    slow_emas = [ema_slow]
    for p in prices[slow_period:]:
        ema_slow = p * k_slow + ema_slow * (1.0 - k_slow)
        slow_emas.append(ema_slow)

    # Align lengths
    offset = slow_period - fast_period
    macd_series = []
    for i in range(len(slow_emas)):
        macd_series.append(fast_emas[i + offset] - slow_emas[i])

    if len(macd_series) < signal_period:
        return {
            "macd": round(macd_series[-1], 2),
            "signal": 0.0,
            "hist": 0.0,
            "prev_hist": 0.0,
            "contracting": False,
        }

    # Signal Line (EMA of MACD)
    k_sig = 2.0 / (signal_period + 1.0)
    sig = sum(macd_series[:signal_period]) / signal_period
    sig_series = [sig]
    for m in macd_series[signal_period:]:
        sig = m * k_sig + sig * (1.0 - k_sig)
        sig_series.append(sig)

    curr_macd = macd_series[-1]
    curr_sig = sig_series[-1]
    curr_hist = curr_macd - curr_sig

    prev_hist = 0.0
    contracting = False
    if len(sig_series) >= 2:
        prev_macd = macd_series[-2]
        prev_sig = sig_series[-2]
        prev_hist = prev_macd - prev_sig
        if abs(curr_hist) < abs(prev_hist):
            contracting = True

    return {
        "macd": round(curr_macd, 4),
        "signal": round(curr_sig, 4),
        "hist": round(curr_hist, 4),
        "prev_hist": round(prev_hist, 4),
        "contracting": contracting,
    }


def compute_support_resistance_levels(
    high: float,
    low: float,
    close: float,
    ltp: float,
) -> Dict[str, Any]:
    """
    Classical floor pivots and structural clearance.
    Prevents LOD breakdown self-blocking by checking if LOD is already cracked.
    """
    if high <= 0 or low <= 0 or close <= 0 or ltp <= 0:
        return {
            "pivot": 0.0,
            "r1": 0.0,
            "r2": 0.0,
            "s1": 0.0,
            "s2": 0.0,
            "hod": high,
            "lod": low,
            "clearance_pts": 0.0,
            "clearance_pct": 0.0,
            "next_resistance": 0.0,
            "next_support": 0.0,
            "is_near_support": False,
            "is_near_resistance": False,
        }

    pivot = (high + low + close) / 3.0
    r1 = (2.0 * pivot) - low
    s1 = (2.0 * pivot) - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)

    # Next Resistance above LTP
    resistances = sorted([lvl for lvl in [r1, r2, high] if lvl > ltp])
    next_res = resistances[0] if resistances else ltp * 1.05
    clearance_up_pts = max(0.0, next_res - ltp)
    clearance_up_pct = (clearance_up_pts / ltp) * 100.0 if ltp > 0 else 0.0

    # Next Support below LTP
    supports = sorted([lvl for lvl in [s1, s2, low] if lvl < ltp], reverse=True)
    next_sup = supports[0] if supports else ltp * 0.95
    clearance_down_pts = max(0.0, ltp - next_sup)
    clearance_down_pct = (clearance_down_pts / ltp) * 100.0 if ltp > 0 else 0.0

    # Nearness check (within 0.35% of major level)
    near_thresh = ltp * 0.0035

    # If LTP is breaking LOD or already below LOD, LOD is breached — not a support block!
    lod_breached = ltp <= (low + (ltp * 0.0005))
    is_near_sup = False
    if not lod_breached:
        for s in [s1, s2, low]:
            if 0 < (ltp - s) <= near_thresh:
                is_near_sup = True
                break

    is_near_res = False
    hod_breached = ltp >= (high - (ltp * 0.0005))
    if not hod_breached:
        for r in [r1, r2, high]:
            if 0 < (r - ltp) <= near_thresh:
                is_near_res = True
                break

    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "hod": round(high, 2),
        "lod": round(low, 2),
        "clearance_pts": round(clearance_up_pts, 2),
        "clearance_pct": round(clearance_up_pct, 2),
        "clearance_down_pct": round(clearance_down_pct, 2),
        "next_resistance": round(next_res, 2),
        "next_support": round(next_sup, 2),
        "is_near_support": is_near_sup,
        "is_near_resistance": is_near_res,
        "lod_breached": lod_breached,
        "hod_breached": hod_breached,
    }

