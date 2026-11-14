#!/usr/bin/env python3
"""
infrastructure/instruments/instrument_filter.py
===============================================
Filters the master database to resolve the complete F&O-eligible Equity universe
(~210-220 underlying stocks on the National Stock Exchange of India).

Features:
- Fast multi-pass streaming CSV parser (zero external dependencies).
- Identifies underlying equities with active derivative (Option/Futures) listings.
- Resolves proper Upstox instrument keys:
    Equities: NSE_EQ|INE...
    Options : NSE_FO|exchange_token
    Futures : NSE_FO|exchange_token
- Preserves contract metadata: lot_size, tick_size, freeze_qty, trading_symbol, ISIN.
- Generates data/filtered.csv for downstream volume indexing and manifest creation.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class EquityInstrument:
    exchange: str
    exchange_token: str
    trading_symbol: str
    name: str
    isin: str
    lot_size: int
    tick_size: float
    upstox_instrument_key: str
    total_fno_contracts: int = 0
    available_expiries: List[str] = None
    min_strike: float = 0.0
    max_strike: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d["available_expiries"] is None:
            d["available_expiries"] = []
        return d


def run_instrument_filter(
    master_csv: str = "data/instrument.csv",
    output_csv: str = "data/filtered.csv",
    target_exchange: str = "NSE",
    skip_if_exists: bool = False,
) -> Optional[str]:
    """
    Parse master_csv, isolate equities that have active F&O contracts,
    and write to output_csv.
    """
    if skip_if_exists and os.path.exists(output_csv) and os.path.getsize(output_csv) > 1000:
        print(f"  ✓ Found existing Filtered Equities at '{output_csv}' ({os.path.getsize(output_csv) / 1024:.1f} KB). (Skipping filtering)")
        return output_csv

    if not os.path.exists(master_csv):
        print(f"❌ Master CSV file not found: {master_csv}", file=sys.stderr)
        return None

    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    print(f"  🔍 Scanning instrument master '{master_csv}' for active F&O contracts...")

    if target_exchange.upper() == "MCX":
        # Extract active commodity futures and options
        from datetime import date
        today_str = date.today().strftime("%Y-%m-%d")
        comm_futs = defaultdict(list)
        comm_opts = defaultdict(list)

        with open(master_csv, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("exchange") != "MCX_FO":
                    continue
                exp = row.get("expiry") or row.get("expiry_date") or ""
                if not exp or exp < today_str:
                    continue
                itype = (row.get("instrument_type") or "").strip().upper()
                name = (row.get("name") or "").strip().upper()
                if not name:
                    continue
                if itype == "FUTCOM":
                    comm_futs[name].append(row)
                elif itype in ("OPTFUT", "CE", "PE"):
                    comm_opts[name].append(row)

        mcx_instruments = []
        for comm, fut_list in sorted(comm_futs.items()):
            # Filter for flagship benchmark contract: clean_name + 2 digits + 3 letters + FUT
            # e.g., SILVER26DECFUT (lot 30) over SILVER100/SILVERM/SILVERMIC, GOLD26DECFUT (lot 1) over GOLDPETAL/GOLDGUINEA/GOLDM
            clean_name = re.sub(r"[^A-Z0-9]", "", comm.upper())
            benchmark_pattern = re.compile(rf"^{clean_name}\d{{2}}[A-Z]{{3}}FUT$")
            flagship_contracts = [
                f for f in fut_list
                if benchmark_pattern.match((f.get("tradingsymbol") or f.get("trading_symbol") or "").strip().upper())
            ]
            candidates = flagship_contracts if flagship_contracts else fut_list
            candidates.sort(key=lambda x: x.get("expiry") or x.get("expiry_date") or "")
            nearest = candidates[0]
            opts = comm_opts.get(comm, [])
            strikes = []
            for o in opts:
                s_raw = o.get("strike") or o.get("strike_price")
                try:
                    if s_raw:
                        strikes.append(float(s_raw))
                except Exception:
                    pass

            mcx_instruments.append({
                "exchange": "MCX_FO",
                "exchange_token": nearest.get("exchange_token", ""),
                "trading_symbol": nearest.get("tradingsymbol") or nearest.get("trading_symbol") or comm,
                "name": comm,
                "isin": "",
                "lot_size": int(nearest.get("lot_size") or 1),
                "tick_size": float(nearest.get("tick_size") or 0.05),
                "upstox_instrument_key": nearest.get("instrument_key") or f"MCX_FO|{nearest.get('exchange_token', '')}",
                "total_fno_contracts": len(opts) + len(fut_list),
                "available_expiries": sorted(list({o.get("expiry") or o.get("expiry_date") for o in opts if o.get("expiry") or o.get("expiry_date")})),
                "min_strike": min(strikes) if strikes else 0.0,
                "max_strike": max(strikes) if strikes else 0.0,
            })

        fieldnames = [
            "exchange", "exchange_token", "trading_symbol", "name", "isin",
            "lot_size", "tick_size", "upstox_instrument_key", "total_fno_contracts",
            "available_expiries", "min_strike", "max_strike",
        ]
        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for item in mcx_instruments:
                row_copy = dict(item)
                row_copy["available_expiries"] = ";".join(row_copy["available_expiries"])
                writer.writerow(row_copy)

        print(f"  ✓ Successfully filtered and saved {len(mcx_instruments):,} MCX commodities to '{output_csv}'")
        return output_csv

    # Pass 1: Identify underlying symbols that have active FNO contracts
    fno_by_underlying: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    fno_expiries: Dict[str, Set[str]] = defaultdict(set)
    fno_strikes: Dict[str, List[float]] = defaultdict(list)

    # Cash equities indexed by trading symbol and token
    cash_equities: Dict[str, Dict[str, Any]] = {}
    cash_equities_by_token: Dict[str, Dict[str, Any]] = {}

    with open(master_csv, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            exch = (row.get("exchange") or "").strip().upper()
            seg = (row.get("segment") or "").strip().upper()
            itype = (row.get("instrument_type") or "").strip().upper()
            sym = (row.get("trading_symbol") or row.get("tradingsymbol") or "").strip().upper()

            # 1. Upstox CDN format
            if "instrument_key" in row:
                if exch == "NSE_EQ" and itype in ("EQUITY", "EQ"):
                    if sym and not sym.isdigit() and not re.match(r"^\d+[A-Z]+\d+$", sym):
                        cash_equities[sym] = row
                        tok = (row.get("exchange_token") or "").strip()
                        if tok:
                            cash_equities_by_token[tok] = row
                elif exch == "NSE_FO" and itype in ("OPTSTK", "FUTSTK", "CE", "PE", "FUT"):
                    und_sym = (row.get("underlying_symbol") or "").strip().upper()
                    if not und_sym:
                        m = re.match(r"^([A-Z0-9_\-]+?)\d{2}[A-Z]{3}", sym)
                        if m:
                            und_sym = m.group(1)
                    if und_sym:
                        fno_by_underlying[und_sym].append(row)
                        exp = (row.get("expiry_date") or row.get("expiry") or "").strip()
                        if exp:
                            fno_expiries[und_sym].add(exp)
                        strk_raw = row.get("strike_price") or row.get("strike")
                        if strk_raw:
                            try:
                                s_val = float(strk_raw)
                                if s_val > 0:
                                    fno_strikes[und_sym].append(s_val)
                            except (ValueError, TypeError):
                                pass

            # 2. Groww / Legacy format
            else:
                if exch != target_exchange:
                    continue
                if seg == "CASH" and itype in ("EQ", ""):
                    series = (row.get("series") or "").strip().upper()
                    if series in ("EQ", ""):
                        cash_equities[sym] = row
                        tok = (row.get("exchange_token") or "").strip()
                        if tok:
                            cash_equities_by_token[tok] = row
                elif seg == "FNO" and itype in ("CE", "PE", "FUT"):
                    und_sym = (row.get("underlying_symbol") or "").strip().upper()
                    if und_sym:
                        fno_by_underlying[und_sym].append(row)
                        exp = (row.get("expiry_date") or "").strip()
                        if exp:
                            fno_expiries[und_sym].add(exp)
                        strk_raw = row.get("strike_price")
                        if strk_raw:
                            try:
                                s_val = float(strk_raw)
                                if s_val > 0:
                                    fno_strikes[und_sym].append(s_val)
                            except (ValueError, TypeError):
                                pass

    print(f"  ✓ Identified {len(fno_by_underlying):,} unique F&O underlying derivatives in {target_exchange}.")

    # Pass 2: Match FNO underlyings to Cash Equities
    filtered_equities: List[EquityInstrument] = []
    # Known index tickers to skip from equity stock universe
    index_tickers = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

    for und_sym, contracts in sorted(fno_by_underlying.items()):
        if und_sym in index_tickers:
            continue

        # Find corresponding Cash EQ row
        eq_row = cash_equities.get(und_sym)
        if not eq_row:
            # Check by token or partial match
            sample_contract = contracts[0]
            und_tok = sample_contract.get("underlying_exchange_token", "").strip()
            if und_tok in cash_equities_by_token:
                eq_row = cash_equities_by_token[und_tok]

        if not eq_row:
            continue

        isin = (eq_row.get("isin") or "").strip()
        tok = (eq_row.get("exchange_token") or "").strip()
        name = (eq_row.get("name") or eq_row.get("display_name") or und_sym).strip()
        lot_sz = 1
        # Extract derivative lot size from sample option contract if available
        sample_contract = contracts[0]
        try:
            lot_sz = int(sample_contract.get("lot_size") or 1)
        except (ValueError, TypeError):
            lot_sz = 1

        tick_sz = 0.05
        try:
            tick_sz = float(eq_row.get("tick_size") or 0.05)
        except (ValueError, TypeError):
            tick_sz = 0.05

        upstox_key = eq_row.get("instrument_key") or (f"{target_exchange}_EQ|{isin}" if isin else f"{target_exchange}_EQ|{tok}")

        strikes = fno_strikes.get(und_sym, [])
        min_s = min(strikes) if strikes else 0.0
        max_s = max(strikes) if strikes else 0.0

        filtered_equities.append(
            EquityInstrument(
                exchange=target_exchange,
                exchange_token=tok,
                trading_symbol=und_sym,
                name=name,
                isin=isin,
                lot_size=lot_sz,
                tick_size=tick_sz,
                upstox_instrument_key=upstox_key,
                total_fno_contracts=len(contracts),
                available_expiries=sorted(list(fno_expiries.get(und_sym, []))),
                min_strike=min_s,
                max_strike=max_s,
            )
        )

    # Sort alphabetically by symbol
    filtered_equities.sort(key=lambda x: x.trading_symbol)

    # Write output CSV
    fieldnames = [
        "exchange",
        "exchange_token",
        "trading_symbol",
        "name",
        "isin",
        "lot_size",
        "tick_size",
        "upstox_instrument_key",
        "total_fno_contracts",
        "available_expiries",
        "min_strike",
        "max_strike",
    ]

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for eq in filtered_equities:
            row_dict = eq.to_dict()
            row_dict["available_expiries"] = ";".join(row_dict["available_expiries"])
            writer.writerow(row_dict)

    print(f"  ✓ Successfully filtered and saved {len(filtered_equities):,} F&O equities to '{output_csv}'")
    return output_csv


def main():
    parser = argparse.ArgumentParser(description="Filter F&O Eligible Equities")
    parser.add_argument("--master", default="data/instrument.csv", help="Master CSV path")
    parser.add_argument("--output", default="data/filtered.csv", help="Output filtered CSV path")
    parser.add_argument("--exchange", default="NSE", help="Target exchange")
    args = parser.parse_args()

    res = run_instrument_filter(master_csv=args.master, output_csv=args.output, target_exchange=args.exchange)
    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()

