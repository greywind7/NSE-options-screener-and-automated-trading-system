"""
Alert Service: Ingests real-time and archived setup alerts, inspects live Shared Memory
candle states, caches live snapshots locally, and provides candle charts with alert trigger points.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.candles.shm_layout import (
    NUM_SYMBOLS,
    create_or_attach_shm,
    get_shm_views,
)
from core.common.logging_utils import get_logger
from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST

_logger = get_logger("ALERT_SERVICE")
from web.candle_service import CandleService


class AlertService:
    def __init__(self, recordings_dir: str = "recordings", logs_dir: str = "logs"):
        self.recordings_dir = recordings_dir
        self.logs_dir = logs_dir
        self.candle_service = CandleService(recordings_dir=recordings_dir)

        # In-memory local cache: (date, symbol) -> List of candle dicts
        self._local_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._shm = None
        self._candles_view = None

        # Universe mapping: symbol -> shm_idx
        self.symbol_to_idx: Dict[str, int] = {}
        self._load_universe_map()

    def _load_universe_map(self) -> None:
        """Map equities to SHM row indices from data/filtered.csv."""
        path = "data/filtered.csv"
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                idx = 0
                for row in reader:
                    sym = (
                        row.get("trading_symbol")
                        or row.get("tradingsymbol")
                        or row.get("symbol")
                        or row.get("display_name")
                    )
                    if sym and idx < NUM_SYMBOLS:
                        clean = sym.strip().upper()
                        self.symbol_to_idx[clean] = idx
                        self.symbol_to_idx[f"NSE:{clean}"] = idx
                        ik = row.get("upstox_instrument_key") or row.get("instrument_key")
                        if ik:
                            self.symbol_to_idx[ik.strip()] = idx
                            self.symbol_to_idx[normalize_instrument_token(ik.strip())] = idx
                        idx += 1
        except Exception as e:
            _logger.warning(f"Error loading universe map: {e}")

    def _get_symbol_shm_index(self, symbol: str) -> Optional[int]:
        clean = symbol.strip().upper()
        if clean in self.symbol_to_idx:
            return self.symbol_to_idx[clean]
        if f"NSE:{clean}" in self.symbol_to_idx:
            return self.symbol_to_idx[f"NSE:{clean}"]
        return None

    def get_alerts(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Retrieve all alerts generated on target_date.
        Returns top 5 latest alerts at the top and the full log below.
        """
        now = time.time()
        if not target_date or target_date in ("today", "now", "live", ""):
            canonical_date = datetime.fromtimestamp(now, tz=IST).strftime("%Y-%m-%d")
        else:
            canonical_date = target_date
            if len(target_date) == 8 and target_date.isdigit():
                canonical_date = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"

        raw_candidates = [
            os.path.join(self.logs_dir, "alerts", f"alerts_{canonical_date}.jsonl"),
            os.path.join(self.logs_dir, "alerts", f"alerts_{canonical_date.replace('-', '')}.jsonl"),
            os.path.join(self.logs_dir, f"alerts_{canonical_date}.jsonl"),
            os.path.join(self.logs_dir, f"alerts_{canonical_date.replace('-', '')}.jsonl"),
        ]

        alerts_list: List[Dict[str, Any]] = []

        for p in raw_candidates:
            if os.path.exists(p) and os.path.getsize(p) > 0:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                d = json.loads(line)
                                ts = float(d.get("timestamp") or 0.0)
                                d["time_str"] = (
                                    datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M:%S")
                                    if ts > 0
                                    else "-"
                                )
                                d["trigger_price"] = round(float(d.get("trigger_price", 0.0)), 2)
                                d["rvol"] = round(float(d.get("rvol", 0.0)), 2)
                                d["clv"] = round(float(d.get("clv", 0.0)), 2)
                                d["atr"] = round(float(d.get("atr", 0.0)), 2)
                                d["date"] = canonical_date
                                alerts_list.append(d)
                            except Exception:
                                pass
                except Exception:
                    pass
                break

        # Sort descending by timestamp (newest first)
        alerts_list.sort(key=lambda a: float(a.get("timestamp", 0.0)), reverse=True)

        latest_5 = alerts_list[:5]

        return {
            "date": canonical_date,
            "total_count": len(alerts_list),
            "latest_5": latest_5,
            "alerts": alerts_list,
        }

    def get_alert_candles(
        self,
        symbol: str,
        target_date: Optional[str] = None,
        trigger_price: Optional[float] = None,
        trigger_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve 5-minute candlestick data for an alert.
        Attaches to live CandleManager SHM if active, updates local cache,
        and falls back to local snapshot or recorded Parquet data.
        """
        clean_sym = symbol.strip().upper()
        now = time.time()
        canonical_date = target_date or datetime.fromtimestamp(now, tz=IST).strftime("%Y-%m-%d")
        if len(canonical_date) == 8 and canonical_date.isdigit():
            canonical_date = f"{canonical_date[:4]}-{canonical_date[4:6]}-{canonical_date[6:8]}"

        candles: List[Dict[str, Any]] = []
        cache_key = (canonical_date, clean_sym)

        # 1. Immediate in-memory cache hit (0.00ms lookup)
        if cache_key in self._local_cache and self._local_cache[cache_key]:
            candles = self._local_cache[cache_key]

        # 2. Check local disk snapshot cache (<1ms lookup)
        if not candles:
            cached = self._read_from_local_cache(canonical_date, clean_sym)
            if cached:
                candles = cached
                self._local_cache[cache_key] = candles

        # 3. Attempt to attach to live CandleManager Shared Memory
        if not candles:
            shm_candles = self._read_shm_candles(clean_sym)
            if shm_candles and len(shm_candles) > 0:
                candles = shm_candles
                self._save_to_local_cache(canonical_date, clean_sym, candles)
                self._local_cache[cache_key] = candles

        # 4. Fall back to recorded Parquet files (scanned once, then cached in memory & disk)
        if not candles:
            try:
                candles = self.candle_service.get_candles(
                    target_date=canonical_date,
                    token=f"NSE:{clean_sym}",
                    symbol=clean_sym,
                    is_option=False,
                    interval_minutes=5,
                )
                if candles:
                    self._save_to_local_cache(canonical_date, clean_sym, candles)
                    self._local_cache[cache_key] = candles
            except Exception as e:
                _logger.warning(f"Parquet fallback error: {e}")

        # Build trigger marker
        markers = []
        if trigger_ts and float(trigger_ts) > 0:
            t_price = float(trigger_price or 0.0)
            if t_price <= 0 and candles:
                matching = None
                for i in range(len(candles) - 1):
                    if candles[i]["timestamp"] <= trigger_ts < candles[i + 1]["timestamp"]:
                        matching = candles[i]
                        break
                if not matching and len(candles) > 0:
                    matching = candles[-1] if trigger_ts >= candles[-1]["timestamp"] else candles[0]
                t_price = matching["open"] if matching else 0.0

            markers.append({
                "timestamp": float(trigger_ts),
                "price": t_price,
                "type": "TRIGGER",
            })

        return {
            "symbol": clean_sym,
            "date": canonical_date,
            "candles": candles,
            "markers": markers,
            "trigger_price": trigger_price,
            "trigger_ts": trigger_ts,
        }

    def close(self) -> None:
        self._candles_view = None
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception:
                pass
            self._shm = None

    def __del__(self) -> None:
        self.close()

    def _read_shm_candles(self, symbol: str) -> List[Dict[str, Any]]:
        """Read 5-minute bars for symbol directly from live POSIX/mmap Shared Memory."""
        idx = self._get_symbol_shm_index(symbol)
        if idx is None or idx < 0 or idx >= NUM_SYMBOLS:
            return []

        bars: List[Dict[str, Any]] = []
        try:
            if self._shm is None or self._candles_view is None:
                self._shm = create_or_attach_shm(name="shm_ohlc_221", create=False)
                _, self._candles_view = get_shm_views(self._shm)
            row = self._candles_view[idx]

            for b in row:
                ts = int(b["timestamp"])
                if ts <= 0:
                    continue
                bars.append({
                    "timestamp": ts,
                    "time": datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M"),
                    "open": round(float(b["open"]), 2),
                    "high": round(float(b["high"]), 2),
                    "low": round(float(b["low"]), 2),
                    "close": round(float(b["close"]), 2),
                    "volume": int(b["volume"]),
                    "rvol": round(float(b["rvol"]), 2),
                    "atr": round(float(b["atr"]), 2),
                })
        except Exception:
            # SHM segment offline or recreated: reset cached handle cleanly
            self.close()

        return bars

    def _save_to_local_cache(self, date: str, symbol: str, candles: List[Dict[str, Any]]) -> None:
        """Persist snapshot to disk cache."""
        try:
            cache_dir = os.path.join(self.logs_dir, "alert_candle_cache")
            os.makedirs(cache_dir, exist_ok=True)
            p = os.path.join(cache_dir, f"{date}_{symbol}.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(candles, f)
        except Exception:
            pass

    def _read_from_local_cache(self, date: str, symbol: str) -> Optional[List[Dict[str, Any]]]:
        """Read cached snapshot from memory or disk."""
        if (date, symbol) in self._local_cache:
            return self._local_cache[(date, symbol)]
        p = os.path.join(self.logs_dir, "alert_candle_cache", f"{date}_{symbol}.json")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._local_cache[(date, symbol)] = data
                    return data
            except Exception:
                pass
        return None
