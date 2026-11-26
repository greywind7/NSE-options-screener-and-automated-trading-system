"""
Cross-Session Batch Simulation Analytics Engine.

Aggregates multiple single-day simulation runs into ranked track leaderboards,
risk/consistency metrics, Paper vs. Paper-Delayed slippage/retention analysis,
multi-day cumulative equity and underwater drawdown curves, regime-grouped
performance matrices, multi-batch comparisons, and CSV/Markdown exports.
"""

import csv
import io
import math
import statistics
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from web.regime_classifier import REGIME_LABELS, classify_trade_window

DEFAULT_START_CAPITAL = 250000.0
PROFIT_FACTOR_CAP = 99.99

RANK_METRIC_KEYS = {
    "net_pnl": ("net_pnl", True),
    "consistency": ("linearity_score", True),
    "max_green_days": ("daily_win_rate_pct", True),
    "calmar": ("calmar_ratio", True),
    "profit_factor": ("profit_factor", True),
    "daily_win_rate": ("daily_win_rate_pct", True),
    "win_rate": ("win_rate_pct", True),
    "expectancy": ("expectancy_inr", True),
    "lowest_maxdd": ("max_drawdown_pct", False),
    "delayed_retention": ("delayed_retention_pct", True),
    "sharpe": ("daily_sharpe", True),
}


def parse_track_taxonomy(track_name: str) -> Dict[str, str]:
    """Infer strategy family and option moneyness tier from track name."""
    lower = track_name.lower()
    if "composite_shared" in lower or "shared" in lower:
        family = "composite_shared"
    elif lower.startswith("e10_"):
        family = "e10_experimental"
    elif "fast_momentum" in lower:
        family = "fast_momentum"
    elif "trend_pullback" in lower:
        family = "trend_pullback"
    elif "fixed_target" in lower:
        family = "fixed_target"
    elif "gamma_runner" in lower:
        family = "gamma_runner"
    else:
        family = "custom"

    if "_itm" in lower or lower.endswith("itm"):
        tier = "ITM"
    elif "_atm" in lower or lower.endswith("atm"):
        tier = "ATM"
    elif "_otm" in lower or lower.endswith("otm"):
        tier = "OTM"
    elif "shared" in lower:
        tier = "SHARED"
    else:
        tier = "OTHER"

    return {"family": family, "moneyness": tier}


def _compute_drawdown_stats(pnls: List[float], start_capital: float) -> Tuple[float, float, float]:
    """Return (max_dd_inr, max_dd_pct, ulcer_index) across a sequential P&L series."""
    if not pnls:
        return 0.0, 0.0, 0.0

    cum = 0.0
    peak_cum = 0.0
    max_dd_inr = 0.0
    max_dd_pct = 0.0
    sq_dd_pcts: List[float] = []

    for pnl in pnls:
        cum += pnl
        if cum > peak_cum:
            peak_cum = cum
        dd_inr = peak_cum - cum
        peak_equity = max(1.0, start_capital + peak_cum)
        dd_pct = (dd_inr / peak_equity) * 100.0
        if dd_inr > max_dd_inr:
            max_dd_inr = dd_inr
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        sq_dd_pcts.append(dd_pct * dd_pct)

    ulcer = math.sqrt(sum(sq_dd_pcts) / len(sq_dd_pcts)) if sq_dd_pcts else 0.0
    return max_dd_inr, max_dd_pct, ulcer


def _max_consecutive_negative(values: List[float]) -> int:
    streak = 0
    best = 0
    for v in values:
        if v < 0:
            streak += 1
            if streak > best:
                best = streak
        else:
            streak = 0
    return best


