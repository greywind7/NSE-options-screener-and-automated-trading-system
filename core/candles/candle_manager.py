"""
CandleManager: Single Writer & Owner of Fixed Shared Memory Segment (shm_ohlc_221).
Aggregates live 5-minute OHLCV bars for 221 F&O equities aligned to wall-clock boundaries (09:15-15:30 IST).
Computes incremental Wilder's ATR-14 on bar closes.
Calculates strict current candle rVOL with linear intra-minute extrapolation against 30-day baseline.
Synchronizes updates via 16-byte Seqlock headers for microsecond lock-free reader access.
"""
import argparse
import csv
import json
import math
import os
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import numpy as np
import zmq

from core.candles.shm_layout import (
    CANDLE_DTYPE,
    SEQLOCK_DTYPE,
    NUM_SYMBOLS,
    NUM_BARS,
    SHM_NAME,
    create_or_attach_shm,
    get_shm_views,
    unlink_shm,
)
from core.common.symbol_utils import normalize_instrument_token
from core.common.ipc_schemas import unpack_message
from core.config.trading_schedule import get_trading_schedule, NSE_SCHEDULE

DEFAULT_FEED_SUB_IPC = "ipc:///tmp/feed_equity.ipc"


class CandleManager:
    def __init__(
        self,
        sub_ipc: Optional[str] = None,
        shm_name: str = SHM_NAME,
        universe_csv: str = "data/filtered.csv",
        baseline_file: str = "data/index.json",
        exchange: str = "NSE",
    ):
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        ipc_endpoints = cfg.get_ipc_endpoints()
        self.sub_ipc = sub_ipc or ipc_endpoints.get("feed_equity_ipc", DEFAULT_FEED_SUB_IPC)
        self.shm_name = shm_name
        self.universe_csv = universe_csv
        self.baseline_file = baseline_file
        self.schedule = get_trading_schedule(exchange)
        self.running = False

        # Load 221 Universe symbols
        self.symbol_to_index: Dict[str, int] = {}
        self.index_to_symbol: List[str] = []
        self._load_universe()

        # Load 30-Day 1-minute volume baseline table (221, 375)
        self.volume_baseline = np.zeros((NUM_SYMBOLS, 375), dtype=np.float64)
        self._load_volume_baseline()

        # Allocate and own Shared Memory (Single Writer)
        print(f"[CANDLE_MANAGER] Initializing Shared Memory segment '{self.shm_name}' (777 KB)...")
        self.shm = create_or_attach_shm(name=self.shm_name, create=True)
        self.headers, self.candles = get_shm_views(self.shm)

        # Initialize memory with zeros
        self.headers[:] = 0
        self.candles[:] = 0

        # In-memory incremental calculation state per symbol
        self.atr14 = np.zeros(NUM_SYMBOLS, dtype=np.float64)
        self.prev_close = np.zeros(NUM_SYMBOLS, dtype=np.float64)
        self.prev_cum_vol = np.zeros(NUM_SYMBOLS, dtype=np.int64)
        self.bar_start_vol = np.zeros(NUM_SYMBOLS, dtype=np.int64)
        self.min_start_vol = np.zeros(NUM_SYMBOLS, dtype=np.int64)
        self.vol_at_min_edges = np.zeros((NUM_SYMBOLS, 5), dtype=np.int64)
        self.current_bar_idx = np.full(NUM_SYMBOLS, -1, dtype=np.int32)
        self.current_min_idx = np.zeros(NUM_SYMBOLS, dtype=np.int32)

        # ZeroMQ Subscriber
        self.ctx = zmq.Context()
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.RCVHWM, 100000)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"TICK.EQ")
        self.sub.connect(self.sub_ipc)

    def _load_universe(self) -> None:
        """Load exactly active 221 symbols from data/filtered.csv and build token mapping."""
        symbols = []
        extra_keys: Dict[str, int] = {}

        if os.path.exists(self.universe_csv):
            with open(self.universe_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = (
                        row.get("trading_symbol")
                        or row.get("tradingsymbol")
                        or row.get("symbol")
                        or row.get("display_name")
                    )
                    if sym:
                        sym = sym.strip().upper()
                        idx = len(symbols)
                        symbols.append(sym)
                        # Also map instrument key if present
                        ik = row.get("upstox_instrument_key") or row.get("instrument_key")
                        if ik:
                            extra_keys[ik.strip()] = idx
                            extra_keys[normalize_instrument_token(ik.strip())] = idx
        else:
            print(f"[CANDLE_MANAGER] Warning: {self.universe_csv} not found. Generating default slot index.")
            symbols = [f"STOCK_{i}" for i in range(NUM_SYMBOLS)]

        # Clamp/pad to NUM_SYMBOLS (221)
        symbols = symbols[:NUM_SYMBOLS]
        while len(symbols) < NUM_SYMBOLS:
            symbols.append(f"RESERVED_{len(symbols)}")

        self.index_to_symbol = symbols
        self.symbol_to_index = {}
        for idx, sym in enumerate(symbols):
            self.symbol_to_index[sym] = idx
            self.symbol_to_index[f"NSE:{sym}"] = idx

        # Merge instrument key mappings
        for k, idx in extra_keys.items():
            if idx < len(symbols):
                self.symbol_to_index[k] = idx

        print(f"[CANDLE_MANAGER] Loaded universe: {len(symbols)} symbols mapped to SHM rows 0..{len(symbols)-1}")

    def _load_volume_baseline(self) -> None:
        """Load 30-day 1-minute historical median volume profile."""
        # 1. Initialize default uniform baseline first (1,000 shares/min)
        self.volume_baseline.fill(1000.0)

        npy_path = "data/volume_profile_221.npy"
        if os.path.exists(npy_path):
            arr = np.load(npy_path)
            if arr.shape == (NUM_SYMBOLS, 375):
                self.volume_baseline = arr
                print(f"[CANDLE_MANAGER] Loaded 30-day volume baseline from {npy_path}")
                return

        # 2. Parse data/index.json if present
        if os.path.exists(self.baseline_file):
            try:
                with open(self.baseline_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    loaded_count = 0
                    for inst_key, rec in data.items():
                        if not isinstance(rec, dict):
                            continue
                        sym = rec.get("symbol")
                        idx = None
                        if sym and sym in self.symbol_to_index:
                            idx = self.symbol_to_index[sym]
                        elif inst_key in self.symbol_to_index:
                            idx = self.symbol_to_index[inst_key]
                        elif normalize_instrument_token(inst_key) in self.symbol_to_index:
                            idx = self.symbol_to_index[normalize_instrument_token(inst_key)]

                        if idx is not None and 0 <= idx < NUM_SYMBOLS:
                            slots = rec.get("time_slot_avg_volume", {})
                            if len(slots) >= 375:
                                vals = [float(v) for v in slots.values()]
                                self.volume_baseline[idx, :] = np.array(vals[:375], dtype=np.float64)
                                loaded_count += 1
                            elif len(slots) > 0:
                                vals = [float(v) for v in slots.values()]
                                tile = np.resize(np.array(vals, dtype=np.float64), 375)
                                self.volume_baseline[idx, :] = tile
                                loaded_count += 1
                            elif float(rec.get("avg_1m_volume", 0.0)) > 0.0:
                                self.volume_baseline[idx, :].fill(float(rec["avg_1m_volume"]))
                                loaded_count += 1
                print(f"[CANDLE_MANAGER] Initialized baseline profile from {self.baseline_file} ({loaded_count} symbols mapped)")
                return
            except Exception as e:
                print(f"[CANDLE_MANAGER] Warning reading {self.baseline_file}: {e}")

    def _get_bar_index(self, ts: float) -> Tuple[int, int, int]:
        """
        Calculate bar index (0..74), minute offset within bar (0..4), and second in minute (0..59).
        Aligned to 09:15 IST opening.
        """
        dt = datetime.fromtimestamp(ts)
        market_start_sec = (9 * 3600) + (15 * 60)
        curr_sec = (dt.hour * 3600) + (dt.minute * 60) + dt.second
        elapsed = curr_sec - market_start_sec

        if elapsed < 0:
            return 0, 0, 0
        bar_idx = min(NUM_BARS - 1, elapsed // 300)
        sec_in_bar = elapsed % 300
        min_offset = min(4, sec_in_bar // 60)
        sec_in_min = sec_in_bar % 60
        return int(bar_idx), int(min_offset), int(sec_in_min)

    def on_tick(self, token: str, ltp: float, vol: int, ts: float) -> None:
        """Process an underlying equity tick and update Shared Memory atomically."""
        sym_key = normalize_instrument_token(token)
        if sym_key not in self.symbol_to_index:
            # Fallback check symbol without prefix
            _, raw_sym = sym_key.split(":", 1) if ":" in sym_key else ("", sym_key)
            if raw_sym not in self.symbol_to_index:
                return
            idx = self.symbol_to_index[raw_sym]
        else:
            idx = self.symbol_to_index[sym_key]

        bar_idx, min_offset, sec_in_min = self._get_bar_index(ts)

        # Read current Seqlock sequence
        seq = int(self.headers[idx]["sequence_id"])

        # 1. Acquire Seqlock (odd sequence indicates write in progress)
        self.headers[idx]["sequence_id"] = seq + 1
        self.headers[idx]["last_update_ns"] = time.time_ns()

        # Compute delta volume since last received tick
        prev_v = self.prev_cum_vol[idx]
        if prev_v > 0 and vol >= prev_v:
            delta_vol = vol - prev_v
        else:
            delta_vol = 0

        if vol > 0:
            self.prev_cum_vol[idx] = vol

        # Bar transition check
        active_bar = self.candles[idx, bar_idx]
        is_new_bar = (bar_idx != self.current_bar_idx[idx])

        if is_new_bar:
            # Finalize previous bar ATR-14 and close status
            prev_bar_idx = self.current_bar_idx[idx]
            if prev_bar_idx >= 0:
                prev_b = self.candles[idx, prev_bar_idx]
                prev_b["is_closed"] = True
                if prev_b["close"] > 0:
                    high_val = prev_b["high"]
                    low_val = prev_b["low"]
                    c_prev = self.prev_close[idx] if self.prev_close[idx] > 0 else prev_b["open"]
                    tr = max(high_val - low_val, abs(high_val - c_prev), abs(low_val - c_prev))
                    if self.atr14[idx] == 0:
                        self.atr14[idx] = tr
                    else:
                        self.atr14[idx] = (self.atr14[idx] * 13.0 + tr) / 14.0
                    self.prev_close[idx] = prev_b["close"]
                    prev_b["atr"] = float(self.atr14[idx])

            # Reset new bar metrics
            self.current_bar_idx[idx] = bar_idx
            self.bar_start_vol[idx] = vol
            self.vol_at_min_edges[idx, :] = 0
            active_bar["timestamp"] = int(ts)
            active_bar["open"] = ltp
            active_bar["high"] = ltp
            active_bar["low"] = ltp
            active_bar["close"] = ltp
            active_bar["volume"] = delta_vol
            active_bar["is_closed"] = False
        else:
            active_bar["high"] = max(active_bar["high"], ltp)
            active_bar["low"] = min(active_bar["low"], ltp) if active_bar["low"] > 0 else ltp
            active_bar["close"] = ltp
            active_bar["volume"] += delta_vol

        bar_vol = int(active_bar["volume"])

        # Minute edge tracking & linear extrapolation
        if min_offset != self.current_min_idx[idx]:
            # Record volume accumulated at minute boundary
            prev_min = self.current_min_idx[idx]
            if 0 <= prev_min < 5:
                self.vol_at_min_edges[idx, prev_min] = bar_vol
            self.current_min_idx[idx] = min_offset

        # Strict current candle rVOL & ATR write directly to Shared Memory
        active_bar["rvol"] = self.calculate_rvol(idx, ts)
        active_bar["atr"] = float(self.atr14[idx]) if self.atr14[idx] > 0 else max(1.0, float(active_bar["high"] - active_bar["low"]))
        active_bar["is_closed"] = False

        # Release Seqlock (even sequence indicates write complete)
        self.headers[idx]["sequence_id"] = seq + 2

    def calculate_rvol(self, idx: int, ts: float) -> float:
        """
        Calculate strict current-candle rVOL with linear extrapolation.
        Called lock-free or by reader.
        """
        bar_idx, min_offset, sec_in_min = self._get_bar_index(ts)
        bar = self.candles[idx, bar_idx]
        bar_vol = bar["volume"]

        # Baseline volume for 5-minute candle
        m0 = bar_idx * 5
        m_end = min(375, m0 + 5)
        baseline_5m = float(np.sum(self.volume_baseline[idx, m0:m_end]))
        if baseline_5m <= 0:
            return 1.0

        # Exact minute edge evaluation
        if sec_in_min < 5:
            completed_mins = max(1, min_offset)
            expected_fraction = completed_mins / 5.0
            return float(bar_vol) / max(1.0, baseline_5m * expected_fraction)

        # Intra-minute linear extrapolation
        vol_at_prev_edge = self.vol_at_min_edges[idx, min_offset - 1] if min_offset > 0 else 0
        cur_min_vol = max(0, bar_vol - vol_at_prev_edge)
        extrap_min_vol = cur_min_vol * (60.0 / max(5.0, float(sec_in_min)))
        total_extrap_vol = vol_at_prev_edge + extrap_min_vol

        expected_fraction = (min_offset + 1) / 5.0
        expected_vol = baseline_5m * expected_fraction
        return float(total_extrap_vol) / max(1.0, expected_vol)

    def start(self) -> None:
        """Run the main event loop listening to ZeroMQ ticks."""
        self.running = True
        print(f"[CANDLE_MANAGER] Active | Listening on {self.sub_ipc} | SHM: {self.shm_name}")
        while self.running:
            try:
                topic, payload = self.sub.recv_multipart()
                tick = unpack_message(payload)
                self.on_tick(
                    token=tick.get("token", ""),
                    ltp=float(tick.get("ltp", 0.0)),
                    vol=int(tick.get("volume", 0)),
                    ts=float(tick.get("received_timestamp", time.time())),
                )
            except zmq.ZMQError:
                break
            except Exception as e:
                print(f"[CANDLE_MANAGER] Error processing tick: {e}", file=sys.stderr)

    def stop(self) -> None:
        self.running = False
        self.sub.close()
        self.ctx.term()
        self.shm.close()
        print("[CANDLE_MANAGER] Stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="CandleManager Single Writer & SHM Owner")
    parser.add_argument("--ipc", default=DEFAULT_FEED_SUB_IPC, help="ZeroMQ feed address")
    parser.add_argument("--shm", default=SHM_NAME, help="Shared Memory name")
    args = parser.parse_args()

    manager = CandleManager(sub_ipc=args.ipc, shm_name=args.shm)

    def sig_handler(sig, frame):
        manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    manager.start()


if __name__ == "__main__":
    main()
