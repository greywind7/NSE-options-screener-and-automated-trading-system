#!/usr/bin/env python3
"""
bear_flag_engine.py (Unified Bidirectional Flag Option Strategy Engine)
=======================================================================
Algorithmic Execution & Paper-Trading Engine for both Bull Flag (CE) and Bear Flag (PE) Option Setups.
Translates discretionary trader chart reading into strict, quantified rules:

1. Setup Mechanics (Underlying Equity):
   - Bull Flag:
     * Impulse Surge (Pole): Sharp push into upper 15% of day's range (range_pos >= 0.85).
     * Flag Consolidation: Tight pause/coiling in upper range (range_pos >= 0.80) on dry volume
       (RVOL_5m < 0.70x) for >= 2 cycles.
     * Breakout Entry: Stock breaks above Flag High on volume expansion (RVOL_5m >= 0.85x).
     * Derivative: Buys Call Options (CE).
   - Bear Flag:
     * Impulse Drop (Pole): Sharp drop into lower 15% of day's range (range_pos <= 0.15).
     * Flag Consolidation: Tight pause/coiling in lower range (range_pos <= 0.20) on dry volume
       (RVOL_5m < 0.70x) for >= 2 cycles.
     * Breakdown Entry: Stock breaks below Flag Low on volume expansion (RVOL_5m >= 0.85x).
     * Derivative: Buys Put Options (PE).

2. Option Derivative Resolution (CE & PE):
   - Upstox Live Option Chain API (`/v2/option/chain`) with instant fallback to `instrument.csv`.
   - Multi-strike simulation side-by-side:
     * ATM: Strike closest to spot price (min |K - S|).
     * ITM: Call: 1 strike lower (K < S); Put: 1 strike higher (K > S). High delta (~0.65-0.70).
     * OTM: Call: 1 strike higher (K > S); Put: 1 strike lower (K < S). Cheaper, lower delta (~0.30).

3. Dynamic Tiered Capital Gating (Limited Capital Management):
   - Tier 1 (50% - 100% Capital): At 50-100% remaining capital (adjusted to realized P&L),
     trades are capped at max 50% of available capital.
   - Tier 2 (25% - 50% Capital): At 25-50% remaining capital, can use up to 75% of remaining capital.
   - Tier 3 (< 25% Capital): Below 25% capital, 100% of remaining capital is allowed (to afford >= 1 lot).
   - Setup Quality Priority: When multiple setups trigger concurrently and capital is limited,
     candidates are prioritized by Setup Score (0-100) and RVOL_5m.

4. Quantified Risk & Trade Management:
   - Technical Stop Loss:
     * Bull Flag: Underlying drops below Flag Low (invalidation).
     * Bear Flag: Underlying rises above Flag High (invalidation).
   - Target 1 (1.5R): 1.5x Flag Height -> Trails stop to breakeven (Entry).
   - Target 2 (2.0R): 2.0x Flag Height -> Full profit realization.
   - Option Hard Stop: If option premium declines >= 35% from entry -> Emergency cut.
   - EOD Square-Off: Automatic close at 15:20 IST.
"""

from __future__ import annotations

import csv
import math
import os
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

try:
    from filter_instruments import Instrument, InstrumentManager, load_instruments
    from upstox_common import http_get_json, safe_float, safe_int
except ImportError:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from filter_instruments import Instrument, InstrumentManager, load_instruments
    from upstox_common import http_get_json, safe_float, safe_int


