"""
Track 1: Structural Convexity Track.
Low Risk / High Reward: Anchors primary invalidation to the underlying stock's flag support level,
eliminating synthetic option stop-outs. Stage 1 locks breakeven at +8%, and Stage 2 trails 50% of peak profit open-ended.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class StructuralConvexityTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 1.0,               # Open-ended target (+100%)
        sl_pct: float = 0.12,                  # -12% Emergency catastrophic option SL
        breakeven_trigger_pct: float = 0.08,   # +8% Breakeven free-roll
        profit_trail_threshold_pct: float = 0.15, # +15% Activates 50% peak trail
        profit_trail_ratio: float = 0.50,      # Lock 50% of peak profit
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ATM",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"structural_convexity_{preferred_mode}" if preferred_mode else "structural_convexity")
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
        self.profit_trail_threshold_pct = float(profit_trail_threshold_pct)
        self.profit_trail_ratio = float(profit_trail_ratio)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        return opp.strikes.get("ATM") or opp.strikes.get("ITM1")

    def on_underlying_tick(self, trade: TradeState, ltp: float, ts: float) -> None:
        """Evaluate underlying structural support violation in real-time."""
        direction = trade.metadata.get("direction", "BULL")
        flag_low = trade.metadata.get("flag_low")
        flag_high = trade.metadata.get("flag_high")

        if direction == "BULL" and flag_low is not None and ltp <= float(flag_low):
            exit_p = trade.current_bid if trade.current_bid > 0.0 else 0.0
            self.exit_trade(trade, exit_price=exit_p, reason="STRUCTURAL_INVALIDATION", ts=ts)
        elif direction == "BEAR" and flag_high is not None and ltp >= float(flag_high):
            exit_p = trade.current_bid if trade.current_bid > 0.0 else 0.0
            self.exit_trade(trade, exit_price=exit_p, reason="STRUCTURAL_INVALIDATION", ts=ts)

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        # 1. Structural Invalidation via latest underlying equity LTP
        direction = trade.metadata.get("direction", "BULL")
        flag_low = trade.metadata.get("flag_low")
        flag_high = trade.metadata.get("flag_high")
        u_ltp = trade.metadata.get("underlying_ltp")

        if u_ltp is not None:
            if direction == "BULL" and flag_low is not None and u_ltp <= float(flag_low):
                return "STRUCTURAL_INVALIDATION"
            elif direction == "BEAR" and flag_high is not None and u_ltp >= float(flag_high):
                return "STRUCTURAL_INVALIDATION"

        # 2. Hard Emergency Option Stop Loss (-12%)
        if bid <= trade.stop_loss:
            return "EMERGENCY_SL_HIT"

        # 3. Target Check
        if trade.target_price and bid >= trade.target_price:
            return "TARGET_HIT"

        # 4. Asymmetric Trailing & Breakeven Ratchets
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
        peak_profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # Stage 1: Breakeven Free-Roll at +8% (locks entry + 0.5% brokerage buffer)
        if profit_pct >= self.breakeven_trigger_pct - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.005, 2))
            trade.ratchet_level = 1

        # Stage 2: Asymmetric 50% Profit Trail at +15%
        if peak_profit_pct >= self.profit_trail_threshold_pct - 1e-6:
            trail_sl = round(trade.entry_price * (1.0 + self.profit_trail_ratio * peak_profit_pct), 2)
            trade.stop_loss = max(trade.stop_loss, trail_sl)
            trade.ratchet_level = max(trade.ratchet_level, 2)

        return None
