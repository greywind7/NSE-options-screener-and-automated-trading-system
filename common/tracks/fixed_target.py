"""
Track 3: Fixed Target Track.
Consistent absolute PnL lock: ₹2,000 INR fixed target per lot or +15% Ask Target,
-10% Ask SL, breakeven ratchet at +8%.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class FixedTargetTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.15,            # +15% on Ask
        sl_pct: float = 0.10,                # -10% on Ask
        breakeven_trigger_pct: Optional[float] = 0.025,
        fixed_target_inr: float = 2000.0,    # ₹2,000 INR target per lot
        timeout_sec: float = 600.0,
        max_trades: int = 2,
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = None,
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"fixed_target_{preferred_mode}" if preferred_mode else "fixed_target")
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
        self.fixed_target_inr = fixed_target_inr
        timeout = timeout_sec if timeout_sec is not None else (kwargs.get("timeout_sec") or kwargs.get("stagnation_timeout_sec") or 600.0)
        self.timeout_sec = float(timeout)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        return opp.strikes.get("ATM") or opp.strikes.get("OTM1") or opp.strikes.get("ITM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        current_inr_pnl = (bid - trade.entry_price) * trade.quantity

        # 1. Absolute INR Target Gate
        if current_inr_pnl >= self.fixed_target_inr:
            return "INR_TARGET_HIT"

        # 2. Percentage Target Check
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 3. Hard Stop-Loss Check
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 4. Breakeven Ratchet at +8%
        profit_pct = (bid - trade.entry_price) / trade.entry_price
        if profit_pct >= 0.08 - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            trade.ratchet_level = 1

        # 5. Stagnation Timeout: timeout_sec and negative PnL
        if elapsed >= self.timeout_sec and current_inr_pnl < 0:
            return "FIXED_STAGNATION_TIMEOUT"

        return None
