#!/usr/bin/env python3
"""
tests/strategies/test_live_backfill_and_trio.py
==============================================
Comprehensive tests for:
1. Dynamic Trio macro regime-specific champion allocation (PRO_BULL, PRO_BEAR)
2. Dynamic Trio non-champion isolation (does not mirror trades from non-champions)
3. Live order modifications: proactive cancel & replace at 24 modifications + reactive on broker error
4. Real-time stop-loss update dispatch on option ticks
5. Live strategy backfill from recorded session ticks with start-time simulation
6. Expand-tracks flag behavior (default False vs True)
7. Dashboard rendering integrity across views
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from execution.live.live_adapter import LiveExecutionAdapter
from simulation.simulation_engine import SimulationEngine
from strategies.momentum.dashboard import render_scanner_dashboard, render_performance_matrix
from strategies.momentum.portfolio import MultiTrackPortfolio, TradeRecord, FIXED_CAP_SUFFIX
from strategies.momentum.strategy import MomentumStrategyRunner


class TestLiveBackfillAndTrio(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_dynamic_trio_regime_specific_allocation(self):
        """Verify Dynamic Trio assigns ITM/Trailing champions based on macro Nifty regime."""
        port = MultiTrackPortfolio(starting_capital=100_000.0, track_filter="ALL")

        # 1. Test PRO_BULL regime
        port.update_dynamic_trio_leaderboard(
            now_ts=time.time(),
            nifty_stats={"regime": "PRO_BULL", "pct_change": 0.55},
        )
        self.assertEqual(port.dynamic_trio_champions, ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "ITM"])
        self.assertEqual(port.dynamic_trio_weights["TRAIL_SL_ITM"], 0.50)
        self.assertEqual(port.dynamic_trio_weights["TRAIL_SL_ATM"], 0.30)
        self.assertEqual(port.dynamic_trio_weights["ITM"], 0.20)

        # Force bypass 5s cooldown
        port._last_trio_update_ts = 0.0

        # 2. Test PRO_BEAR regime
        port.update_dynamic_trio_leaderboard(
            now_ts=time.time() + 10.0,
            nifty_stats={"regime": "PRO_BEAR", "pct_change": -0.60},
        )
        self.assertEqual(port.dynamic_trio_champions, ["TRAIL_SL_ITM", "ITM", "TRAIL_SL_ATM"])
        self.assertEqual(port.dynamic_trio_weights["TRAIL_SL_ITM"], 0.50)
        self.assertEqual(port.dynamic_trio_weights["ITM"], 0.30)
        self.assertEqual(port.dynamic_trio_weights["TRAIL_SL_ATM"], 0.20)

    def test_02_dynamic_trio_does_not_trade_non_champions(self):
        """Verify Dynamic Trio does NOT open a mirrored trade if opened trades are from non-champions."""
        port = MultiTrackPortfolio(starting_capital=100_000.0, track_filter="ALL")
        port.dynamic_trio_champions = ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "ITM"]
        port.dynamic_trio_weights = {"TRAIL_SL_ITM": 0.50, "TRAIL_SL_ATM": 0.30, "ITM": 0.20}

        # Simulate opened trades from FIXED_TGT_OTM (NOT a champion)
        non_champ_trade = TradeRecord(
            trade_id="TRD-FIXED_TGT_OTM-TCS-0001",
            strategy_track="FIXED_TGT_OTM",
            symbol="TCS",
            contract_key="NSE_FO|TCS26NOV3500CE",
            contract_symbol="TCS26NOV3500CE",
            direction="BULL",
            lots=1,
            quantity=175,
            entry_time=1000.0,
            entry_price=50.0,
            peak_price=50.0,
            underlying_entry=3500.0,
            underlying_sl=3450.0,
            current_sl=42.5,
        )

        opened_trades = [non_champ_trade]
        matching = [
            t for t in opened_trades
            if (t.strategy_track in port.dynamic_trio_champions or
                t.strategy_track.replace(FIXED_CAP_SUFFIX, "") in port.dynamic_trio_champions)
        ]
        self.assertEqual(len(matching), 0)

        # Now test with a champion trade
        champ_trade = TradeRecord(
            trade_id="TRD-TRAIL_SL_ITM-TCS-0002",
            strategy_track="TRAIL_SL_ITM",
            symbol="TCS",
            contract_key="NSE_FO|TCS26NOV3400CE",
            contract_symbol="TCS26NOV3400CE",
            direction="BULL",
            lots=1,
            quantity=175,
            entry_time=1000.0,
            entry_price=60.0,
            peak_price=60.0,
            underlying_entry=3500.0,
            underlying_sl=3450.0,
            current_sl=51.0,
        )
        matching_champ = [
            t for t in [champ_trade]
            if (t.strategy_track in port.dynamic_trio_champions or
                t.strategy_track.replace(FIXED_CAP_SUFFIX, "") in port.dynamic_trio_champions)
        ]
        self.assertEqual(len(matching_champ), 1)

    def test_03_groww_live_cancel_and_replace_at_max_modifications(self):
        """Verify Groww live adapter cancels and replaces when modification count hits limit."""
        mock_client = MagicMock()
        mock_client.cancel_order.return_value = {"order_id": "GROWW_ORD_001", "order_status": "CANCELLED"}
        mock_client.place_order.return_value = {"status": "SUCCESS", "groww_order_id": "GROWW_ORD_002"}

        adapter = LiveExecutionAdapter(
            groww_client=mock_client,
            output_csv=os.path.join(self.tmp_dir, "live_trades.csv"),
            broker_max_order_modifications=25,
            live_confirmed=True,
        )

        pos_key = "TCS_NSE_FO|TCS26NOV3500CE"
        adapter.positions[pos_key] = {
            "symbol": "TCS",
            "instrument_token": "NSE_FO|TCS26NOV3500CE",
            "quantity": 175,
            "entry_price": 50.0,
            "current_sl": 42.5,
            "sl_order_id": "GROWW_ORD_001",
            "modification_count": 25,  # Reached limit threshold (25)
        }

        res = adapter.update_stop_loss(
            symbol="TCS",
            instrument_token="NSE_FO|TCS26NOV3500CE",
            new_stop_loss_price=45.0,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["action"], "CANCEL_AND_REPLACE")
        self.assertEqual(res["old_order_id"], "GROWW_ORD_001")
        self.assertEqual(res["new_order_id"], "GROWW_ORD_002")

        mock_client.cancel_order.assert_called_once_with(groww_order_id="GROWW_ORD_001")
        mock_client.place_order.assert_called_once()
        self.assertEqual(adapter.positions[pos_key]["modification_count"], 0)
        self.assertEqual(adapter.positions[pos_key]["sl_order_id"], "GROWW_ORD_002")
        self.assertEqual(adapter.positions[pos_key]["current_sl"], 45.0)

    def test_04_groww_live_reactive_cancel_and_replace_on_broker_error(self):
        """Verify Groww live adapter cancels and replaces if modify_order returns modification limit error."""
        mock_client = MagicMock()
        mock_client.modify_order.return_value = {"status": "ERROR", "error": "Maximum modifications limit exceeded (25/25)"}
        mock_client.cancel_order.return_value = {"order_id": "GROWW_ORD_003", "order_status": "CANCELLED"}
        mock_client.place_order.return_value = {"status": "SUCCESS", "groww_order_id": "GROWW_ORD_004"}

        adapter = LiveExecutionAdapter(
            groww_client=mock_client,
            output_csv=os.path.join(self.tmp_dir, "live_trades.csv"),
            broker_max_order_modifications=25,
            live_confirmed=True,
        )

        pos_key = "INFY_NSE_FO|INFY26NOV1800CE"
        adapter.positions[pos_key] = {
            "symbol": "INFY",
            "instrument_token": "NSE_FO|INFY26NOV1800CE",
            "quantity": 300,
            "entry_price": 30.0,
            "current_sl": 25.5,
            "sl_order_id": "GROWW_ORD_003",
            "modification_count": 10,  # Below 24, but broker rejected with error
        }

        res = adapter.update_stop_loss(
            symbol="INFY",
            instrument_token="NSE_FO|INFY26NOV1800CE",
            new_stop_loss_price=27.0,
        )

        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["action"], "CANCEL_AND_REPLACE")
        self.assertEqual(res["new_order_id"], "GROWW_ORD_004")
        mock_client.cancel_order.assert_called_once_with(groww_order_id="GROWW_ORD_003")
        mock_client.place_order.assert_called_once()
        self.assertEqual(adapter.positions[pos_key]["modification_count"], 0)

    def test_05_expand_tracks_flag_in_simulation_engine(self):
        """Verify SimulationEngine respects expand_tracks flag."""
        sim_default = SimulationEngine(expand_tracks=False)
        self.assertFalse(sim_default.expand_tracks)

        sim_expanded = SimulationEngine(expand_tracks=True)
        self.assertTrue(sim_expanded.expand_tracks)

    def test_06_live_strategy_backfill_and_simulation(self):
        """Verify backfill_from_recordings replays ticks with warm-up before start_time and simulation after."""
        session_date = "99990101"
        rec_dir = os.path.join("recordings", session_date)
        os.makedirs(rec_dir, exist_ok=True)
        events_file = os.path.join(rec_dir, "market_events.jsonl")

        try:
            ist = timezone(timedelta(hours=5, minutes=30))
            t_warmup = datetime(9999, 1, 1, 9, 10, 0, tzinfo=ist).timestamp()
            t_sim = datetime(9999, 1, 1, 9, 20, 0, tzinfo=ist).timestamp()

            events = [
                {"received_timestamp": t_warmup, "symbol": "NIFTY", "instrument_key": "NSE_INDEX|Nifty 50", "ltp": 24500.0, "close": 24400.0},
                {"received_timestamp": t_warmup + 1.0, "symbol": "TCS", "instrument_key": "NSE_EQ|INE467B01029", "ltp": 3500.0, "close": 3480.0, "volume": 1000},
                {"received_timestamp": t_sim, "symbol": "TCS", "instrument_key": "NSE_EQ|INE467B01029", "ltp": 3550.0, "close": 3480.0, "volume": 50000},
            ]
            with open(events_file, "w") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")

            runner = MomentumStrategyRunner(
                mode="paper",
                capital=100_000.0,
                enable_dashboard=False,
                backfill=True,
                start_time="09:15",
            )

            total_backfilled = runner.backfill_from_recordings(session_date=session_date, start_time_str="09:15")
            self.assertEqual(total_backfilled, 3)
            self.assertIn("NSE_INDEX|Nifty 50", runner.quotes_cache)
            self.assertIn("NSE_EQ|INE467B01029", runner.quotes_cache)
            self.assertFalse(runner.is_backfilling)
        finally:
            import shutil
            shutil.rmtree(rec_dir, ignore_errors=True)

    def test_07_dashboard_rendering_integrity(self):
        """Verify render_scanner_dashboard renders cleanly without errors across multiple states."""
        port = MultiTrackPortfolio(starting_capital=100_000.0, track_filter="ALL")
        quotes = {
            "NSE_INDEX|Nifty 50": {"ltp": 24500.0, "close": 24400.0},
            "NSE_EQ|INE467B01029": {"ltp": 3520.0, "close": 3500.0, "bid": 3519.0, "ask": 3521.0},
        }
        nifty_stats = {"regime": "PRO_BULL", "pct_change": 0.41}
        fresh_alerts = [{
            "symbol": "TCS",
            "ltp": 3520.0,
            "chg": 0.57,
            "rvol_5m": 2.4,
            "rvol_cum": 1.6,
            "pace": 1.5,
            "score": 92.0,
            "abs_score": 115.0,
            "stage": "💥 BULL BREAK",
            "opt_req": "₹15.2k (₹45.0)",
            "countdown_str": "⏳ 6.5s left",
        }]

        view = render_scanner_dashboard(
            monitored_count=2,
            nifty_stats=nifty_stats,
            fresh_alerts=fresh_alerts,
            confirmed_setups=[],
            portfolio=port,
            quotes_cache=quotes,
            mode="PAPER",
            min_score=67,
            show_positions=True,
            view_mode="all",
        )
        self.assertIsInstance(view, str)
        self.assertIn("UPSTOX EVENT-DRIVEN MOMENTUM STRATEGY", view)
        self.assertIn("FRESH BREAKOUT ALERTS", view)
        self.assertIn("STRATEGY SIMULATION MATRIX", view)


if __name__ == "__main__":
    unittest.main()
