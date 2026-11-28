import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

import polars as pl

from web.batch_analytics import (
    build_batch_comparison_summary,
    compute_batch_report,
    export_batch_csv,
    export_batch_markdown,
)
from web.regime_classifier import SessionRegimeClassifier, classify_trade_window
from web.server import AnalyticsWebServer

IST = timezone(timedelta(hours=5, minutes=30))


def _make_synthetic_equity_df(date_str: str, bar_moves: list[float], include_nifty: bool = False) -> pl.DataFrame:
    """
    Construct synthetic 5-minute equity ticks for 25 F&O stocks on `date_str`.
    `bar_moves` gives the per-5m-bar proportional price change applied sequentially.
    """
    dt_open = datetime.strptime(f"{date_str} 09:15:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    t0 = dt_open.timestamp()

    tokens = []
    symbols = []
    ltps = []
    timestamps = []

    num_stocks = 25
    prices = [1000.0 + i * 20.0 for i in range(num_stocks)]

    for bar_idx, move in enumerate(bar_moves):
        ts_start = t0 + bar_idx * 300 + 5.0
        ts_end = t0 + bar_idx * 300 + 290.0
        for s_idx in range(num_stocks):
            sym = f"SYM{s_idx:02d}"
            tok = f"NSE_EQ|{s_idx}"
            open_p = prices[s_idx]
            close_p = open_p * (1.0 + move)
            prices[s_idx] = close_p

            tokens.extend([tok, tok])
            symbols.extend([sym, sym])
            ltps.extend([open_p, close_p])
            timestamps.extend([ts_start, ts_end])

    if include_nifty:
        n_price = 24000.0
        for bar_idx, move in enumerate(bar_moves):
            tokens.extend(["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"])
            symbols.extend(["Nifty 50", "Nifty 50"])
            close_n = n_price * (1.0 + move)
            ltps.extend([n_price, close_n])
            n_price = close_n
            timestamps.extend([t0 + bar_idx * 300 + 5.0, t0 + bar_idx * 300 + 290.0])

    return pl.DataFrame({
        "token": tokens,
        "symbol": symbols,
        "ltp": ltps,
        "exchange_timestamp": timestamps,
    })


def _make_mock_run(run_id: str, date_str: str, track_specs: dict, batch_id: str = "BATCH-1") -> dict:
    """
    Create a deterministic mock simulation run dict matching SimulationService structure.
    `track_specs`: {(mode, track): [(entry_hm, net_pnl, ret_pct), ...]}
    """
    matrix = {}
    details = {}
    dt_day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)

    for (mode, track), trades_list in track_specs.items():
        m_mat = matrix.setdefault(mode, {})
        m_det = details.setdefault(mode, {})

        built_trades = []
        gross = 0.0
        brok = 0.0
        net = 0.0
        wins = 0
        be = 0
        losses = 0

        for idx, (hhmm, tr_net, tr_ret) in enumerate(trades_list):
            hh, mm = map(int, hhmm.split(":"))
            entry_dt = dt_day.replace(hour=hh, minute=mm, second=0)
            tr_brok = 60.0
            tr_gross = tr_net + tr_brok
            gross += tr_gross
            brok += tr_brok
            net += tr_net
            if tr_ret > 2.5 or (tr_ret == 0.0 and tr_net > 0):
                wins += 1
            elif tr_net < 0:
                losses += 1
            else:
                be += 1
            built_trades.append({
                "trade_id": f"TRD_{run_id}_{idx}",
                "symbol": "RELIANCE",
                "net_pnl": tr_net,
                "gross_pnl": tr_gross,
                "brokerage": tr_brok,
                "return_pct": tr_ret,
                "entry_time": entry_dt.timestamp(),
                "entry_time_str": f"{hh:02d}:{mm:02d}:00",
                "exit_time": entry_dt.timestamp() + 300.0,
                "exit_time_str": f"{hh:02d}:{mm+5:02d}:00",
                "exit_reason": "TARGET" if tr_net > 0 else "SL",
            })

        total_tr = len(built_trades)
        m_mat[track] = {
            "trades": total_tr,
            "wins": wins,
            "breakevens": be,
            "losses": losses,
            "gross_pnl": gross,
            "total_brokerage": brok,
            "net_pnl": net,
            "win_rate": (wins / total_tr * 100.0) if total_tr > 0 else 0.0,
            "start_capital": 250000.0,
            "end_capital": 250000.0 + net,
            "return_pct": net / 250000.0 * 100.0,
        }
        m_det[track] = {"trades": built_trades, "curve": []}

    return {
        "id": run_id,
        "date": date_str,
        "batch_id": batch_id,
        "params": {},
        "results": {"date": date_str, "cached": True, "matrix": matrix, "details": details},
        "runtime": 1.0,
        "timestamp": "16:00:00",
    }


