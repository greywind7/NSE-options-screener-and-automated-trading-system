#!/usr/bin/env python3
"""
execution/paper/paper_delayed_adapter.py
========================================
Paper Execution Adapter with Simulated Network and Ingestion Latency:
Introduces a realistic random delay (default: 700ms to 2,500ms) before order execution,
re-sampling market quotes from the live `quotes_cache` at the moment of delayed fill.

Use Case:
1. Simulates realistic transmission, broker API gateway ingestion, and matching engine delay.
2. Captures real-world price slippage and adverse selection on high-velocity breakouts.
3. Provides side-by-side comparison in the strategy dashboard between instantaneous
   paper trading, delayed paper trading, official sandbox, and live execution.
"""

from __future__ import annotations

import csv
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from execution.base import BaseExecutionAdapter
from infrastructure.common.time_utils import is_quote_fresh, normalize_time_str


class PaperDelayedExecutionAdapter(BaseExecutionAdapter):
    """
    Paper trading execution adapter that introduces a realistic 700ms - 2.5s latency
    and executes orders at the dynamically updated quote after the delay.
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        brokerage_per_order: float = 30.0,
        output_csv: str = "logs/paper_delayed_trades.csv",
        min_delay_ms: float = 200,
        max_delay_ms: float = 500,
        quotes_cache: Optional[Dict[str, Any]] = None,
        async_execution: bool = True,
        enforce_market_hours: bool = False,
        market_open_time: str = "09:15",
        market_close_time: str = "15:30",
        eod_freeze_time: str = "15:15",
        eod_square_off_time: str = "15:20",
    ):
        session_date = datetime.now().strftime("%Y%m%d")
        if output_csv is None or output_csv == "logs/paper_delayed_trades.csv":
            output_csv = os.path.join("recordings", session_date, "paper_delayed_trades.csv")
        elif "{date}" in output_csv:
            output_csv = output_csv.replace("{date}", session_date)

        is_default_workspace_path = (
            output_csv.startswith("logs/")
            or output_csv.startswith("recordings/")
            or output_csv in ("logs/paper_trades.csv", "logs/paper_delayed_trades.csv", "logs/live_trades.csv")
        )
        if is_default_workspace_path and (os.environ.get("TRADING_TEST_MODE") == "1" or "unittest" in sys.modules):
            import tempfile, atexit
            output_csv = os.path.join(tempfile.gettempdir(), f"test_paper_delayed_trades_{os.getpid()}.csv")
            try:
                atexit.register(lambda: os.path.exists(output_csv) and os.remove(output_csv))
            except Exception:
                pass
        else:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
            except Exception:
                pass

        self.initial_capital = float(initial_capital)
        self.available_capital = float(initial_capital)
        self.brokerage_per_order = float(brokerage_per_order)
        self.output_csv = output_csv
        self.min_delay_ms = float(min_delay_ms)
        self.max_delay_ms = float(max_delay_ms)
        self.quotes_cache: Optional[Dict[str, Any]] = quotes_cache
        self.async_execution = async_execution
        self.enforce_market_hours = enforce_market_hours
        self.market_open_time = normalize_time_str(market_open_time, "09:15")
        self.market_close_time = normalize_time_str(market_close_time, "15:30")
        self.eod_freeze_time = normalize_time_str(eod_freeze_time, "15:15")
        self.eod_square_off_time = normalize_time_str(eod_square_off_time, "15:20")

        self.orders: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self._order_counter = 0
        self._lock = threading.Lock()
        self._active_timers: List[threading.Timer] = []

        self._init_csv()

    def _init_csv(self) -> None:
        """Initialize paper-delayed trades CSV journal."""
        os.makedirs(os.path.dirname(os.path.abspath(self.output_csv)), exist_ok=True)
        if not os.path.exists(self.output_csv):
            with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "order_id", "symbol", "instrument_token", "direction",
                    "quantity", "entry_price", "exit_price", "gross_pnl",
                    "brokerage", "net_pnl", "entry_time", "exit_time",
                    "reason", "delay_ms"
                ])

    def set_quotes_cache(self, quotes_cache: Dict[str, Any]) -> None:
        """Bind shared real-time quotes cache."""
        self.quotes_cache = quotes_cache

    def place_order(
        self,
        symbol: str,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        product: str = "I",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: Optional[str] = None,
        market_quote: Optional[Dict[str, Any]] = None,
        stop_loss_price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Submit order with simulated 200ms - 1.5s latency.
        Executes at the freshest quote sampled after the latency window.
        """
        trans_type = transaction_type.upper()
        now_ts = time.time()

        # Market Hours & Call Auction Session (CAS) Guard
        if getattr(self, "enforce_market_hours", False):
            ist = timezone(timedelta(hours=5, minutes=30))
            dt_ist = datetime.fromtimestamp(now_ts, tz=ist)
            if dt_ist.year >= 2020:
                is_entry = trans_type in ("BUY", "ENTRY")
                cur_hm = dt_ist.strftime("%H:%M")

                if dt_ist.weekday() >= 5 and tag != "EOD_MIS_SQUAREOFF":
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "status": "REJECTED_MARKET_CLOSED",
                        "error": f"Market closed (Weekend: {dt_ist.strftime('%A')}). Paper-delayed orders blocked.",
                    }

                if cur_hm < self.market_open_time:
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "status": "REJECTED_PRE_MARKET_CAS",
                        "error": f"Pre-market Call Auction Session (CAS) active until {self.market_open_time} IST. Paper-delayed orders blocked.",
                    }

                if cur_hm >= self.market_close_time and tag != "EOD_MIS_SQUAREOFF":
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "status": "REJECTED_MARKET_CLOSED",
                        "error": f"Market closed at {self.market_close_time} IST. Paper-delayed orders blocked.",
                    }

                if is_entry and cur_hm >= self.eod_freeze_time:
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "status": "REJECTED_EOD_FREEZE",
                        "error": f"Intraday entry cutoff {self.eod_freeze_time} IST reached. Paper-delayed entry orders blocked.",
                    }

        with self._lock:
            self._order_counter += 1
            order_id = f"P-DLY-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self._order_counter:04d}"

        # Sample random delay
        delay_ms = random.uniform(self.min_delay_ms, self.max_delay_ms)
        delay_sec = delay_ms / 1000.0

        order_record = {
            "order_id": order_id,
            "symbol": symbol,
            "instrument_token": instrument_token,
            "quantity": quantity,
            "transaction_type": trans_type,
            "order_type": order_type,
            "product": product,
            "price": price,
            "trigger_price": trigger_price,
            "tag": tag,
            "initial_quote": dict(market_quote) if market_quote else {},
            "submitted_at": now_ts,
            "target_fill_at": now_ts + delay_sec,
            "delay_ms": round(delay_ms, 1),
            "status": "OPEN" if self.async_execution else "FILLED",
            "api_status": "delayed_in_flight" if self.async_execution else "delayed_fill",
        }

        with self._lock:
            self.orders[order_id] = order_record

        if self.async_execution:
            # Asynchronous background execution: does not block the strategy engine!
            timer = threading.Timer(
                delay_sec,
                self._execute_fill,
                args=(order_id, symbol, instrument_token, quantity, trans_type, order_type, price, tag, market_quote, delay_ms),
            )
            timer.daemon = True
            with self._lock:
                self._active_timers.append(timer)
            timer.start()
            return order_record
        else:
            # Synchronous execution (useful for unit tests or deterministic backtests)
            if delay_sec > 0:
                time.sleep(delay_sec)
            return self._execute_fill(
                order_id=order_id,
                symbol=symbol,
                instrument_token=instrument_token,
                quantity=quantity,
                trans_type=trans_type,
                order_type=order_type,
                price=price,
                tag=tag,
                initial_quote=market_quote,
                delay_ms=delay_ms,
            )

    def _resolve_quote_from_cache(
        self,
        symbol: str,
        instrument_token: str,
        contract_symbol: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve freshest real quote from quotes_cache with full key normalization.
        Never falls back to underlying equity symbol for option contracts!
        """
        if not self.quotes_cache:
            return None
        # 1. Exact match
        q = self.quotes_cache.get(instrument_token)
        if q:
            return q
        # 2. Key format variants (: vs |)
        q = self.quotes_cache.get(instrument_token.replace(":", "|")) or self.quotes_cache.get(instrument_token.replace("|", ":"))
        if q:
            return q
        # 3. Contract human-readable symbol (e.g. HDFCAMC 2600 PE)
        if contract_symbol:
            q = (
                self.quotes_cache.get(contract_symbol)
                or self.quotes_cache.get(contract_symbol.replace(":", "|"))
                or self.quotes_cache.get(contract_symbol.replace("|", ":"))
            )
            if q:
                return q
        # 4. Token suffix (if token is e.g. NSE_FO|CONTRACT)
        suffix = instrument_token.split("|")[-1].split(":")[-1]
        if suffix and suffix != instrument_token:
            q = self.quotes_cache.get(suffix)
            if q:
                return q
        # 5. Equity cash symbol ONLY if symbol == instrument_token (never for option contracts!)
        if symbol == instrument_token:
            q = self.quotes_cache.get(symbol)
            if q:
                return q
        return None

    def _execute_fill(
        self,
        order_id: str,
        symbol: str,
        instrument_token: str,
        quantity: int,
        trans_type: str,
        order_type: str,
        price: float,
        tag: Optional[str],
        initial_quote: Optional[Dict[str, Any]],
        delay_ms: float,
    ) -> Dict[str, Any]:
        """
        Executes order after delay has elapsed, resolving the latest quote from quotes_cache.
        Uses retry polling against quotes_cache to ensure fresh websocket ticks are captured.
        NEVER falls back to initial_quote or synthetic estimates when quotes_cache is active!
        For SELL liquidation orders: preserves position in portfolio if no quote is available.
        """
        now_ts = time.time()

        # 1. Look up position metadata in advance if this is a SELL
        contract_sym = ""
        pos_key = f"{symbol}_{instrument_token}"
        if trans_type == "SELL":
            with self._lock:
                pos_info = self.positions.get(pos_key)
                if not pos_info:
                    for p in self.positions.values():
                        if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                            pos_info = p
                            break
                if pos_info:
                    contract_sym = pos_info.get("contract_symbol", "")

        # 2. Resolve freshest quote after latency delay
        q = None
        max_retries = 5
        retry_interval_sec = 0.15

        if self.quotes_cache is not None:
            # Poll quotes_cache with retries to wait for next incoming websocket tick
            for attempt in range(max_retries):
                candidate = self._resolve_quote_from_cache(
                    symbol=symbol,
                    instrument_token=instrument_token,
                    contract_symbol=contract_sym or (tag or ""),
                )
                if candidate:
                    cand_bid = float(candidate.get("bid") or 0.0)
                    cand_ask = float(candidate.get("ask") or 0.0)
                    cand_ltp = float(candidate.get("last_price") or candidate.get("ltp") or 0.0)
                    if trans_type == "BUY" and (cand_ask > 0 or cand_ltp > 0):
                        q = candidate
                        break
                    elif trans_type == "SELL" and (cand_bid > 0 or cand_ltp > 0):
                        q = candidate
                        break
                if attempt < max_retries - 1:
                    time.sleep(retry_interval_sec)
            # NEVER fall back to initial_quote when quotes_cache is active!
        else:
            # Standalone offline execution / unit tests without live quotes cache
            q = initial_quote or {}

        q = q or {}
        bid = float(q.get("bid") or 0.0)
        ask = float(q.get("ask") or 0.0)
        ltp = float(q.get("last_price") or q.get("ltp") or 0.0)
        ref_price = float(price) if price and float(price) > 0 else 0.0

        if trans_type == "BUY":
            is_fresh, quote_age = is_quote_fresh(q, now_ts=now_ts, max_age_sec=30.0)
            if not is_fresh:
                with self._lock:
                    if order_id in self.orders:
                        self.orders[order_id]["status"] = "REJECTED_STALE_QUOTE"
                        self.orders[order_id]["error"] = f"Quote is stale ({quote_age:.1f}s > 30.0s threshold)"
                return {
                    "order_id": order_id,
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "transaction_type": trans_type,
                    "status": "REJECTED_STALE_QUOTE",
                    "error": f"Market quote for BUY {instrument_token} is stale ({quote_age:.1f}s > 30.0s threshold). Order rejected.",
                    "timestamp": datetime.now().isoformat(),
                }
            fill_price = ask if ask > 0 else (ltp if ltp > 0 else ref_price)
        else:
            fill_price = bid if bid > 0 else (ltp if ltp > 0 else ref_price)

        if fill_price <= 0:
            with self._lock:
                if order_id in self.orders:
                    self.orders[order_id]["status"] = "REJECTED_NO_QUOTE"
                    if trans_type == "SELL":
                        err_msg = (
                            f"CRITICAL: No valid bid quote in quotes_cache for SELL {instrument_token} "
                            f"(Bid={bid}, LTP={ltp}) after {max_retries if self.quotes_cache is not None else 0} retries. "
                            f"Order rejected; position kept OPEN in portfolio to prevent unmanaged drops."
                        )
                        print(f"\n🚨 [EXECUTION ERROR] {err_msg}\n")
                    else:
                        err_msg = (
                            f"No valid ask quote in quotes_cache for BUY {instrument_token} "
                            f"(Ask={ask}, LTP={ltp}) after retries. Buy order rejected."
                        )
                    self.orders[order_id]["error"] = err_msg
            return self.orders.get(order_id, {"status": "REJECTED_NO_QUOTE"})

        fill_price = round(fill_price, 2)
        trade_cost = fill_price * quantity
        trading_sym = contract_sym or (instrument_token.split("|")[-1] if "|" in instrument_token else symbol)

        with self._lock:
            if trans_type == "BUY":
                self.available_capital -= trade_cost
                self.positions[pos_key] = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "contract_symbol": trading_sym,
                    "quantity": quantity,
                    "entry_price": fill_price,
                    "entry_time": now_ts,
                    "cost": trade_cost,
                    "tag": tag or "PAPER_DELAYED",
                    "delay_ms": delay_ms,
                }
            elif trans_type == "SELL":
                pos = self.positions.pop(pos_key, None)
                if not pos:
                    for k, p in list(self.positions.items()):
                        if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                            pos = self.positions.pop(k)
                            break
                if pos:
                    ep = float(pos.get("entry_price", 0.0))
                    gross = (fill_price - ep) * quantity
                    round_trip_brokerage = self.brokerage_per_order * 2.0
                    net = gross - round_trip_brokerage
                    self.available_capital += (pos.get("cost", trade_cost) + net)

                    closed_rec = {
                        "order_id": order_id,
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "direction": "BULL" if "CE" in trading_sym else "BEAR",
                        "quantity": quantity,
                        "entry_price": ep,
                        "exit_price": fill_price,
                        "gross_pnl": gross,
                        "brokerage": round_trip_brokerage,
                        "net_pnl": net,
                        "entry_time": pos.get("entry_time", now_ts),
                        "exit_time": now_ts,
                        "reason": tag or "DELAYED_EXIT",
                        "delay_ms": delay_ms,
                    }
                    self.closed_trades.append(closed_rec)
                    self._log_trade_csv(closed_rec)

            if order_id in self.orders:
                self.orders[order_id]["status"] = "FILLED"
                self.orders[order_id]["api_status"] = "delayed_fill"
                self.orders[order_id]["fill_price"] = fill_price
                self.orders[order_id]["filled_at"] = now_ts
                self.orders[order_id]["delay_ms"] = delay_ms

        # Record in PnLAggregator and ProtectedPositionRegistry
        try:
            from infrastructure.pnl.pnl_aggregator import get_global_pnl_aggregator
            if trans_type == "BUY":
                get_global_pnl_aggregator().record_trade_open(
                    trade_id=order_id,
                    strategy="momentum",
                    track=tag or "PAPER_DELAYED",
                    mode="paper_delayed",
                    symbol=symbol,
                    contract_key=instrument_token,
                    contract_symbol=trading_sym,
                    direction="BULL" if "CE" in trading_sym else "BEAR",
                    quantity=quantity,
                    entry_price=fill_price,
                    entry_time=now_ts,
                    brokerage=self.brokerage_per_order,
                )
            elif trans_type == "SELL" and pos:
                get_global_pnl_aggregator().record_trade_exit(
                    trade_id=order_id,
                    exit_price=fill_price,
                    exit_time=now_ts,
                    exit_reason=tag or "DELAYED_EXIT",
                    strategy="momentum",
                    track=tag or "PAPER_DELAYED",
                    mode="paper_delayed",
                    symbol=symbol,
                    contract_key=instrument_token,
                    contract_symbol=trading_sym,
                    direction="BULL" if "CE" in trading_sym else "BEAR",
                    quantity=quantity,
                    entry_price=pos.get("entry_price", fill_price),
                    entry_time=pos.get("entry_time", now_ts),
                    brokerage=self.brokerage_per_order * 2.0,
                )
        except Exception:
            pass

        return self.orders.get(order_id, {"status": "FILLED", "fill_price": fill_price})

    def modify_order(
        self,
        order_id: str,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        order_type: str = "LIMIT",
        validity: str = "DAY",
        disclosed_quantity: int = 0,
    ) -> Dict[str, Any]:
        with self._lock:
            if order_id not in self.orders:
                return {"status": "error", "message": f"Order {order_id} not found"}
            ord_rec = self.orders[order_id]
            if quantity is not None:
                ord_rec["quantity"] = int(quantity)
            if price is not None:
                ord_rec["price"] = float(price)
            if trigger_price is not None:
                ord_rec["trigger_price"] = float(trigger_price)
            ord_rec["order_type"] = order_type.upper()
            ord_rec["validity"] = validity.upper()
            ord_rec["modified_at"] = time.time()
            return {"status": "success", "order_id": order_id, "order": ord_rec}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        with self._lock:
            if order_id not in self.orders:
                return {"status": "error", "message": f"Order {order_id} not found"}
            self.orders[order_id]["status"] = "CANCELLED"
            self.orders[order_id]["cancelled_at"] = time.time()
            return {"status": "success", "order_id": order_id}

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        with self._lock:
            return self.orders.get(order_id, {"status": "NOT_FOUND"})

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.positions.values())

    def update_stop_loss(
        self,
        symbol: str,
        instrument_token: str,
        new_stop_loss_price: float,
    ) -> Dict[str, Any]:
        """Update trailing stop-loss price on active position."""
        pos_key = f"{symbol}_{instrument_token}"
        pos = self.positions.get(pos_key)
        if not pos:
            for k, p in self.positions.items():
                if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                    pos = p
                    break
        if pos:
            pos["current_sl"] = float(new_stop_loss_price)
            return {"status": "SUCCESS", "current_sl": float(new_stop_loss_price)}
        return {"status": "NOT_FOUND"}

    def get_portfolio_summary(self, quotes_cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            wins = sum(1 for t in self.closed_trades if t.get("net_pnl", 0.0) > 0)
            losses = len(self.closed_trades) - wins
            tot = len(self.closed_trades)
            net_realized = sum(t.get("net_pnl", 0.0) for t in self.closed_trades)
            mtm = 0.0
            if quotes_cache:
                for p in self.positions.values():
                    q = quotes_cache.get(p.get("instrument_token"), {})
                    bid = float(q.get("bid") or q.get("last_price") or q.get("ltp") or p.get("price", 0.0))
                    mtm += (bid - p.get("price", 0.0)) * p.get("quantity", 0)
            total_net_pnl = net_realized + mtm
            wr = (wins / tot * 100.0) if tot > 0 else 0.0
            roi = (total_net_pnl / self.initial_capital * 100.0) if self.initial_capital > 0 else 0.0

            return {
                "initial_capital": self.initial_capital,
                "available_capital": self.available_capital,
                "open_positions": len(self.positions),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wr, 1),
                "net_realized": round(net_realized, 2),
                "open_mtm": round(mtm, 2),
                "total_net_pnl": round(total_net_pnl, 2),
                "net_roi": round(roi, 2),
                "min_delay_ms": self.min_delay_ms,
                "max_delay_ms": self.max_delay_ms,
            }

    def _log_trade_csv(self, trade: Dict[str, Any]) -> None:
        """Log trade to CSV."""
        try:
            with open(self.output_csv, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    trade.get("order_id"),
                    trade.get("symbol"),
                    trade.get("instrument_token"),
                    trade.get("direction"),
                    trade.get("quantity"),
                    trade.get("entry_price"),
                    trade.get("exit_price"),
                    round(trade.get("gross_pnl", 0.0), 2),
                    round(trade.get("brokerage", 0.0), 2),
                    round(trade.get("net_pnl", 0.0), 2),
                    datetime.fromtimestamp(trade.get("entry_time", time.time())).strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.fromtimestamp(trade.get("exit_time", time.time())).strftime("%Y-%m-%d %H:%M:%S"),
                    trade.get("reason"),
                    round(trade.get("delay_ms", 0.0), 1),
                ])
        except Exception:
            pass

    def check_eod_squareoff(
        self,
        now_ts: Optional[float] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        eod_time: str = "15:20",
        freeze_time: str = "15:15",
    ) -> List[Dict[str, Any]]:
        """
        End-of-Day RMS Liquidation Guard for Paper-Delayed Execution:
        - At or after freeze_time: Cancels active delay timers and pending OPEN orders.
        - At or after eod_time: Squares off all open delayed paper positions at market Bid.
        """
        ts = now_ts or time.time()
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime.fromtimestamp(ts, tz=ist)
        if dt_ist.year < 2020:
            return []

        cur_hm = dt_ist.strftime("%H:%M")

        # 1. At freeze_time: Cancel active delay timers and pending OPEN orders
        if cur_hm >= freeze_time or cur_hm >= eod_time:
            with self._lock:
                for timer in self._active_timers:
                    try:
                        timer.cancel()
                    except Exception:
                        pass
                self._active_timers.clear()

                for oid, ord_rec in list(self.orders.items()):
                    if ord_rec.get("status") == "OPEN":
                        ord_rec["status"] = "CANCELLED"
                        ord_rec["api_status"] = "cancelled_eod_freeze"

        # 2. At eod_time: Square off all open positions immediately (synchronously)
        if cur_hm < eod_time:
            return []

        exited = []
        with self._lock:
            pos_items = list(self.positions.items())

        for pos_key, pos in pos_items:
            sym = pos.get("symbol", "")
            token = pos.get("instrument_token", "")
            qty = int(pos.get("quantity", 1))
            q = (quotes_cache or self.quotes_cache or {}).get(token) or (quotes_cache or self.quotes_cache or {}).get(sym) or {}

            # Execute synchronous market sell to avoid timer delay during EOD liquidation
            prev_async = self.async_execution
            self.async_execution = False
            try:
                res = self.place_order(
                    symbol=sym,
                    instrument_token=token,
                    quantity=qty,
                    transaction_type="SELL",
                    order_type="MARKET",
                    tag="EOD_MIS_SQUAREOFF",
                    market_quote=q,
                )
                exited.append(res)
                print(f"⏰ [EOD GUARD] {eod_time} IST Auto-Squareoff: Closed {sym} ({token}) on paper-delayed execution.")
            finally:
                self.async_execution = prev_async

        return exited

    def shutdown(self) -> None:
        """Cancel any active timers on shutdown."""
        with self._lock:
            for timer in self._active_timers:
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._active_timers.clear()

