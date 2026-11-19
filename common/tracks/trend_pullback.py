"""
Track 2: Trend Pullback Track.
Trend-following swing with wider breathing room: +25% Ask Target, -5% Ask SL.
Features dynamic stepped trailing: once breakeven is triggered (default +2.5%),
trails stop loss for every 1% (trailing_step_pct) increase in contract value,
locking in profits as price advances. Includes configurable max-hold timeout_sec.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class TrendPullbackTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.25,  # +25% on Ask
        sl_pct: float = 0.05,      # -5% on Ask
        breakeven_trigger_pct: Optional[float] = 0.025,
        max_trades: int = 2,
        trailing_step_pct: float = 0.01,
        timeout_sec: float = 3600.0,
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = None,
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"trend_pullback_{preferred_mode}" if preferred_mode else "trend_pullback")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=breakeven_trigger_pct,
            max_trades=max_trades,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        step_pct = kwargs.get("trailing_step_pct", trailing_step_pct)
        self.trailing_step_pct = float(step_pct) if step_pct is not None else 0.01
        if self.trailing_step_pct > 1.0:
            self.trailing_step_pct /= 100.0

        timeout = timeout_sec if timeout_sec is not None else (kwargs.get("timeout_sec") or kwargs.get("stagnation_timeout_sec") or 3600.0)
        self.timeout_sec = float(timeout)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        # Prefers ITM1 for higher delta, falls back to ATM
        return opp.strikes.get("ITM1") or opp.strikes.get("ATM") or opp.strikes.get("OTM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        trade.highest_price = max(trade.highest_price, bid)

        # 1. Stepped Trailing Stop Loss Logic:
        # After breakeven is triggered (at breakeven_trigger_pct, e.g. +2.5%),
        # trail stop loss every 1% (trailing_step_pct) increase in the contract value.
        if trade.entry_price > 0:
            max_profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price

            if self.breakeven_trigger_pct and max_profit_pct >= (self.breakeven_trigger_pct - 1e-6):
                # Lock in breakeven (entry price) as baseline stop-loss floor
                trade.stop_loss = max(trade.stop_loss, trade.entry_price)
                trade.metadata["breakeven_triggered"] = True

                # For every trailing_step_pct increase above breakeven trigger, trail SL upward
                if self.trailing_step_pct and self.trailing_step_pct > 0.0:
                    excess_pct = max_profit_pct - self.breakeven_trigger_pct
                    steps = int(excess_pct / self.trailing_step_pct)
                    if steps > 0:
                        stepped_sl = round(trade.entry_price * (1.0 + (steps * self.trailing_step_pct)), 2)
                        trade.stop_loss = max(trade.stop_loss, stepped_sl)
                        trade.ratchet_level = max(trade.ratchet_level, steps)

        # 2. Hard Target Check
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 3. Stop-Loss Check (initial SL, breakeven SL, or trailed SL)
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 4. Configurable Max Holding Timeout
        if elapsed >= self.timeout_sec:
            return "PULLBACK_TIMEOUT"

        return None
