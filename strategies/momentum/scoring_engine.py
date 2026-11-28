"""
Alpha Scoring Engine 3.0: High-Performance Shared Memory Price Action Engine.
Evaluates strict physical invariants (50% Retracement, Pure Directional Thrust, CLV >= 0.75, rVOL >= 2.0x).
Eliminates continuous 0-100 linear score, regime biases, and artificial dud inflation.
Dispatches actionable opportunities sorted by rVOL descending.
"""
import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

from core.candles.shm_layout import (
    CANDLE_DTYPE,
    SEQLOCK_DTYPE,
    NUM_SYMBOLS,
    NUM_BARS,
    SHM_NAME,
    create_or_attach_shm,
    get_shm_views,
)
from core.common.ipc_schemas import SetupAlert
from core.common.logging_utils import get_logger
from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST
from strategies.momentum.opportunity_pool import OpportunityPool

logger = get_logger("SCORING_ENGINE_3_0")


@dataclass
class SymbolMetadata:
    last_evaluated_ts: float = 0.0
    status: Optional[str] = None
    status_bar_idx: int = -1
    breakout_bars: List[int] = field(default_factory=list)
    pole_bars: List[int] = field(default_factory=list)

    def has_breakout_in_last_n_bars(self, n: int, current_bar_idx: int) -> bool:
        for b in reversed(self.breakout_bars):
            if current_bar_idx - b < n:
                return True
            else:
                break
        return False

    def has_pole_in_last_n_bars(self, n: int, current_bar_idx: int) -> bool:
        for b in reversed(self.pole_bars):
            if current_bar_idx - b < n:
                return True
            else:
                break
        return False

    def record_pole(self, bar_idx: int) -> None:
        self.pole_bars.append(bar_idx)
        if len(self.pole_bars) > 20:
            self.pole_bars = self.pole_bars[-20:]

    def record_breakout(self, bar_idx: int) -> None:
        self.breakout_bars.append(bar_idx)
        if len(self.breakout_bars) > 20:
            self.breakout_bars = self.breakout_bars[-20:]


