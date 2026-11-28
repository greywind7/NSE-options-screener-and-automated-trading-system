"""
Analyze 45 experiments x 8 clean sessions (2026-11-16..2026-11-26) and print complete stats + generate SIMULATION_HYPOTHESIS_REPORT.md.
"""
import json
import os
import sys
from typing import Any, Dict, List, Tuple
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_45_experiments as r45

DATES = r45.CLEAN_DATES
RESULTS_DIR = r45.RESULTS_DIR

V1_TRACKS = [
    "fast_momentum_ATM", "fast_momentum_ITM", "fast_momentum_OTM",
    "trend_pullback_ATM", "trend_pullback_ITM", "trend_pullback_OTM",
    "fixed_target_ATM", "fixed_target_ITM", "fixed_target_OTM",
    "composite_shared_ATM", "composite_shared_ITM", "composite_shared_OTM",
]
V2_TRACKS = [
    "volatility_expansion_ATM", "adaptive_regime_ATM", "trend_pullback_v2_ITM1",
    "micro_chandelier_ITM1", "opening_thrust_convex_ATM", "delta_scalp_ITM2",
    "gamma_runner_OTM2", "structural_convexity_ATM",
]
ALL_TRACKS = V1_TRACKS + V2_TRACKS

REGIMES = {
    "TREND": ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"],
    "CHOP": ["2026-11-16", "2026-11-25"],
    "EXPIRY": ["2026-11-23", "2026-11-26"],
}


def compute_max_dd(daily_pnls: List[float]) -> float:
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in daily_pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def fmt_inr(v: float, signed: bool = False) -> str:
    if signed:
        sign = "+" if v >= 0 else "-"
        return f"{sign}₹{abs(v):,.0f}"
    return f"₹{v:,.0f}"


def fmt_k(v: float, signed: bool = True) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{abs(v)/1000.0:.1f}k" if signed else f"{v/1000.0:.1f}k"


def load_all() -> Tuple[List[r45.ExpConfig], Dict[str, Dict[str, Any]]]:
    exps = r45.build_all_45_experiments()
    data_by_date: Dict[str, Dict[str, Any]] = {}
    for d in DATES:
        path = os.path.join(RESULTS_DIR, f"results_{d}.json")
        with open(path, "r", encoding="utf-8") as f:
            data_by_date[d] = json.load(f)
    return exps, data_by_date


