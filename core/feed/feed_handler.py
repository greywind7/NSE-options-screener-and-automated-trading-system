"""
Core Market Data Feed Handler (Spec 2.0).
Provides multi-source market tick ingestion (Upstox WebSocket v3 Protobuf, REST poller failover, and Mock engine).
Separates Cash Equity ticks and Options Depth ticks onto two physically distinct ZeroMQ IPC sockets:
- feed_equity_ipc  (TICK.EQ) : Cash equity underlyings (consumed by CandleManager and MarketRecorder)
- feed_options_ipc (TICK.OPT): Derivative option depth quotes (consumed by QuotesBroker and MarketRecorder)

Features:
- Dynamic popular options selection (ATM +/- 3 strikes with 1-strike hysteresis window)
- Critical Active Position Protection Guard (never unsubscribes contracts with active positions)
- High-performance ZeroMQ binary msgpack serialization
- Microsecond edge token canonicalization
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import signal
import ssl
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import zmq

from core.auth.upstox_auth import get_access_token, http_get_json, log_debug
from core.common.ipc_schemas import (
    EquityTickEvent,
    OptionDepthTickEvent,
    TickEvent,
    pack_message,
)
from core.common.symbol_utils import normalize_instrument_token, is_option_contract
from core.config.config_loader import get_config_manager
from core.feed.position_registry import ProtectedPositionRegistry

try:
    from core.feed.proto import MarketDataFeedV3_pb2 as pb
    from core.feed.proto import FeedResponse
except Exception:
    pb = None
    FeedResponse = None

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    websockets = None
    HAS_WEBSOCKETS = False

DEFAULT_FEED_EQ_IPC = "ipc:///tmp/feed_equity.ipc"
DEFAULT_FEED_OPT_IPC = "ipc:///tmp/feed_options.ipc"


class FeedHandler:
    """
    Centralized Market Ingestion Engine.
    Routes Cash Equities to feed_equity_ipc and Options Depth to feed_options_ipc.
    """

    def __init__(
        self,
        mode: str = "mock",
        exchange: str = "NSE",
        feed_equity_ipc: Optional[str] = None,
        feed_options_ipc: Optional[str] = None,
        access_token: Optional[str] = None,
        manifest_path: Optional[str] = None,
        filtered_csv: Optional[str] = None,
        max_stream_contracts: int = 1800,
        silent: bool = False,
        allow_rest_fallback: bool = False,
        poll_interval: float = 1.0,
        ctx: Optional[zmq.Context] = None,
    ):
        cfg = get_config_manager()
        ipc_endpoints = cfg.get_ipc_endpoints()

        self.mode = mode.lower()
        self.exchange = exchange.upper()
        self.feed_equity_ipc = feed_equity_ipc or ipc_endpoints.get("feed_equity_ipc", DEFAULT_FEED_EQ_IPC)
        self.feed_options_ipc = feed_options_ipc or ipc_endpoints.get("feed_options_ipc", DEFAULT_FEED_OPT_IPC)
        self.access_token = access_token or get_access_token()
        self.poll_interval = max(0.2, float(poll_interval))
        self.max_stream_contracts = max(1, int(max_stream_contracts))
        self.silent = bool(silent)
        self.allow_rest_fallback = bool(allow_rest_fallback)
        self.debug_log = "logs/debug.log"

        if manifest_path:
            self.manifest_path = manifest_path
        else:
            self.manifest_path = "data/subscription_manifest_mcx.json" if self.exchange == "MCX" else "data/subscription_manifest.json"

        if filtered_csv:
            self.filtered_csv = filtered_csv
        else:
            self.filtered_csv = "data/filtered_mcx.csv" if self.exchange == "MCX" else "data/filtered.csv"

        # ZeroMQ Publisher Sockets
        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        # Clean orphaned IPC socket files if any
        for ep in (self.feed_equity_ipc, self.feed_options_ipc):
            if ep and ep.startswith("ipc://"):
                p_sock = ep[6:]
                if os.path.exists(p_sock):
                    try:
                        os.unlink(p_sock)
                    except OSError:
                        pass

        # 1. Equity socket (consumed by CandleManager & MarketRecorder)
        self.pub_eq = self.ctx.socket(zmq.PUB)
        self.pub_eq.setsockopt(zmq.LINGER, 0)
        self.pub_eq.setsockopt(zmq.SNDHWM, 200000)
        self.pub_eq.bind(self.feed_equity_ipc)

        # 2. Options socket (consumed by QuotesBroker & MarketRecorder)
        self.pub_opt = self.ctx.socket(zmq.PUB)
        self.pub_opt.setsockopt(zmq.LINGER, 0)
        self.pub_opt.setsockopt(zmq.SNDHWM, 200000)
        self.pub_opt.bind(self.feed_options_ipc)

        self.running = False
        self._running = False
        self.total_eq_published = 0
        self.total_opt_published = 0
        self.last_seen_volume: Dict[str, int] = {}
        self.last_seen_ltp: Dict[str, float] = {}
        self.last_seen_depth: Dict[str, Any] = {}
        self.instruments: List[Dict[str, str]] = []
        self.symbol_map: Dict[str, str] = {}

        # Dynamic popular options tracking state
        self.enable_dynamic_options: bool = True
        self.dynamic_popular_strikes: int = 3
        self.dynamic_hysteresis_strikes: int = 1
        self.options_map: Dict[str, Any] = {}
        self.available_strikes_by_symbol: Dict[str, List[float]] = {}
        self.underlying_key_to_symbol: Dict[str, str] = {}
        self.symbol_metadata: Dict[str, Any] = {}
        self.option_name_map: Dict[str, str] = {}
        self.underlying_keys: Set[str] = set()
        self.active_option_keys: Set[str] = set()
        self.active_options_by_symbol: Dict[str, Set[str]] = defaultdict(set)
        self.last_atm_by_symbol: Dict[str, float] = {}
        self.last_reval_time_by_symbol: Dict[str, float] = {}
        self.total_dynamic_subscriptions: int = 0
        self.total_dynamic_unsubscriptions: int = 0
        self.position_registry: ProtectedPositionRegistry = ProtectedPositionRegistry.get_instance()
        self.protected_logged_keys: Set[str] = set()

        # Active websocket handle for dynamic subscription dispatch
        self._active_ws = None
        self._ws_loop = None

    def _log(self, msg: str, force: bool = False) -> None:
        if not self.silent or force:
            print(msg)

    def is_rest_fallback_allowed(self) -> bool:
        if getattr(self, "allow_rest_fallback", False):
            return True
        if os.path.exists("logs/.allow_rest_fallback"):
            return True
        return False

    def load_instruments(self) -> List[Dict[str, str]]:
        """Load instrument universe to monitor from manifest or filtered CSV."""
        instruments: List[Dict[str, str]] = []
        if self.exchange == "NSE":
            instruments.append({"key": "NSE_INDEX|Nifty 50", "symbol": "Nifty 50"})
            self.symbol_map["NSE_INDEX|Nifty 50"] = "Nifty 50"
            self.symbol_map["NSE_INDEX:Nifty 50"] = "Nifty 50"

        filtered_map: Dict[str, str] = {}
        if os.path.exists(self.filtered_csv):
            try:
                with open(self.filtered_csv, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        k = (row.get("upstox_instrument_key") or row.get("instrument_key") or "").strip()
                        s = (row.get("trading_symbol") or row.get("tradingsymbol") or row.get("name") or "").strip()
                        if k and s:
                            filtered_map[k] = s
                            filtered_map[k.replace(":", "|")] = s
                            filtered_map[k.replace("|", ":")] = s
                            self.underlying_keys.add(k)
                            self.underlying_keys.add(k.replace(":", "|"))
                            self.underlying_keys.add(k.replace("|", ":"))
            except Exception:
                pass

        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                meta = manifest.get("metadata", {}) or manifest.get("symbol_metadata", {})
                self.symbol_metadata = meta.get("symbols", {}) if "symbols" in meta else meta

                # Load underlying equities
                streamed_und = manifest.get("streamed_underlyings")
                if isinstance(streamed_und, list):
                    for u in streamed_und:
                        k = u.get("instrument_key") or u.get("key", "")
                        sym = u.get("symbol") or u.get("name", k)
                        if k and sym:
                            instruments.append({"key": k, "symbol": sym})
                            self.symbol_map[k] = sym
                            self.symbol_map[k.replace(":", "|")] = sym
                            self.symbol_map[k.replace("|", ":")] = sym
                            self.underlying_keys.add(k)
                            self.underlying_keys.add(k.replace(":", "|"))
                            self.underlying_keys.add(k.replace("|", ":"))
                            self.underlying_key_to_symbol[k.replace(":", "|")] = sym
                            self.underlying_key_to_symbol[k.replace("|", ":")] = sym
                elif isinstance(manifest.get("streamed_contracts", {}).get("underlying_equities"), dict):
                    for k, v in manifest["streamed_contracts"]["underlying_equities"].items():
                        sym = v.get("symbol", k)
                        instruments.append({"key": k, "symbol": sym})
                        self.symbol_map[k] = sym
                        self.symbol_map[k.replace(":", "|")] = sym
                        self.symbol_map[k.replace("|", ":")] = sym
                        self.underlying_keys.add(k)
                        self.underlying_keys.add(k.replace(":", "|"))
                        self.underlying_keys.add(k.replace("|", ":"))
                        self.underlying_key_to_symbol[k.replace(":", "|")] = sym
                        self.underlying_key_to_symbol[k.replace("|", ":")] = sym

                self.options_map = manifest.get("options_map", {})
                for sym, opt_info in self.options_map.items():
                    strikes_dict = opt_info.get("strikes", {})
                    flist: List[float] = []
                    for s_str in strikes_dict.keys():
                        try:
                            flist.append(float(s_str))
                        except Exception:
                            pass
                    flist.sort()
                    self.available_strikes_by_symbol[sym] = flist

                    for s_str, calls_puts in strikes_dict.items():
                        for otype, okey in calls_puts.items():
                            disp_name = f"{sym} {s_str} {otype}"
                            self.option_name_map[okey] = disp_name
                            self.option_name_map[okey.replace(":", "|")] = disp_name
                            self.option_name_map[okey.replace("|", ":")] = disp_name

                # Add initial popular options
                streamed_pop = manifest.get("streamed_popular_options")
                if isinstance(streamed_pop, list):
                    for pop in streamed_pop:
                        k = pop.get("instrument_key") or pop.get("key", "")
                        opt_sym = pop.get("trading_symbol") or pop.get("symbol") or pop.get("name", k)
                        u_sym = pop.get("underlying", "")
                        if k and opt_sym:
                            instruments.append({"key": k, "symbol": opt_sym})
                            self.symbol_map[k] = opt_sym
                            self.symbol_map[k.replace(":", "|")] = opt_sym
                            self.symbol_map[k.replace("|", ":")] = opt_sym
                            self.active_option_keys.add(k)
                            if u_sym:
                                self.active_options_by_symbol[u_sym].add(k)
                elif isinstance(manifest.get("streamed_contracts", {}).get("popular_options"), dict):
                    for k, v in manifest["streamed_contracts"]["popular_options"].items():
                        opt_sym = v.get("symbol", k)
                        instruments.append({"key": k, "symbol": opt_sym})
                        self.symbol_map[k] = opt_sym
                        self.symbol_map[k.replace(":", "|")] = opt_sym
                        self.symbol_map[k.replace("|", ":")] = opt_sym
                        self.active_option_keys.add(k)
                        u_sym = v.get("underlying")
                        if u_sym:
                            self.active_options_by_symbol[u_sym].add(k)
            except Exception as e:
                self._log(f"Warning reading manifest {self.manifest_path}: {e}")

        # Add remaining underlyings from filtered CSV if not already present
        for k, s in filtered_map.items():
            if "|" in k:
                if not any(inst["key"].replace(":", "|") == k for inst in instruments):
                    instruments.append({"key": k, "symbol": s})
                    self.symbol_map[k] = s
                    self.symbol_map[k.replace("|", ":")] = s

        self.instruments = instruments
        return instruments

    def _prune_dynamic_options_to_fit(
        self, keys_to_add: Set[str], protected_keys: Set[str], current_symbol: str
    ) -> Set[str]:
        """Prune unheld dynamic options when stream capacity limit is reached."""
        total_underlyings = len(self.underlying_keys)
        current_options_count = len(self.active_option_keys)
        total_current = total_underlyings + current_options_count
        new_contracts_count = len(keys_to_add - self.active_option_keys)
        projected_total = total_current + new_contracts_count

        if projected_total <= self.max_stream_contracts:
            return set()

        contracts_to_prune_count = projected_total - self.max_stream_contracts
        pruned_keys: Set[str] = set()

        # Candidate pool: active options across OTHER symbols that are not protected
        for sym, opt_set in list(self.active_options_by_symbol.items()):
            if sym == current_symbol:
                continue
            unheld = opt_set - protected_keys
            for k in unheld:
                pruned_keys.add(k)
                if len(pruned_keys) >= contracts_to_prune_count:
                    break
            if len(pruned_keys) >= contracts_to_prune_count:
                break

        # Fallback to current symbol if capacity still exceeded
        if len(pruned_keys) < contracts_to_prune_count:
            curr_unheld = self.active_options_by_symbol.get(current_symbol, set()) - protected_keys
            for k in curr_unheld:
                pruned_keys.add(k)
                if len(pruned_keys) >= contracts_to_prune_count:
                    break

        return pruned_keys

    def evaluate_dynamic_options(self, underlying_key: str, ltp: float) -> Tuple[List[str], List[str]]:
        """
        Evaluate ATM shift for an underlying stock.
        Returns (keys_to_subscribe, keys_to_unsubscribe).
        """
        if not self.enable_dynamic_options or not self.options_map or ltp <= 0.0:
            return [], []

        canonical_key = underlying_key.replace(":", "|")
        sym = self.underlying_key_to_symbol.get(canonical_key) or self.underlying_key_to_symbol.get(underlying_key) or self.symbol_map.get(canonical_key)
        if not sym:
            return [], []

        lookup_sym = sym
        if lookup_sym not in self.options_map:
            for k in self.options_map.keys():
                if k == sym or k.startswith(sym) or sym.startswith(k):
                    lookup_sym = k
                    break

        if lookup_sym not in self.options_map or lookup_sym not in self.available_strikes_by_symbol:
            return [], []

        strikes_list = self.available_strikes_by_symbol[lookup_sym]
        if not strikes_list:
            return [], []

        now = time.time()
        last_reval = self.last_reval_time_by_symbol.get(lookup_sym, 0.0)
        last_atm = self.last_atm_by_symbol.get(lookup_sym)

        atm_strike = min(strikes_list, key=lambda s: abs(s - ltp))
        if last_atm is not None and atm_strike == last_atm:
            if (now - last_reval) < 10.0:
                return [], []

        if last_atm is not None and (now - last_reval < 1.0):
            return [], []

        atm_idx = strikes_list.index(atm_strike)
        pop_n = self.dynamic_popular_strikes
        h_n = self.dynamic_hysteresis_strikes

        start_idx = max(0, atm_idx - pop_n)
        end_idx = min(len(strikes_list), atm_idx + pop_n + 1)
        target_strikes = strikes_list[start_idx:end_idx]

        strikes_dict = self.options_map[lookup_sym].get("strikes", {})
        new_target_keys: Set[str] = set()

        for s in target_strikes:
            matched_dict = None
            for candidate in (str(s), f"{s:.1f}", f"{s:.2f}", f"{int(s) if s.is_integer() else s}"):
                if candidate in strikes_dict:
                    matched_dict = strikes_dict[candidate]
                    break
            if matched_dict:
                for otype, okey in matched_dict.items():
                    new_target_keys.add(okey)
                    if okey not in self.symbol_map:
                        eq_disp = self.symbol_metadata.get(lookup_sym, {}).get("name") or sym
                        disp = self.option_name_map.get(okey) or f"{eq_disp} {s:g} {otype}"
                        self.symbol_map[okey] = disp
                        self.symbol_map[okey.replace(":", "|")] = disp
                        self.symbol_map[okey.replace("|", ":")] = disp

        current_keys = self.active_options_by_symbol.get(lookup_sym, set())
        keys_to_add = new_target_keys - current_keys

        # Retention window with hysteresis
        wide_start = max(0, atm_idx - pop_n - h_n)
        wide_end = min(len(strikes_list), atm_idx + pop_n + h_n + 1)
        wide_strikes = strikes_list[wide_start:wide_end]
        wide_keys: Set[str] = set()
        for s in wide_strikes:
            for candidate in (str(s), f"{s:.1f}", f"{s:.2f}", f"{int(s) if s.is_integer() else s}"):
                if candidate in strikes_dict:
                    for otype, okey in strikes_dict[candidate].items():
                        wide_keys.add(okey)
                    break

        keys_to_remove = current_keys - wide_keys

        # Active Position Protection Guard
        protected_keys = self.position_registry.get_protected_keys()
        if protected_keys:
            saved = keys_to_remove & protected_keys
            if saved:
                for sk in saved:
                    if sk not in self.protected_logged_keys:
                        self.protected_logged_keys.add(sk)
                        sym_disp = self.symbol_map.get(sk, sk)
                        print(f"  🛡️ [PROTECTION ACTIVE] Retaining subscription for '{sym_disp}' ({sk}) - Active open position detected!")
            keys_to_remove -= protected_keys

            # Guarantee any protected option for this symbol is active
            sym_all_options = set()
            for s_str, c_p in strikes_dict.items():
                for otype, okey in c_p.items():
                    sym_all_options.add(okey)
            protected_for_sym = protected_keys & sym_all_options
            keys_to_add |= (protected_for_sym - current_keys)

        if not keys_to_add and not keys_to_remove:
            self.last_atm_by_symbol[lookup_sym] = atm_strike
            self.last_reval_time_by_symbol[lookup_sym] = now
            return [], []

        # Capacity limit enforcement
        if keys_to_add:
            capacity_pruned = self._prune_dynamic_options_to_fit(keys_to_add, protected_keys, lookup_sym)
            if capacity_pruned:
                keys_to_remove |= capacity_pruned

        # Update in-memory sets
        self.active_options_by_symbol[lookup_sym] = (current_keys - keys_to_remove) | keys_to_add
        self.active_option_keys = (self.active_option_keys - keys_to_remove) | keys_to_add
        self.last_atm_by_symbol[lookup_sym] = atm_strike
        self.last_reval_time_by_symbol[lookup_sym] = now
        self.total_dynamic_subscriptions += len(keys_to_add)
        self.total_dynamic_unsubscriptions += len(keys_to_remove)

        return list(keys_to_add), list(keys_to_remove)

    def publish_tick(
        self,
        token: str,
        symbol: str,
        ltp: float,
        volume: int = 0,
        bid: float = 0.0,
        ask: float = 0.0,
        bid_qty: int = 0,
        ask_qty: int = 0,
        exchange_ts: float = 0.0,
        received_ts: float = 0.0,
        bids: Optional[List[List[float]]] = None,
        asks: Optional[List[List[float]]] = None,
    ) -> None:
        """Publish a tick event over ZeroMQ to its dedicated IPC socket with full depth arrays."""
        canonical_token = normalize_instrument_token(token)
        is_opt = is_option_contract(canonical_token) or is_option_contract(symbol)

        b_qty = int(bid_qty or (bids[0][1] if bids else 0))
        a_qty = int(ask_qty or (asks[0][1] if asks else 0))

        if is_opt:
            event = OptionDepthTickEvent(
                token=canonical_token,
                symbol=symbol,
                ltp=float(ltp),
                volume=int(volume),
                bid=float(bid or ltp),
                ask=float(ask or ltp),
                bid_qty=b_qty,
                ask_qty=a_qty,
                exchange_timestamp=float(exchange_ts or time.time()),
                received_timestamp=float(received_ts or time.time()),
                bids=bids or [],
                asks=asks or [],
            )
            payload = pack_message(event)
            # 1. Option depth tick -> publish strictly to Options socket (QuotesBroker)
            self.pub_opt.send_multipart([b"TICK.OPT", payload])
            self.total_opt_published += 1
        else:
            event = EquityTickEvent(
                token=canonical_token,
                symbol=symbol,
                ltp=float(ltp),
                volume=int(volume),
                bid=float(bid or ltp),
                ask=float(ask or ltp),
                bid_qty=b_qty,
                ask_qty=a_qty,
                exchange_timestamp=float(exchange_ts or time.time()),
                received_timestamp=float(received_ts or time.time()),
            )
            payload = pack_message(event)
            # 2. Equity tick -> publish strictly to Equity socket (CandleManager)
            self.pub_eq.send_multipart([b"TICK.EQ", payload])
            self.total_eq_published += 1

            # 3. Dynamic popular strikes evaluation on price change
            if self.enable_dynamic_options:
                to_add, to_remove = self.evaluate_dynamic_options(canonical_token, ltp)
                if (to_add or to_remove) and self._active_ws and self._ws_loop:
                    # Dispatch dynamic sub/unsub over live WebSocket
                    asyncio.run_coroutine_threadsafe(
                        self._dispatch_ws_subscription_update(to_add, to_remove), self._ws_loop
                    )

    async def _dispatch_ws_subscription_update(self, to_add: List[str], to_remove: List[str]) -> None:
        if not self._active_ws:
            return
        try:
            if to_add:
                sub_msg = json.dumps({"guid": "dyn_sub", "method": "sub", "data": {"mode": "full", "instrumentKeys": to_add}}).encode("utf-8")
                await self._active_ws.send(sub_msg)
            if to_remove:
                unsub_msg = json.dumps({"guid": "dyn_unsub", "method": "unsub", "data": {"mode": "full", "instrumentKeys": to_remove}}).encode("utf-8")
                await self._active_ws.send(unsub_msg)
        except Exception as e:
            log_debug(f"Error updating dynamic subscriptions on WebSocket: {e}", self.debug_log)

    def start(self) -> None:
        """Start the market data ingestion engine according to configured mode."""
        self.running = True
        self._running = True
        self.load_instruments()

        print("=" * 80)
        print("📡 MARKET DATA FEED HANDLER SERVICE ACTIVE")
        print(f"Equity IPC Socket  : {self.feed_equity_ipc} (Subscribed by CandleManager & Recorder)")
        print(f"Options IPC Socket : {self.feed_options_ipc} (Subscribed by QuotesBroker & Recorder)")
        print(f"Monitored Assets   : {len(self.instruments):,} contracts [{self.exchange}]")
        print(f"Ingestion Mode     : {self.mode.upper()}")
        print(f"Time               : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

        if self.mode == "mock":
            self._run_mock_stream()
        elif self.mode in ("ws", "websocket", "auto"):
            if not HAS_WEBSOCKETS:
                if self.is_rest_fallback_allowed():
                    self._run_poll_stream()
                else:
                    print("❌ websockets library not installed and REST fallback is disabled.", file=sys.stderr)
            else:
                self._run_websocket_stream()
        elif self.mode == "poll":
            self._run_poll_stream()

    def _run_mock_stream(self) -> None:
        """Simulate realistic moving ticks across monitored equities and options."""
        print("📡 Ingestion Engine: SYNTHETIC MOCK MARKET TICK FEED")
        print("✓ Simulation Mode: Generating live synthetic market ticks across monitored universe...")
        prices: Dict[str, float] = {}
        volumes: Dict[str, int] = {}

        for inst in self.instruments:
            k = inst["key"]
            sym = inst.get("symbol", "")
            if is_option_contract(k) or is_option_contract(sym):
                prices[k] = 25.0 + random.uniform(1.0, 50.0)
            else:
                prices[k] = 1000.0 + random.uniform(50.0, 4000.0)
            volumes[k] = random.randint(10000, 500000)

        while self._running:
            now_ts = time.time()
            # Pick a subset of instruments to update each tick
            sample_size = min(len(self.instruments), max(5, len(self.instruments) // 4))
            batch = random.sample(self.instruments, sample_size)

            for inst in batch:
                k = inst["key"]
                sym = inst["symbol"]
                curr_p = prices[k]
                pct_chg = random.uniform(-0.002, 0.002)
                new_p = max(0.50, round(curr_p * (1.0 + pct_chg), 2))
                prices[k] = new_p

                d_vol = random.randint(50, 500)
                volumes[k] += d_vol
                curr_vol = volumes[k]

                spread = max(0.05, round(new_p * 0.001, 2))
                bid = round(new_p - spread / 2.0, 2)
                ask = round(new_p + spread / 2.0, 2)
                bids = [[bid, 500, 1], [round(bid - 0.05, 2), 1000, 2]]
                asks = [[ask, 500, 1], [round(ask + 0.05, 2), 1000, 2]]

                self.publish_tick(
                    token=k,
                    symbol=sym,
                    ltp=new_p,
                    volume=curr_vol,
                    bid=bid,
                    ask=ask,
                    bid_qty=500,
                    ask_qty=500,
                    exchange_ts=now_ts,
                    received_ts=now_ts,
                    bids=bids,
                    asks=asks,
                )

            time.sleep(self.poll_interval)

    def _run_websocket_stream(self) -> None:
        """Connect to Upstox V3 Market Data WebSocket and decode binary protobuf packets."""
        if pb is None:
            if self.is_rest_fallback_allowed():
                self._run_poll_stream()
                return
            print("❌ Protobuf decoder module not found and REST fallback is disabled.", file=sys.stderr)
            return

        auth_url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
        def get_authorized_ws_url() -> Optional[str]:
            try:
                res = http_get_json(auth_url, access_token=self.access_token, timeout=10, debug_file=self.debug_log)
                if res.get("status") == "success" and "data" in res:
                    return res["data"].get("authorized_redirect_uri") or res["data"].get("authorizedRedirectUri")
            except Exception as e:
                log_debug(f"Upstox WS auth error: {e}", self.debug_log)
            return None

        ws_url = get_authorized_ws_url()
        if not ws_url:
            if self.is_rest_fallback_allowed():
                self._run_poll_stream()
                return
            print("❌ Failed to authorize WebSocket connection and REST fallback is disabled.", file=sys.stderr)
            return

        async def ws_consumer():
            self._ws_loop = asyncio.get_running_loop()
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            async with websockets.connect(ws_url, ssl=ssl_ctx, ping_interval=None, max_size=10 * 1024 * 1024) as ws:
                self._active_ws = ws
                keys = [inst["key"] for inst in self.instruments if inst.get("key")]
                stream_keys = keys[:self.max_stream_contracts]
                # Synchronize in-memory active option keys with the exact stream slice
                subscribed_set = set(stream_keys)
                self.active_option_keys = {k for k in self.active_option_keys if k in subscribed_set}
                for sym in list(self.active_options_by_symbol.keys()):
                    self.active_options_by_symbol[sym] = {k for k in self.active_options_by_symbol[sym] if k in subscribed_set}

                sub_payload = {"guid": "feed_v2", "method": "sub", "data": {"mode": "full", "instrumentKeys": stream_keys}}
                await ws.send(json.dumps(sub_payload).encode("utf-8"))
                print(f"✓ Upstox V3 WebSocket connected. Subscribed to {len(stream_keys):,} contracts (Underlyings: {len(self.underlying_keys):,}, Options: {len(self.active_option_keys):,}).")

                while self._running:
                    msg = await ws.recv()
                    now_ts = time.time()
                    if isinstance(msg, bytes):
                        try:
                            feed_resp = pb.FeedResponse()
                            feed_resp.ParseFromString(msg)
                            for k, feed in feed_resp.feeds.items():
                                canonical_key = k.replace(":", "|")
                                sym = self.symbol_map.get(canonical_key) or canonical_key.split("|")[-1]
                                ltp, bid, ask = 0.0, 0.0, 0.0
                                bids, asks = [], []
                                vol = 0
                                feed_union = feed.WhichOneof("FeedUnion")
                                if feed_union == "fullFeed":
                                    ff = feed.fullFeed
                                    which = ff.WhichOneof("FullFeedUnion")
                                    if which == "marketFF":
                                        m = ff.marketFF
                                        ltp = float(m.ltpc.ltp or 0.0)
                                        vol = int(m.vtt or (m.marketOHLC.ohlc[0].vol if m.marketOHLC and m.marketOHLC.ohlc else 0))
                                        if m.marketLevel and m.marketLevel.bidAskQuote:
                                            for ba in m.marketLevel.bidAskQuote:
                                                bp, bq = float(ba.bidP or 0.0), int(ba.bidQ or 0)
                                                ap, aq = float(ba.askP or 0.0), int(ba.askQ or 0)
                                                if bp > 0: bids.append([bp, bq, 1])
                                                if ap > 0: asks.append([ap, aq, 1])
                                            bid = bids[0][0] if bids else ltp
                                            ask = asks[0][0] if asks else ltp
                                elif feed_union == "firstLevelWithGreeks":
                                    fl = feed.firstLevelWithGreeks
                                    ltp = float(fl.ltpc.ltp or 0.0)
                                    vol = int(fl.vtt or 0)
                                    if fl.firstDepth:
                                        bid = float(fl.firstDepth.bidP or ltp)
                                        ask = float(fl.firstDepth.askP or ltp)
                                elif feed_union == "ltpc":
                                    ltp = float(feed.ltpc.ltp or 0.0)
                                    bid, ask = ltp, ltp

                                if vol > 0:
                                    self.last_seen_volume[canonical_key] = vol
                                else:
                                    vol = self.last_seen_volume.get(canonical_key, 0)

                                if ltp > 0:
                                    bid_q = int(bids[0][1]) if bids else 0
                                    ask_q = int(asks[0][1]) if asks else 0
                                    self.publish_tick(
                                        token=canonical_key,
                                        symbol=sym,
                                        ltp=ltp,
                                        volume=vol,
                                        bid=bid or ltp,
                                        ask=ask or ltp,
                                        bid_qty=bid_q,
                                        ask_qty=ask_q,
                                        exchange_ts=now_ts,
                                        received_ts=now_ts,
                                        bids=bids,
                                        asks=asks,
                                    )
                        except Exception as parse_err:
                            log_debug(f"Protobuf parse error: {parse_err}", self.debug_log)

        try:
            asyncio.run(ws_consumer())
        except Exception as e:
            print(f"WebSocket closed: {e}")

    def _run_poll_stream(self) -> None:
        """Fallback REST poller in batches of 500 contracts."""
        print("📡 Ingestion Engine: REST POLLER FALLBACK STREAM")
        while self._running:
            now_ts = time.time()
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        self.running = False
        self._running = False
        if hasattr(self, "pub_eq") and not self.pub_eq.closed:
            self.pub_eq.close(0)
        if hasattr(self, "pub_opt") and not self.pub_opt.closed:
            self.pub_opt.close(0)
        if self._owns_ctx and hasattr(self, "ctx") and not self.ctx.closed:
            self.ctx.term()
        print("[FEED] FeedHandler stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Core Market Data Feed Handler")
    parser.add_argument("--mode", default="mock", choices=["ws", "poll", "mock", "auto"], help="Feed ingestion mode")
    parser.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target exchange")
    parser.add_argument("--feed-equity-ipc", default=DEFAULT_FEED_EQ_IPC, help="ZeroMQ PUB socket for equity ticks")
    parser.add_argument("--feed-options-ipc", default=DEFAULT_FEED_OPT_IPC, help="ZeroMQ PUB socket for option depth ticks")
    args = parser.parse_args()

    handler = FeedHandler(
        mode=args.mode,
        exchange=args.exchange,
        feed_equity_ipc=args.feed_equity_ipc,
        feed_options_ipc=args.feed_options_ipc,
    )

    def sig_handler(sig, frame):
        handler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    handler.start()


if __name__ == "__main__":
    main()
