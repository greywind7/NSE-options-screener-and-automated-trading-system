"""
Market Regime Classifier for Batch Simulation Analytics.

Derives deterministic daily market regimes and intraday window characteristics
directly from recorded F&O underlying equity ticks (`equity_ticks.parquet`),
without requiring a Nifty 50 index feed (using Nifty only as an optional
supplemental metric when present).
"""

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import polars as pl

IST = timezone(timedelta(hours=5, minutes=30))
IST_OFFSET_SEC = 19800  # 5h 30m

# Session windows in IST seconds from midnight
OPEN_START_SEC = 9 * 3600 + 15 * 60     # 09:15:00 -> 33300
OPEN_END_SEC = 11 * 3600 + 30 * 60      # 11:30:00 -> 41400
MID_END_SEC = 13 * 3600 + 30 * 60       # 13:30:00 -> 48600
CLOSE_END_SEC = 15 * 3600 + 20 * 60     # 15:20:00 -> 55200
MAX_SESSION_SEC = 15 * 3600 + 25 * 60   # 15:25:00 -> 55500

BUCKET_SEC = 300  # 5-minute buckets

REGIME_LABELS = {
    "TREND_UP": "Trend Up",
    "TREND_DOWN": "Trend Down",
    "CHOPPY_RANGE": "Choppy / Range",
    "V_REVERSAL": "V-Reversal",
    "HIGH_VOL_EXPIRY": "High-Vol / Expiry",
    "UNKNOWN": "Unknown",
}

INDEX_SYMBOLS = {"NIFTY 50", "NIFTY50", "INDIA VIX", "NA"}


def classify_trade_window(entry_time: Optional[float] = None, entry_time_str: Optional[str] = None) -> str:
    """Map a trade timestamp or HH:MM[:SS] string to OPENING, MIDDAY, or CLOSING."""
    sec_in_day: Optional[int] = None
    if entry_time is not None and entry_time > 0:
        sec_in_day = int((float(entry_time) + IST_OFFSET_SEC) % 86400)
    elif entry_time_str:
        parts = str(entry_time_str).strip().split(":")
        if len(parts) >= 2:
            try:
                hh = int(parts[0])
                mm = int(parts[1])
                ss = int(float(parts[2])) if len(parts) >= 3 else 0
                sec_in_day = hh * 3600 + mm * 60 + ss
            except ValueError:
                sec_in_day = None

    if sec_in_day is None:
        return "OPENING"
    if sec_in_day < OPEN_END_SEC:
        return "OPENING"
    if sec_in_day < MID_END_SEC:
        return "MIDDAY"
    return "CLOSING"


def _compute_window_stats(buckets: List[Dict[str, Any]], start_sec: int, end_sec: int) -> Dict[str, Any]:
    """Compute compounded synthetic return, breadth, and bias for an intraday window."""
    sub = [b for b in buckets if start_sec <= b["bucket_sec"] < end_sec]
    if not sub:
        return {
            "return_pct": 0.0,
            "mean_breadth_pct": 50.0,
            "bars": 0,
            "bias": "NEUTRAL",
        }

    equity = 1.0
    breadths: List[float] = []
    for b in sub:
        equity *= (1.0 + b["median_ret"])
        breadths.append(b["pct_adv"])

    ret_pct = (equity - 1.0) * 100.0
    mean_breadth = float(np.mean(breadths)) if breadths else 50.0
    if ret_pct >= 0.25 or (ret_pct >= 0.12 and mean_breadth >= 56.0):
        bias = "BULL"
    elif ret_pct <= -0.25 or (ret_pct <= -0.12 and mean_breadth <= 44.0):
        bias = "BEAR"
    else:
        bias = "RANGE"

    return {
        "return_pct": round(ret_pct, 3),
        "mean_breadth_pct": round(mean_breadth, 1),
        "bars": len(sub),
        "bias": bias,
    }


def _derive_regime_label(
    basket_ret_pct: float,
    efficiency_ratio: float,
    realized_vol_pct: float,
    mean_breadth_pct: float,
    morning_ret_pct: float,
    afternoon_ret_pct: float,
    is_thursday: bool,
) -> str:
    """Apply deterministic structural rules to classify the session regime."""
    abs_morning = abs(morning_ret_pct)
    abs_afternoon = abs(afternoon_ret_pct)

    # 1. V-Reversal: sharp morning excursion followed by opposing afternoon reversal
    if (
        abs_morning >= 0.45
        and abs_afternoon >= 0.45
        and (morning_ret_pct * afternoon_ret_pct) < 0
        and abs(basket_ret_pct) < 0.70 * (abs_morning + abs_afternoon)
    ):
        return "V_REVERSAL"

    # 2. High-Vol / Expiry chop or wide expansion
    if (is_thursday and realized_vol_pct >= 0.85) or realized_vol_pct >= 1.25:
        return "HIGH_VOL_EXPIRY"

    # 3. Clean Directional Trend Up / Down
    if basket_ret_pct >= 0.50 and (efficiency_ratio >= 0.25 or mean_breadth_pct >= 56.0):
        return "TREND_UP"
    if basket_ret_pct <= -0.50 and (efficiency_ratio >= 0.25 or mean_breadth_pct <= 44.0):
        return "TREND_DOWN"

    # 4. Default balanced/choppy day
    return "CHOPPY_RANGE"