def _extract_session_track_view(
    run: Dict[str, Any],
    mode: str,
    track: str,
    intraday_window: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Extract session stats and trade list for a given (run, mode, track),
    optionally recomputing metrics restricted to an intraday time window.
    """
    results = run.get("results") or {}
    matrix = results.get("matrix") or {}
    details = results.get("details") or {}

    mode_matrix = matrix.get(mode) or {}
    if track not in mode_matrix:
        return None

    st = mode_matrix[track]
    start_cap = float(st.get("start_capital") or DEFAULT_START_CAPITAL)
    all_trades = ((details.get(mode) or {}).get(track) or {}).get("trades") or []

    if intraday_window and intraday_window != "ALL":
        filtered_trades = [
            tr for tr in all_trades
            if classify_trade_window(tr.get("entry_time"), tr.get("entry_time_str")) == intraday_window
        ]
        wins = 0
        breakevens = 0
        losses = 0
        gross_pnl = 0.0
        total_brokerage = 0.0
        net_pnl = 0.0
        for tr in filtered_trades:
            ret_pct = float(tr.get("return_pct") or 0.0)
            tr_net = float(tr.get("net_pnl") or 0.0)
            tr_brok = float(tr.get("brokerage") if tr.get("brokerage") is not None else 60.0)
            tr_gross = float(tr.get("gross_pnl") if tr.get("gross_pnl") is not None else (tr_net + tr_brok))
            gross_pnl += tr_gross
            total_brokerage += tr_brok
            net_pnl += tr_net
            if ret_pct > 2.5 or (ret_pct == 0.0 and tr_net > 0):
                wins += 1
            elif tr_net < 0:
                losses += 1
            else:
                breakevens += 1

        return {
            "start_capital": start_cap,
            "trades": len(filtered_trades),
            "wins": wins,
            "breakevens": breakevens,
            "losses": losses,
            "gross_pnl": gross_pnl,
            "total_brokerage": total_brokerage,
            "net_pnl": net_pnl,
            "trade_list": filtered_trades,
        }

    return {
        "start_capital": start_cap,
        "trades": int(st.get("trades") or 0),
        "wins": int(st.get("wins") or 0),
        "breakevens": int(st.get("breakevens") or 0),
        "losses": int(st.get("losses") or 0),
        "gross_pnl": float(st.get("gross_pnl") or 0.0),
        "total_brokerage": float(st.get("total_brokerage") or 0.0),
        "net_pnl": float(st.get("net_pnl") or 0.0),
        "trade_list": all_trades,
    }


def _aggregate_track_mode(
    runs: List[Dict[str, Any]],
    mode: str,
    track: str,
    regimes_by_date: Dict[str, Dict[str, Any]],
    intraday_window: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Aggregate complete cross-session metrics and equity curve for one (track, mode)."""
    sessions_data: List[Dict[str, Any]] = []
    all_trades: List[Dict[str, Any]] = []

    for r in runs:
        view = _extract_session_track_view(r, mode, track, intraday_window=intraday_window)
        if view is None:
            continue
        date_str = str(r.get("date") or "")
        reg = regimes_by_date.get(date_str) or {}
        regime_tag = reg.get("regime", "UNKNOWN")
        sessions_data.append({
            "run_id": r.get("id", ""),
            "date": date_str,
            "regime": regime_tag,
            **view,
        })
        for tr in view["trade_list"]:
            all_trades.append({**tr, "_session_date": date_str, "_regime": regime_tag})

    if not sessions_data:
        return None

    start_cap = sessions_data[0]["start_capital"] or DEFAULT_START_CAPITAL
    sessions_count = len(sessions_data)

    total_trades = sum(s["trades"] for s in sessions_data)
    total_wins = sum(s["wins"] for s in sessions_data)
    total_be = sum(s["breakevens"] for s in sessions_data)
    total_losses = sum(s["losses"] for s in sessions_data)
    gross_pnl = sum(s["gross_pnl"] for s in sessions_data)
    total_brokerage = sum(s["total_brokerage"] for s in sessions_data)
    net_pnl = sum(s["net_pnl"] for s in sessions_data)

    daily_pnls = [s["net_pnl"] for s in sessions_data]
    green_days = sum(1 for p in daily_pnls if p > 0)
    red_days = sum(1 for p in daily_pnls if p < 0)
    flat_days = sessions_count - green_days - red_days

    win_rate_pct = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0
    non_loss_rate_pct = ((total_wins + total_be) / total_trades * 100.0) if total_trades > 0 else 0.0
    daily_win_rate_pct = (green_days / sessions_count * 100.0) if sessions_count > 0 else 0.0
    return_pct = (net_pnl / start_cap * 100.0) if start_cap > 0 else 0.0
    expectancy_inr = (net_pnl / total_trades) if total_trades > 0 else 0.0
    avg_daily_pnl = net_pnl / sessions_count if sessions_count > 0 else 0.0

    brokerage_drag_pct: Optional[float] = None
    if gross_pnl > 0:
        brokerage_drag_pct = (total_brokerage / gross_pnl) * 100.0

    # Trade-level profitability & risk stats (fallback cleanly to daily series if trade details omitted)
    if all_trades:
        win_trade_pnls = [float(t.get("net_pnl") or 0.0) for t in all_trades if float(t.get("net_pnl") or 0.0) > 0]
        loss_trade_pnls = [float(t.get("net_pnl") or 0.0) for t in all_trades if float(t.get("net_pnl") or 0.0) < 0]
        trade_pnl_seq = [float(t.get("net_pnl") or 0.0) for t in all_trades]
        avg_trade_ret_pct = sum(float(t.get("return_pct") or 0.0) for t in all_trades) / len(all_trades)
    else:
        win_trade_pnls = [p for p in daily_pnls if p > 0]
        loss_trade_pnls = [p for p in daily_pnls if p < 0]
        trade_pnl_seq = list(daily_pnls)
        avg_trade_ret_pct = 0.0

    sum_win_inr = sum(win_trade_pnls)
    sum_loss_abs = abs(sum(loss_trade_pnls))
    pf_inf = False
    if sum_loss_abs > 1e-9:
        profit_factor = min(PROFIT_FACTOR_CAP, sum_win_inr / sum_loss_abs)
    elif sum_win_inr > 0:
        profit_factor = PROFIT_FACTOR_CAP
        pf_inf = True
    else:
        profit_factor = 0.0

    avg_win_inr = (sum_win_inr / len(win_trade_pnls)) if win_trade_pnls else 0.0
    avg_loss_inr = (sum(loss_trade_pnls) / len(loss_trade_pnls)) if loss_trade_pnls else 0.0
    payoff_ratio = (avg_win_inr / abs(avg_loss_inr)) if avg_loss_inr < 0 else (PROFIT_FACTOR_CAP if avg_win_inr > 0 else 0.0)

    daily_max_dd_inr, daily_max_dd_pct, daily_ulcer = _compute_drawdown_stats(daily_pnls, start_cap)
    trade_max_dd_inr, trade_max_dd_pct, trade_ulcer = _compute_drawdown_stats(trade_pnl_seq, start_cap)

    max_dd_inr = max(daily_max_dd_inr, trade_max_dd_inr)
    max_dd_pct = max(daily_max_dd_pct, trade_max_dd_pct)
    ulcer_index = max(daily_ulcer, trade_ulcer)

    if max_dd_inr > 1e-9:
        calmar_ratio = net_pnl / max_dd_inr
    elif net_pnl > 0:
        calmar_ratio = PROFIT_FACTOR_CAP
    else:
        calmar_ratio = 0.0

    mean_daily = statistics.mean(daily_pnls)
    std_daily = statistics.stdev(daily_pnls) if sessions_count >= 2 else 0.0
    pnl_cv = (std_daily / abs(mean_daily)) if (std_daily > 0 and abs(mean_daily) > 1e-9) else None
    daily_sharpe = ((mean_daily / std_daily) * math.sqrt(252.0)) if std_daily > 1e-9 else 0.0

    # Cumulative multi-day equity & underwater drawdown curve (indexed by session)
    equity_curve: List[Dict[str, Any]] = []
    cum_net = 0.0
    cum_gross = 0.0
    peak_cum = 0.0
    for idx, s in enumerate(sessions_data):
        cum_net += s["net_pnl"]
        cum_gross += s["gross_pnl"]
        if cum_net > peak_cum:
            peak_cum = cum_net
        dd_inr = peak_cum - cum_net
        peak_equity = max(1.0, start_cap + peak_cum)
        dd_pct = (dd_inr / peak_equity) * 100.0
        equity_curve.append({
            "index": idx,
            "date": s["date"],
            "run_id": s["run_id"],
            "regime": s["regime"],
            "trades": s["trades"],
            "wins": s["wins"],
            "day_net_pnl": round(s["net_pnl"], 2),
            "day_gross_pnl": round(s["gross_pnl"], 2),
            "cum_net_pnl": round(cum_net, 2),
            "cum_gross_pnl": round(cum_gross, 2),
            "equity": round(start_cap + cum_net, 2),
            "drawdown_inr": round(dd_inr, 2),
            "drawdown_pct": round(dd_pct, 2),
        })

    # Linear 45-degree slope consistency: normalized RMSE against ideal straight line (0 -> net_pnl)
    if sessions_count <= 1:
        slope_deviation_pct = 0.0
        linearity_score = 100.0 if net_pnl > 0 else 0.0
    elif net_pnl <= 0:
        # Measure excursion vs straight trajectory, but score 0 so losing curves cannot rank top on 45-deg consistency
        ideal_sq = [
            ((equity_curve[k]["cum_net_pnl"] - ((k + 1) / sessions_count) * net_pnl) ** 2)
            for k in range(sessions_count)
        ]
        rmse = math.sqrt(sum(ideal_sq) / sessions_count)
        scale = max(1.0, abs(net_pnl), max((abs(pt["cum_net_pnl"]) for pt in equity_curve), default=1.0))
        slope_deviation_pct = (rmse / scale) * 100.0
        linearity_score = 0.0
    else:
        ideal_sq = [
            ((equity_curve[k]["cum_net_pnl"] - ((k + 1) / sessions_count) * net_pnl) ** 2)
            for k in range(sessions_count)
        ]
        rmse = math.sqrt(sum(ideal_sq) / sessions_count)
        rel_rmse = rmse / max(1.0, net_pnl)
        slope_deviation_pct = rel_rmse * 100.0
        linearity_score = 100.0 / (1.0 + rel_rmse * 2.5)

    # Per-regime stats for this specific track + mode
    regime_stats: Dict[str, Dict[str, Any]] = {}
    for s in sessions_data:
        reg = s["regime"]
        bucket = regime_stats.setdefault(reg, {
            "sessions": 0,
            "green_days": 0,
            "trades": 0,
            "wins": 0,
            "net_pnl": 0.0,
        })
        bucket["sessions"] += 1
        if s["net_pnl"] > 0:
            bucket["green_days"] += 1
        bucket["trades"] += s["trades"]
        bucket["wins"] += s["wins"]
        bucket["net_pnl"] += s["net_pnl"]

    for reg, bucket in regime_stats.items():
        bucket["win_rate_pct"] = round((bucket["wins"] / bucket["trades"] * 100.0) if bucket["trades"] > 0 else 0.0, 1)
        bucket["net_pnl"] = round(bucket["net_pnl"], 2)

    taxonomy = parse_track_taxonomy(track)

    return {
        "track": track,
        "mode": mode,
        "family": taxonomy["family"],
        "moneyness": taxonomy["moneyness"],
        "sessions": sessions_count,
        "green_days": green_days,
        "red_days": red_days,
        "flat_days": flat_days,
        "daily_win_rate_pct": round(daily_win_rate_pct, 1),
        "start_capital": round(start_cap, 2),
        "end_capital": round(start_cap + net_pnl, 2),
        "trades": total_trades,
        "wins": total_wins,
        "breakevens": total_be,
        "losses": total_losses,
        "win_rate_pct": round(win_rate_pct, 1),
        "non_loss_rate_pct": round(non_loss_rate_pct, 1),
        "gross_pnl": round(gross_pnl, 2),
        "total_brokerage": round(total_brokerage, 2),
        "brokerage_drag_pct": round(brokerage_drag_pct, 1) if brokerage_drag_pct is not None else None,
        "net_pnl": round(net_pnl, 2),
        "return_pct": round(return_pct, 2),
        "avg_daily_net_pnl": round(avg_daily_pnl, 2),
        "expectancy_inr": round(expectancy_inr, 2),
        "avg_trade_return_pct": round(avg_trade_ret_pct, 2),
        "profit_factor": round(profit_factor, 2),
        "profit_factor_inf": pf_inf,
        "avg_win_inr": round(avg_win_inr, 2),
        "avg_loss_inr": round(avg_loss_inr, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "max_drawdown_inr": round(max_dd_inr, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "daily_max_drawdown_inr": round(daily_max_dd_inr, 2),
        "calmar_ratio": round(calmar_ratio, 2),
        "ulcer_index": round(ulcer_index, 2),
        "worst_day_pnl": round(min(daily_pnls), 2),
        "best_day_pnl": round(max(daily_pnls), 2),
        "max_consecutive_losses": _max_consecutive_negative(daily_pnls),
        "max_consecutive_losing_trades": _max_consecutive_negative(trade_pnl_seq),
        "pnl_cv": round(pnl_cv, 2) if pnl_cv is not None else None,
        "slope_deviation_pct": round(slope_deviation_pct, 1),
        "linearity_score": round(linearity_score, 1),
        "daily_sharpe": round(daily_sharpe, 2),
        # Cross-mode fields populated below once all modes are aggregated
        "delayed_net_pnl": None,
        "delayed_retention_pct": None,
        "slippage_cost_inr": None,
        "latency_resilience": None,
        "regime_stats": regime_stats,
        "equity_curve": equity_curve,
    }


def _build_portfolio_curve(
    track_rows: List[Dict[str, Any]],
    runs: List[Dict[str, Any]],
    regimes_by_date: Dict[str, Dict[str, Any]],
    mode: str,
) -> List[Dict[str, Any]]:
    """Build equal-weighted portfolio average equity & drawdown curve for one mode."""
    mode_rows = [r for r in track_rows if r["mode"] == mode]
    if not mode_rows:
        return []

    by_date_vals: Dict[str, List[float]] = defaultdict(list)
    by_date_gross: Dict[str, List[float]] = defaultdict(list)
    by_date_trades: Dict[str, List[int]] = defaultdict(list)
    run_id_by_date: Dict[str, str] = {}

    for r in runs:
        run_id_by_date[str(r.get("date") or "")] = str(r.get("id") or "")

    for row in mode_rows:
        for pt in row["equity_curve"]:
            by_date_vals[pt["date"]].append(pt["day_net_pnl"])
            by_date_gross[pt["date"]].append(pt["day_gross_pnl"])
            by_date_trades[pt["date"]].append(pt["trades"])

    dates_sorted = sorted(by_date_vals.keys())
    start_cap = mode_rows[0]["start_capital"] or DEFAULT_START_CAPITAL
    cum_net = 0.0
    cum_gross = 0.0
    peak_cum = 0.0
    curve: List[Dict[str, Any]] = []

    for idx, d in enumerate(dates_sorted):
        day_net = statistics.mean(by_date_vals[d])
        day_gross = statistics.mean(by_date_gross[d])
        day_trades = int(round(sum(by_date_trades[d])))
        cum_net += day_net
        cum_gross += day_gross
        if cum_net > peak_cum:
            peak_cum = cum_net
        dd_inr = peak_cum - cum_net
        dd_pct = (dd_inr / max(1.0, start_cap + peak_cum)) * 100.0
        reg = (regimes_by_date.get(d) or {}).get("regime", "UNKNOWN")
        curve.append({
            "index": idx,
            "date": d,
            "run_id": run_id_by_date.get(d, ""),
            "regime": reg,
            "trades": day_trades,
            "day_net_pnl": round(day_net, 2),
            "day_gross_pnl": round(day_gross, 2),
            "cum_net_pnl": round(cum_net, 2),
            "cum_gross_pnl": round(cum_gross, 2),
            "equity": round(start_cap + cum_net, 2),
            "drawdown_inr": round(dd_inr, 2),
            "drawdown_pct": round(dd_pct, 2),
        })

    return curve


def _compute_intraday_window_breakdown(
    runs: List[Dict[str, Any]],
    mode: str = "paper",
) -> List[Dict[str, Any]]:
    """Aggregate trade performance across Opening / Midday / Closing intraday windows."""
    buckets = {
        "OPENING": {"window": "OPENING", "label": "09:15 - 11:30 Opening", "trades": 0, "wins": 0, "net_pnl": 0.0},
        "MIDDAY": {"window": "MIDDAY", "label": "11:30 - 13:30 Midday", "trades": 0, "wins": 0, "net_pnl": 0.0},
        "CLOSING": {"window": "CLOSING", "label": "13:30 - 15:20 Closing", "trades": 0, "wins": 0, "net_pnl": 0.0},
    }

    for r in runs:
        details = ((r.get("results") or {}).get("details") or {}).get(mode) or {}
        for tr_list_wrap in details.values():
            for tr in tr_list_wrap.get("trades") or []:
                win_key = classify_trade_window(tr.get("entry_time"), tr.get("entry_time_str"))
                b = buckets[win_key]
                b["trades"] += 1
                tr_net = float(tr.get("net_pnl") or 0.0)
                ret_pct = float(tr.get("return_pct") or 0.0)
                b["net_pnl"] += tr_net
                if ret_pct > 2.5 or (ret_pct == 0.0 and tr_net > 0):
                    b["wins"] += 1

    out: List[Dict[str, Any]] = []
    for k in ("OPENING", "MIDDAY", "CLOSING"):
        b = buckets[k]
        wr = (b["wins"] / b["trades"] * 100.0) if b["trades"] > 0 else 0.0
        exp = (b["net_pnl"] / b["trades"]) if b["trades"] > 0 else 0.0
        out.append({
            "window": k,
            "label": b["label"],
            "trades": b["trades"],
            "wins": b["wins"],
            "win_rate_pct": round(wr, 1),
            "net_pnl": round(b["net_pnl"], 2),
            "expectancy_inr": round(exp, 2),
        })
    return out


def _pick_spotlights(rows: List[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Identify winner tracks across distinct objectives (preferring paper mode)."""
    paper_rows = [r for r in rows if r["mode"] == "paper"] or rows
    if not paper_rows:
        return {}

    def _pack(r: Dict[str, Any], val_str: str) -> Dict[str, Any]:
        return {
            "track": r["track"],
            "mode": r["mode"],
            "value_display": val_str,
            "net_pnl": r["net_pnl"],
            "daily_win_rate_pct": r["daily_win_rate_pct"],
            "green_days": r["green_days"],
            "sessions": r["sessions"],
            "max_drawdown_pct": r["max_drawdown_pct"],
            "calmar_ratio": r["calmar_ratio"],
            "linearity_score": r.get("linearity_score", 0.0),
            "slope_deviation_pct": r.get("slope_deviation_pct", 0.0),
            "pnl_cv": r.get("pnl_cv"),
        }

    best_net = max(paper_rows, key=lambda r: r["net_pnl"])
    best_calmar = max(paper_rows, key=lambda r: (r["calmar_ratio"], r["net_pnl"]))

    profitable_rows = [r for r in paper_rows if r["net_pnl"] > 0] or paper_rows
    most_consistent = max(
        profitable_rows,
        key=lambda r: (
            r.get("linearity_score", 0.0),
            -(r["pnl_cv"] if r.get("pnl_cv") is not None else 99.0),
            r["net_pnl"],
        ),
    )
    max_green_days = max(
        paper_rows,
        key=lambda r: (
            r["daily_win_rate_pct"],
            r["green_days"],
            -(r["pnl_cv"] if r.get("pnl_cv") is not None else 99.0),
            r["net_pnl"],
        ),
    )
    lowest_dd = min(paper_rows, key=lambda r: (r["max_drawdown_pct"], -r["net_pnl"]))

    resilient_candidates = [r for r in paper_rows if r.get("delayed_retention_pct") is not None]
    best_retention = (
        max(resilient_candidates, key=lambda r: (r["delayed_retention_pct"], r["net_pnl"]))
        if resilient_candidates
        else None
    )

    spotlights: Dict[str, Optional[Dict[str, Any]]] = {
        "highest_net": _pack(best_net, f"{best_net['net_pnl']:+,.0f}"),
        "best_calmar": _pack(best_calmar, f"{best_calmar['calmar_ratio']:.2f}x"),
        "most_consistent": _pack(most_consistent, f"{most_consistent.get('linearity_score', 0.0):.1f}/100 Slope"),
        "max_green_days": _pack(max_green_days, f"{max_green_days['daily_win_rate_pct']:.1f}% ({max_green_days['green_days']}/{max_green_days['sessions']})"),
        "lowest_drawdown": _pack(lowest_dd, f"-{lowest_dd['max_drawdown_pct']:.2f}%"),
        "best_delayed_retention": (
            _pack(best_retention, f"{best_retention['delayed_retention_pct']:.1f}%")
            if best_retention
            else None
        ),
    }
    return spotlights


def compute_batch_report(
    runs: List[Dict[str, Any]],
    regimes_by_date: Optional[Dict[str, Dict[str, Any]]] = None,
    batch_id: str = "AD-HOC",
    batch_label: str = "Batch Simulation Report",
    regime_filter: Optional[str] = None,
    intraday_window: Optional[str] = None,
    rank_by: str = "net_pnl",
) -> Dict[str, Any]:
    """
    Build the full cross-date Batch Simulation Report for a sequence of runs.
    """
    regimes_map = regimes_by_date or {}

    # Deduplicate same-date runs conservatively (keep latest run per date inside a batch)
    # unless multiple configs intentionally share a date with unique IDs — wait:
    # For a batch range, dates are unique; if repeated, keep latest run per date.
    ordered_all = sorted(runs, key=lambda r: (str(r.get("date") or ""), str(r.get("timestamp") or ""), str(r.get("id") or "")))
    by_date_latest: Dict[str, Dict[str, Any]] = {}
    for r in ordered_all:
        by_date_latest[str(r.get("date") or r.get("id") or "")] = r
    chronological_runs = sorted(by_date_latest.values(), key=lambda r: str(r.get("date") or ""))

    # Build complete sessions metadata timeline (pre-filter) for regime pills & P&L attribution
    all_sessions_meta: List[Dict[str, Any]] = []
    regime_counts: Dict[str, int] = defaultdict(int)
    for r in chronological_runs:
        d = str(r.get("date") or "")
        reg = regimes_map.get(d) or {"date": d, "regime": "UNKNOWN", "label": "Unknown", "metrics": {}}
        reg_code = reg.get("regime", "UNKNOWN")
        regime_counts[reg_code] += 1

        mat = (r.get("results") or {}).get("matrix") or {}
        p_tracks = mat.get("paper") or {}
        d_tracks = mat.get("paper_delayed") or {}
        p_nets = [float(st.get("net_pnl") or 0.0) for st in p_tracks.values()]
        d_nets = [float(st.get("net_pnl") or 0.0) for st in d_tracks.values()]
        ref_tracks = p_tracks or d_tracks
        sess_trades = sum(int(st.get("trades") or 0) for st in ref_tracks.values())
        sess_wins = sum(int(st.get("wins") or 0) for st in ref_tracks.values())
        top_track = "-"
        top_track_pnl = 0.0
        if ref_tracks:
            best_pair = max(ref_tracks.items(), key=lambda kv: float(kv[1].get("net_pnl") or 0.0))
            top_track = best_pair[0]
            top_track_pnl = float(best_pair[1].get("net_pnl") or 0.0)

        all_sessions_meta.append({
            "run_id": r.get("id", ""),
            "date": d,
            "runtime": r.get("runtime", 0),
            "cached": bool(r.get("cached", False)),
            "regime": reg_code,
            "regime_label": reg.get("label", REGIME_LABELS.get(reg_code, reg_code)),
            "basket_return_pct": (reg.get("metrics") or {}).get("basket_return_pct", 0.0),
            "efficiency_ratio": (reg.get("metrics") or {}).get("efficiency_ratio", 0.0),
            "realized_vol_pct": (reg.get("metrics") or {}).get("realized_vol_pct", 0.0),
            "mean_breadth_pct": (reg.get("metrics") or {}).get("mean_breadth_pct", 50.0),
            "is_thursday": bool(reg.get("is_thursday", False)),
            "trades": sess_trades,
            "win_rate_pct": round((sess_wins / sess_trades * 100.0) if sess_trades > 0 else 0.0, 1),
            "paper_avg_net_pnl": round(statistics.mean(p_nets), 2) if p_nets else None,
            "delayed_avg_net_pnl": round(statistics.mean(d_nets), 2) if d_nets else None,
            "top_track": top_track,
            "top_track_pnl": round(top_track_pnl, 2),
        })

    # Apply optional regime filter
    active_filter = (regime_filter or "ALL").strip().upper()
    if active_filter and active_filter != "ALL":
        allowed = {tag.strip() for tag in active_filter.split(",") if tag.strip()}
        filtered_runs = [
            r for r in chronological_runs
            if (regimes_map.get(str(r.get("date") or "")) or {}).get("regime", "UNKNOWN") in allowed
        ]
    else:
        filtered_runs = chronological_runs

    active_window = (intraday_window or "ALL").strip().upper()

    # Collect all (mode, track) pairs
    modes_seen: List[str] = []
    tracks_by_mode: Dict[str, set] = defaultdict(set)
    for r in filtered_runs:
        matrix = (r.get("results") or {}).get("matrix") or {}
        for mName, mTracks in matrix.items():
            if mName not in modes_seen:
                modes_seen.append(mName)
            for trName in mTracks.keys():
                tracks_by_mode[mName].add(trName)

    # Order paper first, then paper_delayed, then any remaining modes
    ordered_modes = [m for m in ("paper", "paper_delayed") if m in modes_seen]
    ordered_modes.extend(m for m in modes_seen if m not in ordered_modes)

    # Aggregate every (track, mode)
    rows_by_pair: Dict[Tuple[str, str], Dict[str, Any]] = {}
    leaderboard: List[Dict[str, Any]] = []
    for m in ordered_modes:
        for tr in sorted(tracks_by_mode[m]):
            agg = _aggregate_track_mode(
                filtered_runs,
                mode=m,
                track=tr,
                regimes_by_date=regimes_map,
                intraday_window=active_window,
            )
            if agg:
                rows_by_pair[(tr, m)] = agg
                leaderboard.append(agg)

    # Populate Paper vs Paper-Delayed retention and slippage comparison columns
    for (tr, m), row in rows_by_pair.items():
        delayed = rows_by_pair.get((tr, "paper_delayed"))
        paper = rows_by_pair.get((tr, "paper"))
        if m == "paper" and delayed is not None:
            row["delayed_net_pnl"] = delayed["net_pnl"]
            row["slippage_cost_inr"] = round(row["net_pnl"] - delayed["net_pnl"], 2)
            if row["net_pnl"] > 0:
                ret = (delayed["net_pnl"] / row["net_pnl"]) * 100.0
                row["delayed_retention_pct"] = round(ret, 1)
                row["latency_resilience"] = (
                    "HIGH" if ret >= 80.0 else ("MODERATE" if ret >= 50.0 else "FRAGILE")
                )
        elif m == "paper_delayed" and paper is not None:
            row["delayed_net_pnl"] = row["net_pnl"]
            row["slippage_cost_inr"] = round(paper["net_pnl"] - row["net_pnl"], 2)
            if paper["net_pnl"] > 0:
                ret = (row["net_pnl"] / paper["net_pnl"]) * 100.0
                row["delayed_retention_pct"] = round(ret, 1)
                row["latency_resilience"] = (
                    "HIGH" if ret >= 80.0 else ("MODERATE" if ret >= 50.0 else "FRAGILE")
                )

    # Rank leaderboard cleanly by chosen objective
    metric_key, descending = RANK_METRIC_KEYS.get(rank_by, ("net_pnl", True))

    def _sort_key(item: Dict[str, Any]) -> Tuple[int, float, float]:
        val = item.get(metric_key)
        is_missing = 1 if val is None else 0
        num = float(val) if val is not None else 0.0
        primary = -num if descending else num
        # Secondary tie-breaker: higher net_pnl
        return (is_missing, primary, -float(item.get("net_pnl") or 0.0))

    leaderboard.sort(key=_sort_key)
    for rank_idx, row in enumerate(leaderboard, start=1):
        row["rank"] = rank_idx

    # Build Portfolio Equity & Drawdown curves + Top-3 paper tracks curves
    portfolio_curves: Dict[str, List[Dict[str, Any]]] = {}
    for m in ordered_modes:
        portfolio_curves[m] = _build_portfolio_curve(leaderboard, filtered_runs, regimes_map, m)

    # Family & Moneyness Rollups (based on paper mode when available, else first mode)
    rollup_mode = "paper" if "paper" in ordered_modes else (ordered_modes[0] if ordered_modes else "paper")
    rollup_rows = [r for r in leaderboard if r["mode"] == rollup_mode]

    def _group_rollup(key: str) -> List[Dict[str, Any]]:
        grp: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
            "tracks": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "trades": 0, "wins": 0, "max_dd_pcts": []
        })
        for r in rollup_rows:
            g = grp[r[key]]
            g["tracks"] += 1
            g["net_pnl"] += r["net_pnl"]
            g["gross_pnl"] += r["gross_pnl"]
            g["trades"] += r["trades"]
            g["wins"] += r["wins"]
            g["max_dd_pcts"].append(r["max_drawdown_pct"])
        out = []
        for name, g in grp.items():
            wr = (g["wins"] / g["trades"] * 100.0) if g["trades"] > 0 else 0.0
            out.append({
                key: name,
                "tracks": g["tracks"],
                "avg_net_pnl": round(g["net_pnl"] / g["tracks"], 2),
                "total_net_pnl": round(g["net_pnl"], 2),
                "trades": g["trades"],
                "win_rate_pct": round(wr, 1),
                "avg_max_dd_pct": round(statistics.mean(g["max_dd_pcts"]), 2) if g["max_dd_pcts"] else 0.0,
            })
        out.sort(key=lambda x: x["avg_net_pnl"], reverse=True)
        return out

    # Market Regime Breakdown across sessions (avg track performance in each regime)
    regime_breakdown: List[Dict[str, Any]] = []
    runs_by_regime: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in chronological_runs:
        reg_code = (regimes_map.get(str(r.get("date") or "")) or {}).get("regime", "UNKNOWN")
        runs_by_regime[reg_code].append(r)

    for reg_code, reg_runs in sorted(runs_by_regime.items()):
        avg_day_nets: List[float] = []
        tot_trades = 0
        tot_wins = 0
        for rr in reg_runs:
            m_tracks = ((rr.get("results") or {}).get("matrix") or {}).get(rollup_mode) or {}
            if m_tracks:
                vals = [float(st.get("net_pnl") or 0.0) for st in m_tracks.values()]
                avg_day_nets.append(statistics.mean(vals))
                tot_trades += sum(int(st.get("trades") or 0) for st in m_tracks.values())
                tot_wins += sum(int(st.get("wins") or 0) for st in m_tracks.values())
        regime_breakdown.append({
            "regime": reg_code,
            "label": REGIME_LABELS.get(reg_code, reg_code),
            "sessions": len(reg_runs),
            "dates": [str(rr.get("date") or "") for rr in reg_runs],
            "avg_track_session_pnl": round(statistics.mean(avg_day_nets), 2) if avg_day_nets else 0.0,
            "green_sessions": sum(1 for v in avg_day_nets if v > 0),
            "total_trades": tot_trades,
            "win_rate_pct": round((tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0, 1),
        })

    # Intraday window breakdown (always computed over filtered runs)
    intraday_breakdown = _compute_intraday_window_breakdown(filtered_runs, mode=rollup_mode)

    # Top-level portfolio headline metrics
    port_curve = portfolio_curves.get(rollup_mode) or []
    port_net = port_curve[-1]["cum_net_pnl"] if port_curve else 0.0
    port_gross = port_curve[-1]["cum_gross_pnl"] if port_curve else 0.0
    port_max_dd_inr = max((pt["drawdown_inr"] for pt in port_curve), default=0.0)
    port_max_dd_pct = max((pt["drawdown_pct"] for pt in port_curve), default=0.0)
    port_green_days = sum(1 for pt in port_curve if pt["day_net_pnl"] > 0)
    dates_included = [str(r.get("date") or "") for r in filtered_runs]

    return {
        "batch_id": batch_id,
        "batch_label": batch_label,
        "filters": {
            "regime": active_filter,
            "intraday_window": active_window,
            "rank_by": rank_by,
        },
        "summary": {
            "total_sessions": len(chronological_runs),
            "filtered_sessions": len(filtered_runs),
            "date_start": dates_included[0] if dates_included else "",
            "date_end": dates_included[-1] if dates_included else "",
            "modes": ordered_modes,
            "track_count": len({r["track"] for r in leaderboard}),
            "portfolio_mode": rollup_mode,
            "portfolio_avg_net_pnl": round(port_net, 2),
            "portfolio_avg_gross_pnl": round(port_gross, 2),
            "portfolio_max_dd_inr": round(port_max_dd_inr, 2),
            "portfolio_max_dd_pct": round(port_max_dd_pct, 2),
            "portfolio_green_days": port_green_days,
            "portfolio_daily_win_rate_pct": round(
                (port_green_days / len(filtered_runs) * 100.0) if filtered_runs else 0.0, 1
            ),
        },
        "spotlights": _pick_spotlights(leaderboard),
        "regime_counts": dict(regime_counts),
        "regime_breakdown": regime_breakdown,
        "intraday_breakdown": intraday_breakdown,
        "family_rollup": _group_rollup("family"),
        "moneyness_rollup": _group_rollup("moneyness"),
        "sessions": all_sessions_meta,
        "leaderboard": leaderboard,
        "portfolio_curves": portfolio_curves,
    }


def build_batch_comparison_summary(batch_reports: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a compact side-by-side comparison table across multiple completed batches."""
    comparison: List[Dict[str, Any]] = []
    for rep in batch_reports:
        sm = rep.get("summary") or {}
        top = (rep.get("spotlights") or {}).get("highest_net") or {}
        calmar_top = (rep.get("spotlights") or {}).get("best_calmar") or {}
        comparison.append({
            "batch_id": rep.get("batch_id", ""),
            "batch_label": rep.get("batch_label", ""),
            "date_range": f"{sm.get('date_start', '')} -> {sm.get('date_end', '')}",
            "sessions": sm.get("total_sessions", 0),
            "track_count": sm.get("track_count", 0),
            "portfolio_avg_net_pnl": sm.get("portfolio_avg_net_pnl", 0.0),
            "portfolio_max_dd_pct": sm.get("portfolio_max_dd_pct", 0.0),
            "portfolio_daily_win_rate_pct": sm.get("portfolio_daily_win_rate_pct", 0.0),
            "top_track": top.get("track", "-"),
            "top_track_net_pnl": top.get("net_pnl", 0.0),
            "best_calmar_track": calmar_top.get("track", "-"),
            "best_calmar": calmar_top.get("calmar_ratio", 0.0),
        })
    return comparison


def export_batch_markdown(report: Dict[str, Any]) -> str:
    """Format a Batch Simulation Report into clean Markdown tables."""
    sm = report.get("summary") or {}
    lines: List[str] = [
        f"# Batch Simulation Report: {report.get('batch_label', report.get('batch_id'))}",
        "",
        f"- **Batch ID**: `{report.get('batch_id')}`",
        f"- **Sessions**: {sm.get('filtered_sessions', 0)} / {sm.get('total_sessions', 0)} "
        f"({sm.get('date_start', '-')} to {sm.get('date_end', '-')})",
        f"- **Filters**: Regime=`{report.get('filters', {}).get('regime', 'ALL')}`, "
        f"Window=`{report.get('filters', {}).get('intraday_window', 'ALL')}`, "
        f"RankBy=`{report.get('filters', {}).get('rank_by', 'net_pnl')}`",
        f"- **Portfolio Avg Net P&L ({sm.get('portfolio_mode', 'paper').upper()})**: "
        f"INR {sm.get('portfolio_avg_net_pnl', 0.0):+,.2f} | "
        f"Max DD: -{sm.get('portfolio_max_dd_pct', 0.0):.2f}% | "
        f"Daily Win Rate: {sm.get('portfolio_daily_win_rate_pct', 0.0):.1f}%",
        "",
        "## Track Leaderboard",
        "",
        "| Rank | Mode | Track | Sessions | Green% | Trades | WR% | Net P&L (INR) | Ret% | Profit Factor | Expectancy | Max DD% | Calmar | Delayed Ret% |",
        "|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for r in report.get("leaderboard") or []:
        del_ret = f"{r['delayed_retention_pct']:.1f}%" if r.get("delayed_retention_pct") is not None else "-"
        pf_str = "INF" if r.get("profit_factor_inf") else f"{r['profit_factor']:.2f}"
        lines.append(
            f"| {r['rank']} | {r['mode'].upper()} | `{r['track']}` | {r['sessions']} | "
            f"{r['daily_win_rate_pct']:.1f}% | {r['trades']} | {r['win_rate_pct']:.1f}% | "
            f"{r['net_pnl']:+,.2f} | {r['return_pct']:+.2f}% | {pf_str} | "
            f"{r['expectancy_inr']:+,.1f} | -{r['max_drawdown_pct']:.2f}% | "
            f"{r['calmar_ratio']:.2f} | {del_ret} |"
        )

    lines.extend([
        "",
        "## Market Regime Breakdown",
        "",
        "| Regime | Sessions | Green Days | Avg Track P&L/Day | Trades | Win Rate% | Dates |",
        "|:---|---:|---:|---:|---:|---:|:---|",
    ])
    for rb in report.get("regime_breakdown") or []:
        lines.append(
            f"| {rb['label']} (`{rb['regime']}`) | {rb['sessions']} | {rb['green_sessions']} | "
            f"{rb['avg_track_session_pnl']:+,.2f} | {rb['total_trades']} | {rb['win_rate_pct']:.1f}% | "
            f"{', '.join(rb['dates'])} |"
        )

    return "\n".join(lines) + "\n"


def export_batch_csv(report: Dict[str, Any]) -> str:
    """Serialize the Batch Leaderboard into CSV format."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    headers = [
        "rank", "mode", "track", "family", "moneyness", "sessions", "green_days",
        "daily_win_rate_pct", "trades", "wins", "breakevens", "losses", "win_rate_pct",
        "gross_pnl", "total_brokerage", "brokerage_drag_pct", "net_pnl", "return_pct",
        "expectancy_inr", "profit_factor", "payoff_ratio", "max_drawdown_inr",
        "max_drawdown_pct", "calmar_ratio", "ulcer_index", "worst_day_pnl",
        "best_day_pnl", "max_consecutive_losses", "pnl_cv", "daily_sharpe",
        "delayed_net_pnl", "delayed_retention_pct", "slippage_cost_inr", "latency_resilience",
    ]
    writer.writerow(headers)
    for row in report.get("leaderboard") or []:
        writer.writerow([row.get(h, "") for h in headers])
    return buf.getvalue()
