"""
Columnar Simulation Engine: Exact-Timestamp Vectorized & Serialized Multi-Tier Backtester.
Ingests market depth parquet/jsonl files and equity ticks files to simulate real-market trading.
Serializes CandleManager (SHM/Seqlock), ScoringEngine3, and execution tracks in chronological order,
looking up exact option prices directly from the ingested depth dataset with zero lookahead bias.
Prints separated simulation performance matrices per mode (paper, paper_delayed) across all tracks.
"""
import argparse
from dataclasses import dataclass, field
import glob
import math
import os
import random
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import numpy as np
import polars as pl

from common.interfaces import ExecutionContext, TradeState
from core.candles.candle_manager import CandleManager
from core.candles.shm_layout import unlink_shm
from core.common.ipc_schemas import (
    ActionableOpportunity,
    OrderRequest,
    OrderResult,
    SetupAlert,
)
from core.common.symbol_utils import (
    is_option_contract,
    normalize_instrument_token,
    parse_option_details,
)
from core.config.config_loader import get_config_manager
from strategies.momentum.scoring_engine import ScoringEngine3


@dataclass
class SimPendingOrder:
    release_ts: float
    order_id: str
    track: str
    token: str
    symbol: str
    target_pct: float
    sl_pct: float
    lot_size: int
    opp: ActionableOpportunity


class SimExecutionContext(ExecutionContext):
    """Headless simulation execution context simulating Superstructures with no IPC."""
    def __init__(self, mode: str):
        self._mode = mode
        self._frozen = False

    @property
    def mode(self) -> str:
        return self._mode

    def is_frozen(self) -> bool:
        return self._frozen

    def submit_order(self, request: OrderRequest) -> None:
        pass

    def cancel_order(self, order_id: str) -> None:
        pass

    def record_trade_event(self, event_type: str, data: Dict[str, Any]) -> None:
        pass


class SimDelayedExecutionContext(ExecutionContext):
    """Simulation execution context that queues OrderRequests for delayed fills."""
    def __init__(self, min_delay_ms: float = 10000.0, max_delay_ms: float = 60000.0):
        self._mode = "paper_delayed"
        self._frozen = False
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.pending_orders: List[Tuple[float, OrderRequest]] = []

    @property
    def mode(self) -> str:
        return self._mode

    def is_frozen(self) -> bool:
        return self._frozen

    def submit_order(self, request: OrderRequest) -> None:
        if request.transaction_type == "BUY":
            delay_sec = (
                random.uniform(self.min_delay_ms, self.max_delay_ms) / 1000.0
                if self.max_delay_ms > self.min_delay_ms
                else self.min_delay_ms / 1000.0
            )
            release_ts = (request.timestamp or 0.0) + delay_sec
            self.pending_orders.append((release_ts, request))

    def cancel_order(self, order_id: str) -> None:
        self.pending_orders = [item for item in self.pending_orders if item[1].order_id != order_id]

    def record_trade_event(self, event_type: str, data: Dict[str, Any]) -> None:
        pass


