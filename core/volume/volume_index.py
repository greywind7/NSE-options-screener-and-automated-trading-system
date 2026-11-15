#!/usr/bin/env python3
"""
infrastructure/volume/volume_index.py
=====================================
Constructs and maintains the 30-Day Historical Volume Baseline Index.

For every filtered equity:
1. Calculates historical 1-minute and 5-minute slot averages (09:15 to 15:29).
2. Computes the cumulative expected volume curve throughout the trading day.
3. Computes daily average volume, baseline variance, and slot distributions.
4. Saves to data/index.json for sub-second, offline RVOL calculations during market hours.
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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.auth.upstox_auth import get_access_token, http_get_json, safe_float, safe_int


def compute_volume_profile(candles: List[List[Any]], interval_minutes: int = 1) -> Dict[str, Any]:
    """
    Given a list of Upstox historical candles:
      [timestamp_str, open, high, low, close, volume, oi]
    Compute comprehensive volume metrics.
    """
    if not candles:
        return {
            "interval_minutes": interval_minutes,
            "total_candles": 0,
            "total_volume": 0,
            "has_volume_data": False,
            "avg_candle_volume": 0.0,
            "avg_1m_volume": 0.0,
            "avg_5m_volume": 0.0,
            "daily_avg_volume": 0.0,
            "time_slot_avg_volume": {},
            "time_slot_cum_volume": {},
        }

    # Check if these are daily candles (e.g. 1 candle per day)
    is_daily_candles = interval_minutes == 1 and len(candles) <= 60 and any("T00:00:00" in str(c[0]) for c in candles)
    if is_daily_candles:
        day_vols = [safe_float(c[5], 0.0) for c in candles if len(c) > 5 and safe_float(c[5], 0.0) > 0]
        total_vol = sum(day_vols)
        num_days = len(day_vols)
        has_data = total_vol > 0 and num_days > 0
        d_avg = (total_vol / num_days) if has_data else 0.0
        avg_5m = (d_avg / 75.0) if has_data else 0.0  # 375 minutes in trading day / 5 = 75 slots
        avg_1m = (d_avg / 375.0) if has_data else 0.0
        return {
            "interval_minutes": 1,
            "total_candles": len(candles),
            "total_volume": round(total_vol, 0),
            "trading_days_found": num_days,
            "has_volume_data": has_data,
            "avg_candle_volume": round(avg_1m, 2),
            "avg_1m_volume": round(avg_1m, 2),
            "avg_5m_volume": round(avg_5m, 2),
            "daily_avg_volume": round(d_avg, 2),
            "time_slot_avg_volume": {},
            "time_slot_cum_volume": {},
        }

    volumes_by_time_slot: Dict[str, List[float]] = defaultdict(list)
    daily_volumes: Dict[str, float] = defaultdict(float)
    all_volumes: List[float] = []

    for c in candles:
        ts_str = str(c[0])
        vol = safe_float(c[5], 0.0) if len(c) > 5 else 0.0
        all_volumes.append(vol)

        try:
            sep = "T" if "T" in ts_str else " "
            dt_parts = ts_str.split(sep)
            day_str = dt_parts[0]
            time_part = dt_parts[1][:5]  # HH:MM
            volumes_by_time_slot[time_part].append(vol)
            daily_volumes[day_str] += vol
        except Exception:
            pass

    total_candles = len(all_volumes)
    total_volume = sum(all_volumes)
    avg_candle_volume = total_volume / total_candles if total_candles > 0 else 0.0

    time_slot_avg: Dict[str, float] = {}
    for t_slot in sorted(volumes_by_time_slot.keys()):
        slot_vals = volumes_by_time_slot[t_slot]
        time_slot_avg[t_slot] = round(sum(slot_vals) / len(slot_vals), 2)

    time_slot_cum: Dict[str, float] = {}
    cum_sum = 0.0
    for t_slot in sorted(time_slot_avg.keys()):
        cum_sum += time_slot_avg[t_slot]
        time_slot_cum[t_slot] = round(cum_sum, 2)

    unique_days_count = max(1, len(daily_volumes))
    daily_avg_volume = total_volume / unique_days_count if unique_days_count > 0 else 0.0

    avg_1m = avg_candle_volume if interval_minutes == 1 else avg_candle_volume / max(1, interval_minutes)
    avg_5m = avg_1m * 5.0
    has_data = bool(total_volume > 0 and total_candles > 0 and daily_avg_volume > 0)

    return {
        "interval_minutes": interval_minutes,
        "total_candles": total_candles,
        "total_volume": round(total_volume, 0),
        "trading_days_found": unique_days_count,
        "has_volume_data": has_data,
        "avg_candle_volume": round(avg_candle_volume, 2),
        "avg_1m_volume": round(avg_1m, 2),
        "avg_5m_volume": round(avg_5m, 2),
        "daily_avg_volume": round(daily_avg_volume, 2),
        "time_slot_avg_volume": time_slot_avg,
        "time_slot_cum_volume": time_slot_cum,
    }


def fetch_historical_candles(
    instrument_key: str,
    access_token: str,
    from_date: str,
    to_date: str,
    interval_unit: str = "1minute",
) -> Tuple[List[List[Any]], Optional[str]]:
    """Fetch historical candles via Upstox REST API v2, returning (candles, error_reason)."""
    encoded_key = urllib.parse.quote(instrument_key, safe="")

    # Upstox API allows at most 30 days for 1minute candles per call
    url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/{interval_unit}/{to_date}/{from_date}"
    err_reason = None

    try:
        data = http_get_json(url, access_token)
        if data.get("status") == "success" and "data" in data:
            candles = data["data"].get("candles", [])
            if candles:
                return candles, None
            err_reason = "Empty candles returned for 1-minute interval"
        else:
            err_reason = data.get("message") or str(data.get("errors", "Non-success status"))
    except Exception as e:
        err_reason = f"HTTP 1-minute candle exception: {e}"

    # If 1-minute call failed or was empty, fall back to daily candles to capture real daily volume
    if interval_unit == "1minute":
        try:
            day_url = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
            day_data = http_get_json(day_url, access_token)
            if day_data.get("status") == "success" and "data" in day_data:
                day_candles = day_data["data"].get("candles", [])
                if day_candles:
                    return day_candles, None
                err_reason = (err_reason + "; " if err_reason else "") + "Empty candles returned for daily interval"
            else:
                day_err = day_data.get("message") or str(day_data.get("errors", "Non-success status"))
                err_reason = (err_reason + "; " if err_reason else "") + f"Daily interval error: {day_err}"
        except Exception as e:
            err_reason = (err_reason + "; " if err_reason else "") + f"HTTP daily candle exception: {e}"

    return [], err_reason


def run_volume_indexer(
    instruments_csv: str = "data/filtered.csv",
    output_json: str = "data/index.json",
    days: int = 30,
    skip_if_exists: bool = True,
) -> Optional[str]:
    """
    Construct or validate 30-day volume index for all filtered equities.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)

    # 1. Check existing index - must have real historical candles (>0), not dummy fallbacks!
    min_required_len = 10 if ("mcx" in output_json.lower() or "mcx" in instruments_csv.lower()) else 150
    if os.path.exists(output_json) and skip_if_exists:
        try:
            with open(output_json, "r", encoding="utf-8") as f:
                idx = json.load(f)
            instruments_with_data = sum(
                1 for v in idx.values()
                if v.get("has_volume_data", True) and v.get("total_candles", 0) > 0 and v.get("daily_avg_volume", 0) > 0
            )
            if idx and len(idx) >= min_required_len and instruments_with_data >= min(len(idx) * 0.70, 100):
                print(f"  ✓ Valid Volume Index found at '{output_json}' with {len(idx):,} instruments ({instruments_with_data} with volume). Skipping rebuild.")
                return output_json
            else:
                print(f"  ⚠️ Existing volume index at '{output_json}' lacks historical volume ({instruments_with_data}/{len(idx)} valid). Rebuilding...")
        except Exception:
            print("  ⚠️ Existing volume index is corrupted. Rebuilding...")

    # 2. Load filtered instruments
    if not os.path.exists(instruments_csv):
        print(f"❌ Instruments CSV file not found: {instruments_csv}", file=sys.stderr)
        return None

    instruments: List[Dict[str, Any]] = []
    with open(instruments_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            instruments.append(r)

    if not instruments:
        print(f"❌ No instruments found in {instruments_csv}", file=sys.stderr)
        return None

    failure_log_path = os.path.join("logs", "volume_index_failures.log")
    token = get_access_token()
    if not token:
        print("❌ Cannot build volume index: UPSTOX_ACCESS_TOKEN not set in environment or .env", file=sys.stderr)
        os.makedirs("logs", exist_ok=True)
        with open(failure_log_path, "a", encoding="utf-8") as lf:
            lf.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [ABORT] Volume index build aborted: UPSTOX_ACCESS_TOKEN missing\n")
        return None

    today = date.today()
    # Stay within Upstox 30-day limit for 1-minute historical candles
    query_days = min(28, max(7, days))
    from_dt = today - timedelta(days=query_days)
    from_str = from_dt.strftime("%Y-%m-%d")
    to_str = today.strftime("%Y-%m-%d")

    print(f"  Building 30-Day Volume Index for {len(instruments):,} equities ({from_str} to {to_str})...")

    index_data: Dict[str, Any] = {}
    success_count = 0
    failures: List[str] = []

    for i, inst in enumerate(instruments, 1):
        key = inst.get("upstox_instrument_key") or inst.get("instrument_key") or ""
        sym = inst.get("trading_symbol") or inst.get("symbol") or ""
        name = inst.get("name") or sym

        if not key:
            continue

        print(f"  [{i}/{len(instruments)}] Fetching {sym} ({key})...", end="\r", flush=True)
        candles, err_reason = fetch_historical_candles(key, token, from_str, to_str, interval_unit="1minute")
        profile = compute_volume_profile(candles, interval_minutes=1)
        profile["symbol"] = sym
        profile["name"] = name
        profile["exchange"] = inst.get("exchange", "NSE")
        profile["lot_size"] = safe_int(inst.get("lot_size"), 1)
        profile["tick_size"] = safe_float(inst.get("tick_size"), 0.05)

        index_data[key] = profile
        if profile.get("has_volume_data"):
            success_count += 1
        else:
            reason = err_reason or "No candles returned by broker API"
            failures.append(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [FAILED] {sym} ({key}): {reason}")

        time.sleep(0.05)  # Rate limiting guard

    print(f"\n  ✓ Successfully indexed {success_count}/{len(instruments)} instruments with historical candle data.")
    if failures:
        os.makedirs("logs", exist_ok=True)
        with open(failure_log_path, "a", encoding="utf-8") as lf:
            for fl in failures:
                lf.write(fl + "\n")
        print(f"  ⚠️ {len(failures)} instruments lacked historical volume data (marked absent, logged to {failure_log_path}).")

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2)

    print(f"  ✓ Saved Volume Index to '{output_json}' ({os.path.getsize(output_json) / 1024:.1f} KB)")
    return output_json


def main():
    parser = argparse.ArgumentParser(description="Build 30-Day RVOL Historical Profile Index")
    parser.add_argument("--input", default="data/filtered.csv", help="Filtered instruments CSV")
    parser.add_argument("--output", default="data/index.json", help="Output volume index JSON")
    parser.add_argument("--days", type=int, default=30, help="Number of days to index")
    parser.add_argument("--force", action="store_true", help="Force rebuild even if index exists")
    args = parser.parse_args()

    res = run_volume_indexer(
        instruments_csv=args.input,
        output_json=args.output,
        days=args.days,
        skip_if_exists=not args.force,
    )
    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()

