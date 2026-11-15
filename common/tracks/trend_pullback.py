"""
Track 2: Trend Pullback Track.
Trend-following swing with wider breathing room: +25% Ask Target, -12% Ask SL,
prefers ITM1 strike for delta preservation, stepped trailing ratchets at +15% and +20%.
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
        sl_pct: float = 0.12,      # -12% on Ask
        max_trades: int = 2,
        trailing_step_pct: float = 0.02,
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
            max_trades=max_trades,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        self.trailing_step_pct = trailing_step_pct

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        # Prefers ITM1 for higher delta, falls back to ATM
        return opp.strikes.get("ITM1") or opp.strikes.get("ATM") or opp.strikes.get("OTM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time

        # 1. Hard Target Check
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Hard Stop-Loss Check
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Stepped Trailing SL Ratchets
        profit_pct = (bid - trade.entry_price) / trade.entry_price

        # Step 1: At +15% profit, move SL to +5%
        if profit_pct >= 0.15 - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.05, 2))
            trade.ratchet_level = 1

        # Step 2: At +20% profit, move SL to +12%
        if profit_pct >= 0.20 - 1e-6 and trade.ratchet_level < 2:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.12, 2))
            trade.ratchet_level = 2

        # 4. Long-Horizon Stagnation Timeout: 30 minutes (1800s) and profit < +5%
        if elapsed >= 1800.0 and profit_pct < (0.05 - 1e-6):
            return "PULLBACK_STAGNATION_TIMEOUT"

        return None
