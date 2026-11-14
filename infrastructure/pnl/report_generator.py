#!/usr/bin/env python3
"""
infrastructure/pnl/report_generator.py
======================================
End-of-Day P&L Report Generator & Terminal Line Graph Visualizer:
1. Ingests structured trade journals from:
   - recordings/{session_date}/trades.jsonl
   - logs/trade_journal_{session_date}.jsonl
   - Fallback: logs/paper_trades.csv, logs/sandbox_trades.csv, logs/live_trades.csv
2. Computes granular performance metrics:
   - Gross & Net Realized P&L, Brokerage friction
   - Win Rate, Profit Factor, Best/Worst trades
   - Breakdowns by Execution Mode (live, paper, sandbox)
   - Breakdowns by Strategy and Sub-Strategy Track (ATM, COMBO, DYNAMIC_TRIO, etc.)
3. Renders terminal-native Unicode / ASCII cumulative P&L line graphs
   and sparklines across the full trading day.
"""

from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Tuple


class ReportColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"


def get_track_group_color(track_name: str) -> str:
    """Return ANSI color code corresponding to a track family."""
    tn = str(track_name or "").upper()
    if "DYNAMIC_TRIO" in tn:
        return ReportColors.YELLOW + ReportColors.BOLD
    elif "TRAIL_SL" in tn or "TRAILING" in tn:
        return ReportColors.MAGENTA
    elif "FIXED_TGT" in tn or "FIXED_TARGET" in tn:
        return ReportColors.BLUE
    elif "COMBINED" in tn:
        return ReportColors.WHITE + ReportColors.BOLD
    elif "ITM" in tn or "ATM" in tn or "OTM" in tn:
        return ReportColors.CYAN
    return ReportColors.WHITE


def format_hhmmss(val: Any) -> str:
    """Format timestamp or datetime string to HH:MM:SS."""
    if not val:
        return ""
    if isinstance(val, (int, float)):
        if val > 0:
            try:
                return datetime.fromtimestamp(val).strftime("%H:%M:%S")
            except Exception:
                pass
    s = str(val).strip()
    if " " in s:
        parts = s.split()
        if len(parts) >= 2 and len(parts[1]) >= 8:
            return parts[1][:8]
    if ":" in s:
        parts = s.split(":")
        try:
            if len(parts) == 2:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
            elif len(parts) >= 3:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2][:2]):02d}"
        except Exception:
            pass
    return s


def normalize_time_filter(t_str: Optional[str], is_end: bool = False) -> Optional[str]:
    """Normalize input time like '09:15' or '09:15:30' to '09:15:00' (or '09:15:59' if is_end)."""
    if not t_str:
        return None
    s = str(t_str).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else (59 if is_end else 0)
        sec = int(parts[2]) if len(parts) > 2 else (59 if is_end else 0)
        return f"{h:02d}:{m:02d}:{sec:02d}"
    except Exception:
        return s


def parse_track_filter(track_arg: Optional[str]) -> Tuple[Dict[str, List[str]], bool]:
    """
    Parses track filter argument into mode -> list of patterns.
    Examples:
    1. Mode-specific: "paper:ATM,OTM,DYN;paper_delayed:TRAIL_SL,DYN"
       -> ({'paper': ['ATM', 'OTM', 'DYN'], 'paper_delayed': ['TRAIL_SL', 'DYN']}, True)
    2. Global: "ATM,OTM,DYN"
       -> ({'*': ['ATM', 'OTM', 'DYN']}, False)
    """
    if not track_arg:
        return {}, False

    track_map: Dict[str, List[str]] = defaultdict(list)
    has_mode_prefix = False

    segments = [s.strip() for s in str(track_arg).split(";") if s.strip()]
    for seg in segments:
        if ":" in seg:
            has_mode_prefix = True
            m_part, t_part = seg.split(":", 1)
            m_key = m_part.strip().lower()
            patterns = [p.strip() for p in t_part.split(",") if p.strip()]
            track_map[m_key].extend(patterns)
        else:
            patterns = [p.strip() for p in seg.split(",") if p.strip()]
            track_map["*"].extend(patterns)

    return dict(track_map), has_mode_prefix


