#!/usr/bin/env python3
"""
tests/infrastructure/test_staleness_gate.py
============================================
Comprehensive test suite for the 30-Second Quote Staleness Gate:
1. Tests extract_quote_timestamp across epoch seconds, milliseconds, and ISO strings.
2. Tests is_quote_fresh against 30-second boundary.
3. Tests PaperExecutionAdapter BUY rejection on stale quotes (>30s).
4. Tests OptionStrikeResolver LOW_PREM mode OTM restriction and stale quote avoidance.
5. Tests MultiTrackPortfolio._execute_open_trades skipping stale quotes.
"""

import os
import shutil
import tempfile
import time
import unittest

from execution.paper.paper_adapter import PaperExecutionAdapter
from infrastructure.common.time_utils import (
    extract_quote_timestamp,
    is_quote_fresh,
)
from strategies.momentum.option_selector import OptionContract, OptionStrikeResolver
from strategies.momentum.portfolio import MultiTrackPortfolio


class TestStalenessGate(unittest.TestCase):
    def test_extract_quote_timestamp(self):
        # Seconds
        q1 = {"received_timestamp": 1700000000.5}
        self.assertAlmostEqual(extract_quote_timestamp(q1), 1700000000.5)

        # Milliseconds
        q2 = {"exchange_timestamp": 1700000000500}
        self.assertAlmostEqual(extract_quote_timestamp(q2), 1700000000.5)

        # ISO string
        q3 = {"timestamp": "2026-11-09T10:20:00+05:30"}
        ts3 = extract_quote_timestamp(q3)
        self.assertGreater(ts3, 1700000000)

        # Empty / None
        self.assertEqual(extract_quote_timestamp({}), 0.0)
        self.assertEqual(extract_quote_timestamp(None), 0.0)

    def test_is_quote_fresh_boundary(self):
        now = 1794199819.0
        # Exactly 10s old -> fresh
        q_fresh = {"received_timestamp": now - 10.0}
        fresh, age = is_quote_fresh(q_fresh, now_ts=now, max_age_sec=30.0)
        self.assertTrue(fresh)
        self.assertAlmostEqual(age, 10.0)

        # Exactly 30s old -> fresh
        q_30 = {"received_timestamp": now - 30.0}
        fresh, age = is_quote_fresh(q_30, now_ts=now, max_age_sec=30.0)
        self.assertTrue(fresh)
        self.assertAlmostEqual(age, 30.0)

        # 30.1s old -> stale
        q_stale = {"received_timestamp": now - 30.1}
        fresh, age = is_quote_fresh(q_stale, now_ts=now, max_age_sec=30.0)
        self.assertFalse(fresh)
        self.assertAlmostEqual(age, 30.1, places=3)

        # 47 minutes old (2820s) -> stale
        q_ancient = {"received_timestamp": now - 2820.0}
        fresh, age = is_quote_fresh(q_ancient, now_ts=now, max_age_sec=30.0)
        self.assertFalse(fresh)
        self.assertGreater(age, 2800)

    def test_paper_adapter_stale_quote_rejection(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            adapter = PaperExecutionAdapter(
                output_csv=os.path.join(tmp_dir, "trades.csv"),
                enforce_market_hours=False,
            )
            now = time.time()

            # Fresh quote -> fills normally
            fresh_quote = {
                "bid": 5.0,
                "ask": 5.2,
                "ltp": 5.1,
                "received_timestamp": now - 5.0,
            }
            res_fresh = adapter.place_order(
                symbol="TEST_STOCK",
                instrument_token="NSE_FO|11111",
                quantity=100,
                transaction_type="BUY",
                market_quote=fresh_quote,
            )
            self.assertEqual(res_fresh["status"], "FILLED")
            self.assertEqual(res_fresh["fill_price"], 5.2)

            # Stale quote (40 seconds old) -> REJECTED_STALE_QUOTE
            stale_quote = {
                "bid": 3.2,
                "ask": 3.28,
                "ltp": 3.21,
                "received_timestamp": now - 40.0,
            }
            res_stale = adapter.place_order(
                symbol="TEST_STOCK",
                instrument_token="NSE_FO|22222",
                quantity=100,
                transaction_type="BUY",
                market_quote=stale_quote,
            )
            self.assertEqual(res_stale["status"], "REJECTED_STALE_QUOTE")
            self.assertIn("stale", res_stale["error"].lower())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_option_resolver_low_prem_avoids_itm_and_stale(self):
        resolver = OptionStrikeResolver()
        resolver._loaded = True

        # Mock an underlying option chain for IREDA around LTP 114.0
        # Strikes: 110, 111, 112, 113 (ITM), 114 (ATM), 115, 116, 117 (OTM)
        chain = []
        for s in [110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.0, 117.0]:
            chain.append(
                OptionContract(
                    instrument_key=f"NSE_FO|CE_{int(s)}",
                    trading_symbol=f"IREDA26NOV{int(s)}CE",
                    underlying_symbol="IREDA",
                    strike_price=s,
                    option_type="CE",
                    expiry_date="2026-11-23",
                    lot_size=4525,
                    tick_size=0.05,
                )
            )
        resolver.options_by_symbol["IREDA"] = {"2026-11-23": chain}

        now = 1000.0
        # Case 1: Quotes cache has an ITM strike (111 CE) with LTP 3.28 (even if fresh).
        # LOW_PREM must NEVER select an ITM strike for a Bull trade!
        quotes = {
            "NSE_FO|CE_111": {"ltp": 3.28, "ask": 3.28, "received_timestamp": now - 5.0},
            "NSE_FO|CE_114": {"ltp": 3.00, "ask": 3.05, "received_timestamp": now - 5.0},
            "NSE_FO|CE_116": {"ltp": 2.50, "ask": 2.55, "received_timestamp": now - 5.0},
        }

        c = resolver.resolve_strike(
            symbol="IREDA",
            underlying_ltp=114.2,
            direction="BULL",
            strike_mode="LOW_PREM_OTM",
            quotes_cache=quotes,
            now_ts=now,
            max_quote_age_sec=30.0,
        )
        self.assertIsNotNone(c)
        # Must pick OTM strike (115 CE or 116 CE), NOT ITM 111 CE!
        self.assertGreater(c.strike_price, 114.0)

        # Also verify select_lowest_outlay_candidate skips stale quotes
        quotes_with_stale = {
            "NSE_FO|CE_111": {"ltp": 3.28, "ask": 3.28, "received_timestamp": now - 5.0},
            "NSE_FO|CE_116": {"ltp": 2.20, "ask": 2.25, "received_timestamp": now - 50.0},  # Stale!
            "NSE_FO|CE_117": {"ltp": 2.40, "ask": 2.45, "received_timestamp": now - 10.0},  # Fresh!
        }
        cand_res = resolver.select_lowest_outlay_candidate(
            candidate_symbols=["IREDA"],
            underlying_prices={"IREDA": 114.2},
            directions={"IREDA": "BULL"},
            quotes_cache=quotes,
            strike_mode="ATM",
            now_ts=now,
            max_quote_age_sec=30.0,
        )
        self.assertIsNotNone(cand_res)
        best_sym, best_c, best_cost = cand_res
        self.assertEqual(best_sym, "IREDA")

    def test_portfolio_execute_open_trades_skips_stale_quotes(self):
        port = MultiTrackPortfolio(starting_capital=100000.0, mode="paper")
        contract = OptionContract(
            instrument_key="NSE_FO|133251",
            trading_symbol="IREDA_2026-11-23_111.0_CE",
            underlying_symbol="IREDA",
            strike_price=111.0,
            option_type="CE",
            expiry_date="2026-11-23",
            lot_size=4525,
            tick_size=0.05,
        )

        sim_now = 1794199819.0  # 10:20:19 IST
        stale_47m_quote = {
            "ask": 3.28,
            "bid": 3.21,
            "ltp": 3.21,
            "received_timestamp": 1794197003.0,  # 09:33:23 IST (46m 56s old!)
        }
        cache = {"NSE_FO|133251": stale_47m_quote}

        # Attempt to open position with stale quote
        opened = port._execute_open_trades(
            symbol="IREDA",
            contract=contract,
            direction="BULL",
            underlying_ltp=114.23,
            option_quote=stale_47m_quote,
            flag_high=115.0,
            flag_low=113.0,
            now_ts=sim_now,
            quotes_cache=cache,
        )
        # Staleness gate must block all tracks from opening positions on 47m-stale quote!
        self.assertEqual(len(opened), 0)
        self.assertEqual(len(port.tracks["ATM"].open_positions), 0)


if __name__ == "__main__":
    unittest.main()
