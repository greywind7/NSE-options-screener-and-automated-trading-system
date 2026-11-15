"""
Unit Tests for Unified Configuration Loader and Trading Schedule.
"""
import os
import unittest
from core.config.trading_schedule import get_trading_schedule, normalize_time_str
from core.config.config_loader import ConfigManager, get_config_manager


class TestConfigAndSchedule(unittest.TestCase):
    def test_normalize_time_str(self):
        self.assertEqual(normalize_time_str("09:15"), "09:15")
        self.assertEqual(normalize_time_str("9:15"), "09:15")
        self.assertEqual(normalize_time_str("15:20"), "15:20")
        self.assertEqual(normalize_time_str(None, default="15:15"), "15:15")
        self.assertEqual(normalize_time_str("invalid", default="15:15"), "15:15")

    def test_nse_schedule_cutoffs(self):
        sch = get_trading_schedule("NSE")
        self.assertEqual(sch.market_open_time, "09:15")
        self.assertEqual(sch.market_close_time, "15:30")
        self.assertEqual(sch.live_entry_freeze_time, "15:10")
        self.assertEqual(sch.live_eod_squareoff_time, "15:15")
        self.assertEqual(sch.paper_eod_squareoff_time, "15:20")

        # Open check
        self.assertTrue(sch.is_market_open("10:00"))
        self.assertFalse(sch.is_market_open("09:00"))
        self.assertFalse(sch.is_market_open("15:35"))

        # Midday check (11:00 to 13:30)
        self.assertTrue(sch.is_midday_lull("12:00"))
        self.assertFalse(sch.is_midday_lull("10:30"))
        self.assertFalse(sch.is_midday_lull("14:00"))

        # Freeze check (15:10)
        self.assertTrue(sch.is_live_entry_frozen("15:12"))
        self.assertFalse(sch.is_live_entry_frozen("14:59"))

        # EOD squareoff (15:15 live, 15:20 paper)
        self.assertTrue(sch.is_live_squareoff_time("15:16"))
        self.assertFalse(sch.is_live_squareoff_time("15:14"))
        self.assertTrue(sch.is_paper_squareoff_time("15:22"))
        self.assertFalse(sch.is_paper_squareoff_time("15:19"))

    def test_mcx_schedule_cutoffs(self):
        sch = get_trading_schedule("MCX")
        self.assertEqual(sch.market_open_time, "09:00")
        self.assertEqual(sch.market_close_time, "23:30")
        self.assertEqual(sch.live_entry_freeze_time, "23:10")
        self.assertEqual(sch.live_eod_squareoff_time, "23:15")
        self.assertEqual(sch.paper_eod_squareoff_time, "23:20")
        self.assertTrue(sch.is_market_open("20:00"))
        self.assertTrue(sch.is_live_entry_frozen("23:11"))

    def test_config_loader(self):
        mgr = get_config_manager()
        self.assertIn("strategies", mgr.strategy)
        self.assertIn("execution", mgr.execution)
        self.assertIn("system", mgr.system)
        self.assertIn("instruments", mgr.instruments)

        auth_key = mgr.get_ipc_auth_key()
        self.assertIsInstance(auth_key, bytes)
        self.assertGreater(len(auth_key), 0)

        # Track parameters from config
        fm_cfg = mgr.get_track_config("fast_momentum")
        self.assertEqual(fm_cfg.get("target_pct"), 0.12)
        self.assertEqual(fm_cfg.get("sl_pct"), 0.08)

        tp_cfg = mgr.get_track_config("trend_pullback")
        self.assertEqual(tp_cfg.get("target_pct"), 0.25)
        self.assertEqual(tp_cfg.get("sl_pct"), 0.12)

        ft_cfg = mgr.get_track_config("fixed_target")
        self.assertEqual(ft_cfg.get("fixed_target_inr"), 750.0)

        # Scoring 3.0 parameters from config
        sc_cfg = mgr.get_scoring_config()
        self.assertEqual(sc_cfg.get("min_volume_thrust_mult"), 2.0)
        self.assertEqual(sc_cfg.get("min_clv"), 0.75)
        self.assertEqual(sc_cfg.get("invalidation_retrace_pct"), 0.50)

        # RMS and risk parameters from config
        rk_cfg = mgr.get_risk_config()
        self.assertEqual(rk_cfg.get("daily_loss_limit_inr"), 10000.0)
        self.assertEqual(rk_cfg.get("max_concurrent_positions"), 5)

        # Recorder parameters from config
        rec_cfg = mgr.get_recorder_config()
        self.assertEqual(rec_cfg.get("format"), "parquet")
        self.assertEqual(rec_cfg.get("compression"), "snappy")
        self.assertTrue(rec_cfg.get("preserve_full_depth"))

        # Execution mode parameters from config
        delayed_cfg = mgr.get_execution_mode_config("paper_delayed")
        self.assertEqual(delayed_cfg.get("delay_sec"), 60.0)

        # Endpoints
        endpoints = mgr.get_ipc_endpoints()
        self.assertIn("feed_ipc", endpoints)
        self.assertIn("quotes_router_ipc", endpoints)


if __name__ == "__main__":
    unittest.main()

