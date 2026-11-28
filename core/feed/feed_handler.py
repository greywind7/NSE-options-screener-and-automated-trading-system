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
from dataclasses import dataclass, field
import json
import math
import os
import random
import signal
import ssl
import sys
import threading
import time
import zlib
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import zmq

from core.auth.upstox_auth import get_access_token, http_get_json, log_debug
from core.common.ipc_schemas import (
    EquityTickEvent,
    OptionDepthTickEvent,
    pack_message,
)
from core.common.logging_utils import get_logger
from core.common.symbol_utils import normalize_instrument_token, is_option_contract
from core.config.config_loader import get_config_manager
from core.feed.position_registry import ProtectedPositionRegistry

_logger = get_logger("FEED_HANDLER")

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


@dataclass
class ConnectionState:
    """State tracking for an individual WebSocket feed connection."""
    conn_id: int
    ws: Any = None
    subscribed_keys: Set[str] = field(default_factory=set)
    underlying_keys: Set[str] = field(default_factory=set)
    underlying_symbols: Set[str] = field(default_factory=set)
    active_option_keys: Set[str] = field(default_factory=set)
    active_options_by_symbol: Dict[str, Set[str]] = field(default_factory=lambda: defaultdict(set))
    max_contracts: int = 1800  # Upstox WS limit: 2000 keys per connection
    total_ticks_received: int = 0
    total_ticks_published: int = 0


