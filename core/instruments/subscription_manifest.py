#!/usr/bin/env python3
"""
core/instruments/subscription_manifest.py
=========================================
Generates the comprehensive multi-stream subscription manifest for:
1. Real-time Dual WebSocket streaming:
   - All ~215 F&O underlying equities (NSE_EQ|INE...) + NIFTY 50 index.
   - Popular option strikes: 3 strikes above and 3 strikes below ATM for CE & PE
     on the nearest expiry (~14 contracts per stock).
   - Capacity Budget: 215 stocks * 14 contracts = 3,010 options + 215 equities = 3,225 keys.
     Sharded 50/50 across dual Upstox WebSockets (~1,612 keys/conn, well below 2,000 limit).
2. Periodic background REST polling:
   - All remaining strikes across the option chain (~27,800+ contracts) partitioned
     into 400-key batches for session archival and historical depth analysis.
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

from core.common.logging_utils import get_logger

_logger = get_logger("PREMARKET.MANIFEST")


def run_manifest_generator(
    filtered_csv: str = "data/filtered.csv",
    volume_index_json: str = "data/index.json",
    master_csv: str = "data/instrument.csv",
    output_json: str = "data/subscription_manifest.json",
    popular_strikes: int = 3,
    max_stream_underlyings_with_options: Optional[int] = None,
    skip_if_exists: bool = False,
    session_date: Optional[str] = None,
) -> Optional[str]:
    """
    Generate subscription_manifest.json containing underlying streaming keys,
    popular option streaming keys, and background polling batches.
    Strategy B: Options are streamed for the top N underlyings (or full universe if configured)
    partitioned across parallel WebSocket connections (each staying below the 2,000 Upstox limit).
    Remaining options are background polled.
    """
    if skip_if_exists and os.path.exists(output_json) and os.path.getsize(output_json) > 10_000:
        _logger.info(f"Valid Subscription Manifest found at '{output_json}'. Skipping generation.")
        return output_json

    if not os.path.exists(filtered_csv):
        _logger.error(f"Filtered equities file not found: {filtered_csv}")
        return None

    if not os.path.exists(master_csv):
        _logger.error(f"Master instruments file not found: {master_csv}")
        return None

    # Load configured max_stream_underlyings_with_options if not explicitly provided
    if max_stream_underlyings_with_options is None:
        try:
            sys_cfg_path = "config/system.json"
            if os.path.exists(sys_cfg_path):
                with open(sys_cfg_path, "r", encoding="utf-8") as f:
                    cfg_obj = json.load(f)
                    sys_dict = cfg_obj.get("system", {})
                    max_stream_underlyings_with_options = (
                        sys_dict.get("manifest", {}).get("max_stream_underlyings_with_options")
                        or sys_dict.get("feed", {}).get("max_stream_underlyings_with_options")
                    )
        except Exception:
            pass
        if max_stream_underlyings_with_options is None:
            max_stream_underlyings_with_options = 110
    else:
        max_stream_underlyings_with_options = int(max_stream_underlyings_with_options)

    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
    _logger.info(f"Generating Subscription Manifest from '{filtered_csv}' & '{master_csv}' (Max Option Underlyings: {max_stream_underlyings_with_options})...")

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
    equity_last_prices: Dict[str, float] = {}
    today_str = date.today().strftime("%Y-%m-%d")

    with open(master_csv, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            exch = (row.get("exchange") or "").strip().upper()
            seg = (row.get("segment") or "").strip().upper()
            itype = (row.get("option_type") or row.get("instrument_type") or "").strip().upper()
            ts = (row.get("tradingsymbol") or row.get("trading_symbol") or "").strip().upper()
            und_sym = (row.get("underlying_symbol") or row.get("name") or "").strip().upper()

            # Record underlying equity last traded / closing price
            if itype == "EQUITY" or seg in ("EQ", ""):
                sym_cand = ts if ts in equities else (und_sym if und_sym in equities else None)
                if sym_cand:
                    try:
                        lp = float(row.get("last_price") or 0.0)
                        if lp > 0:
                            equity_last_prices[sym_cand] = lp
                    except Exception:
                        pass

            if seg != "FNO" and not ("_FO" in exch or exch in ("MCX", "NSE")):
                continue

            if itype not in ("CE", "PE"):
                continue
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

    # 3. Strategy B: Rank underlyings by 30-day baseline turnover / volume from data/index.json
    volume_scores: Dict[str, float] = {}
    if volume_index_json and os.path.exists(volume_index_json):
        try:
            with open(volume_index_json, "r", encoding="utf-8") as f:
                vdata = json.load(f)
            for s_key, eq_i in equities.items():
                u_key = eq_i.get("upstox_instrument_key", "")
                v_entry = vdata.get(u_key) or vdata.get(s_key) or {}
                d_vol = float(v_entry.get("daily_avg_volume", 0.0) or v_entry.get("total_volume", 0.0) or 0.0)
                l_p = float(equity_last_prices.get(s_key, 0.0))
                # Turnover in INR (daily volume * price) or pure volume fallback
                score = (d_vol * l_p) if (d_vol > 0 and l_p > 0) else d_vol
                volume_scores[s_key] = score
        except Exception as e:
            _logger.warning(f"Warning reading volume index: {e}")

    # Underlyings that actually have options
    underlyings_with_options = [s for s in equities if options_by_underlying.get(s)]
    # Sort by turnover / volume descending (most liquid first)
    ranked_underlyings = sorted(
        underlyings_with_options,
        key=lambda s: (volume_scores.get(s, 0.0), s),
        reverse=True,
    )
    # The top N underlyings get their popular options streamed via WebSocket
    stream_options_symbols: Set[str] = set(ranked_underlyings[:max_stream_underlyings_with_options])

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
                "options_streamed": False,
                "turnover_score": volume_scores.get(sym, 0.0),
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

        # Determine ATM strike using equity's last traded / close price if available
        baseline_price = equity_last_prices.get(sym, 0.0)
        if baseline_price > 0:
            atm_idx = min(range(len(sorted_strikes)), key=lambda i: abs(sorted_strikes[i] - baseline_price))
        else:
            atm_idx = len(sorted_strikes) // 2

        start_idx = max(0, atm_idx - popular_strikes)
        end_idx = min(len(sorted_strikes), atm_idx + popular_strikes + 1)
        selected_strikes = set(sorted_strikes[start_idx:end_idx])

        # Segregate into popular (streamed on WS) and remaining (polled in background)
        # Strategy B: Only stream options for Top N underlyings in stream_options_symbols
        should_stream_options = (sym in stream_options_symbols)

        for o in opt_list:
            opt_key = o.get("_opt_key", "")
            if not opt_key:
                continue
            exp = o.get("_exp", "")
            s_price = o.get("_strike", 0.0)
            itype = o.get("_itype", "")
            opt_ts = (o.get("tradingsymbol") or o.get("trading_symbol") or "").strip()
            opt_name_disp = f"{eq_display} {s_price:.0f} {itype}"

            if should_stream_options and exp == nearest_expiry and s_price in selected_strikes:
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
            "options_streamed": should_stream_options,
            "turnover_score": volume_scores.get(sym, 0.0),
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
            "max_stream_underlyings_with_options": max_stream_underlyings_with_options,
            "underlyings_with_streamed_options": len(stream_options_symbols),
            "total_streamed_ws_keys": len(underlying_keys) + len(popular_option_keys),
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

    _logger.info(
        f"Saved Subscription Manifest to '{output_json}' | Underlyings={len(underlying_keys):,}, "
        f"PopularOptions={len(popular_option_keys):,} ({len(stream_options_symbols)} underlyings), "
        f"StreamedWS={len(underlying_keys) + len(popular_option_keys):,}, "
        f"PollBatches={len(background_poll_batches)} ({len(background_poll_keys):,} keys), "
        f"TotalTracked={manifest['summary']['total_tracked_instruments']:,}"
    )
    return output_json


def main():
    parser = argparse.ArgumentParser(description="Generate Multi-Stream Subscription Manifest")
    parser.add_argument("--filtered", default="data/filtered.csv", help="Filtered equities CSV")
    parser.add_argument("--index", default="data/index.json", help="Volume index JSON")
    parser.add_argument("--master", default="data/instrument.csv", help="Master instruments CSV")
    parser.add_argument("--output", default="data/subscription_manifest.json", help="Output manifest JSON")
    parser.add_argument("--strikes", type=int, default=3, help="Popular strikes above/below ATM (default: 3)")
    parser.add_argument("--max-options-underlyings", type=int, default=None, help="Top N liquid underlyings to stream options for (default from system.json or 110)")
    args = parser.parse_args()

    res = run_manifest_generator(
        filtered_csv=args.filtered,
        volume_index_json=args.index,
        master_csv=args.master,
        output_json=args.output,
        popular_strikes=args.strikes,
        max_stream_underlyings_with_options=args.max_options_underlyings,
    )
    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()

