"""
Adaptive Regime Ratchet Track.
Extends AdaptiveRegimeTrack with regime-calibrated progressive stepped ratcheting across
all three intraday clock phases (Morning Thrust, Midday Consolidation, Afternoon Trend),
preventing profitable trades from decaying into timeouts or full stop-outs.
"""
from typing import Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.adaptive_regime import AdaptiveRegimeTrack


class AdaptiveRegimeRatchetTrack(AdaptiveRegimeTrack):
    """Multi-phase adaptive track with consistent per-phase stepped profit ratcheting."""

    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.18,
        sl_pct: float = 0.07,
        breakeven_trigger_pct: float = 0.05,       # Morning/Afternoon BE trigger (+5%)
        trailing_step_pct: float = 0.025,          # Morning/Afternoon ratchet step (+2.5%)
        midday_breakeven_pct: float = 0.03,        # Midday choppy-session BE trigger (+3%)
        midday_trailing_step_pct: float = 0.015,   # Midday ratchet step (+1.5%)
        midday_timeout_sec: float = 600.0,         # 10-minute midday consolidation timeout
        track_name: Optional[str] = None,
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        be_pct = float(breakeven_trigger_pct)
        if be_pct > 1.0:
            be_pct /= 100.0
        step_pct = float(trailing_step_pct if trailing_step_pct is not None else 0.025)
        if step_pct > 1.0:
            step_pct /= 100.0
        mid_be = float(midday_breakeven_pct if midday_breakeven_pct is not None else 0.03)
        if mid_be > 1.0:
            mid_be /= 100.0
        mid_step = float(midday_trailing_step_pct if midday_trailing_step_pct is not None else 0.015)
        if mid_step > 1.0:
            mid_step /= 100.0

        name = track_name or "adaptive_regime_ratchet"
        super().__init__(
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            track_name=name,
            parent_track=parent_track,
            **kwargs,
        )
        # Override base breakeven_trigger_pct cleanly so UI/factory edits propagate consistently
        self.breakeven_trigger_pct = be_pct
        self.trailing_step_pct = step_pct
        self.midday_breakeven_pct = mid_be
        self.midday_trailing_step_pct = mid_step
        self.midday_timeout_sec = float(midday_timeout_sec)

    def _apply_stepped_ratchet(self, trade: TradeState, peak_profit_pct: float, be_trigger: float, step_pct: float) -> None:
        if peak_profit_pct < (be_trigger - 1e-6):
            return
        be_floor = round(trade.entry_price * 1.005, 2)
        trade.stop_loss = max(trade.stop_loss, be_floor)
        trade.ratchet_level = max(trade.ratchet_level, 1)
        trade.metadata["breakeven_triggered"] = True

        if step_pct > 0.0:
            excess_pct = peak_profit_pct - be_trigger
            steps = int((excess_pct + 1e-9) / step_pct)
            if steps > 0:
                stepped_sl = round(trade.entry_price * (1.0 + (steps * step_pct)), 2)
                trade.stop_loss = max(trade.stop_loss, stepped_sl)
                trade.ratchet_level = max(trade.ratchet_level, steps + 1)

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        trade.highest_price = max(trade.highest_price, bid)
        phase = trade.metadata.get("regime_phase") or self._get_regime_phase(trade.entry_time)

        if trade.entry_price <= 0.0:
            return None

        profit_pct = (bid - trade.entry_price) / trade.entry_price
        peak_profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price

        # 1. Hard Target Hit
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss / Trailed Ratchet Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Consistent Regime-Calibrated Stepped Ratcheting
        if phase == "MIDDAY":
            self._apply_stepped_ratchet(
                trade,
                peak_profit_pct=peak_profit_pct,
                be_trigger=self.midday_breakeven_pct,
                step_pct=self.midday_trailing_step_pct,
            )
            if elapsed >= self.midday_timeout_sec and profit_pct < 0.02 and trade.ratchet_level < 2:
                return "MIDDAY_CONSOLIDATION_TIMEOUT"
        elif phase == "AFTERNOON":
            self._apply_stepped_ratchet(
                trade,
                peak_profit_pct=peak_profit_pct,
                be_trigger=self.breakeven_trigger_pct,
                step_pct=self.trailing_step_pct,
            )
            if peak_profit_pct >= 0.10 - 1e-6:
                trail_sl = round(trade.entry_price * (1.0 + 0.50 * peak_profit_pct), 2)
                trade.stop_loss = max(trade.stop_loss, trail_sl)
                trade.ratchet_level = max(trade.ratchet_level, 2)
        else:
            # MORNING phase
            self._apply_stepped_ratchet(
                trade,
                peak_profit_pct=peak_profit_pct,
                be_trigger=self.breakeven_trigger_pct,
                step_pct=self.trailing_step_pct,
            )

        return None
