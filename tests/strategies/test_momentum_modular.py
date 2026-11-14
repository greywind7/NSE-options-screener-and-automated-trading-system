#!/usr/bin/env python3
"""
tests/strategies/test_momentum_modular.py
=========================================
Unit tests for the modular Momentum Strategy and new enhancements:
1. Volume Inflation Gate for Bull Flags (rejects fluff breakouts).
2. Dual Scoring Architecture (Relative [0, 100] + Unbounded Absolute Score).
3. Nifty Market Regime Bias (Macro index alignment).
4. 16 Simulation Tracks + DYNAMIC_TRIO Ensemble Allocator.
5. Sandbox Execution Spread Fill Model.
"""

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from strategies.momentum.indicators import compute_macd, compute_rsi, compute_support_resistance_levels
from strategies.momentum.patterns import FlagPatternTracker
from strategies.momentum.portfolio import ALL_SIM_TRACKS, MultiTrackPortfolio
from strategies.momentum.scoring import calculate_setup_score


class TestMomentumModularStrategy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._saved_freeze_files = {}
        if os.path.exists("logs"):
            for fname in os.listdir("logs"):
                if fname.startswith(".freeze_"):
                    fpath = os.path.join("logs", fname)
                    try:
                        with open(fpath, "rb") as f:
                            cls._saved_freeze_files[fpath] = f.read()
                        os.remove(fpath)
                    except Exception:
                        pass

    @classmethod
    def tearDownClass(cls):
        # Clean any freeze flags created during tests
        if os.path.exists("logs"):
            for fname in os.listdir("logs"):
                if fname.startswith(".freeze_"):
                    try:
                        os.remove(os.path.join("logs", fname))
                    except Exception:
                        pass
        # Restore previously existing freeze flags
        if hasattr(cls, "_saved_freeze_files"):
            for fpath, data in cls._saved_freeze_files.items():
                try:
                    with open(fpath, "wb") as f:
                        f.write(data)
                except Exception:
                    pass

    def test_01_bull_flag_volume_inflation_gate(self):
        """Verify fluff/low-volume bull flag breakouts are rejected, and only volume-inflated breaks fire."""
        tracker = FlagPatternTracker()
        key = "NSE_EQ|INFY"

        # 1. Establish Bull Pole
        t0 = 1000.0
        p_state1 = tracker.update(
            instrument_key=key,
            ltp=100.0,
            rvol_5m=2.0,
            rvol_cum=1.5,
            a_vol=1.5,
            pace=1.3,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=t0,
        )
        self.assertTrue(p_state1["has_bull_pole"])

        # 2. Tight Consolidation / Coiling with dry volume
        for i in range(1, 4):
            tracker.update(
                instrument_key=key,
                ltp=99.5,
                rvol_5m=0.60,
                rvol_cum=1.1,
                a_vol=0.70,
                pace=0.60,
                range_pos=90.0,
                is_hod=False,
                is_lod=False,
                now_ts=t0 + i * 60,
            )

        # 3. Fluff Breakout: Price crosses above flag high (100.5 > 100.0) but volume is DRY (RVOL_5m = 0.70x, A_vol = 0.80x)
        fluff_state = tracker.update(
            instrument_key=key,
            ltp=100.5,
            rvol_5m=0.70,
            rvol_cum=1.0,
            a_vol=0.80,
            pace=0.75,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=t0 + 300,
        )
        self.assertFalse(fluff_state["bull_breakout"], "Fluff breakout should NOT fire confirmed bull_breakout!")
        self.assertTrue(fluff_state["bull_weak_break"], "Fluff breakout should be flagged as bull_weak_break!")

        # In scoring, fluff break is docked and capped at <= 60
        score_fluff = calculate_setup_score(
            ltp=100.5,
            high=100.5,
            low=98.0,
            close=98.0,
            volume_stats={"rvol_5m": 0.70, "rvol_cum": 1.0, "a_vol": 0.80, "pace": 0.75, "z_vol": 0.2},
            pattern_state=fluff_state,
        )
        self.assertEqual(score_fluff["stage"], "👀 WEAK BREAK / NO VOL")
        self.assertLessEqual(score_fluff["score"], 60.0)

        # 4. Legitimate Breakout: Price crosses with INFLATING VOLUME (RVOL_5m = 1.80x, A_vol = 1.60x, Pace = 1.30x)
        legit_state = tracker.update(
            instrument_key=key,
            ltp=101.0,
            rvol_5m=1.80,
            rvol_cum=1.60,
            a_vol=1.60,
            pace=1.30,
            range_pos=100.0,
            is_hod=True,
            is_lod=False,
            now_ts=t0 + 360,
        )
        self.assertTrue(legit_state["bull_breakout"], "Legitimate volume-inflated breakout MUST fire bull_breakout!")

        score_legit = calculate_setup_score(
            ltp=101.0,
            high=101.0,
            low=98.0,
            close=98.0,
            volume_stats={"rvol_5m": 1.80, "rvol_cum": 1.60, "a_vol": 1.60, "pace": 1.30, "z_vol": 1.5},
            pattern_state=legit_state,
        )
        self.assertEqual(score_legit["stage"], "💥 BULL BREAKOUT")
        self.assertGreaterEqual(score_legit["score"], 70.0)

    def test_02_dual_scoring_and_unbounded_absolute_score(self):
        """Verify unbounded absolute score rewards monster runners far beyond the 100-point relative screen."""
        # Setup A: Ordinary solid breakout
        score_a = calculate_setup_score(
            ltp=100.0,
            high=100.0,
            low=98.0,
            close=98.0,
            volume_stats={"rvol_5m": 2.0, "rvol_cum": 1.5, "a_vol": 1.4, "pace": 1.3, "z_vol": 1.0},
            pattern_state={"bull_breakout": True, "is_vol_inflated": True},
        )

        # Setup B: Monster runner with massive volume explosion (RVOL 5m = 5.0x, A_vol = 3.0x, Z_vol = 3.5σ)
        score_b = calculate_setup_score(
            ltp=100.0,
            high=100.0,
            low=98.0,
            close=98.0,
            volume_stats={"rvol_5m": 5.0, "rvol_cum": 3.0, "a_vol": 3.0, "pace": 2.2, "z_vol": 3.5},
            pattern_state={"bull_breakout": True, "is_vol_inflated": True},
            recent_ticks=[98.0, 99.0, 100.0],
        )

        # Relative score for both is high (clamped <= 100)
        self.assertLessEqual(score_a["score"], 100.0)
        self.assertLessEqual(score_b["score"], 100.0)

        # Unbounded Absolute score captures monster runner's true alpha
        self.assertGreater(score_b["absolute_score"], 130.0, "Monster runner should achieve S_abs > 130")
        self.assertGreater(score_b["absolute_score"], score_a["absolute_score"] + 20.0)

    def test_03_nifty_market_regime_bias(self):
        """Verify Nifty 50 regime boosts aligned setups and penalizes/suppresses counter-trend setups."""
        # 1. Pro-Bull Market (Nifty +0.60%)
        bull_nifty = {"pct_change": 0.60, "regime": "PRO_BULL"}

        # In Pro-Bull, Bull setup gets bonus
        score_bull = calculate_setup_score(
            ltp=100.0, high=100.0, low=98.0, close=98.0,
            volume_stats={"rvol_5m": 1.8, "rvol_cum": 1.5, "a_vol": 1.3, "pace": 1.2, "z_vol": 1.0},
            pattern_state={"bull_breakout": True, "is_vol_inflated": True},
            nifty_stats=bull_nifty,
        )
        self.assertEqual(score_bull["market_regime"], "PRO_BULL")
        self.assertEqual(score_bull["bonuses"]["regime"], 10.0)

        # In Pro-Bull, Bear setup is penalized and capped <= 65
        score_bear_in_bull = calculate_setup_score(
            ltp=95.0, high=98.0, low=95.0, close=98.0,
            volume_stats={"rvol_5m": 1.8, "rvol_cum": 1.5, "a_vol": 1.3, "pace": 1.2, "z_vol": 1.0},
            pattern_state={"bear_breakdown": True, "is_vol_inflated": True},
            nifty_stats=bull_nifty,
        )
        self.assertLessEqual(score_bear_in_bull["score"], 65.0, "Bear setup in raging bull market must be capped <= 65")
        self.assertEqual(score_bear_in_bull["bonuses"]["regime"], -25.0)

        # 2. Pro-Bear Market (Nifty -0.50%)
        bear_nifty = {"pct_change": -0.50, "regime": "PRO_BEAR"}
        score_bear = calculate_setup_score(
            ltp=95.0, high=98.0, low=95.0, close=98.0,
            volume_stats={"rvol_5m": 1.8, "rvol_cum": 1.5, "a_vol": 1.3, "pace": 1.2, "z_vol": 1.0},
            pattern_state={"bear_breakdown": True, "is_vol_inflated": True},
            nifty_stats=bear_nifty,
        )
        self.assertEqual(score_bear["market_regime"], "PRO_BEAR")
        self.assertEqual(score_bear["bonuses"]["regime"], 10.0)

    def test_04_multi_track_and_dynamic_trio_ensemble(self):
        """Verify 16 simulation tracks and DYNAMIC_TRIO ensemble capital allocator."""
        portfolio = MultiTrackPortfolio(starting_capital=100_000.0, output_csv="/tmp/test_paper_trades.csv")

        # Check all tracks initialized
        for t in ALL_SIM_TRACKS:
            self.assertIn(t, portfolio.tracks)

        # DYNAMIC_TRIO starts with 3 champions
        self.assertEqual(len(portfolio.dynamic_trio_champions), 3)

        # Simulate a trade fill: Buy at Ask (₹20.00)
        from dataclasses import dataclass

        @dataclass
        class MockContract:
            instrument_key: str = "NSE_FO|12345"
            trading_symbol: str = "TCS26NOV3500CE"
            lot_size: int = 100

        opened = portfolio.open_position(
            symbol="TCS",
            contract=MockContract(),
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote={"ask": 20.0, "bid": 19.5, "last_price": 20.0},
            flag_high=3500.0,
            flag_low=3470.0,
            now_ts=1000.0,
        )
        self.assertGreater(len(opened), 10, "Positions opened across simulation tracks")

        # Verify entry fill price was Ask (₹20.00)
        first_trade = opened[0]
        self.assertEqual(first_trade.entry_price, 20.0)

        # Simulate Target Exit at Bid ₹24.00 (paying spread, hits 15% target)
        closed = portfolio.update_positions(
            quotes_cache={
                "NSE_FO|12345": {"bid": 24.0, "ask": 24.5, "last_price": 24.0},
                "TCS": {"last_price": 3520.0},
            },
            now_ts=1050.0,
        )
        self.assertGreater(len(closed), 0, "Target exits executed")

        # Verify exit price was Bid (₹24.00) and net P&L incorporates ₹60 brokerage
        # Gross = (24.0 - 20.0) * 100 = ₹400.00
        # Net = ₹400.00 - ₹60.00 = ₹340.00
        first_closed = closed[0]
        self.assertEqual(first_closed.exit_price, 24.0)
        self.assertEqual(first_closed.gross_pnl, 400.0)
        self.assertEqual(first_closed.brokerage, 60.0)
        self.assertEqual(first_closed.net_pnl, 340.0)

    def test_05_strike_specific_values_and_track_filters(self):
        """Verify strike-specific contracts for ATM, ITM, OTM and track filtering."""
        from dataclasses import dataclass
        from strategies.momentum.dashboard import render_focused_strategy_table, render_scanner_dashboard

        @dataclass
        class MockContract:
            instrument_key: str
            trading_symbol: str
            lot_size: int = 100

        c_atm = MockContract("NSE_FO|ATM_KEY", "TCS26NOV3500CE")
        c_itm = MockContract("NSE_FO|ITM_KEY", "TCS26NOV3450CE")
        c_otm = MockContract("NSE_FO|OTM_KEY", "TCS26NOV3550CE")

        contracts_by_mode = {
            "ATM": c_atm,
            "ITM": c_itm,
            "OTM": c_otm,
        }

        quotes_cache = {
            "NSE_FO|ATM_KEY": {"ask": 18.0, "bid": 17.5},
            "NSE_FO|ITM_KEY": {"ask": 35.0, "bid": 34.0},
            "NSE_FO|OTM_KEY": {"ask": 8.0, "bid": 7.85},
        }

        # 1. Full portfolio with distinct strike contracts
        portfolio = MultiTrackPortfolio(starting_capital=100000.0)
        opened = portfolio.open_position(
            symbol="TCS",
            contract=c_atm,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=quotes_cache["NSE_FO|ATM_KEY"],
            flag_high=3500.0,
            flag_low=3470.0,
            now_ts=1000.0,
            contracts_by_mode=contracts_by_mode,
            quotes_cache=quotes_cache,
        )

        # Check distinct entries
        trade_by_track = {t.strategy_track: t for t in opened}
        self.assertEqual(trade_by_track["ATM"].entry_price, 18.0)
        self.assertEqual(trade_by_track["ITM"].entry_price, 35.0)
        self.assertEqual(trade_by_track["OTM"].entry_price, 8.0)
        self.assertEqual(trade_by_track["ATM"].contract_key, "NSE_FO|ATM_KEY")
        self.assertEqual(trade_by_track["ITM"].contract_key, "NSE_FO|ITM_KEY")
        self.assertEqual(trade_by_track["OTM"].contract_key, "NSE_FO|OTM_KEY")

        # 2. Track filtering: test --track ATM filters strictly to ATM family
        p_atm = MultiTrackPortfolio(starting_capital=100000.0, track_filter="ATM")
        for tname in p_atm.tracks:
            self.assertIn("ATM", tname)

        # 3. Test focused views in dashboard
        view_atm = render_focused_strategy_table(portfolio, quotes_cache, "ATM")
        self.assertIn("FOCUSED STRATEGY VIEW: ATM", view_atm)

        view_itm = render_focused_strategy_table(portfolio, quotes_cache, "ITM")
        self.assertIn("FOCUSED STRATEGY VIEW: ITM", view_itm)

        dash = render_scanner_dashboard(
            monitored_count=215,
            nifty_stats={"regime": "BULL", "pct_change": 0.85},
            fresh_alerts=[],
            confirmed_setups=[],
            portfolio=portfolio,
            quotes_cache=quotes_cache,
            view_mode="itm",
        )
        self.assertIn("FOCUSED STRATEGY VIEW: ITM", dash)

    def test_06_option_ticks_vs_underlying_separation(self):
        """Verify that option ticks feed quotes_cache but are strictly isolated from monitored scanner assets."""
        from strategies.momentum.strategy import MomentumStrategyRunner

        runner = MomentumStrategyRunner(mode="paper", manifest_path="data/subscription_manifest_mcx.json")
        runner.initialize()

        # Check option contract detection
        self.assertTrue(runner._is_option_contract("MCX_FO|581801", {"symbol": "COPPER 1380 CE"}))
        self.assertTrue(runner._is_option_contract("MCX_FO|999999", {}, display_name="CRUDE OIL 5800 PE"))
        self.assertFalse(runner._is_option_contract("MCX_FO|578634", {"symbol": "COPPER26NOVFUT"}, display_name="COPPER"))

        # 1. Send Option Tick (e.g. COPPER 1380 CE)
        opt_event = {
            "event_type": "TICK",
            "instrument_key": "MCX_FO|581801",
            "symbol": "COPPER 1380 CE",
            "ltp": 45.2,
            "volume": 5000,
            "bid": 45.0,
            "ask": 45.4,
            "close": 40.0,
            "received_timestamp": 1000.0,
        }
        runner.on_market_event(opt_event)

        # Quotes cache must be populated with live option price
        self.assertIn("MCX_FO|581801", runner.quotes_cache)
        self.assertEqual(runner.quotes_cache["MCX_FO|581801"]["ltp"], 45.2)

        # Monitored scanner assets and setups must NOT contain the option!
        self.assertNotIn("MCX_FO|581801", runner.monitored_assets)
        self.assertFalse(any(s["key"] == "MCX_FO|581801" for s in runner.confirmed_setups))
        self.assertFalse(any(s["key"] == "MCX_FO|581801" for s in runner.fresh_alerts.values()))

        # 2. Send Underlying Tick (e.g. COPPER future)
        und_event = {
            "event_type": "TICK",
            "instrument_key": "MCX_FO|578634",
            "symbol": "COPPER26NOVFUT",
            "ltp": 1382.5,
            "volume": 25000,
            "bid": 1382.0,
            "ask": 1383.0,
            "high": 1385.0,
            "low": 1370.0,
            "close": 1375.0,
            "received_timestamp": 1000.0,
        }
        runner.on_market_event(und_event)

        # Quotes cache AND monitored assets must be populated for underlying
        self.assertIn("MCX_FO|578634", runner.quotes_cache)
        self.assertIn("MCX_FO|578634", runner.monitored_assets)
        self.assertEqual(runner.monitored_assets["MCX_FO|578634"]["symbol"], "COPPER")

    def test_07_option_strike_resolver_aliases(self):
        """Verify OptionStrikeResolver resolves strikes across clean names, futures symbols, and keys."""
        from strategies.momentum.option_selector import OptionStrikeResolver

        resolver = OptionStrikeResolver(manifest_path="data/subscription_manifest_mcx.json")
        resolver.load()

        c_name = resolver.resolve_strike("COPPER", 1380.0, "BULL", "ATM")
        c_sym = resolver.resolve_strike("COPPER26NOVFUT", 1380.0, "BULL", "ATM")
        c_key = resolver.resolve_strike("MCX_FO|578634", 1380.0, "BULL", "ATM")

        self.assertIsNotNone(c_name)
        self.assertIsNotNone(c_sym)
        self.assertIsNotNone(c_key)
        self.assertEqual(c_name.instrument_key, c_sym.instrument_key)
        self.assertEqual(c_name.instrument_key, c_key.instrument_key)
        self.assertEqual(c_name.option_type, "CE")

    def test_08_single_track_execution_isolation(self):
        """
        Verify that while the strategy runs all 16 simulation tracks in memory,
        physical execution (multi_executor) places orders ONLY for the single designated track
        (e.g. DYNAMIC_TRIO or specified --track), and only exits when that track's position closes.
        """
        from execution.multi_executor import MultiExecutionManager
        from strategies.momentum.strategy import MomentumStrategyRunner

        placed_orders = []

        class MockAdapter:
            def place_order(self, **kwargs):
                placed_orders.append(kwargs)
                return {"status": "FILLED", "order_id": f"ORD_{len(placed_orders)}"}

        manager = MultiExecutionManager({"live": MockAdapter()})
        runner = MomentumStrategyRunner(
            mode="live",
            track_filter="DYNAMIC_TRIO",
            execution_adapter=manager,
        )

        self.assertEqual(runner.designated_execution_track, "DYNAMIC_TRIO")

        # Mock trade entry: portfolio opens 16 simulated tracks
        from strategies.momentum.option_selector import OptionContract
        mock_contract = OptionContract(
            instrument_key="NSE_FO|2500CE",
            trading_symbol="RELIANCE26NOV2500CE",
            underlying_symbol="RELIANCE",
            strike_price=2500.0,
            option_type="CE",
            expiry_date="2026-11-26",
            lot_size=250,
            tick_size=0.05,
        )
        runner.quotes_cache["NSE_FO|2500CE"] = {"ask": 50.0, "bid": 49.0, "ltp": 49.5}

        # 1. Trigger open_position
        opened = runner.portfolio.open_position(
            symbol="RELIANCE",
            contract=mock_contract,
            direction="BULL",
            underlying_ltp=2500.0,
            option_quote={"ask": 50.0, "bid": 49.0},
            flag_high=2510.0,
            flag_low=2490.0,
            now_ts=1000.0,
            quotes_cache=runner.quotes_cache,
        )

        # In simulation: multiple tracks opened!
        self.assertGreater(len(opened), 1)

        # Dispatch execution for designated track
        target_trade = next((t for t in opened if t.strategy_track == runner.designated_execution_track), None)
        self.assertIsNotNone(target_trade)
        self.assertEqual(target_trade.strategy_track, "DYNAMIC_TRIO")

        runner.multi_executor.place_order(
            symbol=target_trade.symbol,
            instrument_token=target_trade.contract_key,
            quantity=target_trade.quantity,
            transaction_type="BUY",
            order_type="MARKET",
            tag=target_trade.strategy_track,
            market_quote={"ask": 50.0},
        )

        # EXACTLY 1 order placed on broker!
        self.assertEqual(len(placed_orders), 1)
        self.assertEqual(placed_orders[0]["tag"], "DYNAMIC_TRIO")
        self.assertEqual(placed_orders[0]["transaction_type"], "BUY")

        # 2. Simulate closing an unrelated track (e.g. ITM)
        from strategies.momentum.portfolio import TradeRecord
        fake_itm_exit = TradeRecord(
            trade_id="TRD-ITM-1",
            strategy_track="ITM",
            symbol="RELIANCE",
            contract_key="NSE_FO|2460CE",
            contract_symbol="RELIANCE 2460 CE",
            direction="BULL",
            lots=1,
            quantity=250,
            entry_time=1000.0,
            exit_time=1010.0,
            entry_price=60.0,
            exit_price=70.0,
            exit_reason="TARGET_HIT",
        )

        runner._dispatch_execution_exits([fake_itm_exit])
        # Multi-executor MUST NOT place any sell order for ITM!
        self.assertEqual(len(placed_orders), 1)

        # 3. Simulate closing the designated DYNAMIC_TRIO track
        fake_dt_exit = TradeRecord(
            trade_id="TRD-DYNAMIC_TRIO-1",
            strategy_track="DYNAMIC_TRIO",
            symbol="RELIANCE",
            contract_key="NSE_FO|2500CE",
            contract_symbol="RELIANCE 2500 CE",
            direction="BULL",
            lots=1,
            quantity=250,
            entry_time=1000.0,
            exit_time=1020.0,
            entry_price=50.0,
            exit_price=65.0,
            exit_reason="TARGET_HIT",
        )

        runner._dispatch_execution_exits([fake_dt_exit])
        # Multi-executor placed EXACTLY 1 SELL order for DYNAMIC_TRIO!
        self.assertEqual(len(placed_orders), 2)
        self.assertEqual(placed_orders[1]["tag"], "DYNAMIC_TRIO")
        self.assertEqual(placed_orders[1]["transaction_type"], "SELL")

    def test_09_fixed_capital_non_compounding_tracks(self):
        """
        Verify that for every core track, a fixed-capital (_FIXED_CAP) non-compounding version exists.
        Verify that:
        1. Non-compounding tracks strictly cap available trading cash at starting_capital at max.
        2. Compounding tracks allow profits to expand cash beyond starting_capital.
        """
        from strategies.momentum.portfolio import CORE_STRATEGY_TRACKS, FIXED_CAP_SUFFIX, MultiTrackPortfolio

        # 1. Verify core tracks have a _FIXED_CAP partner in ALL_SIM_TRACKS
        self.assertEqual(len(CORE_STRATEGY_TRACKS), 9)
        portfolio = MultiTrackPortfolio(starting_capital=100_000.0)

        for core in CORE_STRATEGY_TRACKS:
            fixed_track = f"{core}{FIXED_CAP_SUFFIX}"
            self.assertIn(core, portfolio.tracks)
            self.assertIn(fixed_track, portfolio.tracks)
            self.assertTrue(portfolio.tracks[core].allow_compounding)
            self.assertFalse(portfolio.tracks[fixed_track].allow_compounding)

        # 2. Simulate profitable trades across both tracks
        from strategies.momentum.option_selector import OptionContract
        mock_contract = OptionContract(
            instrument_key="NSE_FO|ATM_OPT",
            trading_symbol="RELIANCE26NOV2500CE",
            underlying_symbol="RELIANCE",
            strike_price=2500.0,
            option_type="CE",
            expiry_date="2026-11-26",
            lot_size=200,
            tick_size=0.05,
        )

        # Entry at Ask ₹50.0 (Cost = 200 * 50 = ₹10,000)
        quotes = {
            "NSE_FO|ATM_OPT": {"ask": 50.0, "bid": 49.0, "ltp": 49.5},
            "RELIANCE": {"ltp": 2500.0},
        }

        opened = portfolio.open_position(
            symbol="RELIANCE",
            contract=mock_contract,
            direction="BULL",
            underlying_ltp=2500.0,
            option_quote=quotes["NSE_FO|ATM_OPT"],
            flag_high=2500.0,
            flag_low=2480.0,
            now_ts=1000.0,
            quotes_cache=quotes,
        )

        acct_cmp = portfolio.tracks["ATM"]
        acct_fix = portfolio.tracks["ATM_FIXED_CAP"]

        # Before exit: both deducted ₹10,000 cost -> cash = ₹90,000
        self.assertEqual(acct_cmp.cash, 90_000.0)
        self.assertEqual(acct_fix.cash, 90_000.0)

        # Huge profit exit: Bid jumps to ₹100.0 (Proceeds = 200 * 100 = ₹20,000; Profit = ₹10,000 - ₹60 = ₹9,940)
        quotes["NSE_FO|ATM_OPT"] = {"ask": 101.0, "bid": 100.0, "ltp": 100.0}
        quotes["RELIANCE"] = {"ltp": 2550.0}

        closed = portfolio.update_positions(quotes_cache=quotes, now_ts=1060.0)

        # Compounding track: cash = 90,000 + 20,000 = ₹110,000 (> starting_capital)
        self.assertEqual(acct_cmp.cash, 110_000.0)
        self.assertEqual(acct_cmp.net_realized, 9_940.0)

        # Fixed-capital non-compounding track: cash is strictly capped at starting_capital (₹100,000)
        self.assertEqual(acct_fix.cash, 100_000.0)
        # But net_realized and ROI reflect the full legitimate profit!
        self.assertEqual(acct_fix.net_realized, 9_940.0)

    def test_10_dynamic_trio_blended_scoring_two_thirds_one_third(self):
        """
        Verify that DYNAMIC_TRIO evaluates candidates using the 2/3 Non-Compounding + 1/3 Compounding blended formula:
            Score_blended = (2/3) * Score_fixed_cap + (1/3) * Score_compounding
        """
        portfolio = MultiTrackPortfolio(starting_capital=100_000.0, rolling_lookback_minutes=15)
        now_ts = 2000.0

        # Simulate closed trades manually into closed_trades for 3 base strategies
        from strategies.momentum.portfolio import TradeRecord

        def make_trade(track_name, net_pnl, exit_time):
            return TradeRecord(
                trade_id=f"T_{track_name}_{exit_time}",
                strategy_track=track_name,
                symbol="RELIANCE",
                contract_key="KEY",
                contract_symbol="SYM",
                direction="BULL",
                lots=1,
                quantity=100,
                entry_time=exit_time - 60,
                exit_time=exit_time,
                entry_price=50.0,
                exit_price=50.0 + (net_pnl + 60.0) / 100.0,
                gross_pnl=net_pnl + 60.0,
                brokerage=60.0,
                net_pnl=net_pnl,
                is_closed=True,
            )

        # Strategy 1: ATM
        # Fixed cap: +600, Compounding: +1200
        # Blended = (2/3)*600 + (1/3)*1200 = 400 + 400 = 800.0
        portfolio.tracks["ATM_FIXED_CAP"].closed_trades.append(make_trade("ATM_FIXED_CAP", 600.0, now_ts - 100))
        portfolio.tracks["ATM_FIXED_CAP"].net_realized = 600.0
        portfolio.tracks["ATM"].closed_trades.append(make_trade("ATM", 1200.0, now_ts - 100))
        portfolio.tracks["ATM"].net_realized = 1200.0

        # Strategy 2: ITM
        # Fixed cap: +900, Compounding: +300
        # Blended = (2/3)*900 + (1/3)*300 = 600 + 100 = 700.0
        portfolio.tracks["ITM_FIXED_CAP"].closed_trades.append(make_trade("ITM_FIXED_CAP", 900.0, now_ts - 100))
        portfolio.tracks["ITM_FIXED_CAP"].net_realized = 900.0
        portfolio.tracks["ITM"].closed_trades.append(make_trade("ITM", 300.0, now_ts - 100))
        portfolio.tracks["ITM"].net_realized = 300.0

        # Strategy 3: TRAIL_SL_ATM
        # Fixed cap: +450, Compounding: +600
        # Blended = (2/3)*450 + (1/3)*600 = 300 + 200 = 500.0
        portfolio.tracks["TRAIL_SL_ATM_FIXED_CAP"].closed_trades.append(make_trade("TRAIL_SL_ATM_FIXED_CAP", 450.0, now_ts - 100))
        portfolio.tracks["TRAIL_SL_ATM_FIXED_CAP"].net_realized = 450.0
        portfolio.tracks["TRAIL_SL_ATM"].closed_trades.append(make_trade("TRAIL_SL_ATM", 600.0, now_ts - 100))
        portfolio.tracks["TRAIL_SL_ATM"].net_realized = 600.0

        # Strategy 4: OTM (Lower score)
        # Fixed cap: +150, Compounding: +150 -> Blended = 150.0
        portfolio.tracks["OTM_FIXED_CAP"].closed_trades.append(make_trade("OTM_FIXED_CAP", 150.0, now_ts - 100))
        portfolio.tracks["OTM_FIXED_CAP"].net_realized = 150.0
        portfolio.tracks["OTM"].closed_trades.append(make_trade("OTM", 150.0, now_ts - 100))
        portfolio.tracks["OTM"].net_realized = 150.0

        # Trigger dynamic trio evaluation
        portfolio.update_dynamic_trio_leaderboard(now_ts=now_ts)

        # Ranked Champions should be: #1 ATM (800), #2 ITM (700), #3 TRAIL_SL_ATM (500)
        self.assertEqual(portfolio.dynamic_trio_champions, ["ATM", "ITM", "TRAIL_SL_ATM"])

        # Check that weights are allocated proportionally with diversification floor
        weights = portfolio.dynamic_trio_weights
        self.assertIn("ATM", weights)
        self.assertIn("ITM", weights)
        self.assertIn("TRAIL_SL_ATM", weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=2)
        # Highest ranked (ATM) should have highest weight
        self.assertGreater(weights["ATM"], weights["ITM"])
        self.assertGreater(weights["ITM"], weights["TRAIL_SL_ATM"])

    def test_11_simulation_matrix_formatting_and_width(self):
        """Verify that every row and header in the 33-track simulation matrix is exactly 148 characters."""
        from strategies.momentum.dashboard import (
            render_performance_matrix,
            render_focused_strategy_table,
            render_positions_summary,
            visible_length,
        )

        from strategies.momentum.portfolio import ALL_SIM_TRACKS
        port = MultiTrackPortfolio(starting_capital=250000.0)
        self.assertEqual(len(port.tracks), len(ALL_SIM_TRACKS))

        quotes = {
            "NSE_FO|NIFTY_ATM": {"bid": 100.0, "ask": 102.0, "ltp": 101.0},
            "NSE_FO|BANKNIFTY_ITM": {"bid": 250.0, "ask": 252.0, "ltp": 251.0},
        }

        # 1. Performance Matrix Table
        matrix_str = render_performance_matrix(port, quotes, mode_name="paper")
        matrix_lines = matrix_str.split("\n")
        self.assertGreater(len(matrix_lines), 20)

        for idx, line in enumerate(matrix_lines):
            if idx == 1:
                # Banner title line
                continue
            vlen = visible_length(line)
            self.assertEqual(
                vlen,
                148,
                f"Matrix line {idx} visible length {vlen} != 148: {line}",
            )

        # 2. Focused Strategy Table
        focused_str = render_focused_strategy_table(port, quotes, filter_tag="ATM", mode_name="paper")
        for idx, line in enumerate(focused_str.split("\n")):
            if idx == 1 or "ACTIVE POSITIONS" in line or "No open positions" in line:
                continue
            vlen = visible_length(line)
            self.assertEqual(
                vlen,
                148,
                f"Focused line {idx} visible length {vlen} != 148: {line}",
            )

        # 3. Expanded Positions Table
        pos_str = render_positions_summary(port, quotes, show_positions=True, mode_name="paper")
        for idx, line in enumerate(pos_str.split("\n")):
            if "EXPANDED ACTIVE POSITIONS" in line:
                continue
            vlen = visible_length(line)
            self.assertEqual(
                vlen,
                148,
                f"Positions line {idx} visible length {vlen} != 148: {line}",
            )

    def test_12_per_mode_simulation_matrices(self):
        """Verify per-mode simulation matrix routing in scanner dashboard."""
        from strategies.momentum.dashboard import render_scanner_dashboard
        from execution.multi_executor import create_multi_executor

        p_paper = MultiTrackPortfolio(starting_capital=250000.0, mode="paper")
        p_delayed = MultiTrackPortfolio(starting_capital=250000.0, mode="paper_delayed", min_delay_ms=700, max_delay_ms=2500)
        portfolios_by_mode = {"paper": p_paper, "paper_delayed": p_delayed}

        multi_exec = create_multi_executor(modes_input="paper,paper_delayed")

        # Default view with multiple active modes renders both matrices
        out_default = render_scanner_dashboard(
            monitored_count=5,
            nifty_stats={"regime": "NEUTRAL", "pct_change": 0.0},
            fresh_alerts=[],
            confirmed_setups=[],
            portfolio=p_paper,
            quotes_cache={},
            mode="paper,paper_delayed",
            multi_executor=multi_exec,
            portfolios_by_mode=portfolios_by_mode,
        )
        self.assertIn("STRATEGY SIMULATION MATRIX [PAPER]", out_default)
        self.assertIn("STRATEGY SIMULATION MATRIX [PAPER_DELAYED]", out_default)

        # Filtered view for paper only
        out_paper = render_scanner_dashboard(
            monitored_count=5,
            nifty_stats={"regime": "NEUTRAL", "pct_change": 0.0},
            fresh_alerts=[],
            confirmed_setups=[],
            portfolio=p_paper,
            quotes_cache={},
            view_mode="paper",
            multi_executor=multi_exec,
            portfolios_by_mode=portfolios_by_mode,
        )
        self.assertIn("STRATEGY SIMULATION MATRIX [PAPER]", out_paper)
        self.assertNotIn("STRATEGY SIMULATION MATRIX [PAPER_DELAYED]", out_paper)

        # Filtered view for paper_delayed only
        out_delayed = render_scanner_dashboard(
            monitored_count=5,
            nifty_stats={"regime": "NEUTRAL", "pct_change": 0.0},
            fresh_alerts=[],
            confirmed_setups=[],
            portfolio=p_paper,
            quotes_cache={},
            view_mode="paper_delayed",
            multi_executor=multi_exec,
            portfolios_by_mode=portfolios_by_mode,
        )
        self.assertIn("STRATEGY SIMULATION MATRIX [PAPER_DELAYED]", out_delayed)
        self.assertNotIn("STRATEGY SIMULATION MATRIX [PAPER]", out_delayed)

    def test_13_paper_delayed_portfolio_latency_modeling(self):
        """Verify that paper_delayed portfolio queues pending orders and executes at updated quotes after delay."""
        p_delayed = MultiTrackPortfolio(
            starting_capital=250000.0,
            mode="paper_delayed",
            min_delay_ms=300.0,
            max_delay_ms=800.0,
        )
        self.assertEqual(len(p_delayed.pending_orders), 0)

        # Mock contract and initial quote at t = 100.0
        class MockContract:
            instrument_key = "NSE_FO|RELIANCE26NOV2600CE"
            trading_symbol = "RELIANCE26NOV2600CE"
            lot_size = 250

        c = MockContract()
        q_init = {"ask": 50.0, "bid": 49.0, "ltp": 50.0}
        quotes_cache = {c.instrument_key: q_init}

        # Open position at t = 100.0
        opened = p_delayed.open_position(
            symbol="RELIANCE",
            contract=c,
            direction="BULL",
            underlying_ltp=2600.0,
            option_quote=q_init,
            flag_high=2610.0,
            flag_low=2590.0,
            now_ts=100.0,
            quotes_cache=quotes_cache,
        )
        # Latency delay active: opened trades should be empty initially, pending orders populated
        self.assertEqual(len(opened), 0)
        self.assertEqual(len(p_delayed.pending_orders), 1)
        self.assertTrue(p_delayed.is_position_active("RELIANCE"))

        # Update at t = 100.1 (100ms in): order still pending
        p_delayed.update_positions(quotes_cache, now_ts=100.1)
        self.assertEqual(len(p_delayed.pending_orders), 1)
        open_cnt = sum(len(a.open_positions) for a in p_delayed.tracks.values())
        self.assertEqual(open_cnt, 0)

        # Market moves: ask increases to 54.0 due to breakout velocity, bid rises to 53.5
        quotes_cache[c.instrument_key]["ask"] = 54.0
        quotes_cache[c.instrument_key]["bid"] = 53.5

        # Update at t = 101.0 (1,000ms in): delay has elapsed (target <= 100.8)
        p_delayed.update_positions(quotes_cache, now_ts=101.0)
        self.assertEqual(len(p_delayed.pending_orders), 0)
        open_cnt_after = sum(len(a.open_positions) for a in p_delayed.tracks.values())
        self.assertEqual(open_cnt_after, len(ALL_SIM_TRACKS))

        # Filled at updated quote (₹54.00) reflecting realistic slippage
        atm_trade = list(p_delayed.tracks["ATM"].open_positions.values())[0]
        self.assertEqual(atm_trade.entry_price, 54.0)

    def test_14_eod_freeze_and_auto_squareoff(self):
        """
        Verify End-Of-Day (EOD) RMS auto-squareoff & entry freeze behavior:
        1. RiskManager: Identifies 15:15 IST freeze and 15:20 IST auto-squareoff.
        2. MultiTrackPortfolio: Blocks entries at >= 15:15 and liquidates all 33 simulation tracks at >= 15:20.
        3. MultiExecutionManager: Auto-freezes all execution adapters and squares off open positions.
        4. MCX Mode: Respects commodity market hours (23:15 freeze, 23:25 squareoff).
        """
        from execution.multi_executor import MultiExecutionManager
        from execution.paper.paper_adapter import PaperExecutionAdapter
        from execution.paper.paper_delayed_adapter import PaperDelayedExecutionAdapter
        from strategies.momentum.risk import RiskManager

        ist = timezone(timedelta(hours=5, minutes=30))
        t_1430 = datetime(2026, 11, 9, 14, 30, 0, tzinfo=ist).timestamp()
        t_1515 = datetime(2026, 11, 9, 15, 15, 0, tzinfo=ist).timestamp()
        t_1520 = datetime(2026, 11, 9, 15, 20, 0, tzinfo=ist).timestamp()

        # -------------------------------------------------------------
        # 1. Test RiskManager EOD Rules
        # -------------------------------------------------------------
        rm = RiskManager(eod_square_off_time="15:20", eod_freeze_time="15:15")
        self.assertFalse(rm.is_eod_freeze(t_1430))
        self.assertFalse(rm.is_eod_squareoff(t_1430))

        self.assertTrue(rm.is_eod_freeze(t_1515))
        self.assertFalse(rm.is_eod_squareoff(t_1515))

        self.assertTrue(rm.is_eod_freeze(t_1520))
        self.assertTrue(rm.is_eod_squareoff(t_1520))

        # Position exit evaluation at 15:20 IST
        dummy_pos = {
            "entry_price": 50.0,
            "quantity": 250,
            "direction": "BULL",
            "entry_time": t_1430,
            "peak_price": 50.0,
            "current_sl": 42.5,
            "underlying_sl": 2580.0,
            "strategy_type": "ATM",
        }
        should_exit, reason = rm.evaluate_exit(dummy_pos, current_option_price=51.0, current_underlying_price=2600.0, now_ts=t_1520)
        self.assertTrue(should_exit)
        self.assertIn("EOD_MIS_SQUAREOFF", reason)
        self.assertIn("15:20", reason)

        # -------------------------------------------------------------
        # 2. Test MultiTrackPortfolio Freeze & Squareoff across 33 Tracks
        # -------------------------------------------------------------
        port = MultiTrackPortfolio(
            starting_capital=250000.0,
            eod_square_off_time="15:20",
            eod_freeze_time="15:15",
        )

        class MockContract:
            instrument_key = "NSE_FO|TCS26NOV3500CE"
            trading_symbol = "TCS26NOV3500CE"
            lot_size = 250

        contract = MockContract()
        q = {"bid": 48.0, "ask": 49.0, "ltp": 48.5}
        quotes = {contract.instrument_key: q, "TCS": {"ltp": 3500.0}}

        # Open positions at 14:30 IST (succeeds)
        opened = port.open_position(
            symbol="TCS",
            contract=contract,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=q,
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=t_1430,
            quotes_cache=quotes,
        )
        self.assertEqual(len(opened), len(ALL_SIM_TRACKS))
        self.assertTrue(port.is_position_active("TCS"))

        # At 15:15 IST: Entry freeze blocks new position opens
        opened_blocked = port.open_position(
            symbol="INFY",
            contract=contract,
            direction="BULL",
            underlying_ltp=1800.0,
            option_quote=q,
            flag_high=1810.0,
            flag_low=1790.0,
            now_ts=t_1515,
            quotes_cache=quotes,
        )
        self.assertEqual(len(opened_blocked), 0)
        self.assertFalse(port.is_position_active("INFY"))

        # At 15:20 IST: Check EOD squareoff closes all 33 simulation tracks
        closed = port.check_eod_squareoff(quotes, now_ts=t_1520)
        self.assertEqual(len(closed), len(ALL_SIM_TRACKS))
        for c_trd in closed:
            self.assertTrue(c_trd.is_closed)
            self.assertIn("EOD_MIS_SQUAREOFF", c_trd.exit_reason)

        remaining_open = sum(len(a.open_positions) for a in port.tracks.values())
        self.assertEqual(remaining_open, 0)
        self.assertFalse(port.is_position_active("TCS"))

        # -------------------------------------------------------------
        # 3. Test MultiExecutionManager Broadcast Squareoff
        # -------------------------------------------------------------
        paper_adapter = PaperExecutionAdapter(initial_capital=250000.0)
        paper_delayed_adapter = PaperDelayedExecutionAdapter(initial_capital=250000.0, async_execution=False)
        multi_mgr = MultiExecutionManager({
            "paper": paper_adapter,
            "paper_delayed": paper_delayed_adapter,
        })

        # Place positions at 14:30
        paper_adapter.place_order(
            symbol="TCS",
            instrument_token=contract.instrument_key,
            quantity=175,
            transaction_type="BUY",
            order_type="MARKET",
            market_quote=q,
        )
        paper_delayed_adapter.place_order(
            symbol="TCS",
            instrument_token=contract.instrument_key,
            quantity=175,
            transaction_type="BUY",
            order_type="MARKET",
            market_quote=q,
        )
        self.assertEqual(len(paper_adapter.get_positions()), 1)
        self.assertEqual(len(paper_delayed_adapter.get_positions()), 1)

        # Trigger EOD freeze at 15:15
        multi_mgr.check_eod_squareoff(now_ts=t_1515, quotes_cache=quotes, eod_time="15:20", freeze_time="15:15")
        self.assertTrue(multi_mgr.is_frozen("all"))
        self.assertTrue(multi_mgr.is_frozen("paper"))
        self.assertTrue(multi_mgr.is_frozen("paper_delayed"))

        # Trigger EOD squareoff at 15:20
        sq_orders = multi_mgr.check_eod_squareoff(now_ts=t_1520, quotes_cache=quotes, eod_time="15:20", freeze_time="15:15")
        self.assertEqual(len(sq_orders), 2)
        self.assertEqual(len(paper_adapter.get_positions()), 0)
        self.assertEqual(len(paper_delayed_adapter.get_positions()), 0)

        # -------------------------------------------------------------
        # 4. Test MCX Mode Timing
        # -------------------------------------------------------------
        t_2315 = datetime(2026, 11, 9, 23, 15, 0, tzinfo=ist).timestamp()
        t_2325 = datetime(2026, 11, 9, 23, 25, 0, tzinfo=ist).timestamp()
        mcx_rm = RiskManager(is_mcx=True)
        # Not frozen or squared off at 15:20 IST
        self.assertFalse(mcx_rm.is_eod_freeze(t_1520))
        self.assertFalse(mcx_rm.is_eod_squareoff(t_1520))
        # Freezes at 23:15 IST, squares off at 23:25 IST
        self.assertTrue(mcx_rm.is_eod_freeze(t_2315))
        self.assertFalse(mcx_rm.is_eod_squareoff(t_2315))
        self.assertTrue(mcx_rm.is_eod_squareoff(t_2325))

    def test_15_market_hours_and_cas_auction_guard(self):
        """
        Verify Pre-Market Call Auction Session (CAS 09:00 - 09:15 IST),
        Post-Market (>= 15:30 IST), and Weekend order gates:
        1. Pre-market CAS (09:10 IST): Trade entries strictly blocked across simulation and live adapters.
        2. Continuous Trading (09:15 IST): Trade entries allowed.
        3. Post-market (15:35 IST): Trade entries blocked.
        4. Weekend (Sunday): Orders blocked with REJECTED_MARKET_CLOSED.
        """
        from execution.live.live_adapter import GrowwLiveAdapter
        from execution.multi_executor import MultiExecutionManager
        from execution.paper.paper_adapter import PaperExecutionAdapter
        from execution.paper.paper_delayed_adapter import PaperDelayedExecutionAdapter
        from strategies.momentum.risk import RiskManager

        ist = timezone(timedelta(hours=5, minutes=30))
        # Monday Nov 9, 2026 timestamps
        t_cas = datetime(2026, 11, 9, 9, 12, 0, tzinfo=ist).timestamp()      # 09:12 CAS Auction
        t_open = datetime(2026, 11, 9, 9, 15, 0, tzinfo=ist).timestamp()     # 09:15 Continuous Open
        t_post = datetime(2026, 11, 9, 15, 35, 0, tzinfo=ist).timestamp()    # 15:35 Post-Market Closed
        t_sun = datetime(2026, 11, 8, 11, 0, 0, tzinfo=ist).timestamp()      # Sunday Weekend

        # 1. RiskManager market session identification
        rm = RiskManager()
        sess_cas, desc_cas = rm.get_market_session(t_cas)
        self.assertEqual(sess_cas, "PRE_MARKET_CAS")
        self.assertTrue(rm.is_pre_market_auction(t_cas))
        self.assertFalse(rm.is_market_open(t_cas))
        allowed_cas, reason_cas = rm.is_entry_allowed(t_cas)
        self.assertFalse(allowed_cas)
        self.assertIn("CAS active until 09:15", reason_cas)

        sess_open, _ = rm.get_market_session(t_open)
        self.assertEqual(sess_open, "NORMAL_TRADING")
        self.assertTrue(rm.is_market_open(t_open))
        self.assertFalse(rm.is_pre_market_auction(t_open))
        allowed_open, _ = rm.is_entry_allowed(t_open)
        self.assertTrue(allowed_open)

        sess_post, _ = rm.get_market_session(t_post)
        self.assertEqual(sess_post, "POST_MARKET")
        self.assertFalse(rm.is_entry_allowed(t_post)[0])

        sess_sun, _ = rm.get_market_session(t_sun)
        self.assertEqual(sess_sun, "WEEKEND")
        self.assertFalse(rm.is_entry_allowed(t_sun)[0])

        # 2. MultiTrackPortfolio blocks entries during CAS
        port = MultiTrackPortfolio(starting_capital=250000.0)
        class MockContract:
            instrument_key = "NSE_FO|TCS26NOV3500CE"
            trading_symbol = "TCS26NOV3500CE"
            lot_size = 250
        c = MockContract()
        q = {"bid": 50.0, "ask": 51.0, "ltp": 50.5}

        # Attempt to open position during CAS (09:12 IST)
        opened_cas = port.open_position(
            symbol="TCS",
            contract=c,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=q,
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=t_cas,
        )
        self.assertEqual(len(opened_cas), 0)
        self.assertFalse(port.is_position_active("TCS"))

        # Open position at 09:15 continuous open
        opened_open = port.open_position(
            symbol="TCS",
            contract=c,
            direction="BULL",
            underlying_ltp=3500.0,
            option_quote=q,
            flag_high=3510.0,
            flag_low=3490.0,
            now_ts=t_open,
        )
        self.assertEqual(len(opened_open), len(ALL_SIM_TRACKS))
        self.assertTrue(port.is_position_active("TCS"))

        # 3. Live and Paper execution adapters reject orders during CAS and off-hours
        live = GrowwLiveAdapter(enforce_market_hours=True)
        paper = PaperExecutionAdapter(enforce_market_hours=True)
        delayed = PaperDelayedExecutionAdapter(enforce_market_hours=True, async_execution=False)

        # CAS Auction Rejections
        res_live_cas = live.place_order(symbol="TCS", instrument_token=c.instrument_key, quantity=175, transaction_type="BUY", market_quote=q)
        # Note: res_live_cas was called with real time or passed timestamp
        res_live_cas_ts = live.place_order(symbol="TCS", instrument_token=c.instrument_key, quantity=175, transaction_type="BUY", market_quote=q, stop_loss_price=None)
        # Pass simulated CAS timestamp
        def test_order_at_ts(adapter, ts_val):
            prev_time = time.time
            try:
                time.time = lambda: ts_val
                return adapter.place_order(symbol="TCS", instrument_token=c.instrument_key, quantity=175, transaction_type="BUY", market_quote=q)
            finally:
                time.time = prev_time

        res_live = test_order_at_ts(live, t_cas)
        self.assertEqual(res_live.get("status"), "REJECTED_PRE_MARKET_CAS")
        self.assertIn("09:15", res_live.get("error", ""))
        self.assertIn("CAS", res_live.get("error", ""))

        res_paper = test_order_at_ts(paper, t_cas)
        self.assertEqual(res_paper.get("status"), "REJECTED_PRE_MARKET_CAS")

        res_delayed = test_order_at_ts(delayed, t_cas)
        self.assertEqual(res_delayed.get("status"), "REJECTED_PRE_MARKET_CAS")

        # Weekend Rejection
        res_sun = test_order_at_ts(live, t_sun)
        self.assertEqual(res_sun.get("status"), "REJECTED_MARKET_CLOSED")
        self.assertIn("Weekend", res_sun.get("error", ""))

        # Post-Market Rejection
        res_post = test_order_at_ts(live, t_post)
        self.assertEqual(res_post.get("status"), "REJECTED_MARKET_CLOSED")

        # Multi-Executor Broadcast during CAS
        multi_mgr = MultiExecutionManager(
            adapters={"live": live, "paper": paper},
            enforce_market_hours=True,
        )
        prev_t = time.time
        try:
            time.time = lambda: t_cas
            res_multi = multi_mgr.place_order(symbol="TCS", instrument_token=c.instrument_key, quantity=175, transaction_type="BUY", market_quote=q)
            self.assertEqual(res_multi["live"]["status"], "BLOCKED")
            self.assertEqual(res_multi["paper"]["status"], "BLOCKED")
            self.assertIn("CAS", res_multi["live"]["reason"])
        finally:
            time.time = prev_t

    def test_16_execution_freeze_blocks_portfolio_and_adapter_entries(self):
        """
        Verify that when a freeze flag is present (.freeze_all or .freeze_<mode>),
        MultiTrackPortfolio and MultiExecutionManager completely block all new position entries,
        while existing positions remain active for trailing stops and target exits.
        """
        from execution.multi_executor import MultiExecutionManager
        from execution.paper.paper_adapter import PaperExecutionAdapter

        os.makedirs("logs", exist_ok=True)
        flag_all = "logs/.freeze_all"
        flag_paper = "logs/.freeze_paper"

        # Ensure clean state across all possible freeze flags
        for m in ("all", "paper", "paper_delayed", "live", "sim"):
            ff = f"logs/.freeze_{m}"
            if os.path.exists(ff):
                os.remove(ff)

        try:
            port = MultiTrackPortfolio(starting_capital=250000.0, mode="paper")
            self.assertFalse(port.is_frozen())

            class MockContract:
                instrument_key = "NSE_FO|INFY26NOV1800CE"
                trading_symbol = "INFY26NOV1800CE"
                lot_size = 400
            c = MockContract()
            q = {"bid": 25.0, "ask": 25.5, "ltp": 25.2}

            # 1. Normal entry without freeze works
            opened_normal = port.open_position(
                symbol="INFY",
                contract=c,
                direction="BULL",
                underlying_ltp=1800.0,
                option_quote=q,
                flag_high=1810.0,
                flag_low=1790.0,
                now_ts=1000.0,
            )
            self.assertEqual(len(opened_normal), len(ALL_SIM_TRACKS))
            self.assertTrue(port.is_position_active("INFY"))

            # 2. Freeze paper mode via flag file
            with open(flag_paper, "w") as f:
                f.write("frozen\n")

            self.assertTrue(port.is_frozen())

            # Attempt to open a NEW symbol (TCS) while frozen
            c_tcs = MockContract()
            c_tcs.instrument_key = "NSE_FO|TCS26NOV3500CE"
            opened_frozen = port.open_position(
                symbol="TCS",
                contract=c_tcs,
                direction="BULL",
                underlying_ltp=3500.0,
                option_quote=q,
                flag_high=3510.0,
                flag_low=3490.0,
                now_ts=1000.0,
            )
            self.assertEqual(len(opened_frozen), 0, "Freeze failed: open_position allowed entries while frozen!")
            self.assertFalse(port.is_position_active("TCS"))

            # 3. Existing INFY position continues trailing and exits normally
            q_exit = {"bid": 30.0, "ask": 30.5, "ltp": 30.0}
            quotes = {c.instrument_key: q_exit}
            closed = port.update_positions(quotes)
            self.assertGreater(len(closed), 0, "Frozen portfolio must continue trailing and exiting active positions!")

            # 4. MultiExecutionManager place_order blocks new entries
            paper_adapter = PaperExecutionAdapter()
            multi_mgr = MultiExecutionManager(adapters={"paper": paper_adapter})
            multi_mgr.check_freeze_flags()
            self.assertTrue(multi_mgr.is_frozen("paper"))

            res = multi_mgr.place_order(
                symbol="TCS",
                instrument_token=c_tcs.instrument_key,
                quantity=175,
                transaction_type="BUY",
                market_quote=q,
            )
            self.assertEqual(res["paper"]["status"], "BLOCKED")
            self.assertIn("FROZEN", res["paper"]["reason"])

        finally:
            for f in (flag_all, flag_paper):
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    def test_17_midday_volume_expansion_and_acceleration_gate(self):
        """
        Verify elimination of midday false positives:
        1. Volume deceleration (A_vol < req_accel, e.g. 0.75x) is rejected even if RVOL_5m is high.
        2. In midday (after 10:00 AM IST), stricter thresholds (RVOL_5m >= 1.35x, A_vol >= 1.15x) are enforced.
        """
        tracker = FlagPatternTracker(
            min_breakout_rvol_5m=1.20,
            min_breakout_accel=1.05,
            min_breakout_pace=1.05,
            midday_rvol_5m=1.35,
            midday_accel=1.15,
        )
        key = "NSE_EQ|BANKINDIA"

        # Midday timestamp: 10:15:00 IST on Nov 9, 2026
        midday_dt = datetime(2026, 11, 9, 10, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        t_midday = midday_dt.timestamp()

        # 1. Establish Bull Pole in midday
        p_pole = tracker.update(
            instrument_key=key,
            ltp=140.0,
            rvol_5m=2.5,
            rvol_cum=1.5,
            a_vol=1.5,
            pace=1.3,
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=t_midday,
        )
        self.assertTrue(p_pole["has_bull_pole"])

        # 2. Coiling cycles
        for i in range(1, 3):
            tracker.update(
                instrument_key=key,
                ltp=139.5,
                rvol_5m=0.70,
                rvol_cum=1.1,
                a_vol=0.70,
                pace=0.60,
                range_pos=90.0,
                is_hod=False,
                is_lod=False,
                now_ts=t_midday + i * 60,
            )

        # 3. Midday attempt with decelerating volume (A_vol = 0.75x) but high RVOL_5m (2.0x):
        # Even with high RVOL, dying/decelerating volume MUST be rejected!
        state_decel = tracker.update(
            instrument_key=key,
            ltp=140.5,
            rvol_5m=2.0,
            rvol_cum=1.5,
            a_vol=0.75,
            pace=1.2,
            range_pos=96.0,
            is_hod=True,
            is_lod=False,
            now_ts=t_midday + 180,
        )
        self.assertFalse(state_decel["bull_breakout"], "Decelerating volume (A_vol=0.75) must NOT fire breakout!")
        self.assertTrue(state_decel["bull_weak_break"], "Decelerating volume breakout attempt must be marked as weak break!")

        # In scoring, must be capped as weak break
        score_res = calculate_setup_score(
            ltp=140.5,
            high=140.5,
            low=135.0,
            close=136.0,
            volume_stats={"rvol_5m": 2.0, "rvol_cum": 1.5, "a_vol": 0.75, "pace": 1.2, "z_vol": 0.5},
            pattern_state=state_decel,
        )
        self.assertEqual(score_res["stage"], "👀 WEAK BREAK / NO VOL")
        self.assertLessEqual(score_res["score"], 60.0)

        # 4. Midday attempt with moderate opening RVOL (1.25x) that is below midday floor (1.35x)
        state_mid_low_vol = tracker.update(
            instrument_key=key,
            ltp=140.5,
            rvol_5m=1.25,
            rvol_cum=1.2,
            a_vol=1.20,
            pace=1.10,
            range_pos=96.0,
            is_hod=True,
            is_lod=False,
            now_ts=t_midday + 240,
        )
        self.assertFalse(state_mid_low_vol["bull_breakout"], "Midday RVOL below 1.35 must NOT fire breakout!")

        # 5. Legitimate Midday Breakout: RVOL_5m >= 1.35x AND A_vol >= 1.15x AND Pace >= 1.05x
        state_mid_legit = tracker.update(
            instrument_key=key,
            ltp=141.0,
            rvol_5m=1.60,
            rvol_cum=1.3,
            a_vol=1.35,
            pace=1.25,
            range_pos=98.0,
            is_hod=True,
            is_lod=False,
            now_ts=t_midday + 300,
        )
        self.assertTrue(state_mid_legit["bull_breakout"], "Midday breakout with expanding RVOL & acceleration MUST fire!")

    def test_18_bear_breakdown_requires_new_lows_and_lod_confirmation(self):
        """
        Verify elimination of bear breakdown false positives:
        1. When price breaks below flag level but volume is dry (A_vol < 1.05 / RVOL_5m < 1.20),
           bear_breakdown is strictly BLOCKED.
        2. When price breaks below flag level but is NOT making new lows / LOD (e.g. range_pos=25%),
           bear_breakdown is strictly BLOCKED.
        3. Only when price penetrates the bear pole price AND is at session low (is_lod or range_pos <= 15.0%)
           with volume expansion and acceleration, does bear_breakdown fire.
        """
        tracker = FlagPatternTracker()
        key = "NSE_EQ|BHARATFORG"
        t0 = 2000.0

        # 1. Establish Bear Pole down to 100.0 (session low is 100.0)
        p_pole = tracker.update(
            instrument_key=key,
            ltp=100.0,
            rvol_5m=2.2,
            rvol_cum=1.5,
            a_vol=1.4,
            pace=1.3,
            range_pos=5.0,
            is_hod=False,
            is_lod=True,
            now_ts=t0,
        )
        self.assertTrue(p_pole["has_bear_pole"])

        # 2. Price bounces UP into consolidation flag (bounces between 100.0 and 101.5)
        for i in range(1, 4):
            tracker.update(
                instrument_key=key,
                ltp=101.0,
                rvol_5m=0.60,
                rvol_cum=1.1,
                a_vol=0.70,
                pace=0.60,
                range_pos=25.0,
                is_hod=False,
                is_lod=False,
                now_ts=t0 + i * 60,
            )

        # 3. Fluff Bear Breakdown: Price dips below flag low (99.8 < 100.0), BUT volume is DRY:
        # RVOL_5m = 0.65x, A_vol = 0.70x
        state_fluff = tracker.update(
            instrument_key=key,
            ltp=99.8,
            rvol_5m=0.65,
            rvol_cum=1.0,
            a_vol=0.70,
            pace=0.70,
            range_pos=5.0,
            is_hod=False,
            is_lod=True,
            now_ts=t0 + 260,
        )
        self.assertFalse(state_fluff["bear_breakdown"], "Dry volume breakdown must NOT fire bear breakdown!")
        self.assertTrue(state_fluff["bear_weak_break"], "Dry volume breakdown must be flagged as weak crack!")

        # In scoring, weak crack is capped at <= 60
        score_fluff = calculate_setup_score(
            ltp=99.8,
            high=105.0,
            low=99.8,
            close=104.0,
            volume_stats={"rvol_5m": 0.65, "rvol_cum": 1.0, "a_vol": 0.70, "pace": 0.70, "z_vol": 0.2},
            pattern_state=state_fluff,
        )
        self.assertEqual(score_fluff["stage"], "👀 WEAK CRACK / NO VOL")
        self.assertLessEqual(score_fluff["score"], 60.0)

        # 4. False Breakdown: Price penetrates flag low (99.7 < 99.8) with volume, BUT range_pos is 25.0% (not at new lows/LOD)
        state_no_lod = tracker.update(
            instrument_key=key,
            ltp=99.7,
            rvol_5m=1.80,
            rvol_cum=1.3,
            a_vol=1.30,
            pace=1.20,
            range_pos=25.0,
            is_hod=False,
            is_lod=False,
            now_ts=t0 + 300,
        )
        self.assertFalse(state_no_lod["bear_breakdown"], "Breakdown without LOD / extreme low confirmation must be BLOCKED!")
        self.assertTrue(state_no_lod["bear_weak_break"], "Breakdown without new low must be flagged as weak crack!")

        # 5. Legitimate Bear Breakdown: Price breaches 100.0 to 99.5 (makes NEW LOW)
        # with is_lod=True / range_pos <= 10.0%, with volume expansion & acceleration
        state_legit_bear = tracker.update(
            instrument_key=key,
            ltp=99.5,
            rvol_5m=1.90,
            rvol_cum=1.4,
            a_vol=1.40,
            pace=1.25,
            range_pos=0.0,
            is_hod=False,
            is_lod=True,
            now_ts=t0 + 360,
        )
        self.assertTrue(state_legit_bear["bear_breakdown"], "Legitimate breakdown making new low MUST fire bear_breakdown!")

        score_legit = calculate_setup_score(
            ltp=99.5,
            high=105.0,
            low=99.5,
            close=104.0,
            volume_stats={"rvol_5m": 1.90, "rvol_cum": 1.4, "a_vol": 1.40, "pace": 1.25, "z_vol": 0.8},
            pattern_state=state_legit_bear,
        )
        self.assertEqual(score_legit["stage"], "💥 BEAR BREAKDOWN")
        self.assertGreaterEqual(score_legit["score"], 70.0)

    def test_missing_volume_baseline_does_not_assume_dummy_values(self):
        """Verify that missing/empty volume profile never assumes 1000 or 10000; marks absent and blocks fake RVOL breakouts."""
        from strategies.momentum.scanner import RollingVolumeTracker
        from strategies.momentum.scoring import calculate_setup_score
        from strategies.momentum.patterns import FlagPatternTracker

        tracker = RollingVolumeTracker()
        now_ts = 1700000000.0  # mock timestamp

        # Feed ticks with a completely empty profile (e.g. historical candles failed)
        empty_profile = {}
        tracker.update("STOCK_X", 1000, 100.0, empty_profile, now_ts=now_ts)
        v_stats = tracker.update("STOCK_X", 50000, 105.0, empty_profile, now_ts=now_ts + 60)

        # Baseline must be marked absent; RVOL must be 0.0, NOT 50x!
        self.assertFalse(v_stats["has_baseline"])
        self.assertEqual(v_stats["rvol_5m"], 0.0)
        self.assertEqual(v_stats["rvol_cum"], 0.0)
        self.assertEqual(v_stats["pace"], 0.0)

        # Pattern tracker must reject breakout when baseline is missing
        flag_tracker = FlagPatternTracker()
        p_state = flag_tracker.update(
            instrument_key="STOCK_X",
            ltp=105.0,
            rvol_5m=v_stats["rvol_5m"],
            rvol_cum=v_stats["rvol_cum"],
            a_vol=v_stats["a_vol"],
            pace=v_stats["pace"],
            range_pos=95.0,
            is_hod=True,
            is_lod=False,
            now_ts=now_ts + 60,
            has_baseline=v_stats["has_baseline"],
        )
        self.assertFalse(p_state["is_vol_inflated"])
        self.assertFalse(p_state["bull_breakout"])

        # Setup scoring must not award fake volume points or trigger breakout stage
        score_res = calculate_setup_score(
            ltp=105.0,
            high=105.0,
            low=95.0,
            close=98.0,
            volume_stats=v_stats,
            pattern_state=p_state,
        )
        self.assertFalse(score_res["has_baseline"])
        self.assertNotEqual(score_res["stage"], "💥 BULL BREAKOUT")
        self.assertNotEqual(score_res["stage"], "🚀 BREAKOUT")

    def test_20_custom_configuration_injection_and_test_configs(self):
        """Verify configurables can be adjusted via JSON files or in-memory config dictionaries."""
        import json
        from strategies.momentum.strategy import MomentumStrategyRunner
        from strategies.momentum.scoring import calculate_setup_score
        from strategies.momentum.portfolio import MultiTrackPortfolio

        # 1. Verify existence and valid JSON of test configs
        self.assertTrue(os.path.exists("config/strategies_test.json"))
        self.assertTrue(os.path.exists("config/execution_test.json"))

        with open("config/strategies_test.json", "r", encoding="utf-8") as f:
            test_strat_cfg = json.load(f)
        with open("config/execution_test.json", "r", encoding="utf-8") as f:
            test_exec_cfg = json.load(f)

        self.assertEqual(test_strat_cfg["strategies"]["momentum"]["minimum_view_score"], 50)
        self.assertEqual(test_exec_cfg["execution"]["paper_delayed"]["min_delay_ms"], 10.0)

        # 2. Verify calculate_setup_score responds dynamically to custom configuration
        v_stats = {"rvol_5m": 2.2, "rvol_cum": 1.5, "a_vol": 1.4, "pace": 1.5, "z_vol": 1.0, "has_baseline": True}
        
        # Default scoring
        res_default = calculate_setup_score(
            ltp=103.0, high=103.0, low=100.0, close=100.0, volume_stats=v_stats
        )

        # Custom scoring with 50-pt Pillar 1 weight
        custom_cfg = {
            "scoring": {
                "pillar1_volume_rvol_weight": 50.0,
                "pillar1_rvol_pts": 35.0,
                "pillar1_rvol_min": 0.50,
                "pillar1_rvol_max": 2.50,
            }
        }
        res_custom = calculate_setup_score(
            ltp=103.0, high=103.0, low=100.0, close=100.0, volume_stats=v_stats, config=custom_cfg
        )
        self.assertGreater(res_custom["pillars"]["p1_volume"], res_default["pillars"]["p1_volume"])

        # 3. Verify MultiTrackPortfolio accepts custom risk_config and allocator_config
        custom_risk = {
            "option_stop_loss_pct": 8.0,
            "stagnation_timeout_sec": 450.0,
            "fixed_target_inr": 500.0,
            "underlying_breathing_room_pct": 1.20,
        }
        custom_alloc = {
            "tranches": 2,
            "historical_lookback_days": 3,
            "roi_weight": 0.60,
        }
        portfolio = MultiTrackPortfolio(
            starting_capital=100000.0,
            risk_config=custom_risk,
            allocator_config=custom_alloc,
        )
        self.assertEqual(portfolio.risk_manager.option_sl_pct, 8.0)
        self.assertEqual(portfolio.risk_manager.stagnation_timeout_sec, 450.0)
        self.assertEqual(portfolio.risk_manager.fixed_target_inr, 500.0)
        self.assertEqual(portfolio.risk_manager.underlying_breathing_room_pct, 1.20)
        self.assertEqual(portfolio.dynamic_trio_tranches, 2)
        self.assertEqual(portfolio.dynamic_trio_roi_weight, 0.60)

        # 4. Verify MomentumStrategyRunner accepts test config paths
        runner = MomentumStrategyRunner(
            mode="paper",
            config_path="config/strategies_test.json",
            execution_config_path="config/execution_test.json",
        )
        self.assertEqual(runner.min_score, 50)
        self.assertEqual(runner.min_trade_score, 55)
        self.assertEqual(runner.capital, 100000.0)


if __name__ == "__main__":
    unittest.main()

