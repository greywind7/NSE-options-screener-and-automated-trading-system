"""
Track 4: Micro-Chandelier Track.
Low Risk / Low Reward: High-frequency profit locking on ITM1 / ATM contracts.
Targets +10%, SL -5%, micro-trailing locks every +2% gain, 300s stagnation timeout.
"""
from typing import Any, Dict, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity


class MicroChandelierTrack(BaseTrack):
    def __init__(
        self,
        context: ExecutionContext,
        target_pct: float = 0.10,              # +10% Target
        sl_pct: float = 0.05,                  # -5% Stop Loss
        stagnation_timeout_sec: float = 300.0, # 5-minute stagnation timeout
        track_name: Optional[str] = None,
        preferred_mode: Optional[str] = "ITM1",
        parent_track: Optional[str] = None,
        **kwargs,
    ):
        name = track_name or (f"micro_chandelier_{preferred_mode}" if preferred_mode else "micro_chandelier")
        super().__init__(
            track_name=name,
            context=context,
            target_pct=target_pct,
            sl_pct=sl_pct,
            breakeven_trigger_pct=0.03,
            preferred_mode=preferred_mode,
            parent_track=parent_track,
            **kwargs,
        )
        self.stagnation_timeout_sec = float(stagnation_timeout_sec)

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        return opp.strikes.get("ITM1") or opp.strikes.get("ITM") or opp.strikes.get("ATM")

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        elapsed = ts - trade.entry_time
        profit_pct = (bid - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0

        # 1. Target Hit (+10%)
        if bid >= trade.target_price:
            return "TARGET_HIT"

        # 2. Stop Loss Hit
        if bid <= trade.stop_loss:
            return "SL_HIT"

        # 3. Micro-Trailing Ratchets
        if profit_pct >= 0.09 - 1e-6 and trade.ratchet_level < 4:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.07, 2))
            trade.ratchet_level = 4
        elif profit_pct >= 0.07 - 1e-6 and trade.ratchet_level < 3:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.05, 2))
            trade.ratchet_level = 3
        elif profit_pct >= 0.05 - 1e-6 and trade.ratchet_level < 2:
            trade.stop_loss = max(trade.stop_loss, round(trade.entry_price * 1.03, 2))
            trade.ratchet_level = 2
        elif profit_pct >= 0.03 - 1e-6 and trade.ratchet_level < 1:
            trade.stop_loss = max(trade.stop_loss, trade.entry_price)
            trade.ratchet_level = 1

        # 4. Stagnation Timeout (5 min and profit < +2%)
        if elapsed >= self.stagnation_timeout_sec and profit_pct < 0.02:
            return "STAGNATION_TIMEOUT"

        return None
