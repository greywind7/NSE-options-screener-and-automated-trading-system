"""
Track 5: Gamma Runner Track.
High Risk / High Reward: Targets asymmetric explosive moves using Out-of-the-Money contracts (OTM2, Delta ~0.30).
Small absolute INR capital commitment per lot, wide -15% SL to withstand OTM delta noise,
locking breakeven at +25% and scaling into +50% to +100% targets.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class GammaRunnerTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.75,              # +75% Target on OTM
        sl_pct: float = 0.15,                  # -15% Option SL
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "OTM2",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"gamma_runner_{preferred_mode}" if preferred_mode else "gamma_runner")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=0.25,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        # Prefers OTM2, fallbacks to OTM1 or OTM
        return opp.strikes.get("OTM2") or opp.strikes.get("OTM1") or opp.strikes.get("OTM") or opp.strikes.get("ATM")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # 1. Target Hit (+75%)
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss Hit (-15%)
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Tiered Ratchets for Explosive Runners (scaled by option convexity factor)
        for lvl, trig_pct, lock_pct in (
            (3, 0.75, 0.50),
            (2, 0.50, 0.25),
            (1, 0.25, 0.01),
        ):
            if profit_pct >= self._scaled_pct(trade, trig_pct) - 1e-6 and trade.ratchet_level < lvl:
                sl_price = round(trade.entry_price * (1.0 + self._scaled_pct(trade, lock_pct)), 2)
                trade.stop_loss = max(trade.stop_loss, sl_price)
                trade.ratchet_level = lvl
                break

        return None
