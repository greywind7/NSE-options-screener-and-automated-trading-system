#!/usr/bin/env python3
"""
strategies/momentum/scanner.py
==============================
Real-Time Feature Ingestion & State Cache.

Maintains:
1. Rolling Volume Tracker: RVOL_5m, RVOL_cum, Pace ratio, Intra-5m Volume Acceleration A_vol.
2. Statistical Variance Surge (Z_vol): Rolling 60-minute mean & standard deviation.
3. Micro-Tick History & Velocity Engine.
4. Macro Market Tracking: Real-time Nifty 50 index tracker for Market Regime Bias.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


class RollingVolumeTracker:
    """
    Computes real-time rolling metrics across all universe instruments and the Nifty 50 index.
    """

    def __init__(self, nifty_key: str = "NSE_INDEX|Nifty 50", bias_threshold_pct: float = 0.25):
        # key -> list of (epoch_sec: float, cumulative_vol: int)
        self.history: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        # key -> dict for minute candle boundaries
        self.minute_state: Dict[str, Dict[str, Any]] = {}
        # key -> list of (epoch_sec: float, rvol_5m: float) for rolling 60m variance
        self.rvol_5m_history: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        # key -> list of (epoch_sec: float, ltp: float) for 5m price change
        self.price_history_5m: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        # Macro Nifty Index State
        self.nifty_key = nifty_key
        self.bias_threshold_pct = float(bias_threshold_pct)
        self.nifty_close = 0.0
        self.nifty_ltp = 0.0
        self.nifty_pct = 0.0

    def update_nifty(self, ltp: float, close: float) -> Dict[str, Any]:
        """Update Nifty 50 index state and compute regime."""
        if ltp > 0:
            self.nifty_ltp = ltp
        if close > 0:
            self.nifty_close = close

        if self.nifty_close > 0 and self.nifty_ltp > 0:
            net = self.nifty_ltp - self.nifty_close
            self.nifty_pct = round((net / self.nifty_close) * 100.0, 2)

        regime = "NEUTRAL"
        if self.nifty_pct >= self.bias_threshold_pct:
            regime = "PRO_BULL"
        elif self.nifty_pct <= -self.bias_threshold_pct:
            regime = "PRO_BEAR"

        return {
            "ltp": self.nifty_ltp,
            "close": self.nifty_close,
            "pct_change": self.nifty_pct,
            "regime": regime,
        }

    def update(
        self,
        instrument_key: str,
        current_volume: int,
        ltp: float,
        profile: Dict[str, Any],
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Record current tick and compute rolling volume and momentum metrics.
        """
        if now_ts is None:
            now_ts = time.time()

        hist = self.history[instrument_key]
        p_hist = self.price_history_5m[instrument_key]

        # Append tick records
        hist.append((now_ts, current_volume))
        if ltp > 0:
            p_hist.append((now_ts, ltp))

        # Prune records older than 10 minutes for history, 60 minutes for variance
        cutoff_5m = now_ts - 300.0
        while hist and hist[0][0] < cutoff_5m - 60.0:
            hist.pop(0)

        while p_hist and p_hist[0][0] < cutoff_5m - 60.0:
            p_hist.pop(0)

        # 1. Rolling 5-minute Volume Delta
        vol_5m = 0
        if len(hist) >= 2:
            earliest_in_window = hist[-1][1]
            for ts, v in reversed(hist):
                if ts <= cutoff_5m:
                    earliest_in_window = v
                    break
                earliest_in_window = v
            vol_5m = max(0, current_volume - earliest_in_window)

        # Baseline expected 5m volume and cumulative expected volume
        # Strictly require genuine historical candle data (>0); NEVER assume dummy values (like 1,000 or 10,000)!
        has_baseline = bool(
            profile
            and profile.get("has_volume_data", True)
            and float(profile.get("daily_avg_volume") or 0.0) > 0.0
            and int(profile.get("total_candles") or 0) > 0
        )

        avg_5m_expected = float(profile.get("avg_5m_volume") or 0.0) if has_baseline else 0.0
        cum_expected = float(profile.get("daily_avg_volume") or 0.0) if has_baseline else 0.0

        if has_baseline and avg_5m_expected > 0:
            rvol_5m = round(vol_5m / avg_5m_expected, 2)
        else:
            rvol_5m = 0.0

        # Cumulative RVOL (normalized to time-of-day expected volume)
        expected_cum_vol = cum_expected
        if has_baseline and now_ts >= 1_000_000_000:
            try:
                import datetime
                ist_dt = datetime.datetime.fromtimestamp(
                    now_ts, tz=datetime.timezone(datetime.timedelta(hours=5, minutes=30))
                )
                time_str = ist_dt.strftime("%H:%M")
                slot_cum = profile.get("time_slot_cum_volume", {})
                if slot_cum and time_str in slot_cum and float(slot_cum[time_str]) > 0:
                    expected_cum_vol = float(slot_cum[time_str])
                else:
                    # Intraday progression: 375 minutes (09:15 to 15:30)
                    mins_elapsed = (ist_dt.hour * 60 + ist_dt.minute) - (9 * 60 + 15)
                    day_fraction = max(0.04, min(1.0, mins_elapsed / 375.0))
                    expected_cum_vol = cum_expected * day_fraction
            except Exception:
                expected_cum_vol = cum_expected

        if has_baseline and expected_cum_vol > 0:
            rvol_cum = round(current_volume / expected_cum_vol, 2)
        else:
            rvol_cum = 0.0

        # Pace Ratio: RVOL_5m / max(0.1, RVOL_cum) if both available
        if has_baseline and rvol_cum > 0:
            pace = round(rvol_5m / max(0.1, rvol_cum), 2)
        else:
            pace = 0.0

        # Intra-5m Volume Acceleration A_vol
        # Compare last 1 minute delta to expected 1m portion of 5m
        cutoff_1m = now_ts - 60.0
        vol_1m = 0
        earliest_1m = current_volume
        for ts, v in reversed(hist):
            if ts <= cutoff_1m:
                earliest_1m = v
                break
            earliest_1m = v
        vol_1m = max(0, current_volume - earliest_1m)

        proj_5m_from_1m = vol_1m * 5.0
        a_vol = round(proj_5m_from_1m / max(1.0, float(vol_5m)), 2) if vol_5m > 0 else 0.0

        # Rolling 60m Variance Surge (Z_vol)
        var_hist = self.rvol_5m_history[instrument_key]
        var_hist.append((now_ts, rvol_5m))
        cutoff_60m = now_ts - 3600.0
        while var_hist and var_hist[0][0] < cutoff_60m:
            var_hist.pop(0)

        z_vol = 0.0
        if len(var_hist) >= 10:
            vals = [val for _, val in var_hist]
            mean_val = sum(vals) / len(vals)
            variance = sum((x - mean_val) ** 2 for x in vals) / len(vals)
            std_dev = math.sqrt(variance)
            if std_dev > 0.05:
                z_vol = round((rvol_5m - mean_val) / std_dev, 2)

        # Rolling 5m price change
        p_5m_delta = 0.0
        if len(p_hist) >= 2:
            earliest_p = p_hist[0][1]
            for ts, p in reversed(p_hist):
                if ts <= cutoff_5m:
                    earliest_p = p
                    break
                earliest_p = p
            p_5m_delta = round(ltp - earliest_p, 2)

        return {
            "has_baseline": has_baseline,
            "rvol_5m": rvol_5m,
            "rvol_cum": rvol_cum,
            "pace": pace,
            "a_vol": a_vol,
            "z_vol": z_vol,
            "vol_5m": vol_5m,
            "vol_1m": vol_1m,
            "p_5m_delta": p_5m_delta,
        }

