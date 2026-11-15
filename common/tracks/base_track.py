"""
Base Track Interface & Common Lifecycle Engine.
Enforces Invariant #2 (Strict Ask-based target and SL computations)
and provides isolated, track-specific exit rules dispatching.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import time

from common.interfaces import ExecutionContext, TradeState
from core.common.ipc_schemas import ActionableOpportunity, OrderRequest, OrderResult


class BaseTrack(ABC):
    def __init__(
        self,
        track_name: str,
        context: ExecutionContext,
        target_pct: float,
        sl_pct: float,
        max_trades: Optional[int] = None,
        preferred_mode: Optional[str] = None,
        parent_track: Optional[str] = None,
        allocated_capital: Optional[float] = None,
        **kwargs,
    ):
        self.track_name = track_name
        self.context = context
        self.target_pct = target_pct
        self.sl_pct = sl_pct
        self.max_trades = max_trades
        self.preferred_mode = preferred_mode.upper() if preferred_mode else None
        self.parent_track = parent_track

        # Capital Availability Management (replaces arbitrary max_trades limits)
        self.allocated_capital: float = float(allocated_capital or kwargs.get("capital") or 250000.0)
        self.committed_capital: float = 0.0
        self.realized_pnl: float = 0.0
        self.parent_composite: Optional[Any] = None

        self.active_trades: Dict[str, TradeState] = {}
        self.closed_trades: List[TradeState] = []
        self._pending_orders: Dict[str, str] = {}  # order_id -> trade_id

    @property
    def available_capital(self) -> float:
        if self.parent_composite is not None:
            return self.parent_composite.available_capital
        net_worth = self.allocated_capital + self.realized_pnl
        return max(0.0, net_worth - self.committed_capital)

    def can_allocate(self, required_capital: float) -> bool:
        if self.parent_composite is not None:
            return self.parent_composite.can_allocate(required_capital)
        if required_capital <= 0.0:
            return False
        return self.available_capital >= required_capital

    def commit_capital(self, required_capital: float) -> None:
        if self.parent_composite is not None:
            self.parent_composite.commit_capital(required_capital)
        else:
            self.committed_capital += required_capital

    def release_capital(self, required_capital: float, pnl: float = 0.0) -> None:
        if self.parent_composite is not None:
            self.parent_composite.release_capital(required_capital, pnl)
        else:
            self.committed_capital = max(0.0, self.committed_capital - required_capital)
            self.realized_pnl += pnl

    def select_strike(self, opp: ActionableOpportunity) -> Optional[Dict[str, Any]]:
        """Select preferred strike contract from opportunity strikes dict."""
        if self.preferred_mode:
            m = self.preferred_mode
            if m == "ATM":
                return opp.strikes.get("ATM")
            elif m in ("ITM", "ITM1"):
                return opp.strikes.get("ITM1") or opp.strikes.get("ITM")
            elif m in ("OTM", "OTM1"):
                return opp.strikes.get("OTM1") or opp.strikes.get("OTM")
            return opp.strikes.get(m)
        return None

    @abstractmethod
    def evaluate_exit_rules(
        self, trade: TradeState, ltp: float, bid: float, ask: float, ts: float
    ) -> Optional[str]:
        """Track-specific exit rules and trailing ratchets. Returns exit reason or None."""
        pass

    def on_opportunity(self, opp: ActionableOpportunity) -> Optional[TradeState]:
        """Handle incoming setup opportunity and initiate entry order."""
        # Risk Gate 1: Check in-memory freeze flag
        if self.context.is_frozen():
            return None

        strike_info = self.select_strike(opp)
        if not strike_info:
            return None

        token = strike_info["token"]
        ask = float(strike_info["ask"])
        bid = float(strike_info["bid"])
        lot_size = int(strike_info.get("lot_size", 1))
        quantity = lot_size

        if ask <= 0.0 or quantity <= 0:
            return None

        required_capital = ask * quantity

        # Risk Gate 2: Capital availability limit (no arbitrary max_trades limits)
        if not self.can_allocate(required_capital):
            return None

        # INVARIANT #2: Strict Ask-based entry, target, and SL computations
        entry_ask = ask
        target_price = entry_ask * (1.0 + self.target_pct)
        stop_loss = entry_ask * (1.0 - self.sl_pct)

        effective_track_name = self.parent_track or self.track_name
        trade_id = f"TRD_{effective_track_name}_{opp.symbol}_{int(time.time()*1000)}"
        order_id = f"ORD_{trade_id}"

        now_ts = opp.timestamp or time.time()
        trade = TradeState(
            trade_id=trade_id,
            mode=self.context.mode,
            track=effective_track_name,
            symbol=opp.symbol,
            token=token,
            option_type=strike_info.get("option_type", "CE"),
            strike=float(strike_info.get("strike", 0.0)),
            lot_size=lot_size,
            quantity=quantity,
            entry_ask=entry_ask,
            entry_price=entry_ask,  # Will update upon actual fill
            entry_time=now_ts,
            target_price=target_price,
            stop_loss=stop_loss,
            highest_price=entry_ask,
            current_bid=bid,
            status="OPEN",
            metadata={
                "opp_id": opp.opp_id,
                "archetype": opp.archetype,
                "sub_track": self.track_name,
                "preferred_mode": self.preferred_mode,
                "required_capital": required_capital,
            },
        )

        self.commit_capital(required_capital)
        self.active_trades[trade_id] = trade
        self._pending_orders[order_id] = trade_id

        # Subscribe to QuotesBroker for live depth streaming
        self.context.subscribe_quotes([token])

        # Submit entry BUY order
        req = OrderRequest(
            order_id=order_id,
            mode=self.context.mode,
            track=effective_track_name,
            instrument_token=token,
            symbol=opp.symbol,
            transaction_type="BUY",
            order_type="MARKET",
            quantity=quantity,
            price=entry_ask,
            trigger_price=0.0,
            timestamp=now_ts,
        )
        self.context.submit_order(req)
        self.context.record_trade_event("TRADE_ENTRY_SUBMITTED", trade.to_dict())
        return trade

    def on_tick(self, token: str, ltp: float, bid: float, ask: float, ts: float) -> None:
        """Process streaming market depth tick for active trades."""
        trades_to_eval = [t for t in self.active_trades.values() if t.token == token and t.status == "OPEN"]
        for trade in trades_to_eval:
            trade.current_bid = bid
            trade.highest_price = max(trade.highest_price, bid)

            # Evaluate track-specific exit rules
            reason = self.evaluate_exit_rules(trade, ltp, bid, ask, ts)
            if reason:
                self.exit_trade(trade, exit_price=bid, reason=reason, ts=ts)

    def exit_trade(self, trade: TradeState, exit_price: float, reason: str, ts: float) -> None:
        """Initiate trade exit."""
        trade.status = "CLOSED"
        trade.exit_price = exit_price
        trade.exit_time = ts
        trade.exit_reason = reason
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity

        order_id = f"EXIT_{trade.trade_id}"
        req = OrderRequest(
            order_id=order_id,
            mode=self.context.mode,
            track=trade.track,
            instrument_token=trade.token,
            symbol=trade.symbol,
            transaction_type="SELL",
            order_type="MARKET",
            quantity=trade.quantity,
            price=exit_price,
            trigger_price=0.0,
            timestamp=ts,
        )
        self.context.submit_order(req)
        self.context.record_trade_event("TRADE_EXIT", trade.to_dict())

        # Unsubscribe from live quotes stream
        self.context.unsubscribe_quotes([trade.token])

        # Release committed capital back to pool with realized PnL
        committed = trade.entry_ask * trade.quantity
        self.release_capital(committed, trade.realized_pnl)

        # Move to closed trades
        self.active_trades.pop(trade.trade_id, None)
        self.closed_trades.append(trade)

    def on_order_result(self, result: OrderResult) -> None:
        """Handle order execution confirmation."""
        trade_id = self._pending_orders.get(result.order_id)
        if trade_id and trade_id in self.active_trades:
            trade = self.active_trades[trade_id]
            if result.status == "FILLED":
                trade.entry_price = result.fill_price
                trade.entry_time = result.fill_timestamp
                # Re-calculate target and SL strictly using executable Ask / fill
                trade.target_price = result.fill_price * (1.0 + self.target_pct)
                trade.stop_loss = result.fill_price * (1.0 - self.sl_pct)
                self.context.record_trade_event("TRADE_FILLED", trade.to_dict())
            elif result.status == "REJECTED":
                trade.status = "REJECTED"
                committed = trade.entry_ask * trade.quantity
                self.release_capital(committed, 0.0)
                self.active_trades.pop(trade_id, None)
                self.context.record_trade_event("TRADE_REJECTED", trade.to_dict())
