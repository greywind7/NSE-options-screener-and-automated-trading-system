#!/usr/bin/env python3
"""
tests/infrastructure/test_premarket.py
======================================
Unit tests for Pre-Market Preparation Pipeline:
- Master Fetcher verification
- F&O Instrument Filtering
- Volume Index Baseline
- Subscription Manifest Generation
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.instruments.master_fetcher import run_master_fetcher
from core.instruments.instrument_filter import run_instrument_filter
from core.volume.volume_index import compute_volume_profile, run_volume_indexer
from core.instruments.subscription_manifest import run_manifest_generator


class TestPreMarketPipeline(unittest.TestCase):

    def test_01_master_fetcher_local(self):
        """Verify master fetcher recognizes existing local file without redundant download."""
        res = run_master_fetcher(force_download=False, target_csv="data/instrument.csv")
        self.assertIsNotNone(res)
        self.assertTrue(os.path.exists(res))
        self.assertGreater(os.path.getsize(res), 1_000_000)

    def test_02_instrument_filter(self):
        """Verify F&O equity filtering generates valid output with ~200+ symbols."""
        out_csv = "data/filtered.csv"
        res = run_instrument_filter(master_csv="data/instrument.csv", output_csv=out_csv)
        self.assertEqual(res, out_csv)
        self.assertTrue(os.path.exists(out_csv))

        # Check rows
        with open(out_csv, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertGreater(len(lines), 200, "Expected > 200 F&O equities")
        header = lines[0].strip().split(",")
        self.assertIn("upstox_instrument_key", header)
        self.assertIn("trading_symbol", header)
        self.assertIn("lot_size", header)

    def test_03_compute_volume_profile(self):
        """Verify volume profile mathematical calculations."""
        # 3 mock candles: [timestamp, open, high, low, close, volume, oi]
        mock_candles = [
            ["2026-11-06T09:15:00+05:30", 100.0, 101.0, 99.5, 100.5, 10000.0, 500],
            ["2026-11-06T09:16:00+05:30", 100.5, 102.0, 100.2, 101.5, 15000.0, 520],
            ["2026-11-06T09:17:00+05:30", 101.5, 101.8, 101.0, 101.2, 5000.0, 510],
        ]
        profile = compute_volume_profile(mock_candles, interval_minutes=1)
        self.assertEqual(profile["total_candles"], 3)
        self.assertEqual(profile["total_volume"], 30000.0)
        self.assertEqual(profile["avg_1m_volume"], 10000.0)
        self.assertEqual(profile["avg_5m_volume"], 50000.0)
        self.assertEqual(profile["time_slot_avg_volume"]["09:15"], 10000.0)
        self.assertEqual(profile["time_slot_cum_volume"]["09:16"], 25000.0)
        self.assertEqual(profile["time_slot_cum_volume"]["09:17"], 30000.0)
        self.assertTrue(profile["has_volume_data"])

    def test_03b_compute_volume_profile_empty_no_assumptions(self):
        """Verify empty/missing candles do NOT make dangerous assumptions (0.0 volume, has_volume_data=False)."""
        empty_profile = compute_volume_profile([], interval_minutes=1)
        self.assertEqual(empty_profile["total_candles"], 0)
        self.assertEqual(empty_profile["total_volume"], 0)
        self.assertEqual(empty_profile["daily_avg_volume"], 0.0)
        self.assertEqual(empty_profile["avg_5m_volume"], 0.0)
        self.assertEqual(empty_profile["avg_1m_volume"], 0.0)
        self.assertFalse(empty_profile["has_volume_data"])

    def test_04_subscription_manifest(self):
        """Verify subscription manifest correctly partitions underlyings, popular options, and background polling."""
        out_json = "data/subscription_manifest.json"
        res = run_manifest_generator(
            filtered_csv="data/filtered.csv",
            volume_index_json="data/index.json",
            master_csv="data/instrument.csv",
            output_json=out_json,
            popular_strikes=3,
        )
        self.assertEqual(res, out_json)
        self.assertTrue(os.path.exists(out_json))

        with open(out_json, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        summary = manifest.get("summary", {})
        self.assertGreater(summary.get("total_underlying_equities", 0), 200)
        # Dual-connection architecture: Upstox 2,000 keys per connection (up to 4,000 total across 2 connections)
        self.assertGreater(summary.get("total_popular_options_streamed", 0), 1000)
        total_ws_keys = summary.get("total_underlying_equities", 0) + summary.get("total_popular_options_streamed", 0)
        self.assertLessEqual(total_ws_keys, 4000, "Total WebSocket streamed keys must not exceed dual-connection limit of 4,000 keys")
        self.assertLessEqual(total_ws_keys // 2, 2000, "Average per-connection keys must not exceed Upstox 2,000 key limit")
        self.assertGreater(summary.get("total_background_options_polled", 0), 10000)

        # Verify underlying keys format
        first_und = manifest["underlying_keys"][0]
        self.assertTrue(first_und.startswith("NSE_EQ|"), f"Invalid underlying key format: {first_und}")

        # Verify popular option keys format
        first_opt = manifest["popular_option_keys"][0]
        self.assertTrue(first_opt.startswith("NSE_FO|"), f"Invalid option key format: {first_opt}")

    def test_05_soft_prepare_and_dating(self):
        """Verify soft prepare dates documents and skips redundant preparation if files already exist."""
        import argparse
        from datetime import datetime
        from runner import cmd_prepare

        today_tag = datetime.now().strftime("%Y%m%d")
        args = argparse.Namespace(
            date=today_tag,
            reset=False,
            volume_days=30,
        )
        code = cmd_prepare(args)
        self.assertEqual(code, 0)

        # Verify dated files exist
        self.assertTrue(os.path.exists(f"data/instrument_{today_tag}.csv"))
        self.assertTrue(os.path.exists(f"data/filtered_{today_tag}.csv"))
        self.assertTrue(os.path.exists(f"data/index_{today_tag}.json"))
        self.assertTrue(os.path.exists(f"data/subscription_manifest_{today_tag}.json"))

        # Verify canonical files exist and match
        self.assertTrue(os.path.exists("data/instrument.csv"))
        self.assertTrue(os.path.exists("data/filtered.csv"))
        self.assertTrue(os.path.exists("data/index.json"))
        self.assertTrue(os.path.exists("data/subscription_manifest.json"))

    def test_06_mcx_flagship_benchmark_selection(self):
        """Verify MCX commodity filtering prioritizes flagship benchmark contracts over mini/micro/petal."""
        import csv
        out_csv = "data/filtered_mcx.csv"
        res = run_instrument_filter(master_csv="data/instrument.csv", output_csv=out_csv, target_exchange="MCX")
        self.assertEqual(res, out_csv)
        self.assertTrue(os.path.exists(out_csv))

        rows_by_name = {}
        with open(out_csv, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows_by_name[r["name"]] = r

        # SILVER: Flagship 30kg benchmark (495214) over SILVER100 / SILVERM / SILVERMIC
        silver = rows_by_name.get("SILVER")
        self.assertIsNotNone(silver)
        self.assertEqual(silver["trading_symbol"], "SILVER26DECFUT")
        self.assertEqual(silver["upstox_instrument_key"], "MCX_FO|495214")
        self.assertEqual(int(silver["lot_size"]), 30)

        # GOLD: Flagship 1kg benchmark (495213) over GOLDPETAL / GOLDGUINEA / GOLDTEN
        gold = rows_by_name.get("GOLD")
        self.assertIsNotNone(gold)
        self.assertEqual(gold["trading_symbol"], "GOLD26DECFUT")
        self.assertEqual(gold["upstox_instrument_key"], "MCX_FO|495213")

        # CRUDE OIL: Flagship 100-barrel benchmark over CRUDEOILM
        crude = rows_by_name.get("CRUDE OIL")
        self.assertIsNotNone(crude)
        self.assertIn(crude["trading_symbol"], ("CRUDEOIL26NOVFUT", "CRUDEOIL26DECFUT"))
        self.assertIn(crude["upstox_instrument_key"], ("MCX_FO|573422", "MCX_FO|576264"))
        self.assertEqual(int(crude["lot_size"]), 100)

        # NATURALGAS: Flagship 1250-mmBtu benchmark (574319 or 578249) over NATGASMINI
        natgas = rows_by_name.get("NATURALGAS")
        self.assertIsNotNone(natgas)
        self.assertIn(natgas["trading_symbol"], ("NATURALGAS26NOVFUT", "NATURALGAS26DECFUT"))
        self.assertIn(natgas["upstox_instrument_key"], ("MCX_FO|574319", "MCX_FO|578249"))
        self.assertEqual(int(natgas["lot_size"]), 1250)


if __name__ == "__main__":
    unittest.main()

