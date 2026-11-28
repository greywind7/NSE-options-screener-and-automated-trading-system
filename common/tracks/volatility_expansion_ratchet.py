"""
Volatility Expansion Ratchet Track.
Extends VolatilityExpansionTrack with progressive monotonic stepped ratcheting:
locks breakeven + brokerage buffer early (+5%), then advances the trailing stop-loss
in fixed increments as option premium climbs, preventing winning expansion trades
from stagnating or giving back gains into EOD/deceleration exits.
"""
from typing import Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.volatility_expansion import VolatilityExpansionTrack


class VolatilityExpansionRatchetTrack(VolatilityExpansionTrack):
    """IV-expansion breakout track with consistent progressive stepped ratcheting."""

    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.25,                  # +25% Ask Target
        sl_pct: float = 0.08,                      # -8% Ask SL
        breakeven_trigger_pct: float = 0.05,       # +5% Breakeven + brokerage buffer Lock
        trailing_step_pct: float = 0.025,          # +2.5% progressive ratchet step
        deceleration_timeout_sec: float = 300.0,   # 5-minute deceleration timeout
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ATM",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (
            f"volatility_expansion_ratchet_{preferred_mode}"
            if preferred_mode
            else "volatility_expansion_ratchet"
        )
        be_pct = float(breakeven_trigger_pct)
        if be_pct > 1.0:
            be_pct /= 100.0
        super().__init__(
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=be_pct,
            deceleration_timeout_sec=deceleration_timeout_sec,
            track_name=name,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        step_pct = float(trailing_step_pct if trailing_step_pct is not None else 0.025)
        if step_pct > 1.0:
            step_pct /= 100.0
        self.trailing_step_pct: float = step_pct

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        trade.highest_price = max(trade.highest_price, bid)

        if trade.entry_price <= 0.0:
            return None

        profit_pct = (bid - trade.entry_price) / trade.entry_price
        peak_profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price

        # 1. Hard Target Hit (+25%)
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss / Trailed Ratchet Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Progressive Monotonic Ratcheting
        if peak_profit_pct >= (self.breakeven_trigger_pct - 1e-6):
            # Level 1: Lock entry + 0.5% brokerage buffer at breakeven trigger
            be_floor = round(trade.entry_price * 1.005, 2)
            trade.stop_loss = max(trade.stop_loss, be_floor)
            trade.ratchet_level = max(trade.ratchet_level, 1)
            trade.metadata["breakeven_triggered"] = True

            # Subsequent Levels: advance SL by trailing_step_pct per step above breakeven
            if self.trailing_step_pct > 0.0:
                excess_pct = peak_profit_pct - self.breakeven_trigger_pct
                steps = int((excess_pct + 1e-9) / self.trailing_step_pct)
                if steps > 0:
                    stepped_sl = round(trade.entry_price * (1.0 + (steps * self.trailing_step_pct)), 2)
                    trade.stop_loss = max(trade.stop_loss, stepped_sl)
                    trade.ratchet_level = max(trade.ratchet_level, steps + 1)

        # 4. Early Deceleration Timeout (only before ratcheting locks profits beyond buffer)
        if elapsed >= self.deceleration_timeout_sec and profit_pct < 0.03 and trade.ratchet_level < 2:
            return "DECELERATION_TIMEOUT"

        return None
