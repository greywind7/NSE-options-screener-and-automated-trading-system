"""
Candle Service: High-speed Polars-powered OHLCV candle aggregator with in-memory caching.
Extracts intraday candles (default: 5-minute bars) for cash equities and option contracts from recorded Parquet files.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
import os
import time
from typing import Any, Dict, List, Optional, Tuple
import polars as pl

from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST


class CandleService:
    def __init__(self, recordings_dir: str = "recordings"):
        self.recordings_dir = recordings_dir
        # In-memory LRU caches
        self._candle_cache: OrderedDict[Tuple[str, str, bool, int], List[Dict[str, Any]]] = OrderedDict()
        self._df_cache: OrderedDict[Tuple[str, bool], pl.DataFrame] = OrderedDict()

    def get_candles(
        self,
        target_date: str,
        token: str,
        symbol: str = "",
        is_option: bool = False,
        interval_minutes: int = 5,
        source_df: Optional[pl.DataFrame] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate OHLCV candles (default: 5-minute intervals) for the requested symbol/token on target_date.
        If source_df is provided (e.g. from in-memory cache), filters in-memory at microsecond speed.
        """
        norm_date = target_date.replace("-", "") if not ("-" in target_date and len(target_date) == 10) else target_date
        cache_key = (norm_date, token or symbol, is_option, interval_minutes)
        if cache_key in self._candle_cache:
            self._candle_cache.move_to_end(cache_key)
            return self._candle_cache[cache_key]

        norm_tok = normalize_instrument_token(token)
        clean_sym = symbol.strip().upper()

        # Build filter expression
        filters = []
        if norm_tok:
            filters.append(pl.col("token") == norm_tok)
        if clean_sym:
            filters.append(pl.col("symbol") == clean_sym)
            if not is_option and ":" not in clean_sym:
                filters.append(pl.col("token") == f"NSE:{clean_sym}")

        if not filters:
            return []

        combined_filter = filters[0]
        for f in filters[1:]:
            combined_filter = combined_filter | f

        df = None
        if source_df is not None and len(source_df) > 0:
            try:
                df = source_df.filter(combined_filter)
            except Exception:
                df = None

        if df is None:
            # Check cached in-memory DataFrame
            df_cache_key = (norm_date, is_option)
            if df_cache_key in self._df_cache:
                try:
                    self._df_cache.move_to_end(df_cache_key)
                    df = self._df_cache[df_cache_key].filter(combined_filter)
                except Exception:
                    df = None

            if df is None:
                cand_dirs = [
                    os.path.join(self.recordings_dir, target_date),
                    os.path.join(self.recordings_dir, target_date.replace("-", "")),
                ]

                pq_filename = "option_depth.parquet" if is_option else "equity_ticks.parquet"
                pq_path = None
                for d in cand_dirs:
                    p = os.path.join(d, pq_filename)
                    if os.path.exists(p) and os.path.getsize(p) > 0:
                        pq_path = p
                        break

                if not pq_path:
                    return []

                try:
                    schema = pl.scan_parquet(pq_path).collect_schema()
                    if not is_option:
                        # For equity ticks, read essential columns and cache the DataFrame in memory
                        # Memory footprint is ~30MB for 2M ticks, enabling sub-millisecond filtering for all 221 equities
                        needed_cols = [c for c in ["token", "symbol", "exchange_timestamp", "received_timestamp", "ltp", "volume"] if c in schema]
                        loaded_df = pl.read_parquet(pq_path, columns=needed_cols)
                        if len(self._df_cache) >= 3:
                            self._df_cache.popitem(last=False)
                        self._df_cache[df_cache_key] = loaded_df
                        df = loaded_df.filter(combined_filter)
                    else:
                        # For options, use select + predicate pushdown to minimize disk I/O and RAM
                        needed_cols = [c for c in ["token", "symbol", "exchange_timestamp", "received_timestamp", "ltp", "bid", "ask", "volume"] if c in schema]
                        df = pl.scan_parquet(pq_path).select(needed_cols).filter(combined_filter).collect()
                except Exception:
                    return []

        if df is None or len(df) == 0:
            return []

        try:
            ts_col = "exchange_timestamp" if "exchange_timestamp" in df.columns else "received_timestamp"
            # Resample to requested timeframe bars (default: 5 minutes = 300s)
            tf_sec = max(60, interval_minutes * 60)
            
            price_cols = [pl.col(c) for c in ["ltp", "bid", "ask"] if c in df.columns]
            if not price_cols:
                return []
            price_expr = pl.coalesce(price_cols).alias("price") if len(price_cols) > 1 else price_cols[0].alias("price")

            bars_df = df.with_columns(
                (pl.col(ts_col) // tf_sec * tf_sec).cast(pl.Int64).alias("bar_ts"),
                price_expr
            ).group_by("bar_ts").agg([
                pl.col("price").first().alias("open"),
                pl.col("price").max().alias("high"),
                pl.col("price").min().alias("low"),
                pl.col("price").last().alias("close"),
                pl.col("volume").last().alias("vol_end"),
                pl.col("volume").first().alias("vol_start"),
            ]).sort("bar_ts")

            candles: List[Dict[str, Any]] = []
            prev_vol = 0
            for row in bars_df.iter_rows(named=True):
                bar_ts = int(row["bar_ts"])
                if bar_ts <= 0:
                    continue

                v_end = int(row.get("vol_end") or 0)
                v_start = int(row.get("vol_start") or 0)
                # Intraday volume delta for this bar
                bar_vol = max(0, v_end - v_start) if v_end >= v_start else v_end

                candles.append({
                    "timestamp": bar_ts,
                    "time": datetime.fromtimestamp(bar_ts, tz=IST).strftime("%H:%M"),
                    "open": round(float(row["open"]), 2),
                    "high": round(float(row["high"]), 2),
                    "low": round(float(row["low"]), 2),
                    "close": round(float(row["close"]), 2),
                    "volume": bar_vol,
                })

            if len(self._candle_cache) >= 256:
                self._candle_cache.popitem(last=False)
            self._candle_cache[cache_key] = candles
            return candles

        except Exception as e:
            print(f"[CANDLE_SERVICE] Error generating candles for {token}/{symbol}: {e}")
            return []
