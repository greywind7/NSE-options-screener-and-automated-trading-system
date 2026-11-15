"""
Track 1: Fast Momentum Track.
High-velocity breakout scalp: +12% Ask Target, -8% Ask SL, 5-minute stagnation timeout,
target decay after 3 minutes, aggressive multi-tier breakeven ratchets.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class FastMomentumTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.12,  # +12% on Ask
        sl_pct: float = 0.08,      # -8% on Ask
        max_trades: int = 2,
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = None,
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"fast_momentum_{preferred_mode}" if preferred_mode else "fast_momentum")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            max_trades=max_trades,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        # Prefers ATM, fallbacks to OTM1 or ITM1
        return opp.strikes.get("ATM") or opp.strikes.get("OTM1") or opp.strikes.get("ITM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time

        # 1. Target Decay: After 3 minutes (180s), decay target by 1% per minute down to floor of +4%
        if elapsed >= 180.0:
            extra_mins = int((elapsed - 180.0) // 60)
            decayed_target_pct = max(0.04, self.target_pct - (extra_mins * 0.01))
            trade.target_price = min(trade.target_price, trade.entry_price * (1.0 + decayed_target_pct))

        # 2. Hard Target Check
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 3. Hard Stop-Loss Check
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 4. Multi-tier Trailing SL Ratchets
        profit_pct = (bid - trade.entry_price) / trade.entry_price

        # Tier 1: At +6% profit, ratchet SL to breakeven (entry price)
        if profit_pct >= 0.06 - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            trade.ratchet_level = 1

        # Tier 2: At +9% profit, ratchet SL to lock in +4%
        if profit_pct >= 0.09 - 1e-6 and trade.ratchet_level < 2:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.04, 2))
            trade.ratchet_level = 2

        # 5. Stagnation Timeout: 5 minutes (300s) and profit < +3%
        if elapsed >= 300.0 and profit_pct < (0.03 - 1e-6):
            return "STAGNATION_TIMEOUT"

        return None
