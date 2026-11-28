"""
Unit Tests for Columnar Simulation Engine.
Verifies exact-timestamp ASOF simulation and side-by-side mode execution.
"""
import os
import shutil
import tempfile
import unittest
import pyarrow as pa
import pyarrow.parquet as pq

from core.recorder.market_recorder import TICK_SCHEMA
from simulation.columnar_sim_engine import ColumnarSimEngine


class TestColumnarSimEngine(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_sim_")
        self.parquet_path = os.path.join(self.test_dir, "test_session.parquet")

        # Create tick dataset for an option contract
        # 12 ticks spanning 120 seconds
        token = "NSE:NIFTY26NOV24000CE"
        ticks = []
        base_ts = 1700000000.0

        for i in range(15):
            ts = base_ts + i * 10.0  # 0s, 10s, 20s, ... 140s
            ltp = 100.0 + (i * 2.0)  # Ascending: 100, 102, 104 ... 128 (Target reached)
            ticks.append({
                "token": token,
                "symbol": "NIFTY",
                "ltp": ltp,
                "volume": 500,
                "bid": ltp - 0.20,
                "ask": ltp + 0.20,
                "bid_qty": 300,
                "ask_qty": 300,
                "exchange_timestamp": ts,
                "received_timestamp": ts + 0.02,
            })

        import polars as pl
        df = pl.DataFrame(ticks)
        df.write_parquet(self.parquet_path)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_columnar_simulation_paper_and_delayed(self):
        sim = ColumnarSimEngine(
            data_path=self.parquet_path,
            tracks=["fast_momentum"],
            delay_sec=30.0,
        )

        results = sim.run_simulation()

        self.assertIn("paper", results)
        self.assertIn("paper_delayed", results)

        # Fast momentum in paper mode entered at t=0s, ask=100.20
        # Target was 100.20 * 1.12 = 112.22
        # Ticks reached bid >= 112.22 at i=7 (ts=70s, ltp=114, bid=113.80)
        paper_fm = results["paper"]["fast_momentum"]
        self.assertEqual(paper_fm["trades"], 1)
        self.assertEqual(paper_fm["wins"], 1)
        self.assertGreater(paper_fm["net_pnl"], 0.0)

        # Fast momentum in delayed mode entered at t=30s (i=3, ask=106.20)
        delayed_fm = results["paper_delayed"]["fast_momentum"]
        self.assertEqual(delayed_fm["trades"], 1)

        summary_text = sim.print_summary(results)
        self.assertIn("COLUMNAR SIMULATION ENGINE", summary_text)
        self.assertIn("fast_momentum", summary_text)

    def test_columnar_simulation_dual_stream_directory(self):
        # Test loading a folder containing both equity ticks and option depth parquet files
        import polars as pl
        dual_dir = os.path.join(self.test_dir, "dual_recordings")
        os.makedirs(dual_dir, exist_ok=True)

        eq_ticks = [{
            "token": "NSE_EQ|INE002A01018",
            "symbol": "RELIANCE",
            "ltp": 2500.0 + i,
            "volume": 100 * (i + 1),
            "bid": 2499.5,
            "ask": 2500.5,
            "bid_qty": 50,
            "ask_qty": 60,
            "exchange_timestamp": 1700000000.0 + i,
            "received_timestamp": 1700000000.05 + i,
        } for i in range(5)]
        pl.DataFrame(eq_ticks).write_parquet(os.path.join(dual_dir, "equity_ticks_1700000000_0001.parquet"))

        opt_ticks = [{
            "token": "NSE:NIFTY26NOV24000CE",
            "symbol": "NIFTY",
            "ltp": 100.0 + (i * 2.0),
            "volume": 500,
            "bid": 99.80 + (i * 2.0),
            "ask": 100.20 + (i * 2.0),
            "bid_qty": 300,
            "ask_qty": 300,
            "exchange_timestamp": 1700000000.0 + (i * 10.0),
            "received_timestamp": 1700000000.02 + (i * 10.0),
        } for i in range(12)]
        pl.DataFrame(opt_ticks).write_parquet(os.path.join(dual_dir, "option_depth_1700000000_0001.parquet"))

        sim = ColumnarSimEngine(
            data_path=dual_dir,
            tracks=["fast_momentum"],
            delay_sec=30.0,
        )
        results = sim.run_simulation()
        self.assertIn("paper", results)
        self.assertIn("paper_delayed", results)
        self.assertEqual(results["paper"]["fast_momentum"]["trades"], 1)

    def test_option_depth_exact_lookup_and_catalog(self):
        sim = ColumnarSimEngine(data_path=self.parquet_path)
        sim.load_data()

        # Check options catalog built
        self.assertIn("NIFTY", sim.options_catalog)
        self.assertIn("CE", sim.options_catalog["NIFTY"])
        self.assertEqual(len(sim.options_catalog["NIFTY"]["CE"]), 1)
        self.assertEqual(sim.options_catalog["NIFTY"]["CE"][0][0], 24000.0)

        # Test exact-timestamp point-in-time lookup
        # Ticks are at ts = 1700000000 + i * 10
        # Query at ts = 1700000025 -> should match tick at i=2 (ts=1700000020, ltp=104, ask=104.20)
        q = sim.get_option_quote("NSE:NIFTY26NOV24000CE", timestamp=1700000025.0)
        self.assertIsNotNone(q)
        self.assertEqual(q["ltp"], 104.0)
        self.assertEqual(q["ask"], 104.20)
        self.assertEqual(q["bid"], 103.80)

        # Query at ts = 1700000085 -> should match tick at i=8 (ts=1700000080, ltp=116, ask=116.20)
        q2 = sim.get_option_quote("NSE:NIFTY26NOV24000CE", timestamp=1700000085.0)
        self.assertIsNotNone(q2)
        self.assertEqual(q2["ltp"], 116.0)
        self.assertEqual(q2["ask"], 116.20)

    def test_atm_proximity_guard_rejects_distant_strikes(self):
        """Verify resolve_sim_strikes rejects distant strikes (e.g. HAVELLS at 1162 with only 1110 recorded)."""
        sim = ColumnarSimEngine(data_path=self.parquet_path)
        # Catalog only has 1100 and 1110 strikes
        sim.options_catalog["HAVELLS"] = {
            "CE": [(1100.0, "TOK_1100"), (1110.0, "TOK_1110")],
            "PE": [(1100.0, "TOK_1100P"), (1110.0, "TOK_1110P")],
        }
        # Spot / trigger is 1162.0 (nearest strike 1110 is 52 rupees away > 2%)
        resolved = sim.resolve_sim_strikes("HAVELLS", "BULL", trigger_price=1162.0, timestamp=1000.0)
        self.assertEqual(resolved, {}, "ATM proximity guard must reject distant strikes > 2% from spot")

    def test_win_rate_and_breakeven_classification(self):
        """Verify > 2.5% is counted as win, 0-2.5% as breakeven, and < 0% as loss."""
        from common.interfaces import TradeState
        sim = ColumnarSimEngine(data_path=self.parquet_path, modes=["paper"], tracks=["trend_pullback"])

        # Trade 1: +10% gain (e.g. trailing SL locked in profit) -> WIN
        t1 = TradeState(
            trade_id="T1", mode="paper", track="trend_pullback", symbol="S1", token="TOK1",
            option_type="CE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1000.0,
            exit_price=110.0, exit_time=1060.0, status="CLOSED", realized_pnl=100.0,
            target_price=125.0, stop_loss=105.0, highest_price=115.0, current_bid=110.0,
        )
        # Trade 2: +1.5% gain (breakeven / minor win) -> BREAKEVEN
        t2 = TradeState(
            trade_id="T2", mode="paper", track="trend_pullback", symbol="S2", token="TOK2",
            option_type="CE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1000.0,
            exit_price=101.5, exit_time=1060.0, status="CLOSED", realized_pnl=15.0,
            target_price=125.0, stop_loss=100.0, highest_price=103.0, current_bid=101.5,
        )
        # Trade 3: -5.0% loss -> LOSS
        t3 = TradeState(
            trade_id="T3", mode="paper", track="trend_pullback", symbol="S3", token="TOK3",
            option_type="CE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1000.0,
            exit_price=95.0, exit_time=1060.0, status="CLOSED", realized_pnl=-50.0,
            target_price=125.0, stop_loss=95.0, highest_price=100.0, current_bid=95.0,
        )

        sim.closed_trades["paper"] = [t1, t2, t3]
        results = sim._build_results()
        tp_res = results["paper"]["trend_pullback"]

        self.assertEqual(tp_res["trades"], 3)
        self.assertEqual(tp_res["wins"], 1, "Only > 2.5% should be counted as win")
        self.assertEqual(tp_res["breakevens"], 1, "0-2.5% should be counted as breakeven")
        self.assertEqual(tp_res["losses"], 1, "< 0% should be counted as loss")
        self.assertAlmostEqual(tp_res["win_rate"], 33.33, places=1)

    def test_brokerage_deduction_and_journal_formatting(self):
        """Verify brokerage deduction per order, net PnL, journal entry-time sorting, and trigger column."""
        from common.interfaces import TradeState
        sim = ColumnarSimEngine(
            data_path=self.parquet_path,
            modes=["paper"],
            tracks=["trend_pullback"],
            brokerage_per_order=30.0,
        )

        # Trade 1: entered at t=1050 (later), Bull Flag
        t1 = TradeState(
            trade_id="T1", mode="paper", track="trend_pullback", symbol="S1", token="TOK1",
            option_type="CE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1050.0,
            exit_price=110.0, exit_time=1060.0, status="CLOSED", realized_pnl=100.0,
            target_price=125.0, stop_loss=105.0, highest_price=115.0, current_bid=110.0,
            metadata={"archetype": "FLAG_BULL_BREAKOUT"},
        )
        # Trade 2: entered at t=1010 (earliest), Pure Breakdown
        t2 = TradeState(
            trade_id="T2", mode="paper", track="trend_pullback", symbol="S2", token="TOK2",
            option_type="PE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1010.0,
            exit_price=101.5, exit_time=1070.0, status="CLOSED", realized_pnl=15.0,
            target_price=125.0, stop_loss=100.0, highest_price=103.0, current_bid=101.5,
            metadata={"archetype": "PURE_BEAR_BREAKDOWN"},
        )
        # Trade 3: entered at t=1030 (middle), Bear Flag
        t3 = TradeState(
            trade_id="T3", mode="paper", track="trend_pullback", symbol="S3", token="TOK3",
            option_type="PE", strike=100.0, lot_size=10, quantity=10,
            entry_ask=100.0, entry_price=100.0, entry_time=1030.0,
            exit_price=95.0, exit_time=1080.0, status="CLOSED", realized_pnl=-50.0,
            target_price=125.0, stop_loss=95.0, highest_price=100.0, current_bid=95.0,
            metadata={"archetype": "FLAG_BEAR_BREAKDOWN"},
        )

        # Append in non-chronological order of entry (T1 exit 1060, T2 exit 1070, T3 exit 1080)
        sim.closed_trades["paper"] = [t1, t2, t3]
        results = sim._build_results()
        tp_res = results["paper"]["trend_pullback"]

        # 3 trades * (2 * 30.0) = 180.0 total brokerage
        self.assertEqual(tp_res["gross_pnl"], 65.0)
        self.assertEqual(tp_res["total_brokerage"], 180.0)
        self.assertEqual(tp_res["net_pnl"], -115.0)
        self.assertEqual(tp_res["end_capital"], 250000.0 - 115.0)

        # Verify summary output table has all required columns
        summary_out = sim.print_summary(results)
        self.assertIn("GROSS PNL", summary_out)
        self.assertIn("BROKERAGE", summary_out)
        self.assertIn("NET PNL (INR)", summary_out)
        self.assertIn("₹180.00", summary_out)

        # Verify journal output
        journal_out = sim.print_journal()
        self.assertIn("TRIGGER", journal_out)
        self.assertIn("Flag Breakout", journal_out)
        self.assertIn("Pure Breakdown", journal_out)
        self.assertIn("Flag Breakdown", journal_out)

        # Verify chronological sorting by entry_time:
        # T2 (entry 1010, index 1 in table) must appear before T3 (entry 1030, index 2), which is before T1 (entry 1050, index 3)
        pos_t2 = journal_out.find("S2")
        pos_t3 = journal_out.find("S3")
        pos_t1 = journal_out.find("S1")
        self.assertGreater(pos_t3, pos_t2, "T2 (entry 1010) must appear before T3 (entry 1030)")
        self.assertGreater(pos_t1, pos_t3, "T3 (entry 1030) must appear before T1 (entry 1050)")

    def test_trade_underlying_entry_price_captured(self):
        """Verify TradeState captures underlying_entry_price from opportunity trigger_price."""
        from common.tracks.base_track import BaseTrack
        from common.interfaces import ExecutionContext, OrderRequest
        from core.common.ipc_schemas import ActionableOpportunity

        class MockCtx(ExecutionContext):
            @property
            def mode(self) -> str:
                return "paper"
            def is_frozen(self) -> bool:
                return False
            def submit_order(self, request: OrderRequest) -> None:
                pass
            def cancel_order(self, order_id: str) -> None:
                pass
            def record_trade_event(self, event_type: str, data: dict) -> None:
                pass

        class DummyTrack(BaseTrack):
            def evaluate_exit_rules(self, trade):
                pass

        track = DummyTrack("test_track", MockCtx(), target_pct=0.15, sl_pct=0.05, preferred_mode="ATM")
        track.allocated_capital = 100000.0

        opp = ActionableOpportunity(
            opp_id="OPP_TEST_1",
            alert_id="ALT_TEST_1",
            symbol="ANGELONE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=352.65,
            strikes={
                "ATM": {
                    "token": "NSE:70289",
                    "strike": 360.0,
                    "option_type": "CE",
                    "bid": 5.0,
                    "ask": 5.1,
                    "lot_size": 100,
                }
            },
            timestamp=1794888045.0,
        )

        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.underlying_entry_price, 352.65)
        self.assertEqual(trade.metadata.get("underlying_entry_price"), 352.65)
        self.assertEqual(trade.metadata.get("trigger_price"), 352.65)

        # Check serialization
        t_dict = trade.to_dict()
        self.assertEqual(t_dict.get("underlying_entry_price"), 352.65)

    def test_sim_circuit_breaker_and_spread_gate_from_strategy_config(self):
        """Verify ColumnarSimEngine reads enable_circuit_breaker & max_spread_pct from strategy config."""
        cfg_default = {
            "strategies": {
                "momentum": {
                    "risk": {
                        "enable_circuit_breaker": False,
                        "daily_loss_limit_inr": 5000.0,
                        "max_spread_pct": 0.03,
                    }
                }
            }
        }
        sim_off = ColumnarSimEngine(
            data_path=self.parquet_path,
            modes=["paper"],
            tracks=["fast_momentum"],
            strategy_cfg=cfg_default,
        )
        self.assertFalse(sim_off.enable_circuit_breaker)
        self.assertEqual(sim_off.max_spread_pct, 0.03)

        # Configure spread gate of 3%: ₹0.50 on ₹10.00 ask (5%) must be rejected identically to RMS
        sim_off.options_catalog["TESTSYM"] = {"CE": [(100.0, "NSE:TESTSYM26NOV100CE")], "PE": []}
        sim_off.options_by_token["NSE:TESTSYM26NOV100CE"] = {
            "timestamps": __import__("numpy").array([1000.0]),
            "bids": __import__("numpy").array([9.50]),
            "asks": __import__("numpy").array([10.00]),
            "ltps": __import__("numpy").array([9.75]),
            "bid_qtys": __import__("numpy").array([100]),
            "ask_qtys": __import__("numpy").array([100]),
        }
        self.assertEqual(sim_off.resolve_sim_strikes("TESTSYM", "BULL", 100.0, 1005.0), {})

        # Configure custom 6% spread gate -> ₹0.50 on ₹10.00 (5%) is accepted
        cfg_wide = {
            "strategies": {
                "momentum": {
                    "risk": {
                        "enable_circuit_breaker": True,
                        "daily_loss_limit_inr": 5000.0,
                        "max_spread_pct": 0.06,
                    }
                }
            }
        }
        sim_on = ColumnarSimEngine(
            data_path=self.parquet_path,
            modes=["paper"],
            tracks=["fast_momentum"],
            strategy_cfg=cfg_wide,
        )
        sim_on.options_catalog = sim_off.options_catalog
        sim_on.options_by_token = sim_off.options_by_token
        resolved = sim_on.resolve_sim_strikes("TESTSYM", "BULL", 100.0, 1005.0)
        self.assertIn("ATM", resolved)

        from simulation.columnar_sim_engine import SimExecutionContext
        ctx_cb = SimExecutionContext(mode="paper", daily_loss_limit_inr=5000.0, enable_circuit_breaker=True)
        self.assertFalse(ctx_cb.is_frozen())
        ctx_cb.record_trade_event("TRADE_EXIT", {"realized_pnl": -6000.0})
        self.assertTrue(ctx_cb.is_frozen())

    def test_tick_by_tick_replay_catches_intra_interval_dip(self):
        """Verify ColumnarSimEngine processes every option tick (including intra-0.5s spikes/dips)."""
        import polars as pl
        from datetime import datetime
        from core.common.time_utils import IST
        from common.interfaces import TradeState

        tick_dir = os.path.join(self.test_dir, "tick_fidelity")
        os.makedirs(tick_dir, exist_ok=True)
        # Create >10 equity ticks spanning 09:30..09:32 with a pole+flag breakout at 09:30:00..09:31:00
        # Wait: drive direct option tick replay verification inside run_simulation by testing that
        # an option tick at t + 0.15s triggers SL even when the next equity tick is at t + 1.0s (and t + 0.5s quote recovered).
        t0 = datetime(2026, 11, 26, 10, 0, 0, tzinfo=IST).timestamp()
        # 12 equity ticks spaced by 1.0s (no equity ticks at 0.15s)
        eq_rows = []
        for i in range(15):
            ts = t0 + i * 1.0
            # Build clean bull flag on bar 0..1? Simpler: test option replay helper directly? No, test end-to-end with synthetic candle breakout!
            eq_rows.append({
                "token": "NSE:INFY",
                "symbol": "INFY",
                "ltp": 1500.0 + (i * 0.1),
                "volume": 1000 * (i + 1),
                "bid": 1500.0,
                "ask": 1500.5,
                "bid_qty": 100,
                "ask_qty": 100,
                "exchange_timestamp": ts,
                "received_timestamp": ts,
            })
        # Option depth has a brief dip to 90.0 (below 95.0 SL on 100.0 entry) at t0+2.15s, then recovers to 101.0 by t0+2.45s!
        # Old 0.5s sampler at t0+2.0s / t0+2.5s / t0+3.0s would completely miss the 90.0 stop-loss at t0+2.15s!
        opt_rows = [
            {"token": "NSE:INFY26NOV1500CE", "symbol": "INFY26NOV1500CE", "ltp": 100.0, "bid": 99.5, "ask": 100.0, "bid_qty": 100, "ask_qty": 100, "exchange_timestamp": t0, "received_timestamp": t0},
            {"token": "NSE:INFY26NOV1500CE", "symbol": "INFY26NOV1500CE", "ltp": 90.0, "bid": 90.0, "ask": 90.5, "bid_qty": 100, "ask_qty": 100, "exchange_timestamp": t0 + 2.15, "received_timestamp": t0 + 2.15},
            {"token": "NSE:INFY26NOV1500CE", "symbol": "INFY26NOV1500CE", "ltp": 101.0, "bid": 101.0, "ask": 101.5, "bid_qty": 100, "ask_qty": 100, "exchange_timestamp": t0 + 2.45, "received_timestamp": t0 + 2.45},
        ]
        pl.DataFrame(eq_rows).write_parquet(os.path.join(tick_dir, "equity_ticks_1.parquet"))
        pl.DataFrame(opt_rows).write_parquet(os.path.join(tick_dir, "option_depth_1.parquet"))

        from unittest.mock import patch
        from core.common.ipc_schemas import SetupAlert

        sim = ColumnarSimEngine(
            data_path=tick_dir,
            modes=["paper"],
            tracks=["fast_momentum_ATM"],
            shm_name="tmp_test_tick_replay",
        )
        emitted = [False]
        def fake_eval(now_ts: float):
            if not emitted[0] and now_ts >= t0 + 1.0:
                emitted[0] = True
                return [SetupAlert(
                    alert_id="ALT_DIP_1",
                    symbol="INFY",
                    direction="BULL",
                    archetype="FLAG_BULL_BREAKOUT",
                    trigger_price=1500.0,
                    rvol=3.0,
                    clv=0.9,
                    atr=10.0,
                    flag_high=1500.0,
                    flag_low=1490.0,
                    timestamp=now_ts,
                )]
            return []

        with patch("strategies.momentum.scoring_engine.ScoringEngine3.evaluate_universe", side_effect=fake_eval):
            results = sim.run_simulation()

        closed = sim.closed_trades["paper"]
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].exit_time, t0 + 2.15, places=3)
        self.assertAlmostEqual(closed[0].exit_price, 90.0, places=2)
        self.assertEqual(closed[0].exit_reason, "SL_HIT")

    def test_e10_sim_engine_defaults_and_atr_spread_gate(self):
        """Verify ColumnarSimEngine reads E10_W3_CAP3 defaults from strategies.json and enforces ATR/Spread gate."""
        sim = ColumnarSimEngine(
            data_path=self.parquet_path,
            modes=["paper"],
            tracks=["fast_momentum_ATM"],
        )
        self.assertEqual(sim.min_candle_elapsed_sec, 15.0)
        self.assertAlmostEqual(sim.flag_pole_buffer_pct, 0.25, places=6)
        self.assertAlmostEqual(sim.pure_3bar_expansion_pct, 0.0045, places=6)
        self.assertEqual(sim.max_alerts_per_symbol_per_day, 3)
        self.assertTrue(sim.atr_spread_gate_enabled)
        self.assertAlmostEqual(sim.atr_spread_gate_mult, 2.0, places=6)
        self.assertNotIn("gamma_runner_OTM2", ColumnarSimEngine(data_path=self.parquet_path).tracks)

        # Spot=1000, ask=20, bid=19.6 (2% spread < 3% cap).
        # With quiet ATR=1.0 -> exp_opt_move=2.5% < 2 * 2% -> rejected when atr provided!
        sim.options_catalog["GATE_SYM"] = {"CE": [(1000.0, "NSE:GATESYM26NOV1000CE")], "PE": []}
        sim.options_by_token["NSE:GATESYM26NOV1000CE"] = {
            "timestamps": __import__("numpy").array([1000.0]),
            "bids": __import__("numpy").array([19.60]),
            "asks": __import__("numpy").array([20.00]),
            "ltps": __import__("numpy").array([19.80]),
            "bid_qtys": __import__("numpy").array([100]),
            "ask_qtys": __import__("numpy").array([100]),
        }
        self.assertEqual(sim.resolve_sim_strikes("GATE_SYM", "BULL", 1000.0, 1005.0, atr=1.0), {})
        self.assertIn("ATM", sim.resolve_sim_strikes("GATE_SYM", "BULL", 1000.0, 1005.0, atr=4.0))

    def test_manifest_options_map_preserves_unrecorded_middle_strikes(self):
        """Verify ColumnarSimEngine uses manifest options_map so missing middle strikes do not shift ITM/OTM roles."""
        import json
        import polars as pl

        sim_dir = os.path.join(self.test_dir, "manifest_grid")
        os.makedirs(sim_dir, exist_ok=True)
        manifest = {
            "options_map": {
                "TESTGRID": {
                    "strikes": {
                        "10300.0": {"CE": "NSE_FO|9001"},
                        "10400.0": {"CE": "NSE_FO|9002"},
                        "10500.0": {"CE": "NSE_FO|9003"},
                    }
                }
            }
        }
        with open(os.path.join(sim_dir, "subscription_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        # Only 10300 CE and 10500 CE were recorded in option_depth; 10400 CE (ATM) has no ticks
        opt_rows = [
            {"token": "NSE:9001", "symbol": "TESTGRID26NOV10300CE", "ltp": 520.0, "volume": 100, "bid": 518.0, "ask": 520.0, "bid_qty": 50, "ask_qty": 50, "exchange_timestamp": 1000.0, "received_timestamp": 1000.0},
            {"token": "NSE:9003", "symbol": "TESTGRID26NOV10500CE", "ltp": 425.0, "volume": 100, "bid": 423.0, "ask": 425.0, "bid_qty": 50, "ask_qty": 50, "exchange_timestamp": 1000.0, "received_timestamp": 1000.0},
        ]
        pl.DataFrame(opt_rows).write_parquet(os.path.join(sim_dir, "option_depth.parquet"))

        sim = ColumnarSimEngine(data_path=sim_dir, modes=["paper"], tracks=["fast_momentum_ATM"])
        sim.load_data()
        resolved = sim.resolve_sim_strikes("TESTGRID", "BULL", 10408.0, 1001.0, atr=46.0)
        self.assertNotIn("ATM", resolved)
        self.assertEqual(resolved["ITM1"]["token"], "NSE:9001")
        self.assertEqual(resolved["OTM1"]["token"], "NSE:9003")

    def test_prune_old_preparation_files_preserves_manifests(self):
        """Verify prepare cleanup keeps dated index and subscription_manifest files while pruning old CSVs."""
        from core.instruments.cleanup import prune_old_preparation_files

        prep_dir = os.path.join(self.test_dir, "prep_data")
        os.makedirs(prep_dir, exist_ok=True)
        for fname in [
            "index_20261120.json",
            "subscription_manifest_20261120.json",
            "subscription_manifest_mcx_20261120.json",
            "instrument_20261120.csv",
            "filtered_20261120.csv",
            "filtered_20261128.csv",
        ]:
            with open(os.path.join(prep_dir, fname), "w", encoding="utf-8") as f:
                f.write("{}")

        pruned = prune_old_preparation_files(keep_session_date="20261128", data_dir=prep_dir, silent=True)
        pruned_names = {os.path.basename(p) for p, _ in pruned}
        self.assertEqual(pruned_names, {"instrument_20261120.csv", "filtered_20261120.csv"})
        self.assertTrue(os.path.exists(os.path.join(prep_dir, "index_20261120.json")))
        self.assertTrue(os.path.exists(os.path.join(prep_dir, "subscription_manifest_20261120.json")))
        self.assertTrue(os.path.exists(os.path.join(prep_dir, "subscription_manifest_mcx_20261120.json")))
        self.assertTrue(os.path.exists(os.path.join(prep_dir, "filtered_20261128.csv")))


if __name__ == "__main__":
    unittest.main()
