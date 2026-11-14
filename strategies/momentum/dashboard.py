#!/usr/bin/env python3
"""
strategies/momentum/dashboard.py
================================
148-Column Terminal ANSI Dashboard & Multi-Mode Performance Matrix:
1. Header Banner with Real-Time Nifty 50 Market Regime Bias.
2. 🚨 Fresh Breakout Alerts Table (< 8s Live Countdown & 1-Lot ATM Cost).
3. 🎯 Confirmed Breakouts & Breakdowns Table.
4. 📊 Top Monitored Active Assets (Live Stream Feed) with human-readable instrument names.
5. Active Positions View: Compact 1-line summary by default, full table with --show-positions.
6. Multi-Track Strategy Simulation Matrix featuring NET REALIZED P&L prominently.
7. Multi-Mode Execution Overview (Paper, Sandbox, Live) & Dedicated Mode Views.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from strategies.momentum.portfolio import MultiTrackPortfolio


# ---------------------------------------------------------------------------
# Terminal ANSI Colors
# ---------------------------------------------------------------------------

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BLACK = "\033[30m"
    MAGENTA = "\033[95m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_CYAN = "\033[46m"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# ---------------------------------------------------------------------------
# ANSI-Aware Text Padding & Formatting Engine
# ---------------------------------------------------------------------------

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_length(s: str) -> int:
    """Return visible character width ignoring ANSI escape codes."""
    return len(ANSI_ESCAPE_RE.sub("", str(s)))


def pad_visible(s: str, width: int, align: str = "right") -> str:
    """Pad or truncate string to exact visible character width, correctly handling ANSI codes."""
    s_str = str(s)
    vlen = visible_length(s_str)
    if vlen > width:
        out = []
        cur_v = 0
        in_ansi = False
        ansi_buf = []
        has_ansi = False
        for ch in s_str:
            if ch == "\x1b":
                in_ansi = True
                has_ansi = True
                ansi_buf.append(ch)
                continue
            if in_ansi:
                ansi_buf.append(ch)
                if ch == "m":
                    in_ansi = False
                    out.append("".join(ansi_buf))
                    ansi_buf = []
                continue
            if cur_v < width:
                out.append(ch)
                cur_v += 1
            else:
                break
        res = "".join(out)
        if has_ansi and not res.endswith("\x1b[0m"):
            res += "\x1b[0m"
        return res

    pad = width - vlen
    if align == "left":
        return s_str + (" " * pad)
    elif align == "center":
        left = pad // 2
        right = pad - left
        return (" " * left) + s_str + (" " * right)
    else:  # right
        return (" " * pad) + s_str


def _c_pnl(val: float, is_pct: bool = False, width: int = 0) -> str:
    """Format currency or percentage P&L with bold green/red coloring and exact padding."""
    sign = "+" if val > 0 else ""
    fmt = f"{sign}{val:.1f}%" if is_pct else f"{sign}₹{val:,.2f}"
    padded = pad_visible(fmt, width, "right") if width > 0 else fmt
    if val > 0:
        return f"{Colors.BOLD}{Colors.GREEN}{padded}{Colors.RESET}"
    elif val < 0:
        return f"{Colors.BOLD}{Colors.RED}{padded}{Colors.RESET}"
    else:
        return f"{Colors.WHITE}{padded}{Colors.RESET}"


# ---------------------------------------------------------------------------
# Legacy Visual Badges & Sparklines Engine
# ---------------------------------------------------------------------------

_SPARK_BLOCKS = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def make_sparkline(values: List[float], width: int = 8) -> Tuple[str, str]:
    """Generate an 8-character Unicode sparkline block chart from rolling values."""
    if not values:
        return f"{Colors.GRAY}{'─' * width}{Colors.RESET}", f"{Colors.GRAY}→{Colors.RESET}"

    vals = values[-width:]
    if len(vals) < width:
        vals = [vals[0]] * (width - len(vals)) + vals

    min_v = min(vals)
    max_v = max(vals)
    v_range = max_v - min_v

    is_up = vals[-1] >= vals[0]
    arrow = f"{Colors.GREEN}↗{Colors.RESET}" if vals[-1] > vals[0] else (f"{Colors.RED}↘{Colors.RESET}" if vals[-1] < vals[0] else f"{Colors.CYAN}→{Colors.RESET}")
    color = Colors.GREEN if is_up else (Colors.RED if vals[-1] < vals[0] else Colors.WHITE)

    if v_range == 0:
        bar = "▄" * width
        return f"{color}{bar}{Colors.RESET}", arrow

    chars = []
    for v in vals:
        ratio = (v - min_v) / v_range
        idx = int(ratio * 7)
        idx = max(0, min(7, idx))
        chars.append(_SPARK_BLOCKS[idx])

    return f"{color}{''.join(chars)}{Colors.RESET}", arrow


def make_range_bar(low: float, high: float, ltp: float, width: int = 7) -> str:
    """
    Generate an intraday position bar showing where LTP is within day's [Low ──●── High].
    Colors green if near HOD (>=85%), red if near LOD (<=15%), cyan in middle.
    """
    if high <= low:
        return f"{Colors.GRAY}[───●───]{Colors.RESET}"
    ratio = max(0.0, min(1.0, (ltp - low) / (high - low)))
    pos = int(ratio * (width - 1))
    chars = ["─"] * width
    chars[pos] = "●"
    bar_str = "".join(chars)
    if ratio >= 0.85:
        return f"{Colors.GREEN}[{bar_str}]{Colors.RESET}"
    elif ratio <= 0.15:
        return f"{Colors.RED}[{bar_str}]{Colors.RESET}"
    else:
        return f"{Colors.CYAN}[{bar_str}]{Colors.RESET}"


def make_score_badge(score: float, width: int = 9) -> str:
    """Format setup score with legacy color thresholds."""
    raw = f"{score:>4.0f}/100" if score <= 100 else f"{score:>6.1f}"
    padded = pad_visible(raw, width, "right")
    if score >= 80:
        return f"{Colors.BOLD}{Colors.GREEN}{padded}{Colors.RESET}"
    elif score >= 67:
        return f"{Colors.BOLD}{Colors.YELLOW}{padded}{Colors.RESET}"
    elif score >= 50:
        return f"{Colors.WHITE}{padded}{Colors.RESET}"
    else:
        return f"{Colors.GRAY}{padded}{Colors.RESET}"


def make_setup_badge(stage: str, direction: str = "NEUTRAL", width: int = 18) -> str:
    """Format setup stage badge with legacy colors matching stream_rvol_scanner.py."""
    stg = stage.strip()
    if stg in ("🚀 BREAKOUT", "🚀 BULL BREAKOUT"):
        badge = f"{Colors.BG_GREEN}{Colors.BLACK} 🚀 BREAKOUT  {Colors.RESET}"
    elif stg in ("🩸 BREAKDOWN", "🩸 BEAR BREAKDOWN"):
        badge = f"{Colors.BG_RED}{Colors.WHITE} 🩸 BREAKDOWN {Colors.RESET}"
    elif stg == "💥 BULL BREAKOUT":
        badge = f"{Colors.BG_GREEN}{Colors.BLACK} 💥 BULL BREAK{Colors.RESET}"
    elif stg == "💥 BEAR BREAKDOWN":
        badge = f"{Colors.BG_RED}{Colors.WHITE} 💥 BEAR BREAK{Colors.RESET}"
    elif stg == "⚡ FORMING":
        badge = f"{Colors.BOLD}{Colors.YELLOW} ⚡ FORMING   {Colors.RESET}"
    elif stg == "⚡ CRACKING":
        badge = f"{Colors.BOLD}{Colors.MAGENTA} ⚡ CRACKING  {Colors.RESET}"
    elif stg in ("🌀 BULL FLAG", "🌀 COILING"):
        badge = f"{Colors.BOLD}{Colors.CYAN} 🌀 BULL FLAG {Colors.RESET}"
    elif stg == "🚩 BEAR FLAG":
        badge = f"{Colors.BOLD}{Colors.RED} 🚩 BEAR FLAG {Colors.RESET}"
    elif stg in ("⚠️ DRY HIGH", "⚠️ DRY LOW", "⚠️ DRY EXTREM"):
        badge = f"{Colors.YELLOW} ⚠️ DRY EXTREM{Colors.RESET}"
    elif "WATCH" in stg:
        badge = f"{Colors.MAGENTA} 👀 WATCH (S) {Colors.RESET}" if direction == "BEAR" else f"{Colors.CYAN} 👀 WATCH (L) {Colors.RESET}"
    else:
        badge = f"{Colors.GRAY}  ── NORMAL   {Colors.RESET}"

    return pad_visible(badge, width, "left")


def make_activity_badge(state: str, width: int = 14) -> str:
    """Format intraday activity/stream state with legacy colors."""
    st = state.strip()
    if st == "⚡ STREAM":
        badge = f"{Colors.BOLD}{Colors.GREEN}⚡ STREAM     {Colors.RESET}"
    elif st == "🔥 SURGE 5m":
        badge = f"{Colors.BOLD}{Colors.RED}🔥 SURGE 5m   {Colors.RESET}"
    elif st == "⚡ ACTIVE":
        badge = f"{Colors.BOLD}{Colors.YELLOW}⚡ ACTIVE     {Colors.RESET}"
    elif st in ("🌀 BULL FLAG", "🌀 COILING"):
        badge = f"{Colors.BOLD}{Colors.CYAN}🌀 BULL FLAG  {Colors.RESET}"
    elif st == "🚩 BEAR FLAG":
        badge = f"{Colors.BOLD}{Colors.RED}🚩 BEAR FLAG  {Colors.RESET}"
    elif st in ("⚠️ DRY HIGH", "⚠️ DRY LOW"):
        badge = f"{Colors.YELLOW}⚠️ DRY EXTREM {Colors.RESET}"
    elif st == "💤 INACTIVE":
        badge = f"{Colors.GRAY}💤 INACTIVE   {Colors.RESET}"
    elif st in ("💤 IDLE", "🧊 DORMANT"):
        badge = f"{Colors.GRAY}💤 IDLE       {Colors.RESET}"
    else:
        badge = f"{Colors.WHITE}── NORMAL     {Colors.RESET}"

    return pad_visible(badge, width, "left")


# ---------------------------------------------------------------------------
# 148-Column Table Renderers
# ---------------------------------------------------------------------------

def render_performance_matrix(
    portfolio: MultiTrackPortfolio,
    quotes_cache: Dict[str, Any],
    mode_name: Optional[str] = None,
) -> str:
    """
    Render 148-character ANSI Strategy Simulation Matrix across all simulation tracks.
    Prominently displays NET REALIZED P&L with bold green/red coloring.
    """
    lines = []
    lines.append("=" * 148)
    frz_tag = f" {Colors.CYAN}{Colors.BOLD}[❄️ FROZEN]{Colors.RESET}" if hasattr(portfolio, "is_frozen") and portfolio.is_frozen() else ""
    mode_tag = (f" [{mode_name.upper()}]" if mode_name else "") + frz_tag
    lines.append(f" {Colors.BOLD}STRATEGY SIMULATION MATRIX{mode_tag} ({len(portfolio.tracks)} Tracks | Starting ₹{portfolio.starting_capital:,.0f} each){Colors.RESET}")
    lines.append("=" * 148)

    hdr = (
        f"{'STRATEGY TRACK':<26} | "
        f"{'CAPITAL':>10} | "
        f"{'AVAIL CASH':>10} | "
        f"{'OPEN MTM':>11} | "
        f"{'GROSS REAL':>10} | "
        f"{'CHARGES':>8} | "
        f"{'NET REALIZED P&L':>18} | "
        f"{'WIN RATE':>8} | "
        f"{'TRADES (W/L)':>12} | "
        f"{'ROI %':>8}"
    )
    lines.append(f"{Colors.BOLD}{hdr}{Colors.RESET}")
    lines.append("-" * 148)

    for name, acct in portfolio.tracks.items():
        mtm = acct.compute_mtm(quotes_cache)
        gross_pnl = acct.gross_realized
        charges = acct.brokerage_paid
        net_realized = acct.net_realized
        roi = acct.compute_roi(quotes_cache)

        clean_name = name.replace("_FIXED_CAP", " [FX]")
        is_dt = name == "DYNAMIC_TRIO"
        is_champ = name in getattr(portfolio, "dynamic_trio_champions", [])
        if is_dt:
            track_disp = f"{Colors.MAGENTA}★ {clean_name}{Colors.RESET}"
        elif is_champ:
            track_disp = f"{Colors.YELLOW}◆ {clean_name}{Colors.RESET}"
        else:
            track_disp = f"  {clean_name}"

        track_str = pad_visible(track_disp, 26, "left")
        cap_str = pad_visible(f"₹{acct.starting_capital:>8,.0f}", 10, "right")
        cash_str = pad_visible(f"₹{acct.cash:>8,.0f}", 10, "right")
        mtm_str = _c_pnl(mtm, width=11)
        gross_str = _c_pnl(gross_pnl, width=10)
        brkr_str = pad_visible(f"₹{charges:>6,.0f}", 8, "right")
        net_real_str = _c_pnl(net_realized, width=18)
        wr_str = pad_visible(f"{acct.win_rate:>6.1f}%", 8, "right")
        trades_str = pad_visible(f"{acct.total_trades} ({acct.wins}W/{acct.losses}L)", 12, "right")
        roi_str = _c_pnl(roi, is_pct=True, width=8)

        line = (
            f"{track_str} | "
            f"{cap_str} | "
            f"{cash_str} | "
            f"{mtm_str} | "
            f"{gross_str} | "
            f"{brkr_str} | "
            f"{net_real_str} | "
            f"{wr_str} | "
            f"{trades_str} | "
            f"{roi_str}"
        )
        lines.append(line)

    lines.append("=" * 148)
    return "\n".join(lines)


def render_focused_strategy_table(
    portfolio: MultiTrackPortfolio,
    quotes_cache: Dict[str, Any],
    filter_tag: str,
    mode_name: Optional[str] = None,
) -> str:
    """
    Render focused comparison table and active positions for a specific strategy mode or strike group.
    """
    ft = filter_tag.upper()
    matching_tracks = {}
    for name, acct in portfolio.tracks.items():
        if ft == "DYNAMIC_TRIO" and (name == "DYNAMIC_TRIO" or name in portfolio.dynamic_trio_champions):
            matching_tracks[name] = acct
        elif ft in name:
            matching_tracks[name] = acct

    if not matching_tracks:
        matching_tracks = portfolio.tracks

    lines = []
    lines.append("=" * 148)
    frz_tag = f" {Colors.CYAN}{Colors.BOLD}[❄️ FROZEN]{Colors.RESET}" if hasattr(portfolio, "is_frozen") and portfolio.is_frozen() else ""
    mode_tag = (f" [{mode_name.upper()}]" if mode_name else "") + frz_tag
    lines.append(f" {Colors.CYAN}{Colors.BOLD}FOCUSED STRATEGY VIEW: {ft}{mode_tag} ({len(matching_tracks)} Tracks Monitored){Colors.RESET}")
    lines.append("=" * 148)

    hdr = (
        f"{'STRATEGY TRACK':<26} | "
        f"{'CAPITAL':>10} | "
        f"{'AVAIL CASH':>10} | "
        f"{'OPEN MTM':>11} | "
        f"{'GROSS REAL':>10} | "
        f"{'CHARGES':>8} | "
        f"{'NET REALIZED P&L':>18} | "
        f"{'WIN RATE':>8} | "
        f"{'TRADES (W/L)':>12} | "
        f"{'ROI %':>8}"
    )
    lines.append(f"{Colors.BOLD}{hdr}{Colors.RESET}")
    lines.append("-" * 148)

    for name, acct in matching_tracks.items():
        mtm = acct.compute_mtm(quotes_cache)
        gross_pnl = acct.gross_realized
        charges = acct.brokerage_paid
        net_realized = acct.net_realized
        roi = acct.compute_roi(quotes_cache)

        clean_name = name.replace("_FIXED_CAP", " [FX]")
        is_dt = name == "DYNAMIC_TRIO"
        is_champ = name in getattr(portfolio, "dynamic_trio_champions", [])
        if is_dt:
            track_disp = f"{Colors.MAGENTA}★ {clean_name}{Colors.RESET}"
        elif is_champ:
            track_disp = f"{Colors.YELLOW}◆ {clean_name}{Colors.RESET}"
        else:
            track_disp = f"  {clean_name}"

        track_str = pad_visible(track_disp, 26, "left")
        cap_str = pad_visible(f"₹{acct.starting_capital:>8,.0f}", 10, "right")
        cash_str = pad_visible(f"₹{acct.cash:>8,.0f}", 10, "right")
        mtm_str = _c_pnl(mtm, width=11)
        gross_str = _c_pnl(gross_pnl, width=10)
        brkr_str = pad_visible(f"₹{charges:>6,.0f}", 8, "right")
        net_real_str = _c_pnl(net_realized, width=18)
        wr_str = pad_visible(f"{acct.win_rate:>6.1f}%", 8, "right")
        trades_str = pad_visible(f"{acct.total_trades} ({acct.wins}W/{acct.losses}L)", 12, "right")
        roi_str = _c_pnl(roi, is_pct=True, width=8)

        line = (
            f"{track_str} | "
            f"{cap_str} | "
            f"{cash_str} | "
            f"{mtm_str} | "
            f"{gross_str} | "
            f"{brkr_str} | "
            f"{net_real_str} | "
            f"{wr_str} | "
            f"{trades_str} | "
            f"{roi_str}"
        )
        lines.append(line)

    lines.append("-" * 148)

    # Active positions for this focused group
    open_count = sum(len(acct.open_positions) for acct in matching_tracks.values())
    lines.append(f" {Colors.BOLD}ACTIVE POSITIONS FOR {ft} ({open_count} Open):{Colors.RESET}")
    if open_count == 0:
        lines.append(f"  {Colors.GRAY}No open positions currently in this mode group.{Colors.RESET}")
    else:
        # 148-character positions table:
        # 26 + 3 + 20 + 3 + 18 + 3 + 6 + 3 + 5 + 3 + 10 + 3 + 11 + 3 + 18 + 3 + 10 = 124 + 24 = 148
        pos_hdr = f"{'TRADE ID':<26} | {'STRATEGY':<20} | {'INSTRUMENT':<18} | {'DIR':^6} | {'QTY':>5} | {'ENTRY':>10} | {'LTP (BID)':>11} | {'OPEN MTM':>18} | {'SL':>10}"
        lines.append(f"{Colors.BOLD}{pos_hdr}{Colors.RESET}")
        lines.append("-" * 148)
        for acct in matching_tracks.values():
            for p in acct.open_positions.values():
                q = quotes_cache.get(p.contract_key, {})
                bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or p.entry_price)
                pos_mtm = (bid - p.entry_price) * p.quantity
                dir_col = Colors.GREEN if p.direction == "BULL" else Colors.RED
                lines.append(
                    f"{pad_visible(p.trade_id, 26, 'left')} | "
                    f"{pad_visible(p.strategy_track.replace('_FIXED_CAP', ' [FX]'), 20, 'left')} | "
                    f"{pad_visible(p.symbol, 18, 'left')} | "
                    f"{dir_col}{pad_visible(p.direction, 6, 'center')}{Colors.RESET} | "
                    f"{pad_visible(str(p.quantity), 5, 'right')} | "
                    f"{pad_visible(f'₹{p.entry_price:,.2f}', 10, 'right')} | "
                    f"{pad_visible(f'₹{bid:,.2f}', 11, 'right')} | "
                    f"{_c_pnl(pos_mtm, width=18)} | "
                    f"{pad_visible(f'₹{p.current_sl:,.2f}', 10, 'right')}"
                )
    lines.append("=" * 148)
    return "\n".join(lines)


def render_positions_summary(
    portfolio: MultiTrackPortfolio,
    quotes_cache: Dict[str, Any],
    show_positions: bool = False,
    mode_name: Optional[str] = None,
) -> str:
    """
    Render active positions: compact 1-line by default, full table if show_positions=True.
    """
    total_open = sum(len(acct.open_positions) for acct in portfolio.tracks.values())
    total_mtm = sum(acct.compute_mtm(quotes_cache) for acct in portfolio.tracks.values())

    mtm_col = Colors.GREEN if total_mtm > 0 else (Colors.RED if total_mtm < 0 else Colors.GRAY)
    mode_tag = f" [{mode_name.upper()}]" if mode_name else ""

    if not show_positions:
        open_syms = set()
        for acct in portfolio.tracks.values():
            for p in acct.open_positions.values():
                open_syms.add(p.symbol)
        sym_str = ", ".join(sorted(list(open_syms))) if open_syms else "None"
        return (
            f" {Colors.BOLD}ACTIVE POSITIONS{mode_tag}:{Colors.RESET} {total_open} Open across {len(portfolio.tracks)} tracks | "
            f"Total MTM: {mtm_col}₹{total_mtm:+,.2f}{Colors.RESET} | "
            f"Active: [{sym_str}] {Colors.GRAY}(Use --show-positions to expand table){Colors.RESET}"
        )

    lines = []
    lines.append("-" * 148)
    lines.append(f" {Colors.BOLD}EXPANDED ACTIVE POSITIONS{mode_tag} ({total_open} Open | Total MTM: {mtm_col}₹{total_mtm:+,.2f}{Colors.RESET}):{Colors.RESET}")
    # 148 characters: 20 + 19 + 16 + 6 + 5 + 10 + 11 + 14 + 10 + 10 + 27 = 148
    pos_hdr = f"{'TRADE ID':<20} | {'STRATEGY':<19} | {'INSTRUMENT':<16} | {'DIR':^6} | {'QTY':>5} | {'ENTRY':>10} | {'LTP (BID)':>11} | {'OPEN MTM':>14} | {'SL':>10} | {'HOLD TIME':>10}"
    lines.append(f"{Colors.BOLD}{pos_hdr}{Colors.RESET}")
    lines.append("-" * 148)

    for acct in portfolio.tracks.values():
        for p in acct.open_positions.values():
            q = quotes_cache.get(p.contract_key, {})
            bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or p.entry_price)
            pos_mtm = (bid - p.entry_price) * p.quantity
            hold_sec = max(0.0, time.time() - p.entry_time)
            hold_str = f"{int(hold_sec // 60)}m {int(hold_sec % 60):02d}s"
            dir_col = Colors.GREEN if p.direction == "BULL" else Colors.RED

            inst_disp = q.get("symbol") or p.contract_symbol or p.symbol
            lines.append(
                f"{pad_visible(p.trade_id, 20, 'left')} | "
                f"{pad_visible(p.strategy_track.replace('_FIXED_CAP', ' [FX]'), 19, 'left')} | "
                f"{pad_visible(inst_disp, 16, 'left')} | "
                f"{dir_col}{pad_visible(p.direction, 6, 'center')}{Colors.RESET} | "
                f"{pad_visible(str(p.quantity), 5, 'right')} | "
                f"{pad_visible(f'₹{p.entry_price:,.2f}', 10, 'right')} | "
                f"{pad_visible(f'₹{bid:,.2f}', 11, 'right')} | "
                f"{_c_pnl(pos_mtm, width=14)} | "
                f"{pad_visible(f'₹{p.current_sl:,.2f}', 10, 'right')} | "
                f"{pad_visible(hold_str, 10, 'right')}"
            )

    lines.append("-" * 148)
    return "\n".join(lines)


def render_multi_mode_summary(multi_executor: Any, quotes_cache: Optional[Dict[str, Any]] = None) -> str:
    """
    Render comparative overview table across all active execution modes (Paper, Sandbox, Live).
    """
    if not multi_executor or not getattr(multi_executor, "adapters", None):
        return ""

    lines = []
    lines.append("=" * 148)
    modes_str = ", ".join([m.upper() for m in multi_executor.active_modes])
    lines.append(f" {Colors.CYAN}{Colors.BOLD}MULTI-MODE EXECUTION OVERVIEW (Active Modes: {modes_str}){Colors.RESET}")
    lines.append("=" * 148)

    # 10 columns totaling 148 chars:
    # 14 + 22 + 10 + 10 + 9 + 16 + 8 + 10 + 8 + 14 = 121 + 27 = 148
    hdr = f"{'EXECUTION MODE':<14} | {'STATUS / ENGINE':<22} | {'CAPITAL':>10} | {'AVAIL CASH':>10} | {'POSITIONS':>9} | {'NET REALIZED P&L':>16} | {'WIN RATE':>8} | {'TRADES':>10} | {'ROI %':>8} | {'JOURNAL FILE':<14}"
    lines.append(f"{Colors.BOLD}{hdr}{Colors.RESET}")
    lines.append("-" * 148)

    summaries = multi_executor.get_portfolio_summaries(quotes_cache)
    for mode_name, adapter in multi_executor.adapters.items():
        s = summaries.get(mode_name, {})
        cap = float(s.get("initial_capital", 100_000.0))
        cash = float(s.get("available_capital", 100_000.0))
        open_pos = int(s.get("open_positions", len(adapter.get_positions())))
        net_pnl = float(s.get("net_realized", 0.0))
        win_rate = float(s.get("win_rate", 0.0))
        wins = int(s.get("wins", 0))
        losses = int(s.get("losses", 0))
        tot_trades = wins + losses
        roi = float(s.get("net_roi", 0.0))
        out_csv = getattr(adapter, "output_csv", f"logs/{mode_name}_trades.csv")

        if s.get("frozen", False):
            status_desc = f"{Colors.YELLOW}❄️ FROZEN (Close Only){Colors.RESET}"
        elif mode_name == "paper":
            status_desc = f"{Colors.GREEN}ACTIVE (Local Paper){Colors.RESET}"
        elif mode_name in ("paper_delayed", "paper-delayed"):
            status_desc = f"{Colors.MAGENTA}DELAYED (700ms-2.5s){Colors.RESET}"
        elif mode_name == "live":
            is_conf = getattr(adapter, "live_confirmed", False)
            status_desc = f"{Colors.RED}LIVE TRADING (v3){Colors.RESET}" if is_conf else f"{Colors.YELLOW}STANDBY (Locked){Colors.RESET}"
        else:
            status_desc = f"{Colors.WHITE}ACTIVE{Colors.RESET}"

        line = (
            f"{Colors.BOLD}{pad_visible(mode_name.upper()[:14], 14, 'left')}{Colors.RESET} | "
            f"{pad_visible(status_desc, 22, 'left')} | "
            f"{pad_visible(f'₹{cap:>8,.0f}', 10, 'right')} | "
            f"{pad_visible(f'₹{cash:>8,.0f}', 10, 'right')} | "
            f"{pad_visible(f'{open_pos} Open', 9, 'right')} | "
            f"{_c_pnl(net_pnl, width=16)} | "
            f"{pad_visible(f'{win_rate:>6.1f}%', 8, 'right')} | "
            f"{pad_visible(f'{tot_trades} ({wins}W/{losses}L)', 10, 'right')} | "
            f"{_c_pnl(roi, is_pct=True, width=8)} | "
            f"{pad_visible(os.path.basename(out_csv)[:14], 14, 'left')}"
        )
        lines.append(line)

    lines.append("=" * 148)
    return "\n".join(lines)


def render_focused_mode_view(mode_name: str, adapter: Any, quotes_cache: Dict[str, Any]) -> str:
    """
    Render detailed table of open positions and order audit logs for a single execution mode.
    """
    lines = []
    lines.append("=" * 148)
    lines.append(f" {Colors.CYAN}{Colors.BOLD}FOCUSED EXECUTION VIEW: {mode_name.upper()}{Colors.RESET}")
    lines.append("=" * 148)

    if not adapter:
        lines.append(f"  {Colors.RED}Execution mode '{mode_name.upper()}' is not currently active.{Colors.RESET}")
        lines.append("=" * 148)
        return "\n".join(lines)

    summary = adapter.get_portfolio_summary() if hasattr(adapter, "get_portfolio_summary") else {}
    cap = float(summary.get("initial_capital", 100_000.0))
    cash = float(summary.get("available_capital", 100_000.0))
    net_pnl = float(summary.get("net_realized", 0.0))
    pnl_col = Colors.GREEN if net_pnl > 0 else (Colors.RED if net_pnl < 0 else Colors.WHITE)

    lines.append(
        f" Capital: ₹{cap:,.0f} | Cash: ₹{cash:,.0f} | Net Realized: {pnl_col}₹{net_pnl:+,.2f}{Colors.RESET} | "
        f"Win Rate: {summary.get('win_rate', 0.0):.1f}% | Total Closed Trades: {summary.get('closed_trades', 0)} | "
        f"Journal: {getattr(adapter, 'output_csv', 'N/A')}"
    )
    lines.append("-" * 148)

    positions = adapter.get_positions()
    lines.append(f" {Colors.BOLD}ACTIVE POSITIONS IN {mode_name.upper()} ({len(positions)} Open):{Colors.RESET}")
    if not positions:
        lines.append(f"  {Colors.GRAY}No open positions currently active in {mode_name.upper()} mode.{Colors.RESET}")
    else:
        hdr = f"{'ORDER ID':<26} | {'INSTRUMENT':<20} | {'CONTRACT':<22} | {'QTY':>6} | {'ENTRY':>10} | {'LTP (BID)':>11} | {'OPEN MTM':>16} | {'STATUS':<10}"
        lines.append(f"{Colors.BOLD}{hdr}{Colors.RESET}")
        lines.append("-" * 148)
        for pos in positions:
            sym = pos.get("symbol", "")
            tok = pos.get("instrument_token", "")
            q = quotes_cache.get(tok, {})
            bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or pos.get("entry_price", 0.0))
            ep = float(pos.get("entry_price", 0.0))
            qty = int(pos.get("quantity", 1))
            mtm = (bid - ep) * qty
            ord_id = pos.get("order_id", "N/A")

            lines.append(
                f"{pad_visible(ord_id[:26], 26, 'left')} | "
                f"{pad_visible(sym[:20], 20, 'left')} | "
                f"{pad_visible(tok[:22], 22, 'left')} | "
                f"{pad_visible(str(qty), 6, 'right')} | "
                f"{pad_visible(f'₹{ep:,.2f}', 10, 'right')} | "
                f"{pad_visible(f'₹{bid:,.2f}', 11, 'right')} | "
                f"{_c_pnl(mtm, width=16)} | "
                f"{pad_visible('OPEN', 10, 'center')}"
            )

    lines.append("=" * 148)
    return "\n".join(lines)


def render_recently_executed_orders(
    portfolio: MultiTrackPortfolio,
    quotes_cache: Dict[str, Any],
    max_orders: int = 5,
) -> str:
    """
    Render compact 'Recently Executed Orders' table (last 3-5 fills and exits).
    Enables instant visibility of execution activity without expanding full position logs.
    Total width: 148 characters.
    """
    lines = []
    lines.append("-" * 148)

    recent_trades = portfolio.get_recent_trades(limit=max_orders) if hasattr(portfolio, "get_recent_trades") else []

    tot_trades = sum(len(acct.closed_trades) + len(acct.open_positions) for acct in portfolio.tracks.values())
    open_cnt = sum(len(acct.open_positions) for acct in portfolio.tracks.values())
    closed_cnt = sum(len(acct.closed_trades) for acct in portfolio.tracks.values())

    lines.append(
        f" {Colors.BOLD}{Colors.WHITE}RECENTLY EXECUTED ORDERS ({tot_trades} Total Executed | "
        f"{Colors.CYAN}{open_cnt} Active Open{Colors.RESET}{Colors.BOLD}{Colors.WHITE} | "
        f"{Colors.GREEN}{closed_cnt} Closed{Colors.RESET}{Colors.BOLD}{Colors.WHITE}):{Colors.RESET}"
    )

    if not recent_trades:
        lines.append(f"  {Colors.GRAY}Session Executed Trades: None yet (Awaiting breakout triggers with valid option quotes)...{Colors.RESET}")
        lines.append("-" * 148)
        return "\n".join(lines)

    # 12 columns totaling 148 chars (115 text + 33 separators = 148):
    # 10 + 8 + 12 + 4 + 16 + 5 + 8 + 9 + 9 + 14 + 6 + 14 = 115
    hdr = (
        f"{'TRACK':<10} | "
        f"{'TIME':^8} | "
        f"{'SYMBOL':<12} | "
        f"{'DIR':^4} | "
        f"{'CONTRACT':<16} | "
        f"{'QTY':>5} | "
        f"{'ENTRY':>8} | "
        f"{'EXIT/LTP':>9} | "
        f"{'GROSS':>9} | "
        f"{'NET P&L (₹/%)':>14} | "
        f"{'STATUS':^6} | "
        f"{'OUTCOME / REASON':<14}"
    )
    lines.append(f"{Colors.BOLD}{hdr}{Colors.RESET}")
    lines.append("-" * 148)

    for t in recent_trades:
        is_closed = t.is_closed
        ts = t.exit_time if is_closed else t.entry_time
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts > 0 else "--:--:--"

        opt_q = quotes_cache.get(t.contract_key, {})
        live_p = float(opt_q.get("bid") or opt_q.get("last_price") or opt_q.get("ltp") or t.entry_price)
        cur_p = t.exit_price if is_closed else live_p

        gross = t.gross_pnl if is_closed else ((cur_p - t.entry_price) * t.quantity)
        net = t.net_pnl if is_closed else (gross - 60.0)
        ret_pct = t.return_pct if is_closed else (((cur_p - t.entry_price) / t.entry_price * 100.0) if t.entry_price > 0 else 0.0)

        dir_col = Colors.GREEN if t.direction == "BULL" else Colors.RED
        status_col = (Colors.GREEN if net >= 0 else Colors.RED) if is_closed else Colors.CYAN
        status_txt = "CLOSED" if is_closed else "OPEN"

        gross_sign = "+" if gross > 0 else ""
        gross_str = f"{gross_sign}₹{gross:,.0f}"

        sign = "+" if net > 0 else ""
        pnl_col = Colors.GREEN if net > 0 else (Colors.RED if net < 0 else Colors.WHITE)
        net_str = f"{pnl_col}{sign}₹{net:,.0f} ({sign}{ret_pct:.1f}%){Colors.RESET}"

        reason = t.exit_reason if is_closed else f"SL:₹{t.current_sl:.1f}"

        lines.append(
            f"{Colors.BOLD}{pad_visible(t.strategy_track[:10], 10, 'left')}{Colors.RESET} | "
            f"{pad_visible(time_str, 8, 'center')} | "
            f"{pad_visible(t.symbol[:12], 12, 'left')} | "
            f"{dir_col}{pad_visible(t.direction[:4], 4, 'center')}{Colors.RESET} | "
            f"{pad_visible(t.contract_symbol[:16], 16, 'left')} | "
            f"{pad_visible(str(t.quantity), 5, 'right')} | "
            f"{pad_visible(f'₹{t.entry_price:.2f}', 8, 'right')} | "
            f"{pad_visible(f'₹{cur_p:.2f}', 9, 'right')} | "
            f"{pad_visible(gross_str, 9, 'right')} | "
            f"{pad_visible(net_str, 14, 'right')} | "
            f"{status_col}{pad_visible(status_txt, 6, 'center')}{Colors.RESET} | "
            f"{pad_visible(reason[:14], 14, 'left')}"
        )

    lines.append("-" * 148)
    return "\n".join(lines)


def render_scanner_dashboard(
    monitored_count: int,
    nifty_stats: Dict[str, Any],
    fresh_alerts: List[Dict[str, Any]],
    confirmed_setups: List[Dict[str, Any]],
    portfolio: MultiTrackPortfolio,
    quotes_cache: Dict[str, Any],
    mode: str = "PAPER",
    min_score: int = 67,
    show_positions: bool = False,
    view_mode: str = "all",
    multi_executor: Optional[Any] = None,
    monitored_assets: Optional[List[Dict[str, Any]]] = None,
    portfolios_by_mode: Optional[Dict[str, MultiTrackPortfolio]] = None,
) -> str:
    """
    Render 148-column unified scanner and strategy dashboard with ANSI border alignment.
    Displays human-readable instrument names (e.g. CRUDE OIL, NATURALGAS, COPPER 1380 CE).
    """
    lines = []
    lines.append("=" * 148)
    regime = nifty_stats.get("regime", "NEUTRAL")
    nifty_chg = nifty_stats.get("pct_change", 0.0)
    reg_col = Colors.GREEN if regime == "BULL" else (Colors.RED if regime == "BEAR" else Colors.YELLOW)

    vm_tag = view_mode.upper()
    sess_type, _ = portfolio.risk_manager.get_market_session()
    if sess_type == "PRE_MARKET_CAS":
        sess_tag = f"{Colors.BG_YELLOW}{Colors.BLACK}{Colors.BOLD} 🔔 CAS AUCTION {Colors.RESET}"
    elif sess_type == "NORMAL_TRADING":
        sess_tag = f"{Colors.GREEN}{Colors.BOLD}⚡ ACTIVE{Colors.RESET}"
    elif sess_type == "ENTRY_FREEZE":
        sess_tag = f"{Colors.CYAN}{Colors.BOLD}❄️ FROZEN{Colors.RESET}"
    elif sess_type == "EOD_SQUAREOFF":
        sess_tag = f"{Colors.BG_RED}{Colors.WHITE}{Colors.BOLD} 🚪 RMS CLOSE {Colors.RESET}"
    elif sess_type in ("POST_MARKET", "OFF_HOURS", "WEEKEND"):
        sess_tag = f"{Colors.GRAY}💤 CLOSED{Colors.RESET}"
    else:
        sess_tag = f"{Colors.WHITE}{sess_type}{Colors.RESET}"

    is_mode_frozen = (multi_executor and multi_executor.is_frozen(mode)) or os.path.exists("logs/.freeze_all") or os.path.exists(f"logs/.freeze_{mode.lower()}")
    mode_display = f"{Colors.BOLD}{mode.upper()}{Colors.RESET}" + (f" {Colors.CYAN}{Colors.BOLD}[❄️ FROZEN]{Colors.RESET}" if is_mode_frozen else "")

    header = (
        f" UPSTOX EVENT-DRIVEN MOMENTUM STRATEGY & SCANNER | "
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
        f"Session: {sess_tag} | "
        f"Mode: {mode_display} | "
        f"View: {Colors.CYAN}{vm_tag}{Colors.RESET} | "
        f"Nifty 50: {reg_col}{regime} ({nifty_chg:+.2f}%){Colors.RESET} | "
        f"Min Score: {min_score} | Monitored: {monitored_count:,}"
    )
    lines.append(header)
    lines.append("=" * 148)

    vm = view_mode.lower()

    # Specialized View: Focused Strategy Table
    if vm in ("atm", "itm", "otm", "low_prem", "trail_sl", "fixed_tgt", "combo", "dynamic_trio"):
        if portfolios_by_mode and len(portfolios_by_mode) > 1:
            for m_name, port in portfolios_by_mode.items():
                lines.append(render_focused_strategy_table(port, quotes_cache, filter_tag=vm.upper(), mode_name=m_name))
        else:
            lines.append(render_focused_strategy_table(portfolio, quotes_cache, filter_tag=vm.upper(), mode_name=mode))
        return "\n".join(lines)

    # Specialized View: Expanded Positions Only
    if vm == "positions":
        if portfolios_by_mode and len(portfolios_by_mode) > 1:
            for m_name, port in portfolios_by_mode.items():
                lines.append(render_positions_summary(port, quotes_cache, show_positions=True, mode_name=m_name))
        else:
            lines.append(render_positions_summary(portfolio, quotes_cache, show_positions=True, mode_name=mode))
        return "\n".join(lines)

    # Specialized View: Matrix Only
    if vm == "matrix":
        if portfolios_by_mode and len(portfolios_by_mode) > 1:
            for m_name, port in portfolios_by_mode.items():
                lines.append(render_performance_matrix(port, quotes_cache, mode_name=m_name))
        else:
            lines.append(render_performance_matrix(portfolio, quotes_cache, mode_name=mode))
        return "\n".join(lines)

    # Specialized View: Multi-Mode Overview (Paper, Sandbox, Live)
    if vm in ("modes", "all_modes"):
        if multi_executor:
            lines.append(render_multi_mode_summary(multi_executor, quotes_cache))
            for m_name, adapter in multi_executor.adapters.items():
                lines.append(render_focused_mode_view(m_name, adapter, quotes_cache))
                if portfolios_by_mode and m_name in portfolios_by_mode:
                    lines.append(render_performance_matrix(portfolios_by_mode[m_name], quotes_cache, mode_name=m_name))
        else:
            lines.append(render_performance_matrix(portfolio, quotes_cache, mode_name=mode))
        return "\n".join(lines)

    # Specialized View: Focused Execution Mode (Paper, Paper-Delayed, or Live)
    if vm in ("paper", "paper_delayed", "live"):
        adapter = multi_executor.get_adapter(vm) if multi_executor else None
        lines.append(render_focused_mode_view(vm, adapter, quotes_cache))
        target_port = (portfolios_by_mode.get(vm) if portfolios_by_mode else None) or portfolio
        lines.append(render_performance_matrix(target_port, quotes_cache, mode_name=vm))
        return "\n".join(lines)

    # 1. Fresh Breakout Alerts Table (< 8s Execution Window)
    # 11 columns totaling 148 chars:
    # 15 + 10 + 8 + 9 + 9 + 7 + 9 + 9 + 18 + 12 + 12 = 118 + 30 = 148
    if fresh_alerts:
        lines.append(f"{Colors.BG_RED}{Colors.WHITE}{Colors.BOLD} 🚨 FRESH BREAKOUT ALERTS (< 8s WINDOW — ACT INSTANTLY!) {Colors.RESET}")
        fresh_hdr = (
            f"{'SYMBOL / ASSET':<15} | "
            f"{'LTP (₹)':>10} | "
            f"{'CHG %':>8} | "
            f"{'5m RVOL':>9} | "
            f"{'DAY RVOL':>9} | "
            f"{'PACE':>7} | "
            f"{'REL SCORE':>9} | "
            f"{'ABS SCORE':>9} | "
            f"{'SETUP STAGE':<18} | "
            f"{'1-LOT REQ':>12} | "
            f"{'COUNTDOWN':>12}"
        )
        lines.append(f"{Colors.BOLD}{fresh_hdr}{Colors.RESET}")
        lines.append("-" * 148)
        for a in fresh_alerts:
            cd = a.get("countdown_str", "⏳ 8.0s")
            chg = float(a.get("chg") or 0.0)
            chg_col = Colors.GREEN if chg > 0 else (Colors.RED if chg < 0 else Colors.WHITE)
            ltp_val = float(a.get("ltp") or 0.0)
            r5m = float(a.get("rvol_5m") or 0.0)
            rcum = float(a.get("rvol_cum") or 0.0)
            pace_val = float(a.get("pace") or 0.0)
            score_val = float(a.get("score") or 0.0)
            abs_val = float(a.get("abs_score") or score_val)
            stage_val = a.get("stage", "NORMAL")
            opt_val = a.get("opt_req", "Cash Only")
            sym_val = a.get("symbol", "")
            r5m_str = f"{Colors.BOLD}{Colors.RED}{r5m:>7.2f}x{Colors.RESET}" if r5m >= 2.0 else f"{r5m:>7.2f}x"
            rcum_str = f"{Colors.YELLOW}{rcum:>7.2f}x{Colors.RESET}" if rcum >= 1.5 else f"{rcum:>7.2f}x"

            lines.append(
                f"{Colors.BOLD}{pad_visible(sym_val[:15], 15, 'left')}{Colors.RESET} | "
                f"{pad_visible(f'₹{ltp_val:>8,.2f}', 10, 'right')} | "
                f"{chg_col}{pad_visible(f'{chg:>+7.2f}%', 8, 'right')}{Colors.RESET} | "
                f"{pad_visible(r5m_str, 9, 'right')} | "
                f"{pad_visible(rcum_str, 9, 'right')} | "
                f"{pad_visible(f'{pace_val:>5.2f}x', 7, 'right')} | "
                f"{make_score_badge(score_val, width=9)} | "
                f"{pad_visible(f'{abs_val:>7.1f}', 9, 'right')} | "
                f"{make_setup_badge(stage_val, width=18)} | "
                f"{pad_visible(opt_val[:12], 12, 'right')} | "
                f"{Colors.BOLD}{Colors.RED}{pad_visible(cd, 12, 'center')}{Colors.RESET}"
            )
        lines.append("-" * 148)

    # 2. Confirmed Setups Table (🚀 Breakouts, 🩸 Breakdowns & 💥 Flag Breaks)
    # 11 columns totaling 148 chars:
    # 15 + 10 + 8 + 9 + 9 + 7 + 9 + 9 + 9 + 18 + 15 = 118 + 30 = 148
    lines.append(f" {Colors.BOLD}{Colors.GREEN}🎯 CONFIRMED BREAKOUTS & BREAKDOWNS (Score >= {min_score}):{Colors.RESET}")
    setups_hdr = (
        f"{'SYMBOL / ASSET':<15} | "
        f"{'LTP (₹)':>10} | "
        f"{'CHG %':>8} | "
        f"{'5m RVOL':>9} | "
        f"{'DAY RVOL':>9} | "
        f"{'PACE':>7} | "
        f"{'RANGE POS':^9} | "
        f"{'REL SCORE':>9} | "
        f"{'ABS SCORE':>9} | "
        f"{'SETUP STAGE':<17} | "
        f"{'1-LOT REQ (ATM)':>16}"
    )
    lines.append(f"{Colors.BOLD}{setups_hdr}{Colors.RESET}")
    lines.append("-" * 148)

    if not confirmed_setups:
        lines.append(f"  {Colors.GRAY}Monitoring active instruments. Awaiting breakout triggers meeting quality filter (Score >= {min_score})...{Colors.RESET}")
    else:
        for s in confirmed_setups[:8]:
            chg = float(s.get("chg") or 0.0)
            chg_col = Colors.GREEN if chg > 0 else (Colors.RED if chg < 0 else Colors.WHITE)
            ltp_val = float(s.get("ltp") or 0.0)
            r5m = float(s.get("rvol_5m") or 0.0)
            rcum = float(s.get("rvol_cum") or 0.0)
            pace_val = float(s.get("pace") or 0.0)
            score_val = float(s.get("score") or 0.0)
            abs_val = float(s.get("abs_score") or score_val)
            stage_val = s.get("stage", "NORMAL")
            opt_val = s.get("opt_req") or "Cash Only"
            sym_val = s.get("symbol", "")
            r5m_str = f"{Colors.BOLD}{Colors.RED}{r5m:>7.2f}x{Colors.RESET}" if r5m >= 2.0 else f"{r5m:>7.2f}x"
            rcum_str = f"{Colors.YELLOW}{rcum:>7.2f}x{Colors.RESET}" if rcum >= 1.5 else f"{rcum:>7.2f}x"
            range_bar = make_range_bar(float(s.get("low", 0.0)), float(s.get("high", 0.0)), ltp_val, width=7)

            lines.append(
                f"{Colors.BOLD}{pad_visible(sym_val[:15], 15, 'left')}{Colors.RESET} | "
                f"{pad_visible(f'₹{ltp_val:>8,.2f}', 10, 'right')} | "
                f"{chg_col}{pad_visible(f'{chg:>+7.2f}%', 8, 'right')}{Colors.RESET} | "
                f"{pad_visible(r5m_str, 9, 'right')} | "
                f"{pad_visible(rcum_str, 9, 'right')} | "
                f"{pad_visible(f'{pace_val:>5.2f}x', 7, 'right')} | "
                f"{pad_visible(range_bar, 9, 'center')} | "
                f"{make_score_badge(score_val, width=9)} | "
                f"{pad_visible(f'{abs_val:>7.1f}', 9, 'right')} | "
                f"{make_setup_badge(stage_val, width=17)} | "
                f"{pad_visible(opt_val[:16], 16, 'right')}"
            )
        lines.append("-" * 148)

    # 3. Top Monitored Active Assets (Live Stream Feed)
    # 11 columns totaling 148 chars:
    # 16 + 10 + 8 + 9 + 9 + 7 + 9 + 9 + 9 + 18 + 14 = 118 + 30 = 148
    if monitored_assets:
        lines.append(f" {Colors.CYAN}{Colors.BOLD}📊 TOP MONITORED ASSETS (Live Stream Feed — {len(monitored_assets)} Instruments):{Colors.RESET}")
        assets_hdr = (
            f"{'SYMBOL / ASSET':<16} | "
            f"{'LTP (₹)':>10} | "
            f"{'CHG %':>8} | "
            f"{'5m RVOL':>9} | "
            f"{'DAY RVOL':>9} | "
            f"{'PACE':>7} | "
            f"{'RANGE POS':^9} | "
            f"{'REL SCORE':>9} | "
            f"{'ABS SCORE':>9} | "
            f"{'SETUP STAGE':<18} | "
            f"{'ACTIVITY':<14}"
        )
        lines.append(f"{Colors.BOLD}{assets_hdr}{Colors.RESET}")
        lines.append("-" * 148)

        # Sort by relative score descending, then by 5m RVOL
        sorted_assets = sorted(
            monitored_assets,
            key=lambda x: (x.get("score", 0.0), x.get("rvol_5m", 0.0), x.get("rvol_cum", 0.0)),
            reverse=True,
        )

        now_ts = time.time()
        for a in sorted_assets[:12]:
            chg = float(a.get("chg") or 0.0)
            chg_col = Colors.GREEN if chg > 0 else (Colors.RED if chg < 0 else Colors.WHITE)
            ltp = float(a.get("ltp") or 0.0)
            r5m = float(a.get("rvol_5m") or 0.0)
            rcum = float(a.get("rvol_cum") or 0.0)
            pace = float(a.get("pace") or 0.0)
            score = float(a.get("score") or 0.0)
            abs_sc = float(a.get("abs_score") or score)
            stg = a.get("stage", "NORMAL")
            sym = a.get("symbol", "")

            # Check tick recency for stream status
            last_up = float(a.get("last_update", 0.0))
            is_active = (now_ts - last_up) <= 10.0
            act_state = "⚡ STREAM" if is_active else a.get("activity_state", "💤 IDLE")

            # 5m RVOL coloring matching legacy
            if r5m >= 2.0:
                r5m_str = f"{Colors.BOLD}{Colors.RED}{r5m:>7.2f}x{Colors.RESET}"
            elif r5m >= 1.5:
                r5m_str = f"{Colors.BOLD}{Colors.YELLOW}{r5m:>7.2f}x{Colors.RESET}"
            elif r5m < 0.55:
                r5m_str = f"{Colors.GRAY}{r5m:>7.2f}x{Colors.RESET}"
            else:
                r5m_str = f"{r5m:>7.2f}x"

            # Day RVOL coloring
            if rcum >= 2.0:
                rcum_str = f"{Colors.RED}{rcum:>7.2f}x{Colors.RESET}"
            elif rcum >= 1.3:
                rcum_str = f"{Colors.YELLOW}{rcum:>7.2f}x{Colors.RESET}"
            else:
                rcum_str = f"{rcum:>7.2f}x"

            # Pace coloring
            if pace >= 1.5:
                pace_str = f"{Colors.BOLD}{Colors.GREEN}{pace:>5.2f}x{Colors.RESET}"
            elif pace >= 1.0:
                pace_str = f"{Colors.CYAN}{pace:>5.2f}x{Colors.RESET}"
            elif pace <= 0.45:
                pace_str = f"{Colors.GRAY}{pace:>5.2f}x{Colors.RESET}"
            else:
                pace_str = f"{pace:>5.2f}x"

            # Range bar: Use actual day high and low or clean neutral indicator
            a_high = float(a.get("high", 0.0))
            a_low = float(a.get("low", 0.0))
            if a_high > 0 and a_low > 0 and a_high > a_low:
                range_bar = make_range_bar(a_low, a_high, ltp, width=7)
            else:
                range_bar = "[  ●  ]"

            lines.append(
                f"{Colors.BOLD}{pad_visible(sym[:16], 16, 'left')}{Colors.RESET} | "
                f"{pad_visible(f'₹{ltp:>8,.2f}', 10, 'right')} | "
                f"{chg_col}{pad_visible(f'{chg:>+7.2f}%', 8, 'right')}{Colors.RESET} | "
                f"{pad_visible(r5m_str, 9, 'right')} | "
                f"{pad_visible(rcum_str, 9, 'right')} | "
                f"{pad_visible(pace_str, 7, 'right')} | "
                f"{pad_visible(range_bar, 9, 'center')} | "
                f"{make_score_badge(score, width=9)} | "
                f"{pad_visible(f'{abs_sc:>7.1f}', 9, 'right')} | "
                f"{make_setup_badge(stg, width=18)} | "
                f"{make_activity_badge(act_state, width=14)}"
            )
        lines.append("-" * 148)

    if vm == "alerts":
        return "\n".join(lines)

    # 4. Multi-Mode Execution Overview (if multi_executor present and running multiple modes)
    if multi_executor and len(multi_executor.active_modes) > 1:
        lines.append(render_multi_mode_summary(multi_executor, quotes_cache))

    # 5. Active Positions View
    lines.append(render_positions_summary(portfolio, quotes_cache, show_positions=show_positions))

    # 6. Recently Executed Orders Table (Compact view of last 5 executed trades)
    lines.append(render_recently_executed_orders(portfolio, quotes_cache, max_orders=5))

    # 7. Performance Matrix Table (Per-Mode or Primary)
    if portfolios_by_mode and len(portfolios_by_mode) > 1:
        for m_name, port in portfolios_by_mode.items():
            lines.append(render_performance_matrix(port, quotes_cache, mode_name=m_name))
    else:
        lines.append(render_performance_matrix(portfolio, quotes_cache, mode_name=mode))

    return "\n".join(lines)