def summarize() -> None:
    exps, data = load_all()

    # Check track presence dynamically (handles 19-track and legacy 20-track ensembles)
    sample_tracks = set(data[DATES[0]][exps[0].exp_id]["modes"]["paper"].keys())
    active_tracks = [t for t in ALL_TRACKS if t in sample_tracks]
    active_v1 = [t for t in V1_TRACKS if t in sample_tracks]
    active_v2 = [t for t in V2_TRACKS if t in sample_tracks]
    n_days = len(DATES)
    n_exps = len(exps)

    # Per-experiment aggregate across active days
    exp_stats = []
    for e in exps:
        eid = e.exp_id
        opps = sum(data[d][eid]["opps"] for d in DATES)
        p_daily, d_daily = [], []
        p_v1_daily, p_v2_daily = [], []
        d_v1_daily, d_v2_daily = [], []
        p_tr = p_w = p_be = p_l = 0
        d_tr = d_w = d_be = d_l = 0
        p_gp = p_brok = p_net = 0.0
        d_gp = d_brok = d_net = 0.0
        p_v1 = p_v2 = d_v1 = d_v2 = 0.0
        track_7d_p = {tr: [] for tr in active_tracks}
        track_7d_d = {tr: [] for tr in active_tracks}

        for dt in DATES:
            pm = data[dt][eid]["modes"]["paper"]
            dm = data[dt][eid]["modes"]["paper_delayed"]
            dp = sum(pm[t]["net_pnl"] for t in active_tracks)
            dd_day = sum(dm[t]["net_pnl"] for t in active_tracks)
            dv1p = sum(pm[t]["net_pnl"] for t in active_v1)
            dv2p = sum(pm[t]["net_pnl"] for t in active_v2)
            dv1d = sum(dm[t]["net_pnl"] for t in active_v1)
            dv2d = sum(dm[t]["net_pnl"] for t in active_v2)
            p_daily.append(dp)
            d_daily.append(dd_day)
            p_v1_daily.append(dv1p)
            p_v2_daily.append(dv2p)
            d_v1_daily.append(dv1d)
            d_v2_daily.append(dv2d)
            p_v1 += dv1p
            p_v2 += dv2p
            d_v1 += dv1d
            d_v2 += dv2d
            for t in active_tracks:
                tp = pm[t]
                td = dm[t]
                p_tr += tp["trades"]; p_w += tp["wins"]; p_be += tp["bes"]; p_l += tp["losses"]
                p_gp += tp["gross_pnl"]; p_brok += tp["brokerage"]; p_net += tp["net_pnl"]
                d_tr += td["trades"]; d_w += td["wins"]; d_be += td["bes"]; d_l += td["losses"]
                d_gp += td["gross_pnl"]; d_brok += td["brokerage"]; d_net += td["net_pnl"]
                track_7d_p[t].append(tp["net_pnl"])
                track_7d_d[t].append(td["net_pnl"])

        # Exclude persistent losers dynamically if present
        losers3 = {"structural_convexity_ATM", "gamma_runner_OTM2", "delta_scalp_ITM2"}
        keep17 = [t for t in active_tracks if t not in losers3]
        p_17 = sum(sum(track_7d_p[t]) for t in keep17)
        d_17 = sum(sum(track_7d_d[t]) for t in keep17)

        exp_stats.append({
            "exp": e,
            "opps": opps,
            "p_tr": p_tr, "d_tr": d_tr,
            "p_wr": (p_w / p_tr * 100.0) if p_tr else 0.0,
            "d_wr": (d_w / d_tr * 100.0) if d_tr else 0.0,
            "p_gp": p_gp, "p_brok": p_brok, "p_net": p_net,
            "d_gp": d_gp, "d_brok": d_brok, "d_net": d_net,
            "p_dd": compute_max_dd(p_daily), "d_dd": compute_max_dd(d_daily),
            "p_green": sum(1 for x in p_daily if x > 0),
            "d_green": sum(1 for x in d_daily if x > 0),
            "p_v1": p_v1, "p_v2": p_v2, "d_v1": d_v1, "d_v2": d_v2,
            "p_17": p_17, "d_17": d_17,
            "p_daily": dict(zip(DATES, p_daily)),
            "d_daily": dict(zip(DATES, d_daily)),
            "p_v1_daily": dict(zip(DATES, p_v1_daily)),
            "p_v2_daily": dict(zip(DATES, p_v2_daily)),
            "track_8d_p": track_7d_p,
            "track_8d_d": track_7d_d,
        })

    exp_stats.sort(key=lambda x: x["p_net"], reverse=True)
    print(f"=== FULL LEADERBOARD ({n_days} DAYS: {DATES[0]}..{DATES[-1]}, TICK-BY-TICK | {n_exps} EXPS | {len(active_tracks)} TRACKS) ===")
    for idx, r in enumerate(exp_stats, 1):
        e = r["exp"]
        ret_dd = r["p_net"] / r["p_dd"] if r["p_dd"] > 0 else float("inf")
        print(
            f"{idx:2d}. {e.exp_id:<32s} | opps={r['opps']:4d} tr={r['p_tr']:5d} WR={r['p_wr']:4.1f}% "
            f"| P={r['p_net']:+10.0f} (DD={r['p_dd']:7.0f}, G={r['p_green']}/{n_days}, R/D={ret_dd:5.1f}) "
            f"| v1={r['p_v1']:+9.0f} v2={r['p_v2']:+9.0f} | D={r['d_net']:+10.0f} (DD={r['d_dd']:7.0f})"
        )

    print(f"\n=== TRACK CONSISTENCY ACROSS ALL {n_exps} EXPERIMENTS ({n_days} DAYS) ===")
    tr_rows = []
    for t in active_tracks:
        prof = "v1" if t in V1_TRACKS else "v2"
        p_tots = []
        d_tots = []
        p_dds = []
        p_greens = []
        all_day_vals = []
        for r in exp_stats:
            arr_p = r["track_8d_p"][t]
            arr_d = r["track_8d_d"][t]
            p_tots.append(sum(arr_p))
            d_tots.append(sum(arr_d))
            p_dds.append(compute_max_dd(arr_p))
            p_greens.append(sum(1 for x in arr_p if x > 0))
            all_day_vals.extend(arr_p)
        tr_rows.append({
            "prof": prof, "track": t,
            "pos_p": sum(1 for x in p_tots if x > 0),
            "pos_d": sum(1 for x in d_tots if x > 0),
            "avg_p": float(np.mean(p_tots)),
            "med_p": float(np.median(p_tots)),
            "avg_d": float(np.mean(d_tots)),
            "min_p": float(min(p_tots)),
            "max_p": float(max(p_tots)),
            "std_p": float(np.std(p_tots)),
            "avg_green": float(np.mean(p_greens)),
            "avg_dd": float(np.mean(p_dds)),
            "max_dd": float(max(p_dds)),
            "worst_day": float(min(all_day_vals)),
            "best_day": float(max(all_day_vals)),
        })
    tr_rows.sort(key=lambda x: x["avg_p"], reverse=True)
    for idx, tr in enumerate(tr_rows, 1):
        print(
            f"{idx:2d}. [{tr['prof']}] {tr['track']:<25s} | Pos={tr['pos_p']:2d}/{n_exps} (D:{tr['pos_d']:2d}/{n_exps}) "
            f"| AvgP={tr['avg_p']:+9.0f} AvgD={tr['avg_d']:+9.0f} | Min..Max={tr['min_p']:+8.0f}..{tr['max_p']:+8.0f} "
            f"| Grn={tr['avg_green']:.2f}/{n_days} | AvgDD={tr['avg_dd']:7.0f} MaxDD={tr['max_dd']:7.0f} | DayMin..Max={tr['worst_day']:+7.0f}..{tr['best_day']:+7.0f}"
        )


if __name__ == "__main__":
    summarize()
