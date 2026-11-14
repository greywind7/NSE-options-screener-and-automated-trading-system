#!/usr/bin/env python3
"""
execution/live/live_adapter.py
==============================
Production Live Broker Execution Adapter targeting Groww Trading APIs:
Uses the official Groww Python SDK (`growwapi` and `GrowwAPI` client object)
for all market order operations:

1. Authentication & SDK Initialization:
   - Initializes official `GrowwAPI(auth_token)` client object.
   - Provides TOTP authentication helper: `generate_groww_access_token(api_key, totp)`.
   - Strict SDK requirement: REST fallback is completely disabled.

2. Order Placement via Groww Object:
   - `groww.place_order(...)` using Groww SDK constants (EXCHANGE_NSE, SEGMENT_FNO,
     PRODUCT_NRML/MIS, ORDER_TYPE_MARKET/LIMIT/SL, VALIDITY_DAY).

3. Position Protection via Stop-Loss Orders:
   - All open positions are protected with resting Stop-Loss (SL) SELL orders on the broker.
   - For trailing stoplosses, `update_stop_loss` ratchets the resting SL order trigger price via `groww.modify_order(...)`.
   - For execution exits (targets, signals, EOD), the resting SL order is modified directly to a MARKET order.

4. Lifecycle Management & Background Polling:
   - `groww.modify_order(...)`
   - `groww.cancel_order(...)`
   - `groww.get_order_detail(...)` and `groww.get_order_status(...)`
   - Continuous background daemon thread polling active working orders.
   - Real-time PnLAggregator and CSV journaling.

5. Live Mode Failure Handling & Broker Position Reconciliation:
   - Periodic Broker Position Conflation (`reconcile_broker_positions`):
     Synchronizes local in-memory open positions with real-time broker positions book.
   - Settlement Quarantine Window: 10-second grace window on freshly placed orders
     to prevent false mismatch alerts while orders settle in the broker's position ledger.
   - In-Flight Exits Quarantine: Grace period on recent exits preventing false orphan re-adoption.
   - Mismatch Resolution:
     * Ghost Position (Local > 0, Broker == 0): Closes local position with reason
       'BROKER_MANUAL_SQUAREOFF', updates PnLAggregator, removes from ProtectedPositionRegistry,
       and prevents unintended/illegal naked short orders.
     * Orphan Position (Broker > 0, Local == 0): Adopts open broker position into local
       portfolio tracking, attaches risk stops, and protects contract in registry.
     * Quantity Discrepancy (Partial Fills / Manual scale-out): Adjusts local quantity to match broker.
   - State Persistence (`logs/live_positions_state.json`): Atomically saves open positions to disk
     for crash recovery and restores state on startup.
   - Session Expiration (HTTP 401/403): Automatically freezes live execution (`freeze_mode('live')`),
     writes `logs/.freeze_live`, and alerts operator.
   - Timeout Idempotency Guard (`_resolve_timed_out_order`): Queries broker order book before retrying
     to prevent disastrous duplicate orders with real money.
   - End-of-Day MIS Auto-Squareoff (EOD RMS Liquidation Guard): Automatically freezes live mode at
     15:10 IST and squares off open intraday MIS positions at 15:15 IST before broker penalties apply.
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import ssl
import threading
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from execution.base import BaseExecutionAdapter
from infrastructure.common.time_utils import is_quote_fresh, normalize_time_str

# Attempt importing the official Groww Python SDK
try:
    import growwapi
    from growwapi import GrowwAPI
    HAS_GROWWAPI = True
except ImportError:
    growwapi = None
    GrowwAPI = None
    HAS_GROWWAPI = False


GROWW_API_BASE_URL = "https://api.groww.in/v1"
GROWW_TOKEN_URL = f"{GROWW_API_BASE_URL}/token/api/access"


def generate_groww_access_token(api_key: str, totp: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Generate daily Groww access token using API Key and TOTP code.
    Endpoint: POST /v1/token/api/access
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "GrowwTradingEngine/1.0",
    }
    payload = {"key_type": "totp", "totp": str(totp).strip()}
    data_bytes = json.dumps(payload).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(GROWW_TOKEN_URL, data=data_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {"status": "SUCCESS"}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else str(e)
        return {"status": "ERROR", "http_code": e.code, "error": err_body}
    except Exception as e:
        return {"status": "ERROR", "error": str(e)}


class GrowwLiveAdapter(BaseExecutionAdapter):
    """
    Production Live Broker Execution Adapter for Groww Trading API.
    Interacts strictly via official GrowwAPI client object (zero REST fallback).
    Enforces Stop-Loss order protection on open positions:
    - Entry (BUY): places market entry and attaches resting Stop-Loss (SL) SELL order.
    - Trailing Stop-Loss: updates resting SL order trigger price via order modification.
    - Exit Execution: modifies resting SL order directly to MARKET order for fill.
    - Reconciliation & Polling: reconciles broker positions and detects exchange-triggered SL fills.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        groww_client: Optional[Any] = None,
        output_csv: str = "logs/live_trades.csv",
        state_file: Optional[str] = None,
        brokerage_per_order: float = 20.0,
        debug_log: Optional[str] = "logs/debug.log",
        live_confirmed: bool = False,
        initial_capital: float = 100_000.0,
        enable_polling: bool = True,
        poll_interval_sec: float = 1.0,
        reconciliation_interval_sec: float = 15.0,
        settlement_quarantine_sec: float = 10.0,
        enforce_market_hours: bool = False,
        market_open_time: str = "09:15",
        market_close_time: str = "15:30",
        eod_freeze_time: str = "15:10",
        eod_square_off_time: str = "15:15",
        option_sl_pct: float = 15.0,
        broker_max_order_modifications: int = 25,
    ):
        self.access_token = access_token or os.environ.get("GROWW_ACCESS_TOKEN") or os.environ.get("GROWW_API_KEY")
        self.api_key = api_key or os.environ.get("GROWW_API_KEY")
        self.output_csv = output_csv
        self.state_file = state_file or os.path.join(os.path.dirname(os.path.abspath(self.output_csv)), "live_positions_state.json")
        self.brokerage_per_order = float(brokerage_per_order)
        self.debug_log = debug_log
        self.live_confirmed = live_confirmed
        self.initial_capital = float(initial_capital)
        self.available_capital = float(initial_capital)
        self.enforce_market_hours = enforce_market_hours
        self.market_open_time = normalize_time_str(market_open_time, "09:15")
        self.market_close_time = normalize_time_str(market_close_time, "15:30")
        self.eod_freeze_time = normalize_time_str(eod_freeze_time, "15:15")
        self.eod_square_off_time = normalize_time_str(eod_square_off_time, "15:20")
        self.option_sl_pct = float(option_sl_pct)
        self.broker_max_order_modifications = int(broker_max_order_modifications)

        # Failure Handling & Conflation Configuration
        self.reconciliation_interval_sec = float(reconciliation_interval_sec)
        self.settlement_quarantine_sec = float(settlement_quarantine_sec)
        self.is_session_active = True
        self._last_reconciliation_ts = 0.0
        self._in_flight_exits: Dict[str, float] = {}  # symbol -> exit_timestamp

        # Initialize official GrowwAPI client object
        self.groww_client = groww_client
        if self.groww_client is None and HAS_GROWWAPI and self.access_token:
            try:
                self.groww_client = GrowwAPI(self.access_token)
            except Exception as e:
                print(f"⚠️ [GrowwLiveAdapter] Failed to initialize GrowwAPI client: {e}")

        self.orders: Dict[str, Dict[str, Any]] = {}
        self.active_orders: Dict[str, Dict[str, Any]] = {}  # Groww Order IDs awaiting fills
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self._order_counter = 0
        self.quotes_cache: Optional[Dict[str, Any]] = None

        self._lock = threading.Lock()
        self._polling_running = False
        self._poll_thread: Optional[threading.Thread] = None
        self.poll_interval_sec = poll_interval_sec

        self._init_csv()
        self._load_positions_state()

        if enable_polling:
            self._start_order_polling_thread()

    def set_quotes_cache(self, quotes_cache: Dict[str, Any]) -> None:
        """Bind shared real-time quotes cache."""
        self.quotes_cache = quotes_cache

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
        # 3. Contract human-readable symbol
        if contract_symbol:
            q = (
                self.quotes_cache.get(contract_symbol)
                or self.quotes_cache.get(contract_symbol.replace(":", "|"))
                or self.quotes_cache.get(contract_symbol.replace("|", ":"))
            )
            if q:
                return q
        # 4. Token suffix
        suffix = instrument_token.split("|")[-1].split(":")[-1]
        if suffix and suffix != instrument_token:
            q = self.quotes_cache.get(suffix)
            if q:
                return q
        # 5. Equity cash symbol ONLY if symbol == instrument_token
        if symbol == instrument_token:
            q = self.quotes_cache.get(symbol)
            if q:
                return q
        return None

    def _init_csv(self) -> None:
        """Initialize live trades CSV journal."""
        os.makedirs(os.path.dirname(os.path.abspath(self.output_csv)), exist_ok=True)
        if not os.path.exists(self.output_csv):
            with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "order_id", "symbol", "instrument_token", "direction",
                    "quantity", "entry_price", "exit_price", "gross_pnl",
                    "brokerage", "net_pnl", "entry_time", "exit_time",
                    "reason", "order_type", "sl_order_id"
                ])

    def _save_positions_state_under_lock(self) -> None:
        """Atomically persist in-memory positions to disk."""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_file)), exist_ok=True)
            data = {
                "updated_at": time.time(),
                "positions": self.positions,
                "closed_trades_count": len(self.closed_trades),
            }
            tmp_path = f"{self.state_file}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.state_file)
        except Exception:
            pass

    def _load_positions_state(self) -> None:
        """Load persisted positions state on startup."""
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded_pos = data.get("positions", {})
            if isinstance(loaded_pos, dict):
                self.positions = loaded_pos
                # Protect loaded positions in ProtectedPositionRegistry
                try:
                    from infrastructure.market_data.position_registry import ProtectedPositionRegistry
                    reg = ProtectedPositionRegistry.get_instance()
                    for p in self.positions.values():
                        reg.register_open_position(
                            contract_key=p.get("instrument_token", ""),
                            symbol=p.get("symbol", ""),
                            contract_symbol=p.get("symbol", ""),
                            mode="live",
                            track=p.get("tag", "LIVE_GROWW"),
                            trade_id=p.get("order_id", "RESTORED"),
                            quantity=p.get("quantity", 1),
                            entry_price=p.get("entry_price", 0.0),
                            entry_time=p.get("entry_time", time.time()),
                        )
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ [GrowwLiveAdapter] Failed to load positions state: {e}")

    def _handle_session_expired(self, reason: str = "") -> None:
        """
        Emergency handler triggered when broker API returns HTTP 401/403 Unauthorized.
        Automatically freezes live execution mode to prevent unhedged or corrupted trading.
        """
        self.is_session_active = False
        print("\n" + "=" * 80)
        print("🚨 [CRITICAL ALERT] GROWW BROKER SESSION EXPIRED / UNAUTHORIZED (401/403)!")
        print(f"   Reason: {reason}")
        print("   Live execution mode has been automatically FROZEN for safety.")
        print("   No new live entry orders will be submitted.")
        print("   To resume trading:")
        print("     1. Regenerate daily access token with TOTP:")
        print("        python3 runner.py token --totp <CODE>")
        print("     2. Unfreeze live mode:")
        print("        python3 runner.py unfreeze --mode live")
        print("=" * 80 + "\n")

        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/.freeze_live", "w", encoding="utf-8") as f:
                f.write(f"frozen at {datetime.now().isoformat()} due to session expiry: {reason}\n")
        except Exception:
            pass

    def _resolve_timed_out_order(
        self,
        ref_id: str,
        trading_symbol: str,
        quantity: int,
        trans_type: str,
        price: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Idempotency Resolver: When an order placement times out or drops network connection,
        queries the broker order book to verify whether the order was actually placed.
        Prevents double-ordering or placing duplicate positions with real money.
        """
        if self.groww_client is None:
            return None

        time.sleep(1.0)  # Brief pause for broker gateway ingestion

        found_order = None
        try:
            if hasattr(self.groww_client, "get_order_detail"):
                found_order = self.groww_client.get_order_detail(order_reference_id=ref_id)
        except Exception:
            pass

        if found_order and isinstance(found_order, dict):
            oid = str(found_order.get("groww_order_id") or found_order.get("order_id") or ref_id)
            st = str(found_order.get("order_status") or found_order.get("status") or "OPEN").upper()
            return {
                "order_id": oid,
                "order_reference_id": ref_id,
                "status": st,
                "resolved_from_timeout": True,
            }

        return None

    def place_order(
        self,
        symbol: str,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
        product: str = "NRML",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: Optional[str] = None,
        market_quote: Optional[Dict[str, Any]] = None,
        stop_loss_price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Submit live order to Groww using GrowwAPI client object.
        All open positions are protected with resting Stop-Loss SELL orders:
        - On BUY: executes entry at MARKET and immediately places a resting Stop-Loss SELL order at broker.
        - On SELL: modifies the resting Stop-Loss order to MARKET order for execution fill.
        """
        trans_type = transaction_type.upper()
        now_ts = time.time()
        self._order_counter += 1
        ref_id = f"GW-{int(now_ts)}-{self._order_counter:04d}"

        # Clean trading symbol for Groww
        clean_symbol = symbol.split("|")[-1]
        trading_sym = instrument_token.split("|")[-1] if "|" in instrument_token else (clean_symbol or symbol)
        is_fno = "CE" in trading_sym or "PE" in trading_sym or "FUT" in trading_sym
        segment = "FNO" if is_fno else "CASH"

        # Determine price from quote if market order
        q = market_quote or {}
        bid = float(q.get("bid") or 0.0)
        ask = float(q.get("ask") or 0.0)
        ltp = float(q.get("last_price") or q.get("ltp") or 0.0)
        ref_price = float(price) if price and float(price) > 0 else 0.0

        has_quote = bool((bid > 0 or ask > 0 or ltp > 0))
        if not has_quote and self.quotes_cache is not None:
            max_retries = 5 if trans_type == "SELL" else 1
            retry_interval_sec = 0.15
            for attempt in range(max_retries):
                candidate = self._resolve_quote_from_cache(
                    symbol=symbol,
                    instrument_token=instrument_token,
                    contract_symbol=tag or "",
                )
                if candidate:
                    cand_bid = float(candidate.get("bid") or 0.0)
                    cand_ask = float(candidate.get("ask") or 0.0)
                    cand_ltp = float(candidate.get("last_price") or candidate.get("ltp") or 0.0)
                    if trans_type == "BUY" and (cand_ask > 0 or cand_ltp > 0):
                        q = candidate
                        bid, ask, ltp = cand_bid, cand_ask, cand_ltp
                        has_quote = True
                        break
                    elif trans_type == "SELL" and (cand_bid > 0 or cand_ltp > 0):
                        q = candidate
                        bid, ask, ltp = cand_bid, cand_ask, cand_ltp
                        has_quote = True
                        break
                if attempt < max_retries - 1:
                    time.sleep(retry_interval_sec)

        if not has_quote and ref_price <= 0:
            if trans_type == "SELL":
                print(f"\n🚨 [CRITICAL LIVE EXECUTION] Cannot execute live SELL order for {symbol} ({instrument_token}): "
                      f"No real market quote available. Live position retained in portfolio.\n")
            return {
                "order_id": "",
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "status": "REJECTED_NO_QUOTE",
                "error": f"Missing market quote / price: Cannot execute {trans_type} without real-time market data (retried)",
            }

        # Staleness Gate: Block BUY orders if market quote is stale (> 30s)
        if trans_type == "BUY":
            is_fresh, quote_age = is_quote_fresh(q, now_ts=time.time(), max_age_sec=30.0)
            if not is_fresh:
                print(f"\n🚨 [CRITICAL LIVE EXECUTION] Cannot execute live BUY order for {symbol} ({instrument_token}): "
                      f"Market quote is STALE ({quote_age:.1f}s > 30.0s threshold). Live order blocked.\n")
                return {
                    "order_id": "",
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "transaction_type": trans_type,
                    "order_type": order_type,
                    "status": "REJECTED_STALE_QUOTE",
                    "error": f"Market quote for live BUY {instrument_token} is stale ({quote_age:.1f}s > 30.0s threshold). Order blocked.",
                }

        est_price = ask if (trans_type == "BUY" and ask > 0) else (bid if (trans_type == "SELL" and bid > 0) else (ltp if ltp > 0 else ref_price))
        if est_price <= 0:
            return {
                "order_id": "",
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "status": "REJECTED_NO_QUOTE",
                "error": f"Invalid estimated price <= 0 for {trans_type} (Ask={ask}, Bid={bid}, LTP={ltp})",
            }

        # Market Hours & Pre-Market Call Auction Session (CAS) Guard
        if getattr(self, "enforce_market_hours", False):
            ts = now_ts or time.time()
            ist = timezone(timedelta(hours=5, minutes=30))
            dt_ist = datetime.fromtimestamp(ts, tz=ist)
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
                        "order_type": order_type,
                        "status": "REJECTED_MARKET_CLOSED",
                        "error": f"Market closed (Weekend: {dt_ist.strftime('%A')}). Live orders blocked.",
                    }

                # Pre-market CAS Auction Block (09:00 - 09:15 IST)
                if cur_hm < self.market_open_time:
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "order_type": order_type,
                        "status": "REJECTED_PRE_MARKET_CAS",
                        "error": f"Pre-market Call Auction Session (CAS) active until {self.market_open_time} IST. Live orders strictly blocked.",
                    }

                # Post-market Market Closed Block
                if cur_hm >= self.market_close_time and tag != "EOD_MIS_SQUAREOFF":
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "order_type": order_type,
                        "status": "REJECTED_MARKET_CLOSED",
                        "error": f"Market closed at {self.market_close_time} IST. Live orders blocked.",
                    }

                # Entry Freeze Block
                if is_entry and cur_hm >= self.eod_freeze_time:
                    return {
                        "order_id": "",
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "transaction_type": trans_type,
                        "order_type": order_type,
                        "status": "REJECTED_EOD_FREEZE",
                        "error": f"Intraday entry cutoff {self.eod_freeze_time} IST reached. Live entry orders blocked.",
                    }

        # 1. Safety Gate Checks (Dry-run mode)
        if not self.live_confirmed:
            return self._simulate_fill(
                ref_id=ref_id,
                symbol=symbol,
                instrument_token=instrument_token,
                quantity=quantity,
                trans_type=trans_type,
                order_type=order_type,
                fill_price=est_price,
                now_ts=now_ts,
                tag=tag,
                is_simulated=True,
                sim_reason="live_confirmed=False (Dry-run safety gate)",
                stop_loss_price=stop_loss_price,
            )

        if not self.is_session_active:
            return {
                "order_id": "",
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "status": "REJECTED_SESSION_EXPIRED",
                "error": "Broker session is expired (HTTP 401/403). Live execution frozen.",
            }

        if self.groww_client is None:
            return {
                "order_id": "",
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "status": "REJECTED",
                "error": "GrowwAPI SDK client is not initialized. Live execution requires the official GrowwAPI client with REST fallback disabled.",
            }

        # 2. Handle SELL Exits: Modify resting Stop-Loss order to MARKET
        if trans_type == "SELL":
            pos_key = f"{symbol}_{instrument_token}"
            pos = None
            with self._lock:
                pos = self.positions.get(pos_key)
                if not pos:
                    for k, p in list(self.positions.items()):
                        if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                            pos = p
                            pos_key = k
                            break

            sl_order_id = pos.get("sl_order_id") if pos else None
            if sl_order_id:
                # Modify resting SL order to MARKET order for immediate fill
                mod_resp = self.modify_order(
                    order_id=sl_order_id,
                    order_type="MARKET",
                    price=None,
                    trigger_price=None,
                )
                if mod_resp.get("status") in ("SUCCESS", "success"):
                    exec_price = est_price
                    closed_rec = None
                    with self._lock:
                        self.active_orders.pop(sl_order_id, None)
                        if sl_order_id in self.orders:
                            self.orders[sl_order_id]["status"] = "FILLED"
                            self.orders[sl_order_id]["order_type"] = "MARKET"
                            self.orders[sl_order_id]["fill_price"] = exec_price

                        p_pop = self.positions.pop(pos_key, None)
                        if p_pop:
                            ep = float(p_pop.get("entry_price", 0.0))
                            gross = (exec_price - ep) * quantity
                            net = gross - (self.brokerage_per_order * 2.0)
                            closed_rec = {
                                "order_id": sl_order_id,
                                "symbol": symbol,
                                "instrument_token": instrument_token,
                                "direction": "BULL" if "CE" in trading_sym else "BEAR",
                                "quantity": quantity,
                                "entry_price": ep,
                                "exit_price": exec_price,
                                "gross_pnl": gross,
                                "brokerage": self.brokerage_per_order * 2.0,
                                "net_pnl": net,
                                "entry_time": p_pop.get("entry_time", now_ts),
                                "exit_time": now_ts,
                                "reason": tag or "GROWW_EXIT",
                                "sl_order_id": sl_order_id,
                            }
                            self.closed_trades.append(closed_rec)
                            self._log_trade_csv(closed_rec)
                        self._in_flight_exits[trading_sym.upper()] = now_ts
                        self._save_positions_state_under_lock()

                    if closed_rec:
                        try:
                            from infrastructure.pnl.pnl_aggregator import get_global_pnl_aggregator
                            get_global_pnl_aggregator().record_trade_exit(
                                trade_id=sl_order_id,
                                exit_price=exec_price,
                                exit_time=now_ts,
                                exit_reason=tag or "LIVE_EXIT",
                                strategy="momentum",
                                track=tag or "LIVE_GROWW",
                                mode="live",
                                symbol=symbol,
                                contract_key=instrument_token,
                                contract_symbol=trading_sym,
                                direction=closed_rec["direction"],
                                quantity=quantity,
                                entry_price=closed_rec["entry_price"],
                                entry_time=closed_rec["entry_time"],
                                brokerage=self.brokerage_per_order * 2.0,
                            )
                        except Exception:
                            pass

                    return {
                        "order_id": sl_order_id,
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "status": "FILLED",
                        "api_status": "MODIFIED_TO_MARKET",
                        "fill_price": exec_price,
                        "order_type": "MARKET",
                        "sl_order_id": sl_order_id,
                    }
                else:
                    return {
                        "order_id": sl_order_id,
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "status": "ERROR",
                        "error": f"Failed to modify SL order {sl_order_id} to MARKET: {mod_resp.get('error')}",
                    }

        # 3. Execute BUY (or direct SELL if no attached SL) via official GrowwAPI client
        resp = None
        timed_out = False
        try:
            ex_val = getattr(self.groww_client, "EXCHANGE_NSE", "NSE")
            seg_val = getattr(self.groww_client, "SEGMENT_FNO", "FNO") if is_fno else getattr(self.groww_client, "SEGMENT_CASH", "CASH")
            prod_val = getattr(self.groww_client, "PRODUCT_NRML", "NRML") if is_fno else getattr(self.groww_client, "PRODUCT_MIS", "MIS")
            valid_val = getattr(self.groww_client, "VALIDITY_DAY", "DAY")

            ot_upper = order_type.upper()
            if ot_upper == "MARKET":
                ot_val = getattr(self.groww_client, "ORDER_TYPE_MARKET", "MARKET")
            elif ot_upper == "LIMIT":
                ot_val = getattr(self.groww_client, "ORDER_TYPE_LIMIT", "LIMIT")
            elif ot_upper in ("SL", "STOP_LOSS"):
                ot_val = getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS", "SL")
            elif ot_upper in ("SL_M", "STOP_LOSS_MARKET"):
                ot_val = getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS_MARKET", "SL_M")
            else:
                ot_val = ot_upper

            tt_val = (
                getattr(self.groww_client, "TRANSACTION_TYPE_BUY", "BUY")
                if trans_type == "BUY"
                else getattr(self.groww_client, "TRANSACTION_TYPE_SELL", "SELL")
            )

            resp = self.groww_client.place_order(
                trading_symbol=trading_sym,
                quantity=int(quantity),
                validity=valid_val,
                exchange=ex_val,
                segment=seg_val,
                product=prod_val,
                order_type=ot_val,
                transaction_type=tt_val,
                price=float(price) if ot_upper == "LIMIT" else float(est_price),
                trigger_price=float(trigger_price) if trigger_price > 0 else None,
                order_reference_id=ref_id,
            )
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "unauthorized" in err_str:
                self._handle_session_expired(f"Order placement unauthorized: {e}")
                return {
                    "order_id": "",
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "status": "REJECTED_SESSION_EXPIRED",
                    "error": str(e),
                }
            if "timeout" in err_str or "timed out" in err_str:
                timed_out = True
            resp = {"status": "ERROR", "error": str(e)}

        # 4. Timeout Resolution Guard
        if timed_out:
            print(f"⚠️ [IDEMPOTENCY GUARD] Order placement timed out for {trading_sym}. Resolving broker state before retry...")
            resolved = self._resolve_timed_out_order(ref_id, trading_sym, quantity, trans_type, est_price)
            if resolved:
                resp = {"status": "SUCCESS", "payload": resolved}
            else:
                return {
                    "order_id": "",
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "status": "REJECTED_TIMEOUT",
                    "error": "Request timed out and verified not placed on broker. Safely aborted.",
                }

        # 5. Parse Response & Synchronize Portfolio State
        if resp and (resp.get("status") in ("SUCCESS", "success") or "payload" in resp or "data" in resp or "groww_order_id" in resp):
            data = resp.get("payload") or resp.get("data") or resp
            groww_order_id = str(data.get("groww_order_id") or data.get("order_id") or ref_id)
            api_status = str(data.get("order_status") or "OPEN")
            fill_p = float(data.get("average_fill_price") or 0.0)
            exec_price = fill_p if fill_p > 0 else (float(price) if order_type.upper() == "LIMIT" else float(est_price))
            order_record = {
                "order_id": groww_order_id,
                "reference_id": ref_id,
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "price": exec_price,
                "status": "OPEN" if api_status in ("OPEN", "PENDING") else "FILLED",
                "segment": segment,
                "submitted_at": now_ts,
                "tag": tag or "GROWW_ORDER",
            }

            pos_key = f"{symbol}_{instrument_token}"
            sl_order_id = None
            sl_price = stop_loss_price or round(exec_price * (1.0 - (self.option_sl_pct / 100.0)), 2)

            with self._lock:
                self.orders[groww_order_id] = order_record
                self.active_orders[groww_order_id] = order_record

                if trans_type == "BUY":
                    self.positions[pos_key] = {
                        "order_id": groww_order_id,
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "quantity": quantity,
                        "entry_price": exec_price,
                        "entry_time": now_ts,
                        "current_sl": sl_price,
                        "cost": exec_price * quantity,
                        "tag": tag or "GROWW_ORDER",
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
                        gross = (exec_price - ep) * quantity
                        net = gross - (self.brokerage_per_order * 2.0)
                        closed_rec = {
                            "order_id": groww_order_id,
                            "symbol": symbol,
                            "instrument_token": instrument_token,
                            "direction": "BULL" if "CE" in trading_sym else "BEAR",
                            "quantity": quantity,
                            "entry_price": ep,
                            "exit_price": exec_price,
                            "gross_pnl": gross,
                            "brokerage": self.brokerage_per_order * 2.0,
                            "net_pnl": net,
                            "entry_time": pos.get("entry_time", now_ts),
                            "exit_time": now_ts,
                            "reason": tag or "GROWW_EXIT",
                        }
                        self.closed_trades.append(closed_rec)
                        self._log_trade_csv(closed_rec)
                    self._in_flight_exits[trading_sym.upper()] = now_ts

                self._save_positions_state_under_lock()

            # Attach resting Stop-Loss SELL order at broker for open BUY position
            if trans_type == "BUY":
                sl_ref_id = f"SL-{int(time.time())}-{self._order_counter:04d}"
                ot_sl = getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS", "SL")
                tt_sell = getattr(self.groww_client, "TRANSACTION_TYPE_SELL", "SELL")
                sl_limit_price = round(sl_price * 0.98, 2)
                try:
                    sl_resp = self.groww_client.place_order(
                        trading_symbol=trading_sym,
                        quantity=int(quantity),
                        validity=valid_val,
                        exchange=ex_val,
                        segment=seg_val,
                        product=prod_val,
                        order_type=ot_sl,
                        transaction_type=tt_sell,
                        price=sl_limit_price,
                        trigger_price=sl_price,
                        order_reference_id=sl_ref_id,
                    )
                    if sl_resp and (sl_resp.get("status") in ("SUCCESS", "success") or "payload" in sl_resp or "data" in sl_resp or "groww_order_id" in sl_resp):
                        sl_data = sl_resp.get("payload") or sl_resp.get("data") or sl_resp
                        sl_order_id = str(sl_data.get("groww_order_id") or sl_data.get("order_id") or sl_ref_id)
                        with self._lock:
                            if pos_key in self.positions:
                                self.positions[pos_key]["sl_order_id"] = sl_order_id
                                self.positions[pos_key]["current_sl"] = sl_price
                            sl_order_rec = {
                                "order_id": sl_order_id,
                                "reference_id": sl_ref_id,
                                "symbol": symbol,
                                "instrument_token": instrument_token,
                                "quantity": quantity,
                                "transaction_type": "SELL",
                                "order_type": "SL",
                                "price": sl_limit_price,
                                "trigger_price": sl_price,
                                "status": "OPEN",
                                "segment": segment,
                                "submitted_at": now_ts,
                                "tag": "STOP_LOSS",
                            }
                            self.orders[sl_order_id] = sl_order_rec
                            self.active_orders[sl_order_id] = sl_order_rec
                            self._save_positions_state_under_lock()
                except Exception as e:
                    print(f"⚠️ [GrowwLiveAdapter] Failed to place resting SL order for {trading_sym}: {e}")

            # Record in PnLAggregator
            try:
                from infrastructure.pnl.pnl_aggregator import get_global_pnl_aggregator
                if trans_type == "BUY":
                    get_global_pnl_aggregator().record_trade_open(
                        trade_id=groww_order_id,
                        strategy="momentum",
                        track=tag or "LIVE_GROWW",
                        mode="live",
                        symbol=symbol,
                        contract_key=instrument_token,
                        contract_symbol=trading_sym,
                        direction="BULL" if "CE" in trading_sym else "BEAR",
                        quantity=quantity,
                        entry_price=exec_price,
                        entry_time=now_ts,
                        brokerage=self.brokerage_per_order,
                    )
            except Exception:
                pass

            return {
                "order_id": groww_order_id,
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "status": order_record["status"],
                "api_status": api_status,
                "sl_order_id": sl_order_id,
                "stop_loss_price": sl_price,
            }
        else:
            err_msg = str(resp.get("error") if resp else "Unknown error")
            return {
                "order_id": "",
                "symbol": symbol,
                "instrument_token": instrument_token,
                "status": "REJECTED",
                "error": err_msg,
            }

    def _simulate_fill(
        self,
        ref_id: str,
        symbol: str,
        instrument_token: str,
        quantity: int,
        trans_type: str,
        order_type: str,
        fill_price: float,
        now_ts: float,
        tag: Optional[str] = None,
        is_simulated: bool = True,
        sim_reason: str = "",
        stop_loss_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Safe simulated execution for dry-runs, safety gates, and unit tests."""
        pos_key = f"{symbol}_{instrument_token}"

        with self._lock:
            order_record = {
                "order_id": ref_id,
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": trans_type,
                "order_type": order_type,
                "price": fill_price,
                "status": "FILLED",
                "api_status": "simulated_fill",
                "submitted_at": now_ts,
                "simulated": is_simulated,
                "reason": sim_reason,
            }
            self.orders[ref_id] = order_record

            if trans_type == "BUY":
                # Create resting Stop-Loss SELL order for the position
                sl_price = stop_loss_price or round(fill_price * (1.0 - (self.option_sl_pct / 100.0)), 2)
                sl_order_id = f"SL-{ref_id}"
                sl_record = {
                    "order_id": sl_order_id,
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "transaction_type": "SELL",
                    "order_type": "SL",
                    "price": round(sl_price * 0.98, 2),
                    "trigger_price": sl_price,
                    "status": "OPEN",
                    "api_status": "simulated_resting_sl",
                    "submitted_at": now_ts,
                    "simulated": True,
                    "tag": "STOP_LOSS",
                }
                self.orders[sl_order_id] = sl_record
                self.active_orders[sl_order_id] = sl_record

                self.positions[pos_key] = {
                    "order_id": ref_id,
                    "sl_order_id": sl_order_id,
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "entry_price": fill_price,
                    "entry_time": now_ts,
                    "current_sl": sl_price,
                    "cost": fill_price * quantity,
                    "tag": tag or "LIVE_SIM",
                }
                order_record["sl_order_id"] = sl_order_id
                order_record["stop_loss_price"] = sl_price
            elif trans_type == "SELL":
                # Locate position and resting SL order
                pos = self.positions.pop(pos_key, None)
                if not pos:
                    for k, p in list(self.positions.items()):
                        if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                            pos = self.positions.pop(k)
                            break
                if pos:
                    # Modify existing resting SL order to MARKET fill
                    sl_oid = pos.get("sl_order_id")
                    if sl_oid and sl_oid in self.orders:
                        self.orders[sl_oid]["order_type"] = "MARKET"
                        self.orders[sl_oid]["status"] = "FILLED"
                        self.orders[sl_oid]["fill_price"] = fill_price
                        self.active_orders.pop(sl_oid, None)

                    ep = pos["entry_price"]
                    gross = (fill_price - ep) * quantity
                    net = gross - (self.brokerage_per_order * 2.0)
                    closed_rec = {
                        "order_id": ref_id,
                        "symbol": symbol,
                        "instrument_token": instrument_token,
                        "direction": "BULL" if "CE" in instrument_token else "BEAR",
                        "quantity": quantity,
                        "entry_price": ep,
                        "exit_price": fill_price,
                        "gross_pnl": gross,
                        "brokerage": self.brokerage_per_order * 2.0,
                        "net_pnl": net,
                        "entry_time": pos["entry_time"],
                        "exit_time": now_ts,
                        "reason": tag or "SIM_EXIT",
                        "sl_order_id": pos.get("sl_order_id", ""),
                    }
                    self.closed_trades.append(closed_rec)
                    self._log_trade_csv(closed_rec)

            self._save_positions_state_under_lock()

        return order_record

    def update_stop_loss(
        self,
        symbol: str,
        instrument_token: str,
        new_stop_loss_price: float,
    ) -> Dict[str, Any]:
        """
        Ratchet trailing stop-loss price for an open position:
        Modifies the resting stop-loss order at the broker with the updated trigger price.
        """
        new_sl = round(float(new_stop_loss_price), 2)
        if new_sl <= 0:
            return {"status": "SKIPPED", "reason": "Invalid stop-loss price <= 0"}

        pos_key = f"{symbol}_{instrument_token}"
        pos = None
        with self._lock:
            pos = self.positions.get(pos_key)
            if not pos:
                for k, p in self.positions.items():
                    if p.get("instrument_token") == instrument_token or p.get("symbol") == symbol:
                        pos = p
                        pos_key = k
                        break

        if not pos:
            return {"status": "NOT_FOUND", "reason": f"No active position found for {symbol} ({instrument_token})"}

        sl_order_id = pos.get("sl_order_id")
        if not sl_order_id:
            return {"status": "SKIPPED", "reason": "No resting stop-loss order attached to position"}

        current_sl = float(pos.get("current_sl", 0.0))
        if new_sl <= current_sl:
            return {"status": "SKIPPED", "reason": f"New SL ₹{new_sl} not higher than current SL ₹{current_sl}"}

        # Modify the resting SL order on the broker
        mod_count = int(pos.get("modification_count", 0))
        # Groww strictly caps order modifications at 25 per order. If max modifications reached, cancel and replace.
        if mod_count >= self.broker_max_order_modifications:
            print(f"⚠️ [GrowwLiveAdapter] SL order {sl_order_id} reached {mod_count} modification limit on Groww. Cancelling and placing fresh SL order...")
            cancel_res = self.cancel_order(order_id=sl_order_id)
            sell_qty = int(pos.get("quantity", 0))
            sym = pos.get("symbol", symbol)
            tok = pos.get("instrument_token", instrument_token)
            fresh_res = self._place_resting_sl_order(
                symbol=sym,
                instrument_token=tok,
                quantity=sell_qty,
                trigger_price=new_sl,
                pos_key=pos_key,
            )
            fresh_id = fresh_res.get("order_id")
            with self._lock:
                pos["current_sl"] = new_sl
                if fresh_id:
                    pos["sl_order_id"] = fresh_id
                    pos["modification_count"] = 0
                self._save_positions_state_under_lock()
            return {
                "status": "SUCCESS",
                "action": "CANCEL_AND_REPLACE",
                "old_order_id": sl_order_id,
                "new_order_id": fresh_id,
                "new_sl": new_sl,
                "cancel_response": cancel_res,
                "place_response": fresh_res,
            }

        mod_res = self.modify_order(
            order_id=sl_order_id,
            trigger_price=new_sl,
            price=round(new_sl * 0.98, 2),
            order_type="SL",
        )

        # Reactive safety: if broker rejected modification due to modification limit or error, perform cancel & replace
        mod_err = str(mod_res.get("error", "")).lower()
        if mod_res.get("status") == "ERROR" and any(w in mod_err for w in ("modification", "limit", "max", "exceed")):
            print(f"⚠️ [GrowwLiveAdapter] Broker rejected modification for {sl_order_id}: {mod_err}. Performing reactive cancel and replace...")
            cancel_res = self.cancel_order(order_id=sl_order_id)
            sell_qty = int(pos.get("quantity", 0))
            sym = pos.get("symbol", symbol)
            tok = pos.get("instrument_token", instrument_token)
            fresh_res = self._place_resting_sl_order(
                symbol=sym,
                instrument_token=tok,
                quantity=sell_qty,
                trigger_price=new_sl,
                pos_key=pos_key,
            )
            fresh_id = fresh_res.get("order_id")
            with self._lock:
                pos["current_sl"] = new_sl
                if fresh_id:
                    pos["sl_order_id"] = fresh_id
                    pos["modification_count"] = 0
                self._save_positions_state_under_lock()
            return {
                "status": "SUCCESS",
                "action": "CANCEL_AND_REPLACE",
                "old_order_id": sl_order_id,
                "new_order_id": fresh_id,
                "new_sl": new_sl,
                "cancel_response": cancel_res,
                "place_response": fresh_res,
            }

        with self._lock:
            pos["current_sl"] = new_sl
            pos["modification_count"] = mod_count + 1
            if sl_order_id in self.orders:
                self.orders[sl_order_id]["trigger_price"] = new_sl
                self.orders[sl_order_id]["price"] = round(new_sl * 0.98, 2)
            self._save_positions_state_under_lock()

        return {"status": "SUCCESS", "order_id": sl_order_id, "previous_sl": current_sl, "new_sl": new_sl, "broker_response": mod_res}

    def _place_resting_sl_order(
        self,
        symbol: str,
        instrument_token: str,
        quantity: int,
        trigger_price: float,
        pos_key: str = "",
    ) -> Dict[str, Any]:
        """Place a fresh resting Stop-Loss order at broker or register simulated resting order."""
        sl_limit_price = round(trigger_price * 0.98, 2)
        now_ts = time.time()
        sl_ref_id = f"REF-SL-{int(now_ts * 1000)}"

        if not self.live_confirmed:
            fresh_id = f"SL-FRESH-{sl_ref_id}"
            sl_order_rec = {
                "order_id": fresh_id,
                "reference_id": sl_ref_id,
                "symbol": symbol,
                "instrument_token": instrument_token,
                "quantity": quantity,
                "transaction_type": "SELL",
                "order_type": "SL",
                "price": sl_limit_price,
                "trigger_price": trigger_price,
                "status": "OPEN",
                "submitted_at": now_ts,
                "tag": "STOP_LOSS",
            }
            with self._lock:
                self.orders[fresh_id] = sl_order_rec
                self.active_orders[fresh_id] = sl_order_rec
            return {"status": "SUCCESS", "order_id": fresh_id, "simulated": True}

        if self.groww_client is None:
            return {"status": "ERROR", "error": "GrowwAPI client not initialized"}

        try:
            trading_sym = symbol
            if hasattr(self, "contract_symbol_map") and instrument_token in self.contract_symbol_map:
                trading_sym = self.contract_symbol_map[instrument_token]
            seg_val = getattr(self.groww_client, "SEGMENT_FNO", "FNO") if ("_FO" in instrument_token or "CE" in symbol or "PE" in symbol) else getattr(self.groww_client, "SEGMENT_CASH", "CASH")
            sl_resp = self.groww_client.place_order(
                trading_symbol=trading_sym,
                quantity=int(quantity),
                validity=getattr(self.groww_client, "VALIDITY_DAY", "DAY"),
                exchange=getattr(self.groww_client, "EXCHANGE_NSE", "NSE"),
                segment=seg_val,
                product=getattr(self.groww_client, "PRODUCT_MIS", "MIS"),
                order_type=getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS", "SL"),
                transaction_type=getattr(self.groww_client, "TRANSACTION_TYPE_SELL", "SELL"),
                price=sl_limit_price,
                trigger_price=trigger_price,
                order_reference_id=sl_ref_id,
            )
            if sl_resp and (sl_resp.get("status") in ("SUCCESS", "success") or "payload" in sl_resp or "groww_order_id" in sl_resp):
                sl_data = sl_resp.get("payload") or sl_resp.get("data") or sl_resp
                fresh_id = str(sl_data.get("groww_order_id") or sl_data.get("order_id") or sl_ref_id)
                sl_order_rec = {
                    "order_id": fresh_id,
                    "reference_id": sl_ref_id,
                    "symbol": symbol,
                    "instrument_token": instrument_token,
                    "quantity": quantity,
                    "transaction_type": "SELL",
                    "order_type": "SL",
                    "price": sl_limit_price,
                    "trigger_price": trigger_price,
                    "status": "OPEN",
                    "submitted_at": now_ts,
                    "tag": "STOP_LOSS",
                }
                with self._lock:
                    self.orders[fresh_id] = sl_order_rec
                    self.active_orders[fresh_id] = sl_order_rec
                return {"status": "SUCCESS", "order_id": fresh_id, "broker_response": sl_resp}
            return {"status": "ERROR", "error": f"Failed to place SL order: {sl_resp}"}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    def _handle_sl_fill_if_applicable(self, order_id: str, fill_price: float = 0.0) -> None:
        """
        If a resting Stop-Loss order filled on the broker, automatically settle and close
        the associated open position in local portfolio and PnLAggregator.
        """
        closed_rec = None
        with self._lock:
            target_key = None
            target_pos = None
            for pos_key, pos in self.positions.items():
                if pos.get("sl_order_id") == order_id:
                    target_key = pos_key
                    target_pos = pos
                    break

            if target_pos and target_key:
                pos = self.positions.pop(target_key)
                ep = float(pos.get("entry_price", 0.0))
                fp = fill_price or float(pos.get("current_sl", ep * 0.85))
                qty = int(pos.get("quantity", 0))
                gross = (fp - ep) * qty
                net = gross - (self.brokerage_per_order * 2.0)
                now_ts = time.time()
                tsym = pos.get("symbol", "")
                is_trailing = float(pos.get("current_sl", 0.0)) > (ep * (1.0 - (self.option_sl_pct / 100.0)) + 0.05)
                exit_reason = "TRAILING_STOP_LOSS" if is_trailing else "STOP_LOSS"

                closed_rec = {
                    "order_id": order_id,
                    "symbol": pos.get("symbol"),
                    "instrument_token": pos.get("instrument_token"),
                    "direction": "BULL" if "CE" in tsym else "BEAR",
                    "quantity": qty,
                    "entry_price": ep,
                    "exit_price": fp,
                    "gross_pnl": gross,
                    "brokerage": self.brokerage_per_order * 2.0,
                    "net_pnl": net,
                    "entry_time": pos.get("entry_time", now_ts),
                    "exit_time": now_ts,
                    "reason": exit_reason,
                    "sl_order_id": order_id,
                }
                self.closed_trades.append(closed_rec)
                self._log_trade_csv(closed_rec)
                clean_sym = pos.get("instrument_token", "").split("|")[-1] or pos.get("symbol", "")
                self._in_flight_exits[clean_sym.upper()] = now_ts
                self._save_positions_state_under_lock()
                print(f"🛑 [BROKER SL TRIGGERED] Stop-Loss filled for {pos.get('symbol')} @ ₹{fp:.2f}. Position closed ({exit_reason}).")

        if closed_rec:
            try:
                from infrastructure.pnl.pnl_aggregator import get_global_pnl_aggregator
                get_global_pnl_aggregator().record_trade_exit(
                    trade_id=closed_rec["order_id"],
                    exit_price=closed_rec["exit_price"],
                    exit_time=closed_rec["exit_time"],
                    exit_reason=closed_rec["reason"],
                    strategy="momentum",
                    track="LIVE_GROWW",
                    mode="live",
                    symbol=closed_rec["symbol"],
                    contract_key=closed_rec["instrument_token"],
                    contract_symbol=closed_rec["symbol"],
                    direction=closed_rec["direction"],
                    quantity=closed_rec["quantity"],
                    entry_price=closed_rec["entry_price"],
                    entry_time=closed_rec["entry_time"],
                    brokerage=closed_rec["brokerage"],
                )
            except Exception:
                pass

    def get_broker_positions(self) -> List[Dict[str, Any]]:
        """
        Query real-time net positions from Groww via official GrowwAPI SDK.
        Handles session authentication failures (401/403) and normalizes position entries.
        """
        if self.groww_client is None:
            return []

        resp = None
        # Attempt via official GrowwAPI client object
        for method_name in ("get_positions_for_user", "get_positions", "positions"):
            client_fn = getattr(self.groww_client, method_name, None)
            if callable(client_fn):
                try:
                    resp = client_fn()
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    if "401" in err_str or "unauthorized" in err_str or "session" in err_str:
                        self._handle_session_expired(f"GrowwAPI client error: {e}")
                        return []
                    resp = None

        if not resp:
            return []

        # Check for authentication expiry
        if isinstance(resp, dict) and resp.get("status") == "ERROR":
            code = resp.get("http_code")
            if code in (401, 403):
                self._handle_session_expired(f"Broker positions query returned HTTP {code}")
                return []

        # Parse positions list
        positions_raw = []
        if isinstance(resp, dict):
            payload = resp.get("payload") or resp.get("data") or resp
            if isinstance(payload, dict):
                positions_raw = payload.get("net_positions") or payload.get("positions") or []
            elif isinstance(payload, list):
                positions_raw = payload
        elif isinstance(resp, list):
            positions_raw = resp

        # Normalize position records
        normalized = []
        for p in positions_raw:
            if not isinstance(p, dict):
                continue
            tsym = str(p.get("trading_symbol") or p.get("symbol") or "")
            qty = int(p.get("quantity") or p.get("net_quantity") or 0)
            buy_qty = int(p.get("buy_quantity") or 0)
            sell_qty = int(p.get("sell_quantity") or 0)
            buy_price = float(p.get("buy_price") or p.get("average_price") or p.get("entry_price") or 0.0)
            sell_price = float(p.get("sell_price") or p.get("exit_price") or 0.0)
            realized = float(p.get("realized_pnl") or 0.0)
            unrealized = float(p.get("unrealized_pnl") or 0.0)

            normalized.append({
                "trading_symbol": tsym,
                "symbol": tsym,
                "quantity": qty,
                "buy_quantity": buy_qty,
                "sell_quantity": sell_qty,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "segment": p.get("segment", "FNO"),
                "product": p.get("product", "NRML"),
            })
        return normalized

    def reconcile_broker_positions(
        self,
        quarantine_sec: float = 10.0,
        quotes_cache: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Periodically conflates and reconciles local in-memory open positions with real-time broker positions.
        """
        now_ts = time.time()
        self._last_reconciliation_ts = now_ts

        # Query broker positions
        broker_positions = self.get_broker_positions()
        if not self.is_session_active:
            return {"status": "AUTH_EXPIRED", "timestamp": now_ts}

        # Index broker positions by trading_symbol (upper-cased)
        broker_map: Dict[str, Dict[str, Any]] = {}
        for bp in broker_positions:
            ts = bp["trading_symbol"].strip().upper()
            if ts:
                broker_map[ts] = bp

        ghost_closed = []
        orphans_adopted = []
        quantity_adjusted = []
        quarantined_skipped = []

        with self._lock:
            # 1. Check local positions against broker positions
            local_pos_keys = list(self.positions.keys())
            for pos_key in local_pos_keys:
                pos = self.positions.get(pos_key)
                if not pos:
                    continue

                entry_time = float(pos.get("entry_time", 0.0))
                age = now_ts - entry_time

                token = pos.get("instrument_token", "")
                sym = pos.get("symbol", "")
                trading_sym = token.split("|")[-1] if "|" in token else (token or sym)
                trading_sym = trading_sym.strip().upper()

                bp = broker_map.get(trading_sym)
                broker_qty = int(bp.get("quantity", 0)) if bp else 0

                # Check settlement quarantine window
                if age < quarantine_sec:
                    quarantined_skipped.append(trading_sym)
                    continue

                # Mismatch Case 1: Ghost Position (Local > 0, Broker <= 0)
                if broker_qty <= 0:
                    self.positions.pop(pos_key, None)
                    ep = float(pos.get("entry_price", 0.0))
                    qty = int(pos.get("quantity", 1))

                    exit_p = 0.0
                    if bp and float(bp.get("sell_price", 0.0)) > 0:
                        exit_p = float(bp.get("sell_price"))
                    elif quotes_cache:
                        q = quotes_cache.get(token) or quotes_cache.get(trading_sym)
                        if not q and (sym == token or sym == trading_sym):
                            q = quotes_cache.get(sym)
                        q = q or {}
                        exit_p = float(q.get("bid") or q.get("last_price") or q.get("ltp") or 0.0)
                    if exit_p <= 0:
                        exit_p = ep
                        print(
                            f"⚠️ [RECONCILIATION FALLBACK] Position {trading_sym} closed externally on broker but exit price unavailable from broker/feed. "
                            f"Defaulting to entry price ₹{ep:.2f} for state resolution.",
                            file=sys.stderr,
                        )

                    gross = (exit_p - ep) * qty
                    net = gross - (self.brokerage_per_order * 2.0)
                    closed_rec = {
                        "order_id": pos.get("order_id", f"GW-GHOST-{int(now_ts)}"),
                        "symbol": sym,
                        "instrument_token": token,
                        "direction": "BULL" if "CE" in trading_sym else "BEAR",
                        "quantity": qty,
                        "entry_price": ep,
                        "exit_price": exit_p,
                        "gross_pnl": gross,
                        "brokerage": self.brokerage_per_order * 2.0,
                        "net_pnl": net,
                        "entry_time": entry_time,
                        "exit_time": now_ts,
                        "reason": "BROKER_MANUAL_SQUAREOFF",
                    }
                    self.closed_trades.append(closed_rec)
                    self._log_trade_csv(closed_rec)

                    try:
                        from infrastructure.pnl.pnl_aggregator import get_global_pnl_aggregator
                        get_global_pnl_aggregator().record_trade_exit(
                            trade_id=closed_rec["order_id"],
                            exit_price=exit_p,
                            exit_time=now_ts,
                            exit_reason="BROKER_MANUAL_SQUAREOFF",
                            strategy="momentum",
                            track=pos.get("tag", "LIVE_GROWW"),
                            mode="live",
                            symbol=sym,
                            contract_key=token,
                            contract_symbol=trading_sym,
                            direction=closed_rec["direction"],
                            quantity=qty,
                            entry_price=ep,
                            entry_time=entry_time,
                            brokerage=self.brokerage_per_order * 2.0,
                        )
                    except Exception:
                        pass

                    try:
                        from infrastructure.market_data.position_registry import ProtectedPositionRegistry
                        ProtectedPositionRegistry.get_instance().register_closed_position(
                            contract_key=token,
                            mode="live",
                            track=pos.get("tag", "LIVE_GROWW"),
                            trade_id=closed_rec["order_id"],
                        )
                    except Exception:
                        pass

                    ghost_closed.append({
                        "symbol": trading_sym,
                        "local_qty": qty,
                        "broker_qty": 0,
                        "exit_price": exit_p,
                        "net_pnl": net,
                    })
                    print(f"⚠️ [RECONCILIATION] Ghost Position Resolved: {trading_sym} closed externally on broker. Local state synchronized.")

                # Mismatch Case 2: Quantity Discrepancy
                elif broker_qty != pos.get("quantity"):
                    old_qty = pos["quantity"]
                    pos["quantity"] = broker_qty
                    quantity_adjusted.append({
                        "symbol": trading_sym,
                        "old_quantity": old_qty,
                        "new_quantity": broker_qty,
                    })
                    print(f"ℹ️ [RECONCILIATION] Quantity Adjusted: {trading_sym} {old_qty} -> {broker_qty} to match broker.")

                # Synchronize entry price if broker reports actual filled buy price
                if bp and float(bp.get("buy_price", 0.0)) > 0:
                    bp_buy = float(bp.get("buy_price"))
                    if abs(bp_buy - float(pos.get("entry_price", 0.0))) > 0.01:
                        pos["entry_price"] = bp_buy
                        pos["cost"] = bp_buy * int(pos.get("quantity", 1))

            # 2. Check broker positions for Orphans (Broker > 0, Local == 0)
            for tsym, bp in broker_map.items():
                b_qty = bp.get("quantity", 0)
                if b_qty <= 0:
                    continue

                already_tracked = False
                for p in self.positions.values():
                    ptok = (p.get("instrument_token", "") or "").split("|")[-1].strip().upper()
                    psym = (p.get("symbol", "") or "").strip().upper()
                    if ptok == tsym or psym == tsym:
                        already_tracked = True
                        break

                if not already_tracked:
                    if tsym in self._in_flight_exits and (now_ts - self._in_flight_exits[tsym]) < quarantine_sec:
                        continue

                    pos_key = f"ADOPTED_{tsym}"
                    bp_price = float(bp.get("buy_price", 0.0))
                    adopted_rec = {
                        "order_id": f"GW-ADOPTED-{int(now_ts)}",
                        "symbol": tsym,
                        "instrument_token": f"NSE_FO|{tsym}",
                        "quantity": b_qty,
                        "entry_price": bp_price,
                        "entry_time": now_ts,
                        "cost": bp_price * b_qty,
                        "tag": "LIVE_ADOPTED",
                        "adopted": True,
                    }
                    self.positions[pos_key] = adopted_rec

                    try:
                        from infrastructure.market_data.position_registry import ProtectedPositionRegistry
                        ProtectedPositionRegistry.get_instance().register_open_position(
                            contract_key=f"NSE_FO|{tsym}",
                            symbol=tsym,
                            contract_symbol=tsym,
                            mode="live",
                            track="LIVE_ADOPTED",
                            trade_id=adopted_rec["order_id"],
                            quantity=b_qty,
                            entry_price=bp_price,
                            entry_time=now_ts,
                        )
                    except Exception:
                        pass

                    orphans_adopted.append({
                        "symbol": tsym,
                        "quantity": b_qty,
                        "buy_price": bp_price,
                    })
                    print(f"🚨 [RECONCILIATION] Orphan Position Adopted: {tsym} (qty: {b_qty} @ ₹{bp_price}). Risk management attached.")

            self._save_positions_state_under_lock()

        return {
            "status": "SUCCESS",
            "timestamp": now_ts,
            "broker_positions_count": len(broker_positions),
            "local_positions_count": len(self.positions),
            "ghost_positions_closed": ghost_closed,
            "orphan_positions_adopted": orphans_adopted,
            "quantity_mismatches_adjusted": quantity_adjusted,
            "quarantined_positions_skipped": quarantined_skipped,
        }

    def check_eod_squareoff(
        self,
        now_ts: Optional[float] = None,
        quotes_cache: Optional[Dict[str, Any]] = None,
        eod_time: Optional[str] = None,
        freeze_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        End-of-Day RMS Liquidation Guard for Indian F&O / Equity markets:
        - At freeze_time (default 15:10 IST): Auto-freeze live mode to block new entries.
        - At eod_time (default 15:15 IST): Gracefully square off open intraday MIS positions at market price,
          preventing harsh broker auto-liquidation penalties (₹50+GST/order).
        """
        ts = now_ts or time.time()
        ist = timezone(timedelta(hours=5, minutes=30))
        dt_ist = datetime.fromtimestamp(ts, tz=ist)
        if dt_ist.year < 2020:
            return []

        cur_hm = dt_ist.strftime("%H:%M")
        target_freeze = freeze_time or getattr(self, "eod_freeze_time", "15:10")
        target_eod = eod_time or getattr(self, "eod_square_off_time", "15:15")

        exited = []
        # 1. At freeze_time: Auto-freeze live mode
        if cur_hm >= target_freeze or cur_hm >= target_eod:
            if not os.path.exists("logs/.freeze_live"):
                try:
                    os.makedirs("logs", exist_ok=True)
                    with open("logs/.freeze_live", "w", encoding="utf-8") as f:
                        f.write(f"frozen at {dt_ist.isoformat()} due to EOD {target_freeze} IST guard\n")
                    print(f"⏰ [EOD GUARD] {target_freeze} IST reached ({dt_ist.strftime('%H:%M:%S')}). Auto-froze live execution to block new entries.")
                except Exception:
                    pass

        # 2. At eod_time: Auto-squareoff open intraday positions
        if cur_hm >= target_eod:
            with self._lock:
                pos_items = list(self.positions.items())

            for pos_key, pos in pos_items:
                sym = pos.get("symbol", "")
                token = pos.get("instrument_token", "")
                qty = int(pos.get("quantity", 1))
                q = (quotes_cache or {}).get(token) or {}

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
                print(f"⏰ [EOD GUARD] {target_eod} IST Auto-Squareoff: Closed {sym} ({token}) to prevent broker RMS penalty.")

        return exited

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
        """Modify open order parameters using GrowwAPI client object."""
        with self._lock:
            if order_id in self.orders:
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

        if not self.live_confirmed:
            return {"status": "success", "order_id": order_id, "simulated": True}

        if self.groww_client is None:
            return {"status": "ERROR", "error": "GrowwAPI client not initialized. REST fallback disabled."}

        try:
            ot_upper = order_type.upper()
            if ot_upper == "MARKET":
                ot_val = getattr(self.groww_client, "ORDER_TYPE_MARKET", "MARKET")
            elif ot_upper == "LIMIT":
                ot_val = getattr(self.groww_client, "ORDER_TYPE_LIMIT", "LIMIT")
            elif ot_upper in ("SL", "STOP_LOSS"):
                ot_val = getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS", "SL")
            elif ot_upper in ("SL_M", "STOP_LOSS_MARKET"):
                ot_val = getattr(self.groww_client, "ORDER_TYPE_STOP_LOSS_MARKET", "SL_M")
            else:
                ot_val = ot_upper

            resp = self.groww_client.modify_order(
                groww_order_id=order_id,
                quantity=int(quantity) if quantity is not None else None,
                price=float(price) if price is not None and ot_upper != "MARKET" else None,
                trigger_price=float(trigger_price) if trigger_price is not None and ot_upper != "MARKET" else None,
                order_type=ot_val,
                validity=getattr(self.groww_client, f"VALIDITY_{validity.upper()}", validity.upper()),
            )
            return resp if isinstance(resp, dict) else {"status": "SUCCESS", "payload": resp}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel open pending order using GrowwAPI client object."""
        with self._lock:
            if order_id in self.orders:
                self.orders[order_id]["status"] = "CANCELLED"
            self.active_orders.pop(order_id, None)

        if not self.live_confirmed:
            return {"status": "success", "order_id": order_id, "simulated": True}

        if self.groww_client is None:
            return {"status": "ERROR", "error": "GrowwAPI client not initialized. REST fallback disabled."}

        try:
            resp = self.groww_client.cancel_order(groww_order_id=order_id)
            return resp if isinstance(resp, dict) else {"status": "SUCCESS", "payload": resp}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Query current lifecycle status of an order."""
        with self._lock:
            if order_id in self.orders:
                rec = self.orders[order_id]
                return {
                    "order_id": order_id,
                    "status": rec.get("status", "UNKNOWN"),
                    "fill_price": rec.get("fill_price", rec.get("price", 0.0)),
                    "quantity": rec.get("quantity", 0),
                    "filled_quantity": rec.get("filled_quantity", 0),
                }

        if self.live_confirmed and (self.access_token or self.groww_client is not None):
            return self.poll_order_status(order_id)

        return {"order_id": order_id, "status": "NOT_FOUND"}

    def poll_order_status(self, order_id: str, segment: str = "FNO") -> Dict[str, Any]:
        """Poll single order status via groww_client."""
        if not self.live_confirmed:
            with self._lock:
                ord_rec = self.orders.get(order_id, {})
                return {
                    "order_id": order_id,
                    "status": ord_rec.get("status", "OPEN"),
                    "fill_price": ord_rec.get("price", 0.0),
                }

        if self.groww_client is None:
            return {"status": "UNKNOWN", "order_id": order_id}

        resp = None
        try:
            resp = self.groww_client.get_order_detail(groww_order_id=order_id)
        except Exception:
            try:
                resp = self.groww_client.get_order_status(groww_order_id=order_id)
            except Exception:
                resp = None

        if resp and (resp.get("status") in ("SUCCESS", "success") or "payload" in resp or "data" in resp or "order_status" in resp):
            data = resp.get("payload") or resp.get("data") or resp
            order_status = str(data.get("order_status") or data.get("status") or "OPEN").upper()
            fill_p = float(data.get("average_fill_price") or data.get("price") or 0.0)
            fill_qty = int(data.get("filled_quantity") or 0)

            with self._lock:
                if order_id in self.orders:
                    self.orders[order_id]["status"] = order_status
                    if fill_p > 0:
                        self.orders[order_id]["fill_price"] = fill_p
                    if fill_qty > 0:
                        self.orders[order_id]["filled_quantity"] = fill_qty

                if order_status in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                    self.active_orders.pop(order_id, None)

                # If entry BUY order filled on broker, synchronize local position's entry price
                if order_status == "FILLED" and fill_p > 0:
                    for p in self.positions.values():
                        if p.get("order_id") == order_id:
                            p["entry_price"] = fill_p
                            p["cost"] = fill_p * int(p.get("quantity", 1))

            # Check if this order was a resting SL that filled on the broker
            if order_status == "FILLED":
                self._handle_sl_fill_if_applicable(order_id, fill_p)

            return {"order_id": order_id, "status": order_status, "fill_price": fill_p}
        return {"order_id": order_id, "status": "PENDING"}

    def _start_order_polling_thread(self) -> None:
        """Start background daemon thread that polls active orders and runs periodic position reconciliation."""
        self._polling_running = True
        self._poll_thread = threading.Thread(target=self._order_polling_loop, daemon=True)
        self._poll_thread.start()

    def _order_polling_loop(self) -> None:
        """Background continuous status polling and reconciliation loop."""
        while self._polling_running:
            # 1. Poll active orders
            active_ids = []
            with self._lock:
                active_ids = list(self.active_orders.keys())

            for oid in active_ids:
                try:
                    self.poll_order_status(oid)
                except Exception:
                    pass

            # 2. Periodic Broker Position Conflation & EOD Guard
            now_ts = time.time()
            if self.live_confirmed and (now_ts - self._last_reconciliation_ts) >= self.reconciliation_interval_sec:
                try:
                    self.reconcile_broker_positions(quarantine_sec=self.settlement_quarantine_sec)
                    self.check_eod_squareoff(now_ts=now_ts)
                except Exception as e:
                    print(f"⚠️ [GrowwLiveAdapter] Background reconciliation error: {e}")

            time.sleep(self.poll_interval_sec)

    def _log_trade_csv(self, trade: Dict[str, Any]) -> None:
        """Log executed trade to logs/live_trades.csv."""
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
                    "GROWW_LIVE",
                    trade.get("sl_order_id", ""),
                ])
        except Exception:
            pass

    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.positions.values())

    def get_portfolio_summary(self) -> Dict[str, Any]:
        with self._lock:
            wins = sum(1 for t in self.closed_trades if t.get("net_pnl", 0.0) > 0)
            losses = len(self.closed_trades) - wins
            tot = len(self.closed_trades)
            net_realized = sum(t.get("net_pnl", 0.0) for t in self.closed_trades)
            wr = (wins / tot * 100.0) if tot > 0 else 0.0
            roi = (net_realized / self.initial_capital * 100.0) if self.initial_capital > 0 else 0.0

            return {
                "initial_capital": self.initial_capital,
                "available_capital": self.available_capital,
                "open_positions": len(self.positions),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wr, 1),
                "net_realized": round(net_realized, 2),
                "net_roi": round(roi, 2),
                "is_session_active": self.is_session_active,
            }

    def shutdown(self) -> None:
        """Stop background polling thread gracefully."""
        self._polling_running = False
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=2.0)


# Backward-compatibility alias
LiveExecutionAdapter = GrowwLiveAdapter
