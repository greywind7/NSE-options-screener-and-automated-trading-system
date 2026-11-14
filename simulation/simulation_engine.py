#!/usr/bin/env python3
"""
simulation/simulation_engine.py
===============================
High-Throughput Historical Tick & Depth Simulation Engine:
1. Consumes recorded market ticks and option order book depth events without artificial speed constraints.
2. Maintains a deterministic internal timestamp state as if it was a live session.
3. Supports single-day and multi-day simulations (--date, --from-date, --to-date).
4. Evaluates all 33 strategy tracks and DYNAMIC_TRIO ensemble across execution modes (paper, paper_delayed).
5. Enforces realistic network and matching latency for paper_delayed with dynamic quote re-sampling.
6. Automatically liquidates all open positions at the latest market quotes at the end of the simulation window.
7. Generates clean terminal summaries grouped by execution mode, with optional comprehensive trade journals
   and PDF reports powered by the unified P&L reporting engine.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

from execution.base import BaseExecutionAdapter
from execution.multi_executor import MultiExecutionManager
from infrastructure.common.time_utils import normalize_time_str
from infrastructure.pnl.pnl_aggregator import PnLAggregator
from infrastructure.pnl.report_generator import DailyReportGenerator, ReportColors, get_track_group_color
from strategies.momentum.portfolio import MultiTrackPortfolio, TradeRecord
from strategies.momentum.strategy import MomentumStrategyRunner


class SimulationDummyAdapter(BaseExecutionAdapter):
    """Zero-overhead pass-through execution adapter for simulation."""

    def __init__(self, mode_name: str = "paper"):
        self.mode_name = mode_name.lower()
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []

    def place_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "FILLED", "simulated": True}

    def modify_order(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"status": "MODIFIED", "simulated": True}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return {"status": "CANCELLED", "simulated": True}

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        return {"status": "FILLED", "simulated": True}

    def get_positions(self) -> List[Dict[str, Any]]:
        return []

    def update_stop_loss(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass


def _fmt_inr(val: float, width: int = 14, signed: bool = False) -> str:
    """Format rupee values to strictly match column width, avoiding any character shift."""
    if signed:
        sign = "+" if val > 0 else ("-" if val < 0 else " ")
        s = f"{sign}₹{abs(val):,.2f}"
    else:
        sign = "-" if val < 0 else ""
        s = f"{sign}₹{abs(val):,.2f}"
    return f"{s:>{width}}"


class SimulationEngine:
    """
    Core deterministic simulation orchestrator.
    Streams recorded market events and depth updates chronologically,
    updating quotes_cache and dispatching events to MomentumStrategyRunner.
    """

    def __init__(
        self,
        dates: Optional[List[str]] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        start_time: str = "09:15",
        end_time: str = "15:15",
        modes: str = "paper,paper_delayed",
        track_filter: str = "DYNAMIC_TRIO",
        capital: Optional[float] = None,
        min_score: Optional[int] = None,
        min_trade_score: Optional[int] = None,
        strategy_config: str = "config/strategies.json",
        execution_config: str = "config/execution.json",
        show_journal: bool = False,
        gen_pdf: bool = False,
        pdf_path: Optional[str] = None,
        record_dir: str = "recordings",
        log_dir: str = "logs",
        exchange: str = "NSE",
        expand_tracks: bool = False,
    ):
        self.record_dir = record_dir
        self.log_dir = log_dir
        self.expand_tracks = bool(expand_tracks)
        self.start_time = normalize_time_str(start_time, "09:15")
        self.end_time = normalize_time_str(end_time, "15:15")
        self.modes = [m.strip().lower() for m in str(modes).split(",") if m.strip()]
        self.track_filter = track_filter or "DYNAMIC_TRIO"
        self.capital = capital
        self.min_score = min_score
        self.min_trade_score = min_trade_score
        self.strategy_config = strategy_config
        self.execution_config = execution_config
        self.show_journal = show_journal
        self.gen_pdf = gen_pdf
        self.pdf_path = pdf_path
        self.exchange = exchange.upper()
        self.is_mcx = self.exchange == "MCX"

        # Resolve list of session dates to simulate
        self.dates = self._resolve_dates(dates, from_date, to_date)

    def _resolve_dates(
        self,
        dates: Optional[List[str]],
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> List[str]:
        """Resolves target dates into an ordered list of YYYYMMDD strings."""
        if dates:
            clean = [d.replace("-", "").strip() for d in dates if d.strip()]
            if clean:
                return sorted(list(set(clean)))

        if from_date and to_date:
            f_clean = from_date.replace("-", "").strip()
            t_clean = to_date.replace("-", "").strip()
            try:
                dt_start = datetime.strptime(f_clean, "%Y%m%d")
                dt_end = datetime.strptime(t_clean, "%Y%m%d")
                cur = dt_start
                res = []
                while cur <= dt_end:
                    res.append(cur.strftime("%Y%m%d"))
                    cur += timedelta(days=1)
                return res
            except Exception as e:
                print(f"⚠️ Error parsing date range {from_date} -> {to_date}: {e}", file=sys.stderr)

        if from_date:
            return [from_date.replace("-", "").strip()]

        # Default to today's date
        return [datetime.now().strftime("%Y%m%d")]

    def _find_session_sources(self, session_date: str) -> Tuple[Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
        """
        Locates (events_source, depth_source) for a session date.
        Returns ((path, 'file'|'archive'), (path, 'file'|'archive')).
        """
        s_dir = os.path.join(self.record_dir, session_date)
        events_file = os.path.join(s_dir, "market_events.jsonl")
        ticks_file = os.path.join(s_dir, "ticks.jsonl")
        depth_file = os.path.join(s_dir, "option_depth.jsonl")
        alt_depth_file = os.path.join(s_dir, "options_depth.jsonl")
        archive_file = os.path.join(self.record_dir, f"{session_date}.tar.gz")
        legacy_archive = os.path.join(self.record_dir, f"session_{session_date}_market.tar.gz")

        events_src = None
        depth_src = None

        # 1. Uncompressed folder files
        if os.path.exists(events_file) and os.path.getsize(events_file) > 0:
            events_src = (events_file, "file")
        elif os.path.exists(ticks_file) and os.path.getsize(ticks_file) > 0:
            events_src = (ticks_file, "file")

        if os.path.exists(depth_file) and os.path.getsize(depth_file) > 0:
            depth_src = (depth_file, "file")
        elif os.path.exists(alt_depth_file) and os.path.getsize(alt_depth_file) > 0:
            depth_src = (alt_depth_file, "file")

        # 2. Archive sources
        if not events_src:
            if os.path.exists(archive_file) and os.path.getsize(archive_file) > 0:
                events_src = (archive_file, "archive")
            elif os.path.exists(legacy_archive) and os.path.getsize(legacy_archive) > 0:
                events_src = (legacy_archive, "archive")

        if not depth_src and events_src and events_src[1] == "archive":
            depth_src = (events_src[0], "archive")

        return events_src, depth_src

    def _stream_source_lines(self, src_info: Optional[Tuple[str, str]], target_kind: str) -> Iterator[Dict[str, Any]]:
        """Yields parsed JSON events from file or tar archive."""
        if not src_info:
            return
        src_path, src_type = src_info

        if src_type == "file":
            with open(src_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except Exception:
                            pass
        elif src_type == "archive":
            with tarfile.open(src_path, "r:gz") as tar:
                target_member = None
                members = tar.getmembers()
                for m in members:
                    if target_kind == "depth":
                        if "depth" in m.name.lower() and m.name.endswith(".jsonl"):
                            target_member = m
                            break
                    else:
                        if ("market_events" in m.name.lower() or "ticks" in m.name.lower()) and m.name.endswith(".jsonl"):
                            target_member = m
                            break

                if target_member:
                    f = tar.extractfile(target_member)
                    if f:
                        text_f = io.TextIOWrapper(f, encoding="utf-8")
                        for line in text_f:
                            line = line.strip()
                            if line:
                                try:
                                    yield json.loads(line)
                                except Exception:
                                    pass

    def _stream_merged_chronological(
        self,
        events_src: Optional[Tuple[str, str]],
        depth_src: Optional[Tuple[str, str]],
    ) -> Iterator[Dict[str, Any]]:
        """
        Merges market events and option depth streams in strict chronological order by received_timestamp.
        """
        gen_events = self._stream_source_lines(events_src, "events") if events_src else iter([])
        gen_depth = self._stream_source_lines(depth_src, "depth") if depth_src else iter([])

        evt1 = next(gen_events, None)
        evt2 = next(gen_depth, None)

        while evt1 is not None or evt2 is not None:
            ts1 = float(evt1.get("received_timestamp") or evt1.get("exchange_timestamp") or 0.0) if evt1 else float("inf")
            ts2 = float(evt2.get("received_timestamp") or evt2.get("exchange_timestamp") or 0.0) if evt2 else float("inf")

            if ts1 <= ts2:
                yield evt1
                evt1 = next(gen_events, None)
            else:
                yield evt2
                evt2 = next(gen_depth, None)

    def _resolve_time_bounds(self, session_date: str) -> Tuple[float, float]:
        """Convert HH:MM start and end times into epoch timestamps for this session date."""
        ist = timezone(timedelta(hours=5, minutes=30))
        y, m, d = int(session_date[:4]), int(session_date[4:6]), int(session_date[6:8])

        s_parts = self.start_time.split(":")
        sh = int(s_parts[0])
        sm = int(s_parts[1]) if len(s_parts) > 1 else 0
        ss = int(s_parts[2]) if len(s_parts) > 2 else 0

        e_parts = self.end_time.split(":")
        eh = int(e_parts[0])
        em = int(e_parts[1]) if len(e_parts) > 1 else 0
        es = int(e_parts[2]) if len(e_parts) > 2 else 0

        start_dt = datetime(y, m, d, sh, sm, ss, tzinfo=ist)
        end_dt = datetime(y, m, d, eh, em, es, tzinfo=ist)
        return start_dt.timestamp(), end_dt.timestamp()

    def run_session(self, session_date: str) -> Dict[str, Any]:
        """Execute simulation for a single session date."""
        print("\n" + "=" * 90)
        print(f"🎬 SIMULATION ENGINE: SESSION {session_date}")
        print(f"Time Range : {self.start_time} - {self.end_time} IST | Modes: {', '.join(self.modes).upper()} | Track: {self.track_filter}")
        print("=" * 90)

        events_src, depth_src = self._find_session_sources(session_date)
        if not events_src:
            print(f"❌ No recordings found for date {session_date} in '{self.record_dir}'. Skipping session.\n", file=sys.stderr)
            return {"date": session_date, "status": "NO_RECORDING", "closed_trades": []}

        src_desc = f"Events: {events_src[0]} [{events_src[1].upper()}]"
        if depth_src:
            src_desc += f" | Depth: {depth_src[0]} [{depth_src[1].upper()}]"
        print(f"✓ {src_desc}")

        # Resolve preparation data documents
        vol_path = f"data/index_{session_date}.json" if os.path.exists(f"data/index_{session_date}.json") else "data/index.json"
        manifest_path = (
            f"data/subscription_manifest_{session_date}.json"
            if os.path.exists(f"data/subscription_manifest_{session_date}.json")
            else "data/subscription_manifest.json"
        )

        # Build isolated multi-execution manager with pass-through simulation adapters
        dummy_adapters = {m: SimulationDummyAdapter(m) for m in self.modes}
        multi_exec = MultiExecutionManager(dummy_adapters, enforce_market_hours=False)

        # Setup isolated trade log destination for this simulation session
        sim_trades_file = os.path.join(self.record_dir, session_date, "trades_sim.jsonl")
        sim_journal_file = os.path.join(self.log_dir, f"trade_journal_{session_date}_sim.jsonl")
        os.makedirs(os.path.dirname(sim_trades_file), exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

        sim_aggregator = PnLAggregator(
            session_date=session_date,
            record_dir=self.record_dir,
            log_dir=self.log_dir,
            auto_enumerate_on_exit=False,
        )
        sim_aggregator.recording_file = sim_trades_file
        sim_aggregator.log_file = sim_journal_file

        # Initialize strategy runner with simulation settings
        modes_str = ",".join(self.modes)
        runner = MomentumStrategyRunner(
            mode=modes_str,
            capital=self.capital,
            min_score=self.min_score,
            min_trade_score=self.min_trade_score,
            track_filter=self.track_filter,
            volume_index_path=vol_path,
            manifest_path=manifest_path,
            execution_adapter=multi_exec,
            config_path=self.strategy_config,
            execution_config_path=self.execution_config,
            eod_square_off_time="15:20",
            eod_freeze_time=self.end_time,
            market_open_time=self.start_time,
            market_close_time="15:30",
            enable_dashboard=False,
            is_mcx=self.is_mcx,
        )
        runner.initialize()

        # Connect isolated simulation PnLAggregator to all portfolios
        for port in runner.portfolios.values():
            port.pnl_aggregator = sim_aggregator
            port.output_csv = os.path.join(self.log_dir, f"{port.mode}_sim_trades.csv")

        start_ts, end_ts = self._resolve_time_bounds(session_date)
        print(f"✓ Simulation Clock Bounds: {datetime.fromtimestamp(start_ts).strftime('%H:%M:%S')} -> {datetime.fromtimestamp(end_ts).strftime('%H:%M:%S')} IST")
        print("✓ High-speed chronological ingestion starting...\n")

        total_events = 0
        last_timer_ts = 0.0
        last_matrix_ts = time.time()
        wall_start = time.time()
        final_sim_ts = start_ts
        entry_frozen = False
        user_interrupted = False

        try:
            # Chronological streaming loop
            for event in self._stream_merged_chronological(events_src, depth_src):
                now_ts = float(event.get("received_timestamp") or event.get("exchange_timestamp") or 0.0)
                if now_ts <= 0:
                    continue

                final_sim_ts = max(final_sim_ts, now_ts)

                # Scoring Engine 2.0: Two-Phase Warm-up Stream
                # If now_ts is before the configured start_ts, update indicators/candles in warm-up mode
                if now_ts < start_ts:
                    if not getattr(runner, "is_warming_up", False):
                        runner.set_warming_up(True)
                else:
                    if getattr(runner, "is_warming_up", False):
                        runner.set_warming_up(False)
                        cur_hm = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S")
                        print(f"🔥 [SIMULATION ACTIVE] Start time {self.start_time} IST reached at {cur_hm}. Warm-up complete; live trade execution active!\n")

                # Entry Freeze Gate: When cutoff time is reached, freeze new entries
                if now_ts >= end_ts and not entry_frozen:
                    entry_frozen = True
                    cur_hm = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S")
                    print(f"\n❄️  [SIMULATION FREEZE] Cutoff {self.end_time} IST reached at {cur_hm}. Entry freeze active (no new entries).")
                    print("   Continuing simulation until all open positions reach 0 organically per risk rules...\n")
                    for p in runner.portfolios.values():
                        p.freeze()
                    runner.multi_executor.freeze_mode("all")

                # Dispatch event to strategy
                runner.on_market_event(event)
                total_events += 1

                # Advance periodic timer per simulated second
                if now_ts - last_timer_ts >= 1.0:
                    last_timer_ts = now_ts
                    runner.on_timer(now_ts)

                # Live performance matrix update every 5 wall-clock seconds
                if time.time() - last_matrix_ts >= 5.0:
                    last_matrix_ts = time.time()
                    self._print_live_performance_matrix(
                        session_date=session_date,
                        portfolios=runner.portfolios,
                        quotes_cache=runner.quotes_cache,
                        now_ts=now_ts,
                        total_events=total_events,
                        wall_start=wall_start,
                        is_frozen=entry_frozen,
                    )

                # Organic completion check: in freeze mode, terminate once all open positions reach 0
                if entry_frozen:
                    total_open = sum(len(a.open_positions) for p in runner.portfolios.values() for a in p.tracks.values())
                    if total_open == 0:
                        cur_hm = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S")
                        print(f"\n✓ [ALL POSITIONS CLOSED] At {cur_hm} IST, all open positions have completed exits per risk rules. Simulation complete.\n")
                        break

        except KeyboardInterrupt:
            user_interrupted = True
            cur_hm = datetime.fromtimestamp(final_sim_ts).strftime("%H:%M:%S")
            print(f"\n\n⚠️  [USER INTERRUPT] Ctrl+C received at {cur_hm} IST!")
            print("🚪 Initiating emergency square-off across all open positions at latest real quotes...\n")
            sq_cnt = self._square_off_open_positions(
                portfolios=runner.portfolios,
                quotes_cache=runner.quotes_cache,
                cutoff_quote_ts=final_sim_ts,
                reason="USER_INTERRUPT (Ctrl+C Squareoff)",
                sim_aggregator=sim_aggregator,
            )
            print(f"  ✓ Emergency square-off complete: {sq_cnt} open position(s) liquidated.")

        wall_duration = max(0.01, time.time() - wall_start)
        print(f"\n✓ Completed chronological event consumption: {total_events:,} events in {wall_duration:.2f}s ({total_events/wall_duration:,.0f} ev/s).")

        # If not user interrupted, check if any open positions remain (e.g. end of recording reached)
        if not user_interrupted:
            total_open_remaining = sum(len(a.open_positions) for p in runner.portfolios.values() for a in p.tracks.values())
            if total_open_remaining > 0:
                cur_hm = datetime.fromtimestamp(final_sim_ts).strftime("%H:%M:%S")
                print(f"🚪 End of recorded stream reached at {cur_hm} IST with {total_open_remaining} open position(s). Enforcing square-off...")
                sq_cnt = self._square_off_open_positions(
                    portfolios=runner.portfolios,
                    quotes_cache=runner.quotes_cache,
                    cutoff_quote_ts=final_sim_ts,
                    reason=f"SESSION_CLOSE ({cur_hm} IST End of Recording)",
                    sim_aggregator=sim_aggregator,
                )
                print(f"  ✓ Liquidated {sq_cnt} open position(s) at latest real quotes.")
            else:
                print("✓ All positions cleanly closed organically to 0.")

        # Collect all closed trade records across modes
        all_closed_records: List[Dict[str, Any]] = []
        for m_name, port in runner.portfolios.items():
            for track_name, acct in port.tracks.items():
                for t in acct.closed_trades:
                    all_closed_records.append({
                        "trade_id": t.trade_id,
                        "session_date": session_date,
                        "event": "EXIT",
                        "timestamp": t.exit_time,
                        "datetime_str": datetime.fromtimestamp(t.exit_time).strftime("%Y-%m-%d %H:%M:%S"),
                        "strategy": "momentum",
                        "track": t.strategy_track,
                        "mode": m_name,
                        "symbol": t.symbol,
                        "contract_key": t.contract_key,
                        "contract_symbol": t.contract_symbol,
                        "direction": t.direction,
                        "quantity": t.quantity,
                        "entry_time": t.entry_time,
                        "entry_datetime": datetime.fromtimestamp(t.entry_time).strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_price": t.entry_price,
                        "exit_time": t.exit_time,
                        "exit_datetime": datetime.fromtimestamp(t.exit_time).strftime("%Y-%m-%d %H:%M:%S"),
                        "exit_price": t.exit_price,
                        "gross_pnl": t.gross_pnl,
                        "brokerage": t.brokerage,
                        "net_pnl": t.net_pnl,
                        "return_pct": t.return_pct,
                        "exit_reason": t.exit_reason,
                        "status": "CLOSED",
                    })

        # Render final terminal performance summary report
        self._print_terminal_summary(session_date, runner.portfolios)

        # If journal requested, invoke DailyReportGenerator for exact terminal line graph & trade journal
        if self.show_journal or self.gen_pdf:
            report_gen = DailyReportGenerator(
                session_date=session_date,
                trades=all_closed_records,
            )
            if self.show_journal:
                rep_out = report_gen.generate_report(
                    mode_filter=",".join(self.modes),
                    track_filter=self.track_filter,
                    start_time=self.start_time,
                    end_time=self.end_time,
                    max_trades=0,  # Full journal
                )
                print("\n" + rep_out)

            if self.gen_pdf:
                try:
                    pdf_target = self.pdf_path or os.path.join(self.record_dir, session_date, f"sim_report_{session_date}.pdf")
                    pdf_file = report_gen.generate_pdf(output_path=pdf_target, trades=all_closed_records)
                    print(f"  ✓ High-Resolution Vector Simulation PDF Generated: {pdf_file}\n")
                except Exception as e:
                    print(f"  ⚠️ Simulation PDF Generation Warning: {e}\n", file=sys.stderr)

        return {
            "date": session_date,
            "status": "SUCCESS",
            "events_processed": total_events,
            "duration_sec": wall_duration,
            "portfolios": runner.portfolios,
            "closed_trades": all_closed_records,
            "user_interrupted": user_interrupted,
        }

    def _square_off_open_positions(
        self,
        portfolios: Dict[str, MultiTrackPortfolio],
        quotes_cache: Dict[str, Any],
        cutoff_quote_ts: float,
        reason: str = "SIMULATION_CUTOFF",
        sim_aggregator: Optional[PnLAggregator] = None,
    ) -> int:
        """
        Force liquidates all remaining open positions across portfolios using latest real quotes.
        Strict zero-tolerance on hallucinated prices: orders without valid bid/ltp are skipped.
        """
        total_squared_off = 0
        for m_name, port in portfolios.items():
            if getattr(port, "pending_orders", None):
                port.pending_orders.clear()

            for track_name, acct in port.tracks.items():
                if not acct.open_positions:
                    continue
                for tid, pos in list(acct.open_positions.items()):
                    ck = pos.contract_key or ""
                    opt_q = (
                        quotes_cache.get(ck)
                        or quotes_cache.get(ck.replace(":", "|"))
                        or quotes_cache.get(ck.replace("|", ":"))
                        or (quotes_cache.get(pos.contract_symbol) if pos.contract_symbol else None)
                        or {}
                    )
                    bid = float(opt_q.get("bid") or 0.0)
                    if bid <= 0:
                        bid = float(opt_q.get("last_price") or opt_q.get("ltp") or 0.0)
                    if bid <= 0:
                        print(
                            f"⚠️ [SIMULATION ERROR] Cannot square off {pos.trade_id} ({ck}): "
                            f"No valid bid/ltp quote at cutoff {cutoff_quote_ts}. Skipping liquidation to prevent fabricated price.",
                            file=sys.stderr,
                        )
                        continue

                    pos.exit_time = cutoff_quote_ts
                    pos.exit_price = round(bid, 2)
                    pos.exit_reason = reason
                    pos.gross_pnl = (pos.exit_price - pos.entry_price) * pos.quantity
                    pos.brokerage = 60.0
                    pos.net_pnl = pos.gross_pnl - pos.brokerage
                    pos.return_pct = ((pos.exit_price - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
                    pos.is_closed = True

                    del acct.open_positions[tid]
                    acct.closed_trades.append(pos)
                    if acct.allow_compounding:
                        acct.cash += pos.exit_price * pos.quantity
                    else:
                        open_committed = sum(p.entry_price * p.quantity for p in acct.open_positions.values())
                        max_allowed_cash = max(0.0, acct.starting_capital - open_committed)
                        acct.cash = min(max_allowed_cash, acct.cash + (pos.exit_price * pos.quantity))

                    acct.gross_realized += pos.gross_pnl
                    acct.brokerage_paid += pos.brokerage
                    acct.net_realized += pos.net_pnl
                    if pos.net_pnl > 0:
                        acct.wins += 1
                    else:
                        acct.losses += 1

                    port._log_trade_csv(pos)
                    if sim_aggregator:
                        try:
                            sim_aggregator.record_trade_exit(
                                trade_id=tid,
                                exit_price=pos.exit_price,
                                exit_time=cutoff_quote_ts,
                                exit_reason=pos.exit_reason,
                                gross_pnl=pos.gross_pnl,
                                net_pnl=pos.net_pnl,
                                brokerage=pos.brokerage,
                                strategy="momentum",
                                track=pos.strategy_track,
                                mode=port.mode,
                                symbol=pos.symbol,
                                contract_key=pos.contract_key,
                                contract_symbol=pos.contract_symbol,
                                direction=pos.direction,
                                quantity=pos.quantity,
                                entry_price=pos.entry_price,
                                entry_time=pos.entry_time,
                            )
                        except Exception:
                            pass
                    total_squared_off += 1

        return total_squared_off

    def _print_live_performance_matrix(
        self,
        session_date: str,
        portfolios: Dict[str, MultiTrackPortfolio],
        quotes_cache: Dict[str, Any],
        now_ts: float,
        total_events: int,
        wall_start: float,
        is_frozen: bool,
    ) -> None:
        """Renders live terminal performance matrix while simulation is running."""
        # Update in-place when running interactively in a TTY terminal
        if sys.stdout.isatty():
            sys.stdout.write("\033[H\033[J")
            sys.stdout.flush()

        cur_hm = datetime.fromtimestamp(now_ts).strftime("%H:%M:%S")
        elapsed = max(0.01, time.time() - wall_start)
        rate = total_events / elapsed
        status_tag = (
            f"{ReportColors.CYAN}{ReportColors.BOLD}❄️  ENTRY FREEZE (Monitoring exits until 0 open){ReportColors.RESET}"
            if is_frozen
            else f"{ReportColors.GREEN}{ReportColors.BOLD}🟢 ACTIVE TRADING ({self.start_time} - {self.end_time} IST){ReportColors.RESET}"
        )

        total_open_all = sum(len(a.open_positions) for p in portfolios.values() for a in p.tracks.values())
        total_closed_all = sum(len(a.closed_trades) for p in portfolios.values() for a in p.tracks.values())

        print("\n" + "=" * 128)
        print(f"📊 LIVE SIMULATION MATRIX | Date: {session_date} | Sim Time: {cur_hm} IST | Status: {status_tag}")
        print(f"Events: {total_events:,} ({rate:,.0f} ev/s) | Active Open Pos: {total_open_all} | Closed Trades: {total_closed_all}")
        print("=" * 128)

        for m_name, port in portfolios.items():
            hdr = f"{'STRATEGY TRACK':<28} {'OPEN':>5} {'CLOSED':>7} {'WIN %':>7} {'CASH AVAIL':>14} {'OPEN MTM':>13} {'GROSS REAL':>14} {'BROKERAGE':>10} {'NET REAL':>14} {'ROI':>9}"
            print(f"{ReportColors.BOLD}=== MODE: {m_name.upper()} ==={ReportColors.RESET}")
            print(f"{ReportColors.GRAY}{hdr}{ReportColors.RESET}")
            print(f"{ReportColors.GRAY}{'-' * len(hdr)}{ReportColors.RESET}")

            t_filter = (self.track_filter or "ALL").upper()
            filter_patterns = [p.strip().upper() for p in t_filter.split(",") if p.strip()]
            display_tracks = []
            for t_name, acct in port.tracks.items():
                if any(p in ("ALL", "*") for p in filter_patterns) or any(p in t_name.upper() for p in filter_patterns):
                    display_tracks.append((t_name, acct))

            if not display_tracks and "DYNAMIC_TRIO" in port.tracks:
                display_tracks.append(("DYNAMIC_TRIO", port.tracks["DYNAMIC_TRIO"]))

            def track_sort_key(item):
                ac = item[1]
                return ac.net_realized + ac.compute_mtm(quotes_cache)

            tot_open = 0
            tot_closed = 0
            tot_wins = 0
            tot_cash = 0.0
            tot_mtm = 0.0
            tot_gross = 0.0
            tot_brkr = 0.0
            tot_net = 0.0

            for t_name, acct in sorted(display_tracks, key=track_sort_key, reverse=True):
                op_count = len(acct.open_positions)
                cl_count = len(acct.closed_trades)
                w_pct = (acct.wins / cl_count * 100.0) if cl_count > 0 else 0.0
                mtm = acct.compute_mtm(quotes_cache)
                gross = acct.gross_realized
                brk = acct.brokerage_paid
                net = acct.net_realized
                roi = acct.compute_roi(quotes_cache)

                tot_open += op_count
                tot_closed += cl_count
                tot_wins += acct.wins
                tot_cash += acct.cash
                tot_mtm += mtm
                tot_gross += gross
                tot_brkr += brk
                tot_net += net

                color_net = ReportColors.GREEN if net > 0 else (ReportColors.RED if net < 0 else ReportColors.WHITE)
                color_mtm = ReportColors.GREEN if mtm > 0 else (ReportColors.RED if mtm < 0 else ReportColors.WHITE)

                clean_name = t_name.replace("_FIXED_CAP", " [FX]")
                if t_name == "DYNAMIC_TRIO":
                    clean_name = f"★ {clean_name}"
                elif t_name in getattr(port, "dynamic_trio_champions", []):
                    clean_name = f"◆ {clean_name}"

                trk_col = get_track_group_color(t_name)
                pad_len = max(0, 28 - len(clean_name))
                disp_name = f"{trk_col}{clean_name}{ReportColors.RESET}{' ' * pad_len}"

                sign_roi = "+" if roi > 0 else ("-" if roi < 0 else " ")
                roi_str = f"{sign_roi}{abs(roi):.1f}%"

                print(
                    f"{disp_name} {op_count:>5} {cl_count:>7} {w_pct:>6.1f}% "
                    f"{_fmt_inr(acct.cash, 14, False)} "
                    f"{color_mtm}{_fmt_inr(mtm, 13, True)}{ReportColors.RESET} "
                    f"{_fmt_inr(gross, 14, True)} {_fmt_inr(brk, 10, False)} "
                    f"{color_net}{_fmt_inr(net, 14, True)}{ReportColors.RESET} {roi_str:>9}"
                )

                # For combined tracks, render constituent sub-tracks just below with sub-track indicator if expanded
                if self.expand_tracks and getattr(acct, "constituents", None):
                    for sub_t in acct.constituents:
                        s_open_pos = [p for p in acct.open_positions.values() if getattr(p, "sub_track", "") == sub_t]
                        s_closed_tr = [p for p in acct.closed_trades if getattr(p, "sub_track", "") == sub_t]
                        s_op_count = len(s_open_pos)
                        s_cl_count = len(s_closed_tr)
                        s_w_pct = (sum(1 for p in s_closed_tr if p.net_pnl > 0) / s_cl_count * 100.0) if s_cl_count > 0 else 0.0
                        s_cash = acct.sub_cash.get(sub_t, 0.0)
                        s_mtm = sum(p.compute_mtm(quotes_cache) for p in s_open_pos)
                        s_gross = sum(p.gross_pnl for p in s_closed_tr)
                        s_brk = sum(p.brokerage for p in s_closed_tr)
                        s_net = sum(p.net_pnl for p in s_closed_tr)
                        s_cap = acct.sub_starting_capital.get(sub_t, 40_000.0)
                        s_roi = ((s_net + s_mtm) / s_cap * 100.0) if s_cap > 0 else 0.0

                        s_col_net = ReportColors.GREEN if s_net > 0 else (ReportColors.RED if s_net < 0 else ReportColors.WHITE)
                        s_col_mtm = ReportColors.GREEN if s_mtm > 0 else (ReportColors.RED if s_mtm < 0 else ReportColors.WHITE)
                        s_color = get_track_group_color(sub_t)
                        clean_sub = sub_t.replace("_FIXED_CAP", " [FX]")
                        sub_text = f"  ↳ {clean_sub}"
                        sub_pad = max(0, 28 - len(sub_text))
                        disp_sub = f"  {ReportColors.GRAY}↳{ReportColors.RESET} {s_color}{clean_sub}{ReportColors.RESET}{' ' * sub_pad}"

                        s_sign_roi = "+" if s_roi > 0 else ("-" if s_roi < 0 else " ")
                        s_roi_str = f"{s_sign_roi}{abs(s_roi):.1f}%"

                        print(
                            f"{disp_sub} {s_op_count:>5} {s_cl_count:>7} {s_w_pct:>6.1f}% "
                            f"{_fmt_inr(s_cash, 14, False)} "
                            f"{s_col_mtm}{_fmt_inr(s_mtm, 13, True)}{ReportColors.RESET} "
                            f"{_fmt_inr(s_gross, 14, True)} {_fmt_inr(s_brk, 10, False)} "
                            f"{s_col_net}{_fmt_inr(s_net, 14, True)}{ReportColors.RESET} {s_roi_str:>9}"
                        )

            tot_win_pct = (tot_wins / tot_closed * 100.0) if tot_closed > 0 else 0.0
            tot_color_net = ReportColors.GREEN if tot_net > 0 else (ReportColors.RED if tot_net < 0 else ReportColors.WHITE)
            tot_color_mtm = ReportColors.GREEN if tot_mtm > 0 else (ReportColors.RED if tot_mtm < 0 else ReportColors.WHITE)
            print(f"{ReportColors.GRAY}{'-' * len(hdr)}{ReportColors.RESET}")
            print(
                f"{ReportColors.BOLD}{'MODE TOTAL':<28}{ReportColors.RESET} {tot_open:>5} {tot_closed:>7} {tot_win_pct:>6.1f}% "
                f"{_fmt_inr(tot_cash, 14, False)} "
                f"{tot_color_mtm}{_fmt_inr(tot_mtm, 13, True)}{ReportColors.RESET} "
                f"{_fmt_inr(tot_gross, 14, True)} {_fmt_inr(tot_brkr, 10, False)} "
                f"{tot_color_net}{_fmt_inr(tot_net, 14, True)}{ReportColors.RESET} {'-':>9}\n"
            )

    def _print_terminal_summary(
        self,
        session_date: str,
        portfolios: Dict[str, MultiTrackPortfolio],
    ) -> None:
        """Prints a clean terminal report with track performance grouped by execution mode."""
        print("\n" + "=" * 126)
        print(f"📊 SIMULATION PERFORMANCE REPORT: SESSION {session_date}")
        print("=" * 126)

        for m_name, port in portfolios.items():
            print(f"\n{ReportColors.BOLD}=== EXECUTION MODE: {m_name.upper()} ==={ReportColors.RESET}")
            hdr = f"{'STRATEGY TRACK':<34} | {'TRADES':>7} | {'WIN %':>7} | {'CASH REMAIN':>14} | {'GROSS P&L':>14} | {'BROKERAGE':>10} | {'NET P&L':>14} | {'RETURN %':>9}"
            print(f"{ReportColors.GRAY}{hdr}{ReportColors.RESET}")
            print(f"{ReportColors.GRAY}{'-' * len(hdr)}{ReportColors.RESET}")

            # Filter tracks to show: if user specified a specific track (e.g. DYNAMIC_TRIO), show it prominently
            t_filter = (self.track_filter or "ALL").upper()
            filter_patterns = [p.strip().upper() for p in t_filter.split(",") if p.strip()]
            display_tracks = []
            for t_name, acct in port.tracks.items():
                if any(p in ("ALL", "*") for p in filter_patterns) or any(p in t_name.upper() for p in filter_patterns):
                    display_tracks.append((t_name, acct))

            if not display_tracks and "DYNAMIC_TRIO" in port.tracks:
                display_tracks.append(("DYNAMIC_TRIO", port.tracks["DYNAMIC_TRIO"]))

            tot_trades = 0
            tot_wins = 0
            tot_cash = 0.0
            tot_gross = 0.0
            tot_brkr = 0.0
            tot_net = 0.0

            for t_name, acct in sorted(display_tracks, key=lambda x: x[1].net_realized, reverse=True):
                tr_count = len(acct.closed_trades)
                w_pct = (acct.wins / tr_count * 100.0) if tr_count > 0 else 0.0
                net = acct.net_realized
                gross = acct.gross_realized
                brk = acct.brokerage_paid
                roi = acct.net_roi

                tot_trades += tr_count
                tot_wins += acct.wins
                tot_cash += acct.cash
                tot_gross += gross
                tot_brkr += brk
                tot_net += net

                color = ReportColors.GREEN if net > 0 else (ReportColors.RED if net < 0 else ReportColors.WHITE)
                clean_name = t_name.replace("_FIXED_CAP", " [FX]")
                if t_name == "DYNAMIC_TRIO":
                    clean_name = f"★ {clean_name}"
                elif t_name in getattr(port, "dynamic_trio_champions", []):
                    clean_name = f"◆ {clean_name}"

                trk_col = get_track_group_color(t_name)
                pad_len = max(0, 34 - len(clean_name))
                disp_trk = f"{trk_col}{clean_name}{ReportColors.RESET}{' ' * pad_len}"

                sign_roi = "+" if roi > 0 else ("-" if roi < 0 else " ")
                roi_str = f"{sign_roi}{abs(roi):.1f}%"

                print(
                    f"{disp_trk} | {tr_count:>7} | {w_pct:>6.1f}% | "
                    f"{_fmt_inr(acct.cash, 14, False)} | "
                    f"{_fmt_inr(gross, 14, True)} | "
                    f"{_fmt_inr(brk, 10, False)} | "
                    f"{color}{_fmt_inr(net, 14, True)}{ReportColors.RESET} | "
                    f"{roi_str:>9}"
                )

                # For combined tracks, render constituent sub-tracks just below with sub-track indicator if expanded
                if self.expand_tracks and getattr(acct, "constituents", None):
                    for sub_t in acct.constituents:
                        s_closed_tr = [p for p in acct.closed_trades if getattr(p, "sub_track", "") == sub_t]
                        s_tr_count = len(s_closed_tr)
                        s_w_pct = (sum(1 for p in s_closed_tr if p.net_pnl > 0) / s_tr_count * 100.0) if s_tr_count > 0 else 0.0
                        s_cash = acct.sub_cash.get(sub_t, 0.0)
                        s_gross = sum(p.gross_pnl for p in s_closed_tr)
                        s_brk = sum(p.brokerage for p in s_closed_tr)
                        s_net = sum(p.net_pnl for p in s_closed_tr)
                        s_cap = acct.sub_starting_capital.get(sub_t, 40_000.0)
                        s_roi = (s_net / s_cap * 100.0) if s_cap > 0 else 0.0

                        s_col = ReportColors.GREEN if s_net > 0 else (ReportColors.RED if s_net < 0 else ReportColors.WHITE)
                        clean_sub = sub_t.replace("_FIXED_CAP", " [FX]")
                        s_grp_col = get_track_group_color(sub_t)
                        sub_text = f"  ↳ {clean_sub}"
                        sub_pad = max(0, 34 - len(sub_text))
                        disp_sub = f"  {ReportColors.GRAY}↳{ReportColors.RESET} {s_grp_col}{clean_sub}{ReportColors.RESET}{' ' * sub_pad}"

                        s_sign_roi = "+" if s_roi > 0 else ("-" if s_roi < 0 else " ")
                        s_roi_str = f"{s_sign_roi}{abs(s_roi):.1f}%"

                        print(
                            f"{disp_sub} | {s_tr_count:>7} | {s_w_pct:>6.1f}% | "
                            f"{_fmt_inr(s_cash, 14, False)} | "
                            f"{_fmt_inr(s_gross, 14, True)} | "
                            f"{_fmt_inr(s_brk, 10, False)} | "
                            f"{s_col}{_fmt_inr(s_net, 14, True)}{ReportColors.RESET} | "
                            f"{s_roi_str:>9}"
                        )

            tot_starting_cap = sum(acct.starting_capital for _, acct in display_tracks)
            tot_roi = (tot_net / tot_starting_cap * 100.0) if tot_starting_cap > 0 else 0.0
            tot_win_pct = (tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0
            tot_color = ReportColors.GREEN if tot_net > 0 else (ReportColors.RED if tot_net < 0 else ReportColors.WHITE)
            tot_sign_roi = "+" if tot_roi > 0 else ("-" if tot_roi < 0 else " ")
            tot_roi_str = f"{tot_sign_roi}{abs(tot_roi):.1f}%"

            print(f"{ReportColors.GRAY}{'-' * len(hdr)}{ReportColors.RESET}")
            print(
                f"{ReportColors.BOLD}{'MODE TOTAL':<34}{ReportColors.RESET} | "
                f"{tot_trades:>7} | {tot_win_pct:>6.1f}% | "
                f"{_fmt_inr(tot_cash, 14, False)} | "
                f"{_fmt_inr(tot_gross, 14, True)} | "
                f"{_fmt_inr(tot_brkr, 10, False)} | "
                f"{tot_color}{_fmt_inr(tot_net, 14, True)}{ReportColors.RESET} | "
                f"{tot_roi_str:>9}\n"
            )

    def run(self) -> int:
        """Run simulation across all requested session dates."""
        print("=" * 90)
        print("🚀 HISTORICAL MARKET SIMULATION ENGINE")
        print(f"Target Dates : {', '.join(self.dates)} ({len(self.dates)} session(s))")
        print(f"Window       : {self.start_time} - {self.end_time} IST")
        print(f"Modes        : {', '.join(self.modes).upper()}")
        print(f"Track Filter : {self.track_filter}")
        print("=" * 90)

        try:
            for d in self.dates:
                res = self.run_session(d)
                if res.get("status") == "ERROR":
                    return 1
                if res.get("user_interrupted"):
                    print("🛑 Simulation execution halted by user interrupt (Ctrl+C).")
                    break
        except KeyboardInterrupt:
            print("\n🛑 Simulation execution interrupted by user.\n")
            return 0

        print("=" * 90)
        print("✅ SIMULATION PIPELINE COMPLETE")
        print("=" * 90 + "\n")
        return 0