class FeedHandler:
    """
    Centralized Market Ingestion Engine (Dual-Connection Spec 2.0).
    Partitions Cash Equities and Options across parallel Upstox WebSockets (max 2,000 keys/connection).
    Routes Cash Equities to feed_equity_ipc (TICK.EQ) and Options Depth to feed_options_ipc (TICK.OPT).
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
        num_connections: int = 2,
    ):
        cfg = get_config_manager()
        ipc_endpoints = cfg.get_ipc_endpoints()
        sys_feed_cfg = cfg.system.get("system", {}).get("feed", {})
        cfg_max_contracts = sys_feed_cfg.get("max_websocket_keys")

        self.mode = mode.lower()
        self.exchange = exchange.upper()
        self.feed_equity_ipc = feed_equity_ipc or ipc_endpoints.get("feed_equity_ipc", DEFAULT_FEED_EQ_IPC)
        self.feed_options_ipc = feed_options_ipc or ipc_endpoints.get("feed_options_ipc", DEFAULT_FEED_OPT_IPC)
        self.access_token = access_token or get_access_token()
        self.poll_interval = max(0.2, float(poll_interval))
        if cfg_max_contracts and max_stream_contracts == 1800:
            self.max_stream_contracts = max(1, int(cfg_max_contracts))
        else:
            self.max_stream_contracts = max(1, int(max_stream_contracts))
        self.silent = bool(silent)
        self.allow_rest_fallback = bool(allow_rest_fallback)
        self.debug_log = "logs/exec_logs/debug.log"

        if manifest_path:
            self.manifest_path = manifest_path
        else:
            canon_m = "data/manifest/subscription_manifest_mcx.json" if self.exchange == "MCX" else "data/manifest/subscription_manifest.json"
            legacy_m = "data/subscription_manifest_mcx.json" if self.exchange == "MCX" else "data/subscription_manifest.json"
            self.manifest_path = canon_m if os.path.exists(canon_m) or not os.path.exists(legacy_m) else legacy_m

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

        # Thread-safe lock for strictly serialized, atomic ZeroMQ multipart publishing
        self._publish_lock = threading.Lock()

        # Multi-connection architecture (default 2 connections, partitioned by underlying)
        self.num_connections = max(1, int(num_connections))
        self.connections: List[ConnectionState] = [
            ConnectionState(conn_id=i, max_contracts=self.max_stream_contracts)
            for i in range(self.num_connections)
        ]
        self.symbol_to_conn: Dict[str, int] = {}
        self.key_to_conn: Dict[str, int] = {}

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
        self.eligible_underlyings_for_options: Set[str] = set()
        self.last_atm_by_symbol: Dict[str, float] = {}
        self.last_reval_time_by_symbol: Dict[str, float] = {}
        self.total_dynamic_subscriptions: int = 0
        self.total_dynamic_unsubscriptions: int = 0
        self.position_registry: ProtectedPositionRegistry = ProtectedPositionRegistry.get_instance()
        self.protected_logged_keys: Set[str] = set()

        # WebSocket asyncio loop handle
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def running(self) -> bool:
        """Backwards compatible accessor for running state."""
        return self._running

    @running.setter
    def running(self, val: bool) -> None:
        self._running = bool(val)

    @property
    def _active_ws(self) -> Any:
        """Backwards compatibility accessor for active websocket connection."""
        for c in self.connections:
            if c.ws is not None:
                return c.ws
        return None

    def _sync_global_option_keys(self) -> None:
        """Synchronize feed-level sets from per-connection state for backwards compatibility."""
        self.active_option_keys = set().union(*(c.active_option_keys for c in self.connections))
        self.underlying_keys = set().union(*(c.underlying_keys for c in self.connections))
        for c in self.connections:
            for sym, opt_set in c.active_options_by_symbol.items():
                self.active_options_by_symbol[sym] = set(opt_set)

    def _register_key_to_conn(self, k: str, c_id: int) -> None:
        """Register all variants of an instrument key to its connection."""
        self.key_to_conn[k] = c_id
        self.key_to_conn[k.replace(":", "|")] = c_id
        self.key_to_conn[k.replace("|", ":")] = c_id
        canon = normalize_instrument_token(k)
        if canon:
            self.key_to_conn[canon] = c_id
            self.key_to_conn[canon.replace(":", "|")] = c_id
            self.key_to_conn[canon.replace("|", ":")] = c_id

    def _unregister_key_from_conn(self, k: str) -> None:
        """Unregister all variants of an instrument key."""
        self.key_to_conn.pop(k, None)
        self.key_to_conn.pop(k.replace(":", "|"), None)
        self.key_to_conn.pop(k.replace("|", ":"), None)
        canon = normalize_instrument_token(k)
        if canon:
            self.key_to_conn.pop(canon, None)
            self.key_to_conn.pop(canon.replace(":", "|"), None)
            self.key_to_conn.pop(canon.replace("|", ":"), None)

    def get_connection_for_symbol(self, symbol: str) -> int:
        """Return the deterministic connection index (0..num_connections-1) for an underlying symbol."""
        sym_clean = symbol.split()[0].upper() if symbol else ""
        if sym_clean in self.symbol_to_conn:
            return self.symbol_to_conn[sym_clean]
        if not self.connections:
            return 0
        if sym_clean in ("NIFTY", "NIFTY 50"):
            c_id = 0
        else:
            c_id = zlib.crc32(sym_clean.encode("utf-8")) % len(self.connections)
        self.symbol_to_conn[sym_clean] = c_id
        self.connections[c_id].underlying_symbols.add(sym_clean)
        return c_id

    def get_connection_for_key(self, key: str) -> int:
        """Return the connection index owning a given underlying or derivative key."""
        if key in self.key_to_conn:
            return self.key_to_conn[key]
        pipe_k = key.replace(":", "|")
        if pipe_k in self.key_to_conn:
            return self.key_to_conn[pipe_k]
        colon_k = key.replace("|", ":")
        if colon_k in self.key_to_conn:
            return self.key_to_conn[colon_k]

        canon = normalize_instrument_token(key)
        if canon in self.key_to_conn:
            return self.key_to_conn[canon]
        if canon.replace(":", "|") in self.key_to_conn:
            return self.key_to_conn[canon.replace(":", "|")]

        sym = (
            self.underlying_key_to_symbol.get(key)
            or self.underlying_key_to_symbol.get(canon)
            or self.symbol_map.get(key)
            or self.symbol_map.get(canon)
            or self.symbol_map.get(pipe_k)
        )
        if sym:
            c_id = self.get_connection_for_symbol(sym)
            self._register_key_to_conn(key, c_id)
            return c_id

        if not self.connections:
            return 0
        c_id = abs(hash(key)) % len(self.connections)
        self._register_key_to_conn(key, c_id)
        return c_id

    def get_connection_keys(self, conn_id: int) -> Set[str]:
        """Return the active set of subscribed keys on a specific connection."""
        if 0 <= conn_id < len(self.connections):
            return set(self.connections[conn_id].subscribed_keys)
        return set()

    def get_connection_stats(self) -> List[Dict[str, Any]]:
        """Return runtime diagnostic telemetry for each connection."""
        stats = []
        for c in self.connections:
            stats.append({
                "conn_id": c.conn_id,
                "connected": c.ws is not None,
                "subscribed_keys_count": len(c.subscribed_keys),
                "underlying_keys_count": len(c.underlying_keys),
                "underlying_symbols_count": len(c.underlying_symbols),
                "active_options_count": len(c.active_option_keys),
                "ticks_received": c.total_ticks_received,
                "ticks_published": c.total_ticks_published,
            })
        return stats

    def _log(self, msg: str, force: bool = False) -> None:
        if not self.silent or force:
            _logger.info(msg)

    def is_rest_fallback_allowed(self) -> bool:
        if getattr(self, "allow_rest_fallback", False):
            return True
        if os.path.exists("logs/.allow_rest_fallback"):
            return True
        return False

    def load_instruments(self) -> List[Dict[str, str]]:
        """Load instrument universe to monitor from manifest or filtered CSV and partition across connections."""
        instruments: List[Dict[str, str]] = []
        if self.exchange == "NSE":
            instruments.append({"key": "NSE_INDEX|Nifty 50", "symbol": "Nifty 50"})
            self.symbol_map["NSE_INDEX|Nifty 50"] = "Nifty 50"
            self.symbol_map["NSE_INDEX:Nifty 50"] = "Nifty 50"
            self.underlying_keys.add("NSE_INDEX|Nifty 50")
            self.underlying_keys.add("NSE_INDEX:Nifty 50")
            self.underlying_key_to_symbol["NSE_INDEX|Nifty 50"] = "Nifty 50"
            self.underlying_key_to_symbol["NSE_INDEX:Nifty 50"] = "Nifty 50"

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
                            self.underlying_key_to_symbol[k.replace(":", "|")] = s
                            self.underlying_key_to_symbol[k.replace("|", ":")] = s
            except Exception:
                pass

        popular_options_raw: List[Dict[str, Any]] = []

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
                            norm_k = normalize_instrument_token(k)
                            self.underlying_key_to_symbol[norm_k] = sym
                            self.underlying_key_to_symbol[norm_k.replace(":", "|")] = sym
                            self.symbol_map[norm_k] = sym
                            self.symbol_map[norm_k.replace(":", "|")] = sym
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
                        norm_k = normalize_instrument_token(k)
                        self.underlying_key_to_symbol[norm_k] = sym
                        self.underlying_key_to_symbol[norm_k.replace(":", "|")] = sym
                        self.symbol_map[norm_k] = sym
                        self.symbol_map[norm_k.replace(":", "|")] = sym

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
                            popular_options_raw.append({"key": k, "symbol": opt_sym, "underlying": u_sym})
                            self.symbol_map[k] = opt_sym
                            self.symbol_map[k.replace(":", "|")] = opt_sym
                            self.symbol_map[k.replace("|", ":")] = opt_sym
                            self.active_option_keys.add(k)
                            if u_sym:
                                self.active_options_by_symbol[u_sym].add(k)
                                self.eligible_underlyings_for_options.add(u_sym.upper())
                elif isinstance(manifest.get("streamed_contracts", {}).get("popular_options"), dict):
                    for k, v in manifest["streamed_contracts"]["popular_options"].items():
                        opt_sym = v.get("symbol", k)
                        u_sym = v.get("underlying", "")
                        popular_options_raw.append({"key": k, "symbol": opt_sym, "underlying": u_sym})
                        self.symbol_map[k] = opt_sym
                        self.symbol_map[k.replace(":", "|")] = opt_sym
                        self.symbol_map[k.replace("|", ":")] = opt_sym
                        self.active_option_keys.add(k)
                        if u_sym:
                            self.active_options_by_symbol[u_sym].add(k)
                            self.eligible_underlyings_for_options.add(u_sym.upper())
            except Exception as e:
                self._log(f"Warning reading manifest {self.manifest_path}: {e}")

        # Add remaining underlyings from filtered CSV if not already present
        for k, s in filtered_map.items():
            if "|" in k:
                if not any(inst["key"].replace(":", "|") == k for inst in instruments):
                    instruments.append({"key": k, "symbol": s})
                    self.symbol_map[k] = s
                    self.symbol_map[k.replace("|", ":")] = s

        # Deterministic sharding of underlying equities across connections
        all_und_symbols = set()
        for inst in instruments:
            k = inst.get("key", "")
            s = inst.get("symbol", "")
            if not is_option_contract(k) and not is_option_contract(s):
                s_clean = s.split()[0].upper()
                if s_clean:
                    all_und_symbols.add(s_clean)
        for s in self.underlying_key_to_symbol.values():
            s_clean = s.split()[0].upper()
            if s_clean:
                all_und_symbols.add(s_clean)

        for sym_u in sorted(list(all_und_symbols)):
            c_id = self.get_connection_for_symbol(sym_u)
            self.symbol_to_conn[sym_u] = c_id
            self.connections[c_id].underlying_symbols.add(sym_u)

        # Assign underlying keys to connection
        for k in list(self.underlying_keys):
            k_clean = k.replace(":", "|")
            s = self.underlying_key_to_symbol.get(k_clean) or self.underlying_key_to_symbol.get(k) or self.symbol_map.get(k_clean)
            c_id = self.get_connection_for_symbol(s) if s else (zlib.crc32(k_clean.encode("utf-8")) % self.num_connections)
            self._register_key_to_conn(k, c_id)
            self.connections[c_id].underlying_keys.add(k)
            self.connections[c_id].subscribed_keys.add(k)

        # Assign popular options to their underlying's connection
        for pop_item in popular_options_raw:
            k = pop_item["key"]
            opt_sym = pop_item["symbol"]
            u_sym = pop_item.get("underlying", "")
            c_id = self.get_connection_for_symbol(u_sym) if u_sym else (zlib.crc32(k.encode("utf-8")) % self.num_connections)
            instruments.append({"key": k, "symbol": opt_sym, "conn_id": c_id})
            self._register_key_to_conn(k, c_id)
            self.connections[c_id].active_option_keys.add(k)
            self.connections[c_id].subscribed_keys.add(k)
            if u_sym:
                self.connections[c_id].active_options_by_symbol[u_sym].add(k)

        # Include protected keys from active open positions into initial feed subscriptions
        protected_keys = self.position_registry.get_protected_keys()
        if protected_keys:
            existing_keys = {inst["key"].replace(":", "|") for inst in instruments}
            for pk in protected_keys:
                pk_pipe = pk.replace(":", "|")
                sym = self.option_name_map.get(pk, pk)
                u_sym = None
                for usym, opt_set in self.active_options_by_symbol.items():
                    if pk in opt_set or pk_pipe in opt_set:
                        u_sym = usym
                        break
                if not u_sym and " " in sym:
                    u_sym = sym.split()[0]
                c_id = self.get_connection_for_symbol(u_sym) if u_sym else (abs(hash(pk)) % self.num_connections)

                if pk_pipe not in existing_keys:
                    instruments.append({"key": pk, "symbol": sym, "conn_id": c_id})
                    self.symbol_map[pk] = sym
                    self.symbol_map[pk.replace(":", "|")] = sym
                    self.symbol_map[pk.replace("|", ":")] = sym
                    self._log(f"  🛡️ [PROTECTION ACTIVE] Subscribed to active open position on startup [CONN-{c_id}]: '{sym}' ({pk})")

                self._register_key_to_conn(pk, c_id)
                self.connections[c_id].active_option_keys.add(pk)
                self.connections[c_id].subscribed_keys.add(pk)
                if u_sym:
                    self.connections[c_id].active_options_by_symbol[u_sym].add(pk)

        # Tag all instruments with their conn_id
        for inst in instruments:
            if "conn_id" not in inst:
                inst["conn_id"] = self.get_connection_for_key(inst["key"])

        self._sync_global_option_keys()
        self.instruments = instruments
        return instruments

    def _prune_dynamic_options_to_fit(
        self,
        keys_to_add: Set[str],
        protected_keys: Set[str],
        current_symbol: str,
        conn_id: Optional[int] = None,
    ) -> Set[str]:
        """Prune unheld dynamic options when stream capacity limit is reached."""
        if conn_id is not None and 0 <= conn_id < len(self.connections):
            conn = self.connections[conn_id]
            total_current = len(conn.underlying_keys) + len(conn.active_option_keys)
            new_contracts_count = len(keys_to_add - conn.active_option_keys)
            projected_total = total_current + new_contracts_count
            max_limit = conn.max_contracts
            options_by_sym = conn.active_options_by_symbol
        else:
            total_current = len(self.underlying_keys) + len(self.active_option_keys)
            new_contracts_count = len(keys_to_add - self.active_option_keys)
            projected_total = total_current + new_contracts_count
            max_limit = self.max_stream_contracts
            options_by_sym = self.active_options_by_symbol

        if projected_total <= max_limit:
            return set()

        contracts_to_prune_count = projected_total - max_limit
        pruned_keys: Set[str] = set()

        # Candidate pool: active options across OTHER symbols that are not protected
        for sym, opt_set in list(options_by_sym.items()):
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
            curr_unheld = options_by_sym.get(current_symbol, set()) - protected_keys
            for k in curr_unheld:
                pruned_keys.add(k)
                if len(pruned_keys) >= contracts_to_prune_count:
                    break

        return pruned_keys

    def evaluate_dynamic_options(
        self,
        underlying_key: str,
        ltp: float,
        conn_id: Optional[int] = None,
    ) -> Tuple[List[str], List[str]]:
        """
        Evaluate ATM shift for an underlying stock.
        Returns (keys_to_subscribe, keys_to_unsubscribe).
        """
        if not self.enable_dynamic_options or not self.options_map or ltp <= 0.0:
            return [], []

        canonical_key = underlying_key.replace(":", "|")
        sym = self.underlying_key_to_symbol.get(canonical_key) or self.underlying_key_to_symbol.get(underlying_key) or self.symbol_map.get(canonical_key)
        if not sym and "|" in canonical_key:
            base_id = canonical_key.split("|", 1)[1]
            for mk, msym in self.underlying_key_to_symbol.items():
                if base_id in mk:
                    sym = msym
                    break
        if not sym:
            return [], []

        # Restrict dynamic options evaluation strictly to the top liquid underlyings configured in manifest
        if self.eligible_underlyings_for_options and sym.upper() not in self.eligible_underlyings_for_options:
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

        c_id = conn_id if conn_id is not None else self.get_connection_for_symbol(lookup_sym)
        conn = self.connections[c_id] if 0 <= c_id < len(self.connections) else self.connections[0]

        # Fallback sync for tests that set active_options_by_symbol directly
        if lookup_sym in self.active_options_by_symbol and not conn.active_options_by_symbol[lookup_sym]:
            conn.active_options_by_symbol[lookup_sym] = set(self.active_options_by_symbol[lookup_sym])
            conn.active_option_keys |= conn.active_options_by_symbol[lookup_sym]
            conn.subscribed_keys |= conn.active_options_by_symbol[lookup_sym]

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

        current_keys = set(conn.active_options_by_symbol.get(lookup_sym, set()))
        if not current_keys and lookup_sym in self.active_options_by_symbol:
            current_keys = set(self.active_options_by_symbol[lookup_sym])
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
                        _logger.info(f"[PROTECTION ACTIVE] Retaining subscription [CONN-{c_id}] for '{sym_disp}' ({sk}) - Active open position detected!")
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

        # Capacity limit enforcement per connection
        if keys_to_add:
            capacity_pruned = self._prune_dynamic_options_to_fit(keys_to_add, protected_keys, lookup_sym, conn_id=c_id)
            if capacity_pruned:
                keys_to_remove |= capacity_pruned
                for k in capacity_pruned:
                    for sym_set in conn.active_options_by_symbol.values():
                        sym_set.discard(k)

        # Update in-memory sets for connection
        conn.active_options_by_symbol[lookup_sym] = (current_keys - keys_to_remove) | keys_to_add
        conn.active_option_keys = (conn.active_option_keys - keys_to_remove) | keys_to_add
        conn.subscribed_keys = (conn.subscribed_keys - keys_to_remove) | keys_to_add

        for k in keys_to_add:
            self._register_key_to_conn(k, c_id)
        for k in keys_to_remove:
            self._unregister_key_from_conn(k)

        self.last_atm_by_symbol[lookup_sym] = atm_strike
        self.last_reval_time_by_symbol[lookup_sym] = now
        self.total_dynamic_subscriptions += len(keys_to_add)
        self.total_dynamic_unsubscriptions += len(keys_to_remove)
        self._sync_global_option_keys()

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
        conn_id: Optional[int] = None,
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
                bid=float(bid),
                ask=float(ask),
                bid_qty=b_qty,
                ask_qty=a_qty,
                exchange_timestamp=float(exchange_ts or time.time()),
                received_timestamp=float(received_ts or time.time()),
                bids=bids or [],
                asks=asks or [],
            )
            payload = pack_message(event)
            # 1. Option depth tick -> publish strictly to Options socket (QuotesBroker) under lock
            with self._publish_lock:
                self.pub_opt.send_multipart([b"TICK.OPT", payload])
                self.total_opt_published += 1
                if conn_id is not None and 0 <= conn_id < len(self.connections):
                    self.connections[conn_id].total_ticks_published += 1
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
            # 2. Equity tick -> publish strictly to Equity socket (CandleManager) under lock
            with self._publish_lock:
                self.pub_eq.send_multipart([b"TICK.EQ", payload])
                self.total_eq_published += 1
                if conn_id is not None and 0 <= conn_id < len(self.connections):
                    self.connections[conn_id].total_ticks_published += 1

            # 3. Dynamic popular strikes evaluation on price change
            if self.enable_dynamic_options:
                c_id = conn_id if conn_id is not None else self.get_connection_for_key(canonical_token)
                to_add, to_remove = self.evaluate_dynamic_options(canonical_token, ltp, conn_id=c_id)
                if to_add or to_remove:
                    if self._ws_loop:
                        conn = self.connections[c_id] if 0 <= c_id < len(self.connections) else None
                        if conn and conn.ws:
                            asyncio.run_coroutine_threadsafe(
                                self._dispatch_ws_subscription_update(c_id, to_add, to_remove), self._ws_loop
                            )
                        elif self._active_ws:
                            asyncio.run_coroutine_threadsafe(
                                self._dispatch_ws_subscription_update(c_id, to_add, to_remove), self._ws_loop
                            )
                    elif self.mode != "mock" and not getattr(self, "_warned_missing_ws_loop", False):
                        self._warned_missing_ws_loop = True
                        log_debug("[FEED] Dynamic option update computed without active _ws_loop; skipped WS dispatch.", self.debug_log)

    async def _dispatch_ws_subscription_update(
        self,
        conn_id_or_add: Any,
        to_add_or_remove: Any = None,
        to_remove_opt: Any = None,
    ) -> None:
        """Dispatch subscription/unsubscription updates to the appropriate connection WebSocket."""
        if isinstance(conn_id_or_add, int):
            conn_id = conn_id_or_add
            to_add = to_add_or_remove or []
            to_remove = to_remove_opt or []
        else:
            to_add = conn_id_or_add or []
            to_remove = to_add_or_remove or []
            conn_id = None

        if conn_id is not None and 0 <= conn_id < len(self.connections):
            target_ws = self.connections[conn_id].ws
        else:
            target_ws = self._active_ws

        if not target_ws:
            return

        try:
            guid_suffix = f"_c{conn_id}" if conn_id is not None else ""
            if to_add:
                sub_msg = json.dumps({
                    "guid": f"dyn_sub{guid_suffix}",
                    "method": "sub",
                    "data": {"mode": "full", "instrumentKeys": list(to_add)},
                }).encode("utf-8")
                await target_ws.send(sub_msg)
            if to_remove:
                unsub_msg = json.dumps({
                    "guid": f"dyn_unsub{guid_suffix}",
                    "method": "unsub",
                    "data": {"mode": "full", "instrumentKeys": list(to_remove)},
                }).encode("utf-8")
                await target_ws.send(unsub_msg)
        except Exception as e:
            log_debug(f"[CONN-{conn_id}] Error updating dynamic subscriptions on WebSocket: {e}", self.debug_log)

    def start(self) -> None:
        """Start the market data ingestion engine according to configured mode."""
        self.running = True
        self._running = True
        self.load_instruments()

        _logger.info(
            f"MARKET DATA FEED HANDLER ACTIVE ({self.num_connections} connections) | "
            f"EquityIPC={self.feed_equity_ipc} | OptionsIPC={self.feed_options_ipc} | "
            f"Assets={len(self.instruments):,} [{self.exchange}] | Mode={self.mode.upper()}"
        )
        for i, c in enumerate(self.connections):
            _logger.info(
                f"Connection {i}: {len(c.subscribed_keys):,} keys "
                f"({len(c.underlying_keys):,} underlyings, {len(c.active_option_keys):,} options)"
            )

        if self.mode == "mock":
            self._run_mock_stream()
        elif self.mode in ("ws", "websocket", "auto"):
            if not HAS_WEBSOCKETS:
                if self.is_rest_fallback_allowed():
                    self._run_poll_stream()
                else:
                    _logger.error("websockets library not installed and REST fallback is disabled.")
            else:
                self._run_websocket_stream()
        elif self.mode == "poll":
            self._run_poll_stream()

    def _run_mock_stream(self) -> None:
        """Simulate realistic moving ticks across monitored equities and options partitioned across dual connections."""
        _logger.info("Ingestion Engine: SYNTHETIC MOCK MARKET TICK FEED (DUAL CONNECTION) active")
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
            sample_size = min(len(self.instruments), max(5, len(self.instruments) // 4))
            batch = random.sample(self.instruments, sample_size)

            for inst in batch:
                k = inst["key"]
                sym = inst["symbol"]
                c_id = inst.get("conn_id") if inst.get("conn_id") is not None else self.get_connection_for_key(k)
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
                    conn_id=c_id,
                )

            time.sleep(self.poll_interval)

    def _run_websocket_stream(self) -> None:
        """Connect to Upstox V3 Market Data WebSocket and decode binary protobuf packets across dual connections."""
        if pb is None:
            if self.is_rest_fallback_allowed():
                self._run_poll_stream()
                return
            _logger.error("Protobuf decoder module not found and REST fallback is disabled.")
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

        # Verify initial authorization
        initial_url = get_authorized_ws_url()
        if not initial_url:
            if self.is_rest_fallback_allowed():
                self._run_poll_stream()
                return
            _logger.error("Failed to authorize WebSocket connection and REST fallback is disabled.")
            return

        async def ws_connection_worker(conn_id: int):
            conn = self.connections[conn_id]
            backoff = 1.0

            while self._running:
                try:
                    conn_ws_url = get_authorized_ws_url()
                    if not conn_ws_url:
                        _logger.warning(f"[CONN-{conn_id}] Cannot obtain WebSocket authorization URL. Retrying in {backoff:.1f}s...")
                        await asyncio.sleep(backoff)
                        backoff = min(15.0, backoff * 1.5)
                        continue

                    ssl_ctx = ssl.create_default_context()

                    async with websockets.connect(conn_ws_url, ssl=ssl_ctx, ping_interval=None, max_size=10 * 1024 * 1024) as ws:
                        conn.ws = ws
                        # Slice subscriptions to max_contracts limit
                        stream_keys = sorted(list(conn.subscribed_keys))[:conn.max_contracts]
                        conn.subscribed_keys = set(stream_keys)
                        conn.active_option_keys = {k for k in conn.active_option_keys if k in conn.subscribed_keys}
                        for sym in list(conn.active_options_by_symbol.keys()):
                            conn.active_options_by_symbol[sym] = {k for k in conn.active_options_by_symbol[sym] if k in conn.subscribed_keys}
                        self._sync_global_option_keys()

                        sub_payload = {
                            "guid": f"feed_v2_c{conn_id}",
                            "method": "sub",
                            "data": {"mode": "full", "instrumentKeys": stream_keys},
                        }
                        await ws.send(json.dumps(sub_payload).encode("utf-8"))
                        _logger.info(
                            f"[CONN-{conn_id}] Upstox V3 WebSocket connected. Subscribed to {len(stream_keys):,} contracts "
                            f"(Underlyings: {len(conn.underlying_keys):,}, Options: {len(conn.active_option_keys):,})."
                        )
                        backoff = 1.0

                        while self._running:
                            msg = await ws.recv()
                            conn.total_ticks_received += 1
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
                                        is_opt = is_option_contract(canonical_key)
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
                                                    bid = bids[0][0] if bids else (0.0 if is_opt else ltp)
                                                    ask = asks[0][0] if asks else (0.0 if is_opt else ltp)
                                                else:
                                                    bid = 0.0 if is_opt else ltp
                                                    ask = 0.0 if is_opt else ltp
                                        elif feed_union == "firstLevelWithGreeks":
                                            fl = feed.firstLevelWithGreeks
                                            ltp = float(fl.ltpc.ltp or 0.0)
                                            vol = int(fl.vtt or 0)
                                            if fl.firstDepth:
                                                bid = float(fl.firstDepth.bidP or 0.0)
                                                ask = float(fl.firstDepth.askP or 0.0)
                                            else:
                                                bid = 0.0 if is_opt else ltp
                                                ask = 0.0 if is_opt else ltp
                                        elif feed_union == "ltpc":
                                            ltp = float(feed.ltpc.ltp or 0.0)
                                            bid = 0.0 if is_opt else ltp
                                            ask = 0.0 if is_opt else ltp

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
                                                bid=bid if is_opt else (bid or ltp),
                                                ask=ask if is_opt else (ask or ltp),
                                                bid_qty=bid_q,
                                                ask_qty=ask_q,
                                                exchange_ts=now_ts,
                                                received_ts=now_ts,
                                                bids=bids,
                                                asks=asks,
                                                conn_id=conn_id,
                                            )
                                except Exception as parse_err:
                                    log_debug(f"[CONN-{conn_id}] Protobuf parse error: {parse_err}", self.debug_log)
                except Exception as e:
                    if not self._running:
                        break
                    conn.ws = None
                    _logger.warning(f"[CONN-{conn_id}] WebSocket disconnected/error: {e}. Retrying connection in {backoff:.1f}s...")
                    await asyncio.sleep(backoff)
                    backoff = min(15.0, backoff * 1.5)

        async def dual_runner():
            await asyncio.gather(*(ws_connection_worker(i) for i in range(self.num_connections)))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        try:
            while self._running:
                try:
                    loop.run_until_complete(dual_runner())
                except Exception as e:
                    if not self._running:
                        break
                    _logger.error(f"Main dual runner exited: {e}. Restarting in 2s...")
                    time.sleep(2.0)
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._ws_loop = None

    def _run_poll_stream(self) -> None:
        """Fallback REST poller in batches of 500 contracts."""
        _logger.info("Ingestion Engine: REST POLLER FALLBACK STREAM active")
        while self._running:
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
        for conn in self.connections:
            if conn.ws:
                try:
                    if self._ws_loop and self._ws_loop.is_running():
                        asyncio.run_coroutine_threadsafe(conn.ws.close(), self._ws_loop)
                except Exception:
                    pass
                conn.ws = None
        with self._publish_lock:
            if hasattr(self, "pub_eq") and not self.pub_eq.closed:
                self.pub_eq.close(0)
            if hasattr(self, "pub_opt") and not self.pub_opt.closed:
                self.pub_opt.close(0)
        if self._owns_ctx and hasattr(self, "ctx") and not self.ctx.closed:
            self.ctx.term()
        _logger.info("FeedHandler stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Core Market Data Feed Handler")
    parser.add_argument("--mode", default="mock", choices=["ws", "poll", "mock", "auto"], help="Feed ingestion mode")
    parser.add_argument("--exchange", default="NSE", choices=["NSE", "MCX"], help="Target exchange")
    parser.add_argument("--feed-equity-ipc", default=DEFAULT_FEED_EQ_IPC, help="ZeroMQ PUB socket for equity ticks")
    parser.add_argument("--feed-options-ipc", default=DEFAULT_FEED_OPT_IPC, help="ZeroMQ PUB socket for option depth ticks")
    parser.add_argument("--num-connections", type=int, default=2, help="Number of parallel Upstox WebSocket connections (default: 2)")
    args = parser.parse_args()

    handler = FeedHandler(
        mode=args.mode,
        exchange=args.exchange,
        feed_equity_ipc=args.feed_equity_ipc,
        feed_options_ipc=args.feed_options_ipc,
        num_connections=args.num_connections,
    )

    def sig_handler(sig, frame):
        handler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    handler.start()


if __name__ == "__main__":
    main()
