"""
Track 2: Opening Thrust Convexity Track.
Low Risk / High Reward: Operates strictly during the high-velocity opening window (09:15 - 10:30 IST).
Uses tight -6% SL, locks Breakeven at +15%, and trails 1% for every 1% advance up to +35% target.
Enforces hard morning session square-off at 10:45 IST to avoid midday chop.
"""
from datetime import datetime
import time
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity
from core.common.time_utils import IST


class OpeningThrustConvexityTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.35,              # +35% Ask Target
        sl_pct: float = 0.06,                  # -6% Ask SL
        breakeven_trigger_pct: float = 0.15,   # +15% Breakeven Trigger
        cutoff_time: str = "10:45",            # 10:45 IST Hard morning cutoff
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ATM",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"opening_thrust_convex_{preferred_mode}" if preferred_mode else "opening_thrust_convex")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=breakeven_trigger_pct,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        self.cutoff_time = str(cutoff_time or "10:45")
        try:
            parts = self.cutoff_time.split(":")
            self.cutoff_minutes = int(parts[0]) * 60 + int(parts[1])
        except Exception:
            self.cutoff_minutes = 10 * 60 + 45

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        return opp.strikes.get("ATM") or opp.strikes.get("ITM1")

    def on_opportunity(self, opp: ActionableOpportunity) -> Optional[TradeState]:
        # Strict Entry Window Gate: 09:15 to 10:30 IST only
        now_ts = opp.timestamp or time.time()
        dt = datetime.fromtimestamp(now_ts, tz=IST)
        curr_min = dt.hour * 60 + dt.minute
        if curr_min < (9 * 60 + 15) or curr_min > (10 * 60 + 30):
            return None
        return super().on_opportunity(opp)

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        # 1. Morning Session Cutoff Check (10:45 IST)
        dt = datetime.fromtimestamp(ts, tz=IST)
        curr_min = dt.hour * 60 + dt.minute
        if curr_min >= self.cutoff_minutes:
            return "MORNING_SESSION_CUTOFF"

        # 2. Hard Target Hit
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 3. Stop Loss Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 4. Stepped Trailing: At +15%, lock breakeven; for every +1% beyond, trail SL up by +1%
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
        if profit_pct >= self.breakeven_trigger_pct - 1e-6:
            # Base breakeven
            stepped_sl = trade.entry_price
            # Additional 1:1 trail above 15%
            excess = profit_pct - self.breakeven_trigger_pct
            if excess > 0.01:
                stepped_sl = round(trade.entry_price * (1.0 + excess), 2)
            trade.stop_loss = max(trade.stop_loss, stepped_sl)
            trade.ratchet_level = max(trade.ratchet_level, 1)

        return None
