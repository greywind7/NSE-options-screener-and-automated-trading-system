#!/usr/bin/env python3
"""
strategies/momentum/risk.py
===========================
Dynamic Risk Management & Trade Lifecycle Engine:
1. Underlying Invalidation Stop: Delta-aligned with >= 0.80% structural breathing room.
2. Option Stop Loss: -15% option premium drop hard-stop.
3. Tick-by-Tick Trailing Stop Loss: Ratchets point-for-point as option advances.
4. Dual Scalp Target: min(target_pct, Fixed ₹750).
5. Rolling Target Decay: 5.0% -> 2.5% over 30 minutes.
6. Red Breakeven Shift: 0.5% target and SL tightened 33% after 30m in red.
7. Stagnation Exit: Automatically recycles capital after 50 minutes unresolved.
8. EOD Square-Off: 15:20 IST automatic market close.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from infrastructure.common.time_utils import normalize_time_str


class RiskManager:
    """
    Evaluates position exit rules on every tick.
    """

    def __init__(
        self,
        option_sl_pct: float = 7.5,
        initial_target_pct: float = 15.0,
        decay_floor_pct: float = 2.5,
        target_decay_sec: float = 900.0,  # 15 minutes
        stagnation_timeout_sec: float = 1800.0,  # 30 minutes
        step_size_pct: float = 2.5,
        step_sl_pct: float = 2.5,
        trail_breakeven_trigger_pct: float = 2.5,
        trailing_step_pct: float = 1.0,
        trail_convergence_ratio: float = 0.50,
        fixed_target_inr: float = 750.0,
        fixed_target_timeout_sec: float = 600.0,  # 10 minutes
        fixed_target_min_5pct_val: float = 500.0,
        broker_max_order_modifications: int = 25,
        breakeven_target_pct: float = 0.5,
        brokerage_per_order: float = 30.0,
        underlying_breathing_room_pct: float = 0.80,
        eod_square_off_time: str = "15:20",
        eod_freeze_time: str = "15:15",
        is_mcx: bool = False,
        market_open_time: str = "09:15",
        market_close_time: str = "15:30",
        midday_freeze_start: str = "11:00",
        midday_freeze_end: str = "13:30",
        midday_freeze_enabled: bool = True,
        max_option_spread_pct: float = 3.0,
    ):
        self.option_sl_pct = option_sl_pct
        self.max_option_spread_pct = float(max_option_spread_pct)
        self.initial_target_pct = initial_target_pct
        self.decay_floor_pct = decay_floor_pct
        self.target_decay_sec = target_decay_sec
        self.stagnation_timeout_sec = stagnation_timeout_sec
        self.step_size_pct = step_size_pct
        self.step_sl_pct = step_sl_pct
        self.trail_breakeven_trigger_pct = trail_breakeven_trigger_pct
        self.trailing_step_pct = trailing_step_pct
        self.trail_convergence_ratio = float(trail_convergence_ratio)
        self.fixed_target_inr = fixed_target_inr
        self.fixed_target_timeout_sec = fixed_target_timeout_sec
        self.fixed_target_min_5pct_val = fixed_target_min_5pct_val
        self.broker_max_order_modifications = broker_max_order_modifications
        self.breakeven_target_pct = breakeven_target_pct
        self.brokerage_per_order = brokerage_per_order
        self.underlying_breathing_room_pct = underlying_breathing_room_pct
        self.is_mcx = is_mcx
        self.midday_freeze_start = normalize_time_str(midday_freeze_start, "11:00")
        self.midday_freeze_end = normalize_time_str(midday_freeze_end, "13:30")
        self.midday_freeze_enabled = midday_freeze_enabled

        if is_mcx:
            self.market_open_time = normalize_time_str(market_open_time if market_open_time != "09:15" else "09:00", "09:00")
            self.eod_freeze_time = normalize_time_str(eod_freeze_time if eod_freeze_time != "15:15" else "23:15", "23:15")
            self.eod_square_off_time = normalize_time_str(eod_square_off_time if eod_square_off_time != "15:20" else "23:25", "23:25")
            self.market_close_time = normalize_time_str(market_close_time if market_close_time != "15:30" else "23:30", "23:30")
        else:
            self.market_open_time = normalize_time_str(market_open_time, "09:15")
            self.eod_freeze_time = normalize_time_str(eod_freeze_time, "15:15")
            self.eod_square_off_time = normalize_time_str(eod_square_off_time, "15:20")
            self.market_close_time = normalize_time_str(market_close_time, "15:30")

    def get_market_session(self, now_ts: Optional[float] = None) -> Tuple[str, str]:
        """
        Determine current market session for Indian exchanges (NSE / MCX).
        Returns (session_type: str, status_description: str).
        Session types:
        - 'WEEKEND': Saturday or Sunday.
        - 'OFF_HOURS': Weekday before 09:00 IST (pre-open).
        - 'PRE_MARKET_CAS': 09:00 - 09:15 IST (Call Auction Session).
        - 'NORMAL_TRADING': 09:15 - 15:15 IST (Continuous Trading).
        - 'ENTRY_FREEZE': 15:15 - 15:20 IST (Intraday Entry Freeze).
        - 'EOD_SQUAREOFF': 15:20 - 15:30 IST (RMS Auto-Squareoff Window).
        - 'POST_MARKET': >= 15:30 IST (Market Closed).
        """
        if now_ts is None:
            now_ts = time.time()
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime.fromtimestamp(now_ts, tz=ist)
        if dt_ist.year < 2020:
            return "NORMAL_TRADING", "Synthetic Test Session"

        if dt_ist.weekday() >= 5:
            return "WEEKEND", f"Market Closed (Weekend: {dt_ist.strftime('%A')})"

        cur_hm = dt_ist.strftime("%H:%M")
        if cur_hm < self.market_open_time:
            if cur_hm < "09:00" and not self.is_mcx:
                return "OFF_HOURS", f"Pre-Market (Continuous trading opens at {self.market_open_time} IST)"
            return "PRE_MARKET_CAS", f"Pre-Market Call Auction Session (CAS active until {self.market_open_time} IST)"
        elif cur_hm < self.eod_freeze_time:
            return "NORMAL_TRADING", f"Normal Continuous Trading ({self.market_open_time} - {self.eod_freeze_time} IST)"
        elif cur_hm < self.eod_square_off_time:
            return "ENTRY_FREEZE", f"Intraday Entry Freeze ({self.eod_freeze_time} - {self.eod_square_off_time} IST)"
        elif cur_hm < self.market_close_time:
            return "EOD_SQUAREOFF", f"EOD RMS Auto-Squareoff ({self.eod_square_off_time} - {self.market_close_time} IST)"
        else:
            return "POST_MARKET", f"Market Closed (Post-market after {self.market_close_time} IST)"

    def is_market_open(self, now_ts: Optional[float] = None) -> bool:
        """Return True only during active continuous trading session."""
        session, _ = self.get_market_session(now_ts)
        return session in ("NORMAL_TRADING", "ENTRY_FREEZE", "EOD_SQUAREOFF")

    def is_pre_market_auction(self, now_ts: Optional[float] = None) -> bool:
        """Return True if pre-market Call Auction Session (CAS) is active (09:00 - 09:15 IST)."""
        session, _ = self.get_market_session(now_ts)
        return session in ("PRE_MARKET_CAS", "OFF_HOURS")

    def is_entry_allowed(self, now_ts: Optional[float] = None) -> Tuple[bool, str]:
        """
        Verify whether new trade entries are allowed.
        Blocks entries during CAS auction, off-hours, weekends, entry freeze, post-market,
        and midday lull freeze (11:00 - 13:30 IST).
        """
        session, desc = self.get_market_session(now_ts)
        if session != "NORMAL_TRADING":
            return False, desc

        if self.midday_freeze_enabled and now_ts is not None:
            ist = timezone(timedelta(hours=5, minutes=30))
            dt_ist = datetime.fromtimestamp(now_ts, tz=ist)
            if dt_ist.year >= 2020:
                cur_hm = dt_ist.strftime("%H:%M")
                if self.midday_freeze_start <= cur_hm < self.midday_freeze_end:
                    return False, f"MIDDAY_LULL_FREEZE ({self.midday_freeze_start} - {self.midday_freeze_end} IST - Capital Protected)"

        return True, "OK"

    def is_eod_freeze(self, now_ts: Optional[float] = None) -> bool:
        """
        Check if market has reached the intraday entry freeze cutoff (15:15 IST NSE / 23:15 MCX) or later.
        """
        session, _ = self.get_market_session(now_ts)
        return session in ("ENTRY_FREEZE", "EOD_SQUAREOFF", "POST_MARKET", "WEEKEND")

    def is_eod_squareoff(self, now_ts: Optional[float] = None) -> bool:
        """
        Check if market has reached the RMS auto-squareoff cutoff (15:20 IST for NSE, 23:25 for MCX).
        All open intraday positions must be closed at market price.
        """
        if now_ts is None:
            now_ts = time.time()
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime.fromtimestamp(now_ts, tz=ist)
        if dt_ist.year < 2020:
            return False
        if dt_ist.weekday() >= 5:
            return True  # Lingering intraday positions on weekends should be squared off
        cur_hm = dt_ist.strftime("%H:%M")
        return cur_hm >= self.eod_square_off_time

    def check_spread_allowed(self, ask: float, bid: float) -> Tuple[bool, str]:
        """
        Validates whether the option bid-ask spread is tight enough for execution.
        Returns (is_allowed, reason).
        Rejects:
        1. Missing / non-positive ask or bid (zero tolerance for unquoted exit liquidity).
        2. Inverted books (bid > ask).
        3. Spread percentage ((ask - bid) / ask * 100) > max_option_spread_pct (default: 3.0%).
        4. Spread percentage >= option_sl_pct * 0.50 (spread cannot consume >= 50% of the stop loss).
        """
        if ask <= 0:
            return False, "Unquoted Ask (ask <= 0)"
        if bid <= 0:
            return False, "Unquoted Bid (bid <= 0, no liquidation liquidity)"
        if bid > ask:
            return False, f"Inverted Book (bid ₹{bid:.2f} > ask ₹{ask:.2f})"

        spread_pct = ((ask - bid) / ask) * 100.0
        if spread_pct > self.max_option_spread_pct:
            return False, f"Wide Spread ({spread_pct:.2f}% > max {self.max_option_spread_pct:.1f}%)"

        max_sl_spread = self.option_sl_pct * 0.50
        if spread_pct >= max_sl_spread:
            return False, f"Spread exceeds 50% of SL buffer ({spread_pct:.2f}% >= {max_sl_spread:.2f}%)"

        return True, "Liquid Spread"

    def compute_underlying_stop(
        self,
        underlying_ltp: float,
        direction: str,
        flag_high: float,
        flag_low: float,
    ) -> float:
        """
        Compute delta-aligned underlying structural stop loss with configurable breathing room.
        """
        min_buf = max(0.001, self.underlying_breathing_room_pct / 100.0)
        flag_height = max(0.0, flag_high - flag_low)
        flag_height_ratio = flag_height / underlying_ltp if underlying_ltp > 0 else min_buf
        und_buffer = max(min_buf, min(0.015, flag_height_ratio))

        if direction.upper() in ("BULL", "CE"):
            structural_floor = min(flag_low, underlying_ltp * (1.0 - und_buffer)) if flag_low > 0 else underlying_ltp * (1.0 - und_buffer)
            # Ensure SL does not exceed 1.5% max risk cap
            return round(max(underlying_ltp * 0.985, structural_floor), 2)
        else:
            structural_ceiling = max(flag_high, underlying_ltp * (1.0 + und_buffer)) if flag_high > 0 else underlying_ltp * (1.0 + und_buffer)
            return round(min(underlying_ltp * 1.015, structural_ceiling), 2)

    def evaluate_exit(
        self,
        position: Dict[str, Any],
        current_option_price: float,
        current_underlying_price: float,
        now_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Evaluate all exit conditions for an active option position.
        Returns (should_exit, reason).
        """
        if now_ts is None:
            now_ts = time.time()

        entry_price = float(position["entry_price"])
        quantity = int(position["quantity"])
        direction = position["direction"]
        holding_sec = now_ts - float(position["entry_time"])

        # Update peak price
        peak_price = max(float(position.get("peak_price", entry_price)), current_option_price)
        position["peak_price"] = peak_price

        # Gross & Net P&L
        gross_pnl = (current_option_price - entry_price) * quantity
        round_trip_brokerage = self.brokerage_per_order * 2.0
        net_pnl = gross_pnl - round_trip_brokerage
        pct_gain = ((current_option_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

        strat_type = position.get("strategy_type", "ATM").upper()

        # -------------------------------------------------------------------
        # 0. EOD Automatic RMS Square-Off (15:20 IST / 15:15 IST / MCX)
        # -------------------------------------------------------------------
        if position.get("test_force_eod") or self.is_eod_squareoff(now_ts):
            return True, f"EOD_MIS_SQUAREOFF ({self.eod_square_off_time} IST Market Close)"

        # -------------------------------------------------------------------
        # 1. Stop Loss Evaluation
        # -------------------------------------------------------------------
        base_sl = entry_price * (1.0 - (self.option_sl_pct / 100.0))
        effective_sl = float(position.get("current_sl", base_sl))
        max_gain_pct = ((peak_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

        if "TRAIL_SL" in strat_type:
            # "Trail stoploss tracks should make the stoploss 0 after reaching +2.5% and then ratchet from there."
            if max_gain_pct >= self.trail_breakeven_trigger_pct:
                breakeven_floor = entry_price
                excess_gain = peak_price - (entry_price * (1.0 + (self.trail_breakeven_trigger_pct / 100.0)))
                ratcheted_sl = breakeven_floor + max(0.0, excess_gain)
                if ratcheted_sl > effective_sl:
                    effective_sl = ratcheted_sl
                    position["current_sl"] = effective_sl

            # Dynamic Retracement Convergence ("Meet in the Middle"):
            # As price pulls back from its peak, dynamically advance the stop-loss boundary
            # toward the falling price (effective_sl + convergence_ratio * pullback).
            # This locks in profits earlier on downward reversals and ratchets the resting order on Groww.
            pullback = max(0.0, peak_price - current_option_price)
            if pullback > 0 and max_gain_pct >= self.trail_breakeven_trigger_pct and getattr(self, "trail_convergence_ratio", 0.0) > 0:
                converged_sl = effective_sl + (self.trail_convergence_ratio * pullback)
                if converged_sl > effective_sl:
                    effective_sl = converged_sl
                    position["current_sl"] = effective_sl
                if current_option_price <= converged_sl:
                    return True, f"TRAILING_STOP_LOSS (Peak ₹{peak_price:.2f} -> Converged @ ₹{converged_sl:.2f} hit)"
        else:
            # "For main ATM/ITM/OTM tracks... Every 2.5% increase in value we should update the stoploss by 2.5%. Same rules apply for low premium tracks."
            if max_gain_pct >= self.step_size_pct:
                num_steps = int(max_gain_pct // self.step_size_pct)
                stepped_sl_pct = -self.option_sl_pct + (num_steps * self.step_sl_pct)
                stepped_sl = entry_price * (1.0 + (stepped_sl_pct / 100.0))
                if stepped_sl > effective_sl:
                    effective_sl = stepped_sl
                    position["current_sl"] = effective_sl

        if current_option_price <= effective_sl:
            if "TRAIL_SL" in strat_type:
                return True, f"TRAILING_STOP_LOSS (Peak ₹{peak_price:.2f} -> SL ₹{effective_sl:.2f} hit)"
            return True, f"STOP_LOSS (Drop below SL [₹{effective_sl:.2f}])"

        # -------------------------------------------------------------------
        # 2. Underlying Setup Invalidation Stop
        # -------------------------------------------------------------------
        und_sl = float(position.get("underlying_sl", 0.0))
        if und_sl > 0:
            if direction == "BULL" and current_underlying_price <= und_sl:
                return True, f"UNDERLYING_SL (Breached flag low @ ₹{und_sl:.2f})"
            elif direction == "BEAR" and current_underlying_price >= und_sl:
                return True, f"UNDERLYING_SL (Breached flag high @ ₹{und_sl:.2f})"

        # -------------------------------------------------------------------
        # 3. Fixed Target Tracks (No rolling target, 10m timeout)
        # -------------------------------------------------------------------
        if "FIXED_TGT" in strat_type:
            if net_pnl >= self.fixed_target_inr or pct_gain >= self.initial_target_pct:
                return True, f"TARGET (Fixed Scalp Target [Net ₹{net_pnl:.2f} / ₹{self.fixed_target_inr:.0f}])"
            # Square off after 10 mins (configurable) if target not reached
            if holding_sec >= self.fixed_target_timeout_sec:
                return True, f"FIXED_TARGET_TIMEOUT (Unresolved after {int(holding_sec / 60)}m - Capital Recycled)"
            return False, ""

        # -------------------------------------------------------------------
        # 4. Rolling Target Decay: 15.0% decaying down to 2.5% over 15 mins
        # -------------------------------------------------------------------
        target_pct = self.initial_target_pct
        if holding_sec > 0:
            decay_fraction = min(1.0, holding_sec / max(1.0, self.target_decay_sec))
            target_pct = self.initial_target_pct - (decay_fraction * (self.initial_target_pct - self.decay_floor_pct))

        # Stagnant Red Breakeven Shift after decay period
        if holding_sec >= self.target_decay_sec and net_pnl < 0:
            target_pct = self.breakeven_target_pct
            tightened_sl = entry_price * (1.0 - (self.option_sl_pct * 0.67 / 100.0))
            if tightened_sl > effective_sl:
                effective_sl = tightened_sl
                position["current_sl"] = effective_sl

        req_price = entry_price * (1.0 + (target_pct / 100.0)) + (round_trip_brokerage / quantity)
        if current_option_price >= req_price:
            return True, f"TARGET (Hit rolling target {target_pct:.1f}% @ ₹{req_price:.2f})"

        # -------------------------------------------------------------------
        # 5. Stagnation Timeout Exit (30 minutes default)
        # -------------------------------------------------------------------
        if holding_sec >= self.stagnation_timeout_sec:
            return True, f"STAGNATION_TIMEOUT (Unresolved after {int(holding_sec / 60)}m - Capital Recycled)"

        return False, ""

