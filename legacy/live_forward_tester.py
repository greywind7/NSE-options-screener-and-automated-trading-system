#!/usr/bin/env python3
"""
live_bear_flag_paper.py
=======================
Dedicated Standalone Live Forward-Tester & Paper-Trading Runner for Bear Flag Option Strategy.

Automates:
1. Continuous live streaming of underlying stock data and RVOL metrics from Upstox.
2. Quantified detection of Bear Flag Setups:
   - Impulse Pole drop into lower 15% range.
   - Volume contraction (RVOL_5m < 0.70x) during tight flag pause (>= 2 cycles).
   - Breakdown crack below Flag Low on volume expansion.
3. Derivative Execution:
   - Resolves ATM, ITM, and OTM Call (CE) for Bull Flags and Put (PE) for Bear Flags.
   - Live Upstox option chain fetching and simultaneous strike execution.
4. Dynamic Tiered Capital Gating:
   - 50-100% Capital: Max 50% allocation per setup.
   - 25-50% Capital: Max 75% allocation per setup.
   - < 25% Capital: 100% allocation allowed.
5. Risk Management:
   - Invalidation Stop Loss (Flag Low for Bull, Flag High for Bear).
   - 1.5R Trailing Stop to Breakeven.
   - 2.0R Full Profit Target.
   - Emergency Hard Stop (-35% Option Premium drop).
   - Automatic 15:20 IST EOD square-off.
6. Live Mark-to-Market (MTM) Terminal Dashboard, side-by-side performance matrix,
   and instant trade journal CSV logging.

Usage:
    # Run default forward tester with ₹100,000 capital (ATM + ITM + OTM side-by-side):
    python3 live_bear_flag_paper.py --index volume_index.json --capital 100000

    # Test ATM options only:
    python3 live_bear_flag_paper.py --index volume_index.json --strike-mode atm

    # Custom polling interval and CSV output:
    python3 live_bear_flag_paper.py --index volume_index.json --interval 2 --output-csv my_trades.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from bear_flag_engine import (
    BearFlagPaperPortfolio,
    Colors,
    load_instruments,
)
from stream_rvol_scanner import (
    RollingVolumeTracker,
    calculate_cumulative_rvol,
    calculate_setup_score,
    clear_screen,
    fetch_live_quotes_batch,
    get_current_time_slot,
)
from upstox_common import get_access_token, safe_float, safe_int


def main():
    parser = argparse.ArgumentParser(
        description="Live Bear Flag Option Strategy Forward-Tester (Paper Trading)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--index",
        default="volume_index.json",
        help="Path to historical volume profile index JSON file",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100_000.0,
        help="Starting paper trading capital in INR",
    )
    parser.add_argument(
        "--direction",
        choices=["both", "bull", "bear"],
        default="both",
        help="Direction to trade: 'both' (Bull Flags CE & Bear Flags PE), 'bull' (Calls only), or 'bear' (Puts only)",
    )
    parser.add_argument(
        "--strike-mode",
        choices=["all", "atm", "itm", "otm", "low_prem", "trail_sl", "fixed_tgt", "combo"],
        default="all",
        help="Strike execution mode: 'all' (simulates ATM, ITM, OTM, LOW_PREM, TRAIL_SL, FIXED_TGT, COMBO), or specific strike",
    )
    parser.add_argument(
        "--fixed-target",
        type=float,
        default=750.0,
        help="Fixed target net gain in INR for dual target simulation (default: 750.0)",
    )
    parser.add_argument(
        "--max-setups",
        type=int,
        default=None,
        help="Optional maximum concurrent active underlying setups (default: None, unlimited - bounded only by capital)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=2,
        help="Market poll interval in seconds",
    )
    parser.add_argument(
        "--output-csv",
        default="bear_flag_paper_trades.csv",
        help="CSV file path to record paper-trading journal",
    )
    parser.add_argument(
        "--instruments-csv",
        default="instrument.csv",
        help="Path to master instruments CSV database",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=55,
        help="Minimum Setup Score required to confirm Bear Flag trigger (default: 55)",
    )
    parser.add_argument(
        "--max-lots",
        type=int,
        default=1,
        help="Maximum lots allowed per trade setup (default: 1 to avoid capital concentration)",
    )
    parser.add_argument(
        "--brokerage",
        type=float,
        default=30.0,
        help="Brokerage fee per order in INR (default: 30.0 flat, round-trip = 60.0)",
    )
    parser.add_argument(
        "--target-pct",
        type=float,
        default=5.0,
        help="Initial target profit percentage on premium paid (default: 5.0%% + brokerage)",
    )
    parser.add_argument(
        "--target-decay-pct",
        type=float,
        default=2.5,
        help="Rolling target floor percentage (default: 2.5%%)",
    )
    parser.add_argument(
        "--breakeven-target-pct",
        type=float,
        default=0.5,
        help="Breakeven target percentage after 30m in red (default: 0.5%%)",
    )
    parser.add_argument(
        "--intermediate-trail-pct",
        type=float,
        default=3.5,
        help="Threshold percentage to activate intermediate trailing floor (default: 3.5%%)",
    )
    parser.add_argument(
        "--intermediate-floor-pct",
        type=float,
        default=2.5,
        help="Base floor percentage for intermediate trailing floor (default: 2.5%%)",
    )
    parser.add_argument(
        "--trailing-step-pct",
        type=float,
        default=1.0,
        help="Trailing distance percentage behind peak for trailing floors (default: 1.0%%)",
    )
    parser.add_argument(
        "--target-runner-seconds",
        type=float,
        default=30.0,
        help="Seconds to let trade run after reaching target with rolling floor (default: 30.0s)",
    )
    parser.add_argument(
        "--r-stop",
        type=float,
        default=0.15,
        help="Option premium stop loss drop fraction (default: 0.15 = 15%% drop)",
    )

    args = parser.parse_args()

    # 1. Validate inputs
    if not os.path.exists(args.index):
        if args.index == "volume_index.json" and os.path.exists("index.json"):
            args.index = "index.json"
        else:
            print(f"\n{Colors.RED}Error: Volume index file not found: {args.index}{Colors.RESET}", file=sys.stderr)
            print("Please build the index first using build_volume_index.py.\n", file=sys.stderr)
            sys.exit(1)

    if not os.path.exists(args.instruments_csv):
        print(f"\n{Colors.RED}Error: Instrument master file not found: {args.instruments_csv}{Colors.RESET}", file=sys.stderr)
        sys.exit(1)

    with open(args.index, "r", encoding="utf-8") as f:
        volume_index: Dict[str, Any] = json.load(f)

    if not volume_index:
        print(f"{Colors.RED}Error: Volume index at {args.index} is empty.{Colors.RESET}", file=sys.stderr)
        sys.exit(1)

    # 2. Load instrument master for Option Chain resolution
    print(f"{Colors.CYAN}Loading F&O instruments and option chains from '{args.instruments_csv}'...{Colors.RESET}")
    inst_manager = load_instruments(args.instruments_csv)
    print(f"{Colors.GREEN}Loaded {len(inst_manager):,} instruments successfully.{Colors.RESET}")

    # 3. Initialize Upstox API Auth & Flag Paper Portfolio
    token = get_access_token()

    portfolio = BearFlagPaperPortfolio(
        starting_capital=args.capital,
        direction=args.direction,
        strike_mode=args.strike_mode,
        max_concurrent_setups=args.max_setups,
        output_csv=args.output_csv,
        manager=inst_manager,
        access_token=token,
        max_lots=args.max_lots,
        brokerage_per_order=args.brokerage,
        target_pct=args.target_pct,
        r_stop=args.r_stop,
        target_decay_pct=args.target_decay_pct,
        breakeven_target_pct=args.breakeven_target_pct,
        intermediate_trail_trigger_pct=args.intermediate_trail_pct,
        intermediate_trail_floor_pct=args.intermediate_floor_pct,
        trailing_step_pct=args.trailing_step_pct,
        target_runner_seconds=args.target_runner_seconds,
        fixed_target_gain=args.fixed_target,
    )

    underlying_keys = list(volume_index.keys())
    sample_profile = next(iter(volume_index.values()), {})
    idx_interval = sample_profile.get("interval_minutes", 1)

    rolling_tracker = RollingVolumeTracker()
    price_histories: Dict[str, List[float]] = defaultdict(list)
    scan_count = 0

    print(f"\nStarting Flag Option Forward-Tester on {len(underlying_keys):,} instruments...")
    print(f"Capital: ₹{args.capital:,.0f} | Direction: {args.direction.upper()} | Strike Mode: {args.strike_mode.upper()} | Journal: {args.output_csv}")
    time.sleep(1.5)

    try:
        while True:
            scan_count += 1
            now = datetime.now()
            current_slot = get_current_time_slot(idx_interval)

            # Determine keys to quote: underlying stocks + any active/pending options
            option_keys = portfolio.get_required_option_keys()
            all_query_keys = list(underlying_keys) + list(option_keys)

            # Fetch live market quotes
            quotes = fetch_live_quotes_batch(all_query_keys, token)

            # Process Underlying Equities
            for key in underlying_keys:
                profile = volume_index[key]
                sym = profile.get("symbol") or profile.get("name") or ""
                exch = profile.get("exchange") or "NSE"

                # Multi-lookup for underlying quote
                quote = (
                    quotes.get(key)
                    or quotes.get(key.replace("|", ":"))
                    or (quotes.get(f"{exch}_EQ:{sym}") if sym else None)
                    or (quotes.get(f"{exch}_EQ|{sym}") if sym else None)
                    or (quotes.get(f"{exch}_FO:{sym}") if sym else None)
                    or (quotes.get(f"{exch}_FO|{sym}") if sym else None)
                    or (quotes.get(sym) if sym else None)
                )

                if not quote or not isinstance(quote, dict):
                    continue

                # Parse volume & price
                vol_raw = quote.get("volume")
                if vol_raw is None:
                    vol_raw = quote.get("volume_traded") or (quote.get("ohlc", {}).get("volume") if isinstance(quote.get("ohlc"), dict) else 0)
                today_vol = safe_int(vol_raw, 0)

                ltp_raw = quote.get("last_price")
                if ltp_raw is None and isinstance(quote.get("ohlc"), dict):
                    ltp_raw = quote["ohlc"].get("close")
                ltp = safe_float(ltp_raw, 0.0)

                net_chg = safe_float(quote.get("net_change"), 0.0)
                ohlc_dict = quote.get("ohlc", {}) if isinstance(quote.get("ohlc"), dict) else {}
                prev_close = safe_float(ohlc_dict.get("close"), 0.0)
                if prev_close <= 0 and ltp > 0:
                    prev_close = ltp - net_chg
                pct_chg = round((net_chg / prev_close) * 100, 2) if prev_close > 0 else 0.0

                if ltp > 0:
                    hist = price_histories[key]
                    if not hist or hist[-1] != ltp:
                        hist.append(ltp)
                        if len(hist) > 30:
                            hist.pop(0)

                open_val = safe_float(ohlc_dict.get("open"), ltp)
                high_val = safe_float(ohlc_dict.get("high"), ltp)
                low_val = safe_float(ohlc_dict.get("low"), ltp)

                rvol_cum, exp_cum_vol, mode = calculate_cumulative_rvol(today_vol, profile, current_time_str=current_slot)

                day_span = high_val - low_val
                range_pos = (ltp - low_val) / day_span if day_span > 0 else 0.5

                v_stats = rolling_tracker.update(
                    key=key,
                    current_volume=today_vol,
                    rvol_cum=rvol_cum,
                    profile=profile,
                    range_pos=range_pos,
                    ltp=ltp,
                    high_price=high_val,
                    low_price=low_val,
                    prev_close=prev_close,
                    open_price=open_val,
                    now=now,
                )
                rvol_5m = v_stats["rvol_5m"]
                rvol_1m = v_stats["rvol_1m"]

                s_score, s_stage, s_dir, s_details = calculate_setup_score(
                    rvol_5m=rvol_5m,
                    rvol_1m=rvol_1m,
                    rvol_cum=rvol_cum,
                    ltp=ltp,
                    open_price=open_val,
                    high_price=high_val,
                    low_price=low_val,
                    pct_change=pct_chg,
                    price_history=price_histories[key],
                    is_inactive=v_stats["is_inactive"],
                    mode=mode,
                    z_vol=v_stats["z_vol"],
                    pct_change_5m=v_stats["pct_change_5m"],
                    seconds_since_hod=v_stats["seconds_since_hod"],
                    seconds_since_lod=v_stats["seconds_since_lod"],
                    is_flag_breakout=v_stats.get("is_flag_breakout", False),
                    flag_breakout_type=v_stats.get("flag_breakout_type", ""),
                    has_bull_pole=v_stats.get("has_bull_pole", True),
                    has_bear_pole=v_stats.get("has_bear_pole", True),
                    vol_accel=v_stats.get("vol_accel", 1.0),
                    is_dry_extreme=v_stats.get("is_dry_extreme", False),
                    rsi=v_stats.get("rsi", 50.0),
                    macd_hist=v_stats.get("macd_hist", 0.0),
                    bull_clearance_pct=v_stats.get("bull_clearance_pct", 1.0),
                    bear_clearance_pct=v_stats.get("bear_clearance_pct", 1.0),
                    is_near_resistance=v_stats.get("is_near_resistance", False),
                    is_near_support=v_stats.get("is_near_support", False),
                )

                # Feed into Flag State Machine
                clearance = v_stats.get("bull_clearance_pct" if s_dir == "BULL" else "bear_clearance_pct", 1.0)
                portfolio.process_underlying_tick(
                    symbol=sym,
                    ltp=ltp,
                    high=high_val,
                    low=low_val,
                    range_pos=range_pos,
                    rvol_5m=rvol_5m,
                    rvol_cum=rvol_cum,
                    stage=s_stage,
                    score=s_score,
                    now=now,
                    vol_accel=v_stats.get("vol_accel", 1.0),
                    is_dry_extreme=v_stats.get("is_dry_extreme", False),
                    clearance_pct=clearance,
                    is_near_resistance=v_stats.get("is_near_resistance", False),
                    is_near_support=v_stats.get("is_near_support", False),
                    rsi=v_stats.get("rsi", 50.0),
                    macd_hist=v_stats.get("macd_hist", 0.0),
                )

            # Fetch fresh quotes for any newly triggered option contracts before processing executions
            needed_opt_keys = portfolio.get_required_option_keys()
            missing_keys = [k for k in needed_opt_keys if k not in quotes and k.replace("|", ":") not in quotes]
            if missing_keys:
                fresh_opt_quotes = fetch_live_quotes_batch(missing_keys, token)
                quotes.update(fresh_opt_quotes)

            # Process Option Quotes & Evaluate Exits / Triggers
            portfolio.process_market_quotes(
                underlying_quotes=quotes,
                option_quotes=quotes,
                now=now,
            )

            # Render Live Dashboard
            clear_screen()
            print(f"{Colors.GRAY}Snapshot #{scan_count} | IST: {now.strftime('%H:%M:%S')} | Polling Interval: {args.interval}s{Colors.RESET}\n")
            print(portfolio.render_dashboard())
            print(f"\n{Colors.GRAY}Press Ctrl+C to stop forward-tester and generate comprehensive performance report.{Colors.RESET}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n\nStopping forward-testing session...")
    finally:
        # Print Final Report
        clear_screen()
        print(portfolio.generate_final_report())
        print(f"\nAll trades logged to: {os.path.abspath(args.output_csv)}\n")


if __name__ == "__main__":
    main()