def matches_track_filter(mode: str, track: str, track_map: Dict[str, List[str]]) -> bool:
    """Check whether a given (mode, track) matches the parsed track filter map."""
    if not track_map:
        return True
    m_lower = str(mode or "").lower()
    t_lower = str(track or "").lower()

    patterns = track_map.get(m_lower)
    if patterns is None:
        patterns = track_map.get("*")
    if patterns is None:
        return False
    if any(p.lower() in ("all", "*") for p in patterns):
        return True
    return any(p.lower() in t_lower for p in patterns)


def render_ascii_line_graph(
    data_points: List[Tuple[float, float]],
    width: int = 70,
    height: int = 12,
    title: str = "CUMULATIVE NET P&L",
) -> List[str]:
    """
    Render terminal-native Unicode line graph for a time-series of (timestamp, value).
    Pure Python standard library implementation with KaTeX/Unicode axes.
    """
    if not data_points:
        return [f"  {ReportColors.GRAY}(No trade data points to plot graph){ReportColors.RESET}"]

    data_points = sorted(data_points, key=lambda x: x[0])
    values = [p[1] for p in data_points]
    timestamps = [p[0] for p in data_points]

    min_val = min(values)
    max_val = max(values)

    # Pad range slightly so lines don't clip at top/bottom
    if min_val == max_val:
        min_val -= 100.0
        max_val += 100.0
    val_range = max_val - min_val
    if val_range <= 0:
        val_range = 1.0

    t_min = timestamps[0]
    t_max = timestamps[-1]
    t_span = max(1.0, t_max - t_min)

    # Sample values into grid columns
    plot_width = max(10, width - 14)
    col_values: List[float] = []

    for c in range(plot_width):
        t_target = t_min + (c / max(1, plot_width - 1)) * t_span
        # Find nearest point
        nearest = min(data_points, key=lambda p: abs(p[0] - t_target))
        col_values.append(nearest[1])

    # Build character canvas (height rows by plot_width cols)
    canvas = [[" " for _ in range(plot_width)] for _ in range(height)]

    # Draw zero reference line if zero is in range
    zero_row = None
    if min_val <= 0.0 <= max_val:
        zero_row = height - 1 - int(round(((0.0 - min_val) / val_range) * (height - 1)))
        zero_row = max(0, min(height - 1, zero_row))
        for c in range(plot_width):
            canvas[zero_row][c] = "·"

    # Plot data points onto canvas
    prev_r = None
    for c, val in enumerate(col_values):
        norm = (val - min_val) / val_range
        r = height - 1 - int(round(norm * (height - 1)))
        r = max(0, min(height - 1, r))

        if prev_r is not None and abs(r - prev_r) > 1:
            step = 1 if r > prev_r else -1
            for inter_r in range(prev_r + step, r, step):
                canvas[inter_r][c] = "│"

        if c == plot_width - 1:
            canvas[r][c] = "●"
        elif prev_r is not None and r < prev_r:
            canvas[r][c] = "╭"
        elif prev_r is not None and r > prev_r:
            canvas[r][c] = "╰"
        else:
            canvas[r][c] = "─"
        prev_r = r

    lines = []
    # Header
    last_val = col_values[-1]
    pnl_col = ReportColors.GREEN if last_val >= 0 else ReportColors.RED
    sign = "+" if last_val > 0 else ""
    lines.append(f" {ReportColors.BOLD}📈 {title}: {pnl_col}{sign}₹{last_val:,.2f}{ReportColors.RESET}")
    lines.append(f" {ReportColors.GRAY}{'─' * (width + 2)}{ReportColors.RESET}")

    # Render canvas rows with Y-axis labels
    for r in range(height):
        row_val = max_val - (r / (height - 1)) * val_range
        v_sign = "+" if row_val > 0 else ""
        if abs(row_val) >= 100_000:
            lbl = f"{v_sign}₹{row_val/100000:.1f}L"
        elif abs(row_val) >= 1_000:
            lbl = f"{v_sign}₹{row_val/1000:.1f}k"
        else:
            lbl = f"{v_sign}₹{row_val:.0f}"

        row_str = "".join(canvas[r])
        # Colorize characters
        colored_chars = []
        for ch in row_str:
            if ch == "●":
                colored_chars.append(f"{ReportColors.BOLD}{pnl_col}●{ReportColors.RESET}")
            elif ch in ("─", "╭", "╰", "│"):
                colored_chars.append(f"{pnl_col}{ch}{ReportColors.RESET}")
            elif ch == "·":
                colored_chars.append(f"{ReportColors.GRAY}·{ReportColors.RESET}")
            else:
                colored_chars.append(" ")

        line_body = "".join(colored_chars)
        axis_char = "┼" if (zero_row is not None and r == zero_row) else "┤"
        lines.append(f" {ReportColors.GRAY}{lbl:>10} {axis_char}{ReportColors.RESET}{line_body}")

    # Bottom axis and time labels
    lines.append(f" {ReportColors.GRAY}{'':>10} └{'─' * plot_width}{ReportColors.RESET}")
    t_start_str = datetime.fromtimestamp(t_min).strftime("%H:%M")
    t_mid_str = datetime.fromtimestamp((t_min + t_max) / 2.0).strftime("%H:%M")
    t_end_str = datetime.fromtimestamp(t_max).strftime("%H:%M")

    axis_space = plot_width - len(t_start_str) - len(t_mid_str) - len(t_end_str)
    s1 = " " * max(1, axis_space // 2)
    s2 = " " * max(1, axis_space - (axis_space // 2))
    time_line = f" {ReportColors.CYAN}{'':>12}{t_start_str}{s1}{t_mid_str}{s2}{t_end_str}{ReportColors.RESET}"
    lines.append(time_line)

    return lines


class DailyReportGenerator:
    """
    Ingests daily session trade records and formats performance analytics.
    """

    def __init__(
        self,
        session_date: Optional[str] = None,
        trades_file: Optional[str] = None,
        trades: Optional[List[Dict[str, Any]]] = None,
    ):
        self.session_date = session_date or datetime.now().strftime("%Y%m%d")
        self.trades_file = trades_file
        self.provided_trades = trades
        self.last_filtered_trades: List[Dict[str, Any]] = []

    def load_trades(self) -> List[Dict[str, Any]]:
        """Load trades from JSONL recordings or fallback CSV files."""
        if self.provided_trades is not None:
            trades = list(self.provided_trades)
            trades.sort(key=lambda t: float(t.get("entry_time") or t.get("timestamp") or 0.0))
            return trades

        trades = []
        seen_trades = set()

        # 1. Discover all candidate JSONL files for self.session_date (including enumerated runs)
        jsonl_files: List[str] = []
        if self.trades_file and os.path.exists(self.trades_file):
            jsonl_files.append(self.trades_file)

        # Check recordings/{self.session_date}/trades*.jsonl
        rec_dir = os.path.join("recordings", self.session_date)
        if os.path.exists(rec_dir) and os.path.isdir(rec_dir):
            for fname in sorted(os.listdir(rec_dir)):
                if fname.startswith("trades") and fname.endswith(".jsonl"):
                    jsonl_files.append(os.path.join(rec_dir, fname))

        # Check logs/trade_journal_{self.session_date}*.jsonl
        if os.path.exists("logs") and os.path.isdir("logs"):
            prefix = f"trade_journal_{self.session_date}"
            for fname in sorted(os.listdir("logs")):
                if fname.startswith(prefix) and fname.endswith(".jsonl"):
                    jsonl_files.append(os.path.join("logs", fname))

        # Also fallback candidate files
        for jp in [f"logs/trade_journal_{self.session_date}.jsonl", "logs/trades.jsonl"]:
            if os.path.exists(jp) and jp not in jsonl_files:
                jsonl_files.append(jp)

        for jpath in jsonl_files:
            if not os.path.exists(jpath) or os.path.getsize(jpath) == 0:
                continue
            try:
                with open(jpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)

                        # Only process EXIT / CLOSED events for complete trade lifecycle
                        if data.get("event") != "EXIT" and data.get("status") != "CLOSED":
                            continue

                        # Map legacy / simulated mode to paper
                        if str(data.get("mode", "")).lower() == "sim":
                            data["mode"] = "paper"

                        tid = str(data.get("trade_id", ""))
                        etime = str(data.get("entry_time") or data.get("entry_datetime") or "")
                        xtime = str(data.get("exit_time") or data.get("timestamp") or "")
                        strat_track = str(data.get("track", ""))
                        # Deduplication key across twin files or duplicate lines
                        dedup_key = (tid, etime, xtime, strat_track)
                        if dedup_key in seen_trades:
                            continue
                        seen_trades.add(dedup_key)
                        trades.append(data)
            except Exception:
                pass

        if trades:
            # Sort chronologically by entry_time or timestamp
            trades.sort(key=lambda t: float(t.get("entry_time") or t.get("timestamp") or 0.0))
            return trades

        # 2. Fallback to CSV journals if JSONL not yet written (dated recordings first, logs second)
        rec_sdir = os.path.join("recordings", self.session_date)
        csv_candidates = [
            ("paper", os.path.join(rec_sdir, "paper_trades.csv")),
            ("paper_delayed", os.path.join(rec_sdir, "paper_delayed_trades.csv")),
            ("live", os.path.join(rec_sdir, "live_trades.csv")),
            ("paper", "logs/paper_trades.csv"),
            ("paper_delayed", "logs/paper_delayed_trades.csv"),
            ("live", "logs/live_trades.csv"),
        ]

        date_prefix = f"{self.session_date[:4]}-{self.session_date[4:6]}-{self.session_date[6:8]}"

        for mode, cpath in csv_candidates:
            if not os.path.exists(cpath):
                continue
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for r in reader:
                        e_time = r.get("entry_time", "") or ""
                        # Filter by session date if present in date_prefix
                        if date_prefix and e_time and not e_time.startswith(date_prefix):
                            continue

                        net_p = float(r.get("net_pnl") or 0.0)
                        gross_p = float(r.get("gross_pnl") or 0.0)
                        brkr = float(r.get("brokerage") or 60.0)

                        tid = r.get("trade_id") or f"CSV-{mode}-{len(trades)}"
                        strat_track = r.get("strategy_track") or r.get("strike_sim") or "ATM"

                        # Parse timestamp
                        ts = time.time()
                        if e_time:
                            try:
                                ts = datetime.strptime(e_time, "%Y-%m-%d %H:%M:%S").timestamp()
                            except Exception:
                                pass

                        trades.append({
                            "trade_id": tid,
                            "strategy": "momentum",
                            "track": strat_track,
                            "mode": mode,
                            "symbol": r.get("symbol", ""),
                            "contract_symbol": r.get("contract_symbol", ""),
                            "direction": r.get("direction", "BULL"),
                            "quantity": int(r.get("quantity") or 1),
                            "entry_price": float(r.get("entry_price") or 0.0),
                            "exit_price": float(r.get("exit_price") or 0.0),
                            "entry_time": ts,
                            "exit_time": ts + 60.0,
                            "timestamp": ts + 60.0,
                            "gross_pnl": gross_p,
                            "brokerage": brkr,
                            "net_pnl": net_p,
                            "return_pct": float(r.get("return_pct") or 0.0),
                            "exit_reason": r.get("exit_reason", ""),
                            "status": "CLOSED",
                            "event": "EXIT",
                        })
            except Exception:
                pass

        return trades

    def generate_pdf(
        self,
        output_path: Optional[str] = None,
        trades: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Generate high-resolution vector PDF report for current session."""
        from infrastructure.pnl.pdf_report import generate_daily_pdf_report
        target_trades = trades if trades is not None else (self.last_filtered_trades or self.load_trades())
        return generate_daily_pdf_report(self.session_date, target_trades, output_path=output_path)

    def generate_report(
        self,
        mode_filter: Optional[str] = None,
        strategy_filter: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        track_filter: Optional[str] = None,
        max_trades: Optional[int] = None,
    ) -> str:
        """
        Generate comprehensive end-of-day P&L analytics report:
        1. Executive summary comparison across execution modes (without conflating tracks into a single net sum).
        2. Mode-by-mode deep dive with dedicated cumulative line graph & track breakdown.
        3. Comprehensive trade journal grouped by mode & track with entry/exit verification.
        """
        all_trades = self.load_trades()

        # Parse track and time filters
        track_map, has_mode_prefix = parse_track_filter(track_filter)
        start_norm = normalize_time_filter(start_time, is_end=False)
        end_norm = normalize_time_filter(end_time, is_end=True)

        # Determine allowed modes
        allowed_modes: Optional[Set[str]] = None
        if mode_filter and mode_filter.lower() not in ("all", "*"):
            allowed_modes = {m.strip().lower() for m in mode_filter.split(",") if m.strip()}
        elif has_mode_prefix:
            allowed_modes = {m.lower() for m in track_map.keys() if m != "*"}

        # Determine default max_trades per track
        # If user explicitly requested specific tracks, default to 0 (show full journal!)
        if max_trades is None:
            max_trades = 0 if track_filter else 30

        # Filter trades
        closed_trades: List[Dict[str, Any]] = []
        for t in all_trades:
            if not (t.get("status") == "CLOSED" or t.get("event") == "EXIT"):
                continue

            m_str = str(t.get("mode", "paper")).lower()
            if allowed_modes and m_str not in allowed_modes:
                continue

            if strategy_filter and t.get("strategy", "").lower() != strategy_filter.lower():
                continue

            # Time filter check: active window matching
            e_str = format_hhmmss(t.get("entry_datetime") or t.get("entry_time"))
            x_str = format_hhmmss(t.get("exit_datetime") or t.get("exit_time") or t.get("timestamp"))

            # Drop trade if it exited strictly before start_norm
            if start_norm and (x_str or e_str) and (x_str or e_str) < start_norm:
                continue
            # Drop trade if it entered strictly after end_norm
            if end_norm and (e_str or x_str) and (e_str or x_str) > end_norm:
                continue

            closed_trades.append(t)

        self.last_filtered_trades = closed_trades

        # Report Header
        lines = []
        lines.append("=" * 104)
        lines.append(f"📊 {ReportColors.BOLD}END-OF-DAY ACTIVITY & P&L REPORT{ReportColors.RESET}")
        w_desc = f"{start_norm or '09:15:00'} - {end_norm or '15:30:00'}" if (start_norm or end_norm) else "FULL SESSION"
        lines.append(f"Session Date: {self.session_date} | Time Window: {w_desc} | Total Closed Trades: {len(closed_trades)}")
        lines.append(f"Generated At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 104)

        if not closed_trades:
            lines.append(f"\n{ReportColors.GRAY}No closed trades recorded matching the specified filters for session {self.session_date}.{ReportColors.RESET}\n")
            lines.append("=" * 104)
            return "\n".join(lines)

        # Group by execution mode
        by_mode: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in closed_trades:
            m = str(t.get("mode", "paper")).upper()
            by_mode[m].append(t)

        # =========================================================================
        # 1. EXECUTIVE SUMMARY ACROSS MODES (No conflated single net sum)
        # =========================================================================
        lines.append(f"\n{ReportColors.BOLD}1. EXECUTIVE SESSION SUMMARY (BY EXECUTION MODE):{ReportColors.RESET}")
        m_hdr = f"{'MODE':<14} | {'TRADES (W/L)':^14} | {'WIN RATE':>10} | {'GROSS P&L':>14} | {'NET P&L':>16} | {'PF':>6} | {'TOP PERFORMING TRACK'}"
        lines.append(f"  {ReportColors.GRAY}{m_hdr}{ReportColors.RESET}")
        lines.append(f"  {'-' * len(m_hdr)}")

        for mode_name, m_trades in sorted(by_mode.items()):
            m_gross = sum(float(t.get("gross_pnl", 0.0)) for t in m_trades)
            m_net = sum(float(t.get("net_pnl", 0.0)) for t in m_trades)
            m_wins = sum(1 for t in m_trades if float(t.get("net_pnl", 0.0)) > 0)
            m_losses = len(m_trades) - m_wins
            m_wr = (m_wins / len(m_trades) * 100.0) if m_trades else 0.0
            m_pos = sum(float(t.get("net_pnl", 0.0)) for t in m_trades if float(t.get("net_pnl", 0.0)) > 0)
            m_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in m_trades if float(t.get("net_pnl", 0.0)) < 0)
            m_pf = (m_pos / m_neg) if m_neg > 0 else (99.9 if m_pos > 0 else 0.0)

            # Find top performing track in this mode
            mode_tracks: Dict[str, float] = defaultdict(float)
            for t in m_trades:
                mode_tracks[str(t.get("track", "ATM"))] += float(t.get("net_pnl", 0.0))
            top_trk = max(mode_tracks.items(), key=lambda x: x[1], default=("NONE", 0.0))
            top_trk_str = f"{top_trk[0]} (+₹{top_trk[1]:,.0f})" if top_trk[1] > 0 else f"{top_trk[0]} (₹{top_trk[1]:,.0f})"

            c = ReportColors.GREEN if m_net > 0 else (ReportColors.RED if m_net < 0 else ReportColors.WHITE)
            s_sign = "+" if m_net > 0 else ""

            lines.append(
                f"  {ReportColors.BOLD}{mode_name:<14}{ReportColors.RESET} | "
                f"{len(m_trades):>3} ({m_wins}W/{m_losses}L)     | "
                f"{m_wr:>9.1f}% | "
                f"₹{m_gross:>12,.2f} | "
                f"{c}{s_sign}₹{m_net:>14,.2f}{ReportColors.RESET} | "
                f"{m_pf:>6.2f} | "
                f"{ReportColors.CYAN}{top_trk_str}{ReportColors.RESET}"
            )
        lines.append(f"  {'-' * len(m_hdr)}")

        # =========================================================================
        # 2. MODE-BY-MODE PERFORMANCE DEEP DIVES (Separate graphs & track tables)
        # =========================================================================
        lines.append(f"\n{ReportColors.BOLD}2. PERFORMANCE BREAKDOWN BY EXECUTION MODE:{ReportColors.RESET}")

        for mode_idx, (mode_name, m_trades) in enumerate(sorted(by_mode.items()), start=1):
            m_gross = sum(float(t.get("gross_pnl", 0.0)) for t in m_trades)
            m_net = sum(float(t.get("net_pnl", 0.0)) for t in m_trades)
            m_brkr = sum(float(t.get("brokerage", 0.0)) for t in m_trades)
            m_wins = sum(1 for t in m_trades if float(t.get("net_pnl", 0.0)) > 0)
            m_losses = len(m_trades) - m_wins
            m_wr = (m_wins / len(m_trades) * 100.0) if m_trades else 0.0
            m_pos = sum(float(t.get("net_pnl", 0.0)) for t in m_trades if float(t.get("net_pnl", 0.0)) > 0)
            m_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in m_trades if float(t.get("net_pnl", 0.0)) < 0)
            m_pf = (m_pos / m_neg) if m_neg > 0 else (99.9 if m_pos > 0 else 0.0)

            best_m = max(m_trades, key=lambda t: float(t.get("net_pnl", 0.0)), default={})
            worst_m = min(m_trades, key=lambda t: float(t.get("net_pnl", 0.0)), default={})

            c = ReportColors.GREEN if m_net > 0 else (ReportColors.RED if m_net < 0 else ReportColors.WHITE)
            sign = "+" if m_net > 0 else ""

            lines.append(f"\n{'─' * 104}")
            lines.append(f"🔷 {ReportColors.BOLD}EXECUTION MODE {mode_idx}: {mode_name}{ReportColors.RESET}")
            lines.append(f"{'─' * 104}")
            lines.append(f"  • Net Realized P&L : {c}{ReportColors.BOLD}{sign}₹{m_net:,.2f}{ReportColors.RESET}")
            lines.append(f"  • Gross Realized   : ₹{m_gross:+,.2f} | Brokerage Paid: ₹{m_brkr:,.2f}")
            lines.append(f"  • Trades Executed  : {len(m_trades)} ({m_wins} Wins / {m_losses} Losses | Win Rate: {m_wr:.1f}%)")
            lines.append(f"  • Profit Factor    : {m_pf:.2f}")
            if best_m:
                lines.append(f"  • Best Trade       : {best_m.get('symbol')} ({best_m.get('track')}): +₹{float(best_m.get('net_pnl', 0.0)):,.2f}")
            if worst_m:
                lines.append(f"  • Worst Trade      : {worst_m.get('symbol')} ({worst_m.get('track')}): ₹{float(worst_m.get('net_pnl', 0.0)):,.2f}")

            # Mode Cumulative P&L Graph
            lines.append(f"\n  📈 {ReportColors.BOLD}Cumulative Net P&L Trajectory ({mode_name}):{ReportColors.RESET}")
            sorted_m_events = sorted(m_trades, key=lambda t: float(t.get("exit_time") or t.get("timestamp") or 0.0))
            cum_m_points: List[Tuple[float, float]] = []
            running_m_net = 0.0
            for t in sorted_m_events:
                ts = float(t.get("exit_time") or t.get("timestamp") or time.time())
                running_m_net += float(t.get("net_pnl", 0.0))
                cum_m_points.append((ts, running_m_net))

            graph_lines = render_ascii_line_graph(cum_m_points, width=76, height=10, title=f"NET P&L ({mode_name})")
            for gl in graph_lines:
                lines.append(f"  {gl}")

            # Sub-Strategy Track Performance Table for this mode
            by_track: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for t in m_trades:
                by_track[str(t.get("track", "ATM"))].append(t)

            lines.append(f"\n  {ReportColors.BOLD}Sub-Strategy Track Performance ({mode_name}):{ReportColors.RESET}")
            t_hdr = f"{'STRATEGY TRACK':<26} | {'TRADES (W/L)':^14} | {'WIN RATE':>10} | {'GROSS P&L':>14} | {'NET P&L':>16} | {'PF':>6}"
            lines.append(f"  {ReportColors.GRAY}{t_hdr}{ReportColors.RESET}")
            lines.append(f"  {'-' * len(t_hdr)}")

            sorted_tracks = sorted(
                by_track.items(),
                key=lambda x: sum(float(t.get("net_pnl", 0.0)) for t in x[1]),
                reverse=True,
            )

            for trk_name, t_trades in sorted_tracks:
                t_gross = sum(float(t.get("gross_pnl", 0.0)) for t in t_trades)
                t_net = sum(float(t.get("net_pnl", 0.0)) for t in t_trades)
                t_wins = sum(1 for t in t_trades if float(t.get("net_pnl", 0.0)) > 0)
                t_losses = len(t_trades) - t_wins
                t_wr = (t_wins / len(t_trades) * 100.0) if t_trades else 0.0
                t_pos = sum(float(t.get("net_pnl", 0.0)) for t in t_trades if float(t.get("net_pnl", 0.0)) > 0)
                t_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in t_trades if float(t.get("net_pnl", 0.0)) < 0)
                t_pf = (t_pos / t_neg) if t_neg > 0 else (99.9 if t_pos > 0 else 0.0)

                tc = ReportColors.GREEN if t_net > 0 else (ReportColors.RED if t_net < 0 else ReportColors.WHITE)
                t_sign = "+" if t_net > 0 else ""
                trk_color = get_track_group_color(trk_name)
                pad_trk = max(0, 26 - len(trk_name))
                disp_trk_name = f"{trk_color}{trk_name}{ReportColors.RESET}{' ' * pad_trk}"

                lines.append(
                    f"  {disp_trk_name} | "
                    f"{len(t_trades):>3} ({t_wins}W/{t_losses}L)     | "
                    f"{t_wr:>9.1f}% | "
                    f"₹{t_gross:>12,.2f} | "
                    f"{tc}{t_sign}₹{t_net:>14,.2f}{ReportColors.RESET} | "
                    f"{t_pf:>6.2f}"
                )
            lines.append(f"  {'-' * len(t_hdr)}")

        # =========================================================================
        # 3. EXECUTED TRADE JOURNAL (TRACK-BY-TRACK AUDIT)
        # =========================================================================
        lines.append(f"\n{'=' * 104}")
        lines.append(f"📖 {ReportColors.BOLD}INDIVIDUAL TRADE JOURNALS & AUDIT LEDGER{ReportColors.RESET}")
        lines.append(f"{'=' * 104}")

        journal_rendered_count = 0

        for mode_name, m_trades in sorted(by_mode.items()):
            mode_tracks: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for t in m_trades:
                mode_tracks[str(t.get("track", "ATM"))].append(t)

            matched_tracks = [trk for trk in sorted(mode_tracks.keys()) if matches_track_filter(mode_name, trk, track_map)]

            # Check if user specified explicit track patterns for this mode that had 0 trades
            expected_patterns = track_map.get(mode_name.lower()) or ([] if has_mode_prefix else track_map.get("*", []))
            missing_patterns = []
            for pat in expected_patterns:
                if pat.lower() in ("all", "*"):
                    continue
                if not any(pat.lower() in trk.lower() for trk in mode_tracks.keys()):
                    missing_patterns.append(pat)

            for trk in matched_tracks:
                trk_trades = sorted(
                    mode_tracks[trk],
                    key=lambda t: float(t.get("exit_time") or t.get("timestamp") or 0.0),
                )
                trk_net = sum(float(t.get("net_pnl", 0.0)) for t in trk_trades)
                trk_wins = sum(1 for t in trk_trades if float(t.get("net_pnl", 0.0)) > 0)
                trk_losses = len(trk_trades) - trk_wins
                trk_wr = (trk_wins / len(trk_trades) * 100.0) if trk_trades else 0.0
                trk_pos = sum(float(t.get("net_pnl", 0.0)) for t in trk_trades if float(t.get("net_pnl", 0.0)) > 0)
                trk_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in trk_trades if float(t.get("net_pnl", 0.0)) < 0)
                trk_pf = (trk_pos / trk_neg) if trk_neg > 0 else (99.9 if trk_pos > 0 else 0.0)

                p_col = ReportColors.GREEN if trk_net > 0 else (ReportColors.RED if trk_net < 0 else ReportColors.WHITE)
                p_sign = "+" if trk_net > 0 else ""

                lines.append(f"\n{ReportColors.BOLD}▶ [{mode_name}] TRACK: {trk} ({len(trk_trades)} Closed Trades){ReportColors.RESET}")
                lines.append(f"  Net P&L: {p_col}{p_sign}₹{trk_net:,.2f}{ReportColors.RESET} | Win Rate: {trk_wr:.1f}% ({trk_wins}W / {trk_losses}L) | PF: {trk_pf:.2f}")

                display_trades = trk_trades if max_trades == 0 else trk_trades[-max_trades:]
                if max_trades > 0 and len(trk_trades) > max_trades:
                    lines.append(f"  {ReportColors.GRAY}(Showing last {len(display_trades)} of {len(trk_trades)} trades. Pass --trades 0 for complete journal){ReportColors.RESET}")

                j_hdr = f"{'START TIME':<10} | {'START PRICE':>11} | {'EXIT TIME':<9} | {'EXIT PRICE':>10} | {'SYMBOL':<10} | {'CONTRACT':<24} | {'DIR':<4} | {'QTY':>5} | {'P&L':>14} | {'EXIT REASON'}"
                lines.append(f"  {ReportColors.GRAY}{j_hdr}{ReportColors.RESET}")
                lines.append(f"  {'-' * len(j_hdr)}")

                for t in display_trades:
                    e_str = format_hhmmss(t.get("entry_datetime") or t.get("entry_time")) or "--:--:--"
                    x_str = format_hhmmss(t.get("exit_datetime") or t.get("exit_time") or t.get("timestamp")) or "--:--:--"
                    ep = float(t.get("entry_price") or 0.0)
                    xp = float(t.get("exit_price") or 0.0)
                    sym = str(t.get("symbol", ""))[:10]
                    raw_c = str(t.get("contract_symbol", t.get("contract_key", "")))
                    s_up = sym.strip().upper()
                    if raw_c.upper().startswith(f"{s_up}_"):
                        cntr = raw_c[len(s_up) + 1:]
                    elif raw_c.upper().startswith(f"{s_up}-"):
                        cntr = raw_c[len(s_up) + 1:]
                    else:
                        cntr = raw_c
                    cntr = cntr[:24]
                    direction = str(t.get("direction", "BULL"))[:4]
                    qty = int(t.get("quantity") or 1)
                    net_p = float(t.get("net_pnl", 0.0))
                    reason = str(t.get("exit_reason", ""))[:18]

                    d_col = ReportColors.GREEN if direction == "BULL" else ReportColors.RED
                    pnl_c = ReportColors.GREEN if net_p > 0 else (ReportColors.RED if net_p < 0 else ReportColors.WHITE)
                    sign_p = "+" if net_p > 0 else ""

                    lines.append(
                        f"  {ReportColors.CYAN}{e_str:<10}{ReportColors.RESET} | "
                        f"₹{ep:>9.2f} | "
                        f"{ReportColors.CYAN}{x_str:<9}{ReportColors.RESET} | "
                        f"₹{xp:>8.2f} | "
                        f"{ReportColors.BOLD}{sym:<10}{ReportColors.RESET} | "
                        f"{ReportColors.GRAY}{cntr:<24}{ReportColors.RESET} | "
                        f"{d_col}{direction:<4}{ReportColors.RESET} | "
                        f"{qty:>5} | "
                        f"{pnl_c}{sign_p}₹{net_p:>12,.2f}{ReportColors.RESET} | "
                        f"{reason}"
                    )
                lines.append(f"  {'-' * len(j_hdr)}")
                journal_rendered_count += 1

            for mis_pat in missing_patterns:
                lines.append(f"\n  {ReportColors.GRAY}▶ [{mode_name}] TRACK PATTERN '{mis_pat}': No trades executed in this session window.{ReportColors.RESET}")

        if journal_rendered_count == 0:
            lines.append(f"\n  {ReportColors.GRAY}(No trade journals matched the specified track filters. Pass --tracks or --mode to broaden){ReportColors.RESET}")

        lines.append("\n" + "=" * 104)
        return "\n".join(lines)

