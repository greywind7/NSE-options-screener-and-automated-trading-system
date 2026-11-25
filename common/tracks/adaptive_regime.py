"""
Track 7: Adaptive Regime Track (All-Weather Multi-Regime).
Automatically switches contract moneyness, risk limits, and holding durations based on time of day:
- Phase 1 (Morning Thrust 09:15-10:30): ATM, Target +18%, SL -7%, Breakeven at +7%.
- Phase 2 (Midday Chop 10:30-13:30): ITM1, Target +8%, SL -5%, 10-minute timeout.
- Phase 3 (Afternoon Trend 13:30-15:00): ATM, Target +25%, SL -7%, 50% peak trail above +10%.
"""
from datetime import datetime
import time
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity
from core.common.time_utils import IST


class AdaptiveRegimeTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.18,
        sl_pct: float = 0.07,
        track_name: Optional[str] = None,
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or "adaptive_regime"
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=0.07,
            preferred_mode=None,  # Dynamically resolved per opportunity
            parent_track=parent_track,
            **kwargs,
        )

    def _get_regime_phase(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, tz=IST)
        curr_min = dt.hour * 60 + dt.minute
        if curr_min < (10 * 60 + 30):
            return "MORNING"
        elif curr_min < (13 * 60 + 30):
            return "MIDDAY"
        return "AFTERNOON"

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        now_ts = opp.timestamp or time.time()
        phase = self._get_regime_phase(now_ts)
        if phase == "MIDDAY":
            # Protect from midday chop with intrinsic ITM1
            return opp.strikes.get("ITM1") or opp.strikes.get("ITM") or opp.strikes.get("ATM")
        # Morning & Afternoon: ATM velocity
        return opp.strikes.get("ATM") or opp.strikes.get("ITM1")

    def on_opportunity(self, opp: ActionableOpportunity) -> Optional[TradeState]:
        now_ts = opp.timestamp or time.time()
        phase = self._get_regime_phase(now_ts)

        # Dynamically compute target & SL per trade without mutating shared instance attributes
        if phase == "MORNING":
            t_pct, s_pct = 0.18, 0.07
        elif phase == "MIDDAY":
            t_pct, s_pct = 0.08, 0.05
        else:
            t_pct, s_pct = 0.25, 0.07

        trade = super().on_opportunity(opp, target_pct_override=t_pct, sl_pct_override=s_pct)
        if trade:
            trade.metadata["regime_phase"] = phase
        return trade

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        phase = trade.metadata.get("regime_phase") or self._get_regime_phase(trade.entry_time)
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
        peak_profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # Hard Target Hit
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # Hard Stop Loss Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # Regime-Specific Logic
        if phase == "MORNING":
            # Breakeven at +7%
            if profit_pct >= 0.07 - 1e-6 and trade.ratchet_level < 1:
                trade.stop_loss = max(trade.stop_loss, trade.entry_price)
                trade.ratchet_level = 1

        elif phase == "MIDDAY":
            # 10-minute timeout for midday consolidation
            if elapsed >= 600.0 and profit_pct < 0.02:
                return "MIDDAY_CONSOLIDATION_TIMEOUT"

        elif phase == "AFTERNOON":
            # Asymmetric 50% peak profit trail above +10%
            if peak_profit_pct >= 0.10 - 1e-6:
                trail_sl = round(trade.entry_price * (1.0 + 0.50 * peak_profit_pct), 2)
                trade.stop_loss = max(trade.stop_loss, trail_sl)
                trade.ratchet_level = max(trade.ratchet_level, 2)

        return None
