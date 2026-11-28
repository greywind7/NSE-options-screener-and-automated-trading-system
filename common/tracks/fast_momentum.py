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
        breakeven_trigger_pct: Optional[float] = 0.025,
        target_decay_sec: float = 180.0,
        decay_floor_pct: float = 0.04,
        stagnation_timeout_sec: float = 300.0,
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
            breakeven_trigger_pct=breakeven_trigger_pct,
            max_trades=max_trades,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        self.target_decay_sec = float(target_decay_sec if target_decay_sec is not None else kwargs.get("target_decay_sec", 180.0))
        d_floor = decay_floor_pct if decay_floor_pct is not None else kwargs.get("decay_floor_pct", 0.04)
        self.decay_floor_pct = float(d_floor) / 100.0 if float(d_floor) > 1.0 else float(d_floor)
        stag = stagnation_timeout_sec if stagnation_timeout_sec is not None else (kwargs.get("stagnation_timeout_sec") or kwargs.get("timeout_sec") or 300.0)
        self.stagnation_timeout_sec = float(stag)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        if self.preferred_mode:
            return super().select_strike(opp)
        # Prefers ATM, fallbacks to OTM1 or ITM1
        return opp.strikes.get("ATM") or opp.strikes.get("OTM1") or opp.strikes.get("ITM1")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time

        # 1. Target Decay: After target_decay_sec, decay target by scaled 1%/min down to scaled decay_floor_pct
        if elapsed >= self.target_decay_sec:
            extra_mins = int((elapsed - self.target_decay_sec) // 60)
            base_target_pct = float(trade.metadata.get("target_pct", self.target_pct))
            decay_step = self._scaled_pct(trade, 0.01)
            decay_floor = self._scaled_pct(trade, self.decay_floor_pct)
            decayed_target_pct = max(decay_floor, base_target_pct - (extra_mins * decay_step))
            trade.target_price = min(trade.target_price, trade.entry_price * (1.0 + decayed_target_pct))

        # 2. Hard Target Check
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 3. Hard Stop-Loss Check
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 4. Multi-tier Trailing SL Ratchets (scaled by option convexity factor)
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
        tier1_trigger = self._scaled_pct(trade, 0.06)
        tier2_trigger = self._scaled_pct(trade, 0.09)
        tier2_lock = self._scaled_pct(trade, 0.04)
        stag_floor = self._scaled_pct(trade, 0.03)

        # Tier 1: At scaled +6% profit, ratchet SL to breakeven (entry price)
        if profit_pct >= tier1_trigger - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            trade.ratchet_level = 1

        # Tier 2: At scaled +9% profit, ratchet SL to lock in scaled +4%
        if profit_pct >= tier2_trigger - 1e-6 and trade.ratchet_level < 2:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * (1.0 + tier2_lock), 2))
            trade.ratchet_level = 2

        # 5. Stagnation Timeout: stagnation_timeout_sec and profit < scaled +3%
        if elapsed >= self.stagnation_timeout_sec and profit_pct < (stag_floor - 1e-6):
            return "STAGNATION_TIMEOUT"

        return None
