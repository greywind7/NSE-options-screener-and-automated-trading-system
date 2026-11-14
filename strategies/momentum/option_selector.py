#!/usr/bin/env python3
"""
strategies/momentum/option_selector.py
======================================
Derivative Option Strike Resolver & Liquidity Guard:
1. Resolves ATM (Delta ~0.50), ITM (Delta ~0.70), OTM (Delta ~0.30), and LOW_PREM strikes.
2. Option Liquidity Guard: Enforces bid-ask spread <= 3.0% and order book depth >= 1 lot.
3. Strict Zero-Tolerance Pricing: Skips unquoted or stale options; never estimates theoretical premiums.
"""

from __future__ import annotations

import csv
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from infrastructure.common.time_utils import is_quote_fresh


@dataclass
class OptionContract:
    instrument_key: str
    trading_symbol: str
    underlying_symbol: str
    strike_price: float
    option_type: str  # CE or PE
    expiry_date: str
    lot_size: int
    tick_size: float


class OptionStrikeResolver:
    """
    Finds and validates appropriate derivative contracts for trade setups.
    """

    def __init__(
        self,
        master_csv: str = "data/instrument.csv",
        manifest_path: Optional[str] = None,
        max_spread_pct: float = 3.0,
        min_depth_lots: int = 1,
    ):
        self.master_csv = master_csv
        self.manifest_path = manifest_path
        self.max_spread_pct = float(max_spread_pct)
        self.min_depth_lots = int(min_depth_lots)
        # underlying_symbol -> expiry -> list of OptionContract
        self.options_by_symbol: Dict[str, Dict[str, List[OptionContract]]] = defaultdict(lambda: defaultdict(list))
        self.lot_sizes: Dict[str, int] = {}
        self.tick_sizes: Dict[str, float] = {}
        self._loaded = False

    def load(self) -> None:
        """Load FNO option contracts from manifest or master CSV."""
        if self._loaded:
            return

        # 1. Try loading from subscription manifest if available
        manifest_candidates = [self.manifest_path, "data/subscription_manifest.json", "data/subscription_manifest_mcx.json"]
        for mpath in manifest_candidates:
            if mpath and os.path.exists(mpath):
                try:
                    import json
                    with open(mpath, "r", encoding="utf-8") as f:
                        m = json.load(f)
                    opt_map = m.get("options_map", {})
                    meta_map = m.get("symbol_metadata", {})
                    if opt_map:
                        for sym, sdata in opt_map.items():
                            exp = sdata.get("nearest_expiry", "")
                            meta = meta_map.get(sym, {})
                            lot = int(meta.get("lot_size", 1))
                            tick = float(meta.get("tick_size", 0.05))
                            c_name = meta.get("name")
                            c_ukey = meta.get("underlying_key")

                            self.lot_sizes[sym] = lot
                            self.tick_sizes[sym] = tick
                            if c_name:
                                self.lot_sizes[c_name] = lot
                                self.tick_sizes[c_name] = tick
                            if c_ukey:
                                self.lot_sizes[c_ukey] = lot
                                self.tick_sizes[c_ukey] = tick

                            for strk_str, cdict in sdata.get("strikes", {}).items():
                                try:
                                    s_price = float(strk_str)
                                except ValueError:
                                    continue
                                for otype, okey in cdict.items():
                                    contract = OptionContract(
                                        instrument_key=okey,
                                        trading_symbol=f"{sym}_{exp}_{s_price}_{otype}",
                                        underlying_symbol=sym,
                                        strike_price=s_price,
                                        option_type=otype,
                                        expiry_date=exp,
                                        lot_size=lot,
                                        tick_size=tick,
                                    )
                                    self.options_by_symbol[sym][exp].append(contract)
                                    if c_name and c_name != sym:
                                        self.options_by_symbol[c_name][exp].append(contract)
                                    if c_ukey and c_ukey != sym:
                                        self.options_by_symbol[c_ukey][exp].append(contract)
                        if self.options_by_symbol:
                            self._loaded = True
                            return
                except Exception:
                    pass

        # 2. Fallback to scanning master CSV
        if not os.path.exists(self.master_csv):
            return

        with open(self.master_csv, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for r in reader:
                itype = (r.get("option_type") or r.get("instrument_type") or "").strip().upper()
                if itype not in ("CE", "PE"):
                    continue

                seg = (r.get("segment") or "").strip().upper()
                if seg and seg != "FNO":
                    continue

                exch = (r.get("exchange") or "").strip().upper()
                if exch and not ("_FO" in exch or exch in ("MCX", "NSE", "BSE")):
                    continue

                und = (r.get("underlying_symbol") or r.get("name") or "").strip().upper()
                exp = (r.get("expiry_date") or r.get("expiry") or "").strip()
                tok = (r.get("exchange_token") or "").strip()
                if not und or not exp or not tok:
                    continue

                try:
                    strike = float(r.get("strike_price") or r.get("strike") or 0)
                    lot = int(r.get("lot_size") or 1)
                    tick = float(r.get("tick_size") or 0.05)
                except (ValueError, TypeError):
                    continue

                key = r.get("instrument_key") or f"{exch if exch else 'NSE_FO'}|{tok}"
                sym = (r.get("trading_symbol") or r.get("tradingsymbol") or "").strip()

                contract = OptionContract(
                    instrument_key=key,
                    trading_symbol=sym,
                    underlying_symbol=und,
                    strike_price=strike,
                    option_type=itype,
                    expiry_date=exp,
                    lot_size=lot,
                    tick_size=tick,
                )
                self.options_by_symbol[und][exp].append(contract)
                self.lot_sizes[und] = lot
                self.tick_sizes[und] = tick

        self._loaded = True

    def get_nearest_expiry(self, symbol: str) -> Optional[str]:
        """Find the nearest active expiry date for the underlying."""
        if not self._loaded:
            self.load()
        if symbol in self.options_by_symbol:
            expiries = sorted(list(self.options_by_symbol[symbol].keys()))
            return expiries[0] if expiries else None

        s_upper = symbol.upper()
        if s_upper in self.options_by_symbol:
            expiries = sorted(list(self.options_by_symbol[s_upper].keys()))
            return expiries[0] if expiries else None

        for k in self.options_by_symbol:
            if k.upper() == s_upper or k.upper().startswith(s_upper) or s_upper.startswith(k.upper()):
                expiries = sorted(list(self.options_by_symbol[k].keys()))
                return expiries[0] if expiries else None
        return None

    def resolve_strike(
        self,
        symbol: str,
        underlying_ltp: float,
        direction: str,  # "BULL" (CE) or "BEAR" (PE)
        strike_mode: str = "ATM",  # "ATM", "ITM", "OTM", or "LOW_PREM"
        quotes_cache: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
        max_quote_age_sec: float = 30.0,
    ) -> Optional[OptionContract]:
        """
        Resolve the specific option contract according to strike_mode.
        """
        if not self._loaded:
            self.load()

        lookup_key = symbol
        if lookup_key not in self.options_by_symbol:
            s_upper = symbol.upper()
            if s_upper in self.options_by_symbol:
                lookup_key = s_upper
            else:
                for k in self.options_by_symbol:
                    if k.upper() == s_upper or k.upper().startswith(s_upper) or s_upper.startswith(k.upper()):
                        lookup_key = k
                        break

        exp = self.get_nearest_expiry(lookup_key)
        if not exp or lookup_key not in self.options_by_symbol:
            return None

        contracts = self.options_by_symbol[lookup_key][exp]
        opt_type = "CE" if direction.upper() in ("BULL", "CE") else "PE"

        # Filter by option type and sort by strike
        typed_contracts = sorted(
            [c for c in contracts if c.option_type == opt_type],
            key=lambda x: x.strike_price,
        )
        if not typed_contracts:
            return None

        # 1. ATM Contract (closest strike to underlying_ltp)
        atm_contract = min(typed_contracts, key=lambda c: abs(c.strike_price - underlying_ltp))
        atm_idx = typed_contracts.index(atm_contract)

        strike_mode_upper = strike_mode.upper()

        if strike_mode_upper in (
            "ATM", "TRAIL_SL_ATM", "FIXED_TGT_ATM",
        ):
            return atm_contract

        elif strike_mode_upper in ("ITM", "TRAIL_SL_ITM", "FIXED_TGT_ITM", "LOW_PREM_ITM"):
            # Bull CE: ITM is lower strike (idx - 1 or 2)
            # Bear PE: ITM is higher strike (idx + 1 or 2)
            if opt_type == "CE":
                target_idx = max(0, atm_idx - 1)
            else:
                target_idx = min(len(typed_contracts) - 1, atm_idx + 1)
            return typed_contracts[target_idx]

        elif strike_mode_upper in ("OTM", "TRAIL_SL_OTM", "FIXED_TGT_OTM", "LOW_PREM_OTM"):
            # Bull CE: OTM is higher strike (idx + 1 or 2)
            # Bear PE: OTM is lower strike (idx - 1 or 2)
            if opt_type == "CE":
                target_idx = min(len(typed_contracts) - 1, atm_idx + 1)
            else:
                target_idx = max(0, atm_idx - 1)
            return typed_contracts[target_idx]

        return atm_contract

    def resolve_next_itm_strike(
        self,
        symbol: str,
        underlying_ltp: float,
        direction: str,
        exclude_contract_keys: Optional[Any] = None,
    ) -> Optional[OptionContract]:
        """
        Resolve the next available ITM strike that is not present in exclude_contract_keys.
        Used for strike diversification in combined allocation tracks when multiple
        sub-tracks would otherwise order the exact same contract.
        """
        if not self._loaded:
            self.load()

        lookup_key = symbol
        if lookup_key not in self.options_by_symbol:
            s_upper = symbol.upper()
            if s_upper in self.options_by_symbol:
                lookup_key = s_upper
            else:
                for k in self.options_by_symbol:
                    if k.upper() == s_upper or k.upper().startswith(s_upper) or s_upper.startswith(k.upper()):
                        lookup_key = k
                        break

        exp = self.get_nearest_expiry(lookup_key)
        if not exp or lookup_key not in self.options_by_symbol:
            return None

        contracts = self.options_by_symbol[lookup_key][exp]
        opt_type = "CE" if direction.upper() in ("BULL", "CE") else "PE"

        typed_contracts = sorted(
            [c for c in contracts if c.option_type == opt_type],
            key=lambda x: x.strike_price,
        )
        if not typed_contracts:
            return None

        atm_contract = min(typed_contracts, key=lambda c: abs(c.strike_price - underlying_ltp))
        atm_idx = typed_contracts.index(atm_contract)

        excluded = set(exclude_contract_keys) if exclude_contract_keys else set()

        # Iterate through ITM strikes in order of increasing ITM depth
        if opt_type == "CE":
            # For CE: strikes lower than ATM are ITM (atm_idx-1, atm_idx-2, ..., 0)
            candidate_indices = list(range(atm_idx - 1, -1, -1))
        else:
            # For PE: strikes higher than ATM are ITM (atm_idx+1, atm_idx+2, ..., len-1)
            candidate_indices = list(range(atm_idx + 1, len(typed_contracts)))

        for idx in candidate_indices:
            c = typed_contracts[idx]
            ckey = getattr(c, "instrument_key", getattr(c, "trading_symbol", ""))
            csym = getattr(c, "trading_symbol", "")
            if ckey not in excluded and csym not in excluded:
                return c

        return None

    def select_lowest_outlay_candidate(
        self,
        candidate_symbols: List[str],
        underlying_prices: Dict[str, float],
        directions: Dict[str, str],
        quotes_cache: Optional[Dict[str, Any]] = None,
        strike_mode: str = "ATM",
        now_ts: Optional[float] = None,
        max_quote_age_sec: float = 30.0,
    ) -> Optional[Tuple[str, OptionContract, float]]:
        """
        When multiple candidate setups are present, select the candidate whose 1-lot option
        capital requirement (premium * lot_size) is the lowest (e.g. 8k vs 25k).
        Returns (symbol, contract, 1_lot_cost).
        """
        outlay_candidates = []
        for sym in candidate_symbols:
            ltp = underlying_prices.get(sym, 0.0)
            if ltp <= 0:
                continue
            direction = directions.get(sym, "BULL")
            c = self.resolve_strike(
                symbol=sym,
                underlying_ltp=ltp,
                direction=direction,
                strike_mode=strike_mode,
                quotes_cache=quotes_cache,
                now_ts=now_ts,
                max_quote_age_sec=max_quote_age_sec,
            )
            if not c:
                continue
            lot = getattr(c, "lot_size", 1)
            q = (
                (quotes_cache.get(c.instrument_key) if quotes_cache else None)
                or (quotes_cache.get(c.instrument_key.replace(":", "|")) if quotes_cache else None)
                or {}
            )
            ask = float(q.get("ask") or q.get("last_price") or q.get("ltp") or 0.0)
            if ask <= 0:
                continue
            is_fresh, _ = is_quote_fresh(q, now_ts=now_ts, max_age_sec=max_quote_age_sec)
            if not is_fresh:
                continue
            cost = ask * lot
            outlay_candidates.append((cost, sym, c))
        if not outlay_candidates:
            return None
        outlay_candidates.sort(key=lambda x: x[0])
        best_cost, best_sym, best_contract = outlay_candidates[0]
        return best_sym, best_contract, best_cost

    def check_liquidity_guard(
        self,
        quote: Dict[str, Any],
        max_spread_pct: Optional[float] = None,
        min_depth_lots: Optional[int] = None,
        lot_size: int = 1,
    ) -> Tuple[bool, str]:
        """
        Enforce Option Liquidity Guard:
        - Bid-Ask Spread <= max_spread_pct (default: from config or 3.0%).
        - Available quantity on top of book >= min_depth_lots * lot_size.
        """
        spread_limit = self.max_spread_pct if max_spread_pct is None else float(max_spread_pct)
        depth_lots = self.min_depth_lots if min_depth_lots is None else int(min_depth_lots)
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        bid_qty = int(quote.get("bid_qty") or 0)
        ask_qty = int(quote.get("ask_qty") or 0)

        if bid <= 0 or ask <= 0:
            return False, "Missing bid or ask"

        spread = ask - bid
        mid = (ask + bid) / 2.0
        spread_pct = (spread / mid) * 100.0 if mid > 0 else 999.0

        if spread_pct > spread_limit:
            return False, f"Spread {spread_pct:.2f}% > {spread_limit}%"

        req_qty = depth_lots * lot_size
        if bid_qty < req_qty or ask_qty < req_qty:
            return False, f"Insufficient depth: bid_qty={bid_qty}, ask_qty={ask_qty} (req: {req_qty})"

        return True, "Liquid"

    def compute_option_requirement(
        self,
        symbol: str,
        underlying_ltp: float,
        direction: str = "BULL",
        quotes_cache: Optional[Dict[str, Any]] = None,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Resolve 1-lot ATM option requirement for display in scanner and alerts.
        Returns:
            {
                "has_fo": bool,
                "one_lot_cost": float,
                "premium": float,
                "lot_size": int,
                "opt_symbol": str,
                "opt_key": str,
                "display_str": str,
                "raw_str": str,
            }
        """
        if underlying_ltp <= 0:
            return {
                "has_fo": False,
                "one_lot_cost": 0.0,
                "premium": 0.0,
                "lot_size": 1,
                "opt_symbol": "",
                "opt_key": "",
                "display_str": "Cash Only",
                "raw_str": "Cash Only",
            }

        contract = self.resolve_strike(symbol, underlying_ltp, direction, strike_mode="ATM", quotes_cache=quotes_cache, now_ts=now_ts)
        if not contract:
            return {
                "has_fo": False,
                "one_lot_cost": 0.0,
                "premium": 0.0,
                "lot_size": 1,
                "opt_symbol": "",
                "opt_key": "",
                "display_str": "Cash Only",
                "raw_str": "Cash Only",
            }

        lot = contract.lot_size if contract.lot_size > 0 else 1
        opt_key = contract.instrument_key
        opt_sym = contract.trading_symbol

        premium = 0.0
        is_fresh = True
        if quotes_cache:
            q = (
                quotes_cache.get(opt_key)
                or quotes_cache.get(opt_key.replace("|", ":"))
                or quotes_cache.get(opt_key.replace(":", "|"))
                or (quotes_cache.get(opt_sym) if opt_sym else None)
            )
            if q and isinstance(q, dict):
                is_fresh, _ = is_quote_fresh(q, now_ts=now_ts, max_age_sec=30.0)
                premium = float(q.get("ask") or q.get("ltp") or q.get("last_price") or 0.0)
                if premium <= 0 and isinstance(q.get("depth"), dict) and q["depth"].get("sell"):
                    try:
                        premium = float(q["depth"]["sell"][0].get("price", 0.0))
                    except Exception:
                        pass

        if not is_fresh:
            return {
                "has_fo": True,
                "one_lot_cost": 0.0,
                "premium": 0.0,
                "lot_size": lot,
                "opt_symbol": opt_sym,
                "opt_key": opt_key,
                "display_str": "Stale Quote",
                "raw_str": "Stale Quote",
            }

        if premium <= 0:
            return {
                "has_fo": True,
                "one_lot_cost": 0.0,
                "premium": 0.0,
                "lot_size": lot,
                "opt_symbol": opt_sym,
                "opt_key": opt_key,
                "display_str": "No Quote",
                "raw_str": "No Quote",
            }

        one_lot_cost = premium * lot

        if one_lot_cost >= 100_000:
            cost_txt = f"₹{one_lot_cost / 100000:.1f}L"
        elif one_lot_cost >= 1_000:
            cost_txt = f"₹{one_lot_cost / 1000:.1f}k"
        else:
            cost_txt = f"₹{one_lot_cost:.0f}"

        prem_txt = f"₹{premium:.1f}" if premium < 100 else f"₹{premium:.0f}"
        raw_str = f"{cost_txt} ({prem_txt})"

        return {
            "has_fo": True,
            "one_lot_cost": round(one_lot_cost, 2),
            "premium": round(premium, 2),
            "lot_size": lot,
            "opt_symbol": opt_sym,
            "opt_key": opt_key,
            "display_str": raw_str,
            "raw_str": raw_str,
        }

