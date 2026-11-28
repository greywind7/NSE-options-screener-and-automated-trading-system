"""
Track 3: Delta Scalp Track.
Low Risk / Low Reward: High-probability steady compounding with Deep In-The-Money options (ITM2, Delta ~0.85).
Zero theta drag, minimal Vega exposure, +7% Ask Target, -4% Ask SL, and 180s velocity timeout.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class DeltaScalpTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.07,              # +7% Target on Deep ITM
        sl_pct: float = 0.04,                  # -4% Tight SL
        velocity_timeout_sec: float = 180.0,   # 3-minute velocity timeout
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ITM2",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"delta_scalp_{preferred_mode}" if preferred_mode else "delta_scalp")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=None,        # Pure rapid scalp
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        self.velocity_timeout_sec = float(velocity_timeout_sec)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        # Prefers Deep ITM (ITM2), fallbacks to ITM1 or ITM
        return opp.strikes.get("ITM2") or opp.strikes.get("ITM1") or opp.strikes.get("ITM") or opp.strikes.get("ATM")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # 1. Target Hit (+7%)
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss Hit (-4%)
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Velocity Timeout: If trade hasn't moved into profit within 180 seconds, cut to preserve capital
        if elapsed >= self.velocity_timeout_sec and profit_pct < self._scaled_pct(trade, 0.02):
            return "VELOCITY_TIMEOUT"

        return None
