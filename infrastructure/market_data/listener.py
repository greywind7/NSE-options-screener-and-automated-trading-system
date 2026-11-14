#!/usr/bin/env python3
"""
infrastructure/market_data/listener.py
======================================
Centralized Market Data Listener Service:
Connects to Upstox Market Data feeds and broadcasts normalized ticks/depth
to independent subscribers (Strategies, Market Recorder, Dashboards) via local IPC.

Ingestion Modes:
1. Live WebSocket Mode (UPSTOX_ACCESS_TOKEN + websockets + protobuf):
   Calls GET https://api.upstox.com/v3/feed/market-data-feed/authorize,
   subscribes to manifest contracts, decodes binary protobuf frames into MarketTicks.
2. High-Speed REST Poller Mode (Fallback / Standard Library):
   Fetches real-time quotes in batches of 500 instruments via
   GET https://api.upstox.com/v2/market-quote/quotes?instrument_key=...
   Normalizes into MarketTicks and broadcasts across IPC bus.
3. Mock / Demo Mode (--mode mock):
   Generates synthetic moving ticks, volume surges, and Nifty 50 regime updates
   for end-to-end strategy verification outside market hours or without credentials.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.auth.upstox_auth import get_access_token, http_get_json, log_debug
from infrastructure.market_data.event_schema import MarketTick, OptionDepth, SystemEvent
from infrastructure.market_data.position_registry import ProtectedPositionRegistry
from infrastructure.market_data.publisher import DEFAULT_IPC_PATH, MarketDataPublisher


class MarketDataListener:
    """
    Independent service that ingests market data from Upstox (or Mock Feeder)
    and publishes normalized events to the local IPC bus.
    """

    def __init__(
        self,
        mode: str = "auto",                  # "auto", "ws", "poll", "mock"
        exchange: str = "NSE",               # "NSE" or "MCX"
        access_token: Optional[str] = None,
        ipc_path: str = DEFAULT_IPC_PATH,
        poll_interval: float = 1.0,
        manifest_path: Optional[str] = None,
        filtered_csv: Optional[str] = None,
        debug_log: Optional[str] = "logs/debug.log",
        enable_socket: bool = True,
        max_stream_contracts: int = 1800,
        silent: bool = False,
        allow_rest_fallback: bool = False,
    ):
        self.mode = mode.lower()
        self.exchange = exchange.upper()
        self.access_token = access_token or get_access_token()
        self.ipc_path = ipc_path
        self.poll_interval = max(0.2, float(poll_interval))
        self.max_stream_contracts = max(1, int(max_stream_contracts))
        self.silent = bool(silent)
        self.allow_rest_fallback = bool(allow_rest_fallback)

        if manifest_path:
            self.manifest_path = manifest_path
        else:
            self.manifest_path = "data/subscription_manifest_mcx.json" if self.exchange == "MCX" else "data/subscription_manifest.json"

        if filtered_csv:
            self.filtered_csv = filtered_csv
        else:
            self.filtered_csv = "data/filtered_mcx.csv" if self.exchange == "MCX" else "data/filtered.csv"

        self.debug_log = debug_log
        self.enable_socket = enable_socket

        self.publisher = MarketDataPublisher(address=ipc_path, enable_socket=enable_socket)
        self._running = False
        self.total_published = 0
        self.total_depth_published = 0
        self.last_seen_volume: Dict[str, int] = {}
        self.last_seen_ltp: Dict[str, float] = {}
        self.last_seen_depth: Dict[str, Any] = {}
        self.instruments: List[Dict[str, str]] = []  # [{"key": ..., "symbol": ...}]
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

    def _log(self, msg: str, force: bool = False) -> None:
        """Output console log message unless running in silent mode."""
        if not self.silent or force:
            print(msg)

    def is_rest_fallback_allowed(self) -> bool:
        """
        Check if fallback to REST polling is permitted when WebSocket fails or drops.
        Default is FALSE to ensure strategies never silently execute with suboptimal polling latency.
        Dynamically checks instance configuration and flag file 'logs/.allow_rest_fallback'.
        """
        if getattr(self, "allow_rest_fallback", False):
            return True
        if os.path.exists("logs/.allow_rest_fallback"):
            return True
        return False

    def load_instruments(self) -> List[Dict[str, str]]:
        """Load instrument universe to monitor from manifest or filtered CSV."""
        instruments: List[Dict[str, str]] = []

        # Include Nifty 50 only for NSE equity regime
        if self.exchange == "NSE":
            instruments.append({"key": "NSE_INDEX|Nifty 50", "symbol": "Nifty 50"})

        # Load filtered CSV mapping if present
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

        # 1. Try Subscription Manifest first
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    m = json.load(f)

                # Extract symbol mappings from symbol_metadata and instrument_name_map
                name_map: Dict[str, str] = m.get("instrument_name_map", {})
                meta_map: Dict[str, str] = {}
                self.options_map = m.get("options_map", {})
                self.symbol_metadata = m.get("symbol_metadata", {})
                self.option_name_map = name_map

                # Pre-index underlying key to symbol and available strike chains
                for sym, meta in self.symbol_metadata.items():
                    ukey = meta.get("underlying_key", "")
                    sname = meta.get("name") or sym
                    if ukey:
                        meta_map[ukey] = sname
                        meta_map[ukey.replace(":", "|")] = sname
                        meta_map[ukey.replace("|", ":")] = sname
                        self.underlying_key_to_symbol[ukey] = sym
                        self.underlying_key_to_symbol[ukey.replace(":", "|")] = sym
                        self.underlying_key_to_symbol[ukey.replace("|", ":")] = sym
                        self.underlying_keys.add(ukey)
                        self.underlying_keys.add(ukey.replace(":", "|"))
                        self.underlying_keys.add(ukey.replace("|", ":"))
                    self.underlying_key_to_symbol[sname] = sym
                    self.underlying_key_to_symbol[sym] = sym

                for sym, sdata in self.options_map.items():
                    strikes_dict = sdata.get("strikes", {})
                    valid_strikes = []
                    for s_str in strikes_dict.keys():
                        try:
                            valid_strikes.append(float(s_str))
                        except ValueError:
                            pass
                    self.available_strikes_by_symbol[sym] = sorted(valid_strikes)
                
                # Check both schema formats
                underlyings = m.get("streamed_underlyings", [])
                if underlyings:
                    for u in underlyings:
                        uk = u["instrument_key"]
                        sym_disp = u.get("name") or name_map.get(uk) or u.get("symbol") or uk.split("|")[-1]
                        instruments.append({"key": uk, "symbol": sym_disp})
                        self.underlying_keys.add(uk)
                        self.underlying_keys.add(uk.replace(":", "|"))
                        self.underlying_keys.add(uk.replace("|", ":"))
                else:
                    for k in m.get("underlying_keys", []):
                        sym = name_map.get(k) or meta_map.get(k) or filtered_map.get(k) or k.split("|")[-1]
                        instruments.append({"key": k, "symbol": sym})
                        self.underlying_keys.add(k)
                        self.underlying_keys.add(k.replace(":", "|"))
                        self.underlying_keys.add(k.replace("|", ":"))

                # -------------------------------------------------------------
                # Protected Position Reservation & Dynamic Popular Options Fall:
                # 1. Any dated protected contracts active for today take first priority.
                # 2. Dynamic popular options quota takes the equivalent fall for reserved contracts.
                # -------------------------------------------------------------
                protected_keys = self.position_registry.get_protected_keys()
                protected_canon = {k.replace(":", "|") for k in protected_keys}
                existing_inst_keys = {inst["key"] for inst in instruments}
                for pk in protected_canon:
                    if pk not in existing_inst_keys:
                        s_name = self.symbol_map.get(pk) or name_map.get(pk) or pk.split("|")[-1]
                        instruments.append({"key": pk, "symbol": s_name})
                        existing_inst_keys.add(pk)
                        self.active_option_keys.add(pk)

                # Add popular option contracts up to remaining budget (taking equivalent fall)
                popular_limit = max(0, self.max_stream_contracts - len(instruments))
                popular = m.get("streamed_popular_options", [])
                if popular:
                    for opt in popular:
                        if len(instruments) >= self.max_stream_contracts:
                            break
                        okey = opt["instrument_key"]
                        if okey in existing_inst_keys:
                            continue
                        s_name = opt.get("name") or name_map.get(okey) or opt.get("trading_symbol", "")
                        instruments.append({"key": okey, "symbol": s_name})
                        existing_inst_keys.add(okey)
                        self.active_option_keys.add(okey)
                        und = opt.get("underlying")
                        if und:
                            self.active_options_by_symbol[und].add(okey)
                else:
                    for k in m.get("popular_option_keys", []):
                        if len(instruments) >= self.max_stream_contracts:
                            break
                        if k in existing_inst_keys:
                            continue
                        sym = name_map.get(k) or filtered_map.get(k) or k.split("|")[-1]
                        instruments.append({"key": k, "symbol": sym})
                        existing_inst_keys.add(k)
                        self.active_option_keys.add(k)

                if instruments:
                    self.instruments = instruments
                    self._build_symbol_map(extra_maps=[name_map, meta_map, filtered_map])
                    return instruments
            except Exception:
                pass

        # 2. Fallback to filtered CSV
        if filtered_map:
            for k, s in filtered_map.items():
                if "|" in k:
                    instruments.append({"key": k, "symbol": s})
            if instruments:
                self.instruments = instruments
                self._build_symbol_map(extra_maps=[filtered_map])
                return instruments

        # 3. Default active instruments
        if self.exchange == "MCX":
            default_mcx = [
                ("MCX_FO|573422", "CRUDEOIL26NOVFUT"),
                ("MCX_FO|574319", "NATURALGAS26NOVFUT"),
                ("MCX_FO|574826", "SILVER10026NOVFUT"),
                ("MCX_FO|574841", "GOLDPETAL26NOVFUT"),
                ("MCX_FO|578640", "NICKEL26NOVFUT"),
                ("MCX_FO|578634", "COPPER26NOVFUT"),
                ("MCX_FO|578639", "ZINCMINI26NOVFUT"),
            ]
            for k, s in default_mcx:
                instruments.append({"key": k, "symbol": s})
        else:
            default_symbols = [
                "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN",
                "BHARTIARTL", "ITC", "LT", "KOTAKBANK", "AXISBANK", "TATAMOTORS",
                "SUNPHARMA", "MARUTI", "TITAN", "BAJFINANCE", "ASIANPAINT", "HCLTECH"
            ]
            for s in default_symbols:
                instruments.append({"key": f"NSE_EQ|{s}", "symbol": s})

        self.instruments = instruments
        self._build_symbol_map(extra_maps=[filtered_map])
        return instruments

    def _build_symbol_map(self, extra_maps: Optional[List[Dict[str, str]]] = None) -> None:
        self.symbol_map = {}
        if extra_maps:
            for emap in extra_maps:
                for k, s in emap.items():
                    if k and s:
                        self.symbol_map[k] = s
                        self.symbol_map[k.replace(":", "|")] = s
                        self.symbol_map[k.replace("|", ":")] = s
        for inst in self.instruments:
            k = inst.get("key", "")
            s = inst.get("symbol", "")
            if k and s:
                self.symbol_map[k] = s
                self.symbol_map[k.replace(":", "|")] = s
                self.symbol_map[k.replace("|", ":")] = s

    def _prune_dynamic_options_to_fit(
        self,
        keys_to_add: Set[str],
        protected_keys: Set[str],
        current_symbol: str,
    ) -> Set[str]:
        """
        Enforce the dynamic option quota: if total subscriptions would exceed max_stream_contracts,
        unheld dynamic options take the equivalent fall to maintain the budget.
        Protected keys and newly requested ATM options are never pruned.
        """
        protected_canon = {k.replace(":", "|") for k in protected_keys} | {k.replace("|", ":") for k in protected_keys}
        underlying_count = len({k.replace(":", "|") for k in self.underlying_keys})

        # Projected total after adding keys_to_add
        projected_total = underlying_count + len(self.active_option_keys | keys_to_add)
        excess = projected_total - self.max_stream_contracts
        if excess <= 0:
            return set()

        candidates: List[Tuple[float, str, str]] = []  # (priority_score, symbol, option_key)

        for sym, opt_keys in list(self.active_options_by_symbol.items()):
            sym_atm = self.last_atm_by_symbol.get(sym, 0.0)

            for okey in list(opt_keys):
                if okey in protected_canon or okey.replace(":", "|") in protected_canon or okey.replace("|", ":") in protected_canon:
                    continue  # Protected key cannot fall
                if okey in keys_to_add or okey.replace(":", "|") in keys_to_add or okey.replace("|", ":") in keys_to_add:
                    continue  # Newly added option cannot fall

                distance = 0.0
                try:
                    disp = self.symbol_map.get(okey, "")
                    for p in disp.split():
                        try:
                            val = float(p)
                            if val > 0:
                                distance = abs(val - sym_atm) if sym_atm > 0 else 0.0
                                break
                        except ValueError:
                            pass
                except Exception:
                    pass

                symbol_weight = 1000.0 if sym != current_symbol else 0.0
                priority_score = distance + symbol_weight
                candidates.append((priority_score, sym, okey))

        if not candidates:
            trimmable = [k for k in list(keys_to_add) if k not in protected_canon]
            for tk in trimmable[:excess]:
                keys_to_add.discard(tk)
            return set()

        candidates.sort(key=lambda x: x[0], reverse=True)
        pruned_items = candidates[:excess]
        pruned_keys = {item[2] for item in pruned_items}

        for item in pruned_items:
            psym = item[1]
            pokey = item[2]
            if psym in self.active_options_by_symbol:
                self.active_options_by_symbol[psym].discard(pokey)
            self.active_option_keys.discard(pokey)

        remaining_excess = excess - len(pruned_keys)
        if remaining_excess > 0:
            trimmable = [k for k in list(keys_to_add) if k not in protected_canon]
            for tk in trimmable[:remaining_excess]:
                keys_to_add.discard(tk)

        return pruned_keys

    def evaluate_dynamic_options(self, underlying_key: str, ltp: float) -> Tuple[List[str], List[str]]:
        """
        Evaluate if underlying price movement requires updating the active popular option strikes.
        Returns (keys_to_subscribe, keys_to_unsubscribe).
        """
        if not self.enable_dynamic_options or not self.options_map or ltp <= 0.0:
            return [], []

        canonical_key = underlying_key.replace(":", "|")
        sym = self.underlying_key_to_symbol.get(canonical_key)
        if not sym:
            sym = self.underlying_key_to_symbol.get(underlying_key)
        if not sym:
            sym = self.symbol_map.get(canonical_key)
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

        # Find closest strike to current LTP
        atm_strike = min(strikes_list, key=lambda s: abs(s - ltp))

        # If ATM strike is identical to last ATM evaluated, check if periodic sweep is due (every 10.0s)
        # Otherwise throttle rapid ticks at the same strike
        if last_atm is not None and atm_strike == last_atm:
            if (now - last_reval) < 10.0:
                return [], []

        # Throttle rapid re-evaluations across strike changes (at most once per 1.0 second per symbol)
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

        # Retention window with hysteresis (wider than popular window so we don't prune on 1-tick fluctuations)
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

        # -------------------------------------------------------------
        # CRITICAL ACTIVE POSITION PROTECTION GATE:
        # NEVER unsubscribe from any option contract that has an active open position!
        # -------------------------------------------------------------
        protected_keys = self.position_registry.get_protected_keys()
        if protected_keys:
            # 1. Unconditionally strip protected contracts from removal
            saved_by_protection = keys_to_remove & protected_keys
            if saved_by_protection:
                for sk in saved_by_protection:
                    sym_name = self.symbol_map.get(sk, sk)
                    if sk not in self.protected_logged_keys:
                        self.protected_logged_keys.add(sk)
                        self._log(f"  🛡️ [PROTECTION ACTIVE] Retaining subscription for '{sym_name}' ({sk}) - Active open position detected!")
                keys_to_remove = keys_to_remove - protected_keys

            # 2. Guarantee that any protected contract for this underlying is actively subscribed
            for pk in protected_keys:
                pk_canon = pk.replace(":", "|")
                disp = self.symbol_map.get(pk, "") or self.symbol_map.get(pk_canon, "")
                if disp and (lookup_sym in disp or sym in disp):
                    if pk_canon not in current_keys and pk not in current_keys:
                        keys_to_add.add(pk_canon)

        # -------------------------------------------------------------
        # GLOBAL SUBSCRIPTION QUOTA & DYNAMIC OPTION EQUIVALENT FALL:
        # If total active instruments + keys_to_add would exceed max_stream_contracts,
        # unheld dynamic options (furthest from ATM) must take the equivalent fall!
        # Protected keys are strictly retained.
        # -------------------------------------------------------------
        forced_pruned = self._prune_dynamic_options_to_fit(keys_to_add, protected_keys, lookup_sym)
        if forced_pruned:
            keys_to_remove.update(forced_pruned)

        if not keys_to_add and not keys_to_remove:
            self.last_atm_by_symbol[lookup_sym] = atm_strike
            self.last_reval_time_by_symbol[lookup_sym] = now
            return [], []

        # Update internal tracking
        updated_keys = (current_keys - keys_to_remove) | keys_to_add
        self.active_options_by_symbol[lookup_sym] = updated_keys
        self.active_option_keys = (self.active_option_keys - keys_to_remove) | keys_to_add
        self.last_atm_by_symbol[lookup_sym] = atm_strike
        self.last_reval_time_by_symbol[lookup_sym] = now

        return list(keys_to_add), list(keys_to_remove)

    def start(self) -> None:
        """Start local IPC publisher and begin market data ingestion loop."""
        self.load_instruments()
        self._running = True
        self.publisher.start()

        if self.silent:
            print("=" * 80)
            print(f"📡 MARKET DATA LISTENER SERVICE ACTIVE [SILENT MODE] | {self.mode.upper()}")
            print(f"IPC Socket Path : {self.ipc_path}")
            print(f"Monitored Assets: {len(self.instruments):,} contracts [{self.exchange}]")
            print(f"REST Fallback   : {'ENABLED' if self.is_rest_fallback_allowed() else 'DISABLED (Default - Optimal Trading Guard)'}")
            print("  (Running in background. Dynamic shifts and heartbeat logs are suppressed)")
            print("  (Press Ctrl+C to stop listener)")
            print("=" * 80 + "\n")
        else:
            print("=" * 80)
            print("📡 MARKET DATA LISTENER SERVICE ACTIVE")
            print(f"IPC Socket Path : {self.ipc_path}")
            print(f"Monitored Assets: {len(self.instruments):,} contracts [{self.exchange}]")
            print(f"Ingestion Mode  : {self.mode.upper()}")
            print(f"REST Fallback   : {'ENABLED' if self.is_rest_fallback_allowed() else 'DISABLED (Default - Optimal Trading Guard)'}")
            print(f"Time            : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("=" * 80)

        # Broadcast System Start Event
        self.publisher.publish(SystemEvent(
            code="LISTENER_STARTED",
            message=f"MarketDataListener online at {self.ipc_path} [{self.exchange}]",
        ))

        # Check available transport libraries
        has_ws = False
        try:
            import websockets
            import google.protobuf
            has_ws = True
        except ImportError:
            pass

        # Determine runtime engine
        token_valid = bool(self.access_token and self.access_token != "your_live_access_token_here")

        if self.mode == "mock":
            self._log("📡 Ingestion Engine: SYNTHETIC MOCK MARKET TICK FEED")
            self._run_mock_stream()
        elif not token_valid:
            print("❌ Cannot start live market data listener: UPSTOX_ACCESS_TOKEN is missing or invalid in .env.", file=sys.stderr)
            print("   Please generate/set a valid Upstox access token or run with --mode mock for synthetic testing.", file=sys.stderr)
            sys.exit(1)
        elif self.mode in ("auto", "ws", "websocket") and has_ws:
            self._log("📡 Ingestion Engine: LIVE UPSTOX WEBSOCKET (Binary Protobuf Feed)")
            self._run_websocket_stream()
        elif self.mode == "poll":
            self._log("📡 Ingestion Engine: HIGH-SPEED REST BATCH POLLER")
            self._run_poll_stream()
        else:
            if not has_ws and self.mode in ("auto", "ws", "websocket"):
                if self.is_rest_fallback_allowed():
                    self._log("📡 Ingestion Engine: HIGH-SPEED REST BATCH POLLER (REST Fallback Active)")
                    self._run_poll_stream()
                else:
                    print("❌ Ingestion Engine Error: 'websockets' is not installed and REST fallback is DISABLED.")
                    print("   Install websockets or pass --allow-rest-fallback / touch 'logs/.allow_rest_fallback'.")
            else:
                if self.is_rest_fallback_allowed():
                    self._log("📡 Ingestion Engine: HIGH-SPEED REST BATCH POLLER (REST Fallback Active)")
                    self._run_poll_stream()
                else:
                    print(f"❌ Ingestion Engine Error: Mode '{self.mode}' unsupported and REST fallback is DISABLED.")

    def _run_websocket_stream(self) -> None:
        """
        Connect to Upstox V3 WebSocket Feed using authorized_redirect_uri and stream updates.
        Decodes incoming binary protobuf frames into MarketTicks and broadcasts across IPC bus.
        Falls back seamlessly to high-speed batch poller if WebSocket connection fails or drops.
        """
        import asyncio
        import ssl
        try:
            import websockets
        except ImportError:
            print("  ⚠️ 'websockets' library not found.")
            if self.is_rest_fallback_allowed():
                print("     Switching to high-speed REST poller (--allow-rest-fallback active)...")
                self._run_poll_stream()
            else:
                print("     ❌ REST fallback is DISABLED by default (optimal trading guard).")
                print("     Install websockets: pip install websockets, or pass --allow-rest-fallback.")
            return

        # Check protobuf module
        pb = None
        try:
            from infrastructure.market_data.proto import MarketDataFeedV3_pb2 as pb
        except (ImportError, AttributeError):
            try:
                import MarketDataFeedV3_pb2 as pb
            except ImportError:
                pb = None

        if pb is None:
            print("  ⚠️ Protobuf decoder module (MarketDataFeedV3_pb2) not found in 'infrastructure/market_data/proto/'.")
            print("     To activate binary Upstox V3 streaming:")
            print("       Compile schema with: protoc -Iinfrastructure/market_data/proto --python_out=infrastructure/market_data/proto infrastructure/market_data/proto/MarketDataFeedV3.proto")
            if self.is_rest_fallback_allowed():
                print("     Switching seamlessly to high-speed REST poller (--allow-rest-fallback active)...\n")
                self._run_poll_stream()
            else:
                print("     ❌ REST fallback is DISABLED by default (optimal trading guard).\n")
            return

        # 1. Authorize WebSocket connection (Upstox V3 Feed)
        auth_url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"

        def get_authorized_ws_url() -> Optional[str]:
            try:
                res = http_get_json(auth_url, access_token=self.access_token, timeout=10, debug_file=self.debug_log)
                if res.get("status") == "success" and "data" in res:
                    return res["data"].get("authorized_redirect_uri") or res["data"].get("authorizedRedirectUri")
                err_msg = res.get("message") or res.get("errors") or "Non-success"
                log_debug(f"Upstox WS auth error: {err_msg}", self.debug_log)
            except Exception as auth_err:
                log_debug(f"Upstox WS auth exception: {auth_err}", self.debug_log)
            return None

        ws_url = None
        auth_attempts = 0
        while self._running and not ws_url:
            auth_attempts += 1
            ws_url = get_authorized_ws_url()
            if ws_url:
                break
            if auth_attempts >= 3:
                if self.is_rest_fallback_allowed():
                    print("  ⚠️ WebSocket authorization failed. Switching to REST poller (--allow-rest-fallback active)...")
                    self._run_poll_stream()
                    return
                else:
                    backoff = min(10, 2 ** min(auth_attempts - 2, 3))
                    print(f"  ⚠️ WebSocket authorization attempt {auth_attempts} failed. [REST fallback DISABLED]")
                    print(f"     Retrying authorization in {backoff}s... (touch logs/.allow_rest_fallback to permit REST)")
                    time.sleep(backoff)
            else:
                time.sleep(1)

        if not ws_url:
            return

        async def ws_consumer():
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            async with websockets.connect(
                ws_url,
                ssl=ssl_ctx,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                keys = [inst["key"] for inst in self.instruments if inst.get("key")]
                stream_keys = keys[:self.max_stream_contracts]
                sub_payload = {
                    "guid": "antigravity_stream",
                    "method": "sub",
                    "data": {
                        "mode": "full",
                        "instrumentKeys": stream_keys,
                    }
                }
                binary_payload = json.dumps(sub_payload).encode("utf-8")
                await asyncio.sleep(0.5)
                await ws.send(binary_payload)
                print(f"✓ Upstox V3 WebSocket connected to {ws_url[:45]}... Subscribed to {len(stream_keys):,} contracts.")
                print("  (Press Ctrl+C to stop listener)\n")

                last_status_ts = time.time()
                ticks_in_window = 0
                depth_in_window = 0
                frames_in_window = 0

                while self._running:
                    msg = await ws.recv()
                    frames_in_window += 1
                    now_ts = time.time()

                    if isinstance(msg, bytes):
                        try:
                            feed_resp = pb.FeedResponse()
                            feed_resp.ParseFromString(msg)

                            for k, feed in feed_resp.feeds.items():
                                canonical_key = k.replace(":", "|")
                                sym = self.symbol_map.get(canonical_key) or canonical_key.split("|")[-1]

                                ltp = 0.0
                                close = 0.0
                                vol = 0
                                high = 0.0
                                low = 0.0
                                bid = 0.0
                                ask = 0.0
                                bids: List[List[float]] = []
                                asks: List[List[float]] = []

                                feed_union = feed.WhichOneof("FeedUnion")
                                if feed_union == "fullFeed":
                                    ff = feed.fullFeed
                                    which = ff.WhichOneof("FullFeedUnion")
                                    if which == "marketFF":
                                        m = ff.marketFF
                                        ltp = float(m.ltpc.ltp or 0.0)
                                        close = float(m.ltpc.cp or 0.0)
                                        vol = int(getattr(m, "vtt", 0) or getattr(m, "volume", 0) or 0)
                                        if m.marketOHLC and m.marketOHLC.ohlc:
                                            high = float(m.marketOHLC.ohlc[0].high or ltp)
                                            low = float(m.marketOHLC.ohlc[0].low or ltp)
                                        else:
                                            high = ltp
                                            low = ltp
                                        if m.marketLevel and m.marketLevel.bidAskQuote:
                                            for ba in m.marketLevel.bidAskQuote:
                                                bp = float(ba.bidP or 0.0)
                                                bq = int(ba.bidQ or 0)
                                                bo = int(getattr(ba, "bidO", 1) or 1)
                                                ap = float(ba.askP or 0.0)
                                                aq = int(ba.askQ or 0)
                                                ao = int(getattr(ba, "askO", 1) or 1)
                                                if bp > 0:
                                                    bids.append([bp, bq, bo])
                                                if ap > 0:
                                                    asks.append([ap, aq, ao])
                                            bid = bids[0][0] if bids else 0.0
                                            ask = asks[0][0] if asks else 0.0
                                        else:
                                            bid = 0.0
                                            ask = 0.0
                                    elif which == "indexFF":
                                        idx = ff.indexFF
                                        ltp = float(idx.ltpc.ltp or 0.0)
                                        close = float(idx.ltpc.cp or 0.0)
                                        if idx.marketOHLC and idx.marketOHLC.ohlc:
                                            high = float(idx.marketOHLC.ohlc[0].high or ltp)
                                            low = float(idx.marketOHLC.ohlc[0].low or ltp)
                                        else:
                                            high = ltp
                                            low = ltp
                                        bid = ltp
                                        ask = ltp
                                elif feed_union == "firstLevelWithGreeks":
                                    fl = feed.firstLevelWithGreeks
                                    ltp = float(fl.ltpc.ltp or 0.0)
                                    close = float(fl.ltpc.cp or 0.0)
                                    vol = int(getattr(fl, "vtt", 0) or 0)
                                    high = ltp
                                    low = ltp
                                    if fl.firstDepth:
                                        bp = float(fl.firstDepth.bidP or 0.0)
                                        bq = int(fl.firstDepth.bidQ or 0)
                                        ap = float(fl.firstDepth.askP or 0.0)
                                        aq = int(fl.firstDepth.askQ or 0)
                                        bid = bp if bp > 0 else 0.0
                                        ask = ap if ap > 0 else 0.0
                                        if bp > 0:
                                            bids.append([bp, bq, 1])
                                        if ap > 0:
                                            asks.append([ap, aq, 1])
                                    else:
                                        bid = 0.0
                                        ask = 0.0
                                elif feed_union == "ltpc":
                                    ltp = float(feed.ltpc.ltp or 0.0)
                                    close = float(feed.ltpc.cp or 0.0)
                                    high = ltp
                                    low = ltp
                                    bid = 0.0
                                    ask = 0.0

                                if close <= 0.0 and ltp > 0.0:
                                    close = ltp

                                if ltp <= 0.0:
                                    continue

                                is_index = "INDEX" in canonical_key or "Nifty 50" in sym or sym == "NIFTY"
                                is_first_seen = canonical_key not in self.last_seen_volume
                                prev_vol = self.last_seen_volume.get(canonical_key, -1)
                                prev_ltp = self.last_seen_ltp.get(canonical_key, -1.0)
                                prev_depth = self.last_seen_depth.get(canonical_key)

                                volume_changed = is_first_seen or (vol != prev_vol)
                                price_changed = is_first_seen or (ltp != prev_ltp)

                                depth_available = bool(bids or asks or (bid > 0 and ask > 0))
                                current_depth_tuple = (
                                    round(bid, 2),
                                    int(bids[0][1]) if bids else 0,
                                    round(ask, 2),
                                    int(asks[0][1]) if asks else 0,
                                    tuple((round(b[0], 2), int(b[1])) for b in bids[:3]),
                                    tuple((round(a[0], 2), int(a[1])) for a in asks[:3]),
                                )
                                depth_changed = depth_available and (is_first_seen or current_depth_tuple != prev_depth)

                                # 1. Publish MarketTick if volume changed (trade) or index moved
                                if is_index:
                                    if price_changed:
                                        self.last_seen_ltp[canonical_key] = ltp
                                        tick = MarketTick(
                                            instrument_key=canonical_key,
                                            symbol=sym,
                                            ltp=ltp,
                                            volume=vol,
                                            close=close,
                                            high=high if high > 0 else ltp,
                                            low=low if low > 0 else ltp,
                                            bid=bid if bid > 0 else ltp,
                                            ask=ask if ask > 0 else ltp,
                                            received_timestamp=now_ts,
                                        )
                                        self.publisher.publish(tick)
                                        self.total_published += 1
                                        ticks_in_window += 1
                                else:
                                    if volume_changed:
                                        self.last_seen_volume[canonical_key] = vol
                                        self.last_seen_ltp[canonical_key] = ltp
                                        tick = MarketTick(
                                            instrument_key=canonical_key,
                                            symbol=sym,
                                            ltp=ltp,
                                            volume=vol,
                                            close=close,
                                            high=high if high > 0 else ltp,
                                            low=low if low > 0 else ltp,
                                            bid=bid if bid > 0 else ltp,
                                            ask=ask if ask > 0 else ltp,
                                            received_timestamp=now_ts,
                                        )
                                        self.publisher.publish(tick)
                                        self.total_published += 1
                                        ticks_in_window += 1

                                # 2. Publish OptionDepth if depth changed (handles both trade+depth change and book-only change)
                                if depth_changed and not is_index:
                                    self.last_seen_depth[canonical_key] = current_depth_tuple
                                    depth = OptionDepth(
                                        instrument_key=canonical_key,
                                        symbol=sym,
                                        bids=bids,
                                        asks=asks,
                                        bid=bid if bid > 0 else ltp,
                                        bid_qty=int(bids[0][1]) if bids else 0,
                                        ask=ask if ask > 0 else ltp,
                                        ask_qty=int(asks[0][1]) if asks else 0,
                                        ltp=ltp,
                                        received_timestamp=now_ts,
                                    )
                                    self.publisher.publish(depth)
                                    self.total_depth_published += 1
                                    depth_in_window += 1

                                # 3. Dynamic popular options subscription re-evaluation on underlying movement
                                if canonical_key in self.underlying_keys or canonical_key in self.underlying_key_to_symbol or sym in self.underlying_key_to_symbol:
                                    to_add, to_remove = self.evaluate_dynamic_options(canonical_key, ltp)
                                    if to_add:
                                        sub_payload = {
                                            "guid": f"antigravity_dyn_sub_{self.total_dynamic_subscriptions}",
                                            "method": "sub",
                                            "data": {
                                                "mode": "full",
                                                "instrumentKeys": to_add,
                                            }
                                        }
                                        await ws.send(json.dumps(sub_payload).encode("utf-8"))
                                        self.total_dynamic_subscriptions += len(to_add)

                                    if to_remove:
                                        unsub_payload = {
                                            "guid": f"antigravity_dyn_unsub_{self.total_dynamic_unsubscriptions}",
                                            "method": "unsub",
                                            "data": {
                                                "instrumentKeys": to_remove,
                                            }
                                        }
                                        await ws.send(json.dumps(unsub_payload).encode("utf-8"))
                                        self.total_dynamic_unsubscriptions += len(to_remove)

                                    if to_add or to_remove:
                                        atm_val = self.last_atm_by_symbol.get(sym, ltp)
                                        self._log(f"  🔄 DYNAMIC OPTION SUBSCRIPTION: {sym} ATM shifted to {atm_val:g} | +{len(to_add)} subscribed, -{len(to_remove)} pruned (Live Total: {len(self.active_option_keys):,} options)")
                        except Exception as decode_err:
                            log_debug(f"Protobuf decode error on frame of length {len(msg)}: {decode_err}", self.debug_log)
                    elif isinstance(msg, str):
                        # Text frame (e.g. JSON connection status, error, or heartbeat from server)
                        log_debug(f"Upstox WS text frame received: {msg}", self.debug_log)
                        try:
                            txt_data = json.loads(msg)
                            if txt_data.get("status") == "error":
                                print(f"  ⚠️ Upstox WS Server Error: {txt_data.get('message', msg)}")
                        except Exception:
                            pass

                    now = time.time()
                    if now - last_status_ts >= 3.0:
                        elapsed = now - last_status_ts
                        f_rate = frames_in_window / elapsed if elapsed > 0 else 0.0
                        t_rate = ticks_in_window / elapsed if elapsed > 0 else 0.0
                        d_rate = depth_in_window / elapsed if elapsed > 0 else 0.0
                        clients = len(self.publisher._socket_clients) + len(self.publisher._queues)
                        prot_count = len(self.position_registry.get_protected_keys())
                        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] 📡 UPSTOX V3 WEBSOCKET | Trades: {self.total_published:,} ({t_rate:.1f}/s) | Book Depth: {self.total_depth_published:,} ({d_rate:.1f}/s) | Dyn Options: {len(self.active_option_keys):,} | Protected: {prot_count:,} | Clients: {clients} | Frames: {f_rate:.1f}/s")
                        last_status_ts = now
                        ticks_in_window = 0
                        depth_in_window = 0
                        frames_in_window = 0

        reconnect_attempts = 0
        max_reconnect_attempts = 5
        while self._running:
            try:
                asyncio.run(ws_consumer())
                break
            except Exception as e:
                if not self._running:
                    break
                reconnect_attempts += 1
                backoff = min(15, 2 ** min(reconnect_attempts, 4))
                if not self.is_rest_fallback_allowed():
                    print(f"\n⚠️ WebSocket stream interrupted ({e}).")
                    print(f"   [OPTIMAL TRADING GUARD] REST fallback is DISABLED by default (prevents suboptimal latency).")
                    print(f"   Reconnecting to Upstox WebSocket in {backoff}s (Attempt {reconnect_attempts})...")
                    time.sleep(backoff)
                    new_ws_url = get_authorized_ws_url()
                    if new_ws_url:
                        ws_url = new_ws_url
                else:
                    if reconnect_attempts <= max_reconnect_attempts:
                        print(f"\n⚠️ WebSocket stream interrupted ({e}). Reconnecting in {backoff}s (Attempt {reconnect_attempts}/{max_reconnect_attempts})...")
                        time.sleep(backoff)
                        new_ws_url = get_authorized_ws_url()
                        if new_ws_url:
                            ws_url = new_ws_url
                    else:
                        print(f"\n⚠️ WebSocket stream interrupted ({e}). Max retries ({max_reconnect_attempts}) reached.")
                        print("   Switching seamlessly to REST poller (--allow-rest-fallback active)...")
                        self._run_poll_stream()
                        break

    def _run_poll_stream(self) -> None:
        """High-speed REST polling feeder fetching quotes in chunks of 500."""
        keys = [inst["key"] for inst in self.instruments if inst.get("key")]
        batch_size = 500
        chunks = [keys[i : i + batch_size] for i in range(0, len(keys), batch_size)]

        print(f"✓ Streaming Upstox Live Feed: {len(keys):,} instruments in {len(chunks)} parallel batch(es)...")
        print("  (Press Ctrl+C to stop listener)\n")

        last_status_ts = time.time()
        ticks_in_window = 0
        depth_in_window = 0

        while self._running:
            loop_start = time.time()

            for chunk in chunks:
                if not self._running:
                    break
                encoded_keys = [urllib.parse.quote(k, safe="") for k in chunk]
                keys_param = ",".join(encoded_keys)
                url = f"https://api.upstox.com/v2/market-quote/quotes?instrument_key={keys_param}"

                try:
                    res = http_get_json(url, access_token=self.access_token, timeout=10, debug_file=self.debug_log)
                    if res.get("status") == "success":
                        data = res.get("data", {})
                        now_ts = time.time()

                        for k, v in data.items():
                            if not isinstance(v, dict):
                                continue

                            ltp = float(v.get("last_price") or 0.0)
                            vol = int(v.get("volume") or 0)
                            ohlc = v.get("ohlc", {})
                            close = float(ohlc.get("close") or 0.0)
                            high = float(ohlc.get("high") or ltp)
                            low = float(ohlc.get("low") or ltp)
                            sym = v.get("symbol") or k.split(":")[-1]

                            depth = v.get("depth", {})
                            buys = depth.get("buy", [])
                            sells = depth.get("sell", [])
                            best_bid = float(buys[0].get("price", 0.0)) if buys else 0.0
                            best_ask = float(sells[0].get("price", 0.0)) if sells else 0.0

                            canonical_key = k.replace(":", "|")
                            bids = [[float(b.get("price", 0.0)), int(b.get("quantity", 0)), int(b.get("orders", 1))] for b in buys if float(b.get("price", 0.0)) > 0]
                            asks = [[float(a.get("price", 0.0)), int(a.get("quantity", 0)), int(a.get("orders", 1))] for a in sells if float(a.get("price", 0.0)) > 0]

                            is_index = "INDEX" in canonical_key or "Nifty 50" in sym or sym == "NIFTY"
                            is_first_seen = canonical_key not in self.last_seen_volume
                            prev_vol = self.last_seen_volume.get(canonical_key, -1)
                            prev_ltp = self.last_seen_ltp.get(canonical_key, -1.0)
                            prev_depth = self.last_seen_depth.get(canonical_key)

                            volume_changed = is_first_seen or (vol != prev_vol)
                            price_changed = is_first_seen or (ltp != prev_ltp)

                            depth_available = bool(bids or asks or (best_bid > 0 and best_ask > 0))
                            current_depth_tuple = (
                                round(best_bid, 2),
                                int(bids[0][1]) if bids else 0,
                                round(best_ask, 2),
                                int(asks[0][1]) if asks else 0,
                                tuple((round(b[0], 2), int(b[1])) for b in bids[:3]),
                                tuple((round(a[0], 2), int(a[1])) for a in asks[:3]),
                            )
                            depth_changed = depth_available and (is_first_seen or current_depth_tuple != prev_depth)

                            if is_index:
                                if price_changed:
                                    self.last_seen_ltp[canonical_key] = ltp
                                    tick = MarketTick(
                                        instrument_key=canonical_key,
                                        symbol=sym,
                                        ltp=ltp,
                                        volume=vol,
                                        close=close,
                                        high=high,
                                        low=low,
                                        bid=best_bid,
                                        ask=best_ask,
                                        received_timestamp=now_ts,
                                    )
                                    self.publisher.publish(tick)
                                    self.total_published += 1
                                    ticks_in_window += 1
                            else:
                                if volume_changed:
                                    self.last_seen_volume[canonical_key] = vol
                                    self.last_seen_ltp[canonical_key] = ltp
                                    tick = MarketTick(
                                        instrument_key=canonical_key,
                                        symbol=sym,
                                        ltp=ltp,
                                        volume=vol,
                                        close=close,
                                        high=high,
                                        low=low,
                                        bid=best_bid,
                                        ask=best_ask,
                                        received_timestamp=now_ts,
                                    )
                                    self.publisher.publish(tick)
                                    self.total_published += 1
                                    ticks_in_window += 1

                            if depth_changed and not is_index:
                                self.last_seen_depth[canonical_key] = current_depth_tuple
                                depth = OptionDepth(
                                    instrument_key=canonical_key,
                                    symbol=sym,
                                    bids=bids,
                                    asks=asks,
                                    bid=best_bid,
                                    bid_qty=int(bids[0][1]) if bids else 0,
                                    ask=best_ask,
                                    ask_qty=int(asks[0][1]) if asks else 0,
                                    ltp=ltp,
                                    received_timestamp=now_ts,
                                )
                                self.publisher.publish(depth)
                                self.total_depth_published += 1
                                depth_in_window += 1

                            if canonical_key in self.underlying_keys or canonical_key in self.underlying_key_to_symbol or sym in self.underlying_key_to_symbol:
                                to_add, to_remove = self.evaluate_dynamic_options(canonical_key, ltp)
                                if to_add or to_remove:
                                    poller_keys_set = set(keys)
                                    for k_add in to_add:
                                        if k_add not in poller_keys_set:
                                            keys.append(k_add)
                                            poller_keys_set.add(k_add)
                                            self.instruments.append({"key": k_add, "symbol": self.symbol_map.get(k_add, k_add)})
                                    for k_rem in to_remove:
                                        if k_rem in poller_keys_set:
                                            keys.remove(k_rem)
                                            poller_keys_set.remove(k_rem)
                                    chunks = [keys[i : i + batch_size] for i in range(0, len(keys), batch_size)]
                                    atm_val = self.last_atm_by_symbol.get(sym, ltp)
                                    self._log(f"  🔄 DYNAMIC OPTION POLLING: {sym} ATM @ {atm_val:g} | +{len(to_add)} added, -{len(to_remove)} pruned (Polling Total: {len(keys):,} instruments)")

                except Exception as e:
                    log_debug(f"MarketDataListener poll error: {e}", self.debug_log)

            # Periodically print heartbeat
            now = time.time()
            if now - last_status_ts >= 3.0:
                elapsed = now - last_status_ts
                t_rate = ticks_in_window / elapsed if elapsed > 0 else 0.0
                d_rate = depth_in_window / elapsed if elapsed > 0 else 0.0
                clients = len(self.publisher._socket_clients) + len(self.publisher._queues)
                self._log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚡ HIGH-SPEED REST POLLER | Trades: {self.total_published:,} ({t_rate:.1f}/s) | Book Depth: {self.total_depth_published:,} ({d_rate:.1f}/s) | Dyn Options: {len(self.active_option_keys):,} | Clients: {clients}")
                last_status_ts = now
                ticks_in_window = 0
                depth_in_window = 0

            # Sleep remaining time
            elapsed_loop = time.time() - loop_start
            sleep_time = max(0.05, self.poll_interval - elapsed_loop)
            time.sleep(sleep_time)

    def _run_mock_stream(self) -> None:
        """Deterministic simulation feeder generating moving ticks for offline testing."""
        print("✓ Simulation Mode: Generating live synthetic market ticks across monitored universe...")
        print("  (Press Ctrl+C to stop listener)\n")

        # Initialize mock state
        state: Dict[str, Dict[str, Any]] = {}
        nifty_base = 24500.0
        nifty_ltp = nifty_base

        for inst in self.instruments:
            base_p = round(random.uniform(200.0, 3500.0), 2)
            state[inst["key"]] = {
                "symbol": inst["symbol"],
                "base_price": base_p,
                "ltp": base_p,
                "volume": random.randint(100_000, 2_000_000),
                "close": base_p,
                "high": base_p * 1.005,
                "low": base_p * 0.995,
            }

        last_status_ts = time.time()
        ticks_in_window = 0
        depth_in_window = 0

        while self._running:
            now_ts = time.time()

            # 1. Update Nifty 50 regime tick
            nifty_ltp += round(random.uniform(-1.5, 1.8), 2)
            nifty_tick = MarketTick(
                instrument_key="NSE_INDEX|Nifty 50",
                symbol="Nifty 50",
                ltp=round(nifty_ltp, 2),
                volume=0,
                close=nifty_base,
                high=max(nifty_base * 1.002, nifty_ltp),
                low=min(nifty_base * 0.998, nifty_ltp),
                bid=round(nifty_ltp, 2),
                ask=round(nifty_ltp, 2),
                received_timestamp=now_ts,
            )
            self.publisher.publish(nifty_tick)
            self.total_published += 1
            ticks_in_window += 1

            # 2. Select a subset of active stocks to update
            sample_keys = random.sample(list(state.keys()), min(15, len(state)))
            for k in sample_keys:
                s = state.get(k)
                if not s:
                    continue
                drift = random.choice([-0.05, 0.0, 0.05, 0.10, 0.25])
                s["ltp"] = round(s["ltp"] + drift, 2)
                s["high"] = max(s["high"], s["ltp"])
                s["low"] = min(s["low"], s["ltp"])
                s["volume"] += random.randint(50, 1500)

                tick = MarketTick(
                    instrument_key=k,
                    symbol=s["symbol"],
                    ltp=s["ltp"],
                    volume=s["volume"],
                    close=s["close"],
                    high=s["high"],
                    low=s["low"],
                    bid=round(s["ltp"] - 0.05, 2),
                    ask=round(s["ltp"] + 0.05, 2),
                    received_timestamp=now_ts,
                )
                self.publisher.publish(tick)
                self.total_published += 1
                ticks_in_window += 1

                # If option contract, also emit OptionDepth
                if " CE" in s["symbol"] or " PE" in s["symbol"] or s["symbol"].endswith("CE") or s["symbol"].endswith("PE"):
                    depth = OptionDepth(
                        instrument_key=k,
                        symbol=s["symbol"],
                        bids=[[round(s["ltp"] - 0.05, 2), 500, 2]],
                        asks=[[round(s["ltp"] + 0.05, 2), 500, 2]],
                        bid=round(s["ltp"] - 0.05, 2),
                        bid_qty=500,
                        ask=round(s["ltp"] + 0.05, 2),
                        ask_qty=500,
                        ltp=s["ltp"],
                        received_timestamp=now_ts,
                    )
                    self.publisher.publish(depth)
                    self.total_depth_published += 1
                    depth_in_window += 1

                # Dynamic options in mock stream
                if k in self.underlying_keys or k in self.underlying_key_to_symbol or s["symbol"] in self.underlying_key_to_symbol:
                    to_add, to_remove = self.evaluate_dynamic_options(k, s["ltp"])
                    if to_add:
                        for opt_k in to_add:
                            if opt_k not in state:
                                opt_sym = self.symbol_map.get(opt_k, opt_k)
                                opt_p = round(random.uniform(5.0, 150.0), 2)
                                state[opt_k] = {
                                    "symbol": opt_sym,
                                    "base_price": opt_p,
                                    "ltp": opt_p,
                                    "volume": random.randint(1000, 50000),
                                    "close": opt_p,
                                    "high": opt_p,
                                    "low": opt_p,
                                }
                    for opt_k in to_remove:
                        state.pop(opt_k, None)

            now = time.time()
            if now - last_status_ts >= 3.0:
                elapsed = now - last_status_ts
                t_rate = ticks_in_window / elapsed if elapsed > 0 else 0.0
                d_rate = depth_in_window / elapsed if elapsed > 0 else 0.0
                clients = len(self.publisher._socket_clients) + len(self.publisher._queues)
                self._log(f"[{datetime.now().strftime('%H:%M:%S')}] ⚡ MOCK FEED ACTIVE | Trades: {self.total_published:,} ({t_rate:.1f}/s) | Book Depth: {self.total_depth_published:,} ({d_rate:.1f}/s) | Dyn Options: {len(self.active_option_keys):,} | Clients: {clients}")
                last_status_ts = now
                ticks_in_window = 0
                depth_in_window = 0

            time.sleep(0.2)

    def stop(self) -> None:
        """Stop market data listener and clean up socket."""
        self._running = False
        self.publisher.stop()
        print("\nMarket Data Listener stopped.")


def main():
    parser = argparse.ArgumentParser(description="Upstox Algorithmic Trading Market Data Listener")
    parser.add_argument("--mode", default="auto", choices=["auto", "poll", "mock", "ws"], help="Ingestion mode")
    parser.add_argument("--ipc-path", default=DEFAULT_IPC_PATH, help="IPC socket path")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Poll interval in seconds")
    parser.add_argument("--silent", "-s", action="store_true", help="Run in silent mode (suppresses dynamic shifts and heartbeats)")
    args = parser.parse_args()

    listener = MarketDataListener(
        mode=args.mode,
        ipc_path=args.ipc_path,
        poll_interval=args.poll_interval,
        silent=args.silent,
    )

    try:
        listener.start()
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    main()
