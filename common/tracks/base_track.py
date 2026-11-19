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
from core.common.symbol_utils import normalize_instrument_token


def _parse_time_to_minutes(time_val: Any, default_minutes: int) -> int:
    """Parse 'HH:MM' string or minutes int into minutes from midnight (0..1440)."""
    if time_val is None:
        return default_minutes
    if isinstance(time_val, (int, float)):
        return int(time_val)
    s = str(time_val).strip()
    if not s:
        return default_minutes
    try:
        parts = s.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return h * 60 + m
    except (ValueError, TypeError):
        return default_minutes


class BaseTrack(ABC):
    def __init__(
        self,
        track_name: str,
        context: ExecutionContext,
        target_pct: float,
        sl_pct: float,
        breakeven_trigger_pct: Optional[float] = 0.025,
        max_trades: Optional[int] = None,
        preferred_mode: Optional[str] = None,
        parent_track: Optional[str] = None,
        allocated_capital: Optional[float] = None,
        entry_freeze_time: Optional[str] = None,
        eod_squareoff_time: Optional[str] = None,
        **kwargs,
    ):
        self.track_name = track_name
        self.context = context
        self.target_pct = target_pct
        self.sl_pct = sl_pct
        be_pct = breakeven_trigger_pct if breakeven_trigger_pct is not None else kwargs.get("breakeven_trigger_pct", 0.025)
        self.breakeven_trigger_pct: float = float(be_pct) if be_pct is not None else 0.025
        if self.breakeven_trigger_pct > 1.0:
            self.breakeven_trigger_pct /= 100.0
        self.max_trades = max_trades
        self.preferred_mode = preferred_mode.upper() if preferred_mode else None
        self.parent_track = parent_track

        # Configurable entry freeze and EOD auto-squareoff times (IST)
        self.entry_freeze_time = str(entry_freeze_time or kwargs.get("entry_freeze_time") or "15:00")
        self.eod_squareoff_time = str(eod_squareoff_time or kwargs.get("eod_squareoff_time") or "15:15")
        self.entry_freeze_minutes = _parse_time_to_minutes(self.entry_freeze_time, 15 * 60)
        self.eod_squareoff_minutes = _parse_time_to_minutes(self.eod_squareoff_time, 15 * 60 + 15)

        # Brokerage per order (BUY & SELL legs = 2x brokerage_per_order round trip)
        brok = kwargs.get("brokerage_per_order")
        self.brokerage_per_order: float = float(brok) if brok is not None else 30.0

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
        # Risk Gate 1a: Check in-memory freeze flag
        if self.context.is_frozen():
            return None

        # Risk Gate 1b: Hard Entry Freeze Cutoff (e.g. 15:00 IST)
        now_ts = opp.timestamp or time.time()
        from datetime import datetime
        from core.common.time_utils import IST
        dt = datetime.fromtimestamp(now_ts, tz=IST)
        curr_minutes = dt.hour * 60 + dt.minute
        if curr_minutes >= self.entry_freeze_minutes:
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

        # Risk Gate 3: Stop-Loss Solvency Invariant
        # Prevailing market bid must be strictly above the calculated stop loss
        if bid <= stop_loss:
            return None

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

    def on_tick(
        self,
        token: str,
        ltp: float,
        bid: float,
        ask: float,
        ts: float,
        topic_token: str = "",
        symbol: str = "",
    ) -> None:
        """Process streaming market depth tick for active trades."""
        norm_token = normalize_instrument_token(token)
        norm_topic = normalize_instrument_token(topic_token) if topic_token else ""
        norm_sym = normalize_instrument_token(symbol) if symbol else ""

        # Match trades by token, topic token, symbol, or normalized contract attributes
        trades_to_eval = []
        for t in self.active_trades.values():
            if t.status != "OPEN":
                continue
            if self.context.mode == "paper_delayed" and t.trade_id in self._pending_orders.values():
                continue
            if t.token == token or normalize_instrument_token(t.token) == norm_token:
                trades_to_eval.append(t)
                continue
            if norm_topic and normalize_instrument_token(t.token) == norm_topic:
                trades_to_eval.append(t)
                continue
            if norm_sym and normalize_instrument_token(t.token) == norm_sym:
                trades_to_eval.append(t)
                continue
            if symbol and t.symbol:
                sc = symbol.upper().replace(" ", "").replace("NSE:", "")
                suffix1 = f"{int(t.strike)}{t.option_type.upper()}"
                suffix2 = f"{t.strike:.1f}{t.option_type.upper()}"
                if sc.startswith(t.symbol.upper()) and (sc.endswith(suffix1) or sc.endswith(suffix2)):
                    trades_to_eval.append(t)
                    continue
        for trade in trades_to_eval:
            trade.current_bid = bid
            trade.highest_price = max(trade.highest_price, bid)

            # Breakeven Ratchet: if price reaches breakeven_trigger_pct, ratchet SL to entry_price (0% loss)
            if self.breakeven_trigger_pct and self.breakeven_trigger_pct > 0.0:
                profit_pct = (trade.highest_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
                if profit_pct >= self.breakeven_trigger_pct - 1e-6:
                    if trade.stop_loss < trade.entry_price:
                        trade.stop_loss = trade.entry_price
                        trade.metadata["breakeven_triggered"] = True

            # Evaluate track-specific exit rules
            reason = self.evaluate_exit_rules(trade, ltp, bid, ask, ts)
            if reason:
                self.exit_trade(trade, exit_price=bid, reason=reason, ts=ts)

    def on_heartbeat(self, ts: float) -> None:
        """Periodic heartbeat evaluation for active open trades (stagnation timeout, EOD square-off)."""
        open_trades = [
            t for t in list(self.active_trades.values())
            if t.status == "OPEN" and not (self.context.mode == "paper_delayed" and t.trade_id in self._pending_orders.values())
        ]
        for trade in open_trades:
            # 1. EOD Square-off Check (default 15:15 IST)
            from datetime import datetime
            from core.common.time_utils import IST
            dt = datetime.fromtimestamp(ts, tz=IST)
            curr_minutes = dt.hour * 60 + dt.minute
            if curr_minutes >= self.eod_squareoff_minutes:
                bid = trade.current_bid if trade.current_bid > 0 else trade.entry_price
                self.exit_trade(trade, exit_price=bid, reason="EOD_SQUAREOFF", ts=ts)
                continue

            # 2. Evaluate timeout rules if elapsed exceeds threshold
            bid = trade.current_bid if trade.current_bid > 0 else trade.entry_price
            ask = trade.entry_ask
            reason = self.evaluate_exit_rules(trade, bid, bid, ask, ts)
            if reason in ("STAGNATION_TIMEOUT", "FIXED_STAGNATION_TIMEOUT", "PULLBACK_STAGNATION_TIMEOUT", "MAX_HOLD_TIMEOUT", "PULLBACK_TIMEOUT"):
                self.exit_trade(trade, exit_price=bid, reason=reason, ts=ts)

    def exit_trade(self, trade: TradeState, exit_price: float, reason: str, ts: float) -> None:
        """Initiate trade exit."""
        trade.status = "CLOSED"
        trade.exit_price = exit_price
        trade.exit_time = ts
        trade.exit_reason = reason
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity
        round_trip_brokerage = 2.0 * getattr(self, "brokerage_per_order", 30.0)
        trade.metadata["brokerage"] = round_trip_brokerage
        trade.metadata["net_pnl"] = trade.realized_pnl - round_trip_brokerage

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
        trade_id = self._pending_orders.pop(result.order_id, None)
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
