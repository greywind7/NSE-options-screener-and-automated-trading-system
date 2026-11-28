"""
Track 6: Volatility Expansion Track.
High Risk / High Reward: Capitalizes on simultaneous price expansion and implied volatility thrust on ATM contracts.
Targets +25%, SL -8%, Breakeven at +10%, and exits on momentum deceleration after 5 minutes if not in profit.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class VolatilityExpansionTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.25,                  # +25% Ask Target
        sl_pct: float = 0.08,                      # -8% Ask SL
        breakeven_trigger_pct: float = 0.10,       # +10% Breakeven Lock
        deceleration_timeout_sec: float = 300.0,   # 5-minute deceleration timeout
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ATM",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"volatility_expansion_{preferred_mode}" if preferred_mode else "volatility_expansion")
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
        self.deceleration_timeout_sec = float(deceleration_timeout_sec)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        return opp.strikes.get("ATM") or opp.strikes.get("ITM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # 1. Target Hit (+25%)
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Breakeven Ratchet at scaled +10%
        eff_be = self._scaled_pct(trade, self.breakeven_trigger_pct)
        if profit_pct >= eff_be - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            trade.ratchet_level = 1

        # 4. Deceleration Timeout: If after 5 minutes profit is < scaled +3%, exit to avoid IV crush
        if elapsed >= self.deceleration_timeout_sec and profit_pct < self._scaled_pct(trade, 0.03):
            return "DECELERATION_TIMEOUT"

        return None
