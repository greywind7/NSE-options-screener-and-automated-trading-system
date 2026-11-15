#!/usr/bin/env python3
"""
infrastructure/instruments/subscription_manifest.py
===================================================
Generates the comprehensive multi-stream subscription manifest for:
1. Real-time WebSocket streaming:
   - All ~213 F&O underlying equities (NSE_EQ|INE...).
   - Popular option strikes: 3 strikes above and 3 strikes below ATM for CE & PE
     on the nearest expiry (~14 contracts per stock).
2. Periodic background REST polling:
   - All remaining strikes across the option chain for raw session coagulated recording.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple


def run_manifest_generator(
    filtered_csv: str = "data/filtered.csv",
    volume_index_json: str = "data/index.json",
    master_csv: str = "data/instrument.csv",
    output_json: str = "data/subscription_manifest.json",
    popular_strikes: int = 3,
    skip_if_exists: bool = False,
    session_date: Optional[str] = None,
) -> Optional[str]:
    """
    Generate subscription_manifest.json containing underlying streaming keys,
    popular option streaming keys, and background polling batches.
    """
    if skip_if_exists and os.path.exists(output_json) and os.path.getsize(output_json) > 10_000:
        print(f"  ✓ Valid Subscription Manifest found at '{output_json}'. (Skipping generation)")
        return output_json

    if not os.path.exists(filtered_csv):
        print(f"❌ Filtered equities file not found: {filtered_csv}", file=sys.stderr)
        return None

    if not os.path.exists(master_csv):
        print(f"❌ Master instruments file not found: {master_csv}", file=sys.stderr)
        return None

    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
    print(f"  📝 Generating Subscription Manifest from '{filtered_csv}' & '{master_csv}'...")

    # 1. Load Filtered Equities
    equities: Dict[str, Dict[str, Any]] = {}
    with open(filtered_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            sym = (r.get("trading_symbol") or "").strip().upper()
            if sym:
                equities[sym] = r

    # 2. Scan Master CSV for FNO Options matching our filtered equities
    options_by_underlying: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    today_str = date.today().strftime("%Y-%m-%d")

    with open(master_csv, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            exch = (row.get("exchange") or "").strip().upper()
            seg = (row.get("segment") or "").strip().upper()
            if seg != "FNO" and not ("_FO" in exch or exch in ("MCX", "NSE")):
                continue

            itype = (row.get("option_type") or row.get("instrument_type") or "").strip().upper()
            if itype not in ("CE", "PE"):
                continue

            ts = (row.get("tradingsymbol") or row.get("trading_symbol") or "").strip().upper()
            und_sym = (row.get("underlying_symbol") or row.get("name") or "").strip().upper()
            matched_sym = None
            if und_sym in equities:
                matched_sym = und_sym
            else:
                m = re.match(r"^([A-Z0-9_\-]+?)\d{2}[A-Z]{3}", ts)
                if m and m.group(1) in equities:
                    matched_sym = m.group(1)
                else:
                    for k in equities:
                        if k == und_sym or (equities[k].get("name") or "").upper() == und_sym:
                            matched_sym = k
                            break

            if not matched_sym:
                continue

            exp = (row.get("expiry_date") or row.get("expiry") or "").strip()
            if exp and exp >= today_str:
                tok = (row.get("exchange_token") or "").strip()
                prefix = "MCX_FO" if "MCX" in exch else "NSE_FO"
                try:
                    s_price = float(row.get("strike_price") or row.get("strike") or 0.0)
                except Exception:
                    s_price = 0.0

                row["_matched_sym"] = matched_sym
                row["_exp"] = exp
                row["_strike"] = s_price
                row["_itype"] = itype
                row["_opt_key"] = row.get("instrument_key") or f"{prefix}|{tok}"
                options_by_underlying[matched_sym].append(row)

    underlying_keys: List[str] = []
    popular_option_keys: List[str] = []
    background_poll_keys: List[str] = []

    symbol_metadata: Dict[str, Any] = {}
    options_map_by_symbol: Dict[str, Dict[str, Any]] = {}

    instrument_name_map: Dict[str, str] = {}
    streamed_popular_options: List[Dict[str, Any]] = []

    for sym, eq_info in sorted(equities.items()):
        eq_key = eq_info.get("upstox_instrument_key", "")
        eq_exch = eq_info.get("exchange", "")
        # Human-readable underlying name (e.g. CRUDE OIL, NATURALGAS, RELIANCE)
        eq_display = (eq_info.get("name") or sym).strip() if "MCX" in eq_exch else sym
        if eq_key:
            underlying_keys.append(eq_key)
            instrument_name_map[eq_key] = eq_display
            instrument_name_map[eq_key.replace(":", "|")] = eq_display
            instrument_name_map[eq_key.replace("|", ":")] = eq_display

        opt_list = options_by_underlying.get(sym, [])
        if not opt_list:
            symbol_metadata[sym] = {
                "underlying_key": eq_key,
                "name": eq_display,
                "lot_size": int(eq_info.get("lot_size", 1)),
                "tick_size": float(eq_info.get("tick_size", 0.05)),
                "has_options": False,
            }
            continue

        # Find nearest expiry
        expiries = sorted(list({o.get("_exp", "") for o in opt_list if o.get("_exp")}))
        nearest_expiry = expiries[0] if expiries else ""

        # Group nearest expiry contracts by strike and type
        strikes_set: Set[float] = set()
        contracts_by_strike_type: Dict[float, Dict[str, str]] = defaultdict(dict)

        for o in opt_list:
            if o.get("_exp") == nearest_expiry:
                s_price = o.get("_strike", 0.0)
                itype = o.get("_itype", "")
                opt_key = o.get("_opt_key", "")
                if s_price > 0 and opt_key:
                    strikes_set.add(s_price)
                    contracts_by_strike_type[s_price][itype] = opt_key

        sorted_strikes = sorted(list(strikes_set))

        # Determine middle strike as baseline (middle of strike chain)
        mid_idx = len(sorted_strikes) // 2
        start_idx = max(0, mid_idx - popular_strikes)
        end_idx = min(len(sorted_strikes), mid_idx + popular_strikes + 1)
        selected_strikes = set(sorted_strikes[start_idx:end_idx])

        # Segregate into popular (streamed on WS) and remaining (polled in background)
        for o in opt_list:
            opt_key = o.get("_opt_key", "")
            if not opt_key:
                continue
            exp = o.get("_exp", "")
            s_price = o.get("_strike", 0.0)
            itype = o.get("_itype", "")
            opt_ts = (o.get("tradingsymbol") or o.get("trading_symbol") or "").strip()
            opt_name_disp = f"{eq_display} {s_price:.0f} {itype}"

            if exp == nearest_expiry and s_price in selected_strikes:
                popular_option_keys.append(opt_key)
                instrument_name_map[opt_key] = opt_name_disp
                instrument_name_map[opt_key.replace(":", "|")] = opt_name_disp
                instrument_name_map[opt_key.replace("|", ":")] = opt_name_disp
                streamed_popular_options.append({
                    "instrument_key": opt_key,
                    "trading_symbol": opt_ts,
                    "name": opt_name_disp,
                    "underlying": sym,
                })
            else:
                background_poll_keys.append(opt_key)
                instrument_name_map[opt_key] = opt_name_disp
                instrument_name_map[opt_key.replace(":", "|")] = opt_name_disp
                instrument_name_map[opt_key.replace("|", ":")] = opt_name_disp

        symbol_metadata[sym] = {
            "underlying_key": eq_key,
            "name": eq_display,
            "lot_size": int(eq_info.get("lot_size", 1)),
            "tick_size": float(eq_info.get("tick_size", 0.05)),
            "nearest_expiry": nearest_expiry,
            "all_expiries": expiries,
            "popular_strikes": sorted(list(selected_strikes)),
            "total_options": len(opt_list),
            "has_options": True,
        }

        options_map_by_symbol[sym] = {
            "nearest_expiry": nearest_expiry,
            "strikes": {str(k): v for k, v in contracts_by_strike_type.items()},
        }

    # Chunk background polling keys into batches of 400 instruments (well within Upstox 500 limit)
    batch_size = 400
    background_poll_batches: List[List[str]] = [
        background_poll_keys[i : i + batch_size]
        for i in range(0, len(background_poll_keys), batch_size)
    ]

    streamed_underlyings = [
        {"instrument_key": meta["underlying_key"], "symbol": sym, "name": meta.get("name", sym)}
        for sym, meta in symbol_metadata.items()
        if meta.get("underlying_key")
    ]

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "session_date": session_date or date.today().strftime("%Y%m%d"),
        "summary": {
            "total_underlying_equities": len(underlying_keys),
            "total_popular_options_streamed": len(popular_option_keys),
            "total_background_options_polled": len(background_poll_keys),
            "total_background_batches": len(background_poll_batches),
            "total_tracked_instruments": len(underlying_keys) + len(popular_option_keys) + len(background_poll_keys),
        },
        "streamed_underlyings": streamed_underlyings,
        "streamed_popular_options": streamed_popular_options,
        "underlying_keys": underlying_keys,
        "popular_option_keys": popular_option_keys,
        "background_poll_batches": background_poll_batches,
        "symbol_metadata": symbol_metadata,
        "options_map": options_map_by_symbol,
        "instrument_name_map": instrument_name_map,
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"  ✓ Saved Subscription Manifest to '{output_json}'")
    print(f"    • Underlyings Streamed      : {len(underlying_keys):,}")
    print(f"    • Popular Options Streamed  : {len(popular_option_keys):,} (3 above/below ATM)")
    print(f"    • Background Options Polled : {len(background_poll_keys):,} across {len(background_poll_batches)} batches")
    print(f"    • Total Contracts Tracked   : {manifest['summary']['total_tracked_instruments']:,}")
    return output_json


def main():
    parser = argparse.ArgumentParser(description="Generate Multi-Stream Subscription Manifest")
    parser.add_argument("--filtered", default="data/filtered.csv", help="Filtered equities CSV")
    parser.add_argument("--index", default="data/index.json", help="Volume index JSON")
    parser.add_argument("--master", default="data/instrument.csv", help="Master instruments CSV")
    parser.add_argument("--output", default="data/subscription_manifest.json", help="Output manifest JSON")
    parser.add_argument("--strikes", type=int, default=3, help="Popular strikes above/below ATM (default: 3)")
    args = parser.parse_args()

    res = run_manifest_generator(
        filtered_csv=args.filtered,
        volume_index_json=args.index,
        master_csv=args.master,
        output_json=args.output,
        popular_strikes=args.strikes,
    )
    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()