class SessionRegimeClassifier:
    """Computes and caches daily F&O synthetic basket + breadth market regimes."""

    def __init__(self, recordings_dir: str = "recordings") -> None:
        self.recordings_dir = recordings_dir
        self.cache: Dict[str, Dict[str, Any]] = {}

    def clear_cache(self) -> None:
        self.cache.clear()

    def _unknown_regime(self, date_str: str, reason: str = "no_data") -> Dict[str, Any]:
        is_thu = False
        weekday_name = ""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            is_thu = dt.weekday() == 3
            weekday_name = dt.strftime("%a")
        except ValueError:
            pass

        return {
            "date": date_str,
            "regime": "UNKNOWN",
            "label": REGIME_LABELS["UNKNOWN"],
            "weekday": weekday_name,
            "is_thursday": is_thu,
            "reason": reason,
            "metrics": {
                "basket_return_pct": 0.0,
                "efficiency_ratio": 0.0,
                "realized_vol_pct": 0.0,
                "mean_breadth_pct": 50.0,
                "nifty_return_pct": None,
                "symbol_count": 0,
                "bar_count": 0,
            },
            "windows": {
                "OPENING": {"return_pct": 0.0, "mean_breadth_pct": 50.0, "bars": 0, "bias": "NEUTRAL"},
                "MIDDAY": {"return_pct": 0.0, "mean_breadth_pct": 50.0, "bars": 0, "bias": "NEUTRAL"},
                "CLOSING": {"return_pct": 0.0, "mean_breadth_pct": 50.0, "bars": 0, "bias": "NEUTRAL"},
            },
            "basket_curve": [],
        }

    def classify_dataframe(self, equity_df: pl.DataFrame, date_str: str) -> Dict[str, Any]:
        """Classify regime directly from an in-memory Polars equity ticks DataFrame."""
        if equity_df is None or equity_df.is_empty():
            return self._unknown_regime(date_str, reason="empty_dataframe")

        required_cols = {"symbol", "ltp", "exchange_timestamp"}
        if not required_cols.issubset(set(equity_df.columns)):
            return self._unknown_regime(date_str, reason="missing_columns")

        is_thu = False
        weekday_name = ""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            is_thu = dt.weekday() == 3
            weekday_name = dt.strftime("%a")
        except ValueError:
            pass

        # Restrict to market window (09:15 - 15:25 IST) and sort by timestamp
        ist_sec_expr = ((pl.col("exchange_timestamp") + IST_OFFSET_SEC) % 86400).cast(pl.Int64)
        base_lf = (
            equity_df.lazy()
            .select(["symbol", "ltp", "exchange_timestamp"])
            .filter(pl.col("ltp") > 0)
            .with_columns(ist_sec_expr.alias("ist_sec"))
            .filter((pl.col("ist_sec") >= OPEN_START_SEC) & (pl.col("ist_sec") <= MAX_SESSION_SEC))
            .with_columns(((pl.col("ist_sec") // BUCKET_SEC) * BUCKET_SEC).alias("bucket_sec"))
            .sort("exchange_timestamp")
        )

        # Optional Nifty 50 diagnostic return when present in recording
        nifty_ret_pct: Optional[float] = None
        nifty_bars = (
            base_lf.filter(pl.col("symbol").str.to_uppercase().is_in(["NIFTY 50", "NIFTY50"]))
            .group_by("bucket_sec")
            .agg([pl.col("ltp").first().alias("open"), pl.col("ltp").last().alias("close")])
            .sort("bucket_sec")
            .collect()
        )
        if not nifty_bars.is_empty():
            n_open = float(nifty_bars["open"][0])
            n_close = float(nifty_bars["close"][-1])
            if n_open > 0:
                nifty_ret_pct = round(((n_close - n_open) / n_open) * 100.0, 3)

        # Equal-weighted F&O basket across all non-index equity symbols
        bars_df = (
            base_lf.filter(~pl.col("symbol").str.to_uppercase().is_in(list(INDEX_SYMBOLS)))
            .group_by(["symbol", "bucket_sec"])
            .agg([pl.col("ltp").first().alias("open"), pl.col("ltp").last().alias("close")])
            .with_columns(((pl.col("close") - pl.col("open")) / pl.col("open")).alias("bar_ret"))
            .filter(pl.col("bar_ret").is_finite() & (pl.col("bar_ret").abs() < 0.25))
            .collect()
        )

        if bars_df.is_empty():
            res = self._unknown_regime(date_str, reason="no_market_bars")
            res["metrics"]["nifty_return_pct"] = nifty_ret_pct
            return res

        symbol_count = int(bars_df["symbol"].n_unique())

        bucket_agg = (
            bars_df.group_by("bucket_sec")
            .agg([
                pl.col("bar_ret").median().alias("median_ret"),
                (pl.col("bar_ret") > 0).sum().alias("adv_count"),
                (pl.col("bar_ret") < 0).sum().alias("dec_count"),
                pl.len().alias("active_count"),
            ])
            .sort("bucket_sec")
        )

        buckets: List[Dict[str, Any]] = []
        index_vals: List[float] = [100.0]
        rets: List[float] = []
        breadths: List[float] = []

        for row in bucket_agg.iter_rows(named=True):
            med = float(row["median_ret"] or 0.0)
            active = max(1, int(row["active_count"] or 1))
            adv = int(row["adv_count"] or 0)
            pct_adv = (adv / active) * 100.0

            new_idx = index_vals[-1] * (1.0 + med)
            index_vals.append(new_idx)
            rets.append(med)
            breadths.append(pct_adv)

            sec = int(row["bucket_sec"])
            hh = sec // 3600
            mm = (sec % 3600) // 60
            buckets.append({
                "bucket_sec": sec,
                "time": f"{hh:02d}:{mm:02d}",
                "median_ret": med,
                "index": round(new_idx, 3),
                "pct_adv": round(pct_adv, 1),
            })

        basket_ret_pct = ((index_vals[-1] / 100.0) - 1.0) * 100.0
        total_path = sum(abs(index_vals[i] - index_vals[i - 1]) for i in range(1, len(index_vals)))
        efficiency_ratio = abs(index_vals[-1] - 100.0) / total_path if total_path > 1e-9 else 0.0
        realized_vol_pct = (float(np.std(rets)) * math.sqrt(len(rets)) * 100.0) if len(rets) >= 2 else 0.0
        mean_breadth_pct = float(np.mean(breadths)) if breadths else 50.0

        opening_stats = _compute_window_stats(buckets, OPEN_START_SEC, OPEN_END_SEC)
        midday_stats = _compute_window_stats(buckets, OPEN_END_SEC, MID_END_SEC)
        closing_stats = _compute_window_stats(buckets, MID_END_SEC, CLOSE_END_SEC + BUCKET_SEC)

        afternoon_equity = (1.0 + midday_stats["return_pct"] / 100.0) * (1.0 + closing_stats["return_pct"] / 100.0)
        afternoon_ret_pct = (afternoon_equity - 1.0) * 100.0

        regime = _derive_regime_label(
            basket_ret_pct=basket_ret_pct,
            efficiency_ratio=efficiency_ratio,
            realized_vol_pct=realized_vol_pct,
            mean_breadth_pct=mean_breadth_pct,
            morning_ret_pct=opening_stats["return_pct"],
            afternoon_ret_pct=afternoon_ret_pct,
            is_thursday=is_thu,
        )

        result = {
            "date": date_str,
            "regime": regime,
            "label": REGIME_LABELS.get(regime, regime),
            "weekday": weekday_name,
            "is_thursday": is_thu,
            "metrics": {
                "basket_return_pct": round(basket_ret_pct, 3),
                "efficiency_ratio": round(efficiency_ratio, 3),
                "realized_vol_pct": round(realized_vol_pct, 3),
                "mean_breadth_pct": round(mean_breadth_pct, 1),
                "nifty_return_pct": nifty_ret_pct,
                "symbol_count": symbol_count,
                "bar_count": len(buckets),
            },
            "windows": {
                "OPENING": opening_stats,
                "MIDDAY": midday_stats,
                "CLOSING": closing_stats,
            },
            "basket_curve": [{"time": b["time"], "index": b["index"], "pct_adv": b["pct_adv"]} for b in buckets],
        }
        self.cache[date_str] = result
        return result

    def get_regime(self, date_str: str, equity_df: Optional[pl.DataFrame] = None) -> Dict[str, Any]:
        """Return cached regime or compute lazily from memory/disk Parquet for a date."""
        if not date_str:
            return self._unknown_regime("", reason="empty_date")

        if date_str in self.cache:
            return self.cache[date_str]

        if equity_df is not None and not equity_df.is_empty():
            return self.classify_dataframe(equity_df, date_str)

        parquet_path = os.path.join(self.recordings_dir, date_str, "equity_ticks.parquet")
        if not os.path.exists(parquet_path):
            return self._unknown_regime(date_str, reason="missing_parquet")

        try:
            df = pl.scan_parquet(parquet_path).select(["symbol", "ltp", "exchange_timestamp"]).collect()
            return self.classify_dataframe(df, date_str)
        except Exception as exc:
            return self._unknown_regime(date_str, reason=f"read_error:{exc}")
