#!/usr/bin/env python3
"""
runner.py
=========
Central Operational Orchestrator for Upstox Algorithmic Trading Infrastructure.

Subcommands:
    prepare : Run pre-market universe filtering, option chain resolution, and 30-day volume indexing.
    start   : Launch market data listener, recorder, strategies, and execution engine.
    replay  : Replay recorded market sessions with historical order-book simulation.
    status  : Check process health, connection state, consumer lag, and recording integrity.
    stop    : Gracefully stop all background services.
    test    : Run test suites across infrastructure, strategy, execution, and legacy parity.

Usage:
    python3 runner.py prepare
    python3 runner.py start --strategy momentum --mode paper
    python3 runner.py start --strategy momentum --mode paper_delayed
    python3 runner.py start --strategy momentum --mode live
    python3 runner.py replay --date 20261106 --strategy momentum --speed 1.0
    python3 runner.py status
    python3 runner.py stop
    python3 runner.py test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.common.time_utils import normalize_time_str


def load_json_config(path: str) -> Dict[str, Any]:
    """Load JSON configuration file safely."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading configuration {path}: {e}", file=sys.stderr)
        return {}


def cmd_prepare(args: argparse.Namespace) -> int:
    """
    Execute pre-market preparation pipeline.

    Soft Prepare (Default):
        Checks if today's dated files (instrument master, filtered F&O, volume index,
        and subscription manifest) are already present. If present and valid, skips
        redundant network downloads and heavy computation.

    Reset Mode (--reset):
        Forces a fresh retrieval and complete rebuild of all documents, updating
        both the dated files and the canonical reference files.
    """
    session_date = getattr(args, "date", None)
    if not session_date:
        session_date = datetime.now().strftime("%Y%m%d")

    is_reset = getattr(args, "reset", False)
    is_mcx = getattr(args, "mcx", False) or getattr(args, "exchange", "NSE").upper() == "MCX"
    target_exchange = "MCX" if is_mcx else "NSE"

    print("=" * 80)
    print("🚀 PRE-MARKET PREPARATION PIPELINE")
    print(f"Session Date: {session_date} | Exchange: {target_exchange} | Mode: {'RESET (Fresh Rebuild)' if is_reset else 'SOFT PREPARE'}")
    print(f"Time        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    from core.instruments.master_fetcher import run_master_fetcher
    from core.instruments.instrument_filter import run_instrument_filter
    from core.volume.volume_index import run_volume_indexer
    from core.instruments.subscription_manifest import run_manifest_generator

    sys_cfg = load_json_config("config/system.json")
    inst_cfg = load_json_config("config/instruments.json")

    # Define dated paths and canonical paths
    dated_master = f"data/instrument_{session_date}.csv"
    canonical_master = inst_cfg.get("instruments", {}).get("paths", {}).get("instrument_master_csv", "data/instrument.csv")

    if is_mcx:
        dated_filtered = f"data/filtered_mcx_{session_date}.csv"
        dated_index = f"data/index_mcx_{session_date}.json"
        dated_manifest = f"data/subscription_manifest_mcx_{session_date}.json"
        canonical_filtered = "data/filtered_mcx.csv"
        canonical_index = "data/index_mcx.json"
        canonical_manifest = "data/subscription_manifest_mcx.json"
    else:
        dated_filtered = f"data/filtered_{session_date}.csv"
        dated_index = f"data/index_{session_date}.json"
        dated_manifest = f"data/subscription_manifest_{session_date}.json"
        canonical_filtered = inst_cfg.get("instruments", {}).get("paths", {}).get("filtered_universe_csv", "data/filtered.csv")
        canonical_index = inst_cfg.get("instruments", {}).get("paths", {}).get("volume_index_json", "data/index.json")
        canonical_manifest = inst_cfg.get("instruments", {}).get("paths", {}).get("subscription_manifest_json", "data/subscription_manifest.json")

    def sync_copy(src: str, dst: str) -> None:
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                print(f"  ⚠️ Warning syncing {src} -> {dst}: {e}", file=sys.stderr)

    # -------------------------------------------------------------
    # Step 1: Upstox Instrument Master Database
    # -------------------------------------------------------------
    print(f"\n[Step 1/4] Checking Instrument Master (Date: {session_date})...")
    if not is_reset and os.path.exists(dated_master) and os.path.getsize(dated_master) > 100_000:
        print(f"  ✓ Found today's Instrument Master at '{dated_master}' ({os.path.getsize(dated_master) / (1024 * 1024):.2f} MB). Skipping download.")
        master_path = dated_master
    elif not is_reset and os.path.exists(canonical_master) and os.path.getsize(canonical_master) > 100_000:
        print(f"  ✓ Found existing local Instrument Master at '{canonical_master}'. Stamping dated copy '{dated_master}'.")
        sync_copy(canonical_master, dated_master)
        master_path = dated_master
    else:
        master_path = run_master_fetcher(force_download=is_reset, target_csv=dated_master)

    if not master_path or not os.path.exists(master_path):
        print(f"❌ Pre-market preparation failed at Step 1: Instrument master not available.", file=sys.stderr)
        return 1
    sync_copy(master_path, canonical_master)

    # -------------------------------------------------------------
    # Step 2: F&O Universe Filter (~213 equities or ~17 MCX commodities)
    # -------------------------------------------------------------
    print(f"\n[Step 2/4] Filtering Eligible {target_exchange} Universe (Date: {session_date})...")
    if not is_reset and os.path.exists(dated_filtered) and os.path.getsize(dated_filtered) > 1000:
        print(f"  ✓ Found today's Filtered {target_exchange} Universe at '{dated_filtered}' ({os.path.getsize(dated_filtered) / 1024:.1f} KB). Skipping filter.")
        filtered_path = dated_filtered
    elif not is_reset and os.path.exists(canonical_filtered) and os.path.getsize(canonical_filtered) > 1000:
        print(f"  ✓ Found existing Filtered {target_exchange} Universe at '{canonical_filtered}'. Stamping dated copy '{dated_filtered}'.")
        sync_copy(canonical_filtered, dated_filtered)
        filtered_path = dated_filtered
    else:
        filtered_path = run_instrument_filter(master_csv=master_path, output_csv=dated_filtered, target_exchange=target_exchange)

    if not filtered_path or not os.path.exists(filtered_path):
        print(f"❌ Pre-market preparation failed at Step 2: {target_exchange} filtering failed.", file=sys.stderr)
        return 1
    sync_copy(filtered_path, canonical_filtered)

    # -------------------------------------------------------------
    # Step 3: Historical 30-Day Volume Index Baseline
    # -------------------------------------------------------------
    print(f"\n[Step 3/4] Building 30-Day Volume Baseline Index (Date: {session_date})...")
    if not is_reset and not os.path.exists(dated_index) and os.path.exists(canonical_index):
        print(f"  ✓ Found existing Volume Index at '{canonical_index}'. Validating for dated copy '{dated_index}'.")
        sync_copy(canonical_index, dated_index)

    volume_path = run_volume_indexer(
        instruments_csv=filtered_path,
        output_json=dated_index,
        days=args.volume_days,
        skip_if_exists=not is_reset,
    )

    if not volume_path or not os.path.exists(volume_path):
        print(f"❌ Pre-market preparation failed at Step 3: Volume index build failed.", file=sys.stderr)
        return 1
    sync_copy(volume_path, canonical_index)

    # -------------------------------------------------------------
    # Step 4: Multi-Stream Subscription Manifest Generator
    # -------------------------------------------------------------
    print(f"\n[Step 4/4] Generating Multi-Stream Subscription Manifest (Date: {session_date})...")
    pop_strikes = inst_cfg.get("instruments", {}).get("options_subscription", {}).get("popular_strikes_above_atm", 3)
    max_opt_und = (
        sys_cfg.get("system", {}).get("manifest", {}).get("max_stream_underlyings_with_options")
        or sys_cfg.get("system", {}).get("feed", {}).get("max_stream_underlyings_with_options", 110)
    )
    if not is_reset and os.path.exists(dated_manifest) and os.path.getsize(dated_manifest) > 10_000:
        print(f"  ✓ Found today's Subscription Manifest at '{dated_manifest}' ({os.path.getsize(dated_manifest) / 1024:.1f} KB). Skipping generation.")
        manifest_path = dated_manifest
    elif not is_reset and os.path.exists(canonical_manifest) and os.path.getsize(canonical_manifest) > 10_000:
        print(f"  ✓ Found existing Subscription Manifest at '{canonical_manifest}'. Stamping dated copy '{dated_manifest}'.")
        sync_copy(canonical_manifest, dated_manifest)
        manifest_path = dated_manifest
    else:
        manifest_path = run_manifest_generator(
            filtered_csv=filtered_path,
            volume_index_json=volume_path,
            master_csv=master_path,
            output_json=dated_manifest,
            popular_strikes=pop_strikes,
            max_stream_underlyings_with_options=int(max_opt_und),
            session_date=session_date,
        )

    if not manifest_path or not os.path.exists(manifest_path):
        print(f"❌ Pre-market preparation failed at Step 4: Manifest generation failed.", file=sys.stderr)
        return 1
    sync_copy(manifest_path, canonical_manifest)

    print("\n" + "=" * 80)
    print(f"✅ PRE-MARKET PREPARATION COMPLETED ({target_exchange} | {'RESET FRESH' if is_reset else 'SOFT PREPARE'})!")
    print(f"  • Date Tagged Documents for Session: {session_date}")
    print(f"    - Instrument Master : {dated_master} (Canonical: {canonical_master})")
    print(f"    - {target_exchange} Universe     : {dated_filtered} (Canonical: {canonical_filtered})")
    print(f"    - Volume Baseline   : {dated_index} (Canonical: {canonical_index})")
    print(f"    - Stream Manifest   : {dated_manifest} (Canonical: {canonical_manifest})")
    print("=" * 80)

    # Maintenance: Prune stale preparation files from older trading sessions and rotate debug logs
    try:
        from core.instruments.cleanup import prune_old_preparation_files
        from core.auth.upstox_auth import prune_debug_log
        prune_old_preparation_files(keep_session_date=session_date)
        prune_debug_log("logs/debug.log")
    except Exception as e:
        print(f"  ⚠️ Preparation maintenance note: {e}", file=sys.stderr)

    return 0


from scripts.run_distributed import (
    spawn_process,
    supervise_processes,
    kill_all_registered,
    load_pids,
    is_pid_alive,
)


def get_python_binary() -> str:
    venv_py = os.path.abspath(".venv/bin/python")
    if os.path.exists(venv_py) and os.access(venv_py, os.X_OK):
        return venv_py
    return sys.executable


def cmd_core(args: argparse.Namespace) -> int:
    """Launch centralized Core Tier: Ingestion, Shared Memory Candle Manager, Quotes Broker, and optional Recorder."""
    feed_flag = getattr(args, "feed", False)
    candle_flag = getattr(args, "candlemanager", False)
    quotes_flag = getattr(args, "quotesbroker", False)
    record_flag = getattr(args, "record", False)

    # If none of the service flags are passed, start full core suite by default
    if not (feed_flag or candle_flag or quotes_flag):
        feed_flag = True
        candle_flag = True
        quotes_flag = True

    print("=" * 80)
    print("🚀 LAUNCHING TRADING SYSTEM SPEC 2.0 - CORE INFRASTRUCTURE TIER")
    print("=" * 80)
    py_bin = get_python_binary()
    procs = []

    # 1. Feed Handler
    feed_mode = getattr(args, "mode", "ws")
    feed_cmd = [py_bin, "-m", "core.feed.feed_handler", "--mode", feed_mode]
    p_feed = spawn_process(feed_cmd, "feed_handler", "logs/feed_handler.log")
    procs.append(p_feed)
    print(f"  • Feed Handler      : Spawned (PID: {p_feed.pid}, Mode: {feed_mode})")

    # 2. Candle Manager
    candle_cmd = [py_bin, "-m", "core.candles.candle_manager"]
    p_candle = spawn_process(candle_cmd, "candle_manager", "logs/candle_manager.log")
    procs.append(p_candle)
    print(f"  • Candle Manager    : Spawned (PID: {p_candle.pid})")

    # 3. Quotes Broker
    quotes_cmd = [py_bin, "-m", "core.quotes.quotes_broker"]
    p_quotes = spawn_process(quotes_cmd, "quotes_broker", "logs/quotes_broker.log")
    procs.append(p_quotes)
    print(f"  • Quotes Broker     : Spawned (PID: {p_quotes.pid})")

    # 4. Optional Market Recorder
    if record_flag:
        rec_cmd = [py_bin, "-m", "core.recorder.market_recorder"]
        if getattr(args, "debug", False):
            rec_cmd.append("--debug")
        p_rec = spawn_process(rec_cmd, "market_recorder", "logs/market_recorder.log")
        procs.append(p_rec)
        print(f"  • Market Recorder   : Spawned (PID: {p_rec.pid})")

    print("=" * 80)
    print("✓ Core tier online. Press Ctrl+C to stop all core services.")
    print("=" * 80)
    return supervise_processes(procs)


def cmd_feed(args: argparse.Namespace) -> int:
    """Launch Feed Handler independently, with optional --candles and --record forks."""
    feed_mode = getattr(args, "mode", "ws")
    py_bin = get_python_binary()
    procs = []

    if getattr(args, "candles", False):
        p_candle = spawn_process([py_bin, "-m", "core.candles.candle_manager"], "candle_manager", "logs/candle_manager.log")
        procs.append(p_candle)
        print(f"  • Candle Manager    : Spawned (PID: {p_candle.pid})")

    if getattr(args, "record", False):
        p_rec = spawn_process([py_bin, "-m", "core.recorder.market_recorder"], "market_recorder", "logs/market_recorder.log")
        procs.append(p_rec)
        print(f"  • Market Recorder   : Spawned (PID: {p_rec.pid})")

    feed_cmd = [py_bin, "-m", "core.feed.feed_handler", "--mode", feed_mode]
    p_feed = spawn_process(feed_cmd, "feed_handler")
    procs.append(p_feed)
    print(f"  • Feed Handler      : Spawned (PID: {p_feed.pid}, Mode: {feed_mode})")
    return supervise_processes(procs)


def cmd_candlemanager(args: argparse.Namespace) -> int:
    """Launch Candle Manager independently."""
    py_bin = get_python_binary()
    cmd = [py_bin, "-m", "core.candles.candle_manager"]
    p = spawn_process(cmd, "candle_manager")
    print(f"  • Candle Manager    : Spawned (PID: {p.pid})")
    return supervise_processes([p])


def cmd_quotesbroker(args: argparse.Namespace) -> int:
    """Launch Quotes Broker independently."""
    py_bin = get_python_binary()
    cmd = [py_bin, "-m", "core.quotes.quotes_broker"]
    p = spawn_process(cmd, "quotes_broker")
    print(f"  • Quotes Broker     : Spawned (PID: {p.pid})")
    return supervise_processes([p])


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Launch Universal Terminal Dashboard."""
    from ui.dashboard import TerminalDashboard
    d = TerminalDashboard(
        alerts_ipc=getattr(args, "alerts_ipc", "ipc:///tmp/scoring_alerts_pub.ipc"),
        rms_ipc=getattr(args, "rms_ipc", "ipc:///tmp/rms_strike_pub.ipc"),
        recordings_dir=getattr(args, "recordings_dir", "recordings"),
    )
    d.start(refresh_interval=getattr(args, "refresh", 1.0))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Launch Modern Analytics & Simulation Web UI."""
    from web.server import AnalyticsWebServer
    host = getattr(args, "host", "0.0.0.0")
    port = int(getattr(args, "port", 8080))
    rec_dir = getattr(args, "record_dir", "recordings")
    server = AnalyticsWebServer(host=host, port=port, recordings_dir=rec_dir)
    server.run()
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    """Launch centralized Market Data Listener service."""
    from core.feed.upstox_listener import MarketDataListener
    is_mcx = getattr(args, "mcx", False) or getattr(args, "exchange", "NSE").upper() == "MCX"
    exchange = "MCX" if is_mcx else "NSE"
    listener = MarketDataListener(
        mode=args.mode,
        exchange=exchange,
        ipc_path=args.ipc_path,
        poll_interval=args.poll_interval,
        max_stream_contracts=getattr(args, "max_contracts", 1800),
        silent=getattr(args, "silent", False),
        allow_rest_fallback=getattr(args, "allow_rest_fallback", False),
    )
    try:
        listener.start()
        return 0
    except KeyboardInterrupt:
        listener.stop()
        return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Launch independent market data recorder service."""
    from core.recorder.market_recorder import MarketRecorder
    output_dir = getattr(args, "record_dir", "recordings")
    debug_flag = getattr(args, "debug", False)
    recorder = MarketRecorder(output_dir=output_dir, debug=debug_flag if debug_flag else None)
    print("=" * 80)
    print("💾 MARKET DATA RECORDER SERVICE ACTIVE")
    print(f"Output Directory: {output_dir}")
    print("=" * 80)
    try:
        recorder.start()
        print("✓ Recorder online. Archiving ticks to columnar parquet...")
        print("  (Press Ctrl+C to stop recording)\n")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        recorder.stop()
        print("\nRecorder stopped gracefully.")
        return 0


def cmd_start_v2(args: argparse.Namespace) -> int:
    """Launch 2.0 Distributed Multi-Track Trading System."""
    mode_arg = getattr(args, "mode", "paper") or "paper"
    modes = [m.strip().lower() for m in mode_arg.split(",") if m.strip()]

    raw_tracks = getattr(args, "tracks", None)
    if not raw_tracks or raw_tracks.strip().upper() == "ALL":
        from common.tracks.factory import get_enabled_track_names
        tracks = get_enabled_track_names()
    else:
        tracks = [t.strip() for t in raw_tracks.split(",") if t.strip()]

    print("=" * 80)
    print("⚡ LAUNCHING TRADING SYSTEM SPEC 2.0 - DISTRIBUTED EXECUTION")
    print(f"Modes  : {', '.join(m.upper() for m in modes)}")
    print(f"Tracks : {', '.join(tracks)}")
    print("=" * 80)

    py_bin = get_python_binary()
    procs = []

    # 1. Scoring Engine 3.0 (Momentum Strategy)
    p_score = spawn_process([py_bin, "-m", "strategies.momentum.scoring_engine"], "scoring_engine", "logs/scoring_engine.log")
    procs.append(p_score)
    print(f"  • Scoring Engine 3.0 : Spawned (PID: {p_score.pid})")

    # 2. RMS Service
    p_rms = spawn_process([py_bin, "-m", "core.rms.rms_service"], "rms_service", "logs/rms_service.log")
    procs.append(p_rms)
    print(f"  • RMS & Strikes      : Spawned (PID: {p_rms.pid})")

    # 3. Execution Superstructures (one child process per mode and track)
    for m in modes:
        for tr in tracks:
            exec_code = f"from execution.superstructures.{'paper' if m == 'paper' else 'delayed' if m == 'paper_delayed' else 'live'} import {'Paper' if m == 'paper' else 'Delayed' if m == 'paper_delayed' else 'Live'}Superstructure; s = {'Paper' if m == 'paper' else 'Delayed' if m == 'paper_delayed' else 'Live'}Superstructure(track_names=['{tr}']); s.start()"
            p_exec = spawn_process([py_bin, "-c", exec_code], f"exec_{m}_{tr}", f"logs/exec_{m}_{tr}.log")
            procs.append(p_exec)
            print(f"  • Track Process      : {m.upper()} [{tr}] (PID: {p_exec.pid})")

    # 4. Optional Market Recorder
    if getattr(args, "record", False):
        rec_cmd = [py_bin, "-m", "core.recorder.market_recorder"]
        if getattr(args, "debug", False):
            rec_cmd.append("--debug")
        p_rec = spawn_process(rec_cmd, "market_recorder", "logs/market_recorder.log")
        procs.append(p_rec)
        print(f"  • Market Recorder   : Spawned (PID: {p_rec.pid})")

    # 5. Universal Dashboard (Started implicitly)
    dash_cmd = [py_bin, "-m", "ui.dashboard", "--modes", ",".join(modes), "--tracks", ",".join(tracks)]
    p_dash = spawn_process(dash_cmd, "dashboard")
    procs.append(p_dash)
    print(f"  • Universal Dashboard: Spawned (PID: {p_dash.pid})")

    os.makedirs("logs", exist_ok=True)
    with open("logs/strategy.pid", "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    print("=" * 80)
    print("✓ All distributed processes online. Press Ctrl+C to terminate.")
    print("=" * 80)
    return supervise_processes(procs)


def cmd_start(args: argparse.Namespace) -> int:
    """Launch 2.0 Distributed Multi-Track Trading System."""
    return cmd_start_v2(args)


def has_market_recordings(p: str) -> bool:
    """Check if path is or contains actual market recording data files."""
    if not os.path.exists(p):
        return False
    if os.path.isfile(p):
        b = os.path.basename(p)
        return not (b.startswith("journal_") or b.startswith("trades_") or "trade" in b.lower())
    if os.path.isdir(p):
        for root, _, files in os.walk(p):
            for f in files:
                if f.endswith(".parquet"):
                    return True
                if f.endswith(".jsonl") and not (
                    f.startswith("journal_") or f.startswith("trades_") or "trade" in f.lower()
                ):
                    return True
    return False


def resolve_recording_path(session_date: Optional[str] = None, data_path: Optional[str] = None) -> str:
    """Resolve recording directory or file path across date formats (YYYYMMDD vs YYYY-MM-DD)."""
    if data_path:
        return data_path
    if not session_date:
        return "recordings"

    cands = []
    if len(session_date) == 8 and session_date.isdigit():
        cands.append(f"recordings/{session_date[:4]}-{session_date[4:6]}-{session_date[6:8]}")
        cands.append(f"recordings/{session_date}")
    elif "-" in session_date:
        cands.append(f"recordings/{session_date}")
        cands.append(f"recordings/{session_date.replace('-', '')}")
    else:
        cands.append(f"recordings/{session_date}")

    for c in cands:
        if has_market_recordings(c):
            return c
    for c in cands:
        if os.path.exists(c):
            return c
    return cands[0]


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay recorded session via high-speed 2.0 Historical Replay Engine."""
    session_date = getattr(args, "date", None)
    data_path = getattr(args, "data_path", None)
    data_path = resolve_recording_path(session_date=session_date, data_path=data_path)

    speed = float(getattr(args, "speed", 1.0))
    print("=" * 80)
    print(f"⏪ HISTORICAL MARKET REPLAY: Date {session_date or 'ALL'}")
    print(f"Speed: {speed}x | Data Path: {data_path}")
    print("=" * 80)

    from core.feed.historical_replay import HistoricalReplayIngestor
    ingestor = HistoricalReplayIngestor(data_path=data_path, speed=speed)
    return ingestor.run()


def cmd_simulate(args: argparse.Namespace) -> int:
    """Execute high-speed deterministic historical market simulation."""
    from simulation.columnar_sim_engine import ColumnarSimEngine
    data_path = getattr(args, "data_path", None)
    session_date = getattr(args, "date", None)
    data_path = resolve_recording_path(session_date=session_date, data_path=data_path)

    delay_sec = getattr(args, "delay", None)
    track_filter = getattr(args, "track", None)
    tracks = None
    if track_filter and track_filter.upper() not in ("ALL", "DYNAMIC_TRIO"):
        tracks = [t.strip() for t in track_filter.split(",") if t.strip()]

    mode_arg = getattr(args, "mode", None) or "paper,paper_delayed"
    modes = [m.strip() for m in mode_arg.split(",") if m.strip()]
    capital = getattr(args, "capital", None)

    strat_cfg_path = getattr(args, "strategy_config", "config/strategies.json")
    from core.config.config_loader import get_config_manager
    cfg = get_config_manager(strategy_config_path=strat_cfg_path, reload=True)
    sc_cfg = cfg.get_scoring_config()

    cli_pole = getattr(args, "min_pole_pct", None)
    if cli_pole is not None:
        pole_pct = float(cli_pole) / 100.0 if float(cli_pole) > 0.5 else float(cli_pole)
    else:
        fm_pole = cfg.strategy.get("strategies", {}).get("momentum", {}).get("tracks", {}).get("fast_momentum", {}).get("min_pole_pct")
        sc_pole = sc_cfg.get("min_pole_pct")
        strat_pole = cfg.strategy.get("strategies", {}).get("momentum", {}).get("min_pole_pct")
        pole_cfg_val = fm_pole if (fm_pole is not None and fm_pole != 0.005) else (sc_pole or strat_pole or fm_pole or 0.005)
        pole_pct = float(pole_cfg_val) / 100.0 if float(pole_cfg_val) > 0.5 else float(pole_cfg_val)

    entry_freeze_time = getattr(args, "entry_freeze_time", None)
    eod_squareoff_time = getattr(args, "eod_squareoff_time", None)
    brokerage_per_order = getattr(args, "brokerage_per_order", None)

    print("=" * 80)
    print(f"🚀 COLUMNAR MARKET SIMULATION ENGINE: Data Path '{data_path}'")
    print(f"Modes: {modes} | Tracks: {tracks or 'ALL_ENABLED'} | Delay: {f'{delay_sec}s' if delay_sec is not None else 'Config (200-500ms)'} | Min Pole: {pole_pct*100.0:.2f}%")
    if capital:
        print(f"Capital Override: ₹{capital:,.2f} per track")
    print("=" * 80)

    engine = ColumnarSimEngine(
        data_path=data_path,
        tracks=tracks,
        modes=modes,
        capital=capital,
        delay_sec=delay_sec,
        session_date=session_date,
        min_pole_pct=pole_pct,
        entry_freeze_time=entry_freeze_time,
        eod_squareoff_time=eod_squareoff_time,
        brokerage_per_order=brokerage_per_order,
    )
    results = engine.run_simulation()
    engine.print_summary(results)
    if getattr(args, "journal", False):
        engine.print_journal()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Check health of infrastructure processes, SHM segments, and prepared documents."""
    print("=" * 80)
    print("🩺 INFRASTRUCTURE STATUS & HEALTH CHECK")
    print("=" * 80)

    # 1. Distributed Process Registry (logs/pids.json)
    pids = load_pids()
    print("\nDistributed Process Registry (logs/pids.json):")
    if pids:
        print(f"  {'ROLE':<22} | {'PID':<8} | {'STATUS':<10} | {'STARTED AT'}")
        print("  " + "-" * 65)
        for role, info in pids.items():
            pid = info.get("pid", 0)
            alive = is_pid_alive(pid)
            st = "RUNNING" if alive else "STOPPED"
            print(f"  {role:<22} | {pid:<8} | {st:<10} | {info.get('started_at', 'N/A')}")
    else:
        print("  ✓ No active background processes registered.")

    # 2. Shared Memory Status
    from core.candles.shm_layout import SHM_NAME
    shm_path = f"/tmp/{SHM_NAME}.shm"
    shm_exists = os.path.exists(shm_path)
    print(f"\nShared Memory Status:")
    print(f"  • Segment Name      : {SHM_NAME}")
    print(f"  • SHM File          : {shm_path} ({'ACTIVE' if shm_exists else 'NOT ALLOCATED'})")

    sys_cfg = load_json_config("config/system.json")
    ipc = sys_cfg.get("system", {}).get("ipc", {})
    eq_ipc = ipc.get("feed_equity_ipc", "ipc:///tmp/feed_equity.ipc").replace("ipc://", "")
    opt_ipc = ipc.get("feed_options_ipc", "ipc:///tmp/feed_options.ipc").replace("ipc://", "")

    print(f"\nZeroMQ IPC Sockets:")
    print(f"  • Equity Feed (TICK.EQ)   : {eq_ipc} ({'ACTIVE' if os.path.exists(eq_ipc) else 'IDLE'})")
    print(f"  • Options Feed (TICK.OPT) : {opt_ipc} ({'ACTIVE' if os.path.exists(opt_ipc) else 'IDLE'})")

    # Check data files (both today's dated documents and canonical references)
    today_tag = datetime.now().strftime("%Y%m%d")
    print(f"\nPreparation Documents (Date: {today_tag}):")
    for path, desc in [
        (f"data/instrument_{today_tag}.csv", f"Dated Master ({today_tag})"),
        ("data/instrument.csv", "Canonical Master"),
        (f"data/filtered_{today_tag}.csv", f"Dated F&O Universe ({today_tag})"),
        ("data/filtered.csv", "Canonical F&O Universe"),
        (f"data/index_{today_tag}.json", f"Dated Volume Index ({today_tag})"),
        ("data/index.json", "Canonical Volume Index"),
        (f"data/subscription_manifest_{today_tag}.json", f"Dated Manifest ({today_tag})"),
        ("data/subscription_manifest.json", "Canonical Manifest"),
        (f"recordings/{today_tag}/paper_trades.csv" if os.path.exists(f"recordings/{today_tag}/paper_trades.csv") else "logs/paper_trades.csv", "Paper Trading Journal"),
        (f"recordings/{today_tag}/paper_delayed_trades.csv" if os.path.exists(f"recordings/{today_tag}/paper_delayed_trades.csv") else "logs/paper_delayed_trades.csv", "Paper Delayed Trading Journal"),
        (f"recordings/{today_tag}/live_trades.csv" if os.path.exists(f"recordings/{today_tag}/live_trades.csv") else "logs/live_trades.csv", "Live Trading Journal"),
        ("logs/live_positions_state.json", "Live Positions State (Persistence)"),
        ("logs/active_positions.json", "Active Protected Positions"),
    ]:
        exists = os.path.exists(path)
        sz = f"{os.path.getsize(path)/1024:.1f} KB" if exists else "N/A"
        print(f"  • {desc:<32}: {'[EXISTS ' + sz + ']' if exists else '[MISSING]'}")

    # Check Active Protected Option Subscriptions
    try:
        from core.feed.position_registry import ProtectedPositionRegistry
        prot_summary = ProtectedPositionRegistry.get_instance().get_protected_summary()
        print(f"\nProtected Option Contracts ({len(prot_summary)} contracts guarded from listener unsubscription):")
        if prot_summary:
            for ckey, info in list(prot_summary.items())[:5]:
                holders_str = ", ".join(info.get("holders", []))
                print(f"  🛡️ {info.get('contract_symbol', ckey)}: {holders_str} ({info.get('open_positions_count')} pos)")
            if len(prot_summary) > 5:
                print(f"  ... and {len(prot_summary) - 5} more contracts guarded.")
        else:
            print("  ✓ No active open positions holding options (normal idle state).")
    except Exception:
        pass

    # Check Active Execution Freeze Flags
    active_freezes = []
    for m in ["all", "live", "paper", "paper_delayed", "sim"]:
        ff = os.path.join("logs", f".freeze_{m}")
        if os.path.exists(ff):
            active_freezes.append(m.upper())
    print("\nExecution Freeze Status:")
    if active_freezes:
        print(f"  ❄️ Active Freeze Flags : {', '.join(active_freezes)} (new entries blocked; trailing exits only)")
    else:
        print("  ✓ Normal Execution    : All modes unfrozen (new entries active)")

    return 0


def cmd_post_market(args: argparse.Namespace) -> int:
    """
    Execute post-market archiving pipeline:
    Compresses session recordings in recordings/{session_date}/ or recordings/session_{session_date}_market.jsonl
    into .tar.gz archives to drastically reduce disk usage, and safely removes uncompressed files.
    """
    import re
    import tarfile

    session_date = getattr(args, "date", None)
    if not session_date:
        session_date = datetime.now().strftime("%Y%m%d")

    record_dir = getattr(args, "record_dir", "recordings")
    delete_raw = getattr(args, "delete_raw", False)
    keep_raw_arg = getattr(args, "keep_raw", None)
    if keep_raw_arg is not None:
        keep_raw = bool(keep_raw_arg)
    else:
        keep_raw = not delete_raw
    archive_all = getattr(args, "all", False)

    print("=" * 80)
    print("📦 POST-MARKET RECORDING ARCHIVE PIPELINE")
    print(f"Target Directory: {record_dir} | Session Date: {session_date} | Keep Raw: {keep_raw} | All: {archive_all}")
    print(f"Time            : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    if not os.path.exists(record_dir):
        print(f"❌ Recording directory not found: {record_dir}", file=sys.stderr)
        return 1

    targets_to_archive: List[Tuple[str, str, str]] = []

    if archive_all:
        for item in sorted(os.listdir(record_dir)):
            item_path = os.path.join(record_dir, item)
            if os.path.isdir(item_path) and re.match(r"^\d{8}$", item):
                targets_to_archive.append(("dir", item, item_path))
            elif os.path.isfile(item_path) and re.match(r"^session_\d{8}_market\.jsonl$", item):
                dt = item.split("_")[1]
                targets_to_archive.append(("legacy_file", dt, item_path))
    else:
        folder_path = os.path.join(record_dir, session_date)
        legacy_file = os.path.join(record_dir, f"session_{session_date}_market.jsonl")
        if os.path.isdir(folder_path):
            targets_to_archive.append(("dir", session_date, folder_path))
        elif os.path.isfile(legacy_file):
            targets_to_archive.append(("legacy_file", session_date, legacy_file))
        else:
            archive_path = os.path.join(record_dir, f"{session_date}.tar.gz")
            if os.path.exists(archive_path):
                print(f"  ✓ Session {session_date} is already archived at '{archive_path}' ({os.path.getsize(archive_path)/(1024*1024):.2f} MB).")
                return 0
            print(f"❌ No uncompressed recording found for session date '{session_date}' in '{record_dir}'.", file=sys.stderr)
            return 1

    total_raw_bytes = 0
    total_comp_bytes = 0

    for t_type, dt, t_path in targets_to_archive:
        if t_type == "dir":
            archive_path = os.path.join(record_dir, f"{dt}.tar.gz")
            raw_bytes = sum(
                os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(t_path)
                for f in files
            )
            print(f"\n[Compressing Session {dt}]")
            print(f"  • Source Directory : {t_path} ({raw_bytes / (1024 * 1024):.2f} MB)")
            print(f"  • Target Archive   : {archive_path}")

            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(t_path, arcname=dt)

            comp_bytes = os.path.getsize(archive_path)
            reduction = ((raw_bytes - comp_bytes) / raw_bytes * 100.0) if raw_bytes > 0 else 0.0
            print(f"  ✓ Created {archive_path} ({comp_bytes / (1024 * 1024):.2f} MB) [Savings: {reduction:.1f}% reduction]")

            total_raw_bytes += raw_bytes
            total_comp_bytes += comp_bytes

            if not keep_raw:
                shutil.rmtree(t_path)
                print(f"  ✓ Cleaned up uncompressed raw folder: {t_path}")
            else:
                print("  ℹ️ Raw files preserved (--keep-raw active).")

        elif t_type == "legacy_file":
            archive_path = os.path.join(record_dir, f"session_{dt}_market.tar.gz")
            raw_bytes = os.path.getsize(t_path)
            print(f"\n[Compressing Legacy Session {dt}]")
            print(f"  • Source File    : {t_path} ({raw_bytes / (1024 * 1024):.2f} MB)")
            print(f"  • Target Archive : {archive_path}")

            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(t_path, arcname=os.path.basename(t_path))

            comp_bytes = os.path.getsize(archive_path)
            reduction = ((raw_bytes - comp_bytes) / raw_bytes * 100.0) if raw_bytes > 0 else 0.0
            print(f"  ✓ Created {archive_path} ({comp_bytes / (1024 * 1024):.2f} MB) [Savings: {reduction:.1f}% reduction]")

            total_raw_bytes += raw_bytes
            total_comp_bytes += comp_bytes

            if not keep_raw:
                os.remove(t_path)
                print(f"  ✓ Cleaned up uncompressed raw file: {t_path}")
            else:
                print("  ℹ️ Raw file preserved (--keep-raw active).")

    print("\n" + "=" * 80)
    overall_savings = ((total_raw_bytes - total_comp_bytes) / total_raw_bytes * 100.0) if total_raw_bytes > 0 else 0.0
    saved_mb = (total_raw_bytes - total_comp_bytes) / (1024 * 1024)
    print("🎉 ARCHIVING SUMMARY")
    print(f"  • Sessions Processed : {len(targets_to_archive)}")
    print(f"  • Total Raw Size     : {total_raw_bytes / (1024 * 1024):.2f} MB")
    print(f"  • Total Archive Size : {total_comp_bytes / (1024 * 1024):.2f} MB")
    print(f"  • Net Disk Saved     : {saved_mb:.2f} MB ({overall_savings:.1f}% reduction)")
    print("=" * 80)

    # EOD Maintenance: Prune stale preparation files from older trading sessions and rotate debug logs
    try:
        from core.instruments.cleanup import prune_old_preparation_files
        from core.auth.upstox_auth import prune_debug_log
        prune_old_preparation_files(keep_session_date=session_date)
        prune_debug_log("logs/debug.log")
    except Exception as e:
        print(f"  ⚠️ EOD maintenance note: {e}", file=sys.stderr)

    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop running processes."""
    print("=" * 80)
    print("🛑 STOPPING DISTRIBUTED TRADING SERVICES")
    print("=" * 80)
    killed = kill_all_registered()
    print(f"✓ Terminated {killed} registered background processes.")

    sys_cfg = load_json_config("config/system.json")
    ipc_addr = sys_cfg.get("system", {}).get("ipc", {}).get("address", "/tmp/upstox_market_feed.ipc")
    for path in [ipc_addr, "/tmp/feed_equity.ipc", "/tmp/feed_options.ipc", "/tmp/feed_ticks_pub.ipc", "/tmp/quotes_router.ipc", "/tmp/quotes_heartbeat.pub", "/tmp/scoring_alerts_pub.ipc", "/tmp/rms_strike_pub.ipc"]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    print("All processes stopped.")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run automated test suites."""
    print("=" * 80)
    print("🧪 RUNNING COMPREHENSIVE TEST SUITES")
    print("=" * 80)

    os.environ["TRADING_TEST_MODE"] = "1"
    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py"])
    if res.returncode == 0:
        print("\n" + "=" * 80)
        print("✅ ALL TEST SUITES PASSED (100% GREEN)!")
        print("=" * 80)
    return res.returncode


def cmd_report(args: argparse.Namespace) -> int:
    """Generate End-of-Day P&L analytics report from modern trade journals."""
    session_date = getattr(args, "date", None) or datetime.now().strftime("%Y-%m-%d")
    cands = []
    if session_date:
        if len(session_date) == 8 and session_date.isdigit():
            cands.append(f"recordings/{session_date[:4]}-{session_date[4:6]}-{session_date[6:8]}")
            cands.append(f"recordings/{session_date}")
        elif "-" in session_date:
            cands.append(f"recordings/{session_date}")
            cands.append(f"recordings/{session_date.replace('-', '')}")
        else:
            cands.append(f"recordings/{session_date}")
    else:
        cands.append("recordings")

    journal_files = []
    seen = set()
    for rec_dir in cands:
        if os.path.exists(rec_dir) and os.path.isdir(rec_dir):
            for f in os.listdir(rec_dir):
                if f.endswith(".jsonl"):
                    full_p = os.path.join(rec_dir, f)
                    if full_p not in seen:
                        seen.add(full_p)
                        journal_files.append(full_p)

    for f in os.listdir("logs") if os.path.exists("logs") else []:
        if session_date in f and f.endswith(".jsonl"):
            full_p = os.path.join("logs", f)
            if full_p not in seen:
                seen.add(full_p)
                journal_files.append(full_p)

    print("=" * 80)
    print(f"📊 END-OF-DAY TRADE JOURNAL & P&L REPORT (Date: {session_date})")
    print("=" * 80)

    if not journal_files:
        print(f"  ✓ No trade journals found for session {session_date}.")
        print("=" * 80)
        return 0

    mode_track_pnl = {}
    total_trades = 0
    total_wins = 0
    total_net = 0.0

    for jf in set(journal_files):
        with open(jf, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    t = json.loads(line)
                    mode = t.get("mode", "unknown")
                    track = t.get("track", "unknown")
                    pnl = float(t.get("pnl", 0.0) or t.get("net_pnl", 0.0))
                    entry_p = float(t.get("entry_price", 0.0))
                    exit_p = float(t.get("exit_price", 0.0))
                    pnl_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0

                    key = (mode, track)
                    if key not in mode_track_pnl:
                        mode_track_pnl[key] = {"trades": 0, "wins": 0, "breakevens": 0, "losses": 0, "net_pnl": 0.0}
                    mode_track_pnl[key]["trades"] += 1
                    total_trades += 1
                    if pnl_pct > 0.025:
                        mode_track_pnl[key]["wins"] += 1
                        total_wins += 1
                    elif pnl_pct >= -1e-6:
                        mode_track_pnl[key]["breakevens"] += 1
                    else:
                        mode_track_pnl[key]["losses"] += 1
                    mode_track_pnl[key]["net_pnl"] += pnl
                    total_net += pnl
                except Exception:
                    pass

    header = f"{'MODE':<16} | {'TRACK':<18} | {'TRADES':<8} | {'WINS':<6} | {'B/E':<6} | {'LOSSES':<6} | {'WIN %':<8} | {'NET PNL (INR)'}"
    print(header)
    print("-" * len(header))
    for (m, tr), s in sorted(mode_track_pnl.items()):
        win_pct = (s["wins"] / max(1, s["trades"])) * 100.0
        pnl_str = f"₹{s['net_pnl']:+,.2f}"
        print(f"{m:<16} | {tr:<18} | {s['trades']:<8} | {s['wins']:<6} | {s.get('breakevens', 0):<6} | {s['losses']:<6} | {win_pct:>6.1f}% | {pnl_str}")

    print("-" * len(header))
    tot_win_pct = (total_wins / max(1, total_trades)) * 100.0
    tot_be = sum(s.get("breakevens", 0) for s in mode_track_pnl.values())
    tot_losses = sum(s["losses"] for s in mode_track_pnl.values())
    print(f"{'TOTAL':<16} | {'ALL TRACKS':<18} | {total_trades:<8} | {total_wins:<6} | {tot_be:<6} | {tot_losses:<6} | {tot_win_pct:>6.1f}% | ₹{total_net:+,.2f}")
    print("=" * 80)
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    """Freeze new entry orders for an execution mode while trailing active positions."""
    mode = getattr(args, "mode", "all") or "all"
    mode = mode.lower()
    os.makedirs("logs", exist_ok=True)
    all_modes = ["live", "paper", "paper_delayed", "sim"]

    created_flags = []
    if mode in ("all", "*"):
        all_flag = os.path.join("logs", ".freeze_all")
        with open(all_flag, "w", encoding="utf-8") as f:
            f.write(f"frozen at {datetime.now().isoformat()}\n")
        created_flags.append(all_flag)
        for m in all_modes:
            flag_file = os.path.join("logs", f".freeze_{m}")
            with open(flag_file, "w", encoding="utf-8") as f:
                f.write(f"frozen at {datetime.now().isoformat()} via freeze all\n")
            created_flags.append(flag_file)
    else:
        flag_file = os.path.join("logs", f".freeze_{mode}")
        with open(flag_file, "w", encoding="utf-8") as f:
            f.write(f"frozen at {datetime.now().isoformat()}\n")
        created_flags.append(flag_file)

    signaled_pid = None
    pid_file = os.path.join("logs", "strategy.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR1)
            signaled_pid = pid
        except Exception:
            pass

    print("=" * 80)
    print(f"❄️  EXECUTION MODE FROZEN: {mode.upper()}")
    print("=" * 80)
    for cf in created_flags:
        print(f"  • Flag File Created : {cf}")
    if signaled_pid:
        print(f"  • Signal Sent       : SIGUSR1 delivered to running strategy (PID: {signaled_pid})")
    print(f"  • Existing Trades   : Continues trailing and exiting per targets/SL dynamically.")
    print(f"  • New Entries       : BLOCKED (No new positions will be opened in {mode.upper()} mode).")
    print(f"  • To resume         : python3 runner.py unfreeze --mode {mode}")
    print("=" * 80)
    return 0


def cmd_unfreeze(args: argparse.Namespace) -> int:
    """Unfreeze an execution mode to resume regular order entry."""
    mode = getattr(args, "mode", "all") or "all"
    mode = mode.lower()
    os.makedirs("logs", exist_ok=True)
    all_modes = ["live", "paper", "paper_delayed", "sim"]
    all_flag = os.path.join("logs", ".freeze_all")

    removed_flags = []
    retained_frozen = []

    if mode in ("all", "*"):
        # Unfreezing all removes .freeze_all and all individual .freeze_<mode> files
        if os.path.exists(all_flag):
            try:
                os.remove(all_flag)
                removed_flags.append(all_flag)
            except Exception as e:
                print(f"Error removing {all_flag}: {e}", file=sys.stderr)
        for m in all_modes:
            flag_file = os.path.join("logs", f".freeze_{m}")
            if os.path.exists(flag_file):
                try:
                    os.remove(flag_file)
                    removed_flags.append(flag_file)
                except Exception as e:
                    print(f"Error removing {flag_file}: {e}", file=sys.stderr)
    else:
        # Unfreezing a single mode:
        # If 'all' was frozen, remove .freeze_all and ensure all other modes are individually frozen!
        if os.path.exists(all_flag):
            try:
                os.remove(all_flag)
                removed_flags.append(all_flag)
            except Exception as e:
                print(f"Error removing {all_flag}: {e}", file=sys.stderr)
            for other_m in all_modes:
                if other_m != mode:
                    other_flag = os.path.join("logs", f".freeze_{other_m}")
                    if not os.path.exists(other_flag):
                        with open(other_flag, "w", encoding="utf-8") as f:
                            f.write(f"frozen at {datetime.now().isoformat()} (retained after unfreezing {mode})\n")
                    retained_frozen.append(other_m)

        # Remove the specific mode's flag file
        target_flag = os.path.join("logs", f".freeze_{mode}")
        if os.path.exists(target_flag):
            try:
                os.remove(target_flag)
                removed_flags.append(target_flag)
            except Exception as e:
                print(f"Error removing {target_flag}: {e}", file=sys.stderr)

    signaled_pid = None
    pid_file = os.path.join("logs", "strategy.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR2)
            signaled_pid = pid
        except Exception:
            pass

    print("=" * 80)
    print(f"🔥 EXECUTION MODE UN-FROZEN: {mode.upper()}")
    print("=" * 80)
    for rf in removed_flags:
        print(f"  • Flag File Removed : {rf}")
    if retained_frozen:
        print(f"  • Remaining Frozen  : {', '.join(m.upper() for m in retained_frozen)} (retained in frozen state)")
    if signaled_pid:
        print(f"  • Signal Sent       : SIGUSR2 delivered to running strategy (PID: {signaled_pid})")
    print(f"  • Normal Execution  : RESUMED across {mode.upper()} mode.")
    print("=" * 80)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Manually trigger broker position reconciliation against Groww."""
    from execution.superstructures.live import LiveSuperstructure, GrowwLiveAdapter
    print("=" * 80)
    print("🔄 BROKER POSITION RECONCILIATION & CONFLATION ENGINE")
    print("=" * 80)
    adapter = LiveSuperstructure(
        live_confirmed=getattr(args, "confirm_live", True),
        enable_polling=False,
    )
    res = adapter.reconcile_broker_positions()
    print(f"Status           : {res.get('status')}")
    print(f"Broker Positions : {res.get('broker_positions_count', 0)}")
    print(f"Local Positions  : {res.get('local_positions_count', 0)}")
    if res.get("ghost_positions_closed"):
        print(f"Ghost Positions Resolved : {len(res['ghost_positions_closed'])}")
        for g in res["ghost_positions_closed"]:
            print(f"  • {g['symbol']}: closed externally on broker, local state cleared.")
    if res.get("orphan_positions_adopted"):
        print(f"Orphan Positions Adopted : {len(res['orphan_positions_adopted'])}")
        for o in res["orphan_positions_adopted"]:
            print(f"  • {o['symbol']}: qty {o['quantity']} adopted into local risk tracking.")
    if res.get("quantity_mismatches_adjusted"):
        print(f"Quantity Discrepancies Adjusted : {len(res['quantity_mismatches_adjusted'])}")
        for a in res["quantity_mismatches_adjusted"]:
            print(f"  • {a['symbol']}: adjusted {a['old_quantity']} -> {a['new_quantity']}.")
    print("=" * 80)
    return 0


def cmd_stop_live(args: argparse.Namespace) -> int:
    """Convenience shortcut to freeze live execution."""
    setattr(args, "mode", "live")
    return cmd_freeze(args)


def cmd_rest_fallback(args: argparse.Namespace) -> int:
    """Check or dynamically toggle REST polling fallback for market data listener."""
    action = getattr(args, "action", "status") or "status"
    flag_file = os.path.join("logs", ".allow_rest_fallback")
    os.makedirs("logs", exist_ok=True)
    if action == "enable":
        with open(flag_file, "w", encoding="utf-8") as f:
            f.write(f"enabled at {datetime.now().isoformat()}\n")
        print("=" * 80)
        print("⚠️  REST FALLBACK DYNAMICALLY ENABLED")
        print("=" * 80)
        print(f"  • Flag File Created : {flag_file}")
        print("  • Listener Policy   : Switching to REST poller is now permitted if WebSocket fails.")
        print("================================================================================")
    elif action == "disable":
        if os.path.exists(flag_file):
            try:
                os.remove(flag_file)
            except Exception as e:
                print(f"Error removing {flag_file}: {e}", file=sys.stderr)
        print("=" * 80)
        print("🛡️  REST FALLBACK DYNAMICALLY DISABLED (Optimal Trading Guard Active)")
        print("=" * 80)
        print(f"  • Flag File Removed : {flag_file}")
        print("  • Listener Policy   : WebSocket reconnect strictly enforced; no suboptimal REST polling.")
        print("================================================================================")
    else:
        is_enabled = os.path.exists(flag_file)
        print("=" * 80)
        print(f"📡 REST FALLBACK STATUS: {'ENABLED' if is_enabled else 'DISABLED (Default - Optimal Trading Guard)'}")
        print("=" * 80)
        print(f"  • Disk Flag File    : {flag_file} ({'PRESENT' if is_enabled else 'ABSENT'})")
        print(f"  • Current Policy    : {'Degraded REST quote polling permitted' if is_enabled else 'Strict binary WebSocket streaming only'}")
        print("  • To Change         : python3 runner.py rest-fallback enable | disable")
        print("================================================================================")
    return 0


def main():
    venv_py = os.path.abspath(".venv/bin/python")
    if os.path.exists(venv_py) and os.path.abspath(sys.executable) != venv_py and not os.environ.get("RUNNER_NO_REEXEC"):
        os.environ["RUNNER_NO_REEXEC"] = "1"
        os.execv(venv_py, [venv_py] + sys.argv)

    parser = argparse.ArgumentParser(
        description="Upstox Algorithmic Trading Infrastructure Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Operational commands")

    # Command: core
    p_core = subparsers.add_parser("core", help="Launch Core Ingestion Tier (Feed, CandleManager, QuotesBroker, optional Recorder)")
    p_core.add_argument("--feed", action="store_true", help="Launch live/mock feed handler")
    p_core.add_argument("--candlemanager", action="store_true", help="Launch shared memory candle manager")
    p_core.add_argument("--quotesbroker", action="store_true", help="Launch LVC quotes broker")
    p_core.add_argument("--record", action="store_true", help="Launch market tick recorder")
    p_core.add_argument("--debug", action="store_true", help="Dump raw incoming ticks to recorder debug log")
    p_core.add_argument("--mode", default="ws", choices=["ws", "mock"], help="Feed ingestion mode (default: ws)")

    # Command: feed
    p_feed = subparsers.add_parser("feed", help="Start feed handler with optional forks")
    p_feed.add_argument("--mode", default="ws", choices=["ws", "mock"], help="Feed mode (default: ws)")
    p_feed.add_argument("--candles", action="store_true", help="Also fork and start candle manager")
    p_feed.add_argument("--record", action="store_true", help="Also fork and start recorder")

    # Command: candlemanager
    subparsers.add_parser("candlemanager", help="Start SHM candle manager")

    # Command: quotesbroker
    subparsers.add_parser("quotesbroker", help="Start LVC quotes broker")

    # Command: dashboard
    p_dash = subparsers.add_parser("dashboard", help="Start universal terminal telemetry dashboard")
    p_dash.add_argument("--refresh", type=float, default=1.0, help="Refresh interval seconds")

    # Command: prepare
    p_prep = subparsers.add_parser("prepare", help="Execute pre-market preparation pipeline")
    p_prep.add_argument("--reset", action="store_true", help="Force fresh retrieval and complete rebuild of all documents (ignores existing files)")
    p_prep.add_argument("--date", default=None, help="Session date in YYYYMMDD format (defaults to today)")
    p_prep.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target exchange universe (default: NSE)")
    p_prep.add_argument("--mcx", action="store_true", help="Shortcut for --exchange MCX")
    p_prep.add_argument("--force-download", action="store_true", dest="reset", help="Alias for --reset")
    p_prep.add_argument("--rebuild-volume", action="store_true", dest="reset", help="Alias for --reset")
    p_prep.add_argument("--volume-days", type=int, default=30, help="Number of trading days for volume baseline")

    # Command: listen
    p_listen = subparsers.add_parser("listen", help="Start centralized Market Data Listener service")
    p_listen.add_argument("--mode", default="auto", choices=["auto", "poll", "mock", "ws"], help="Market data ingestion mode (default: auto)")
    p_listen.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target market exchange (default: NSE)")
    p_listen.add_argument("--mcx", action="store_true", help="Shortcut for --exchange MCX")
    p_listen.add_argument("--ipc-path", default="/tmp/upstox_market_feed.ipc", help="IPC socket path")
    p_listen.add_argument("--poll-interval", type=float, default=1.0, help="Quote poll interval in seconds")
    p_listen.add_argument("--max-contracts", type=int, default=1800, help="Maximum contracts to stream simultaneously (Upstox V3 Full mode limit: 2000, default: 1800)")
    p_listen.add_argument("--silent", "-s", action="store_true", help="Run listener in silent mode (suppresses dynamic shifts and heartbeat logs)")
    p_listen.add_argument("--allow-rest-fallback", action="store_true", default=False, help="Allow fallback to REST quote polling if WebSocket fails (default: False, to prevent suboptimal trading)")

    # Command: record
    p_rec = subparsers.add_parser("record", help="Start independent Market Data Recorder service")
    p_rec.add_argument("--date", default=None, help="Session date YYYYMMDD (defaults to today)")
    p_rec.add_argument("--record-dir", default="recordings", help="Output directory for recordings (default: recordings)")
    p_rec.add_argument("--debug", action="store_true", help="Dump raw incoming ticks to recorder debug log")

    # Command: strat
    p_strat = subparsers.add_parser("strat", help="Execute trading strategy against running listener")
    p_strat.add_argument("--strategy", default="momentum", choices=["momentum"], help="Strategy package to execute")
    p_strat.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target market exchange (default: NSE)")
    p_strat.add_argument("--mcx", action="store_true", help="Shortcut for --exchange MCX")
    p_strat.add_argument("--mode", default="paper", help="Execution mode(s): paper, paper_delayed, live, all, or comma-separated e.g. 'paper,live' (default: paper)")
    p_strat.add_argument("--track", default="ALL", help="Filter active strategy tracks: ALL, ATM, ITM, OTM, LOW_PREM, TRAIL_SL, FIXED_TGT, COMBO, DYNAMIC_TRIO")
    p_strat.add_argument("--view", default="all", choices=["all", "matrix", "atm", "itm", "otm", "low_prem", "trail_sl", "fixed_tgt", "combo", "dynamic_trio", "positions", "alerts", "modes", "paper", "paper_delayed", "live"], help="Select dashboard table view")
    p_strat.add_argument("--capital", type=float, default=None, help="Starting capital per simulation track (INR, defaults to execution.json)")
    p_strat.add_argument("--min-score", type=int, default=None, help="Minimum setup score for dashboard display (defaults to strategies.json)")
    p_strat.add_argument("--min-trade-score", type=int, default=None, help="Minimum setup score to trigger trade execution (defaults to strategies.json)")
    p_strat.add_argument("--show-positions", action="store_true", help="Expand active positions table in dashboard")
    p_strat.add_argument("--confirm-live", action="store_true", help="Explicitly authorize live broker execution when mode includes 'live'")
    p_strat.add_argument("--dynamic-lookback", type=int, default=15, help="Rolling lookback window in minutes for DYNAMIC_TRIO allocation (default: 15)")
    p_strat.add_argument("--start-time", "--from-time", dest="start_time", default=None, help="Strategy trade execution start time HH:MM (default: continuous open 09:15). If starting mid-day, prior ticks are ingested in warm-up mode; execution simulates from this time.")
    p_strat.add_argument("--no-backfill", dest="backfill", action="store_false", default=True, help="Disable automatic backfill of recorded ticks from today on strategy startup")
    p_strat.add_argument("--market-open-time", default=None, help="Continuous trading market open time HH:MM IST (default: 09:15 for NSE, 09:00 for MCX)")
    p_strat.add_argument("--market-close-time", default=None, help="Market close time HH:MM IST (default: 15:30 for NSE, 23:30 for MCX)")
    p_strat.add_argument("--eod-time", default=None, help="EOD RMS auto-squareoff cutoff time HH:MM IST (e.g. 15:15 or 15:20, default: strategies.json)")
    p_strat.add_argument("--eod-freeze-time", default=None, help="EOD entry freeze cutoff time HH:MM IST (e.g. 15:15, default: strategies.json)")
    p_strat.add_argument("--config", "--strategy-config", dest="strategy_config", default="config/strategies.json", help="Path to strategy configuration file (default: config/strategies.json)")
    p_strat.add_argument("--execution-config", dest="execution_config", default="config/execution.json", help="Path to execution configuration file (default: config/execution.json)")
    p_strat.add_argument("--record", action="store_true", help="Launch market tick recorder service")
    p_strat.add_argument("--debug", action="store_true", help="Dump raw incoming ticks to recorder debug log")

    # Command: start (alias for strat)
    p_start = subparsers.add_parser("start", help="Execute trading strategy (alias for strat)")
    p_start.add_argument("--strategy", default="momentum", choices=["momentum"], help="Strategy package to execute")
    p_start.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target market exchange (default: NSE)")
    p_start.add_argument("--mcx", action="store_true", help="Shortcut for --exchange MCX")
    p_start.add_argument("--mode", default="paper", help="Execution mode(s): paper, paper_delayed, live, all, or comma-separated e.g. 'paper,live' (default: paper)")
    p_start.add_argument("--tracks", "--track", dest="tracks", default=None, help="Filter active strategy tracks: fast_momentum, trend_pullback, fixed_target, composite, or ALL")
    p_start.add_argument("--legacy", action="store_true", default=False, help="Run legacy strategy engine instead of 2.0 distributed architecture")
    p_start.add_argument("--record", action="store_true", help="Launch market tick recorder service")
    p_start.add_argument("--debug", action="store_true", help="Dump raw incoming ticks to recorder debug log")
    p_start.add_argument("--view", default="all", choices=["all", "matrix", "atm", "itm", "otm", "low_prem", "trail_sl", "fixed_tgt", "combo", "dynamic_trio", "positions", "alerts", "modes", "paper", "paper_delayed", "live"], help="Select dashboard table view")
    p_start.add_argument("--capital", type=float, default=None, help="Starting capital per simulation track (INR, defaults to execution.json)")
    p_start.add_argument("--min-score", type=int, default=None, help="Minimum setup score for dashboard display (defaults to strategies.json)")
    p_start.add_argument("--min-trade-score", type=int, default=None, help="Minimum setup score to trigger trade execution (defaults to strategies.json)")
    p_start.add_argument("--show-positions", action="store_true", help="Expand active positions table in dashboard")
    p_start.add_argument("--confirm-live", action="store_true", help="Explicitly authorize live broker execution when mode includes 'live'")
    p_start.add_argument("--dynamic-lookback", type=int, default=15, help="Rolling lookback window in minutes for DYNAMIC_TRIO allocation (default: 15)")
    p_start.add_argument("--start-time", "--from-time", dest="start_time", default=None, help="Strategy trade execution start time HH:MM (default: continuous open 09:15). If starting mid-day, prior ticks are ingested in warm-up mode; execution simulates from this time.")
    p_start.add_argument("--no-backfill", dest="backfill", action="store_false", default=True, help="Disable automatic backfill of recorded ticks from today on strategy startup")
    p_start.add_argument("--market-open-time", default=None, help="Continuous trading market open time HH:MM IST (default: 09:15 for NSE, 09:00 for MCX)")
    p_start.add_argument("--market-close-time", default=None, help="Market close time HH:MM IST (default: 15:30 for NSE, 23:30 for MCX)")
    p_start.add_argument("--eod-time", default=None, help="EOD RMS auto-squareoff cutoff time HH:MM IST (e.g. 15:15 or 15:20, default: strategies.json)")
    p_start.add_argument("--eod-freeze-time", default=None, help="EOD entry freeze cutoff time HH:MM IST (e.g. 15:15, default: strategies.json)")
    p_start.add_argument("--config", "--strategy-config", dest="strategy_config", default="config/strategies.json", help="Path to strategy configuration file (default: config/strategies.json)")
    p_start.add_argument("--execution-config", dest="execution_config", default="config/execution.json", help="Path to execution configuration file (default: config/execution.json)")

    # Command: replay
    p_replay = subparsers.add_parser("replay", help="Replay recorded market sessions")
    p_replay.add_argument("--date", required=True, help="Session date in YYYYMMDD format (e.g. 20261106)")
    p_replay.add_argument("--strategy", default="momentum", help="Strategy package to replay")
    p_replay.add_argument("--speed", type=float, default=1.0, help="Replay speed multiplier (1.0 = real-time)")
    p_replay.add_argument("--config", "--strategy-config", dest="strategy_config", default="config/strategies.json", help="Path to strategy configuration file (default: config/strategies.json)")
    p_replay.add_argument("--execution-config", dest="execution_config", default="config/execution.json", help="Path to execution configuration file (default: config/execution.json)")

    # Command: simulate (and alias sim)
    for s_name in ("simulate", "sim"):
        p_sim = subparsers.add_parser(s_name, help="Run deterministic high-speed historical market simulation")
        p_sim.add_argument("--data-path", default=None, help="Path to recorded parquet file or directory")
        p_sim.add_argument("--delay-sec", "--delay", dest="delay", type=float, default=None, help="Explicit delay seconds for paper_delayed mode (defaults to execution.json 200-500ms)")
        p_sim.add_argument("--date", default=None, help="Session date YYYYMMDD (defaults to today)")
        p_sim.add_argument("--from-date", dest="from_date", default=None, help="Start date for simulation range YYYYMMDD")
        p_sim.add_argument("--to-date", dest="to_date", default=None, help="End date for simulation range YYYYMMDD")
        p_sim.add_argument("--start-time", "--from-time", dest="start_time", default="09:15", help="Simulation start time HH:MM (default: 09:15)")
        p_sim.add_argument("--end-time", "--to-time", dest="end_time", default="15:15", help="Simulation end time HH:MM (default: 15:15)")
        p_sim.add_argument("--mode", default="paper,paper_delayed", help="Execution mode(s): paper, paper_delayed, or comma-separated (default: paper,paper_delayed)")
        p_sim.add_argument("--track", "--tracks", dest="track", default="DYNAMIC_TRIO", help="Strategy track filter: DYNAMIC_TRIO, ALL, ATM, ITM, OTM, etc. (default: DYNAMIC_TRIO)")
        p_sim.add_argument("--expand-tracks", "--expand", dest="expand_tracks", action="store_true", default=False, help="Expand constituent sub-tracks underneath combined allocation tracks (default: False)")
        p_sim.add_argument("--capital", type=float, default=None, help="Starting capital per simulation track (INR)")
        p_sim.add_argument("--min-score", type=int, default=None, help="Minimum setup score for dashboard display")
        p_sim.add_argument("--min-trade-score", type=int, default=None, help="Minimum setup score to trigger trade execution")
        p_sim.add_argument("--journal", action="store_true", default=False, help="Show full trade journal using P&L engine report")
        p_sim.add_argument("--pdf", action="store_true", default=False, help="Generate publication-quality vector PDF report")
        p_sim.add_argument("--pdf-path", default=None, help="Custom file path for generated PDF report")
        p_sim.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target market exchange (default: NSE)")
        p_sim.add_argument("--mcx", action="store_true", help="Shortcut for --exchange MCX")
        p_sim.add_argument("--config", "--strategy-config", dest="strategy_config", default="config/strategies.json", help="Path to strategy config (default: config/strategies.json)")
        p_sim.add_argument("--execution-config", dest="execution_config", default="config/execution.json", help="Path to execution config")
        p_sim.add_argument("--min-pole-pct", "--pole-pct", dest="min_pole_pct", type=float, default=None, help="Minimum pole height percentage for flag breakouts (e.g. 0.005, 0.01, 1.0)")
        p_sim.add_argument("--entry-freeze-time", "--freeze-time", dest="entry_freeze_time", default=None, help="Hard entry freeze cutoff time HH:MM IST (default: 15:00)")
        p_sim.add_argument("--eod-squareoff-time", "--eod-time", "--squareoff-time", dest="eod_squareoff_time", default=None, help="EOD auto squareoff cutoff time HH:MM IST (default: 15:15)")
        p_sim.add_argument("--brokerage-per-order", dest="brokerage_per_order", type=float, default=None, help="Brokerage per order in INR (buy and sell side, default: 30.0)")

    # Command: post-market (and alias archive)
    for p_name in ("post-market", "archive"):
        p_pm = subparsers.add_parser(p_name, help="Compress session recordings to .tar.gz (preserves raw folder by default)")
        p_pm.add_argument("--date", default=None, help="Session date YYYYMMDD to archive (defaults to today)")
        p_pm.add_argument("--record-dir", default="recordings", help="Output directory for recordings (default: recordings)")
        p_pm.add_argument("--keep-raw", action="store_true", default=None, help="Explicitly preserve uncompressed raw files (default: True)")
        p_pm.add_argument("--delete-raw", action="store_true", help="Delete uncompressed raw files after creating archive")
        p_pm.add_argument("--all", action="store_true", help="Compress all uncompressed sessions in the recordings directory")

    # Command: status
    subparsers.add_parser("status", help="Check system health, socket state, and files")

    # Command: stop
    subparsers.add_parser("stop", help="Stop background trading services")

    # Command: report (and alias report-activity)
    for r_name in ("report", "report-activity"):
        p_rep = subparsers.add_parser(r_name, help="Generate End-of-Day P&L analytics report, terminal line graph, and PDF report")
        p_rep.add_argument("--date", default=None, help="Session date YYYYMMDD (defaults to today)")
        p_rep.add_argument("--mode", default=None, help="Filter by execution mode (e.g. paper, paper_delayed, live, sim, or comma-separated)")
        p_rep.add_argument("--strategy", default=None, help="Filter by strategy name (e.g. momentum)")
        p_rep.add_argument("--start-time", "--from-time", dest="start_time", default=None, help="Filter trades starting at HH:MM or HH:MM:SS (e.g. 09:15)")
        p_rep.add_argument("--end-time", "--to-time", dest="end_time", default=None, help="Filter trades ending at HH:MM or HH:MM:SS (e.g. 11:30)")
        p_rep.add_argument("--track", "--tracks", dest="tracks", default=None, help="Filter tracks for trade journal: 'ATM,OTM,DYN' or 'paper:ATM,OTM,DYN;paper_delayed:TRAIL_SL,DYN'")
        p_rep.add_argument("--trades", type=int, default=None, help="Max trade journal rows per track (0 for complete journal; defaults to 0 when tracks filtered, 30 if all)")
        p_rep.add_argument("--pdf", action="store_true", default=True, help="Generate publication-quality vector PDF report (default: True)")
        p_rep.add_argument("--no-pdf", dest="pdf", action="store_false", help="Disable PDF report generation")
        p_rep.add_argument("--pdf-path", default=None, help="Custom target file path for generated PDF report")

    # Command: freeze
    p_frz = subparsers.add_parser("freeze", help="Freeze execution mode (blocks new entries while trailing active positions)")
    p_frz.add_argument("--mode", default="all", choices=["live", "paper", "paper_delayed", "sim", "all"], help="Execution mode to freeze (default: all)")

    # Command: unfreeze
    p_unf = subparsers.add_parser("unfreeze", help="Unfreeze execution mode (resumes normal new entries)")
    p_unf.add_argument("--mode", default="all", choices=["live", "paper", "paper_delayed", "sim", "all"], help="Execution mode to unfreeze (default: all)")

    # Command: stop-live (shortcut for freeze --mode live)
    subparsers.add_parser("stop-live", help="Convenience shortcut to freeze live mode (closes existing positions only)")

    # Command: reconcile
    p_rec = subparsers.add_parser("reconcile", help="Conflate and reconcile local positions with real-time broker positions")
    p_rec.add_argument("--confirm-live", action="store_true", default=True, help="Authorize live broker query (default: True)")

    # Command: rest-fallback
    p_rf = subparsers.add_parser("rest-fallback", help="Check or dynamically toggle REST polling fallback for listener")
    p_rf.add_argument("action", nargs="?", default="status", choices=["status", "enable", "disable"], help="Action: status (default), enable, or disable")

    # Command: test
    subparsers.add_parser("test", help="Run automated test suites")

    # Command: web
    p_web = subparsers.add_parser("web", help="Launch modern Analytics & Simulation Web UI")
    p_web.add_argument("--host", default="0.0.0.0", help="Web host (default: 0.0.0.0)")
    p_web.add_argument("--port", type=int, default=8080, help="Web port (default: 8080)")
    p_web.add_argument("--record-dir", default="recordings", help="Recordings directory (default: recordings)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "core": cmd_core,
        "feed": cmd_feed,
        "candlemanager": cmd_candlemanager,
        "quotesbroker": cmd_quotesbroker,
        "dashboard": cmd_dashboard,
        "prepare": cmd_prepare,
        "listen": cmd_listen,
        "record": cmd_record,
        "strat": cmd_start,
        "start": cmd_start,
        "replay": cmd_replay,
        "simulate": cmd_simulate,
        "sim": cmd_simulate,
        "post-market": cmd_post_market,
        "archive": cmd_post_market,
        "report": cmd_report,
        "report-activity": cmd_report,
        "freeze": cmd_freeze,
        "unfreeze": cmd_unfreeze,
        "stop-live": cmd_stop_live,
        "rest-fallback": cmd_rest_fallback,
        "reconcile": cmd_reconcile,
        "status": cmd_status,
        "stop": cmd_stop,
        "test": cmd_test,
        "web": cmd_web,
    }

    code = dispatch[args.command](args)
    sys.exit(code)


if __name__ == "__main__":
    main()

