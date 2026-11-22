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


if __name__ == "__main__":
    unittest.main()
