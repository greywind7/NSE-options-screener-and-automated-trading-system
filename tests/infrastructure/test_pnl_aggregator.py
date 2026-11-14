#!/usr/bin/env python3
"""
tests/infrastructure/test_pnl_aggregator.py
===========================================
Unit tests for Unified P&L Aggregator & End-of-Day Report Generator.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.pnl.pnl_aggregator import PnLAggregator
from infrastructure.pnl.report_generator import DailyReportGenerator, render_ascii_line_graph


class TestPnLAggregator(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.record_dir = os.path.join(self.test_dir, "recordings")
        self.log_dir = os.path.join(self.test_dir, "logs")
        self.session_date = "20261107"
        self.aggregator = PnLAggregator(
            session_date=self.session_date,
            record_dir=self.record_dir,
            log_dir=self.log_dir,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_trade_open_and_exit(self):
        # 1. Record Trade Open
        t_open = self.aggregator.record_trade_open(
            trade_id="TRD-001",
            strategy="momentum",
            track="TRAIL_SL_ATM",
            mode="paper",
            symbol="RELIANCE",
            contract_key="NSE_FO|12345",
            contract_symbol="RELIANCE26NOV2500CE",
            direction="BULL",
            quantity=250,
            entry_price=35.0,
            entry_time=1000.0,
        )
        self.assertEqual(t_open["status"], "OPEN")
        self.assertEqual(t_open["trade_id"], "TRD-001")

        # 2. Record Trade Exit
        t_exit = self.aggregator.record_trade_exit(
            trade_id="TRD-001",
            exit_price=42.0,
            exit_time=1900.0,
            exit_reason="Target Hit",
            brokerage=60.0,
        )
        self.assertEqual(t_exit["status"], "CLOSED")
        self.assertEqual(t_exit["gross_pnl"], (42.0 - 35.0) * 250)  # 1750.0
        self.assertEqual(t_exit["net_pnl"], 1750.0 - 60.0)  # 1690.0

        # Verify file existence and single-event disk logging (no twice-logging of OPEN)
        rec_file = os.path.join(self.record_dir, self.session_date, "trades.jsonl")
        self.assertTrue(os.path.exists(rec_file))
        with open(rec_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "EXIT")
        self.assertEqual(lines[0]["trade_id"], "TRD-001")
        self.assertEqual(lines[0]["entry_price"], 35.0)
        self.assertEqual(lines[0]["exit_price"], 42.0)

    def test_compute_summary_metrics(self):
        # Add 1 winning trade and 1 losing trade
        self.aggregator.record_trade_open("TRD-W", "momentum", "ATM", "paper", "INFY", "K1", "S1", "BULL", 100, 20.0, 1000.0)
        self.aggregator.record_trade_exit("TRD-W", 25.0, 1200.0, "TGT", brokerage=60.0)  # Net: 500 - 60 = +440

        self.aggregator.record_trade_open("TRD-L", "momentum", "ITM", "paper_delayed", "TCS", "K2", "S2", "BEAR", 100, 30.0, 1100.0)
        self.aggregator.record_trade_exit("TRD-L", 27.0, 1300.0, "SL", brokerage=60.0)  # Net: -300 - 60 = -360

        summary = self.aggregator.compute_summary()
        self.assertEqual(summary["total_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["win_rate"], 50.0)
        self.assertEqual(summary["net_pnl"], 440.0 - 360.0)  # +80.0
        self.assertEqual(summary["brokerage"], 120.0)

        # Mode filter summary
        paper_summary = self.aggregator.compute_summary(mode="paper")
        self.assertEqual(paper_summary["total_trades"], 1)
        self.assertEqual(paper_summary["net_pnl"], 440.0)

    def test_pnl_timeseries(self):
        self.aggregator.record_trade_open("T1", "momentum", "ATM", "paper", "INFY", "K1", "S1", "BULL", 100, 10.0, 1000.0)
        self.aggregator.record_trade_exit("T1", 15.0, 1100.0, "TGT", net_pnl=440.0)

        self.aggregator.record_trade_open("T2", "momentum", "ATM", "paper", "INFY", "K1", "S1", "BULL", 100, 10.0, 1200.0)
        self.aggregator.record_trade_exit("T2", 12.0, 1300.0, "TGT", net_pnl=140.0)

        ts = self.aggregator.get_pnl_timeseries()
        self.assertEqual(len(ts), 2)
        self.assertEqual(ts[0], (1100.0, 440.0))
        self.assertEqual(ts[1], (1300.0, 580.0))

    def test_ascii_line_graph_rendering(self):
        data = [
            (1000.0, 0.0),
            (1100.0, 1500.0),
            (1200.0, 3000.0),
            (1300.0, 2500.0),
            (1400.0, 4500.0),
        ]
        graph_lines = render_ascii_line_graph(data, width=60, height=8, title="TEST P&L")
        self.assertGreater(len(graph_lines), 5)
        # Check that graph includes title, axes, and timestamps
        full_text = "\n".join(graph_lines)
        self.assertIn("TEST P&L", full_text)
        self.assertIn("●", full_text)

    def test_daily_report_generator_time_window_filtering(self):
        """Verify report generator filters trades by start-time and end-time correctly."""
        gen = DailyReportGenerator(session_date="20261109")
        # Mock trades across different times
        mock_trades = [
            {
                "trade_id": "T1",
                "mode": "paper",
                "track": "ATM",
                "symbol": "INFY",
                "entry_time": 1000.0,
                "entry_datetime": "2026-11-09 09:16:00",
                "exit_time": 1060.0,
                "exit_datetime": "2026-11-09 09:17:00",
                "entry_price": 20.0,
                "exit_price": 25.0,
                "net_pnl": 500.0,
                "gross_pnl": 560.0,
                "brokerage": 60.0,
                "quantity": 100,
                "direction": "BULL",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET_HIT",
            },
            {
                "trade_id": "T2",
                "mode": "paper",
                "track": "OTM",
                "symbol": "TCS",
                "entry_time": 2000.0,
                "entry_datetime": "2026-11-09 10:15:00",
                "exit_time": 2060.0,
                "exit_datetime": "2026-11-09 10:20:00",
                "entry_price": 50.0,
                "exit_price": 45.0,
                "net_pnl": -500.0,
                "gross_pnl": -440.0,
                "brokerage": 60.0,
                "quantity": 100,
                "direction": "BEAR",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "STOP_LOSS",
            },
            {
                "trade_id": "T3",
                "mode": "paper_delayed",
                "track": "DYNAMIC_TRIO",
                "symbol": "RELIANCE",
                "entry_time": 3000.0,
                "entry_datetime": "2026-11-09 11:45:00",
                "exit_time": 3060.0,
                "exit_datetime": "2026-11-09 11:50:00",
                "entry_price": 30.0,
                "exit_price": 35.0,
                "net_pnl": 1250.0,
                "gross_pnl": 1310.0,
                "brokerage": 60.0,
                "quantity": 250,
                "direction": "BULL",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET_HIT",
            },
        ]
        gen.load_trades = lambda: mock_trades

        # Window 09:15 to 09:30 should only capture T1
        report_early = gen.generate_report(start_time="09:15", end_time="09:30")
        self.assertIn("INFY", report_early)
        self.assertNotIn("TCS", report_early)
        self.assertNotIn("RELIANCE", report_early)
        self.assertEqual(len(gen.last_filtered_trades), 1)

        # Window 10:00 to 11:00 should only capture T2
        report_mid = gen.generate_report(start_time="10:00", end_time="11:00")
        self.assertNotIn("INFY", report_mid)
        self.assertIn("TCS", report_mid)
        self.assertNotIn("RELIANCE", report_mid)
        self.assertEqual(len(gen.last_filtered_trades), 1)

    def test_daily_report_generator_mode_specific_track_filtering(self):
        """Verify report generator handles track filter like 'paper:ATM,OTM;paper_delayed:DYN'."""
        gen = DailyReportGenerator(session_date="20261109")
        mock_trades = [
            {
                "trade_id": "T1",
                "mode": "paper",
                "track": "ATM",
                "symbol": "INFY",
                "entry_datetime": "2026-11-09 09:16:00",
                "exit_datetime": "2026-11-09 09:17:00",
                "entry_price": 20.0,
                "exit_price": 25.0,
                "net_pnl": 500.0,
                "gross_pnl": 560.0,
                "brokerage": 60.0,
                "quantity": 100,
                "direction": "BULL",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET_HIT",
            },
            {
                "trade_id": "T2",
                "mode": "paper",
                "track": "DYNAMIC_TRIO",
                "symbol": "TCS",
                "entry_datetime": "2026-11-09 09:20:00",
                "exit_datetime": "2026-11-09 09:25:00",
                "entry_price": 50.0,
                "exit_price": 45.0,
                "net_pnl": -500.0,
                "gross_pnl": -440.0,
                "brokerage": 60.0,
                "quantity": 100,
                "direction": "BEAR",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "STOP_LOSS",
            },
            {
                "trade_id": "T3",
                "mode": "paper_delayed",
                "track": "DYNAMIC_TRIO",
                "symbol": "RELIANCE",
                "entry_datetime": "2026-11-09 09:30:00",
                "exit_datetime": "2026-11-09 09:35:00",
                "entry_price": 30.0,
                "exit_price": 35.0,
                "net_pnl": 1250.0,
                "gross_pnl": 1310.0,
                "brokerage": 60.0,
                "quantity": 250,
                "direction": "BULL",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET_HIT",
            },
            {
                "trade_id": "T4",
                "mode": "paper_delayed",
                "track": "ATM",
                "symbol": "WIPRO",
                "entry_datetime": "2026-11-09 09:40:00",
                "exit_datetime": "2026-11-09 09:45:00",
                "entry_price": 10.0,
                "exit_price": 12.0,
                "net_pnl": 200.0,
                "gross_pnl": 260.0,
                "brokerage": 60.0,
                "quantity": 100,
                "direction": "BULL",
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET_HIT",
            },
        ]
        gen.load_trades = lambda: mock_trades

        # Request paper:ATM and paper_delayed:DYN
        # In the trade journal:
        # - PAPER should show ATM
        # - PAPER_DELAYED should show DYNAMIC_TRIO
        # - PAPER_DELAYED ATM should NOT be in journal
        report = gen.generate_report(track_filter="paper:ATM;paper_delayed:DYN")

        self.assertIn("▶ [PAPER] TRACK: ATM", report)
        self.assertIn("▶ [PAPER_DELAYED] TRACK: DYNAMIC_TRIO", report)
        self.assertNotIn("▶ [PAPER_DELAYED] TRACK: ATM", report)

        # Verify journal header columns: START TIME, START PRICE, EXIT TIME, EXIT PRICE, P&L
        self.assertIn("START TIME", report)
        self.assertIn("START PRICE", report)
        self.assertIn("EXIT TIME", report)
        self.assertIn("EXIT PRICE", report)
        self.assertIn("P&L", report)
        self.assertNotIn("BROKERAGE", report)

    def test_log_enumeration_on_exit(self):
        # Record trade exit so files have data
        self.aggregator.record_trade_open("TRD-ENUM-1", "momentum", "ATM", "paper", "INFY", "K1", "S1", "BULL", 100, 20.0, 1000.0)
        self.aggregator.record_trade_exit("TRD-ENUM-1", 25.0, 1200.0, "TGT")

        rec_file = os.path.join(self.record_dir, self.session_date, "trades.jsonl")
        log_file = os.path.join(self.log_dir, f"trade_journal_{self.session_date}.jsonl")
        self.assertTrue(os.path.exists(rec_file))
        self.assertTrue(os.path.exists(log_file))

        # Trigger enumeration
        self.aggregator.enumerate_logs()

        # Original files should be rotated to _1.jsonl
        rec_enum_1 = os.path.join(self.record_dir, self.session_date, "trades_1.jsonl")
        log_enum_1 = os.path.join(self.log_dir, f"trade_journal_{self.session_date}_1.jsonl")
        self.assertFalse(os.path.exists(rec_file))
        self.assertFalse(os.path.exists(log_file))
        self.assertTrue(os.path.exists(rec_enum_1))
        self.assertTrue(os.path.exists(log_enum_1))

        # A second run writing and enumerating produces _2.jsonl
        self.aggregator.record_trade_exit("TRD-ENUM-2", 30.0, 1300.0, "TGT")
        self.assertTrue(os.path.exists(rec_file))
        self.aggregator.enumerate_logs()
        rec_enum_2 = os.path.join(self.record_dir, self.session_date, "trades_2.jsonl")
        self.assertTrue(os.path.exists(rec_enum_2))

    def test_legacy_sim_mode_mapped_to_paper(self):
        gen = DailyReportGenerator(session_date=self.session_date)
        mock_trades = [
            {
                "trade_id": "SIM-01",
                "strategy": "momentum",
                "track": "ATM",
                "mode": "sim",
                "symbol": "INFY",
                "contract_symbol": "INFY26NOV1800CE",
                "direction": "BULL",
                "quantity": 100,
                "entry_price": 20.0,
                "exit_price": 25.0,
                "entry_time": 1000.0,
                "exit_time": 1200.0,
                "gross_pnl": 500.0,
                "net_pnl": 440.0,
                "status": "CLOSED",
                "event": "EXIT",
                "exit_reason": "TARGET",
            }
        ]
        # Write to JSONL
        test_rec = os.path.join(self.record_dir, self.session_date, "trades.jsonl")
        os.makedirs(os.path.dirname(test_rec), exist_ok=True)
        with open(test_rec, "w", encoding="utf-8") as f:
            f.write(json.dumps(mock_trades[0]) + "\n")

        # Temporarily mock os.path to load from self.record_dir
        orig_load = gen.load_trades
        try:
            gen.load_trades = lambda: [dict(mock_trades[0], mode="paper")]
            report = gen.generate_report(mode_filter="paper")
            self.assertIn("PAPER", report)
            self.assertNotIn("SIM", report)
        finally:
            gen.load_trades = orig_load


if __name__ == "__main__":
    unittest.main()