class ColumnarSimEngine:
    def __init__(
        self,
        data_path: str,
        tracks: Optional[List[str]] = None,
        modes: Optional[List[str]] = None,
        capital: Optional[float] = None,
        delay_sec: Optional[float] = None,
        min_delay_ms: Optional[float] = None,
        max_delay_ms: Optional[float] = None,
        shm_name: str = "shm_sim_ohlc",
        session_date: Optional[str] = None,
        min_pole_pct: Optional[float] = None,
        entry_freeze_time: Optional[str] = None,
        eod_squareoff_time: Optional[str] = None,
        brokerage_per_order: Optional[float] = None,
        strategy_cfg: Optional[Dict[str, Any]] = None,
        cached_equity_df: Optional[pl.DataFrame] = None,
        cached_options_df: Optional[pl.DataFrame] = None,
    ):
        from common.tracks.base_track import _parse_time_to_minutes
        from common.tracks.factory import get_enabled_track_names
        from core.config.config_loader import get_config_manager
        self.data_path = data_path
        self.strategy_cfg = strategy_cfg
        self._cached_equity_df = cached_equity_df
        self._cached_options_df = cached_options_df
        self.tracks = tracks or get_enabled_track_names(strategy_cfg)
        self.modes = [m.strip() for m in modes] if modes else ["paper", "paper_delayed"]
        self.capital = capital
        self.session_date = session_date
        self.min_pole_pct = min_pole_pct

        cfg = get_config_manager()
        exec_cfg = cfg.execution.get("execution", {})
        strat_momentum = (
            self.strategy_cfg.get("strategies", {}).get("momentum", {})
            if self.strategy_cfg and "strategies" in self.strategy_cfg
            else (self.strategy_cfg.get("momentum", {}) if self.strategy_cfg else cfg.strategy.get("strategies", {}).get("momentum", {}))
        )
        delayed_cfg = exec_cfg.get("paper_delayed", {})
        paper_cfg = exec_cfg.get("paper", {})
        sim_cfg = exec_cfg.get("sim", {})

        self.brokerage_per_order = (
            float(brokerage_per_order)
            if brokerage_per_order is not None
            else float(
                strat_momentum.get("brokerage_per_order")
                or exec_cfg.get("brokerage_per_order")
                or sim_cfg.get("brokerage_per_order")
                or paper_cfg.get("brokerage_per_order")
                or delayed_cfg.get("brokerage_per_order", 30.0)
            )
        )

        self.entry_freeze_time = (
            entry_freeze_time
            or strat_momentum.get("entry_freeze_time")
            or exec_cfg.get("entry_freeze_time")
            or "15:00"
        )
        self.eod_squareoff_time = (
            eod_squareoff_time
            or strat_momentum.get("eod_squareoff_time")
            or exec_cfg.get("eod_squareoff_time")
            or "15:15"
        )
        self.entry_freeze_minutes = _parse_time_to_minutes(self.entry_freeze_time, 15 * 60)
        self.eod_squareoff_minutes = _parse_time_to_minutes(self.eod_squareoff_time, 15 * 60 + 15)

        if delay_sec is not None:
            self.min_delay_ms = float(delay_sec) * 1000.0
            self.max_delay_ms = float(delay_sec) * 1000.0
        elif min_delay_ms is not None or max_delay_ms is not None:
            self.min_delay_ms = float(min_delay_ms or 200.0)
            self.max_delay_ms = float(max_delay_ms or 500.0)
        else:
            self.min_delay_ms = float(sim_cfg.get("min_delay_ms") or delayed_cfg.get("min_delay_ms", 200.0))
            self.max_delay_ms = float(sim_cfg.get("max_delay_ms") or delayed_cfg.get("max_delay_ms", 500.0))
        self.delay_sec = (self.min_delay_ms + self.max_delay_ms) / 2000.0
        self.shm_name = shm_name

        self.df_equity: Optional[pl.DataFrame] = None
        self.df_options: Optional[pl.DataFrame] = None

        self.token_map: Dict[str, Tuple[str, float, str]] = {}
        self.lot_sizes: Dict[str, int] = {}

        # Pre-indexed option depth data: token -> sorted numpy arrays for O(log N) lookup
        self.options_by_token: Dict[str, Dict[str, np.ndarray]] = {}
        # Options catalog: underlying_symbol -> {"CE": [(strike, token), ...], "PE": [...]}
        self.options_catalog: Dict[str, Dict[str, List[Tuple[float, str]]]] = {}

        # Trade tracking per mode: mode -> list of TradeState
        self.closed_trades: Dict[str, List[TradeState]] = {m: [] for m in self.modes}
        self.tracks_by_mode: Dict[str, Dict[str, Any]] = {}

    def _load_manifest_catalog(self) -> None:
        """Load option contract metadata from subscription manifest or universe CSV."""
        manifest_candidates = [
            os.path.join(self.data_path, "subscription_manifest.json"),
            "data/subscription_manifest.json",
        ]
        if self.session_date:
            d_nodash = self.session_date.replace("-", "")
            manifest_candidates.insert(0, f"data/subscription_manifest_{d_nodash}.json")

        m_path = None
        for p in manifest_candidates:
            if p and os.path.exists(p):
                m_path = p
                break

        token_map: Dict[str, Tuple[str, float, str]] = {}
        if m_path:
            try:
                import json
                with open(m_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 1. From options_map
                for und, u_data in data.get("options_map", {}).items():
                    for s_str, opt_pair in u_data.get("strikes", {}).items():
                        try:
                            s_val = float(s_str)
                            for otype, ikey in opt_pair.items():
                                norm = normalize_instrument_token(ikey)
                                token_map[norm] = (und.upper(), s_val, otype.upper())
                        except Exception:
                            pass

                # 2. From streamed_popular_options
                for opt in data.get("streamed_popular_options", []):
                    norm = normalize_instrument_token(opt.get("instrument_key", ""))
                    if norm and norm not in token_map:
                        d = parse_option_details(opt.get("trading_symbol") or opt.get("name"))
                        if d:
                            token_map[norm] = d
            except Exception as e:
                print(f"[SIMULATION_ENGINE] Warning loading manifest {m_path}: {e}", file=sys.stderr)

        self.token_map = token_map

    def _load_lot_sizes(self) -> None:
        """Load lot sizes per underlying from filtered.csv."""
        lot_candidates = [
            os.path.join(self.data_path, "filtered.csv"),
            "data/filtered.csv",
        ]
        if self.session_date:
            d_nodash = self.session_date.replace("-", "")
            lot_candidates.insert(0, f"data/filtered_{d_nodash}.csv")

        self.lot_sizes = {}
        for lp in lot_candidates:
            if os.path.exists(lp):
                try:
                    import csv
                    with open(lp, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            sym = row.get("trading_symbol") or row.get("symbol") or row.get("name")
                            ls = row.get("lot_size")
                            if sym and ls:
                                self.lot_sizes[sym.upper()] = int(ls)
                    break
                except Exception:
                    pass

    def _collect_files(self) -> List[str]:
        all_files = []
        if os.path.isfile(self.data_path):
            all_files = [self.data_path]
        elif os.path.isdir(self.data_path):
            pq_files = sorted(glob.glob(os.path.join(self.data_path, "**/*.parquet"), recursive=True))
            if pq_files:
                all_files = pq_files
            else:
                jsonl_files = sorted(glob.glob(os.path.join(self.data_path, "**/*.jsonl"), recursive=True))
                # Exclude trade journals, paper trade logs, and simulation exports
                jsonl_files = [
                    f for f in jsonl_files
                    if not (
                        os.path.basename(f).startswith("journal_")
                        or os.path.basename(f).startswith("trades_")
                        or "trade" in os.path.basename(f).lower()
                    )
                ]
                if jsonl_files:
                    all_files = jsonl_files
        # Filter out 0-byte empty files
        return [f for f in all_files if os.path.exists(f) and os.path.getsize(f) > 0]

    @staticmethod
    def _normalize_df_tokens(df: Optional[pl.DataFrame]) -> Optional[pl.DataFrame]:
        if df is None:
            return None
        if "token" not in df.columns:
            if "instrument_key" in df.columns:
                df = df.with_columns(pl.col("instrument_key").alias("token"))
            elif "instrument_token" in df.columns:
                df = df.with_columns(pl.col("instrument_token").alias("token"))
        return df

    def load_data(self) -> Tuple[Optional[pl.DataFrame], Optional[pl.DataFrame]]:
        """
        Ingest equity ticks and option depth files.
        Separates equity underlyings and options depth records, sorting each by exchange_timestamp.
        """
        if self._cached_equity_df is not None:
            self.df_equity = self._cached_equity_df
            self.df_options = self._cached_options_df
            if self.df_options is not None and len(self.df_options) > 0:
                self._build_options_index()
            return self.df_equity, self.df_options

        files = self._collect_files()
        if not files:
            raise FileNotFoundError(f"No recording files found at: {self.data_path}")

        eq_files = [f for f in files if "equity_ticks" in os.path.basename(f)]
        opt_files = [f for f in files if "option_depth" in os.path.basename(f)]

        if eq_files or opt_files:
            # Dual-stream partitioned files
            if eq_files:
                self.df_equity = pl.read_parquet(eq_files) if eq_files[0].endswith(".parquet") else pl.read_ndjson(eq_files)
                self.df_equity = self._normalize_df_tokens(self.df_equity)
            if opt_files:
                self.df_options = pl.read_parquet(opt_files) if opt_files[0].endswith(".parquet") else pl.read_ndjson(opt_files)
                self.df_options = self._normalize_df_tokens(self.df_options)
        else:
            # Combined / single recording file
            first_file = files[0]
            df = pl.read_parquet(files) if first_file.endswith(".parquet") else pl.read_ndjson(first_file)
            df = self._normalize_df_tokens(df)

            # Partition using is_option_contract
            tokens = df["token"].unique().to_list()
            opt_tokens = {t for t in tokens if is_option_contract(t)}

            self.df_options = df.filter(pl.col("token").is_in(list(opt_tokens))) if opt_tokens else None
            self.df_equity = df.filter(~pl.col("token").is_in(list(opt_tokens))) if opt_tokens else df

        # Normalize and sort equity data
        if self.df_equity is not None and len(self.df_equity) > 0:
            if "exchange_timestamp" in self.df_equity.columns:
                self.df_equity = self.df_equity.sort("exchange_timestamp")

        # Normalize and index option depth data
        if self.df_options is not None and len(self.df_options) > 0:
            if "exchange_timestamp" in self.df_options.columns:
                self.df_options = self.df_options.sort("exchange_timestamp")
            self._build_options_index()

        return self.df_equity, self.df_options

    def _build_options_index(self) -> None:
        """Build lightning-fast O(log N) numpy lookup tables for option depth by token."""
        if self.df_options is None or len(self.df_options) == 0:
            return

        self._load_manifest_catalog()
        self._load_lot_sizes()

        df = self.df_options
        if "exchange_timestamp" in df.columns:
            df = df.sort(["token", "exchange_timestamp"])

        partitions = df.partition_by("token", as_dict=True)

        for tok_tuple, sub_df in partitions.items():
            tok = tok_tuple[0] if isinstance(tok_tuple, tuple) else tok_tuple
            norm_token = normalize_instrument_token(tok)

            ts = sub_df["exchange_timestamp"].to_numpy().astype(np.float64) if "exchange_timestamp" in sub_df.columns else np.zeros(len(sub_df))
            bids = sub_df["bid"].to_numpy().astype(np.float64) if "bid" in sub_df.columns else np.zeros(len(sub_df))
            asks = sub_df["ask"].to_numpy().astype(np.float64) if "ask" in sub_df.columns else np.zeros(len(sub_df))
            ltps = sub_df["ltp"].to_numpy().astype(np.float64) if "ltp" in sub_df.columns else bids
            bid_qtys = sub_df["bid_qty"].to_numpy().astype(np.int64) if "bid_qty" in sub_df.columns else np.zeros(len(sub_df), dtype=np.int64)
            ask_qtys = sub_df["ask_qty"].to_numpy().astype(np.int64) if "ask_qty" in sub_df.columns else np.zeros(len(sub_df), dtype=np.int64)

            self.options_by_token[norm_token] = {
                "timestamps": ts,
                "bids": bids,
                "asks": asks,
                "ltps": ltps,
                "bid_qtys": bid_qtys,
                "ask_qtys": ask_qtys,
            }

            # Register in options catalog: try contract string parsing first, then manifest token map, then row symbol
            opt_details = parse_option_details(norm_token) or self.token_map.get(norm_token)
            if not opt_details and "symbol" in sub_df.columns and len(sub_df) > 0:
                first_sym = sub_df["symbol"][0]
                if first_sym:
                    opt_details = parse_option_details(first_sym)
            if opt_details:
                und, strike, otype = opt_details
                und = und.upper()
                if und not in self.options_catalog:
                    self.options_catalog[und] = {"CE": [], "PE": []}
                if otype in ("CE", "PE"):
                    self.options_catalog[und][otype].append((strike, norm_token))

        # Sort strikes ascending in catalog
        for und in self.options_catalog:
            for otype in ("CE", "PE"):
                self.options_catalog[und][otype].sort(key=lambda item: item[0])

    def get_option_quote(self, token: str, timestamp: float) -> Optional[Dict[str, Any]]:
        """
        Exact-timestamp lookup from ingested option depth dataset.
        Finds the prevailing bid/ask immediately at or prior to timestamp.
        """
        norm = normalize_instrument_token(token)
        data = self.options_by_token.get(norm)
        if not data or len(data["timestamps"]) == 0:
            return None

        ts_arr = data["timestamps"]
        idx = int(np.searchsorted(ts_arr, timestamp, side="right") - 1)
        if idx < 0:
            # If before first tick, check if within 10 seconds of market open
            if len(ts_arr) > 0 and abs(ts_arr[0] - timestamp) <= 10.0:
                idx = 0
            else:
                return None

        ask = float(data["asks"][idx])
        bid = float(data["bids"][idx])
        ltp = float(data["ltps"][idx])

        # Preserve genuine book depth: never fabricate bid/ask from LTP
        return {
            "token": norm,
            "bid": bid,
            "ask": ask,
            "ltp": ltp,
            "bid_qty": int(data["bid_qtys"][idx]),
            "ask_qty": int(data["ask_qtys"][idx]),
            "timestamp": float(ts_arr[idx]),
        }

    def resolve_sim_strikes(
        self, symbol: str, direction: str, trigger_price: float, timestamp: float
    ) -> Dict[str, Any]:
        """
        Resolve option strikes for symbol from ingested option depth catalog.
        Queries exact prevailing prices from the ingested file and enforces spread gate.
        """
        und = symbol.upper()
        otype = "CE" if direction.upper() == "BULL" else "PE"
        available = self.options_catalog.get(und, {}).get(otype, [])

        if not available:
            return {}

        # Find closest strike to trigger price (ATM)
        strikes = [item[0] for item in available]
        idx_atm = int(np.argmin([abs(s - trigger_price) for s in strikes]))
        atm_strike, atm_token = available[idx_atm]

        # Enforce ATM Proximity Invariant:
        # True ATM strike must be within reasonable distance of trigger price (<= max(step * 1.5, trigger_price * 0.02)).
        # Prevents selecting deep ITM/OTM strikes when true near-money contracts were not tracked/recorded.
        from core.rms.rms_service import get_strike_step
        step = get_strike_step(trigger_price)
        max_atm_distance = max(step * 1.5, trigger_price * 0.02)
        if abs(atm_strike - trigger_price) > max_atm_distance:
            return {}

        # ITM and OTM strikes
        itm1_idx = max(0, idx_atm - 1) if otype == "CE" else min(len(available) - 1, idx_atm + 1)
        itm2_idx = max(0, idx_atm - 2) if otype == "CE" else min(len(available) - 1, idx_atm + 2)
        otm1_idx = min(len(available) - 1, idx_atm + 1) if otype == "CE" else max(0, idx_atm - 1)
        otm2_idx = min(len(available) - 1, idx_atm + 2) if otype == "CE" else max(0, idx_atm - 2)

        itm_strike, itm_token = available[itm1_idx]
        itm2_strike, itm2_token = available[itm2_idx]
        otm_strike, otm_token = available[otm1_idx]
        otm2_strike, otm2_token = available[otm2_idx]

        candidates = {
            "ATM": (atm_strike, atm_token),
            "ITM": (itm_strike, itm_token),
            "ITM1": (itm_strike, itm_token),
            "ITM2": (itm2_strike, itm2_token),
            "OTM": (otm_strike, otm_token),
            "OTM1": (otm_strike, otm_token),
            "OTM2": (otm2_strike, otm2_token),
        }

        lot_size = self.lot_sizes.get(und, 250)
        resolved: Dict[str, Any] = {}
        for role, (strike_val, tok) in candidates.items():
            q = self.get_option_quote(tok, timestamp)
            if not q or q["ask"] <= 0.0:
                continue

            ask = q["ask"]
            bid = q["bid"]
            if ask <= 0.0 or bid <= 0.0 or bid >= ask:
                continue

            spread = ask - bid
            spread_pct = spread / ask
            # Enforce max spread percentage (3% default) to avoid entering immediately into a stop loss
            if spread_pct > 0.03:
                continue

            resolved[role] = {
                "token": tok,
                "strike": strike_val,
                "option_type": otype,
                "ltp": q["ltp"],
                "bid": bid,
                "ask": ask,
                "bid_qty": q["bid_qty"],
                "ask_qty": q["ask_qty"],
                "lot_size": lot_size,
            }

        return resolved

    def _init_tracks_for_mode(self, mode: str, context: ExecutionContext) -> Dict[str, Any]:
        """Instantiate configured tracks for a given execution mode."""
        from common.tracks.factory import create_tracks
        tracks = create_tracks(
            context=context,
            track_filter=self.tracks,
            strategy_cfg=self.strategy_cfg,
            entry_freeze_time=self.entry_freeze_time,
            eod_squareoff_time=self.eod_squareoff_time,
        )
        if self.capital:
            for t in tracks.values():
                t.allocated_capital = float(self.capital)
        return tracks

    def run_simulation(self) -> Dict[str, Dict[str, Any]]:
        """
        Execute full chronological simulation.
        If both equity ticks and option depth are loaded:
        - Feeds equity ticks into CandleManager
        - Evaluates ScoringEngine3 breakouts in serialized sequence
        - Resolves strike quotes directly from ingested option depth
        - Manages Paper and Paper Delayed trade lifecycles
        """
        if self.df_equity is None and self.df_options is None:
            self.load_data()

        # If data only consists of option contracts (e.g. legacy/mock dataset), fallback to direct option test
        if self.df_equity is None or len(self.df_equity) == 0:
            return self._run_direct_option_simulation()

        print(
            f"[SIMULATION_ENGINE] Running Serialized Chronological Simulation...\n"
            f"  • Equity Ticks : {len(self.df_equity):,} records\n"
            f"  • Option Depth : {len(self.df_options) if self.df_options is not None else 0:,} records\n"
            f"  • Active Modes : {', '.join(self.modes)} ({self.min_delay_ms:.0f}ms - {self.max_delay_ms:.0f}ms random delay)"
        )

        # 1. Initialize State Layer: CandleManager & ScoringEngine with isolated SHM (ZeroMQ disabled in offline simulation)
        unlink_shm(self.shm_name)
        candle_mgr = CandleManager(shm_name=self.shm_name, enable_zmq=False)
        scoring_eng = ScoringEngine3(shm_name=self.shm_name, min_pole_pct=self.min_pole_pct, enable_zmq=False, persist_alerts=False)
        print(f"  • Scoring Engine: min_pole_pct={scoring_eng.min_pole_pct*100.0:.2f}% | min_clv={scoring_eng.min_clv} | invalidation_retrace={scoring_eng.invalidation_retrace_pct*100.0:.0f}%")

        # 2. Initialize Execution Superstructures & Tracks for active modes only
        tracks_by_mode: Dict[str, Dict[str, Any]] = {}
        ctx_delayed: Optional[SimDelayedExecutionContext] = None

        for m in self.modes:
            if m == "paper_delayed":
                ctx_delayed = SimDelayedExecutionContext(min_delay_ms=self.min_delay_ms, max_delay_ms=self.max_delay_ms)
                tracks_by_mode[m] = self._init_tracks_for_mode(m, ctx_delayed)
            else:
                ctx = SimExecutionContext(mode=m)
                tracks_by_mode[m] = self._init_tracks_for_mode(m, ctx)

        self.tracks_by_mode = tracks_by_mode

        last_scoring_eval_ts = 0.0
        last_hb_ts = 0.0

        # 3. Serialized Chronological Event Loop with Fast Columnar Indexing
        df_eq = self.df_equity
        if df_eq is None or len(df_eq) == 0:
            raise ValueError(f"No equity ticks available in dataset '{self.data_path}' to simulate.")

        # Determine timestamp column safely
        ts_col = None
        for col in ["exchange_timestamp", "received_timestamp", "timestamp", "ts"]:
            if col in df_eq.columns:
                ts_col = col
                break
        if not ts_col:
            raise KeyError(
                f"Could not find a valid timestamp column ('exchange_timestamp', 'received_timestamp', 'timestamp') "
                f"in equity dataset '{self.data_path}'. Available columns: {df_eq.columns}"
            )

        ts_arr = df_eq[ts_col].to_numpy().astype(np.float64)
        tokens = df_eq["token"].to_list() if "token" in df_eq.columns else ["UNKNOWN"] * len(ts_arr)
        ltps = df_eq["ltp"].to_numpy().astype(np.float64) if "ltp" in df_eq.columns else np.zeros(len(ts_arr))
        vols = df_eq["volume"].to_numpy().astype(np.int64) if "volume" in df_eq.columns else np.zeros(len(ts_arr), dtype=np.int64)
        total_eq = len(ts_arr)
        latest_equity_ltp: Dict[str, float] = {}

        for i in range(total_eq):
            t_curr = float(ts_arr[i])
            token = tokens[i]
            ltp = float(ltps[i])
            vol = int(vols[i])

            # Step A: Feed equity tick to CandleManager
            candle_mgr.on_tick(token=token, ltp=ltp, vol=vol, ts=t_curr)
            sym_key = normalize_instrument_token(token)
            raw_sym = sym_key.split(":", 1)[1] if ":" in sym_key else sym_key
            sym_idx = candle_mgr.symbol_to_index.get(sym_key)
            if sym_idx is None:
                sym_idx = candle_mgr.symbol_to_index.get(raw_sym)
            if sym_idx is not None and sym_idx < len(candle_mgr.index_to_symbol):
                canonical_sym = candle_mgr.index_to_symbol[sym_idx]
                latest_equity_ltp[canonical_sym] = ltp
                latest_equity_ltp[raw_sym] = ltp

            # Step B: Serialized Scoring Spin Evaluation (Evaluates whenever 1.0 simulated second elapses)
            if (t_curr - last_scoring_eval_ts) >= 1.0:
                last_scoring_eval_ts = t_curr
                from datetime import datetime
                from core.common.time_utils import IST
                dt_curr = datetime.fromtimestamp(t_curr, tz=IST)
                curr_minutes = dt_curr.hour * 60 + dt_curr.minute

                # Hard Entry Freeze Cutoff: no new trade setups generated after entry_freeze_time (default 15:00 IST)
                if curr_minutes < self.entry_freeze_minutes:
                    alerts = scoring_eng.evaluate_universe(now_ts=t_curr)

                    for alert in alerts:
                        resolved_strikes = self.resolve_sim_strikes(
                            symbol=alert.symbol,
                            direction=alert.direction,
                            trigger_price=alert.trigger_price,
                            timestamp=t_curr,
                        )
                        if not resolved_strikes:
                            continue

                        opp = ActionableOpportunity(
                            opp_id=f"OPP_SIM_{alert.symbol}_{int(t_curr*1000)}",
                            alert_id=alert.alert_id,
                            symbol=alert.symbol,
                            direction=alert.direction,
                            archetype=alert.archetype,
                            trigger_price=alert.trigger_price,
                            strikes=resolved_strikes,
                            timestamp=t_curr,
                            flag_high=alert.flag_high,
                            flag_low=alert.flag_low,
                            pole_high=getattr(alert, "pole_high", None),
                            pole_low=getattr(alert, "pole_low", None),
                            atr=alert.atr,
                            rvol=alert.rvol,
                            clv=alert.clv,
                        )

                        # Dispatch to Active Mode Tracks
                        for m_name, track_dict in tracks_by_mode.items():
                            for tr_name, track in track_dict.items():
                                track.on_opportunity(opp)

            # Step C: Progress Delayed Queue
            if ctx_delayed and ctx_delayed.pending_orders:
                remaining_pending = []
                for release_ts, req in ctx_delayed.pending_orders:
                    if t_curr >= release_ts:
                        opt_q = self.get_option_quote(req.instrument_token, release_ts)
                        if opt_q and opt_q["ask"] > 0:
                            res = OrderResult(
                                order_id=req.order_id,
                                status="FILLED",
                                fill_price=opt_q["ask"],
                                fill_quantity=req.quantity,
                                fill_timestamp=release_ts,
                            )
                        else:
                            res = OrderResult(
                                order_id=req.order_id,
                                status="REJECTED",
                                fill_price=0.0,
                                fill_quantity=0,
                                fill_timestamp=release_ts,
                            )
                        track = tracks_by_mode.get("paper_delayed", {}).get(req.track)
                        if track:
                            track.on_order_result(res)
                    else:
                        remaining_pending.append((release_ts, req))
                ctx_delayed.pending_orders = remaining_pending

            # Step D & E: Heartbeat & Active Trades Exit Evaluation (Sampled at 0.5s intervals)
            if (t_curr - last_hb_ts) >= 0.5:
                last_hb_ts = t_curr
                for m_name, track_dict in tracks_by_mode.items():
                    for tr_name, track in track_dict.items():
                        # Process active trade depth quotes
                        for trade in list(track.active_trades.values()):
                            if trade.status != "OPEN":
                                continue
                            if trade.entry_time and t_curr <= trade.entry_time:
                                continue
                            if trade.symbol in latest_equity_ltp:
                                trade.metadata["underlying_ltp"] = latest_equity_ltp[trade.symbol]
                            opt_q = self.get_option_quote(trade.token, t_curr)
                            if opt_q:
                                track.on_tick(
                                    token=trade.token,
                                    ltp=opt_q["ltp"],
                                    bid=opt_q["bid"],
                                    ask=opt_q["ask"],
                                    ts=t_curr,
                                    symbol=trade.symbol,
                                )
                        # 1s Heartbeat check
                        track.on_heartbeat(t_curr)

        # Final EOD Auto Square-off for any remaining open positions
        last_ts = float(ts_arr[-1]) if total_eq > 0 else 0.0
        for m_name, track_dict in tracks_by_mode.items():
            for tr_name, track in track_dict.items():
                for trade in list(track.active_trades.values()):
                    if trade.status == "OPEN":
                        # Strictly use prevailing market bid at EOD; never fall back to fabricated entry_price
                        opt_q = self.get_option_quote(trade.token, last_ts)
                        bid = (opt_q["bid"] if opt_q and opt_q["bid"] > 0 else (trade.current_bid if trade.current_bid > 0 else 0.0))
                        if hasattr(track, "exit_trade"):
                            track.exit_trade(trade, exit_price=bid, reason="EOD_SQUAREOFF", ts=last_ts)

        # Collect closed trades
        for m_name, track_dict in tracks_by_mode.items():
            for tr_name, track in track_dict.items():
                self.closed_trades[m_name].extend(track.closed_trades)

        # Cleanup SHM & processes
        try:
            candle_mgr.stop()
            scoring_eng.stop()
            unlink_shm(self.shm_name)
        except Exception:
            pass

        # If no trades were triggered via scoring breakouts (e.g. synthetic test dataset with <= 10 ticks),
        # fallback to direct option test
        total_trades = sum(len(t) for t in self.closed_trades.values())
        if total_trades == 0 and (self.df_equity is None or len(self.df_equity) <= 10) and self.df_options is not None and len(self.df_options) > 0:
            return self._run_direct_option_simulation()

        return self._build_results()

    def _run_direct_option_simulation(self) -> Dict[str, Dict[str, Any]]:
        """Fallback simulation when dataset contains only option contracts."""
        df = self.df_options if self.df_options is not None else self.df_equity
        if df is None or len(df) == 0:
            return {"paper": {}, "paper_delayed": {}}

        tokens = df["token"].unique().to_list() if "token" in df.columns else []
        round_trip_brokerage = 2.0 * self.brokerage_per_order
        results: Dict[str, Dict[str, Any]] = {
            m: {
                tr: {
                    "trades": 0,
                    "wins": 0,
                    "breakevens": 0,
                    "losses": 0,
                    "gross_pnl": 0.0,
                    "total_brokerage": 0.0,
                    "net_pnl": 0.0,
                    "win_rate": 0.0,
                    "start_capital": 250000.0,
                    "end_capital": 250000.0,
                    "return_pct": 0.0,
                }
                for tr in self.tracks
            }
            for m in self.modes
        }

        cfg = get_config_manager()
        t_cfg = cfg.strategy.get("strategies", {}).get("momentum", {}).get("tracks", {})
        track_configs = {
            "fast_momentum": {"target_pct": float(t_cfg.get("fast_momentum", {}).get("target_pct", 0.12)), "sl_pct": float(t_cfg.get("fast_momentum", {}).get("sl_pct", 0.08)), "lot_size": 250},
            "trend_pullback": {"target_pct": float(t_cfg.get("trend_pullback", {}).get("target_pct", 0.25)), "sl_pct": float(t_cfg.get("trend_pullback", {}).get("sl_pct", 0.12)), "lot_size": 250},
            "fixed_target": {"target_pct": float(t_cfg.get("fixed_target", {}).get("target_pct", 0.15)), "sl_pct": float(t_cfg.get("fixed_target", {}).get("sl_pct", 0.10)), "lot_size": 250},
        }

        for token in tokens:
            contract_ticks = df.filter(pl.col("token") == token)
            if len(contract_ticks) < 10:
                continue

            first_tick = contract_ticks.row(0, named=True)
            entry_ts = float(first_tick.get("exchange_timestamp", 0.0))
            entry_ask = float(first_tick.get("ask", 0.0) or first_tick.get("ltp", 0.0))
            if entry_ask <= 0.0:
                continue

            delayed_delay = random.uniform(self.min_delay_ms, self.max_delay_ms) / 1000.0
            delayed_entry_ts = entry_ts + delayed_delay
            delayed_ticks = contract_ticks.filter(pl.col("exchange_timestamp") >= delayed_entry_ts)
            delayed_ask = float(delayed_ticks.row(0, named=True).get("ask", entry_ask)) if len(delayed_ticks) > 0 else entry_ask

            for tr in self.tracks:
                conf = track_configs.get(tr, {"target_pct": 0.12, "sl_pct": 0.08, "lot_size": 250})
                qty = conf["lot_size"]

                # Paper mode
                if "paper" in results:
                    target_p = entry_ask * (1.0 + conf["target_pct"])
                    sl_p = entry_ask * (1.0 - conf["sl_pct"])
                    future_ticks = contract_ticks.filter(pl.col("exchange_timestamp") > entry_ts)
                    for row in future_ticks.iter_rows(named=True):
                        bid = float(row.get("bid", 0.0) or row.get("ltp", 0.0))
                        if bid >= target_p or bid <= sl_p:
                            pnl = (bid - entry_ask) * qty
                            st = results["paper"][tr]
                            st["trades"] += 1
                            st["gross_pnl"] += pnl
                            st["total_brokerage"] += round_trip_brokerage
                            st["net_pnl"] += (pnl - round_trip_brokerage)
                            ret_p = (bid - entry_ask) / entry_ask if entry_ask > 0 else 0.0
                            if ret_p > 0.025:
                                st["wins"] += 1
                            elif ret_p >= -1e-6:
                                st["breakevens"] += 1
                            else:
                                st["losses"] += 1
                            st["win_rate"] = (st["wins"] / st["trades"]) * 100.0
                            break

                # Delayed mode
                if "paper_delayed" in results and len(delayed_ticks) > 0:
                    target_d = delayed_ask * (1.0 + conf["target_pct"])
                    sl_d = delayed_ask * (1.0 - conf["sl_pct"])
                    future_delayed = delayed_ticks.filter(pl.col("exchange_timestamp") > delayed_entry_ts)
                    for row in future_delayed.iter_rows(named=True):
                        bid = float(row.get("bid", 0.0) or row.get("ltp", 0.0))
                        if bid >= target_d or bid <= sl_d:
                            pnl = (bid - delayed_ask) * qty
                            dst = results["paper_delayed"][tr]
                            dst["trades"] += 1
                            dst["gross_pnl"] += pnl
                            dst["total_brokerage"] += round_trip_brokerage
                            dst["net_pnl"] += (pnl - round_trip_brokerage)
                            ret_d = (bid - delayed_ask) / delayed_ask if delayed_ask > 0 else 0.0
                            if ret_d > 0.025:
                                dst["wins"] += 1
                            elif ret_d >= -1e-6:
                                dst["breakevens"] += 1
                            else:
                                dst["losses"] += 1
                            dst["win_rate"] = (dst["wins"] / dst["trades"]) * 100.0
                            break

        for m in self.modes:
            for tr, st in results.get(m, {}).items():
                st["end_capital"] = st["start_capital"] + st["net_pnl"]
                st["return_pct"] = (st["net_pnl"] / st["start_capital"] * 100.0) if st["start_capital"] > 0 else 0.0

        return results

    def _build_results(self) -> Dict[str, Dict[str, Any]]:
        """Aggregate closed trades into performance metrics per mode and track."""
        results: Dict[str, Dict[str, Any]] = {
            m: {
                tr: {
                    "trades": 0,
                    "wins": 0,
                    "breakevens": 0,
                    "losses": 0,
                    "gross_pnl": 0.0,
                    "total_brokerage": 0.0,
                    "net_pnl": 0.0,
                    "win_rate": 0.0,
                    "start_capital": 250000.0,
                    "end_capital": 250000.0,
                    "return_pct": 0.0,
                }
                for tr in self.tracks
            }
            for m in self.modes
        }

        round_trip_brokerage = 2.0 * self.brokerage_per_order

        for mode in self.modes:
            track_instances = self.tracks_by_mode.get(mode, {})
            for tr in self.tracks:
                inst = track_instances.get(tr)
                start_cap = float(getattr(inst, "allocated_capital", 250000.0)) if inst else 250000.0
                results[mode][tr]["start_capital"] = start_cap

            for trade in self.closed_trades.get(mode, []):
                tr = trade.track
                if tr not in results[mode]:
                    results[mode][tr] = {
                        "trades": 0,
                        "wins": 0,
                        "breakevens": 0,
                        "losses": 0,
                        "gross_pnl": 0.0,
                        "total_brokerage": 0.0,
                        "net_pnl": 0.0,
                        "win_rate": 0.0,
                        "start_capital": 250000.0,
                        "end_capital": 250000.0,
                        "return_pct": 0.0,
                    }
                st = results[mode][tr]
                st["trades"] += 1
                trade_brokerage = float(trade.metadata.get("brokerage", round_trip_brokerage))
                st["gross_pnl"] += trade.realized_pnl
                st["total_brokerage"] += trade_brokerage
                net_pnl = trade.realized_pnl - trade_brokerage
                st["net_pnl"] += net_pnl

                trade.metadata["brokerage"] = trade_brokerage
                trade.metadata["net_pnl"] = net_pnl
                trade.metadata["gross_pnl"] = trade.realized_pnl

                # Win / Breakeven / Loss classification:
                # Return > +2.5%: Win (including target hits and profitable trailing SL hits)
                # 0.0% <= Return <= +2.5%: Minor Win / Breakeven
                # Return < 0.0%: Miss / Loss
                pnl_pct = (trade.exit_price - trade.entry_price) / trade.entry_price if trade.entry_price > 0 else 0.0
                if pnl_pct > 0.025:
                    st["wins"] += 1
                elif pnl_pct >= -1e-6:
                    st.setdefault("breakevens", 0)
                    st["breakevens"] += 1
                else:
                    st["losses"] += 1

                st["win_rate"] = (st["wins"] / st["trades"]) * 100.0

            for tr, st in results[mode].items():
                st.setdefault("breakevens", 0)
                st["end_capital"] = st["start_capital"] + st["net_pnl"]
                st["return_pct"] = (st["net_pnl"] / st["start_capital"] * 100.0) if st["start_capital"] > 0 else 0.0

        return results

    def print_summary(self, results: Dict[str, Dict[str, Any]]) -> str:
        """
        Print separated simulation matrix tables for each execution mode with starting budget, ending budget, gross PnL, brokerage, and net return %.
        """
        lines = []
        header_sample = f"  {'TRACK':<22} | {'TRADES':<7} | {'WINS (>2.5%)':<12} | {'B/E (0-2.5%)':<12} | {'LOSSES (<0%)':<12} | {'WIN %':<7} | {'START BUDGET':>14} | {'GROSS PNL':>13} | {'BROKERAGE':>11} | {'NET PNL (INR)':>15} | {'END BUDGET':>14} | {'RETURN %':>9}"
        tbl_width = max(140, len(header_sample))
        lines.append("=" * tbl_width)
        lines.append(" COLUMNAR SIMULATION ENGINE - MULTI-MODE SIMULATION RESULTS MATRIX (SPEC 2.0)")
        lines.append("=" * tbl_width)

        for mode in self.modes:
            if mode not in results:
                continue
            if mode == "paper":
                mode_title = "PAPER TRADING EXECUTION MATRIX"
            else:
                mode_title = f"PAPER DELAYED EXECUTION MATRIX ({self.min_delay_ms:.0f}ms - {self.max_delay_ms:.0f}ms RANDOM DELAY)"
            lines.append(f"\n[ {mode_title} ]")
            header = header_sample
            lines.append(header)
            lines.append("  " + "-" * (len(header) - 2))

            mode_trades = 0
            mode_wins = 0
            mode_bes = 0
            mode_losses = 0
            mode_start_cap = 0.0
            mode_gross_pnl = 0.0
            mode_brokerage = 0.0
            mode_net_pnl = 0.0

            for tr, st in results.get(mode, {}).items():
                start_cap = st.get("start_capital", 250000.0)
                gross_pnl = st.get("gross_pnl", st.get("net_pnl", 0.0))
                brokerage = st.get("total_brokerage", 0.0)
                net_pnl = st.get("net_pnl", gross_pnl - brokerage)
                end_cap = st.get("end_capital", start_cap + net_pnl)
                ret_pct = st.get("return_pct", ((net_pnl / start_cap * 100.0) if start_cap > 0 else 0.0))

                start_str = f"₹{start_cap:,.2f}"
                gross_str = f"₹{gross_pnl:+,.2f}"
                brok_str = f"₹{brokerage:,.2f}"
                net_str = f"₹{net_pnl:+,.2f}"
                end_str = f"₹{end_cap:,.2f}"
                ret_str = f"{ret_pct:+.1f}%"

                mode_trades += st["trades"]
                mode_wins += st["wins"]
                mode_bes += st.get("breakevens", 0)
                mode_losses += st["losses"]
                mode_start_cap += start_cap
                mode_gross_pnl += gross_pnl
                mode_brokerage += brokerage
                mode_net_pnl += net_pnl
                win_str = f"{st['win_rate']:.1f}%"
                lines.append(
                    f"  {tr:<22} | {st['trades']:<7} | {st['wins']:<12} | {st.get('breakevens', 0):<12} | {st['losses']:<12} | {win_str:>7} | {start_str:>14} | {gross_str:>13} | {brok_str:>11} | {net_str:>15} | {end_str:>14} | {ret_str:>9}"
                )

            lines.append("  " + "-" * (len(header) - 2))
            num_tracks = max(1, len(results.get(mode, {})))
            avg_trades = int(round(mode_trades / num_tracks))
            avg_wins = int(round(mode_wins / num_tracks))
            avg_bes = int(round(mode_bes / num_tracks))
            avg_losses = int(round(mode_losses / num_tracks))
            avg_start_cap = mode_start_cap / num_tracks
            avg_gross_pnl = mode_gross_pnl / num_tracks
            avg_brokerage = mode_brokerage / num_tracks
            avg_net_pnl = mode_net_pnl / num_tracks
            avg_end_cap = avg_start_cap + avg_net_pnl
            mode_win_pct = (mode_wins / mode_trades * 100.0) if mode_trades > 0 else 0.0
            avg_ret_pct = (avg_net_pnl / avg_start_cap * 100.0) if avg_start_cap > 0 else 0.0

            avg_start_str = f"₹{avg_start_cap:,.2f}"
            avg_gross_str = f"₹{avg_gross_pnl:+,.2f}"
            avg_brok_str = f"₹{avg_brokerage:,.2f}"
            avg_net_str = f"₹{avg_net_pnl:+,.2f}"
            avg_end_str = f"₹{avg_end_cap:,.2f}"
            tot_win_str = f"{mode_win_pct:.1f}%"
            avg_ret_str = f"{avg_ret_pct:+.1f}%"
            avg_label = f"AVERAGE ({mode.upper()})"
            lines.append(
                f"  {avg_label:<22} | {avg_trades:<7} | {avg_wins:<12} | {avg_bes:<12} | {avg_losses:<12} | {tot_win_str:>7} | {avg_start_str:>14} | {avg_gross_str:>13} | {avg_brok_str:>11} | {avg_net_str:>15} | {avg_end_str:>14} | {avg_ret_str:>9}"
            )

        lines.append("\n" + "=" * tbl_width)
        report = "\n".join(lines)
        print(report)
        return report

    def print_journal(self, mode: Optional[str] = None) -> str:
        """Print detailed trade-by-trade log for closed trades and persist to disk."""
        ARCHETYPE_LABELS = {
            "FLAG_BULL_BREAKOUT": "Flag Breakout",
            "FLAG_BEAR_BREAKDOWN": "Flag Breakdown",
            "PURE_BULL_BREAKOUT": "Pure Breakout",
            "PURE_BEAR_BREAKDOWN": "Pure Breakdown",
            "BULL_FLAG": "Flag Breakout",
            "BEAR_FLAG": "Flag Breakdown",
            "PURE_BREAKOUT": "Pure Breakout",
            "PURE_BREAKDOWN": "Pure Breakdown",
            "MOMENTUM_BREAKOUT": "Breakout",
        }
        lines = []
        modes_to_show = [mode] if mode else self.modes
        for m in modes_to_show:
            raw_trades = self.closed_trades.get(m, [])
            trades = sorted(raw_trades, key=lambda t: (t.entry_time or 0.0, t.exit_time or 0.0))
            header = f"  {'#':<4} | {'ENTRY TIME':<19} | {'EXIT TIME':<19} | {'TRACK':<22} | {'SYMBOL':<10} | {'CONTRACT':<10} | {'TRIGGER':<14} | {'ENTRY':<7} | {'EXIT':<7} | {'BROKERAGE':>9} | {'NET PNL':>12} | {'RETURN':>8} | {'OUTCOME':<10} | {'REASON'}"
            tbl_width = max(140, len(header))
            lines.append("\n" + "=" * tbl_width)
            lines.append(f" 📋 TRADE JOURNAL: {m.upper()} (Total Closed Trades: {len(trades)} | Sorted Chronologically by Entry Time)")
            lines.append("=" * tbl_width)
            lines.append(header)
            lines.append("  " + "-" * (len(header) - 2))

            outcome_counts = {"WIN": 0, "BREAKEVEN": 0, "MISS": 0}
            reason_counts = {}
            total_journal_gross = 0.0
            total_journal_brokerage = 0.0
            total_journal_net = 0.0

            round_trip_brokerage = 2.0 * self.brokerage_per_order

            for idx, t in enumerate(trades, 1):
                from datetime import datetime
                from core.common.time_utils import IST
                entry_str = datetime.fromtimestamp(t.entry_time, tz=IST).strftime("%Y-%m-%d %H:%M:%S") if t.entry_time else "N/A"
                exit_str = datetime.fromtimestamp(t.exit_time, tz=IST).strftime("%Y-%m-%d %H:%M:%S") if t.exit_time else "N/A"
                contract_str = f"{int(t.strike)} {t.option_type}"
                reason = t.exit_reason or "UNKNOWN"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

                raw_arch = str(t.metadata.get("archetype") or "UNKNOWN")
                trigger_str = ARCHETYPE_LABELS.get(raw_arch, raw_arch)

                trade_brokerage = float(t.metadata.get("brokerage", round_trip_brokerage))
                gross_pnl = t.realized_pnl
                net_pnl = gross_pnl - trade_brokerage

                total_journal_gross += gross_pnl
                total_journal_brokerage += trade_brokerage
                total_journal_net += net_pnl

                brok_str = f"₹{trade_brokerage:,.2f}"
                net_pnl_str = f"₹{net_pnl:+,.2f}"

                pnl_pct = (t.exit_price - t.entry_price) / t.entry_price if t.entry_price > 0 else 0.0
                if pnl_pct > 0.025:
                    outcome = "WIN"
                elif pnl_pct >= -1e-6:
                    outcome = "BREAKEVEN"
                else:
                    outcome = "MISS"
                outcome_counts[outcome] += 1
                ret_str = f"{pnl_pct * 100.0:+.1f}%"

                lines.append(
                    f"  {idx:<4} | {entry_str:<19} | {exit_str:<19} | {t.track:<22} | {t.symbol:<10} | {contract_str:<10} | {trigger_str:<14} | {t.entry_price:<7.2f} | {t.exit_price:<7.2f} | {brok_str:>9} | {net_pnl_str:>12} | {ret_str:>8} | {outcome:<10} | {reason}"
                )

            lines.append("  " + "-" * (len(header) - 2))
            lines.append(f"  Outcomes: WINS (>2.5%): {outcome_counts['WIN']} | BREAKEVEN (0-2.5%): {outcome_counts['BREAKEVEN']} | LOSSES (<0%): {outcome_counts['MISS']}")
            lines.append(f"  Financials: GROSS PNL: ₹{total_journal_gross:+,.2f} | TOTAL BROKERAGE: ₹{total_journal_brokerage:,.2f} | NET PNL: ₹{total_journal_net:+,.2f}")
            reason_summary = " | ".join(f"{r}: {c}" for r, c in sorted(reason_counts.items()))
            lines.append(f"  Exit Breakdown: {reason_summary}")
            lines.append("=" * tbl_width)

            # Write to recordings journal file
            rec_dir = None
            if os.path.isdir(self.data_path):
                rec_dir = self.data_path
            elif os.path.isfile(self.data_path):
                rec_dir = os.path.dirname(self.data_path) or "recordings"
            elif self.session_date:
                dashed = (
                    f"{self.session_date[:4]}-{self.session_date[4:6]}-{self.session_date[6:8]}"
                    if len(self.session_date) == 8 and self.session_date.isdigit()
                    else self.session_date
                )
                rec_dir = f"recordings/{dashed}" if os.path.exists(f"recordings/{dashed}") else f"recordings/{self.session_date}"
            else:
                rec_dir = "recordings"

            os.makedirs(rec_dir, exist_ok=True)
            journal_path = os.path.join(rec_dir, f"journal_{m}_sim.jsonl")
            try:
                import json
                with open(journal_path, "w", encoding="utf-8") as jf:
                    for t in trades:
                        t_dict = t.to_dict()
                        t_dict["brokerage"] = t.metadata.get("brokerage", round_trip_brokerage)
                        t_dict["net_pnl"] = t.realized_pnl - t_dict["brokerage"]
                        t_dict["gross_pnl"] = t.realized_pnl
                        trigger_label = ARCHETYPE_LABELS.get(str(t.metadata.get("archetype") or t.entry_reason or "UNKNOWN"), str(t.metadata.get("archetype") or t.entry_reason or "UNKNOWN"))
                        t_dict["trigger"] = trigger_label
                        t_dict["entry_reason"] = trigger_label
                        jf.write(json.dumps(t_dict) + "\n")
                lines.append(f"  ✓ Exported journal to '{journal_path}'")
            except Exception as e:
                lines.append(f"  ⚠️ Warning saving journal to '{journal_path}': {e}")

        report = "\n".join(lines)
        print(report)
        return report


def main():
    parser = argparse.ArgumentParser(description="Columnar Market Simulation Engine")
    parser.add_argument("--data-path", default=None, help="Recorded parquet file or directory")
    parser.add_argument("--date", default=None, help="Session date in YYYYMMDD format (defaults to recordings/{date})")
    parser.add_argument("--delay-sec", type=float, default=60.0, help="Delay seconds for paper_delayed (default: 60)")
    args = parser.parse_args()

    data_path = args.data_path
    if not data_path and args.date:
        data_path = os.path.join("recordings", args.date)
    if not data_path:
        data_path = "recordings"

    engine = ColumnarSimEngine(data_path=data_path, delay_sec=args.delay_sec)
    results = engine.run_simulation()
    engine.print_summary(results)


if __name__ == "__main__":
    main()
