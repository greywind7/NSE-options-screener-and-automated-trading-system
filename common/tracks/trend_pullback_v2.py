"""
Track 8: Trend Pullback V2 Track.
Volatility & Spread Calibrated: Eliminates the synthetic -5% stop-loss friction by calibrating
initial SL to actual bid-ask spread plus volatility buffer: SL = max(0.06, 1.5 * spread_pct + 0.03).
Locks Breakeven at +4%, and trails +0.8% for every +1% price advance up to +25% target.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class TrendPullbackV2Track(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.25,                  # +25% Target
        sl_pct: float = 0.06,                      # Baseline -6% SL (dynamically calibrated)
        breakeven_trigger_pct: float = 0.04,       # +4% Breakeven Lock
        trailing_step_pct: float = 0.008,          # +0.8% trail per +1% gain
        timeout_sec: float = 3600.0,               # 60-minute holding horizon
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ITM1",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"trend_pullback_v2_{preferred_mode}" if preferred_mode else "trend_pullback_v2")
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
        self.trailing_step_pct = float(trailing_step_pct)
        self.timeout_sec = float(timeout_sec)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        return opp.strikes.get("ITM1") or opp.strikes.get("ITM") or opp.strikes.get("ATM")

    def on_opportunity(self, opp: ActionableOpportunity) -> Optional[TradeState]:
        # Dynamically calibrate initial stop-loss to bid-ask spread
        strike_info = self.select_strike(opp)
        if strike_info:
            ask = float(strike_info.get("ask", 0.0))
            bid = float(strike_info.get("bid", 0.0))
            if ask > 0.0 and bid > 0.0:
                spread_pct = (ask - bid) / ask
                # Invariant: SL must be wider than the immediate bid-ask spread
                calibrated_sl = max(0.06, 1.5 * spread_pct + 0.03)
                self.sl_pct = calibrated_sl

        return super().on_opportunity(opp)

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

        # 3. Dynamic Stepped Trailing above +4%
        if profit_pct >= self.breakeven_trigger_pct - 1e-6:
            # Base Breakeven
            base_be = trade.entry_price
            excess = profit_pct - self.breakeven_trigger_pct
            if excess > 0.01:
                # 0.8% trail for every 1% gain
                trail_pts = trade.entry_price * (0.80 * excess)
                base_be = round(trade.entry_price + trail_pts, 2)
            trade.stop_loss = max(trade.stop_loss, base_be)
            trade.ratchet_level = max(trade.ratchet_level, 1)

        # 4. Timeout (60 minutes)
        if elapsed >= self.timeout_sec:
            return "TIMEOUT"

        return None
