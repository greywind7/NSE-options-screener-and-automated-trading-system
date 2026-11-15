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

        for i in range(12):
            ts = base_ts + i * 10.0  # 0s, 10s, 20s, ... 110s
            ltp = 100.0 + (i * 2.0)  # Ascending: 100, 102, 104 ... 122 (Target is 112 -> reached at i=6)
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


if __name__ == "__main__":
    unittest.main()
