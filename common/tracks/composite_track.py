"""
Composite Multi-Horizon & Multi-Track Aggregator (Dynamic Trio / Combination).
Combines constituent sub-tracks with either shared capital pool allocation or partitioned equal share quota.
Enforces capital availability limits with zero arbitrary max_trades ceilings.
"""
from typing import Any, Dict, List, Optional
from common.interfaces import ExecutionContext, TradeState
from common.tracks.base_track import BaseTrack
from core.common.ipc_schemas import ActionableOpportunity, OrderResult


class CompositeTrack:
    def __init__(
        self,
        context: ExecutionContext,
        track_name: str = "combination",
        sub_tracks: Optional[List[BaseTrack]] = None,
        shared_pool: bool = True,
        total_capital: Optional[float] = None,
        max_trades: Optional[int] = None,
        target_pct: float = 0.35,
        sl_pct: float = 0.10,
        **kwargs,
    ):
        self.track_name = track_name
        self.context = context
        self.sub_tracks: List[BaseTrack] = sub_tracks or []
        self.shared_pool = shared_pool
        self.total_capital = float(total_capital or kwargs.get("capital") or 250000.0)
        self.max_trades = max_trades  # Legacy parameter (unconstrained)
        self.target_pct = target_pct
        self.sl_pct = sl_pct

        # Capital Availability Management
        self.committed_capital: float = 0.0
        self.realized_pnl: float = 0.0

        if not self.sub_tracks:
            # Fallback single track behavior
            from common.tracks.fast_momentum import FastMomentumTrack
            legacy = FastMomentumTrack(
                context=context,
                track_name=self.track_name,
                target_pct=target_pct,
                sl_pct=sl_pct,
                allocated_capital=self.total_capital,
            )
            self.sub_tracks = [legacy]

        # Configure capital pool sharing or partitioning
        if not self.shared_pool and self.sub_tracks:
            # Shared pool FALSE: each sub-track gets equal share from total pool
            share = self.total_capital / len(self.sub_tracks)
            for st in self.sub_tracks:
                st.parent_track = self.track_name
                st.parent_composite = None  # Each sub-track enforces its own independent partition
                st.allocated_capital = share
                st.committed_capital = 0.0
                st.realized_pnl = 0.0
        else:
            # Shared pool TRUE: sub-tracks share the same composite capital pool
            for st in self.sub_tracks:
                st.parent_track = self.track_name
                st.parent_composite = self

    @property
    def available_capital(self) -> float:
        """Available uncommitted capital in the shared pool."""
        net_worth = self.total_capital + self.realized_pnl
        return max(0.0, net_worth - self.committed_capital)

    def can_allocate(self, required_capital: float) -> bool:
        """Evaluate capital availability limit."""
        if required_capital <= 0.0:
            return False
        return self.available_capital >= required_capital

    def commit_capital(self, required_capital: float) -> None:
        """Commit capital from shared pool upon trade submission."""
        self.committed_capital += required_capital

    def release_capital(self, required_capital: float, pnl: float = 0.0) -> None:
        """Release committed capital back to shared pool upon trade exit."""
        self.committed_capital = max(0.0, self.committed_capital - required_capital)
        self.realized_pnl += pnl

    @property
    def active_trades(self) -> Dict[str, TradeState]:
        res: Dict[str, TradeState] = {}
        for st in self.sub_tracks:
            res.update(st.active_trades)
        return res

    @property
    def closed_trades(self) -> List[TradeState]:
        res: List[TradeState] = []
        for st in self.sub_tracks:
            res.extend(st.closed_trades)
        return res

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        for st in self.sub_tracks:
            s = st.select_strike(opp)
            if s:
                return s
        return None

    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        return None

    def on_opportunity(self, opp: ActionableOpportunity) -> Optional[TradeState]:
        """
        Handle incoming setup opportunity and delegate across sub-tracks.
        Sub-tracks evaluate entry against capital availability limits.
        Fair rotation: Whichever track accepts the trade rotates to the bottom of the
        ranking list. Tracks that rejected due to illiquid strikes or spread gates retain
        their top priority for future opportunities.
        """
        if self.context.is_frozen():
            return None

        for idx, st in enumerate(self.sub_tracks):
            tr = st.on_opportunity(opp)
            if tr:
                # Rotate the accepting track to the bottom of the priority queue
                if len(self.sub_tracks) > 1:
                    accepting = self.sub_tracks.pop(idx)
                    self.sub_tracks.append(accepting)
                return tr
        return None

    def on_tick(self, token: str, ltp: float, bid: float, ask: float, ts: float, **kwargs) -> None:
        """Route depth tick to all constituent sub-tracks."""
        for st in self.sub_tracks:
            st.on_tick(token=token, ltp=ltp, bid=bid, ask=ask, ts=ts, **kwargs)

    def on_heartbeat(self, ts: float) -> None:
        """Route heartbeat to all constituent sub-tracks."""
        for st in self.sub_tracks:
            if hasattr(st, "on_heartbeat"):
                st.on_heartbeat(ts)

    def on_order_result(self, result: OrderResult) -> None:
        """Route order result to all constituent sub-tracks."""
        for st in self.sub_tracks:
            st.on_order_result(result)

    def exit_trade(self, trade: TradeState, exit_price: float, reason: str, ts: float) -> None:
        """Route trade exit to the sub-track that owns this active trade."""
        for st in self.sub_tracks:
            if trade.trade_id in st.active_trades:
                st.exit_trade(trade, exit_price=exit_price, reason=reason, ts=ts)
                return

    def add_recovered_trade(self, trade: TradeState, is_filled: bool = True) -> None:
        """Route recovered trade from journal (--continue) into its originating sub-track."""
        if not self.sub_tracks:
            return
        origin = trade.origin_track or (trade.metadata or {}).get("sub_track") or (trade.metadata or {}).get("origin_track")
        target_st = None
        if origin:
            for st in self.sub_tracks:
                if st.track_name == origin:
                    target_st = st
                    break
        if target_st is None:
            target_st = self.sub_tracks[0]
        target_st.add_recovered_trade(trade, is_filled=is_filled)