# ---------------------------------------------------------------------------
# Terminal Colors & UI Styling
# ---------------------------------------------------------------------------

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"
    BLACK = "\033[30m"
    MAGENTA = "\033[95m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_GRAY = "\033[100m"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ResolvedStrikes:
    """Represents the resolved ATM, ITM, and OTM options for an underlying stock."""
    underlying_symbol: str
    direction: str                  # 'BULL' or 'BEAR'
    option_type: str                # 'CE' or 'PE'
    spot_price: float
    expiry_date: str
    atm_inst: Optional[Instrument] = None
    itm_inst: Optional[Instrument] = None
    otm_inst: Optional[Instrument] = None

    # Backward compatibility aliases
    @property
    def atm_pe(self) -> Optional[Instrument]:
        return self.atm_inst if self.option_type == "PE" else None

    @property
    def itm_pe(self) -> Optional[Instrument]:
        return self.itm_inst if self.option_type == "PE" else None

    @property
    def otm_pe(self) -> Optional[Instrument]:
        return self.otm_inst if self.option_type == "PE" else None

    @property
    def is_valid(self) -> bool:
        return self.atm_inst is not None


@dataclass
class PaperPosition:
    """Represents an active or closed paper-trade position on an option contract."""
    trade_id: str
    symbol: str                     # Underlying equity symbol (e.g. 'RELIANCE')
    direction: str                  # 'BULL' or 'BEAR'
    option_type: str                # 'CE' or 'PE'
    strike_type: str                # 'ATM', 'ITM', or 'OTM'
    option_symbol: str              # e.g. 'RELIANCE26NOV1220PE' or '...CE'
    option_key: str                 # e.g. 'NSE_FO|144158'
    strike_price: float
    expiry_date: str
    lot_size: int
    lots: int
    quantity: int                   # lot_size * lots
    entry_time: str                 # HH:MM:SS
    entry_underlying: float
    entry_premium: float
    capital_invested: float         # entry_premium * quantity

    # Underlying Risk Levels
    flag_high: float                # Flag consolidation high
    flag_low: float                 # Flag consolidation low
    flag_height: float              # flag_high - flag_low
    stop_loss_underlying: float     # Initial SL on underlying equity
    target_1_underlying: float = 0.0
    target_2_underlying: float = 0.0
    breakeven_underlying: float = 0.0

    # Scalping Targets & Risk Caps (Option Premium)
    target_premium: float = 0.0     # Target option premium (5% premium gain + brokerage)
    stop_loss_premium: float = 0.0  # Option stop loss premium (R = 0.15)
    target_gross_gain: float = 0.0  # (target_pct * capital) + brokerage
    target_net_profit: float = 0.0  # target_pct * capital

    # Dynamic Rolling Target & Breakeven Shift Tracking
    entry_epoch: float = 0.0
    initial_target_pct: float = 5.0
    initial_r_stop: float = 0.15
    initial_sl_underlying: float = 0.0
    current_target_pct: float = 5.0
    current_r_stop: float = 0.15
    current_target_premium: float = 0.0
    current_stop_loss_premium: float = 0.0
    current_target_gross_gain: float = 0.0
    current_stop_loss_underlying: float = 0.0
    in_red_duration_seconds: float = 0.0
    red_stagnant_since: Optional[float] = None
    holding_duration_mins: float = 0.0
    exit_stage_type: str = "NORMAL"  # 'NORMAL', 'ROLLING_DECAY', 'BREAKEVEN_SHIFT'

    # Dynamic Trailing Profit Floors & Target Runner
    peak_pnl_pct: float = 0.0               # High-water mark for Net P&L %
    peak_premium: float = 0.0               # High-water mark for option premium
    trailing_floor_active: bool = False     # True once mtm_pnl_pct >= 3.5% (or intermediate_trail_trigger_pct)
    trailing_floor_pct: float = 0.0         # Current rolling floor % (starts at 2.5%, trails peak - 1.0%)
    trailing_floor_premium: float = 0.0     # Premium equivalent of trailing_floor_pct
    
    target_runner_active: bool = False      # True once target is hit
    target_runner_start_time: Optional[float] = None # Timestamp when target was first reached
    target_runner_elapsed_sec: float = 0.0  # Seconds elapsed in runner mode
    target_runner_floor_pct: float = 0.0    # Rolling floor % during runner (max(target_pct, peak - 1.0%))
    target_runner_floor_premium: float = 0.0# Premium equivalent of target_runner_floor_pct

    # Brokerage Tracking (Flat ₹30 entry + ₹30 exit = ₹60 round-trip)
    brokerage_per_order: float = 30.0
    brokerage: float = 60.0

    # Live Tracking
    current_underlying: float = 0.0
    current_premium: float = 0.0
    gross_mtm_pnl: float = 0.0
    mtm_pnl: float = 0.0            # Net MTM P&L (gross - brokerage)
    mtm_pnl_pct: float = 0.0        # Net MTM %
    trailing_sl_active: bool = False
    status: str = "OPEN"            # 'OPEN' or 'CLOSED'

    # Advanced Simulation Strategies:
    trailing_sl_mode: bool = False          # b) Tick-by-tick trailing stoploss point-for-point
    trailing_sl_premium: float = 0.0        # Dynamic trailing stop loss premium
    enable_fixed_target: bool = False       # c) Target is min(pct, fixed_gain)
    fixed_target_gain: float = 750.0        # Default fixed net gain ₹750
    disable_profit_floors: bool = False     # c) Disable profit floors and 30s runner
    lower_premium_bias: bool = False        # a) Biased towards lower premium options

    # Exit Details
    exit_time: Optional[str] = None
    exit_underlying: Optional[float] = None
    exit_premium: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl: float = 0.0
    realized_pnl: float = 0.0       # Net Realized P&L after ₹60 brokerage
    realized_pnl_pct: float = 0.0   # Net Realized P&L %

    def premium_for_net_pct(self, net_pct: float) -> float:
        """Calculate the option premium corresponding to a given Net P&L % after ₹60 roundtrip brokerage."""
        if self.quantity <= 0 or self.capital_invested <= 0:
            return self.entry_premium
        target_net_profit = (net_pct / 100.0) * self.capital_invested
        target_gross_gain = target_net_profit + self.brokerage
        return round(self.entry_premium + (target_gross_gain / self.quantity), 2)

    def update_mtm(
        self,
        underlying_price: float,
        option_premium: float,
        now: Optional[datetime] = None,
        target_initial: float = 5.0,
        target_decay: float = 2.5,
        target_decay_mins: float = 30.0,
        red_stagnant_mins: float = 30.0,
        breakeven_target: float = 0.5,
        breakeven_shift_mins: float = 10.0,
        sl_upping_pct: float = 0.33,
        intermediate_trail_trigger_pct: float = 3.5,
        intermediate_trail_floor_pct: float = 2.5,
        trailing_step_pct: float = 1.0,
        target_runner_seconds: float = 30.0,
    ) -> None:
        """
        Update live mark-to-market prices and Net P&L accounting for brokerage,
        and dynamically advance:
        1. Rolling Target (5% -> 2.5% over initial 30 minutes)
        2. Stagnant Red Breakeven Shift (2.5% -> 0.5% over next 10 mins if in red >= 30 mins)
        3. Stop Loss Upped by 33% (tighter loss limit) over the same 10-minute window.
        4. Intermediate Trailing Profit Floor (crosses 3.5% -> floor at 2.5% rolling, e.g. 4.5% peak -> 3.5% floor)
        5. Target Runner Mode (Target reached -> run for 30s with floor at max(target, peak - 1.0%), e.g. 7% peak -> 6% floor)
        """
        self.current_underlying = underlying_price
        if option_premium > 0:
            self.current_premium = option_premium
        elif self.current_premium <= 0:
            self.current_premium = self.entry_premium

        self.gross_mtm_pnl = round((self.current_premium - self.entry_premium) * self.quantity, 2)
        # Net MTM deducts round-trip brokerage (₹60) to show true net liquidation value
        self.mtm_pnl = round(self.gross_mtm_pnl - self.brokerage, 2)
        if self.capital_invested > 0:
            self.mtm_pnl_pct = round((self.mtm_pnl / self.capital_invested) * 100, 2)
        else:
            self.mtm_pnl_pct = 0.0

        if now is None:
            now = datetime.now()
        now_ts = now.timestamp()
        if self.entry_epoch <= 0:
            self.entry_epoch = now_ts
        elapsed_sec = max(0.0, now_ts - self.entry_epoch)
        self.holding_duration_mins = round(elapsed_sec / 60.0, 2)

        # Track duration spent in red
        is_in_red = (self.mtm_pnl < 0 or self.current_premium < self.entry_premium)
        last_ts = getattr(self, "_last_mtm_ts", self.entry_epoch)
        if is_in_red and now_ts > last_ts:
            self.in_red_duration_seconds += (now_ts - last_ts)
        self._last_mtm_ts = now_ts

        # Check if position has stayed in red for too long (default 30 mins)
        has_stayed_in_red_30m = (
            (self.holding_duration_mins >= red_stagnant_mins and is_in_red)
            or (self.in_red_duration_seconds >= (red_stagnant_mins * 60.0))
        )

        if has_stayed_in_red_30m and self.red_stagnant_since is None:
            self.red_stagnant_since = now_ts

        if self.red_stagnant_since is not None:
            # Phase 2: Shift to break even (2.5% -> 0.5% over next 10 mins) & up stoploss by 33%
            self.exit_stage_type = "BREAKEVEN_SHIFT"
            red_elapsed_mins = max(0.0, (now_ts - self.red_stagnant_since) / 60.0)
            shift_progress = min(1.0, red_elapsed_mins / max(1.0, breakeven_shift_mins))
            self.current_target_pct = round(target_decay - (target_decay - breakeven_target) * shift_progress, 3)

            # Stop Loss is upped by 33% over the same 10-minute time frame (cuts risk distance by 33%)
            current_sl_tighten = sl_upping_pct * shift_progress
            self.current_r_stop = round(self.initial_r_stop * (1.0 - current_sl_tighten), 4)

            # Tighten underlying stop loss by 33% towards entry
            if self.initial_sl_underlying > 0 and self.entry_underlying > 0:
                delta_und = abs(self.entry_underlying - self.initial_sl_underlying)
                tighten_und = delta_und * current_sl_tighten
                if self.direction == "BULL":
                    self.current_stop_loss_underlying = round(self.initial_sl_underlying + tighten_und, 2)
                else:
                    self.current_stop_loss_underlying = round(self.initial_sl_underlying - tighten_und, 2)
        else:
            # Phase 1: Rolling target from 5.0% down to 2.5% over 30 mins
            self.exit_stage_type = "ROLLING_DECAY"
            decay_progress = min(1.0, self.holding_duration_mins / max(1.0, target_decay_mins))
            self.current_target_pct = round(target_initial - (target_initial - target_decay) * decay_progress, 3)
            self.current_r_stop = self.initial_r_stop
            self.current_stop_loss_underlying = self.stop_loss_underlying

        # Recalculate dynamic target levels: percentage or fixed gain (default ₹750) whichever hit first
        pct_target_net_profit = round((self.current_target_pct / 100.0) * self.capital_invested, 2)
        if getattr(self, "enable_fixed_target", False) and getattr(self, "fixed_target_gain", 750.0) > 0:
            target_net_profit = round(min(pct_target_net_profit, self.fixed_target_gain), 2)
        else:
            target_net_profit = pct_target_net_profit

        self.current_target_gross_gain = round(target_net_profit + self.brokerage, 2)
        self.current_target_premium = round(self.entry_premium + (self.current_target_gross_gain / self.quantity), 2)

        # -------------------------------------------------------------------
        # Update Peak High-Water Marks FIRST (needed for tick-by-tick trailing SL)
        # -------------------------------------------------------------------
        if self.mtm_pnl_pct > self.peak_pnl_pct:
            self.peak_pnl_pct = self.mtm_pnl_pct
        if self.current_premium > self.peak_premium:
            self.peak_premium = self.current_premium

        # Recalculate dynamic stop loss premium:
        base_sl_premium = round(max(0.05, self.entry_premium * (1.0 - self.current_r_stop)), 2)
        if getattr(self, "trailing_sl_mode", False):
            # b) The trail step will be the amount the premium goes up tick by tick
            risk_dist = self.entry_premium * self.current_r_stop
            self.trailing_sl_premium = round(max(base_sl_premium, self.peak_premium - risk_dist), 2)
            self.current_stop_loss_premium = self.trailing_sl_premium
        else:
            self.current_stop_loss_premium = base_sl_premium

        # -------------------------------------------------------------------
        # Target Runner State (Target reached -> run for 30s with rolling floor, bypassed if disable_profit_floors)
        # -------------------------------------------------------------------
        if getattr(self, "disable_profit_floors", False):
            self.target_runner_active = False
            self.trailing_floor_active = False
        else:
            is_target_hit = (
                self.gross_mtm_pnl >= self.current_target_gross_gain
                or self.current_premium >= self.current_target_premium
                or self.mtm_pnl_pct >= self.current_target_pct
            )

            if is_target_hit and self.red_stagnant_since is None and target_runner_seconds > 0:
                if not self.target_runner_active:
                    self.target_runner_active = True
                    self.target_runner_start_time = now_ts
                    self.trailing_floor_active = False

            if self.target_runner_active:
                if self.target_runner_start_time is None:
                    self.target_runner_start_time = now_ts
                self.target_runner_elapsed_sec = max(0.0, now_ts - self.target_runner_start_time)
                # Reversal floor: starts at current_target_pct (e.g. 5.0%), and rolls trailing_step_pct behind peak (e.g. 7% -> 6%)
                self.target_runner_floor_pct = round(
                    max(self.current_target_pct, self.peak_pnl_pct - trailing_step_pct),
                    2
                )
                self.target_runner_floor_premium = self.premium_for_net_pct(self.target_runner_floor_pct)
            else:
                # -------------------------------------------------------------------
                # Intermediate Trailing Profit Floor (crosses 3.5% -> base floor 2.5%, rolling)
                # -------------------------------------------------------------------
                if self.peak_pnl_pct >= intermediate_trail_trigger_pct:
                    self.trailing_floor_active = True
                    self.trailing_floor_pct = round(
                        max(intermediate_trail_floor_pct, self.peak_pnl_pct - trailing_step_pct),
                        2
                    )
                    self.trailing_floor_premium = self.premium_for_net_pct(self.trailing_floor_pct)

    def close(self, exit_time: str, underlying_price: float, exit_premium: float, reason: str) -> None:
        """Close out the position and calculate realized Net P&L after brokerage."""
        self.status = "CLOSED"
        self.exit_time = exit_time
        self.exit_underlying = round(underlying_price, 2)
        self.exit_premium = round(exit_premium if exit_premium > 0 else self.current_premium, 2)
        self.exit_reason = reason
        self.gross_pnl = round((self.exit_premium - self.entry_premium) * self.quantity, 2)
        self.realized_pnl = round(self.gross_pnl - self.brokerage, 2)
        if self.capital_invested > 0:
            self.realized_pnl_pct = round((self.realized_pnl / self.capital_invested) * 100, 2)
        else:
            self.realized_pnl_pct = 0.0


# ---------------------------------------------------------------------------
# Upstox Live Option Chain API Fetcher
# ---------------------------------------------------------------------------

def fetch_upstox_option_chain(
    instrument_key: str,
    access_token: str,
    expiry_date: Optional[str] = None,
    timeout: int = 10,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch live option chain directly from official Upstox API v2 endpoint:
    GET https://api.upstox.com/v2/option/chain?instrument_key={key}&expiry_date={exp}
    Returns list of strike dicts with call_options and put_options if successful.
    """
    if not access_token or not instrument_key:
        return None

    key_enc = urllib.parse.quote(instrument_key, safe="")
    url = f"https://api.upstox.com/v2/option/chain?instrument_key={key_enc}"
    if expiry_date:
        url += f"&expiry_date={expiry_date}"

    try:
        data = http_get_json(url, access_token, timeout=timeout)
        if data.get("status") == "success":
            return data.get("data")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Option Strike Resolver Engine (Supports both CE & PE)
# ---------------------------------------------------------------------------

class OptionStrikeResolver:
    """
    Finds and resolves the ATM, ITM, and OTM Call (CE) or Put (PE) option contracts
    for any underlying stock given its live spot price.
    Supports both local instrument.csv and Upstox Live Option Chain API.
    """

    def __init__(
        self,
        instrument_csv: str = "instrument.csv",
        manager: Optional[InstrumentManager] = None,
        access_token: Optional[str] = None,
    ):
        if manager is not None:
            self.manager = manager
        elif os.path.exists(instrument_csv):
            self.manager = load_instruments(instrument_csv)
        else:
            self.manager = None

        self.access_token = access_token
        self._chain_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    def resolve_strikes(
        self,
        underlying_symbol: str,
        spot_price: float,
        option_type: str = "PE",  # 'CE' for Bull Flag, 'PE' for Bear Flag
    ) -> Optional[ResolvedStrikes]:
        """
        Resolve ATM, ITM, and OTM options for the underlying equity.

        Mechanics:
        - Call (CE) for BULL Flag:
          * ATM CE: Strike closest to spot price (min |K - S|).
          * ITM CE: 1 strike LOWER than ATM (K < S, has intrinsic value).
          * OTM CE: 1 strike HIGHER than ATM (K > S, pure time value).
        - Put (PE) for BEAR Flag:
          * ATM PE: Strike closest to spot price (min |K - S|).
          * ITM PE: 1 strike HIGHER than ATM (K > S, has intrinsic value).
          * OTM PE: 1 strike LOWER than ATM (K < S, pure time value).
        """
        if self.manager is None or spot_price <= 0:
            return None

        sym = underlying_symbol.strip().upper()
        o_type = option_type.upper()
        direction = "BULL" if o_type == "CE" else "BEAR"

        # Look up option chain for nearest active expiry
        chain = self.manager.get_option_chain(sym)
        if not chain:
            for alt_sym in [f"{sym}", f"{sym}EQ", f"NSE_{sym}"]:
                chain = self.manager.get_option_chain(alt_sym)
                if chain:
                    sym = alt_sym
                    break

        if not chain:
            return None

        # Filter rows that have the requested option contract (CE or PE)
        valid_rows = [row for row in chain if row.get(o_type) is not None]
        if not valid_rows:
            return None

        # Sort strictly by strike_price ascending
        valid_rows.sort(key=lambda r: float(r["strike_price"]))

        # Find ATM strike: minimum absolute difference from spot price
        best_idx = 0
        min_diff = float("inf")
        for i, row in enumerate(valid_rows):
            diff = abs(float(row["strike_price"]) - spot_price)
            if diff < min_diff:
                min_diff = diff
                best_idx = i

        atm_inst: Instrument = valid_rows[best_idx][o_type]

        if o_type == "PE":
            # For PUT: ITM is HIGHER strike (best_idx + 1), OTM is LOWER strike (best_idx - 1)
            itm_idx = min(len(valid_rows) - 1, best_idx + 1)
            otm_idx = max(0, best_idx - 1)
        else:
            # For CALL: ITM is LOWER strike (best_idx - 1), OTM is HIGHER strike (best_idx + 1)
            itm_idx = max(0, best_idx - 1)
            otm_idx = min(len(valid_rows) - 1, best_idx + 1)

        itm_inst: Instrument = valid_rows[itm_idx][o_type]
        otm_inst: Instrument = valid_rows[otm_idx][o_type]
        expiry = valid_rows[best_idx].get("expiry_date", "")

        return ResolvedStrikes(
            underlying_symbol=underlying_symbol,
            direction=direction,
            option_type=o_type,
            spot_price=spot_price,
            expiry_date=expiry,
            atm_inst=atm_inst,
            itm_inst=itm_inst,
            otm_inst=otm_inst,
        )

    # Convenience helper for Bear Flag
    def resolve_pe_strikes(self, underlying_symbol: str, spot_price: float) -> Optional[ResolvedStrikes]:
        return self.resolve_strikes(underlying_symbol, spot_price, option_type="PE")

    # Convenience helper for Bull Flag
    def resolve_ce_strikes(self, underlying_symbol: str, spot_price: float) -> Optional[ResolvedStrikes]:
        return self.resolve_strikes(underlying_symbol, spot_price, option_type="CE")


# ---------------------------------------------------------------------------
# Live Market Option Quote Price Extractor
# ---------------------------------------------------------------------------

def extract_live_option_price(quote: Any, side: str = "BUY") -> float:
    """
    Extract realistic option fill price from an Upstox market quote.
    - During live market hours (09:15-15:30 IST): Prefers real Ask (sell depth) for BUY and Bid (buy depth) for SELL.
    - Post-market / after-hours (15:30-09:15 IST): Prefers real closing Last Traded Price (LTP) or OHLC close
      because post-market depth contains unexecuted retail AMO limit orders or empty sides with artificial spreads.
    Returns 0.0 if quote is missing, empty, or invalid.
    """
    if not quote or not isinstance(quote, dict):
        return 0.0

    now = datetime.now()
    is_post_market = (now.hour > 15) or (now.hour == 15 and now.minute >= 30) or (now.hour < 9) or (now.hour == 9 and now.minute < 15)

    if not is_post_market:
        # 1. During market hours: Check order book depth for real market fill execution
        depth = quote.get("depth")
        if isinstance(depth, dict):
            if side.upper() == "BUY":
                sells = depth.get("sell")
                if isinstance(sells, list) and len(sells) > 0 and isinstance(sells[0], dict):
                    ask = safe_float(sells[0].get("price"), 0.0)
                    if ask > 0:
                        return ask
            else:
                buys = depth.get("buy")
                if isinstance(buys, list) and len(buys) > 0 and isinstance(buys[0], dict):
                    bid = safe_float(buys[0].get("price"), 0.0)
                    if bid > 0:
                        return bid

    # 2. Check Last Traded Price (LTP) - Official closing price of the contract
    ltp = safe_float(quote.get("last_price"), 0.0)
    if ltp > 0:
        return ltp

    # 3. Check OHLC close
    ohlc = quote.get("ohlc")
    if isinstance(ohlc, dict):
        close_p = safe_float(ohlc.get("close"), 0.0)
        if close_p > 0:
            return close_p
        open_p = safe_float(ohlc.get("open"), 0.0)
        if open_p > 0:
            return open_p

    # 4. Fallback to order book depth if market is closed and LTP is missing
    depth = quote.get("depth")
    if isinstance(depth, dict):
        if side.upper() == "BUY":
            sells = depth.get("sell")
            if isinstance(sells, list) and len(sells) > 0 and isinstance(sells[0], dict):
                ask = safe_float(sells[0].get("price"), 0.0)
                if ask > 0:
                    return ask
        else:
            buys = depth.get("buy")
            if isinstance(buys, list) and len(buys) > 0 and isinstance(buys[0], dict):
                bid = safe_float(buys[0].get("price"), 0.0)
                if bid > 0:
                    return bid

    # 5. Check average price
    avg_p = safe_float(quote.get("average_price"), 0.0)
    if avg_p > 0:
        return avg_p

    return 0.0


# ---------------------------------------------------------------------------
# Technical Indicator Confluence & S/R Level Computations
# ---------------------------------------------------------------------------

def compute_rsi(prices: List[float], period: int = 14) -> float:
    """
    Calculate Wilder's Relative Strength Index (RSI) on a price series.
    Returns RSI in range [0.0, 100.0]. Defaults to 50.0 if insufficient data.
    """
    if len(prices) < 3:
        return 50.0
    p_len = min(period, len(prices) - 1)
    deltas = [prices[i] - prices[i - 1] for i in range(len(prices) - p_len, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains) / p_len if gains else 0.0
    avg_loss = sum(losses) / p_len if losses else 0.0
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def compute_macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
    """
    Compute MACD Line, Signal Line, and MACD Histogram on a price series.
    Returns (macd_line, macd_signal, macd_hist).
    """
    if len(prices) < 4:
        return 0.0, 0.0, 0.0

    def _ema(series: List[float], n: int) -> List[float]:
        if not series:
            return []
        alpha = 2.0 / (n + 1.0)
        res = [series[0]]
        for val in series[1:]:
            res.append(alpha * val + (1.0 - alpha) * res[-1])
        return res

    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    macd_series = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_series = _ema(macd_series, signal)

    macd_line = round(macd_series[-1], 2)
    macd_signal = round(signal_series[-1], 2)
    macd_hist = round(macd_line - macd_signal, 2)
    return macd_line, macd_signal, macd_hist


def compute_support_resistance_levels(
    prev_close: float,
    open_price: float,
    high_price: float,
    low_price: float,
    ltp: float,
) -> Dict[str, Any]:
    """
    Compute intraday classical and pivot levels (Pivot, R1, R2, S1, S2).
    Evaluates immediate overhead resistance for Bull and support floor for Bear,
    along with percentage clearance (room to run).
    """
    if prev_close <= 0:
        prev_close = open_price if open_price > 0 else ltp
    if high_price <= 0:
        high_price = ltp
    if low_price <= 0:
        low_price = ltp

    pivot = (high_price + low_price + prev_close) / 3.0
    r1 = (2.0 * pivot) - low_price
    s1 = (2.0 * pivot) - high_price
    r2 = pivot + (high_price - low_price)
    s2 = pivot - (high_price - low_price)

    # Bullish: next overhead resistance level strictly above LTP
    # (If LTP is testing or breaking HOD, high_price is breached; look at R1/R2)
    ext_resistances = [r1, r2] if ltp >= high_price * 0.9995 else [high_price, r1, r2]
    resistances = sorted([r for r in ext_resistances if r > ltp * 1.0005])
    next_resistance = resistances[0] if resistances else (high_price * 1.02)
    bull_clearance_pct = round(((next_resistance - ltp) / ltp) * 100, 2)
    is_near_resistance = bool(bull_clearance_pct <= 0.35 and ltp < next_resistance)

    # Bearish: next support floor strictly below LTP
    # (If LTP is testing or breaking LOD, low_price is breached; look at S1/S2)
    ext_supports = [s1, s2] if ltp <= low_price * 1.0005 else [low_price, s1, s2]
    supports = sorted([s for s in ext_supports if s < ltp * 0.9995], reverse=True)
    next_support = supports[0] if supports else (low_price * 0.98)
    bear_clearance_pct = round(((ltp - next_support) / ltp) * 100, 2)
    is_near_support = bool(bear_clearance_pct <= 0.35 and ltp > next_support)

    return {
        "pivot": round(pivot, 2),
        "r1": round(r1, 2),
        "r2": round(r2, 2),
        "s1": round(s1, 2),
        "s2": round(s2, 2),
        "next_resistance": round(next_resistance, 2),
        "bull_clearance_pct": bull_clearance_pct,
        "is_near_resistance": is_near_resistance,
        "next_support": round(next_support, 2),
        "bear_clearance_pct": bear_clearance_pct,
        "is_near_support": is_near_support,
    }


# ---------------------------------------------------------------------------
# Option Liquidity Guard: Bid-Ask Spread & Order Depth Validator
# ---------------------------------------------------------------------------

def check_option_liquidity(
    quote: Any,
    lot_size: int = 1,
    max_spread_pct: float = 3.0,
    min_depth_lots: int = 1,
    now: Optional[datetime] = None,
) -> Tuple[bool, float, str]:
    """
    Evaluate the real-time liquidity and execution quality of an option contract.
    Returns:
        (is_liquid: bool, spread_pct: float, reason: str)
    Checks:
    1. Bid-Ask Spread: Spread must be <= max_spread_pct (default 3.0%).
    2. Depth Availability: Best ask (sell[0]) and best bid (buy[0]) must have >= required lot size.
    3. Activity / Depth Presence: Two-way market depth must exist.
    """
    if not quote or not isinstance(quote, dict):
        return False, 0.0, "Missing quote data"

    if now is None:
        now = datetime.now()
    is_post_market = (now.hour > 15) or (now.hour == 15 and now.minute >= 30) or (now.hour < 9) or (now.hour == 9 and now.minute < 15)
    if is_post_market:
        # Off-hours: Order book is closed, bypass live spread gating
        return True, 0.0, "Post-market (bypassed)"

    depth = quote.get("depth")
    if not isinstance(depth, dict):
        return False, 0.0, "Order book depth unavailable"

    buys = depth.get("buy") or []
    sells = depth.get("sell") or []

    if not buys or not isinstance(buys[0], dict) or safe_float(buys[0].get("price"), 0.0) <= 0:
        return False, 0.0, "Zero Bids (No buyers in market depth)"

    if not sells or not isinstance(sells[0], dict) or safe_float(sells[0].get("price"), 0.0) <= 0:
        return False, 0.0, "Zero Asks (No sellers in market depth)"

    best_bid = safe_float(buys[0].get("price"), 0.0)
    best_ask = safe_float(sells[0].get("price"), 0.0)
    bid_qty = safe_int(buys[0].get("quantity"), 0)
    ask_qty = safe_int(sells[0].get("quantity"), 0)

    mid_price = (best_ask + best_bid) / 2.0
    if mid_price <= 0:
        return False, 0.0, "Invalid mid price <= 0"

    spread_pct = round(((best_ask - best_bid) / mid_price) * 100, 2)

    # 1. Spread Check
    if spread_pct > max_spread_pct:
        return False, spread_pct, f"Wide Spread ({spread_pct:.1f}% > {max_spread_pct:.1f}% max limit)"

    # 2. Depth Quantity Check
    req_qty = lot_size * min_depth_lots
    if ask_qty < req_qty:
        return False, spread_pct, f"Thin Ask Depth ({ask_qty} avail < {req_qty} req for 1 lot)"
    if bid_qty < req_qty:
        return False, spread_pct, f"Thin Bid Depth ({bid_qty} avail < {req_qty} req for 1 lot)"

    return True, spread_pct, "Liquid"


# ---------------------------------------------------------------------------
# Theoretical Option Pricing Fallback (Black-Scholes CE & PE)
# ---------------------------------------------------------------------------

def calculate_theoretical_option_premium(
    spot: float,
    strike: float,
    expiry_date_str: str,
    option_type: str = "PE",
    annual_volatility: float = 0.24,
    risk_free_rate: float = 0.07,
) -> float:
    """
    Black-Scholes estimation fallback for both Calls (CE) and Puts (PE).
    Used when market quotes are temporarily missing or after-hours.
    """
    try:
        today = date.today()
        if expiry_date_str:
            exp_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
            days_to_expiry = max(0.5, (exp_date - today).days)
        else:
            days_to_expiry = 3.0
    except Exception:
        days_to_expiry = 3.0

    T = days_to_expiry / 365.0
    S = max(0.01, spot)
    K = max(0.01, strike)
    sigma = max(0.05, annual_volatility)
    r = risk_free_rate
    is_put = option_type.upper() == "PE"

    intrinsic = max(0.0, (K - S) if is_put else (S - K))

    if T <= 0.001:
        return round(max(0.05, intrinsic), 2)

    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        def norm_cdf(x: float) -> float:
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        if is_put:
            price = K * math.exp(-r * T) * norm_cdf(-d2) - S * norm_cdf(-d1)
        else:
            price = S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

        return round(max(0.05, price), 2)
    except Exception:
        time_value = S * sigma * math.sqrt(T) * 0.4
        return round(max(0.05, intrinsic + time_value), 2)


# Backward compatibility alias
calculate_theoretical_pe_premium = calculate_theoretical_option_premium


# ---------------------------------------------------------------------------
# Bidirectional Flag Pattern Tracker (Bull Flag & Bear Flag)
# ---------------------------------------------------------------------------

class FlagPatternTracker:
    """
    Bidirectional state machine tracking both Bull and Bear flags:
    - BULL:
      1. POLE_DETECTED: Impulse surge to upper 15% (range_pos >= 0.85).
      2. FLAG_COILING: Pausing in upper 20% on dry volume (RVOL_5m < 0.70x).
      3. TRIGGERED: Breakout above Flag High with volume expansion -> Triggers CE (Call).
    - BEAR:
      1. POLE_DETECTED: Impulse drop to lower 15% (range_pos <= 0.15).
      2. FLAG_COILING: Pausing in lower 20% on dry volume (RVOL_5m < 0.70x).
      3. TRIGGERED: Breakdown below Flag Low with volume expansion -> Triggers PE (Put).
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = "MONITORING"
        self.direction = "NEUTRAL"       # 'BULL', 'BEAR', or 'NEUTRAL'
        self.coiling_cycles = 0
        self.flag_high = 0.0
        self.flag_low = float("inf")
        self.flag_height = 0.0
        self.trigger_price = 0.0
        self.trigger_time: Optional[str] = None
        self.cooldown_until: float = 0.0

    def update(
        self,
        ltp: float,
        high: float,
        low: float,
        range_pos: float,
        rvol_5m: float,
        rvol_cum: float,
        stage: str,
        score: int = 50,
        now: Optional[datetime] = None,
        vol_accel: float = 1.0,
        is_dry_extreme: bool = False,
        clearance_pct: float = 1.0,
        is_near_resistance: bool = False,
        is_near_support: bool = False,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Process a new market snapshot for this stock and return (state, event_details).
        """
        if now is None:
            now = datetime.now()
        now_ts = now.timestamp()
        time_str = now.strftime("%H:%M:%S")

        # Check cooldown
        if self.state == "COOLDOWN":
            if now_ts >= self.cooldown_until:
                self.state = "MONITORING"
                self._reset()
            else:
                return "COOLDOWN", {}

        if self.state == "IN_TRADE":
            return "IN_TRADE", {}

        if self.state == "TRIGGERED":
            return "TRIGGERED", self._build_trigger_event(stage, rvol_5m)

        # -------------------------------------------------------------------
        # 0. STRICT ENTRY ON CONFIRMED BREAKOUT OR BREAKDOWN
        # -------------------------------------------------------------------
        # Triggers ONLY on genuine confirmed breakouts (💥 Flag Break or 🚀/🩸 Macro Break)
        # Strictly rejects:
        # 1. Coiling states ('BULL FLAG', 'BEAR FLAG')
        # 2. Developmental states ('FORMING', 'CRACKING') - eliminates premature channel entries!
        # 3. Dry volume fakeout traps (is_dry_extreme)
        # 4. Trapped directly under overhead resistance / support floor
        is_breakout = (
            ("BREAKOUT" in stage)
            and score >= 75
            and not is_dry_extreme
            and not (is_near_resistance and vol_accel < 1.0)
        )
        is_breakdown = (
            ("BREAKDOWN" in stage)
            and score >= 75
            and not is_dry_extreme
            and not (is_near_support and vol_accel < 1.0)
        )

        if is_breakout or is_breakdown:
            direction = "BULL" if is_breakout else "BEAR"
            option_type = "CE" if is_breakout else "PE"

            day_span = max(high - low, ltp * 0.01)
            risk_dist = max(day_span * 0.25, ltp * 0.008)

            if is_breakout:
                flag_high = ltp
                flag_low = max(low, ltp - risk_dist)
                flag_height = max(ltp - flag_low, ltp * 0.008)
            else:
                flag_low = ltp
                flag_high = min(high, ltp + risk_dist)
                flag_height = max(flag_high - ltp, ltp * 0.008)

            self.state = "TRIGGERED"
            self.direction = direction
            self.trigger_price = ltp
            self.trigger_time = time_str
            self.flag_high = flag_high
            self.flag_low = flag_low
            self.flag_height = flag_height
            self.vol_accel = vol_accel
            self.clearance_pct = clearance_pct

            return "TRIGGERED", {
                "symbol": self.symbol,
                "direction": direction,
                "option_type": option_type,
                "trigger_price": self.trigger_price,
                "flag_high": self.flag_high,
                "flag_low": self.flag_low,
                "flag_height": self.flag_height,
                "coiling_cycles": self.coiling_cycles,
                "rvol_5m": rvol_5m,
                "vol_accel": vol_accel,
                "clearance_pct": clearance_pct,
                "trigger_time": self.trigger_time,
                "trigger_source": stage,
            }

        # -------------------------------------------------------------------
        # 1. MONITORING -> POLE_DETECTED (Bull or Bear)
        # -------------------------------------------------------------------
        if self.state == "MONITORING":
            # Bull Flag Pole: Stock pushes into top 15% of day's range
            if range_pos >= 0.85 and ltp > 0:
                self.state = "POLE_DETECTED"
                self.direction = "BULL"
                day_span = max(high - low, ltp * 0.01)
                self.flag_high = ltp
                self.flag_low = max(low, ltp - max(day_span * 0.20, ltp * 0.008))
                self.flag_height = max(self.flag_high - self.flag_low, ltp * 0.008)
                self.coiling_cycles = 0
                return "POLE_DETECTED", {"direction": "BULL", "pole_price": ltp, "time": time_str}

            # Bear Flag Pole: Stock plunges into lower 15% of day's range
            elif range_pos <= 0.15 and ltp > 0:
                self.state = "POLE_DETECTED"
                self.direction = "BEAR"
                day_span = max(high - low, ltp * 0.01)
                self.flag_low = ltp
                self.flag_high = min(high, ltp + max(day_span * 0.20, ltp * 0.008))
                self.flag_height = max(self.flag_high - self.flag_low, ltp * 0.008)
                self.coiling_cycles = 0
                return "POLE_DETECTED", {"direction": "BEAR", "pole_price": ltp, "time": time_str}

        # -------------------------------------------------------------------
        # 2. POLE_DETECTED -> FLAG_COILING & BREAKOUT
        # -------------------------------------------------------------------
        if self.state in ("POLE_DETECTED", "FLAG_COILING"):
            if self.direction == "BULL":
                # Invalidation if price retreats too much below 65% of day's range
                if range_pos < 0.65:
                    self.state = "MONITORING"
                    self._reset()
                    return "MONITORING", {"reason": "bull_flag_invalidated"}

                # Check breakout trigger BEFORE updating flag boundaries
                prev_high = self.flag_high
                if self.coiling_cycles >= 1 and ltp >= prev_high and (rvol_5m >= 0.85 or stage in ("🚀 BREAKOUT", "⚡ FORMING")):
                    self.state = "TRIGGERED"
                    self.trigger_price = ltp
                    self.trigger_time = time_str
                    day_span = max(high - low, ltp * 0.01)
                    effective_flag_low = min(self.flag_low, ltp - max(day_span * 0.20, ltp * 0.008))
                    effective_flag_height = max(ltp - effective_flag_low, ltp * 0.008)
                    return "TRIGGERED", {
                        "symbol": self.symbol,
                        "direction": "BULL",
                        "option_type": "CE",
                        "trigger_price": self.trigger_price,
                        "flag_high": self.flag_high,
                        "flag_low": round(effective_flag_low, 2),
                        "flag_height": round(effective_flag_height, 2),
                        "coiling_cycles": self.coiling_cycles,
                        "rvol_5m": rvol_5m,
                        "trigger_time": self.trigger_time,
                        "trigger_source": "FLAG_BREAKOUT",
                    }

                # Update flag boundaries
                self.flag_high = max(self.flag_high, ltp)
                self.flag_low = min(self.flag_low, ltp)
                self.flag_height = max(self.flag_high - self.flag_low, ltp * 0.008)

                # Coiling check
                if rvol_5m < 0.75 and range_pos >= 0.75:
                    self.state = "FLAG_COILING"
                    self.coiling_cycles += 1

            elif self.direction == "BEAR":
                # Invalidation if price bounces above 35% of day's range
                if range_pos > 0.35:
                    self.state = "MONITORING"
                    self._reset()
                    return "MONITORING", {"reason": "bear_flag_invalidated"}

                # Check breakdown trigger BEFORE updating flag boundaries
                prev_low = self.flag_low
                if self.coiling_cycles >= 1 and ltp <= prev_low and (rvol_5m >= 0.85 or stage in ("🩸 BREAKDOWN", "⚡ CRACKING")):
                    self.state = "TRIGGERED"
                    self.trigger_price = ltp
                    self.trigger_time = time_str
                    day_span = max(high - low, ltp * 0.01)
                    effective_flag_high = max(self.flag_high, ltp + max(day_span * 0.20, ltp * 0.008))
                    effective_flag_height = max(effective_flag_high - ltp, ltp * 0.008)
                    return "TRIGGERED", {
                        "symbol": self.symbol,
                        "direction": "BEAR",
                        "option_type": "PE",
                        "trigger_price": self.trigger_price,
                        "flag_high": round(effective_flag_high, 2),
                        "flag_low": self.flag_low,
                        "flag_height": round(effective_flag_height, 2),
                        "coiling_cycles": self.coiling_cycles,
                        "rvol_5m": rvol_5m,
                        "trigger_time": self.trigger_time,
                        "trigger_source": "FLAG_BREAKDOWN",
                    }

                # Update flag boundaries
                self.flag_high = max(self.flag_high, ltp)
                self.flag_low = min(self.flag_low, ltp)
                self.flag_height = max(self.flag_high - self.flag_low, ltp * 0.008)

                # Coiling check
                if rvol_5m < 0.75 and range_pos <= 0.25:
                    self.state = "FLAG_COILING"
                    self.coiling_cycles += 1

        if self.state == "TRIGGERED":
            return "TRIGGERED", self._build_trigger_event(stage, rvol_5m)
        return self.state, {}

    def _build_trigger_event(self, stage: str = "TRIGGERED", rvol_5m: float = 1.0) -> Dict[str, Any]:
        """Construct a guaranteed complete trigger event dictionary."""
        d = self.direction if self.direction in ("BULL", "BEAR") else "BEAR"
        trig_p = self.trigger_price if self.trigger_price > 0 else (self.flag_high if d == "BULL" else self.flag_low)
        if trig_p <= 0:
            trig_p = max(self.flag_high, 1.0)
        return {
            "symbol": self.symbol,
            "direction": d,
            "option_type": "CE" if d == "BULL" else "PE",
            "trigger_price": trig_p,
            "flag_high": self.flag_high if self.flag_high > 0 else trig_p,
            "flag_low": self.flag_low if self.flag_low < float("inf") else trig_p,
            "flag_height": max(self.flag_height, trig_p * 0.005),
            "coiling_cycles": self.coiling_cycles,
            "rvol_5m": rvol_5m,
            "trigger_time": self.trigger_time or datetime.now().strftime("%H:%M:%S"),
            "trigger_source": stage,
        }

    def _reset(self) -> None:
        self.direction = "NEUTRAL"
        self.coiling_cycles = 0
        self.flag_high = 0.0
        self.flag_low = float("inf")
        self.flag_height = 0.0
        self.trigger_price = 0.0
        self.trigger_time = None

    def enter_trade(self) -> None:
        self.state = "IN_TRADE"

    def exit_trade(self, cooldown_seconds: float = 300.0) -> None:
        self.state = "COOLDOWN"
        self.cooldown_until = time.time() + cooldown_seconds
        self._reset()


# Backward compatibility alias
BearFlagTracker = FlagPatternTracker


# ---------------------------------------------------------------------------
# Independent Strike Simulation & Multi-Portfolio Engine
# ---------------------------------------------------------------------------

class StrikeSimulation:
    """
    Independent paper-trading simulation running on its own dedicated initial capital.
    Evaluates trades for a specific strike type (ATM, ITM, or OTM) with isolated capital gating,
    position sizing, risk management, and performance tracking.
    """

    def __init__(
        self,
        strike_type: str,            # 'ATM', 'ITM', or 'OTM'
        starting_capital: float = 100_000.0,
        max_concurrent_setups: Optional[int] = None,  # None = unlimited (capital is the only gate)
        max_lots: int = 1,
        brokerage_per_order: float = 30.0,
        target_pct: float = 5.0,
        r_stop: float = 0.15,
        target_decay_pct: float = 2.5,
        target_decay_mins: float = 30.0,
        red_stagnant_mins: float = 30.0,
        breakeven_target_pct: float = 0.5,
        breakeven_shift_mins: float = 10.0,
        sl_upping_pct: float = 0.33,
        intermediate_trail_trigger_pct: float = 3.5,
        intermediate_trail_floor_pct: float = 2.5,
        trailing_step_pct: float = 1.0,
        target_runner_seconds: float = 30.0,
        max_holding_mins: float = 50.0,
        lower_premium_bias: bool = False,
        trailing_sl_mode: bool = False,
        fixed_target_gain: float = 750.0,
        enable_fixed_target: bool = False,
        disable_profit_floors: bool = False,
    ):
        self.strike_type = strike_type.upper()
        self.starting_capital = starting_capital
        self.available_capital = starting_capital
        self.max_concurrent_setups = max_concurrent_setups
        self.max_lots = max(1, max_lots)
        self.brokerage_per_order = brokerage_per_order
        self.target_pct = target_pct
        self.r_stop = r_stop
        self.target_decay_pct = target_decay_pct
        self.target_decay_mins = target_decay_mins
        self.red_stagnant_mins = red_stagnant_mins
        self.breakeven_target_pct = breakeven_target_pct
        self.breakeven_shift_mins = breakeven_shift_mins
        self.sl_upping_pct = sl_upping_pct
        self.intermediate_trail_trigger_pct = intermediate_trail_trigger_pct
        self.intermediate_trail_floor_pct = intermediate_trail_floor_pct
        self.trailing_step_pct = trailing_step_pct
        self.target_runner_seconds = target_runner_seconds
        self.max_holding_mins = max_holding_mins
        self.lower_premium_bias = lower_premium_bias
        self.trailing_sl_mode = trailing_sl_mode
        self.fixed_target_gain = fixed_target_gain
        self.enable_fixed_target = enable_fixed_target
        self.disable_profit_floors = disable_profit_floors
        self.active_positions: List[PaperPosition] = []
        self.closed_positions: List[PaperPosition] = []
        self.gated_trades_log: List[Dict[str, Any]] = []
        self.trade_counter = 0

    def get_capital_tier_allocation(self) -> Tuple[float, str, float]:
        """
        Dynamic Tiered Capital Gating Logic:
        Calculates maximum allowed trade budget based on current remaining capital
        relative to original capital adjusted for realized P&L.

        User Defined Rules:
        - 50% - 100% Capital: Max 50% of available capital allowed per trade.
        - 25% - 50% Capital: Max 75% of remaining capital allowed per trade.
        - < 25% Capital: 100% of remaining capital allowed (so small capital can still buy >= 1 lot).
        """
        total_realized_pnl = sum(p.realized_pnl for p in self.closed_positions)
        adjusted_capital = max(1_000.0, self.starting_capital + total_realized_pnl)
        capital_ratio = self.available_capital / adjusted_capital

        if capital_ratio >= 0.50:
            tier_name = "TIER 1 (50-100% Capital)"
            alloc_pct = 0.50
        elif capital_ratio >= 0.25:
            tier_name = "TIER 2 (25-50% Capital)"
            alloc_pct = 0.75
        else:
            tier_name = "TIER 3 (< 25% Capital)"
            alloc_pct = 1.00

        max_trade_budget = round(self.available_capital * alloc_pct, 2)
        return alloc_pct, tier_name, max_trade_budget

    def get_metrics(self) -> Dict[str, Any]:
        """Compute performance metrics for this specific strike simulation."""
        all_closed = self.closed_positions
        total_trades = len(all_closed)
        wins = [p for p in all_closed if p.realized_pnl > 0]
        losses = [p for p in all_closed if p.realized_pnl < 0]
        ties = [p for p in all_closed if p.realized_pnl == 0]

        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
        gross_realized_pnl = sum(p.gross_pnl for p in all_closed)
        total_brokerage = sum(p.brokerage for p in all_closed)
        realized_pnl = sum(p.realized_pnl for p in all_closed)
        mtm_pnl = sum(p.mtm_pnl for p in self.active_positions)
        net_pnl = realized_pnl + mtm_pnl

        gross_profit = sum(p.realized_pnl for p in wins)
        gross_loss = abs(sum(p.realized_pnl for p in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        current_val = self.available_capital + sum(p.capital_invested + p.mtm_pnl for p in self.active_positions)
        roi_pct = ((current_val - self.starting_capital) / self.starting_capital * 100) if self.starting_capital > 0 else 0.0
        avg_return_pct = (sum(p.realized_pnl_pct for p in all_closed) / total_trades) if total_trades > 0 else 0.0
        alloc_pct, tier_name, max_trade_budget = self.get_capital_tier_allocation()

        return {
            "strike_type": self.strike_type,
            "starting_capital": self.starting_capital,
            "available_capital": round(self.available_capital, 2),
            "current_portfolio_val": round(current_val, 2),
            "gross_realized_pnl": round(gross_realized_pnl, 2),
            "total_brokerage": round(total_brokerage, 2),
            "realized_pnl": round(realized_pnl, 2),
            "mtm_pnl": round(mtm_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "portfolio_roi_pct": round(roi_pct, 2),
            "avg_roi_pct": round(avg_return_pct, 2),
            "win_rate": round(win_rate, 1),
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "ties": len(ties),
            "profit_factor": round(profit_factor, 2),
            "active_positions_count": len(self.active_positions),
            "capital_tier_name": tier_name,
            "capital_tier_alloc_pct": round(alloc_pct * 100, 1),
            "max_trade_budget": max_trade_budget,
            "max_lots": self.max_lots,
        }


# ---------------------------------------------------------------------------
# Bidirectional Multi-Simulation Paper Trading Portfolio
# ---------------------------------------------------------------------------

class FlagPaperPortfolio:
    """
    Autonomous Paper-Trading Portfolio Engine managing independent strike simulations (ATM, ITM, OTM).
    - Each strike simulation starts with its OWN separate initial capital (e.g. ₹100,000 each).
    - Dynamic Tiered Capital Gating (as defined by user):
      * Tier 1 (50-100% Capital): Max 50% of available capital per trade.
      * Tier 2 (25-50% Capital): Max 75% of remaining capital per trade.
      * Tier 3 (< 25% Capital): 100% of remaining capital allowed.
    - Zero arbitrary trade limits: trades are bounded solely by available capital and gating rules.
    - Side-by-side performance comparison of ATM vs ITM vs OTM execution.
    """

    def __init__(
        self,
        starting_capital: float = 100_000.0,
        direction: str = "both",             # 'both', 'bull', or 'bear'
        strike_mode: str = "all",            # 'all' (3 separate sims), 'atm', 'itm', or 'otm'
        max_concurrent_setups: Optional[int] = None,  # None = unlimited (capital is the only constraint)
        output_csv: str = "bear_flag_paper_trades.csv",
        instrument_csv: str = "instrument.csv",
        manager: Optional[InstrumentManager] = None,
        access_token: Optional[str] = None,
        max_spread_pct: float = 3.0,
        max_lots: int = 1,
        brokerage_per_order: float = 30.0,
        target_pct: float = 5.0,
        r_stop: float = 0.15,
        target_decay_pct: float = 2.5,
        target_decay_mins: float = 30.0,
        red_stagnant_mins: float = 30.0,
        breakeven_target_pct: float = 0.5,
        breakeven_shift_mins: float = 10.0,
        sl_upping_pct: float = 0.33,
        intermediate_trail_trigger_pct: float = 3.5,
        intermediate_trail_floor_pct: float = 2.5,
        trailing_step_pct: float = 1.0,
        target_runner_seconds: float = 30.0,
        max_holding_mins: float = 50.0,
        fixed_target_gain: float = 750.0,
        show_positions: bool = False,
    ):
        self.starting_capital = starting_capital
        self.direction_pref = direction.lower()
        self.strike_mode = strike_mode.lower()
        self.show_positions = show_positions
        self.max_concurrent_setups = max_concurrent_setups
        self.output_csv = output_csv
        self.access_token = access_token
        self.max_spread_pct = max_spread_pct
        self.max_lots = max(1, max_lots)
        self.brokerage_per_order = brokerage_per_order
        self.target_pct = target_pct
        self.r_stop = r_stop
        self.target_decay_pct = target_decay_pct
        self.target_decay_mins = target_decay_mins
        self.red_stagnant_mins = red_stagnant_mins
        self.breakeven_target_pct = breakeven_target_pct
        self.breakeven_shift_mins = breakeven_shift_mins
        self.sl_upping_pct = sl_upping_pct
        self.intermediate_trail_trigger_pct = intermediate_trail_trigger_pct
        self.intermediate_trail_floor_pct = intermediate_trail_floor_pct
        self.trailing_step_pct = trailing_step_pct
        self.target_runner_seconds = target_runner_seconds
        self.max_holding_mins = max_holding_mins
        self.fixed_target_gain = fixed_target_gain
        if not self.access_token:
            try:
                from upstox_common import load_env
                env = load_env(".env")
                self.access_token = env.get("UPSTOX_ACCESS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN")
            except Exception:
                pass

        self.resolver = OptionStrikeResolver(instrument_csv=instrument_csv, manager=manager, access_token=self.access_token)
        self.trackers: Dict[str, FlagPatternTracker] = {}

        # Initialize independent strike simulations on the SAME starting capital:
        # Full Suite:
        # - ATM: At-The-Money baseline
        # - ITM: In-The-Money baseline
        # - OTM: Out-of-The-Money baseline
        # - LOW_PREM: Bias towards lowest premium liquid option (evaluates OTM -> ATM -> ITM)
        # - TRAIL_SL: Tick-by-tick trailing stoploss point-for-point with peak premium
        # - FIXED_TGT: Target is min(pct, ₹750) with profit floors/runners disabled
        # - COMBO: Lower premium bias + Tick-by-tick trailing SL + min(pct, ₹750) target
        self.simulations: Dict[str, StrikeSimulation] = {}

        def _make_sim(s_type: str, low_prem: bool = False, trail_sl: bool = False, fixed_tgt: bool = False) -> StrikeSimulation:
            return StrikeSimulation(
                strike_type=s_type,
                starting_capital=starting_capital,
                max_concurrent_setups=max_concurrent_setups,
                max_lots=self.max_lots,
                brokerage_per_order=self.brokerage_per_order,
                target_pct=self.target_pct,
                r_stop=self.r_stop,
                target_decay_pct=self.target_decay_pct,
                target_decay_mins=self.target_decay_mins,
                red_stagnant_mins=self.red_stagnant_mins,
                breakeven_target_pct=self.breakeven_target_pct,
                breakeven_shift_mins=self.breakeven_shift_mins,
                sl_upping_pct=self.sl_upping_pct,
                intermediate_trail_trigger_pct=self.intermediate_trail_trigger_pct,
                intermediate_trail_floor_pct=self.intermediate_trail_floor_pct,
                trailing_step_pct=self.trailing_step_pct,
                target_runner_seconds=self.target_runner_seconds,
                max_holding_mins=self.max_holding_mins,
                lower_premium_bias=low_prem,
                trailing_sl_mode=trail_sl,
                fixed_target_gain=self.fixed_target_gain,
                enable_fixed_target=fixed_tgt,
                disable_profit_floors=fixed_tgt,
            )

        if self.strike_mode in ("all", "both", "extended"):
            self.simulations["ATM"] = _make_sim("ATM")
            self.simulations["ITM"] = _make_sim("ITM")
            self.simulations["OTM"] = _make_sim("OTM")
            self.simulations["LOW_PREM"] = _make_sim("LOW_PREM", low_prem=True)
            self.simulations["TRAIL_SL"] = _make_sim("TRAIL_SL", trail_sl=True)
            self.simulations["FIXED_TGT"] = _make_sim("FIXED_TGT", fixed_tgt=True)
            self.simulations["COMBO"] = _make_sim("COMBO", low_prem=True, trail_sl=True, fixed_tgt=True)
        elif self.strike_mode in ("atm", "itm", "otm"):
            s_type = self.strike_mode.upper()
            self.simulations[s_type] = _make_sim(s_type)
        elif self.strike_mode == "low_prem":
            self.simulations["LOW_PREM"] = _make_sim("LOW_PREM", low_prem=True)
        elif self.strike_mode == "trail_sl":
            self.simulations["TRAIL_SL"] = _make_sim("TRAIL_SL", trail_sl=True)
        elif self.strike_mode == "fixed_tgt":
            self.simulations["FIXED_TGT"] = _make_sim("FIXED_TGT", fixed_tgt=True)
        elif self.strike_mode == "combo":
            self.simulations["COMBO"] = _make_sim("COMBO", low_prem=True, trail_sl=True, fixed_tgt=True)
        else:
            self.simulations["ATM"] = _make_sim("ATM")

        # Pending trigger executions: symbol -> trigger dict
        self.pending_triggers: Dict[str, Dict[str, Any]] = {}

        self._init_csv_journal()

    # Backwards-compatible convenience properties
    @property
    def active_positions(self) -> List[PaperPosition]:
        return [p for sim in self.simulations.values() for p in sim.active_positions]

    @property
    def closed_positions(self) -> List[PaperPosition]:
        return [p for sim in self.simulations.values() for p in sim.closed_positions]

    @property
    def available_capital(self) -> float:
        first_sim = next(iter(self.simulations.values()), None)
        return first_sim.available_capital if first_sim else self.starting_capital

    @property
    def gated_trades_log(self) -> List[Dict[str, Any]]:
        return [g for sim in self.simulations.values() for g in sim.gated_trades_log]

    def _init_csv_journal(self) -> None:
        """Initialize trade journal CSV file with full headers if not already created."""
        dirname = os.path.dirname(os.path.abspath(self.output_csv))
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        if not os.path.exists(self.output_csv):
            fieldnames = [
                "trade_id",
                "symbol",
                "direction",
                "option_type",
                "strike_type",
                "option_symbol",
                "strike_price",
                "expiry_date",
                "entry_time",
                "entry_underlying",
                "entry_premium",
                "lot_size",
                "lots",
                "quantity",
                "capital_invested",
                "target_premium",
                "stop_loss_premium",
                "stop_loss_underlying",
                "flag_high",
                "flag_low",
                "flag_height",
                "exit_time",
                "exit_underlying",
                "exit_premium",
                "exit_reason",
                "gross_pnl",
                "brokerage",
                "realized_pnl",
                "realized_pnl_pct",
            ]
            with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()

    def _log_trade_to_csv(self, pos: PaperPosition) -> None:
        """Append closed position to CSV journal."""
        fieldnames = [
            "trade_id",
            "symbol",
            "direction",
            "option_type",
            "strike_type",
            "option_symbol",
            "strike_price",
            "expiry_date",
            "entry_time",
            "entry_underlying",
            "entry_premium",
            "lot_size",
            "lots",
            "quantity",
            "capital_invested",
            "target_premium",
            "stop_loss_premium",
            "stop_loss_underlying",
            "flag_high",
            "flag_low",
            "flag_height",
            "exit_time",
            "exit_underlying",
            "exit_premium",
            "exit_reason",
            "gross_pnl",
            "brokerage",
            "realized_pnl",
            "realized_pnl_pct",
        ]
        with open(self.output_csv, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writerow({
                "trade_id": pos.trade_id,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "option_type": pos.option_type,
                "strike_type": pos.strike_type,
                "option_symbol": pos.option_symbol,
                "strike_price": pos.strike_price,
                "expiry_date": pos.expiry_date,
                "entry_time": pos.entry_time,
                "entry_underlying": pos.entry_underlying,
                "entry_premium": pos.entry_premium,
                "lot_size": pos.lot_size,
                "lots": pos.lots,
                "quantity": pos.quantity,
                "capital_invested": pos.capital_invested,
                "target_premium": pos.target_premium,
                "stop_loss_premium": pos.stop_loss_premium,
                "stop_loss_underlying": pos.stop_loss_underlying,
                "flag_high": pos.flag_high,
                "flag_low": pos.flag_low,
                "flag_height": pos.flag_height,
                "exit_time": pos.exit_time,
                "exit_underlying": pos.exit_underlying,
                "exit_premium": pos.exit_premium,
                "exit_reason": pos.exit_reason,
                "gross_pnl": pos.gross_pnl,
                "brokerage": pos.brokerage,
                "realized_pnl": pos.realized_pnl,
                "realized_pnl_pct": pos.realized_pnl_pct,
            })

    def get_capital_tier_allocation(self) -> Tuple[float, str, float]:
        first_sim = next(iter(self.simulations.values()), None)
        if first_sim:
            return first_sim.get_capital_tier_allocation()
        return (0.50, "TIER 1 (50-100% Capital)", round(self.starting_capital * 0.50, 2))

    def get_required_option_keys(self) -> List[str]:
        """Returns list of Upstox instrument keys needed for open positions and pending triggers."""
        keys: Set[str] = set()
        for pos in self.active_positions:
            if pos.option_key:
                keys.add(pos.option_key)

        for sym, trig in self.pending_triggers.items():
            strikes: ResolvedStrikes = trig["resolved_strikes"]
            for inst in [strikes.atm_inst, strikes.itm_inst, strikes.otm_inst]:
                if inst and inst.upstox_instrument_key:
                    keys.add(inst.upstox_instrument_key)

        return list(keys)

    def _fetch_live_option_quotes(self, keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fetch fresh live quotes on-demand directly from Upstox API for specific option instrument keys."""
        if not keys or not self.access_token:
            return {}
        try:
            from stream_rvol_scanner import fetch_live_quotes_batch
            return fetch_live_quotes_batch(keys, self.access_token)
        except Exception:
            return {}

    def process_underlying_tick(
        self,
        symbol: str,
        ltp: float,
        high: float,
        low: float,
        range_pos: float,
        rvol_5m: float,
        rvol_cum: float,
        stage: str,
        score: int = 50,
        now: Optional[datetime] = None,
        vol_accel: float = 1.0,
        is_dry_extreme: bool = False,
        clearance_pct: float = 1.0,
        is_near_resistance: bool = False,
        is_near_support: bool = False,
        rsi: float = 50.0,
        macd_hist: float = 0.0,
    ) -> Optional[List[str]]:
        """
        Update bidirectional state machine for an underlying stock.
        Triggers Bull Breakouts (CE) or Bear Breakdowns (PE).
        """
        if ltp <= 0:
            return None

        if symbol not in self.trackers:
            self.trackers[symbol] = FlagPatternTracker(symbol)

        tracker = self.trackers[symbol]
        state, event = tracker.update(
            ltp=ltp,
            high=high,
            low=low,
            range_pos=range_pos,
            rvol_5m=rvol_5m,
            rvol_cum=rvol_cum,
            stage=stage,
            score=score,
            now=now,
            vol_accel=vol_accel,
            is_dry_extreme=is_dry_extreme,
            clearance_pct=clearance_pct,
            is_near_resistance=is_near_resistance,
            is_near_support=is_near_support,
        )

        if state == "TRIGGERED" and symbol not in self.pending_triggers:
            # Check market hours & intraday entry cutoff (no new intraday entries after 15:15 IST or post-market)
            t_now = now or datetime.now()
            is_past_entry_cutoff = (t_now.hour > 15) or (t_now.hour == 15 and t_now.minute >= 15) or (t_now.hour < 9) or (t_now.hour == 9 and t_now.minute < 15)
            if is_past_entry_cutoff:
                return None

            # Check direction preference
            direction = event.get("direction", "BEAR")
            if self.direction_pref == "bull" and direction != "BULL":
                return None
            if self.direction_pref == "bear" and direction != "BEAR":
                return None

            # Check if active positions already exist in ALL simulations for this symbol
            active_symbols = {p.symbol for p in self.active_positions}
            if symbol in active_symbols:
                return None

            # Optional max concurrent setups check (only if explicitly configured)
            if self.max_concurrent_setups is not None:
                active_setup_count = len({p.symbol for p in self.active_positions})
                if active_setup_count >= self.max_concurrent_setups:
                    for sim in self.simulations.values():
                        sim.gated_trades_log.append({
                            "time": now.strftime("%H:%M:%S") if now else datetime.now().strftime("%H:%M:%S"),
                            "symbol": symbol,
                            "direction": direction,
                            "reason": f"GATED: Max concurrent setups ({self.max_concurrent_setups}) reached",
                        })
                    return None

            option_type = event.get("option_type", "PE")
            resolved = self.resolver.resolve_strikes(symbol, ltp, option_type=option_type)
            if not resolved or not resolved.is_valid:
                for sim in self.simulations.values():
                    sim.gated_trades_log.append({
                        "time": now.strftime("%H:%M:%S") if now else datetime.now().strftime("%H:%M:%S"),
                        "symbol": symbol,
                        "direction": direction,
                        "reason": f"SKIPPED: No F&O option contracts available for {symbol}",
                    })
                return None

            # Calculate Cross-Sectional Alpha Score for relative competition among qualified candidates
            confluence_pts = 0.0
            if (direction == "BULL" and 55.0 <= rsi <= 75.0) or (direction == "BEAR" and 25.0 <= rsi <= 45.0):
                confluence_pts += 4.0
            if (direction == "BULL" and macd_hist > 0) or (direction == "BEAR" and macd_hist < 0):
                confluence_pts += 3.0

            # 1. Clamped Volume Acceleration adjustment: bounded to [-10.0, +15.0] points
            vol_accel_adj = round(max(-10.0, min(15.0, 15.0 * (vol_accel - 1.0))), 2)

            # 2. S/R Clearance adjustment: clearance_pct is in percentage (e.g. 2.5%), bounded to [0.0, 10.0] points
            sr_clearance_adj = round(min(10.0, max(0.0, clearance_pct * 2.0)), 2)

            # 3. Indicator Confluence adjustment: bounded to [0.0, 7.0] points
            confluence_adj = round(confluence_pts, 2)

            alpha_score = round(float(score) + vol_accel_adj + sr_clearance_adj + confluence_adj, 2)

            self.pending_triggers[symbol] = {
                "event": event,
                "resolved_strikes": resolved,
                "score": score,
                "rvol_5m": rvol_5m,
                "vol_accel": vol_accel,
                "clearance_pct": clearance_pct,
                "rsi": rsi,
                "macd_hist": macd_hist,
                "vol_accel_adj": vol_accel_adj,
                "sr_clearance_adj": sr_clearance_adj,
                "confluence_adj": confluence_adj,
                "alpha_score": alpha_score,
                "trigger_time": now.strftime("%H:%M:%S") if now else datetime.now().strftime("%H:%M:%S"),
                "created_time": time.time(),
            }

            new_keys = []
            for inst in [resolved.atm_inst, resolved.itm_inst, resolved.otm_inst]:
                if inst and inst.upstox_instrument_key:
                    new_keys.append(inst.upstox_instrument_key)
            return new_keys

        return None

    def process_market_quotes(
        self,
        underlying_quotes: Dict[str, Any],
        option_quotes: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> None:
        """
        Executes pending triggers and evaluates exit rules across all active simulations.
        Guarantees that trades are filled at real live market prices (Ask for Buy, Bid/LTP for Exit).
        """
        if option_quotes is None:
            option_quotes = underlying_quotes
        if now is None:
            now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        is_eod = (now.hour == 15 and now.minute >= 20) or (now.hour > 15)
        is_post_market = (now.hour > 15) or (now.hour == 15 and now.minute >= 30) or (now.hour < 9) or (now.hour == 9 and now.minute < 15)

        # If past EOD cutoff (15:20 IST) or post-market, cancel all pending triggers immediately
        if is_eod and self.pending_triggers:
            for sym, trig in list(self.pending_triggers.items()):
                for sim in self.simulations.values():
                    sim.gated_trades_log.append({
                        "time": time_str,
                        "symbol": sym,
                        "direction": trig.get("event", {}).get("direction", "BEAR"),
                        "reason": "CANCELLED: Past intraday entry cutoff (15:20 IST EOD Square-Off)",
                    })
            self.pending_triggers.clear()

        # -------------------------------------------------------------------
        # 1. Pre-fetch Live Option Quotes for Pending Triggers if Missing
        # -------------------------------------------------------------------
        needed_opt_keys: Set[str] = set()
        for sym, trig in self.pending_triggers.items():
            resolved: Optional[ResolvedStrikes] = trig.get("resolved_strikes")
            if resolved:
                for inst in [resolved.atm_inst, resolved.itm_inst, resolved.otm_inst]:
                    if inst and inst.upstox_instrument_key:
                        k = inst.upstox_instrument_key
                        q = (
                            option_quotes.get(k)
                            or option_quotes.get(k.replace("|", ":"))
                            or option_quotes.get(inst.trading_symbol)
                        )
                        if not q or extract_live_option_price(q, side="BUY") <= 0:
                            needed_opt_keys.add(k)

        if needed_opt_keys and self.access_token:
            fresh_quotes = self._fetch_live_option_quotes(list(needed_opt_keys))
            if fresh_quotes:
                option_quotes.update(fresh_quotes)

        # -------------------------------------------------------------------
        # 2. Execute Pending Triggers across Independent Strike Simulations
        # (Only execute during active trading hours before 15:20 EOD cutoff!)
        # -------------------------------------------------------------------
        if is_eod:
            if self.pending_triggers:
                for sym, trig in list(self.pending_triggers.items()):
                    for sim in self.simulations.values():
                        sim.gated_trades_log.append({
                            "time": time_str,
                            "symbol": sym,
                            "direction": trig.get("event", {}).get("direction", "BEAR"),
                            "reason": "CANCELLED: Past intraday cutoff (15:20 IST EOD Square-Off)",
                        })
                self.pending_triggers.clear()
            sorted_pending = []
        else:
            sorted_pending = sorted(
                self.pending_triggers.items(),
                key=lambda item: (
                    item[1].get("alpha_score", 0.0),
                    item[1].get("score", 0),
                    item[1].get("rvol_5m", 0.0),
                ),
                reverse=True,
            )

        symbols_to_remove = []
        for sym, trig in sorted_pending:
            resolved: Optional[ResolvedStrikes] = trig.get("resolved_strikes")
            if not resolved or not resolved.is_valid:
                symbols_to_remove.append(sym)
                continue

            event = trig.get("event") or {}
            tracker_inst = self.trackers.get(sym)
            direction = event.get("direction") or getattr(tracker_inst, "direction", "BEAR")
            option_type = event.get("option_type") or ("CE" if direction == "BULL" else "PE")
            underlying_ltp = event.get("trigger_price") or getattr(tracker_inst, "trigger_price", 0.0)
            flag_high = event.get("flag_high") or getattr(tracker_inst, "flag_high", underlying_ltp)
            flag_low = event.get("flag_low") or getattr(tracker_inst, "flag_low", underlying_ltp)
            flag_height = event.get("flag_height") or getattr(tracker_inst, "flag_height", max(abs(flag_high - flag_low), underlying_ltp * 0.005))

            # Pre-Execution Setup Validity Guard:
            # Check fresh underlying quote if available in underlying_quotes
            fresh_und_quote = underlying_quotes.get(sym)
            if not fresh_und_quote or not isinstance(fresh_und_quote, dict):
                for k, v in underlying_quotes.items():
                    if sym in k and isinstance(v, dict):
                        fresh_und_quote = v
                        break

            live_und_ltp = underlying_ltp
            if fresh_und_quote and isinstance(fresh_und_quote, dict):
                raw_p = fresh_und_quote.get("last_price")
                if raw_p is None and isinstance(fresh_und_quote.get("ohlc"), dict):
                    raw_p = fresh_und_quote["ohlc"].get("close")
                if raw_p and float(raw_p) > 0:
                    live_und_ltp = float(raw_p)

            # 1. Flag Consolidation Width Gate: Scalping flags should be tight (<= 2.5% of LTP)
            flag_width_pct = (flag_height / underlying_ltp) * 100.0 if underlying_ltp > 0 else 0.0
            if flag_width_pct > 2.5:
                for sim in self.simulations.values():
                    sim.gated_trades_log.append({
                        "time": time_str,
                        "symbol": sym,
                        "direction": direction,
                        "reason": f"SKIPPED: Flag consolidation too wide ({flag_width_pct:.1f}% > 2.5% max limit)",
                    })
                symbols_to_remove.append(sym)
                continue

            # 2. Wick Reversal Invalidation: Did underlying slip back inside the pattern prior to fill?
            if fresh_und_quote and live_und_ltp > 0 and flag_high > 0 and flag_low > 0:
                if direction == "BULL" and live_und_ltp < (flag_high * 0.997):
                    for sim in self.simulations.values():
                        sim.gated_trades_log.append({
                            "time": time_str,
                            "symbol": sym,
                            "direction": direction,
                            "reason": f"CANCELLED: Breakout wicked back inside flag before fill (LTP ₹{live_und_ltp:.2f} < ₹{flag_high * 0.997:.2f})",
                        })
                    symbols_to_remove.append(sym)
                    continue
                elif direction == "BEAR" and live_und_ltp > (flag_low * 1.003):
                    for sim in self.simulations.values():
                        sim.gated_trades_log.append({
                            "time": time_str,
                            "symbol": sym,
                            "direction": direction,
                            "reason": f"CANCELLED: Breakdown wicked back inside flag before fill (LTP ₹{live_und_ltp:.2f} > ₹{flag_low * 1.003:.2f})",
                        })
                    symbols_to_remove.append(sym)
                    continue

            strike_map = {
                "ATM": resolved.atm_inst,
                "ITM": resolved.itm_inst,
                "OTM": resolved.otm_inst,
            }

            any_entered_for_sym = False
            missing_quote_for_any_sim = False

            # Each simulation operates on its OWN separate starting capital and budget!
            for s_type, sim in self.simulations.items():
                # Check if this simulation already has an active position in sym
                if any(p.symbol == sym for p in sim.active_positions):
                    continue

                if getattr(sim, "lower_premium_bias", False):
                    # a) Evaluates liquid options (OTM -> ATM -> ITM), selecting lowest premium with spread <= max_spread_pct
                    cands = [resolved.otm_inst, resolved.atm_inst, resolved.itm_inst]
                    valid_cands = []
                    for c_inst in cands:
                        if not c_inst:
                            continue
                        c_key = c_inst.upstox_instrument_key
                        c_quote = (
                            option_quotes.get(c_key)
                            or option_quotes.get(c_key.replace("|", ":"))
                            or option_quotes.get(c_inst.trading_symbol)
                        )
                        c_prem = extract_live_option_price(c_quote, side="BUY")
                        if c_prem <= 0:
                            if not self.access_token:
                                c_prem = calculate_theoretical_option_premium(
                                    spot=underlying_ltp,
                                    strike=c_inst.strike_price,
                                    expiry_date_str=c_inst.expiry_date,
                                    option_type=option_type,
                                )
                        is_liq = True
                        if c_quote and self.access_token and not is_post_market:
                            is_liq, _, _ = check_option_liquidity(
                                quote=c_quote,
                                lot_size=c_inst.lot_size,
                                max_spread_pct=self.max_spread_pct,
                                now=now,
                            )
                        if c_prem > 0 and is_liq:
                            valid_cands.append((c_prem, c_inst, c_quote))

                    if valid_cands:
                        valid_cands.sort(key=lambda x: x[0])
                        opt_premium, inst, opt_quote = valid_cands[0]
                    else:
                        inst = resolved.otm_inst or resolved.atm_inst
                        opt_key = inst.upstox_instrument_key if inst else None
                        opt_quote = (
                            option_quotes.get(opt_key)
                            or option_quotes.get(opt_key.replace("|", ":"))
                            or (option_quotes.get(inst.trading_symbol) if inst else None)
                        )
                        opt_premium = extract_live_option_price(opt_quote, side="BUY") if opt_quote else 0.0
                else:
                    inst = strike_map.get(s_type, resolved.atm_inst)
                    if inst is None:
                        continue

                    # Lookup live quote from Upstox
                    opt_key = inst.upstox_instrument_key
                    opt_quote = (
                        option_quotes.get(opt_key)
                        or option_quotes.get(opt_key.replace("|", ":"))
                        or option_quotes.get(inst.trading_symbol)
                    )

                    # Extract live market price (prefers Ask depth price, then LTP, then close)
                    opt_premium = extract_live_option_price(opt_quote, side="BUY")

                # In LIVE mode with an active access token, NEVER enter on a fake Black-Scholes estimate!
                if opt_premium <= 0:
                    if self.access_token:
                        missing_quote_for_any_sim = True
                        continue
                    else:
                        # Offline mock/test fallback only
                        opt_premium = calculate_theoretical_option_premium(
                            spot=underlying_ltp,
                            strike=inst.strike_price,
                            expiry_date_str=inst.expiry_date,
                            option_type=option_type,
                        )

                # Option Liquidity Guard: Check Bid-Ask spread & depth
                is_liquid, spread_pct, liq_reason = check_option_liquidity(
                    quote=opt_quote,
                    lot_size=inst.lot_size,
                    max_spread_pct=self.max_spread_pct,
                    now=now,
                )

                if not is_liquid and not is_post_market and self.access_token:
                    reason_str = f"GATED (ILLIQUID): {inst.trading_symbol} {liq_reason}"
                    sim.gated_trades_log.append({
                        "time": time_str,
                        "symbol": sym,
                        "direction": direction,
                        "reason": reason_str,
                    })
                    print(
                        f"\n{Colors.BG_YELLOW}{Colors.BLACK}{Colors.BOLD} ⚠️  [{s_type} SIM] LIQUIDITY REJECT: {sym} ({direction}) | {reason_str} {Colors.RESET}"
                    )
                    continue

                cost_1_lot = opt_premium * inst.lot_size

                # Dynamic Tiered Capital Gating for THIS simulation
                alloc_pct, tier_name, max_trade_budget = sim.get_capital_tier_allocation()

                # Flexible single-lot budget allowance:
                # Lot sizes in Indian options are discrete (e.g. 250, 500 shares).
                # To prevent rejecting an affordable single lot (e.g. ₹16k trade when ₹30.5k is available),
                # provide a realistic flexibility buffer specifically for 1-lot executions:
                # - In Tier 1 (50% guideline): allow up to 75% of available cash (or 50% buffer on budget)
                # - In Tier 2 (75% guideline): allow up to 85% of available cash (or 25% buffer on budget)
                # - In Tier 3 (< 25% capital): allow up to 100% of available cash
                if alloc_pct <= 0.50:
                    single_lot_budget = min(sim.available_capital, max(max_trade_budget * 1.50, sim.available_capital * 0.75))
                elif alloc_pct <= 0.75:
                    single_lot_budget = min(sim.available_capital, max(max_trade_budget * 1.25, sim.available_capital * 0.85))
                else:
                    single_lot_budget = sim.available_capital

                # Gating Check: reject ONLY if even 1 single lot exceeds the flexible single-lot budget OR available cash!
                if (cost_1_lot > single_lot_budget or cost_1_lot > sim.available_capital) and sim.available_capital > 0:
                    reason_str = f"GATED ({tier_name}): 1-lot cost (₹{cost_1_lot:,.0f}) exceeds flexible budget (₹{single_lot_budget:,.0f}) [Avail: ₹{sim.available_capital:,.0f}]"
                    sim.gated_trades_log.append({
                        "time": time_str,
                        "symbol": sym,
                        "direction": direction,
                        "reason": reason_str,
                    })
                    print(
                        f"\n{Colors.BG_YELLOW}{Colors.BLACK}{Colors.BOLD} ⚠️  [{s_type} SIM] TRADE GATED: {sym} ({direction}) | {reason_str} {Colors.RESET}"
                    )
                    continue

                if cost_1_lot <= single_lot_budget and cost_1_lot <= sim.available_capital:
                    lots = min(sim.max_lots, max(1, int(max_trade_budget // cost_1_lot)))
                    cost = round(opt_premium * inst.lot_size * lots, 2)
                    if cost > sim.available_capital:
                        lots = min(sim.max_lots, max(1, int(sim.available_capital // cost_1_lot)))
                        cost = round(opt_premium * inst.lot_size * lots, 2)

                    if cost <= sim.available_capital and lots > 0:
                        quantity = inst.lot_size * lots
                        sim.trade_counter += 1
                        trade_id = f"SIM-{s_type}-{sym}-{direction[:1]}-{sim.trade_counter:03d}"

                        # Brokerage: ₹30 entry + ₹30 exit = ₹60 round-trip
                        roundtrip_brokerage = sim.brokerage_per_order * 2.0

                        # Fast Scalp Target: 5% of premium paid + round-trip brokerage (or fixed target gain if enabled)
                        target_net_profit = round((sim.target_pct / 100.0) * cost, 2)
                        if getattr(sim, "enable_fixed_target", False) and getattr(sim, "fixed_target_gain", 750.0) > 0:
                            target_net_profit = round(min(target_net_profit, sim.fixed_target_gain), 2)
                        target_gross_gain = round(target_net_profit + roundtrip_brokerage, 2)
                        target_prem = round(opt_premium + (target_gross_gain / quantity), 2)

                        # Option Stop Loss: R = 0.15 (15% drop from entry premium)
                        stop_prem = round(max(0.05, opt_premium * (1.0 - sim.r_stop)), 2)

                        # Underlying Risk Levels:
                        # Structural invalidation buffer: at least 0.80% minimum buffer to prevent noise choking,
                        # anchored strictly at or beyond the consolidation flag boundary (never inside!).
                        und_risk_buffer = max(0.008, flag_height / underlying_ltp if flag_height > 0 else 0.008)
                        if direction == "BULL":
                            sl_underlying = round(min(flag_low, underlying_ltp * (1.0 - und_risk_buffer)), 2)
                        else:  # BEAR
                            sl_underlying = round(max(flag_high, underlying_ltp * (1.0 + und_risk_buffer)), 2)

                        pos = PaperPosition(
                            trade_id=trade_id,
                            symbol=sym,
                            direction=direction,
                            option_type=option_type,
                            strike_type=s_type,
                            option_symbol=inst.trading_symbol,
                            option_key=inst.upstox_instrument_key,
                            strike_price=inst.strike_price,
                            expiry_date=inst.expiry_date,
                            lot_size=inst.lot_size,
                            lots=lots,
                            quantity=quantity,
                            entry_time=trig["trigger_time"],
                            entry_epoch=now.timestamp() if now else time.time(),
                            entry_underlying=underlying_ltp,
                            entry_premium=opt_premium,
                            capital_invested=cost,
                            flag_high=flag_high,
                            flag_low=flag_low,
                            flag_height=flag_height,
                            stop_loss_underlying=sl_underlying,
                            initial_sl_underlying=sl_underlying,
                            target_premium=target_prem,
                            stop_loss_premium=stop_prem,
                            target_gross_gain=target_gross_gain,
                            target_net_profit=target_net_profit,
                            initial_target_pct=sim.target_pct,
                            initial_r_stop=sim.r_stop,
                            current_target_pct=sim.target_pct,
                            current_r_stop=sim.r_stop,
                            current_target_premium=target_prem,
                            current_stop_loss_premium=stop_prem,
                            current_target_gross_gain=target_gross_gain,
                            current_stop_loss_underlying=sl_underlying,
                            brokerage_per_order=sim.brokerage_per_order,
                            brokerage=roundtrip_brokerage,
                            current_underlying=underlying_ltp,
                            current_premium=opt_premium,
                            trailing_sl_mode=getattr(sim, "trailing_sl_mode", False),
                            enable_fixed_target=getattr(sim, "enable_fixed_target", False),
                            fixed_target_gain=getattr(sim, "fixed_target_gain", 750.0),
                            disable_profit_floors=getattr(sim, "disable_profit_floors", False),
                            lower_premium_bias=getattr(sim, "lower_premium_bias", False),
                        )

                        sim.active_positions.append(pos)
                        sim.available_capital -= cost
                        any_entered_for_sym = True

                        alpha_disp = trig.get("alpha_score", 0.0)
                        base_disp = trig.get("score", 0)
                        vol_disp = trig.get("vol_accel_adj", 0.0)
                        sr_disp = trig.get("sr_clearance_adj", 0.0)
                        conf_disp = trig.get("confluence_adj", 0.0)
                        print(
                            f"\n{Colors.BG_GREEN}{Colors.BLACK}{Colors.BOLD} 🎯 [{s_type} SIM] LIVE TRADE EXECUTED: {sym} ({direction} {option_type}) | "
                            f"{inst.trading_symbol} ({lots}L @ ₹{opt_premium:.2f}) | Alpha: {alpha_disp:.1f} (Base: {base_disp}, Vol: {vol_disp:+0.1f}, SR: {sr_disp:+0.1f}, Conf: {conf_disp:+0.1f}) | "
                            f"TGT: ₹{target_prem:.2f} (Rolling 5->2.5%+₹{roundtrip_brokerage:.0f}) | SL: ₹{stop_prem:.2f} (-{sim.r_stop*100:.0f}%) | Cost: ₹{cost:,.0f} {Colors.RESET}"
                        )

            if any_entered_for_sym:
                if sym in self.trackers:
                    self.trackers[sym].enter_trade()

            # Handle pending trigger removal:
            # If any trades entered, or if timed out (> 20s), or if not waiting for live quotes, remove it.
            # If still waiting for Upstox live quote, keep in pending_triggers for the next tick!
            created_t = trig.get("created_time", time.time())
            is_timed_out = (time.time() - created_t) > 20.0

            if any_entered_for_sym or is_timed_out or not missing_quote_for_any_sim:
                symbols_to_remove.append(sym)
                if is_timed_out and not any_entered_for_sym:
                    for sim in self.simulations.values():
                        sim.gated_trades_log.append({
                            "time": time_str,
                            "symbol": sym,
                            "direction": direction,
                            "reason": f"SKIPPED: Live option quotes unavailable from Upstox API after 20s",
                        })

        for sym in symbols_to_remove:
            self.pending_triggers.pop(sym, None)

        # -------------------------------------------------------------------
        # 3. Evaluate Exits & MTM on Active Positions for EACH Simulation
        # -------------------------------------------------------------------
        for s_type, sim in self.simulations.items():
            remaining_positions: List[PaperPosition] = []

            for pos in sim.active_positions:
                # Lookup underlying quote
                und_quote = underlying_quotes.get(pos.symbol)
                if not und_quote or not isinstance(und_quote, dict):
                    for k, v in underlying_quotes.items():
                        if pos.symbol in k and isinstance(v, dict):
                            und_quote = v
                            break

                und_ltp = pos.entry_underlying
                if und_quote and isinstance(und_quote, dict):
                    raw_ltp = und_quote.get("last_price")
                    if raw_ltp is None and isinstance(und_quote.get("ohlc"), dict):
                        raw_ltp = und_quote["ohlc"].get("close")
                    if raw_ltp and float(raw_ltp) > 0:
                        und_ltp = float(raw_ltp)

                # Lookup option quote
                opt_quote = (
                    option_quotes.get(pos.option_key)
                    or option_quotes.get(pos.option_key.replace("|", ":"))
                    or option_quotes.get(pos.option_symbol)
                )

                # Use real market exit price (Bid / LTP)
                opt_premium = extract_live_option_price(opt_quote, side="SELL")
                if opt_premium <= 0:
                    # Keep last known valid live premium! Never corrupt active positions with Black-Scholes!
                    opt_premium = pos.current_premium if pos.current_premium > 0 else pos.entry_premium

                pos.update_mtm(
                    underlying_price=und_ltp,
                    option_premium=opt_premium,
                    now=now,
                    target_initial=sim.target_pct,
                    target_decay=sim.target_decay_pct,
                    target_decay_mins=sim.target_decay_mins,
                    red_stagnant_mins=sim.red_stagnant_mins,
                    breakeven_target=sim.breakeven_target_pct,
                    breakeven_shift_mins=sim.breakeven_shift_mins,
                    sl_upping_pct=sim.sl_upping_pct,
                    intermediate_trail_trigger_pct=sim.intermediate_trail_trigger_pct,
                    intermediate_trail_floor_pct=sim.intermediate_trail_floor_pct,
                    trailing_step_pct=sim.trailing_step_pct,
                    target_runner_seconds=sim.target_runner_seconds,
                )

                # Real-time console notifications when Runner or Trailing Floor activates
                if pos.target_runner_active and not getattr(pos, "_notified_target_runner", False):
                    pos._notified_target_runner = True
                    print(
                        f"\n{Colors.BG_YELLOW}{Colors.BLACK}{Colors.BOLD} 🏃 [{s_type} SIM] TARGET REACHED: {pos.symbol} ({pos.option_symbol}) hit +{pos.current_target_pct:.1f}% (₹{pos.current_premium:.2f})! "
                        f"RUNNER ACTIVE: Letting run for {sim.target_runner_seconds:.0f}s with rolling floor at +{pos.target_runner_floor_pct:.1f}% (₹{pos.target_runner_floor_premium:.2f}) {Colors.RESET}"
                    )
                elif pos.trailing_floor_active and not getattr(pos, "_notified_trailing_floor", False):
                    pos._notified_trailing_floor = True
                    print(
                        f"\n{Colors.BG_BLUE}{Colors.BLACK}{Colors.BOLD} 🛡️ [{s_type} SIM] PROFIT FLOOR ACTIVE: {pos.symbol} ({pos.option_symbol}) crossed +{pos.peak_pnl_pct:.1f}%! "
                        f"Floor locked at +{pos.trailing_floor_pct:.1f}% (₹{pos.trailing_floor_premium:.2f}) {Colors.RESET}"
                    )

                # Evaluate Dynamic Scalp Exits:
                # 1. Stagnant Red Breakeven Exit (>= 30m in red -> exit on breakeven target bounce)
                # 2. Target Runner Active (Target hit -> 30s runner window with rolling floor, e.g. 7% -> 6%)
                # 3. Intermediate Trailing Floor (Crossed 3.5% -> base floor 2.5%, e.g. 4.5% peak -> 3.5% floor)
                # 4. Standard Target Exit (if runner seconds <= 0)
                # 5. Stop Loss (Option Premium Drop >= current dynamic tightened R-stop)
                # 6. Stop Loss (Underlying Invalidation / Dynamic Tightened Adverse Move)
                # 7. EOD Square-Off at 15:20 IST
                should_exit = False
                exit_reason = ""

                gross_gain = (opt_premium - pos.entry_premium) * pos.quantity

                # 1. Stagnant Red Breakeven Exit:
                if pos.red_stagnant_since is not None and (gross_gain >= pos.current_target_gross_gain or opt_premium >= pos.current_target_premium):
                    should_exit = True
                    exit_reason = f"TARGET (Breakeven Scalp +{pos.current_target_pct:.1f}% + ₹{pos.brokerage:.0f} Brokerage - Red Stagnant >= 30m)"

                # 2. Target Runner Mode Active:
                elif pos.target_runner_active:
                    # Reversal Floor Breach: dropped below rolling floor
                    if pos.mtm_pnl_pct < pos.target_runner_floor_pct or opt_premium < pos.target_runner_floor_premium:
                        should_exit = True
                        exit_reason = f"TARGET (Runner Reversal Floor +{pos.target_runner_floor_pct:.1f}% hit from Peak +{pos.peak_pnl_pct:.1f}%)"
                    # Runner 30-Second Window Elapsed:
                    elif pos.target_runner_elapsed_sec >= sim.target_runner_seconds:
                        should_exit = True
                        exit_reason = f"TARGET (Runner Window +{pos.mtm_pnl_pct:.1f}% Locked after {sim.target_runner_seconds:.0f}s)"

                # 3. Intermediate Trailing Profit Floor:
                elif pos.trailing_floor_active:
                    # Reversal Floor Breach: dropped to or below rolling floor
                    if pos.mtm_pnl_pct <= pos.trailing_floor_pct or opt_premium <= pos.trailing_floor_premium:
                        should_exit = True
                        exit_reason = f"PROFIT_TRAIL_FLOOR (+{pos.trailing_floor_pct:.1f}% Floor Reversal from Peak +{pos.peak_pnl_pct:.1f}%)"

                # 4. Standard Target Exit (fallback if runner seconds <= 0 OR disable_profit_floors is True):
                elif (gross_gain >= pos.current_target_gross_gain or opt_premium >= pos.current_target_premium) and (sim.target_runner_seconds <= 0 or getattr(pos, "disable_profit_floors", False)):
                    should_exit = True
                    if getattr(pos, "enable_fixed_target", False):
                        exit_reason = f"TARGET (Fixed/Pct Scalp Target ₹{pos.current_target_premium:.2f} [Net ₹{pos.mtm_pnl:,.0f} / ₹{pos.fixed_target_gain:,.0f}])"
                    elif pos.holding_duration_mins >= 1.0:
                        exit_reason = f"TARGET (Rolling +{pos.current_target_pct:.1f}% + ₹{pos.brokerage:.0f} Brokerage [{pos.holding_duration_mins:.0f}m])"
                    else:
                        exit_reason = f"TARGET (+{pos.current_target_pct:.1f}% Premium + ₹{pos.brokerage:.0f} Brokerage)"

                # 5. Stop Loss (Option Premium Drop >= current dynamic tightened R-stop OR trailing SL hit)
                elif opt_premium <= pos.current_stop_loss_premium or (pos.entry_premium - opt_premium) * pos.quantity >= (pos.current_r_stop * pos.capital_invested):
                    should_exit = True
                    if getattr(pos, "trailing_sl_mode", False) and pos.peak_premium > pos.entry_premium:
                        exit_reason = f"TRAILING_STOP_LOSS (Peak ₹{pos.peak_premium:.2f} -> SL ₹{pos.current_stop_loss_premium:.2f} hit)"
                    elif pos.red_stagnant_since is not None:
                        exit_reason = f"STOP_LOSS (-{pos.current_r_stop * 100:.1f}% Option Drop - Upped {sim.sl_upping_pct * 100:.0f}% for Red Stagnation)"
                    else:
                        exit_reason = f"STOP_LOSS (-{pos.current_r_stop * 100:.1f}% Option Premium Drop)"

                # 6. Stop Loss (Underlying Invalidation / Dynamic Tightened Adverse Move)
                elif pos.direction == "BULL" and und_ltp <= pos.current_stop_loss_underlying:
                    should_exit = True
                    exit_reason = f"STOP_LOSS (Underlying Invalidation @ ₹{und_ltp:.2f} <= ₹{pos.current_stop_loss_underlying:.2f})"
                elif pos.direction == "BEAR" and und_ltp >= pos.current_stop_loss_underlying:
                    should_exit = True
                    exit_reason = f"STOP_LOSS (Underlying Invalidation @ ₹{und_ltp:.2f} >= ₹{pos.current_stop_loss_underlying:.2f})"

                # 7. Stagnation Timeout Exit (Unresolved after max_holding_mins, default 50m)
                elif pos.holding_duration_mins >= getattr(sim, "max_holding_mins", 50.0):
                    should_exit = True
                    exit_reason = f"STAGNATION_TIMEOUT (Unresolved after {pos.holding_duration_mins:.0f}m - Capital Recycled)"

                # 8. EOD Square-Off at 15:20 IST
                elif is_eod:
                    should_exit = True
                    exit_reason = "EOD_SQUAREOFF (15:20 IST Close)"

                if should_exit:
                    pos.close(
                        exit_time=time_str,
                        underlying_price=und_ltp,
                        exit_premium=opt_premium,
                        reason=exit_reason,
                    )
                    sim.available_capital += pos.capital_invested + pos.realized_pnl
                    sim.closed_positions.append(pos)
                    self._log_trade_to_csv(pos)

                    pnl_color = Colors.GREEN if pos.realized_pnl >= 0 else Colors.RED
                    print(
                        f"\n{Colors.BG_CYAN}{Colors.BLACK}{Colors.BOLD} 🚪 [{s_type} SIM] POSITION CLOSED: {pos.trade_id} ({pos.option_symbol}) | "
                        f"Net P&L: {pnl_color}₹{pos.realized_pnl:+,.2f} ({pos.realized_pnl_pct:+.1f}%){Colors.RESET} (Gross: ₹{pos.gross_pnl:+,.2f}, Brkr: ₹{pos.brokerage:.0f}) | Reason: {exit_reason}"
                    )
                else:
                    remaining_positions.append(pos)

            sim.active_positions = remaining_positions

        # Update tracker state for symbols that have no active positions across ANY sim
        all_active_syms = {p.symbol for p in self.active_positions}
        for sym, tracker in self.trackers.items():
            if tracker.state == "IN_TRADE" and sym not in all_active_syms:
                tracker.exit_trade(cooldown_seconds=300.0)

    # -----------------------------------------------------------------------
    # Performance Metrics & Reporting Engine
    # -----------------------------------------------------------------------

    def get_metrics(self) -> Dict[str, Any]:
        """Compute aggregate performance and individual strike simulation statistics."""
        sim_metrics = {s_type: sim.get_metrics() for s_type, sim in self.simulations.items()}
        all_closed = self.closed_positions
        total_trades = len(all_closed)
        wins = [p for p in all_closed if p.realized_pnl > 0]
        losses = [p for p in all_closed if p.realized_pnl < 0]
        ties = [p for p in all_closed if p.realized_pnl == 0]

        win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
        total_realized_pnl = sum(p.realized_pnl for p in all_closed)
        total_mtm_pnl = sum(p.mtm_pnl for p in self.active_positions)
        net_pnl = total_realized_pnl + total_mtm_pnl

        gross_profit = sum(p.realized_pnl for p in wins)
        gross_loss = abs(sum(p.realized_pnl for p in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        # Directional breakdown (Bull CE vs Bear PE)
        def _calc_dir_stats(dir_name: str) -> Dict[str, Any]:
            d_trades = [p for p in all_closed if p.direction == dir_name]
            d_wins = [p for p in d_trades if p.realized_pnl > 0]
            d_pnl = sum(p.realized_pnl for p in d_trades)
            d_wr = (len(d_wins) / len(d_trades) * 100) if d_trades else 0.0
            return {
                "trades": len(d_trades),
                "wins": len(d_wins),
                "win_rate": round(d_wr, 1),
                "net_pnl": round(d_pnl, 2),
            }

        alloc_pct, tier_name, max_trade_budget = self.get_capital_tier_allocation()

        return {
            "starting_capital": self.starting_capital,
            "simulations": sim_metrics,
            "strike_comparison": sim_metrics,
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "ties": len(ties),
            "win_rate": round(win_rate, 1),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "total_mtm_pnl": round(total_mtm_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "active_positions_count": len(self.active_positions),
            "capital_tier_name": tier_name,
            "capital_tier_alloc_pct": round(alloc_pct * 100, 1),
            "max_trade_budget": max_trade_budget,
            "direction_comparison": {
                "BULL": _calc_dir_stats("BULL"),
                "BEAR": _calc_dir_stats("BEAR"),
            },
        }

    # -----------------------------------------------------------------------
    # Terminal Dashboard Renderer
    # -----------------------------------------------------------------------

    def render_dashboard(self, show_positions: Optional[bool] = None) -> str:
        """Renders an ANSI terminal dashboard displaying capital tiers, active positions, and performance."""
        metrics = self.get_metrics()
        lines: List[str] = []
        display_positions = self.show_positions if show_positions is None else show_positions

        def _pad(s: str, width: int, align: str = "right") -> str:
            if align == "left":
                return s.ljust(width)[:width]
            elif align == "center":
                return s.center(width)[:width]
            return s.rjust(width)[:width]

        def _c_pnl(val: float, is_pct: bool = False, width: int = 0) -> str:
            sign = "+" if val > 0 else ""
            fmt = f"{sign}{val:.1f}%" if is_pct else f"{sign}₹{val:,.2f}"
            padded = _pad(fmt, width, "right") if width > 0 else fmt
            if val > 0:
                return f"{Colors.BOLD}{Colors.GREEN}{padded}{Colors.RESET}"
            elif val < 0:
                return f"{Colors.BOLD}{Colors.RED}{padded}{Colors.RESET}"
            else:
                return f"{Colors.WHITE}{padded}{Colors.RESET}"

        # Header Banner
        lines.append(f"{Colors.BOLD}{Colors.BG_CYAN}{Colors.BLACK}  🏁 BIDIRECTIONAL FLAG OPTION FORWARD-TESTER (BULL CE & BEAR PE)  {Colors.RESET}")
        
        limit_desc = (
            f"{Colors.GREEN}UNLIMITED{Colors.RESET} (Zero artificial trade caps — bounded solely by capital & tiered gating)"
            if self.max_concurrent_setups is None
            else f"{self.max_concurrent_setups} Max Setups"
        )
        sim_names = " / ".join(self.simulations.keys())
        lines.append(
            f"Mode: {Colors.BOLD}{len(self.simulations)} INDEPENDENT SIMULATIONS ({sim_names}){Colors.RESET} | "
            f"Trade Concurrency: {limit_desc}"
        )
        lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")

        # Independent Strike Simulation Status Cards (One line per strike sim)
        sim_order = [
            ("ATM", Colors.YELLOW),
            ("ITM", Colors.MAGENTA),
            ("OTM", Colors.CYAN),
            ("LOW_PREM", Colors.GREEN),
            ("TRAIL_SL", Colors.BLUE),
            ("FIXED_TGT", Colors.WHITE),
            ("COMBO", Colors.BOLD),
        ]
        for s_type, col in sim_order:
            if s_type not in metrics["simulations"]:
                continue
            sm = metrics["simulations"][s_type]
            pnl_c = _c_pnl(sm["net_pnl"])
            roi_c = _c_pnl(sm["portfolio_roi_pct"], is_pct=True)
            wr_str = f"{sm['win_rate']:.1f}% ({sm['wins']}W / {sm['losses']}L)"
            brkr_str = f"₹{sm.get('total_brokerage', 0.0):,.0f}"
            lines.append(
                f"{col}[{s_type} SIM]{Colors.RESET} "
                f"Capital: {Colors.BOLD}₹{sm['starting_capital']:,.0f}{Colors.RESET} | "
                f"Avail: {Colors.BOLD}₹{sm['available_capital']:,.0f}{Colors.RESET} | "
                f"Net P&L: {pnl_c} ({roi_c}) | "
                f"Brkr: {Colors.YELLOW}{brkr_str}{Colors.RESET} | "
                f"WR: {Colors.CYAN}{wr_str}{Colors.RESET} | "
                f"Active: {sm['active_positions_count']} | "
                f"Max Lots: {sm.get('max_lots', 1)} | "
                f"Tier: {Colors.YELLOW}{sm['capital_tier_name']}{Colors.RESET} (Budget: ₹{sm['max_trade_budget']:,.0f})"
            )

        lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")

        sim_colors = {
            "ATM": Colors.YELLOW,
            "ITM": Colors.MAGENTA,
            "OTM": Colors.CYAN,
            "LOW_PREM": Colors.GREEN,
            "TRAIL_SL": Colors.BLUE,
            "FIXED_TGT": Colors.WHITE,
            "COMBO": Colors.BOLD,
        }
        sim_abbr = {
            "ATM": "ATM",
            "ITM": "ITM",
            "OTM": "OTM",
            "LOW_PREM": "LPRM",
            "TRAIL_SL": "TRSL",
            "FIXED_TGT": "FXTG",
            "COMBO": "CMBO",
        }

        # Active Positions Table / Compact View (Toggleable via show_positions)
        active_pos = self.active_positions
        if display_positions:
            if active_pos:
                lines.append(f"{Colors.BOLD}{Colors.CYAN}ACTIVE POSITIONS ({len(active_pos)} Open Across {len(self.simulations)} Independent Simulations):{Colors.RESET}")
                # Group Headers
                lines.append(
                    f"{Colors.GRAY}│ SIM  │ SYMBOL     │ DIR  │ CONTRACT            │ QTY │            OPTION PREMIUM & SCALP TARGETS (₹)             │          UNDERLYING STOCK SPOT (₹)          │{Colors.RESET}"
                )
                lines.append(
                    f"{Colors.GRAY}│      │            │      │                     │     │  ENTRY     LIVE    TARGET(ROLL)  SL(ROLL)       NET MTM   │  ENTRY SPOT    LIVE SPOT      STOP LOSS     │{Colors.RESET}"
                )
                lines.append(f"{Colors.GRAY}├──────┼────────────┼──────┼─────────────────────┼─────┼───────────────────────────────────────────────────┼─────────────────────────────────────────────┤{Colors.RESET}")

                for pos in active_pos:
                    s_color = sim_colors.get(pos.strike_type, Colors.WHITE)
                    col_sim = f"{s_color}{_pad(sim_abbr.get(pos.strike_type, pos.strike_type[:4]), 4, 'center')}{Colors.RESET}"
                    col_sym = _pad(pos.symbol[:10], 10, 'left')
                    dir_str = "🐂BULL" if pos.direction == "BULL" else "🐻BEAR"
                    dir_color = Colors.GREEN if pos.direction == "BULL" else Colors.RED
                    col_dir = f"{dir_color}{dir_str}{Colors.RESET}"
                    col_cnt = _pad(pos.option_symbol[:19], 19, 'left')
                    col_qty = _pad(str(pos.quantity), 3, 'right')

                    # Option Premium Columns
                    col_ent_prm = _pad(f"₹{pos.entry_premium:.2f}", 8, 'right')
                    col_liv_prm = _pad(f"₹{pos.current_premium:.2f}", 8, 'right')
                    tgt_val = pos.current_target_premium if pos.current_target_premium > 0 else pos.target_premium
                    sl_val = pos.current_stop_loss_premium if pos.current_stop_loss_premium > 0 else pos.stop_loss_premium
                    if pos.target_runner_active:
                        col_tgt_prm = _pad(f"🏃₹{pos.target_runner_floor_premium:.1f}", 12, 'right')
                    elif pos.trailing_floor_active:
                        col_tgt_prm = _pad(f"🛡️₹{pos.trailing_floor_premium:.1f}", 12, 'right')
                    else:
                        col_tgt_prm = _pad(f"₹{tgt_val:.2f}", 12, 'right')
                    col_sl_prm = _pad(f"₹{sl_val:.2f}", 10, 'right')
                    sign = "+" if pos.mtm_pnl > 0 else ""
                    raw_pnl = f"{sign}₹{pos.mtm_pnl:,.1f} ({sign}{pos.mtm_pnl_pct:.1f}%)"
                    pnl_color = Colors.GREEN if pos.mtm_pnl > 0 else (Colors.RED if pos.mtm_pnl < 0 else Colors.WHITE)
                    col_mtm = f"{pnl_color}{_pad(raw_pnl, 16, 'right')}{Colors.RESET}"

                    # Underlying Stock Spot & Target Columns
                    col_ent_spt = _pad(f"₹{pos.entry_underlying:,.1f}", 11, 'right')
                    col_liv_spt = _pad(f"₹{pos.current_underlying:,.1f}", 11, 'right')
                    sl_und_val = pos.current_stop_loss_underlying if pos.current_stop_loss_underlying > 0 else pos.stop_loss_underlying
                    raw_sl = f"₹{sl_und_val:,.1f}"
                    col_sl = f"{Colors.RED}{_pad(raw_sl, 13, 'right')}{Colors.RESET}"

                    lines.append(
                        f"│ {col_sim} │ {col_sym} │ {col_dir} │ {col_cnt} │ {col_qty} │ {col_ent_prm} {col_liv_prm} {col_tgt_prm} {col_sl_prm} {col_mtm} │ {col_ent_spt} {col_liv_spt} {col_sl} │"
                    )
                lines.append(f"{Colors.GRAY}└──────┴────────────┴──────┴─────────────────────┴─────┴───────────────────────────────────────────────────┴─────────────────────────────────────────────┘{Colors.RESET}")
            else:
                lines.append(f"{Colors.GRAY}Active Positions: None (Awaiting Bull Flag breakout or Bear Flag breakdown triggers)...{Colors.RESET}")
        else:
            # Compact view (default when --show-positions is not passed)
            if active_pos:
                total_mtm = sum(p.mtm_pnl for p in active_pos)
                mtm_color = Colors.GREEN if total_mtm > 0 else (Colors.RED if total_mtm < 0 else Colors.WHITE)
                pos_summary = ", ".join([f"{p.symbol} ({sim_abbr.get(p.strike_type, p.strike_type)} {p.direction[:1]})" for p in active_pos[:5]])
                if len(active_pos) > 5:
                    pos_summary += f", +{len(active_pos)-5} more"
                lines.append(
                    f"{Colors.BOLD}{Colors.CYAN}ACTIVE POSITIONS:{Colors.RESET} "
                    f"{len(active_pos)} Open across {len(self.simulations)} sims | "
                    f"Total MTM: {mtm_color}₹{total_mtm:+,.1f}{Colors.RESET} | "
                    f"[{pos_summary}] {Colors.GRAY}(Use --show-positions to expand table){Colors.RESET}"
                )
            else:
                lines.append(f"{Colors.GRAY}Active Positions: None Open [Use --show-positions to toggle expanded table]{Colors.RESET}")

        lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")

        # Session Executed Trades Journal Table (Shows all trades taken during this session: Open & Closed)
        all_executed_trades = [
            pos for sim in self.simulations.values()
            for pos in (sim.active_positions + sim.closed_positions)
        ]
        all_executed_trades.sort(key=lambda p: (p.entry_time or "", p.trade_id), reverse=True)

        open_cnt = len(self.active_positions)
        closed_cnt = len(self.closed_positions)
        tot_cnt = len(all_executed_trades)

        lines.append(
            f"{Colors.BOLD}{Colors.WHITE}SESSION EXECUTED TRADES JOURNAL ({tot_cnt} Total Executed │ "
            f"{Colors.CYAN}{open_cnt} Active Open{Colors.RESET}{Colors.BOLD}{Colors.WHITE} │ "
            f"{Colors.GREEN}{closed_cnt} Closed{Colors.RESET}{Colors.BOLD}{Colors.WHITE}):{Colors.RESET}"
        )

        if all_executed_trades:
            lines.append(f"{Colors.GRAY}┌──────┬──────────┬──────────┬──────┬─────────────────────┬─────┬───────────┬───────────┬───────────┬──────────┬──────────────────────┬──────────┬────────────────────────────────┐{Colors.RESET}")
            lines.append(f"{Colors.GRAY}│ SIM  │   TIME   │  SYMBOL  │ DIR  │ CONTRACT            │ QTY │ ENTRY PRM │ LIVE/EXIT │ GROSS P&L │ BRKR(₹)  │     NET P&L (₹ / %)  │  STATUS  │ OUTCOME / EXIT REASON          │{Colors.RESET}")
            lines.append(f"{Colors.GRAY}├──────┼──────────┼──────────┼──────┼─────────────────────┼─────┼───────────┼───────────┼───────────┼──────────┼──────────────────────┼──────────┼────────────────────────────────┤{Colors.RESET}")

            for pos in all_executed_trades[:10]:
                is_closed = pos.exit_time is not None or pos in self.closed_positions
                s_color = sim_colors.get(pos.strike_type, Colors.WHITE)
                col_sim = f"{s_color}{_pad(sim_abbr.get(pos.strike_type, pos.strike_type[:4]), 4, 'center')}{Colors.RESET}"
                t_str = pos.entry_time or ""
                col_time = _pad(t_str, 8, 'center')
                col_sym = _pad(pos.symbol[:8], 8, 'left')
                dir_color = Colors.GREEN if pos.direction == "BULL" else Colors.RED
                col_dir = f"{dir_color}{_pad(pos.direction, 4, 'center')}{Colors.RESET}"
                col_cnt = _pad(pos.option_symbol[:19], 19, 'left')
                col_qty = _pad(str(pos.quantity), 3, 'right')

                col_ent_prm = _pad(f"₹{pos.entry_premium:.2f}", 9, 'right')
                cur_prm = pos.exit_premium if is_closed else pos.current_premium
                col_cur_prm = _pad(f"₹{cur_prm:.2f}", 9, 'right')

                gross_val = pos.gross_pnl if is_closed else pos.gross_mtm_pnl
                brkr_val = pos.brokerage
                net_val = pos.realized_pnl if is_closed else pos.mtm_pnl
                pct_val = pos.realized_pnl_pct if is_closed else pos.mtm_pnl_pct

                gross_sign = "+" if gross_val > 0 else ""
                col_gross = _pad(f"{gross_sign}₹{gross_val:,.1f}", 9, 'right')
                col_brkr = _pad(f"₹{brkr_val:.0f}", 8, 'right')

                sign = "+" if net_val > 0 else ""
                raw_net = f"{sign}₹{net_val:,.1f} ({sign}{pct_val:.1f}%)"
                pnl_color = Colors.GREEN if net_val > 0 else (Colors.RED if net_val < 0 else Colors.WHITE)
                col_net = f"{pnl_color}{_pad(raw_net, 20, 'right')}{Colors.RESET}"

                if is_closed:
                    col_status = f"{Colors.GREEN if net_val >= 0 else Colors.RED}{_pad('CLOSED', 8, 'center')}{Colors.RESET}"
                    reason = pos.exit_reason or "Closed"
                else:
                    col_status = f"{Colors.CYAN}{_pad('OPEN', 8, 'center')}{Colors.RESET}"
                    reason = f"TGT:₹{pos.target_premium:.1f} SL:₹{pos.stop_loss_premium:.1f}"

                col_reason = _pad(reason[:30], 30, 'left')

                lines.append(
                    f"│ {col_sim} │ {col_time} │ {col_sym} │ {col_dir} │ {col_cnt} │ {col_qty} │ {col_ent_prm} │ {col_cur_prm} │ {col_gross} │ {col_brkr} │ {col_net} │ {col_status} │ {col_reason} │"
                )

            lines.append(f"{Colors.GRAY}└──────┴──────────┴──────────┴──────┴─────────────────────┴─────┴───────────┴───────────┴───────────┴──────────┴──────────────────────┴──────────┴────────────────────────────────┘{Colors.RESET}")
            if len(all_executed_trades) > 10:
                lines.append(f"  {Colors.GRAY}(Showing latest 10 of {len(all_executed_trades)} session trades. Full log saved to: {self.output_csv}){Colors.RESET}")
        else:
            lines.append(f"{Colors.GRAY}Session Executed Trades: None yet (Awaiting flag breakout/breakdown signals with valid option quotes)...{Colors.RESET}")

        lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")

        # Strike Performance Matrix (Side-by-side comparative table)
        lines.append(f"{Colors.BOLD}{Colors.WHITE}STRIKE & STRATEGY SIMULATION PERFORMANCE MATRIX ({len(self.simulations)} Isolated ₹{self.starting_capital:,.0f} Portfolios):{Colors.RESET}")
        lines.append(
            f"{Colors.GRAY}{'STRIKE SIM':<18} {'CAPITAL':>10} {'AVAIL CASH':>12} {'ACTIVE MTM':>13} {'GROSS REAL':>13} {'BRKR PAID':>11} "
            f"{'NET REALIZED':>15} {'NET P&L':>14} {'WIN RATE':>10} {'TRADES':>10} {'NET ROI':>12}{Colors.RESET}"
        )
        lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")

        matrix_sim_order = [
            ("ATM", "ATM (Delta ~0.5)", Colors.YELLOW),
            ("ITM", "ITM (Delta ~0.7)", Colors.MAGENTA),
            ("OTM", "OTM (Delta ~0.3)", Colors.CYAN),
            ("LOW_PREM", "LOW_PREM (Lowest)", Colors.GREEN),
            ("TRAIL_SL", "TRAIL_SL (Trail)", Colors.BLUE),
            ("FIXED_TGT", "FIXED_TGT (₹750)", Colors.WHITE),
            ("COMBO", "COMBO (3-in-1)", Colors.BOLD),
        ]
        # Include any custom simulation types configured in metrics
        configured_types = {s[0] for s in matrix_sim_order}
        for extra_type in metrics["simulations"]:
            if extra_type not in configured_types:
                matrix_sim_order.append((extra_type, f"{extra_type[:18]}", Colors.WHITE))

        for s_type, label, col in matrix_sim_order:
            if s_type not in metrics["simulations"]:
                continue
            st = metrics["simulations"][s_type]
            wr_str = f"{st['win_rate']:.1f}%" if st['total_trades'] > 0 else "0.0%"
            trades_str = f"{st['total_trades']} ({st['wins']}W/{st['losses']}L)"
            lines.append(
                f"{col}{_pad(label, 18, 'left')}{Colors.RESET} "
                f"₹{st['starting_capital']:>9,.0f} "
                f"₹{st['available_capital']:>11,.0f} "
                f"{_c_pnl(st['mtm_pnl'], width=13)} "
                f"{_c_pnl(st.get('gross_realized_pnl', 0.0), width=13)} "
                f"₹{st.get('total_brokerage', 0.0):>9,.0f} "
                f"{_c_pnl(st['realized_pnl'], width=15)} "
                f"{_c_pnl(st['net_pnl'], width=14)} "
                f"{_pad(wr_str, 10, 'right')} "
                f"{_pad(trades_str, 10, 'right')} "
                f"{_c_pnl(st['portfolio_roi_pct'], is_pct=True, width=12)}"
            )

        # Directional Performance Summary
        dc = metrics.get("direction_comparison", {})
        if dc:
            lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")
            b_wr = dc.get('BULL', {}).get('win_rate', 0.0)
            b_pnl = _c_pnl(dc.get('BULL', {}).get('net_pnl', 0.0))
            r_wr = dc.get('BEAR', {}).get('win_rate', 0.0)
            r_pnl = _c_pnl(dc.get('BEAR', {}).get('net_pnl', 0.0))
            lines.append(
                f"{Colors.BOLD}DIRECTIONAL PERFORMANCE:{Colors.RESET} "
                f"Bull Flags (Call CE): {Colors.GREEN}{b_wr:.1f}% WR{Colors.RESET} | Net P&L: {b_pnl}   │   "
                f"Bear Flags (Put PE): {Colors.RED}{r_wr:.1f}% WR{Colors.RESET} | Net P&L: {r_pnl}"
            )

        # Recent Gated Trades (Skipped Setups)
        all_gated = self.gated_trades_log
        if all_gated:
            lines.append(f"{Colors.GRAY}{'─' * 148}{Colors.RESET}")
            lines.append(f"{Colors.YELLOW}CAPITAL GATING DECISIONS (Skipped Setups):{Colors.RESET}")
            for g in all_gated[-5:]:
                lines.append(f"  {Colors.GRAY}{g.get('time', '')}{Colors.RESET} | {g.get('symbol', '')} ({g.get('direction', '')}) -> {Colors.RED}{g.get('reason', '')}{Colors.RESET}")

        return "\n".join(lines)

    def generate_final_report(self) -> str:
        """Generate a complete end-of-session markdown report comparing independent strike simulations."""
        metrics = self.get_metrics()
        sims = metrics["simulations"]
        dc = metrics["direction_comparison"]

        strike_sections = []
        labels = {
            "ATM": "1. ATM Options (Delta ~0.5 Baseline)",
            "ITM": "2. ITM Options (Delta ~0.7 Deeper In-The-Money)",
            "OTM": "3. OTM Options (Delta ~0.3 Cheaper Out-of-The-Money)",
            "LOW_PREM": "4. LOW_PREM (Lowest Premium Liquid Option Bias)",
            "TRAIL_SL": "5. TRAIL_SL (Tick-by-Tick Ratchet Trailing Stop Loss)",
            "FIXED_TGT": "6. FIXED_TGT (Dual Target min(pct, ₹750) No Floor)",
            "COMBO": "7. COMBO (Triple Combo: Low Prem + Trail SL + Fixed Tgt)",
        }
        ordered_keys = ["ATM", "ITM", "OTM", "LOW_PREM", "TRAIL_SL", "FIXED_TGT", "COMBO"]
        for k in sims:
            if k not in ordered_keys:
                ordered_keys.append(k)

        idx = 1
        for st_name in ordered_keys:
            if st_name in sims:
                s_data = sims[st_name]
                lbl = labels.get(st_name, f"{idx}. {st_name} Simulation")
                strike_sections.append(
                    f"{lbl}:\n"
                    f"   - Starting Capital:   ₹{s_data['starting_capital']:,.2f}\n"
                    f"   - Ending Capital:     ₹{s_data['current_portfolio_val']:,.2f}\n"
                    f"   - Gross Realized:     ₹{s_data.get('gross_realized_pnl', 0.0):+,.2f}\n"
                    f"   - Total Brokerage:    ₹{s_data.get('total_brokerage', 0.0):,.2f}\n"
                    f"   - Realized Net P&L:   ₹{s_data['realized_pnl']:+,.2f}\n"
                    f"   - Net Portfolio P&L:  ₹{s_data['net_pnl']:+,.2f} ({s_data['portfolio_roi_pct']:+.2f}%)\n"
                    f"   - Trades Executed:    {s_data['total_trades']} ({s_data['wins']} Wins / {s_data['losses']} Losses)\n"
                    f"   - Win Rate:           {s_data['win_rate']:.1f}%\n"
                    f"   - Profit Factor:      {s_data['profit_factor']:.2f}\n"
                    f"   - Avg Trade Return:   {s_data['avg_roi_pct']:+.1f}%"
                )
                idx += 1
        strikes_str = "\n\n".join(strike_sections) if strike_sections else "No active simulations."

        report = f"""
# =====================================================================
#   BIDIRECTIONAL FLAG OPTION FORWARD-TEST REPORT (AGGRESSIVE SCALPER)
# =====================================================================
Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Simulation Mode: {self.strike_mode.upper()} Strike Portfolio(s)
Starting Capital per Simulation: ₹{self.starting_capital:,.2f}
Max Lots Capped per Position: {self.max_lots}
Brokerage: ₹{self.brokerage_per_order:.0f}/order (₹{self.brokerage_per_order * 2:.0f} round-trip)
Target Rule: +{self.target_pct:.0f}% of Option Premium + ₹{self.brokerage_per_order * 2:.0f} Brokerage
Stop Loss Rule: -{self.r_stop * 100:.0f}% Option Premium Drop / 0.15% Underlying Invalidation

--- STRIKE & STRATEGY SIMULATION PERFORMANCE MATRIX ---
{strikes_str}

--- DIRECTIONAL PERFORMANCE (AGGREGATE) ---
- Bull Flags (Call CE): {dc['BULL']['trades']} Trades | {dc['BULL']['win_rate']:.1f}% WR | Net P&L: ₹{dc['BULL']['net_pnl']:+,.2f}
- Bear Flags (Put PE):  {dc['BEAR']['trades']} Trades | {dc['BEAR']['win_rate']:.1f}% WR | Net P&L: ₹{dc['BEAR']['net_pnl']:+,.2f}

--- CAPITAL GATING & LOT SIZING RULES ---
- Max Lots Capped: {self.max_lots} Lot(s) per trade setup
- Round-Trip Brokerage: ₹{self.brokerage_per_order * 2:.0f} per completed trade
- Tier 1 (50-100% Capital): Max 50% allocation per setup
- Tier 2 (25-50% Capital):  Max 75% allocation per setup
- Tier 3 (< 25% Capital):   100% allocation allowed

Trade Journal recorded to: {os.path.abspath(self.output_csv)}
# =====================================================================
"""
        return report


# Backward compatibility alias
BearFlagPaperPortfolio = FlagPaperPortfolio
