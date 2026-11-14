#!/usr/bin/env python3
"""
infrastructure/pnl/pnl_aggregator.py
====================================
Unified Real-Time P&L Trade Journal & Aggregator Service:
1. Records all trade events (OPEN, UPDATE, EXIT) from every strategy,
   sub-strategy track, and execution mode (paper, paper_delayed, live).
2. Persists structured trade entries to:
   - recordings/{session_date}/trades.jsonl
   - logs/trade_journal_{session_date}.jsonl
3. Aggregates intraday P&L time series per strategy, track, and mode.
4. Provides analytical reporting data for end-of-day P&L visualization.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class TradeJournalEntry:
    trade_id: str
    session_date: str
    event: str  # OPEN, UPDATE, EXIT
    timestamp: float
    datetime_str: str
    strategy: str
    track: str
    mode: str
    symbol: str
    contract_key: str
    contract_symbol: str
    direction: str  # BULL or BEAR
    quantity: int
    entry_time: float
    entry_datetime: str
    entry_price: float
    exit_time: float = 0.0
    exit_datetime: str = ""
    exit_price: float = 0.0
    gross_pnl: float = 0.0
    brokerage: float = 60.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    exit_reason: str = ""
    status: str = "OPEN"  # OPEN, CLOSED
    peak_price: float = 0.0
    current_sl: float = 0.0
    underlying_entry: float = 0.0
    underlying_sl: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_test_environment() -> bool:
    """Check if code is running inside a test suite / unit test."""
    if os.environ.get("TRADING_TEST_MODE") == "1":
        return True
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return True
    return False


class PnLAggregator:
    """
    Centralized Trade Journal and Real-time P&L Aggregator.
    Individual strategies and execution managers publish their trades here.
    """

    def __init__(
        self,
        session_date: Optional[str] = None,
        record_dir: str = "recordings",
        log_dir: str = "logs",
        log_open_events: bool = False,
        auto_enumerate_on_exit: bool = True,
    ):
        if (record_dir == "recordings" or log_dir == "logs") and is_test_environment():
            # In test mode, isolate test trade journals into a temporary directory that auto-cleans on exit
            import tempfile, shutil
            self._test_temp_dir = tempfile.mkdtemp(prefix="test_pnl_")
            record_dir = os.path.join(self._test_temp_dir, "recordings")
            log_dir = os.path.join(self._test_temp_dir, "logs")
            auto_enumerate_on_exit = False
            try:
                import atexit
                atexit.register(lambda: shutil.rmtree(self._test_temp_dir, ignore_errors=True))
            except Exception:
                pass

        self.session_date = session_date or datetime.now().strftime("%Y%m%d")
        self.record_dir = record_dir
        self.log_dir = log_dir
        self.log_open_events = log_open_events
        self.auto_enumerate_on_exit = auto_enumerate_on_exit

        self.recording_file = os.path.join(record_dir, self.session_date, "trades.jsonl")
        self.log_file = os.path.join(log_dir, f"trade_journal_{self.session_date}.jsonl")

        self._lock = threading.Lock()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

        # In-memory journal
        self._trades_by_id: Dict[str, Dict[str, Any]] = {}
        self._all_events: List[Dict[str, Any]] = []

        # Time series: list of (timestamp, net_pnl_delta, mode, strategy, track)
        self._pnl_events: List[Tuple[float, float, str, str, str]] = []

        self._ensure_output_dirs()

        if self.auto_enumerate_on_exit:
            try:
                import atexit
                atexit.register(self.enumerate_logs)
            except Exception:
                pass

    def enumerate_logs(self) -> None:
        """
        Enumerate and archive active trade logs on strategy shutdown.
        If trades.jsonl or trade_journal_{date}.jsonl has content,
        archives it sequentially as trades_{k}.jsonl and trade_journal_{date}_{k}.jsonl.
        """
        with self._lock:
            rec_has_data = os.path.exists(self.recording_file) and os.path.getsize(self.recording_file) > 0
            log_has_data = os.path.exists(self.log_file) and os.path.getsize(self.log_file) > 0

            if not rec_has_data and not log_has_data:
                return

            k = 1
            rec_parent = os.path.dirname(os.path.abspath(self.recording_file))
            log_parent = os.path.dirname(os.path.abspath(self.log_file))

            while True:
                candidate_rec = os.path.join(rec_parent, f"trades_{k}.jsonl")
                candidate_log = os.path.join(log_parent, f"trade_journal_{self.session_date}_{k}.jsonl")
                if not os.path.exists(candidate_rec) and not os.path.exists(candidate_log):
                    break
                k += 1

            target_rec = os.path.join(rec_parent, f"trades_{k}.jsonl")
            target_log = os.path.join(log_parent, f"trade_journal_{self.session_date}_{k}.jsonl")

            if rec_has_data:
                try:
                    os.rename(self.recording_file, target_rec)
                except Exception:
                    pass

            if log_has_data:
                try:
                    os.rename(self.log_file, target_log)
                except Exception:
                    pass

    def _ensure_output_dirs(self) -> None:
        """Create target directories for jsonl recording."""
        os.makedirs(os.path.dirname(os.path.abspath(self.recording_file)), exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(self.log_file)), exist_ok=True)

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Subscribe to real-time trade journal events."""
        with self._lock:
            self._listeners.append(callback)

    def record_trade_open(
        self,
        trade_id: str,
        strategy: str,
        track: str,
        mode: str,
        symbol: str,
        contract_key: str,
        contract_symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        entry_time: Optional[float] = None,
        peak_price: Optional[float] = None,
        current_sl: float = 0.0,
        underlying_entry: float = 0.0,
        underlying_sl: float = 0.0,
        brokerage: float = 30.0,
    ) -> Dict[str, Any]:
        """Record trade opening event."""
        now_ts = entry_time or time.time()
        dt_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")

        entry = TradeJournalEntry(
            trade_id=trade_id,
            session_date=self.session_date,
            event="OPEN",
            timestamp=now_ts,
            datetime_str=dt_str,
            strategy=strategy,
            track=track,
            mode=mode.lower(),
            symbol=symbol,
            contract_key=contract_key,
            contract_symbol=contract_symbol,
            direction=direction,
            quantity=quantity,
            entry_time=now_ts,
            entry_datetime=dt_str,
            entry_price=entry_price,
            peak_price=peak_price or entry_price,
            current_sl=current_sl,
            underlying_entry=underlying_entry,
            underlying_sl=underlying_sl,
            brokerage=brokerage,
            status="OPEN",
        )

        if self.auto_enumerate_on_exit and not is_test_environment():
            try:
                from infrastructure.market_data.position_registry import ProtectedPositionRegistry
                ProtectedPositionRegistry.get_instance().register_open_position(
                    contract_key=contract_key,
                    symbol=symbol,
                    contract_symbol=contract_symbol,
                    mode=mode,
                    track=track,
                    trade_id=trade_id,
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_time=now_ts,
                )
            except Exception:
                pass

        return self._append_entry(entry.to_dict(), write_to_disk=self.log_open_events)

    def record_trade_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_time: Optional[float] = None,
        exit_reason: str = "",
        gross_pnl: Optional[float] = None,
        net_pnl: Optional[float] = None,
        brokerage: float = 60.0,
        strategy: Optional[str] = None,
        track: Optional[str] = None,
        mode: Optional[str] = None,
        symbol: Optional[str] = None,
        contract_key: Optional[str] = None,
        contract_symbol: Optional[str] = None,
        direction: Optional[str] = None,
        quantity: Optional[int] = None,
        entry_price: Optional[float] = None,
        entry_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Record trade closing event."""
        now_ts = exit_time or time.time()
        dt_str = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")

        with self._lock:
            existing = self._trades_by_id.get(trade_id, {})

        s = strategy or existing.get("strategy", "momentum")
        tr = track or existing.get("track", "ATM")
        m = (mode or existing.get("mode", "paper")).lower()
        sym = symbol or existing.get("symbol", "")
        ck = contract_key or existing.get("contract_key", "")
        cs = contract_symbol or existing.get("contract_symbol", "")
        d = direction or existing.get("direction", "BULL")
        qty = quantity if quantity is not None else existing.get("quantity", 1)
        ep = entry_price if entry_price is not None else existing.get("entry_price", 0.0)
        et = entry_time if entry_time is not None else existing.get("entry_time", now_ts)
        et_dt = datetime.fromtimestamp(et).strftime("%Y-%m-%d %H:%M:%S")

        if gross_pnl is None:
            gross_pnl = (exit_price - ep) * qty if ep > 0 else 0.0
        if net_pnl is None:
            net_pnl = gross_pnl - brokerage

        ret_pct = ((exit_price - ep) / ep) * 100.0 if ep > 0 else 0.0

        entry = TradeJournalEntry(
            trade_id=trade_id,
            session_date=self.session_date,
            event="EXIT",
            timestamp=now_ts,
            datetime_str=dt_str,
            strategy=s,
            track=tr,
            mode=m,
            symbol=sym,
            contract_key=ck,
            contract_symbol=cs,
            direction=d,
            quantity=qty,
            entry_time=et,
            entry_datetime=et_dt,
            entry_price=ep,
            exit_time=now_ts,
            exit_datetime=dt_str,
            exit_price=exit_price,
            gross_pnl=round(gross_pnl, 2),
            brokerage=round(brokerage, 2),
            net_pnl=round(net_pnl, 2),
            return_pct=round(ret_pct, 2),
            exit_reason=exit_reason,
            status="CLOSED",
        )

        if self.auto_enumerate_on_exit and not is_test_environment():
            try:
                from infrastructure.market_data.position_registry import ProtectedPositionRegistry
                ProtectedPositionRegistry.get_instance().register_closed_position(
                    contract_key=ck,
                    mode=m,
                    track=tr,
                    trade_id=trade_id,
                )
            except Exception:
                pass

        return self._append_entry(entry.to_dict(), write_to_disk=True)

    def record_trade(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Generic trade recorder."""
        data_copy = dict(data)
        data_copy["event"] = event_type.upper()
        if "timestamp" not in data_copy:
            data_copy["timestamp"] = time.time()
        if "datetime_str" not in data_copy:
            data_copy["datetime_str"] = datetime.fromtimestamp(data_copy["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        if "session_date" not in data_copy:
            data_copy["session_date"] = self.session_date
        is_open = (data_copy["event"] == "OPEN")
        return self._append_entry(data_copy, write_to_disk=(not is_open or self.log_open_events))

    def _append_entry(self, record: Dict[str, Any], write_to_disk: bool = True) -> Dict[str, Any]:
        """Append record to thread-safe memory and write immediately to files if write_to_disk=True."""
        tid = record.get("trade_id", "")
        with self._lock:
            self._all_events.append(record)
            if tid:
                # Merge into master trade state
                master = self._trades_by_id.setdefault(tid, {})
                master.update(record)

            # If closed or exit, track net P&L point
            if record.get("event") == "EXIT" or record.get("status") == "CLOSED":
                net_p = float(record.get("net_pnl", 0.0))
                ts = float(record.get("timestamp", time.time()))
                m = str(record.get("mode", "paper")).lower()
                strat = str(record.get("strategy", "momentum"))
                trk = str(record.get("track", "ATM"))
                self._pnl_events.append((ts, net_p, m, strat, trk))

            if write_to_disk:
                line = json.dumps(record) + "\n"
                for fpath in (self.recording_file, self.log_file):
                    try:
                        with open(fpath, "a", encoding="utf-8") as f:
                            f.write(line)
                            f.flush()
                    except Exception:
                        pass

            listeners = list(self._listeners)

        for cb in listeners:
            try:
                cb(record)
            except Exception:
                pass

        return record

    def get_trades(
        self,
        mode: Optional[str] = None,
        strategy: Optional[str] = None,
        track: Optional[str] = None,
        closed_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Retrieve trades matching filter criteria."""
        with self._lock:
            trades = list(self._trades_by_id.values())

        res = []
        for t in trades:
            if mode and t.get("mode", "").lower() != mode.lower():
                continue
            if strategy and t.get("strategy", "").lower() != strategy.lower():
                continue
            if track and t.get("track", "").upper() != track.upper():
                continue
            if closed_only and t.get("status") != "CLOSED":
                continue
            res.append(t)
        return res

    def compute_summary(
        self,
        mode: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute aggregate summary metrics across trades."""
        trades = self.get_trades(mode=mode, strategy=strategy, closed_only=True)

        total_trades = len(trades)
        wins = 0
        losses = 0
        gross_pnl = 0.0
        brokerage = 0.0
        net_pnl = 0.0
        total_gains = 0.0
        total_losses = 0.0
        best_trade = 0.0
        worst_trade = 0.0

        for t in trades:
            np = float(t.get("net_pnl", 0.0))
            gp = float(t.get("gross_pnl", 0.0))
            br = float(t.get("brokerage", 0.0))
            gross_pnl += gp
            brokerage += br
            net_pnl += np

            if np > 0:
                wins += 1
                total_gains += np
            else:
                losses += 1
                total_losses += abs(np)

            if np > best_trade:
                best_trade = np
            if np < worst_trade:
                worst_trade = np

        win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
        profit_factor = (total_gains / total_losses) if total_losses > 0 else (99.9 if total_gains > 0 else 0.0)

        return {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "gross_pnl": round(gross_pnl, 2),
            "brokerage": round(brokerage, 2),
            "net_pnl": round(net_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
        }

    def get_pnl_timeseries(
        self,
        mode: Optional[str] = None,
        strategy: Optional[str] = None,
        track: Optional[str] = None,
    ) -> List[Tuple[float, float]]:
        """
        Return list of (timestamp, cumulative_net_pnl) for line chart plotting.
        """
        with self._lock:
            events = list(self._pnl_events)

        filtered = []
        for ts, net_p, m, s, trk in events:
            if mode and m != mode.lower():
                continue
            if strategy and s.lower() != strategy.lower():
                continue
            if track and trk.upper() != track.upper():
                continue
            filtered.append((ts, net_p))

        filtered.sort(key=lambda x: x[0])

        cumulative: List[Tuple[float, float]] = []
        running_sum = 0.0
        for ts, delta in filtered:
            running_sum += delta
            cumulative.append((ts, round(running_sum, 2)))

        return cumulative


_GLOBAL_AGGREGATOR: Optional[PnLAggregator] = None


def get_global_pnl_aggregator(session_date: Optional[str] = None) -> PnLAggregator:
    """Retrieve or create singleton PnLAggregator instance."""
    global _GLOBAL_AGGREGATOR
    if _GLOBAL_AGGREGATOR is None:
        _GLOBAL_AGGREGATOR = PnLAggregator(session_date=session_date)
    return _GLOBAL_AGGREGATOR


def reset_global_pnl_aggregator() -> None:
    """Reset the global singleton aggregator instance."""
    global _GLOBAL_AGGREGATOR
    _GLOBAL_AGGREGATOR = None


