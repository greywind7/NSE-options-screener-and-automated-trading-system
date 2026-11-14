#!/usr/bin/env python3
"""
build_volume_index.py
=====================
Script 2 in the RVOL Workflow:
Loads filtered instruments from filter_instruments.py and queries Upstox API v3
to fetch 30 days of historical candle data (defaulting to 1-minute, configurable to 5m, 15m, etc.).

Computes a granular volume profile for each instrument:
1. Overall 30-day average candle volume (and normalized 1m/5m averages).
2. Time-of-day volume profile (average volume for 09:15, 09:16, ..., 15:29).
3. Cumulative expected volume up to each minute of the day.
4. Average daily total volume and median candle volume.

Saves the output to `volume_index.json` for consumption by the live RVOL scanner.

Usage:
    # Build 1-minute volume index (default):
    python3 build_volume_index.py --input filtered.csv --target-segment CASH

    # Build 5-minute volume index:
    python3 build_volume_index.py --input filtered.csv --target-segment CASH --interval 5

    # Dump raw API requests & responses to debug.log:
    python3 build_volume_index.py --input filtered.csv --target-segment CASH --debug
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from upstox_common import get_access_token, http_get_json, load_input_instruments, log_debug, safe_float


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
            "avg_candle_volume": 0.0,
            "avg_1m_volume": 0.0,
            "avg_5m_volume": 0.0,
            "daily_avg_volume": 0.0,
            "time_slot_avg_volume": {},
            "time_slot_cum_volume": {},
        }

    # Group volumes by date and by time of day (HH:MM)
    volumes_by_time_slot: Dict[str, List[float]] = defaultdict(list)
    daily_volumes: Dict[str, float] = defaultdict(float)
    all_volumes: List[float] = []

    for c in candles:
        # candle structure: [timestamp, open, high, low, close, volume, oi]
        ts_str = str(c[0])
        vol = safe_float(c[5], 0.0) if len(c) > 5 else 0.0

        all_volumes.append(vol)

        # Parse timestamp: e.g. "2026-10-24T09:15:00+05:30" or "2026-10-24 09:15:00"
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

    # Compute average volume for each time slot (e.g. "09:15": 12450.0)
    time_slot_avg: Dict[str, float] = {}
    for t_slot in sorted(volumes_by_time_slot.keys()):
        slot_vals = volumes_by_time_slot[t_slot]
        time_slot_avg[t_slot] = round(sum(slot_vals) / len(slot_vals), 2)

    # Compute expected cumulative volume curve up to each time slot
    time_slot_cum: Dict[str, float] = {}
    cum_sum = 0.0
    for t_slot in sorted(time_slot_avg.keys()):
        cum_sum += time_slot_avg[t_slot]
        time_slot_cum[t_slot] = round(cum_sum, 2)

    # Daily average volume
    num_days = max(1, len(daily_volumes))
    daily_avg_volume = round(sum(daily_volumes.values()) / num_days, 2)

    # Compute median candle volume
    sorted_vols = sorted(all_volumes)
    mid = total_candles // 2
    median_candle_volume = sorted_vols[mid] if total_candles > 0 else 0.0

    # Compute normalized averages for 1m and 5m
    avg_1m = avg_candle_volume if interval_minutes == 1 else (avg_candle_volume / interval_minutes if interval_minutes > 0 else 0.0)
    avg_5m = (avg_candle_volume * 5) if interval_minutes == 1 else avg_candle_volume

    return {
        "interval_minutes": interval_minutes,
        "total_candles": total_candles,
        "days_count": num_days,
        "total_volume": round(total_volume, 2),
        "avg_candle_volume": round(avg_candle_volume, 2),
        "avg_1m_volume": round(avg_1m, 2),
        "avg_5m_volume": round(avg_5m, 2),
        "median_candle_volume": round(median_candle_volume, 2),
        "daily_avg_volume": daily_avg_volume,
        "time_slot_avg_volume": time_slot_avg,
        "time_slot_cum_volume": time_slot_cum,
    }


def fetch_instrument_candles(
    instrument_key: str,
    access_token: str,
    from_date: str,
    to_date: str,
    interval_minutes: int = 1,
    debug_file: Optional[str] = None,
) -> List[List[Any]]:
    """
    Fetch historical candles from Upstox API v3 (with automatic fallback to v2):
    GET /v3/historical-candle/{instrument_key}/minutes/{interval}/{to_date}/{from_date}
    """
    encoded_key = urllib.parse.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v3/historical-candle/{encoded_key}/minutes/{interval_minutes}/{to_date}/{from_date}"

    try:
        response_json = http_get_json(url, access_token, debug_file=debug_file)
        if response_json.get("status") == "success":
            data = response_json.get("data", {})
            candles = data.get("candles", [])
            if debug_file:
                log_debug(f"V3 returned {len(candles)} candles for {instrument_key}", debug_file)
            return candles
        elif debug_file:
            log_debug(f"V3 non-success response for {instrument_key}: {response_json}", debug_file)
    except Exception as e:
        if debug_file:
            log_debug(f"V3 fetch error for {instrument_key}: {e}", debug_file)

        # Fallback for 1-minute interval: Upstox v2 endpoint /v2/historical-candle/{key}/1minute/{to_date}/{from_date}
        if interval_minutes == 1:
            url_v2 = f"https://api.upstox.com/v2/historical-candle/{encoded_key}/1minute/{to_date}/{from_date}"
            try:
                response_v2 = http_get_json(url_v2, access_token, debug_file=debug_file)
                if response_v2.get("status") == "success":
                    data = response_v2.get("data", {})
                    candles_v2 = data.get("candles", [])
                    if debug_file:
                        log_debug(f"V2 fallback returned {len(candles_v2)} candles for {instrument_key}", debug_file)
                    return candles_v2
            except Exception as e2:
                if debug_file:
                    log_debug(f"V2 fallback also failed for {instrument_key}: {e2}", debug_file)

    return []


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build 30-day historical volume index (1-min or 5-min candles) using Upstox API v3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build 1-minute volume index (default interval: 1 minute):
  python3 build_volume_index.py --input fno_cash_stocks.csv

  # Build 5-minute volume index:
  python3 build_volume_index.py --input fno_cash_stocks.csv --interval 5

  # Run with debug logging to dump raw API responses into debug.log:
  python3 build_volume_index.py --input fno_cash_stocks.csv --debug

  # Specify custom days and output file:
  python3 build_volume_index.py --input filtered.csv --target-segment CASH --interval 1 --days 30 --output volume_index.json

  # Inspect an already built volume index:
  python3 build_volume_index.py --summary --output volume_index.json
        """,
    )

    parser.add_argument("--input", "-i", help="Path to filtered instruments file (.csv, .json, or keys.txt)")
    parser.add_argument("--output", "-o", default="volume_index.json", help="Path to save volume index (default: volume_index.json)")
    parser.add_argument("--interval", "-m", type=int, default=1, help="Candle interval in minutes: 1, 5, 15, 30, etc. (default: 1)")
    parser.add_argument("--days", "-d", type=int, default=30, help="Number of historical days to analyze (default: 30)")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay in seconds between API requests (default: 0.15)")
    parser.add_argument(
        "--target-segment",
        "-s",
        default="CASH",
        choices=["CASH", "EQ", "FUT"],
        help="When input has FNO underlyings, specify segment to analyze: CASH (stock equity) or FUT (near-month future) (default: CASH)",
    )
    parser.add_argument("--csv", default="instrument.csv", help="Path to master instrument.csv for token resolution (default: instrument.csv)")
    parser.add_argument("--debug", action="store_true", help="Dump raw API requests and responses to debug.log")
    parser.add_argument("--debug-file", default="debug.log", help="Path to debug log file (default: debug.log)")
    parser.add_argument("--summary", action="store_true", help="Print summary of an existing volume index file")

    args = parser.parse_args()

    # 1. Summary mode for existing index
    if args.summary:
        if not os.path.exists(args.output):
            print(f"Error: Volume index file not found at: {args.output}", file=sys.stderr)
            sys.exit(1)
        with open(args.output, "r", encoding="utf-8") as f:
            index = json.load(f)
        print("=" * 75)
        print(f"VOLUME INDEX SUMMARY: {args.output}")
        print("=" * 75)
        print(f"Total Indexed Instruments: {len(index):,}\n")
        print(f"{'Symbol / Key':<26} | {'Tf':>3} | {'Days':>5} | {'Candles':>8} | {'Avg Candle Vol':>14} | {'Daily Avg Vol':>14}")
        print("-" * 82)
        for key, info in list(index.items())[:25]:
            sym = info.get("symbol") or key
            tf = f"{info.get('interval_minutes', 1)}m"
            avg_vol = info.get("avg_candle_volume", info.get("avg_5m_volume", 0.0))
            print(f"{sym[:26]:<26} | {tf:>3} | {info.get('days_count', 0):>5} | {info.get('total_candles', 0):>8} | {avg_vol:>14,.0f} | {info.get('daily_avg_volume', 0.0):>14,.0f}")
        if len(index) > 25:
            print(f"... and {len(index) - 25} more instruments.")
        return

    if not args.input:
        print("Error: Please provide an input file using --input (e.g. --input fno_cash_stocks.csv)", file=sys.stderr)
        sys.exit(1)

    # 2. Setup Debug Logging
    debug_log_path = args.debug_file if args.debug else None
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\n[DEBUG SESSION STARTED: {datetime.now().isoformat()}] Interval={args.interval}m Days={args.days}\n{'='*70}\n")
        print(f"Debug mode enabled: Raw API responses will be dumped to '{debug_log_path}'")

    # 3. Load access token from .env
    token = get_access_token()

    # 4. Load instruments (with automatic token resolution)
    print(f"Loading instruments from {args.input} (Target Segment: {args.target_segment})...")
    instruments = load_input_instruments(
        args.input,
        target_segment=args.target_segment,
        master_csv_path=args.csv,
    )
    print(f"Resolved {len(instruments):,} tradable instruments with Upstox keys.")

    # 5. Calculate date range (past N days)
    today = date.today()
    from_dt = today - timedelta(days=args.days)
    to_date_str = today.strftime("%Y-%m-%d")
    from_date_str = from_dt.strftime("%Y-%m-%d")

    print(f"Fetching {args.interval}-minute historical candles from {from_date_str} to {to_date_str} ({args.days} days)...\n")

    volume_index: Dict[str, Any] = {}
    total = len(instruments)
    successful = 0
    empty_or_failed = 0

    start_time = time.time()

    for idx, inst in enumerate(instruments, 1):
        key = inst["upstox_instrument_key"]
        display_label = inst.get("trading_symbol") or inst.get("name") or key

        # Terminal progress output
        percent = (idx / total) * 100
        sys.stdout.write(f"\r[{idx}/{total} - {percent:5.1f}%] Fetching ({args.interval}m): {display_label[:28]:<28} ")
        sys.stdout.flush()

        candles = fetch_instrument_candles(
            key,
            token,
            from_date_str,
            to_date_str,
            interval_minutes=args.interval,
            debug_file=debug_log_path,
        )

        if candles:
            profile = compute_volume_profile(candles, interval_minutes=args.interval)
            profile["symbol"] = inst.get("trading_symbol", "")
            profile["name"] = inst.get("name") or inst.get("display_name", "")
            profile["exchange"] = inst.get("exchange", "")
            profile["segment"] = inst.get("segment", "")
            profile["instrument_type"] = inst.get("instrument_type", "")
            profile["strike_price"] = inst.get("strike_price", "")
            profile["expiry_date"] = inst.get("expiry_date", "")
            profile["lot_size"] = inst.get("lot_size", 1)
            profile["upstox_instrument_key"] = key
            profile["indexed_at"] = datetime.now().isoformat()
            profile["from_date"] = from_date_str
            profile["to_date"] = to_date_str

            volume_index[key] = profile
            successful += 1
            if debug_log_path:
                log_debug(f"Successfully computed volume profile for {key} ({len(candles)} candles)", debug_log_path)
        else:
            empty_or_failed += 1
            if debug_log_path:
                log_debug(f"No candles returned for {key} ({display_label})", debug_log_path)

        # Rate-limiting delay
        if args.delay > 0:
            time.sleep(args.delay)

    sys.stdout.write("\n\n")

    # 5. Save volume index to output JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(volume_index, f, indent=2)

    elapsed = time.time() - start_time
    print("=" * 65)
    print(f"HISTORICAL VOLUME INDEX BUILD COMPLETE in {elapsed:.1f}s")
    print("=" * 65)
    print(f"Total Instruments Processed : {total:,}")
    print(f"Successfully Indexed        : {successful:,}")
    print(f"No Data / Inactive Contracts: {empty_or_failed:,}")
    print(f"Volume Index Saved To       : {args.output}")
    print("=" * 65)
    print(f"\nNext step: Run 'python3 stream_rvol_scanner.py --index {args.output}' to start live RVOL monitoring!")


if __name__ == "__main__":
    main()