class TestRegimeClassifierAndBatchAnalytics(unittest.TestCase):
    def test_synthetic_basket_regime_classification_without_nifty(self):
        clf = SessionRegimeClassifier(recordings_dir=tempfile.gettempdir())

        # 1. Trend Up without any Nifty column
        df_up = _make_synthetic_equity_df("2026-11-16", [0.0005] * 25, include_nifty=False)
        res_up = clf.classify_dataframe(df_up, "2026-11-16")
        self.assertEqual(res_up["regime"], "TREND_UP")
        self.assertIsNone(res_up["metrics"]["nifty_return_pct"])
        self.assertEqual(res_up["metrics"]["symbol_count"], 25)
        self.assertGreater(res_up["metrics"]["basket_return_pct"], 1.0)
        self.assertGreater(res_up["metrics"]["efficiency_ratio"], 0.9)

        # 2. Trend Down with optional Nifty present
        df_dn = _make_synthetic_equity_df("2026-11-17", [-0.0005] * 25, include_nifty=True)
        res_dn = clf.classify_dataframe(df_dn, "2026-11-17")
        self.assertEqual(res_dn["regime"], "TREND_DOWN")
        self.assertIsNotNone(res_dn["metrics"]["nifty_return_pct"])
        self.assertLess(res_dn["metrics"]["basket_return_pct"], -1.0)

        # 3. Choppy / Range (alternating small ticks canceling out)
        chop_moves = [0.0004 if i % 2 == 0 else -0.0004 for i in range(40)]
        df_chop = _make_synthetic_equity_df("2026-11-18", chop_moves)
        res_chop = clf.classify_dataframe(df_chop, "2026-11-18")
        self.assertEqual(res_chop["regime"], "CHOPPY_RANGE")

        # 4. V-Reversal (morning pump + afternoon dump) -> 09:15..11:30 = 27 bars, 11:30..15:20 = 46 bars
        v_moves = ([0.0004] * 27) + ([-0.00025] * 40)
        df_v = _make_synthetic_equity_df("2026-11-20", v_moves)
        res_v = clf.classify_dataframe(df_v, "2026-11-20")
        self.assertEqual(res_v["regime"], "V_REVERSAL")

        # 5. High-Vol Thursday Expiry (2026-11-19 is Thursday)
        exp_moves = [0.0022 if i % 2 == 0 else -0.0022 for i in range(30)]
        df_exp = _make_synthetic_equity_df("2026-11-19", exp_moves)
        res_exp = clf.classify_dataframe(df_exp, "2026-11-19")
        self.assertEqual(res_exp["regime"], "HIGH_VOL_EXPIRY")
        self.assertTrue(res_exp["is_thursday"])

    def test_trade_window_classification(self):
        self.assertEqual(classify_trade_window(entry_time_str="09:45:12"), "OPENING")
        self.assertEqual(classify_trade_window(entry_time_str="12:15:00"), "MIDDAY")
        self.assertEqual(classify_trade_window(entry_time_str="14:40:00"), "CLOSING")

    def test_batch_report_metrics_curves_and_filters(self):
        run1 = _make_mock_run("RUN-1", "2026-11-16", {
            ("paper", "E10_W3_CAP3_ATM"): [("09:30", 5000.0, 12.0), ("10:15", 3000.0, 8.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("09:30", 4200.0, 10.0), ("10:15", 2600.0, 7.0)],
            ("paper", "fast_momentum_OTM"): [("14:10", -4000.0, -5.0)],
        })
        run2 = _make_mock_run("RUN-2", "2026-11-17", {
            ("paper", "E10_W3_CAP3_ATM"): [("10:00", -2000.0, -5.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("10:00", -2200.0, -5.5)],
            ("paper", "fast_momentum_OTM"): [("14:20", 1500.0, 4.0)],
        })
        run3 = _make_mock_run("RUN-3", "2026-11-18", {
            ("paper", "E10_W3_CAP3_ATM"): [("14:00", 4000.0, 9.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("14:00", 3600.0, 8.0)],
            ("paper", "fast_momentum_OTM"): [("09:40", 3500.0, 8.0)],
        })

        regimes = {
            "2026-11-16": {"date": "2026-11-16", "regime": "TREND_UP", "label": "Trend Up", "metrics": {"basket_return_pct": 1.2}},
            "2026-11-17": {"date": "2026-11-17", "regime": "CHOPPY_RANGE", "label": "Choppy / Range", "metrics": {"basket_return_pct": -0.1}},
            "2026-11-18": {"date": "2026-11-18", "regime": "TREND_UP", "label": "Trend Up", "metrics": {"basket_return_pct": 0.9}},
        }

        report = compute_batch_report(
            runs=[run3, run1, run2],  # intentionally unsorted input
            regimes_by_date=regimes,
            batch_id="BATCH-EXP",
            batch_label="Test Experiment",
            rank_by="net_pnl",
        )

        self.assertEqual(report["summary"]["total_sessions"], 3)
        self.assertEqual(report["summary"]["date_start"], "2026-11-16")
        self.assertEqual(report["summary"]["date_end"], "2026-11-18")

        lb = report["leaderboard"]
        top = lb[0]
        self.assertEqual(top["track"], "E10_W3_CAP3_ATM")
        self.assertEqual(top["mode"], "paper")
        self.assertAlmostEqual(top["net_pnl"], 10000.0)
        self.assertEqual(top["green_days"], 2)
        self.assertAlmostEqual(top["daily_win_rate_pct"], 66.7, places=1)
        # Losses = -2000, Wins = 5000+3000+4000 = 12000 -> PF = 6.0
        self.assertAlmostEqual(top["profit_factor"], 6.0, places=2)
        self.assertAlmostEqual(top["max_drawdown_inr"], 2000.0, places=2)
        self.assertAlmostEqual(top["calmar_ratio"], 5.0, places=2)
        # Paper net = 10000, Delayed net = 4200+2600-2200+3600 = 8200 -> 82.0% retention
        self.assertAlmostEqual(top["delayed_net_pnl"], 8200.0, places=2)
        self.assertAlmostEqual(top["delayed_retention_pct"], 82.0, places=1)
        self.assertEqual(top["latency_resilience"], "HIGH")

        # Verify chronological equity curve points & drawdowns
        curve = top["equity_curve"]
        self.assertEqual([pt["date"] for pt in curve], ["2026-11-16", "2026-11-17", "2026-11-18"])
        self.assertAlmostEqual(curve[0]["cum_net_pnl"], 8000.0)
        self.assertAlmostEqual(curve[1]["cum_net_pnl"], 6000.0)
        self.assertAlmostEqual(curve[1]["drawdown_inr"], 2000.0)
        self.assertAlmostEqual(curve[2]["cum_net_pnl"], 10000.0)
        self.assertAlmostEqual(curve[2]["drawdown_inr"], 0.0)

        # Verify session P&L attribution and morning-entered trade closed in afternoon stays OPENING
        self.assertEqual(report["sessions"][0]["top_track"], "E10_W3_CAP3_ATM")
        self.assertIsNotNone(report["sessions"][0]["paper_avg_net_pnl"])
        cross_run = _make_mock_run("RUN-X", "2026-11-16", {
            ("paper", "E10_W3_CAP3_ATM"): [("10:30", 2500.0, 6.0)],
        })
        cross_run["results"]["details"]["paper"]["E10_W3_CAP3_ATM"]["trades"][0]["exit_time_str"] = "14:30:00"
        cross_run["results"]["details"]["paper"]["E10_W3_CAP3_ATM"]["trades"][0]["exit_time"] += 4 * 3600.0
        rep_cross_open = compute_batch_report(runs=[cross_run], intraday_window="OPENING")
        rep_cross_close = compute_batch_report(runs=[cross_run], intraday_window="CLOSING")
        self.assertAlmostEqual(rep_cross_open["leaderboard"][0]["net_pnl"], 2500.0)
        self.assertAlmostEqual(rep_cross_close["leaderboard"][0]["net_pnl"], 0.0)

        # Spotlights & Rollups present
        self.assertEqual(report["spotlights"]["highest_net"]["track"], "E10_W3_CAP3_ATM")
        self.assertIn("max_green_days", report["spotlights"])
        self.assertEqual(report["spotlights"]["max_green_days"]["track"], "E10_W3_CAP3_ATM")
        self.assertIn("most_consistent", report["spotlights"])
        self.assertTrue(len(report["family_rollup"]) >= 2)
        self.assertTrue(len(report["intraday_breakdown"]) == 3)

        # Verify 45-degree linear slope consistency vs sawtooth curve with identical Net P&L
        run_s1 = _make_mock_run("RUN-S1", "2026-11-16", {
            ("paper", "linear_track"): [("09:30", 3000.0, 10.0)],
            ("paper", "jagged_track"): [("09:30", 8000.0, 20.0)],
        })
        run_s2 = _make_mock_run("RUN-S2", "2026-11-17", {
            ("paper", "linear_track"): [("09:30", 3000.0, 10.0)],
            ("paper", "jagged_track"): [("09:30", -5000.0, -10.0)],
        })
        run_s3 = _make_mock_run("RUN-S3", "2026-11-18", {
            ("paper", "linear_track"): [("09:30", 3000.0, 10.0)],
            ("paper", "jagged_track"): [("09:30", 6000.0, 15.0)],
        })
        rep_cons = compute_batch_report(runs=[run_s1, run_s2, run_s3], rank_by="consistency")
        lin_row = next(r for r in rep_cons["leaderboard"] if r["track"] == "linear_track")
        jag_row = next(r for r in rep_cons["leaderboard"] if r["track"] == "jagged_track")
        self.assertAlmostEqual(lin_row["net_pnl"], jag_row["net_pnl"])
        self.assertAlmostEqual(lin_row["linearity_score"], 100.0, places=1)
        self.assertGreater(lin_row["linearity_score"], jag_row["linearity_score"])
        self.assertEqual(rep_cons["spotlights"]["most_consistent"]["track"], "linear_track")
        self.assertEqual(rep_cons["leaderboard"][0]["track"], "linear_track")

        # Filter by Regime = TREND_UP (excludes 2026-11-17)
        rep_up = compute_batch_report(
            runs=[run1, run2, run3],
            regimes_by_date=regimes,
            regime_filter="TREND_UP",
        )
        self.assertEqual(rep_up["summary"]["filtered_sessions"], 2)
        e10_up = next(r for r in rep_up["leaderboard"] if r["track"] == "E10_W3_CAP3_ATM" and r["mode"] == "paper")
        self.assertAlmostEqual(e10_up["net_pnl"], 12000.0)

        # Filter by Intraday Window = OPENING
        rep_open = compute_batch_report(
            runs=[run1, run2, run3],
            regimes_by_date=regimes,
            intraday_window="OPENING",
        )
        e10_open = next(r for r in rep_open["leaderboard"] if r["track"] == "E10_W3_CAP3_ATM" and r["mode"] == "paper")
        self.assertEqual(e10_open["trades"], 3)  # 09:30, 10:15, 10:00 (excludes 14:00)
        self.assertAlmostEqual(e10_open["net_pnl"], 6000.0)

        # Markdown & CSV Exports & Multi-Batch comparison
        md = export_batch_markdown(report)
        csv_text = export_batch_csv(report)
        self.assertIn("E10_W3_CAP3_ATM", md)
        self.assertIn("profit_factor", csv_text)
        cmp_rows = build_batch_comparison_summary([report, rep_up])
        self.assertEqual(len(cmp_rows), 2)


class TestBatchWebEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_batch_web_")
        os.makedirs(os.path.join(self.test_dir, "2026-11-16"), exist_ok=True)
        # Write tiny synthetic parquet for regime endpoint test
        df_up = _make_synthetic_equity_df("2026-11-16", [0.0005] * 25, include_nifty=False)
        df_up.write_parquet(os.path.join(self.test_dir, "2026-11-16", "equity_ticks.parquet"))
        self.server_inst = AnalyticsWebServer(host="127.0.0.1", port=0, recordings_dir=self.test_dir)

    async def asyncTearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    async def test_batch_endpoints_lifecycle_and_export(self):
        from aiohttp.test_utils import make_mocked_request

        sim_svc = self.server_inst.simulation_service
        sim_svc.runs_history["RUN-A"] = _make_mock_run("RUN-A", "2026-11-16", {
            ("paper", "E10_W3_CAP3_ATM"): [("09:30", 5000.0, 12.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("09:30", 4500.0, 11.0)],
        }, batch_id="BATCH-ONE")
        sim_svc.runs_history["RUN-B"] = _make_mock_run("RUN-B", "2026-11-17", {
            ("paper", "E10_W3_CAP3_ATM"): [("10:00", 2000.0, 5.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("10:00", 1800.0, 4.5)],
        }, batch_id="BATCH-ONE")

        # 1. Create two batches so multi-batch comparison populates
        req_c1 = make_mocked_request("POST", "/api/sim/batches", app=self.server_inst.app)
        req_c1._read_bytes = json.dumps({
            "batch_id": "BATCH-ONE", "label": "Baseline Runs", "run_ids": ["RUN-A", "RUN-B"]
        }).encode("utf-8")
        resp_c1 = await self.server_inst.handle_sim_batches_create(req_c1)
        self.assertEqual(resp_c1.status, 200)

        req_c2 = make_mocked_request("POST", "/api/sim/batches", app=self.server_inst.app)
        req_c2._read_bytes = json.dumps({
            "batch_id": "BATCH-TWO", "label": "Single Run Subset", "run_ids": ["RUN-A"]
        }).encode("utf-8")
        resp_c2 = await self.server_inst.handle_sim_batches_create(req_c2)
        self.assertEqual(resp_c2.status, 200)

        # 2. List batches & assert multi-batch comparison present
        req_list = make_mocked_request("GET", "/api/sim/batches", app=self.server_inst.app)
        resp_list = await self.server_inst.handle_sim_batches_list(req_list)
        self.assertEqual(resp_list.status, 200)
        list_data = json.loads(resp_list.text)
        self.assertEqual(len(list_data["batches"]), 2)
        self.assertEqual(len(list_data["comparison"]), 2)

        # 3. Detail endpoint for BATCH-ONE + virtual __all__
        req_det = make_mocked_request(
            "GET", "/api/sim/batch/BATCH-ONE?rank_by=calmar",
            match_info={"batch_id": "BATCH-ONE"}, app=self.server_inst.app
        )
        resp_det = await self.server_inst.handle_sim_batch_detail(req_det)
        self.assertEqual(resp_det.status, 200)
        det_data = json.loads(resp_det.text)
        rep = det_data["report"]
        self.assertEqual(rep["batch_id"], "BATCH-ONE")
        self.assertEqual(rep["sessions"][0]["regime"], "TREND_UP")
        self.assertEqual(rep["sessions"][1]["regime"], "UNKNOWN")

        # 4. 404 on non-existent batch
        req_404 = make_mocked_request(
            "GET", "/api/sim/batch/NO_SUCH",
            match_info={"batch_id": "NO_SUCH"}, app=self.server_inst.app
        )
        resp_404 = await self.server_inst.handle_sim_batch_detail(req_404)
        self.assertEqual(resp_404.status, 404)

        # 5. Export Markdown & CSV
        req_csv = make_mocked_request(
            "GET", "/api/sim/batch/BATCH-ONE/export?format=csv",
            match_info={"batch_id": "BATCH-ONE"}, app=self.server_inst.app
        )
        resp_csv = await self.server_inst.handle_sim_batch_export(req_csv)
        self.assertEqual(resp_csv.status, 200)
        self.assertIn("E10_W3_CAP3_ATM", resp_csv.text)

        req_md = make_mocked_request(
            "GET", "/api/sim/batch/__all__/export?format=md",
            match_info={"batch_id": "__all__"}, app=self.server_inst.app
        )
        resp_md = await self.server_inst.handle_sim_batch_export(req_md)
        self.assertEqual(resp_md.status, 200)
        self.assertIn("# Batch Simulation Report", resp_md.text)

        # 6. Regimes endpoint
        req_reg = make_mocked_request("GET", "/api/sim/regimes?dates=2026-11-16", app=self.server_inst.app)
        resp_reg = await self.server_inst.handle_sim_regimes(req_reg)
        self.assertEqual(resp_reg.status, 200)
        reg_data = json.loads(resp_reg.text)
        self.assertEqual(reg_data["regimes"][0]["regime"], "TREND_UP")

        # 7. Index HTML exposes dual 1:1 batch charts, unified leaderboard pills, and session P&L table
        req_idx = make_mocked_request("GET", "/", app=self.server_inst.app)
        resp_idx = await self.server_inst.handle_index(req_idx)
        self.assertEqual(resp_idx.status, 200)
        self.assertIn("br-winners-canvas", resp_idx.text)
        self.assertIn("br-equity-canvas", resp_idx.text)
        self.assertIn("br-rankings-metric-pills", resp_idx.text)
        self.assertIn("br-sessions-tbody", resp_idx.text)

        # 8. Two batches over the same date range remain strictly isolated
        sim_svc.runs_history["RUN-C"] = _make_mock_run("RUN-C", "2026-11-16", {
            ("paper", "E10_W3_CAP3_ATM"): [("09:30", 9000.0, 20.0)],
            ("paper_delayed", "E10_W3_CAP3_ATM"): [("09:30", 8500.0, 19.0)],
        }, batch_id="BATCH-THREE")
        sim_svc.runs_history["RUN-UNBATCHED"] = _make_mock_run("RUN-UNBATCHED", "2026-11-18", {
            ("paper", "E10_W3_CAP3_ATM"): [("10:30", 1200.0, 3.0)],
        }, batch_id="")
        sim_svc._register_run_in_batch("BATCH-THREE", "RUN-C", label="Override Day 1")
        snap = sim_svc.create_or_update_batch(batch_id="BATCH-SNAP", label="Only Unbatched", run_ids=None)
        self.assertEqual(snap["run_ids"], ["RUN-UNBATCHED"])

        rep_b1 = sim_svc.get_batch_report("BATCH-ONE")
        rep_b3 = sim_svc.get_batch_report("BATCH-THREE")
        self.assertEqual(len(rep_b1["sessions"]), 2)
        self.assertEqual(len(rep_b3["sessions"]), 1)
        self.assertEqual(rep_b1["sessions"][0]["run_id"], "RUN-A")
        self.assertEqual(rep_b3["sessions"][0]["run_id"], "RUN-C")

        # 9. Clear endpoint wipes batches & regime cache
        req_clr = make_mocked_request("POST", "/api/sim/clear", app=self.server_inst.app)
        await self.server_inst.handle_sim_clear(req_clr)
        self.assertEqual(len(sim_svc.batches_history), 0)
        self.assertEqual(len(sim_svc.regime_classifier.cache), 0)


if __name__ == "__main__":
    unittest.main()
