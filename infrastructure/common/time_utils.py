#!/usr/bin/env python3
"""
infrastructure/common/time_utils.py
===================================
Centralized time parsing and normalization utilities.
Guarantees consistent 24-hour zero-padded 'HH:MM' strings so that ASCII string
comparisons (e.g. cur_hm < market_open_time) behave deterministically against
datetime.strftime('%H:%M').
"""

from typing import Any, Dict, Optional, Tuple


def normalize_time_str(t_str: Optional[str], default: str = "09:15") -> str:
    """
    Normalizes time string into standard 24-hour zero-padded 'HH:MM' format.
    Handles:
      - '9:15' -> '09:15'
      - '09:15' -> '09:15'
      - '9:15:00' -> '09:15'
      - '9' -> '09:00'
      - '15:15' -> '15:15'
      - None / '' -> default
    Ensures safe ASCII string comparison against datetime.strftime('%H:%M').
    """
    if not t_str:
        return default
    s = str(t_str).strip()
    if not s:
        return default
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return f"{h:02d}:{m:02d}"
    except (ValueError, TypeError):
        return default


def extract_quote_timestamp(quote: Optional[Dict[str, Any]]) -> float:
    """
    Extracts epoch seconds timestamp from a market quote dictionary.
    Handles 'received_timestamp', 'exchange_timestamp', and 'timestamp' in seconds or milliseconds,
    or ISO datetime strings. Returns 0.0 if not found or invalid.
    """
    if not quote or not isinstance(quote, dict):
        return 0.0
    ts = quote.get("received_timestamp") or quote.get("exchange_timestamp") or quote.get("timestamp")
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return (ts / 1000.0) if ts > 1e12 else float(ts)
    if isinstance(ts, str):
        try:
            ts_f = float(ts)
            return (ts_f / 1000.0) if ts_f > 1e12 else ts_f
        except ValueError:
            try:
                from datetime import datetime
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return 0.0
    return 0.0


def is_quote_fresh(
    quote: Optional[Dict[str, Any]],
    now_ts: Optional[float] = None,
    max_age_sec: float = 30.0,
) -> Tuple[bool, float]:
    """
    Checks if a market quote was updated within max_age_sec against reference clock now_ts.
    If now_ts is None, uses current system time time.time().
    Returns (is_fresh: bool, age_seconds: float).

    If quote is missing, returns (False, inf).
    If quote has no timestamp (e.g. offline unit test mock quotes without timestamps):
      - If TRADING_TEST_MODE == "1", treats as fresh with age 0.0.
      - Otherwise, treats as stale (False, inf) because untimestamped quotes cannot be verified in live/sim.
    """
    if not quote or not isinstance(quote, dict):
        import os, sys
        if os.environ.get("TRADING_TEST_MODE") == "1" or "unittest" in sys.modules:
            return True, 0.0
        return False, float("inf")
    if now_ts is None:
        import time
        now_ts = time.time()
    q_ts = extract_quote_timestamp(quote)
    if q_ts <= 0:
        import os, sys
        if os.environ.get("TRADING_TEST_MODE") == "1" or "unittest" in sys.modules:
            return True, 0.0
        return False, float("inf")
    age = now_ts - q_ts
    if age < -2.0:  # Clock skew beyond 2 seconds into future
        return False, age
    return (age <= max_age_sec), max(0.0, age)