class ScoringEngine3:
    def __init__(
        self,
        shm_name: Optional[str] = None,
        pool: Optional[OpportunityPool] = None,
        universe_csv: Optional[str] = None,
        baseline_file: Optional[str] = None,
        min_pole_pct: Optional[float] = None,
        enable_zmq: bool = True,
        persist_alerts: Optional[bool] = None,
        min_candle_elapsed_sec: Optional[float] = None,
        flag_pole_buffer_pct: Optional[float] = None,
        pure_3bar_expansion_pct: Optional[float] = None,
    ):
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        sys_cfg = cfg.system.get("system", {})
        shm_cfg = sys_cfg.get("shm", {})
        inst_paths = cfg.instruments.get("instruments", {}).get("paths", {})
        scoring_cfg = cfg.get_scoring_config()

        self.enable_zmq = enable_zmq
        self.persist_alerts = persist_alerts
        self.shm_name = shm_name or shm_cfg.get("shm_name", SHM_NAME)
        self.min_volume_thrust_mult = float(scoring_cfg.get("min_volume_thrust_mult", 2.0))
        self.min_clv = float(scoring_cfg.get("min_clv", 0.75))
        self.invalidation_retrace_pct = float(scoring_cfg.get("invalidation_retrace_pct", 0.50))
        if min_pole_pct is not None:
            pole_val = min_pole_pct
        else:
            fm_pole = cfg.strategy.get("strategies", {}).get("momentum", {}).get("tracks", {}).get("fast_momentum", {}).get("min_pole_pct")
            sc_pole = scoring_cfg.get("min_pole_pct")
            strat_pole = cfg.strategy.get("strategies", {}).get("momentum", {}).get("min_pole_pct")
            pole_val = fm_pole if (fm_pole is not None and fm_pole != 0.005) else (sc_pole or strat_pole or fm_pole or 0.005)

        self.min_pole_pct = float(pole_val) if pole_val is not None else 0.005
        if self.min_pole_pct > 0.5:
            self.min_pole_pct /= 100.0

        self.max_consolidation_candles = int(scoring_cfg.get("max_consolidation_candles", 5))
        self.quarantine_cooldown_sec = float(scoring_cfg.get("quarantine_cooldown_sec", 5.0))
        self.min_candle_elapsed_sec = float(
            scoring_cfg.get("min_candle_elapsed_sec", 15.0)
            if min_candle_elapsed_sec is None
            else min_candle_elapsed_sec
        )
        self.flag_pole_buffer_pct = float(
            scoring_cfg.get("flag_pole_buffer_pct", 0.25)
            if flag_pole_buffer_pct is None
            else flag_pole_buffer_pct
        )
        self.pure_3bar_expansion_pct = float(
            scoring_cfg.get("pure_3bar_expansion_pct", 0.0045)
            if pure_3bar_expansion_pct is None
            else pure_3bar_expansion_pct
        )

        self._owns_pool = pool is None
        self.pool = pool or OpportunityPool(
            cooldown_seconds=self.quarantine_cooldown_sec,
            enable_zmq=enable_zmq,
            persist_alerts=persist_alerts,
        )
        self.universe_csv = universe_csv or inst_paths.get("filtered_universe_csv", "data/filtered.csv")
        self.running = False

        # Load universe mapping
        self.symbol_to_index: Dict[str, int] = {}
        self.index_to_symbol: List[str] = []
        self._load_universe()

        # Attach to Shared Memory (Read-Only)
        self.shm = create_or_attach_shm(name=self.shm_name, create=False)
        self.headers, self.candles = get_shm_views(self.shm)

        # Per-symbol state metadata
        self.meta: Dict[str, SymbolMetadata] = {
            sym: SymbolMetadata() for sym in self.index_to_symbol
        }

    def _load_universe(self) -> None:
        symbols = []
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
                        symbols.append(sym.strip().upper())
        else:
            symbols = [f"STOCK_{i}" for i in range(NUM_SYMBOLS)]

        symbols = symbols[:NUM_SYMBOLS]
        while len(symbols) < NUM_SYMBOLS:
            symbols.append(f"RESERVED_{len(symbols)}")

        self.index_to_symbol = symbols
        self.symbol_to_index = {sym: idx for idx, sym in enumerate(symbols)}

    def _get_bar_index(self, ts: float) -> Tuple[int, int, int]:
        dt = datetime.fromtimestamp(ts, tz=IST)
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

    def _read_candle_seqlock(self, idx: int, bar_idx: int, max_retries: int = 10) -> np.void:
        """Lock-free atomic seqlock read of active candle."""
        for _ in range(max_retries):
            seq1 = int(self.headers[idx]["sequence_id"])
            if seq1 & 1:
                continue
            candle = self.candles[idx, bar_idx].copy()
            seq2 = int(self.headers[idx]["sequence_id"])
            if seq1 == seq2:
                return candle
        return self.candles[idx, bar_idx].copy()

    def _has_recent_pole(self, sym_idx: int, bar_idx: int, n: int = 5) -> bool:
        """
        Check if any candidate pole was formed within the last n bars.
        Used to prevent entering pure breakouts during contraction.
        """
        start_bar = max(0, bar_idx - n)
        raw_bars = self.candles[sym_idx, start_bar:bar_idx]
        bars = [b for b in raw_bars if b["open"] > 0]
        n_bars = len(bars)
        if n_bars < 1:
            return False

        for length in (3, 2, 1):
            for i in range(n_bars - length + 1):
                slice_b = bars[i : i + length]
                all_green = all(float(b["close"]) >= float(b["open"]) for b in slice_b)
                all_red = all(float(b["close"]) <= float(b["open"]) for b in slice_b)
                if not (all_green or all_red):
                    continue

                low = float(min(b["low"] for b in slice_b))
                high = float(max(b["high"] for b in slice_b))
                height = high - low
                if low <= 0.0 or height <= 0.01:
                    continue
                if (height / low) < self.min_pole_pct:
                    continue

                # Pole must have institutional volume thrust (rvol >= min_volume_thrust_mult)
                has_thrust = any(float(b["rvol"]) >= self.min_volume_thrust_mult for b in slice_b)
                if has_thrust:
                    return True

        return False

    def _check_flag_breakout(
        self, symbol: str, idx: int, bar_idx: int, c: np.void, rvol: float, atr: float, now_ts: float
    ) -> Optional[SetupAlert]:
        """
        Archetype 1: Bull / Bear Flag Breakout (Contraction Breakout).
        Searches dynamically across the last up to 7 candles to detect the most recent pole (1 to 3 candles max),
        refines pole boundaries:
        - Expands backwards on earlier side if new candle adds >= 33% pole length without significant volume loss.
        - Trims latest candle if removing it still leaves a valid pole and increases average pole volume per candle.
        Verifies average pole volume >= 2.0x consolidation volume, consolidation >= 2 candles, and 50% Retracement Check
        evaluated FIRST before OLS regression target.
        """
        if bar_idx < 3:
            return None

        # Lookback up to 7 closed candles (filtering for populated historical bars)
        lookback = min(7, bar_idx)
        start_idx = bar_idx - lookback
        raw_hist = self.candles[idx, start_idx : bar_idx]
        hist_tuples = [(start_idx + offset, b) for offset, b in enumerate(raw_hist) if b["open"] > 0]
        n_hist = len(hist_tuples)
        if n_hist < 3:
            return None
        hist_candles = [t[1] for t in hist_tuples]
        hist_bar_indices = [t[0] for t in hist_tuples]

        evaluated_pairs = set()

        # Search for candidate pole start index (p_start) backwards to identify the most recent pole
        # Consolidation requires at least 2 candles: p_end = p_start + pole_len <= n_hist - 2
        for p_start in range(n_hist - 3, -1, -1):
            for pole_len in (3, 2, 1):
                p_end = p_start + pole_len
                if p_end > n_hist - 2:
                    continue

                init_pole = hist_candles[p_start:p_end]
                is_bull_cand = all(float(b["close"]) >= float(b["open"]) for b in init_pole)
                is_bear_cand = all(float(b["close"]) <= float(b["open"]) for b in init_pole)
                if not (is_bull_cand or is_bear_cand):
                    continue

                init_low = float(min(b["low"] for b in init_pole))
                init_high = float(max(b["high"] for b in init_pole))
                init_height = init_high - init_low
                if init_height <= 0.01 or init_low <= 0.0:
                    continue
                if (init_height / init_low) < self.min_pole_pct:
                    continue

                cur_direction = "BULL" if is_bull_cand else "BEAR"
                cur_p_start = p_start
                cur_p_end = p_end

                # Refinement 1: Try extending pole backwards on earlier side (cur_p_start - 1).
                # Number of pole candles capped at 3.
                while (cur_p_end - cur_p_start) < 3 and cur_p_start > 0:
                    prev_bar = hist_candles[cur_p_start - 1]
                    if cur_direction == "BULL" and not (float(prev_bar["close"]) >= float(prev_bar["open"])):
                        break
                    if cur_direction == "BEAR" and not (float(prev_bar["close"]) <= float(prev_bar["open"])):
                        break

                    cur_pole = hist_candles[cur_p_start:cur_p_end]
                    cur_low = float(min(b["low"] for b in cur_pole))
                    cur_high = float(max(b["high"] for b in cur_pole))
                    cur_height = cur_high - cur_low
                    cur_avg_vol = float(np.mean([float(b["volume"]) for b in cur_pole]))

                    test_pole = hist_candles[cur_p_start - 1 : cur_p_end]
                    test_low = float(min(b["low"] for b in test_pole))
                    test_high = float(max(b["high"] for b in test_pole))
                    test_height = test_high - test_low
                    test_avg_vol = float(np.mean([float(b["volume"]) for b in test_pole]))

                    # Does including it increase the pole length significantly? At least 33%?
                    height_increase_pct = (test_height - cur_height) / max(0.01, cur_height)
                    if height_increase_pct < 0.33:
                        break

                    # Does including it increase average volume or not decrease it significantly?
                    if test_avg_vol < 0.90 * cur_avg_vol:
                        break

                    cur_p_start -= 1

                # Refinement 2: Check if latest pole candle is actually a contraction candle.
                # If removing it increases average volume per candle and still leaves a valid pole.
                while (cur_p_end - cur_p_start) > 1:
                    cur_pole = hist_candles[cur_p_start:cur_p_end]
                    cur_avg_vol = float(np.mean([float(b["volume"]) for b in cur_pole]))

                    trimmed_pole = hist_candles[cur_p_start : cur_p_end - 1]
                    trimmed_low = float(min(b["low"] for b in trimmed_pole))
                    trimmed_high = float(max(b["high"] for b in trimmed_pole))
                    trimmed_height = trimmed_high - trimmed_low
                    if trimmed_low <= 0.0 or trimmed_height <= 0.01:
                        break
                    trimmed_pct = trimmed_height / trimmed_low

                    # Still a valid pole?
                    if trimmed_pct < self.min_pole_pct:
                        break

                    trimmed_avg_vol = float(np.mean([float(b["volume"]) for b in trimmed_pole]))
                    # Does removing that candle volume increase our average volume per candle in the pole?
                    if trimmed_avg_vol > cur_avg_vol:
                        cur_p_end -= 1
                    else:
                        break

                if (cur_p_start, cur_p_end) in evaluated_pairs:
                    continue
                evaluated_pairs.add((cur_p_start, cur_p_end))

                pole = hist_candles[cur_p_start:cur_p_end]
                contraction = hist_candles[cur_p_end:]
                if len(contraction) < 2 or len(contraction) > self.max_consolidation_candles:
                    continue

                pole_vol = float(np.mean([float(b["volume"]) for b in pole]))
                contr_vol = float(np.mean([float(b["volume"]) for b in contraction]))

                # Invariant: Average volume in pole must be at least twice that of contraction
                if pole_vol < 2.0 * contr_vol:
                    continue

                # Record that a valid pole is detected for this symbol (recorded at the actual pole end bar)
                pole_end_bar = hist_bar_indices[cur_p_end - 1]
                self.meta[symbol].record_pole(pole_end_bar)

                pole_low = float(min(b["low"] for b in pole))
                pole_high = float(max(b["high"] for b in pole))
                pole_height = pole_high - pole_low
                if pole_height <= 0.01 or pole_low <= 0.0:
                    continue

                # --- Bull Flag ---
                if cur_direction == "BULL":
                    contr_low = float(min(b["low"] for b in contraction))
                    retracement = (pole_high - contr_low) / pole_height

                    # Anti-Reversal Invariant: 50% Retracement evaluated FIRST before OLS
                    if retracement <= self.invalidation_retrace_pct and contr_low >= pole_low:
                        xs = np.arange(len(contraction), dtype=np.float64)
                        ys = np.array([float(b["high"]) for b in contraction], dtype=np.float64)
                        m, c_val = np.polyfit(xs, ys, 1)
                        proj_trigger = float(m * len(contraction) + c_val)
                        req_trig = (
                            max(proj_trigger, pole_high - self.flag_pole_buffer_pct * pole_height)
                            if self.flag_pole_buffer_pct > 0.0
                            else proj_trigger
                        )

                        avg_contr_rov = float(np.mean([float(b["volume"]) / 300.0 for b in contraction]))
                        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
                        curr_rov = float(c["volume"]) / curr_elapsed
                        clv = float(c["close"] - c["low"]) / max(0.01, float(c["high"] - c["low"]))

                        if (
                            float(c["close"]) >= req_trig
                            and curr_rov >= self.min_volume_thrust_mult * avg_contr_rov
                            and clv >= self.min_clv
                        ):
                            return SetupAlert(
                                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                                symbol=symbol,
                                direction="BULL",
                                archetype="FLAG_BULL_BREAKOUT",
                                trigger_price=float(c["close"]),
                                rvol=rvol,
                                clv=clv,
                                atr=atr,
                                flag_high=pole_high,
                                flag_low=contr_low,
                                timestamp=now_ts,
                                pole_high=pole_high,
                                pole_low=pole_low,
                                strategy_id="momentum",
                            )

                # --- Bear Flag ---
                if cur_direction == "BEAR":
                    contr_high = float(max(b["high"] for b in contraction))
                    bear_retracement = (contr_high - pole_low) / pole_height

                    # Anti-Reversal Invariant: 50% Retracement evaluated FIRST before OLS
                    if bear_retracement <= self.invalidation_retrace_pct and contr_high <= pole_high:
                        xs = np.arange(len(contraction), dtype=np.float64)
                        ys = np.array([float(b["low"]) for b in contraction], dtype=np.float64)
                        m, c_val = np.polyfit(xs, ys, 1)
                        proj_trigger = float(m * len(contraction) + c_val)
                        req_trig = (
                            min(proj_trigger, pole_low + self.flag_pole_buffer_pct * pole_height)
                            if self.flag_pole_buffer_pct > 0.0
                            else proj_trigger
                        )

                        avg_contr_rov = float(np.mean([float(b["volume"]) / 300.0 for b in contraction]))
                        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
                        curr_rov = float(c["volume"]) / curr_elapsed
                        clv_bear = float(c["high"] - c["close"]) / max(0.01, float(c["high"] - c["low"]))

                        if (
                            float(c["close"]) <= req_trig
                            and curr_rov >= self.min_volume_thrust_mult * avg_contr_rov
                            and clv_bear >= self.min_clv
                        ):
                            return SetupAlert(
                                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                                symbol=symbol,
                                direction="BEAR",
                                archetype="FLAG_BEAR_BREAKDOWN",
                                trigger_price=float(c["close"]),
                                rvol=rvol,
                                clv=clv_bear,
                                atr=atr,
                                flag_high=contr_high,
                                flag_low=pole_low,
                                timestamp=now_ts,
                                pole_high=pole_high,
                                pole_low=pole_low,
                                strategy_id="momentum",
                            )

        return None

    def _check_pure_breakout(
        self, symbol: str, idx: int, bar_idx: int, c: np.void, rvol: float, atr: float, now_ts: float
    ) -> Optional[SetupAlert]:
        """
        Archetype 2: Pure 3-Candle Impulse Breakout / Breakdown.
        Enforces 2.0x Baseline Thrust, structural multi-bar break, and CLV >= 0.75.
        Guarded against taking breakouts within 5 candles of a pole to prevent contraction traps.
        """
        if bar_idx < 3:
            return None

        # Guard: If a pole was detected within the last 5 candles, do not check for breakout lest we get caught in a contraction
        if self._has_recent_pole(idx, bar_idx, 5) or self.meta[symbol].has_pole_in_last_n_bars(5, bar_idx):
            return None

        prev = self.candles[idx, (bar_idx - 3) : bar_idx]
        if len(prev) < 3 or any(b["open"] <= 0 for b in prev):
            return None

        # Bull Impulse
        combined_high = float(max(b["high"] for b in prev))
        combined_low = float(min(b["low"] for b in prev))
        avg_thrust = float(sum(abs(float(b["close"]) - float(b["open"])) for b in prev)) / 3.0
        baseline_thrust = max(avg_thrust, 0.35 * atr)
        curr_thrust = float(c["close"] - c["open"])

        avg_rov = float(sum(b["volume"] / 300.0 for b in prev)) / 3.0
        curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))
        curr_rov = float(c["volume"]) / curr_elapsed
        clv = float(c["close"] - c["low"]) / max(0.01, float(c["high"] - c["low"]))
        bull_exp_ok = self.pure_3bar_expansion_pct <= 0.0 or (
            combined_low > 0.0 and ((float(c["close"]) - combined_low) / combined_low) >= self.pure_3bar_expansion_pct
        )

        if (
            float(c["close"]) > combined_high
            and bull_exp_ok
            and curr_thrust >= self.min_volume_thrust_mult * baseline_thrust
            and curr_rov >= self.min_volume_thrust_mult * avg_rov
            and clv >= self.min_clv
        ):
            return SetupAlert(
                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                symbol=symbol,
                direction="BULL",
                archetype="PURE_BULL_BREAKOUT",
                trigger_price=float(c["close"]),
                rvol=rvol,
                clv=clv,
                atr=atr,
                flag_high=combined_high,
                flag_low=combined_low,
                timestamp=now_ts,
                pole_high=combined_high,
                pole_low=combined_low,
                strategy_id="momentum",
            )

        # Bear Impulse
        avg_bear_thrust = avg_thrust
        baseline_bear_thrust = baseline_thrust
        curr_bear_thrust = float(c["open"] - c["close"])
        clv_bear = float(c["high"] - c["close"]) / max(0.01, float(c["high"] - c["low"]))
        bear_exp_ok = self.pure_3bar_expansion_pct <= 0.0 or (
            combined_high > 0.0 and ((combined_high - float(c["close"])) / combined_high) >= self.pure_3bar_expansion_pct
        )

        if (
            float(c["close"]) < combined_low
            and bear_exp_ok
            and curr_bear_thrust >= self.min_volume_thrust_mult * baseline_bear_thrust
            and curr_rov >= self.min_volume_thrust_mult * avg_rov
            and clv_bear >= self.min_clv
        ):
            return SetupAlert(
                alert_id=f"ALT_{symbol}_{int(now_ts*1000)}",
                symbol=symbol,
                direction="BEAR",
                archetype="PURE_BEAR_BREAKDOWN",
                trigger_price=float(c["close"]),
                rvol=rvol,
                clv=clv_bear,
                atr=atr,
                flag_high=combined_high,
                flag_low=combined_low,
                timestamp=now_ts,
                pole_high=combined_high,
                pole_low=combined_low,
                strategy_id="momentum",
            )

        return None

    def evaluate_universe(
        self, now_ts: Optional[float] = None, symbol_indices: Optional[Any] = None
    ) -> List[SetupAlert]:
        """Single spin-loop pass across monitored F&O equities (< 500 microseconds)."""
        now_ts = now_ts or time.time()
        bar_idx, _, _ = self._get_bar_index(now_ts)
        actionable: List[SetupAlert] = []
        indices = symbol_indices if symbol_indices is not None else range(len(self.index_to_symbol))

        for idx in indices:
            symbol = self.index_to_symbol[idx]
            m = self.meta[symbol]

            # Fast Guard: Already triggered on this bar before reading SHM
            if m.status is not None and m.status_bar_idx == bar_idx:
                continue

            candle = self._read_candle_seqlock(idx, bar_idx) if self.enable_zmq else self.candles[idx, bar_idx]

            # Gate 1: No activity on this bar yet
            c_ts = float(candle["timestamp"])
            if c_ts == 0.0 or c_ts == m.last_evaluated_ts:
                continue

            # Gate 2b: Min elapsed seconds inside active candle before firing setup alerts
            if self.min_candle_elapsed_sec > 0.0 and (now_ts - c_ts) < self.min_candle_elapsed_sec:
                continue

            # Strict current-candle rVOL and ATR-14 from Shared Memory (computed by CandleManager)
            rvol = float(candle["rvol"])

            # Gate 3: Institutional rVOL Invariant (rVOL >= min_volume_thrust_mult)
            if rvol < self.min_volume_thrust_mult:
                continue

            raw_atr = float(candle["atr"])
            atr = raw_atr if raw_atr > 0.0 else max(1.0, float(candle["high"] - candle["low"]))

            # Archetype 1: Bull / Bear Flag Breakout
            flag_alert = self._check_flag_breakout(symbol, idx, bar_idx, candle, rvol, atr, now_ts)
            if flag_alert:
                actionable.append(flag_alert)
                m.status = flag_alert.archetype
                m.status_bar_idx = bar_idx
                m.last_evaluated_ts = c_ts
                m.record_breakout(bar_idx)
                continue

            # Archetype 2: Pure 3-Candle Impulse Breakout
            # Guard against entering pure breakouts if recent breakout occurred or within 5 bars of a pole
            if (
                m.has_breakout_in_last_n_bars(3, bar_idx)
                or m.has_pole_in_last_n_bars(5, bar_idx)
                or self._has_recent_pole(idx, bar_idx, 5)
            ):
                m.last_evaluated_ts = c_ts
                continue

            pure_alert = self._check_pure_breakout(symbol, idx, bar_idx, candle, rvol, atr, now_ts)
            if pure_alert:
                actionable.append(pure_alert)
                m.status = pure_alert.archetype
                m.status_bar_idx = bar_idx
                m.last_evaluated_ts = c_ts
                m.record_breakout(bar_idx)

        # Priority Dispatch: Sort actionable candidates by rVOL descending
        if actionable:
            actionable.sort(key=lambda a: a.rvol, reverse=True)
            if self.pool:
                for alert in actionable:
                    self.pool.submit_alert(alert)

        return actionable

    def run_spin_loop(self) -> None:
        """Continuous microsecond spin-loop with 5ms sleep yield."""
        self.running = True
        logger.info(f"Active | SHM: {self.shm_name} | 221 Universe symbols")

        while self.running:
            try:
                self.evaluate_universe()
                time.sleep(0.005)  # 5ms yield to prevent 100% single-thread core burnout
            except Exception as e:
                logger.error(f"Loop error: {e}")

    def stop(self) -> None:
        self.running = False
        try:
            self.headers = None
            self.candles = None
            if hasattr(self, "shm") and self.shm is not None:
                self.shm.close()
                self.shm = None
            if self._owns_pool and self.pool:
                self.pool.stop()
        except Exception:
            pass
        logger.info("Stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Alpha Scoring Engine 3.0")
    parser.add_argument("--shm-name", default=SHM_NAME, help="Shared Memory segment name")
    parser.add_argument("--alerts-ipc", default="ipc:///tmp/scoring_alerts_pub.ipc", help="PUB endpoint")
    args = parser.parse_args()

    pool = OpportunityPool(pub_ipc=args.alerts_ipc)
    engine = ScoringEngine3(shm_name=args.shm_name, pool=pool)

    def sig_handler(sig, frame):
        engine.stop()
        pool.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    engine.run_spin_loop()


if __name__ == "__main__":
    main()
