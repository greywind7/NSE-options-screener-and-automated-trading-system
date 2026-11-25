"""
Historical Service: Scans historical sessions, parses trade journals, and generates analytics.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST


class HistoricalService:
    def __init__(self, recordings_dir: str = "recordings", logs_dir: str = "logs"):
        self.recordings_dir = recordings_dir
        self.logs_dir = logs_dir
        self._date_cache: Dict[str, Dict[Tuple[str, str], List[Dict[str, Any]]]] = {}
        self._overview_cache: Dict[str, Dict[str, Any]] = {}
        self._track_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def get_available_dates(self) -> List[Dict[str, Any]]:
        """
        Scan recordings and logs directory for session dates.
        Returns a list of dicts: [{"date": "2026-11-19", "has_parquet": True, "has_journals": True}, ...]
        """
        date_map: Dict[str, Dict[str, Any]] = {}

        if os.path.exists(self.recordings_dir):
            for entry in os.listdir(self.recordings_dir):
                entry_path = os.path.join(self.recordings_dir, entry)
                if not os.path.isdir(entry_path):
                    continue

                canonical_date = self._normalize_date(entry)
                if not canonical_date:
                    continue

                if canonical_date not in date_map:
                    date_map[canonical_date] = {
                        "date": canonical_date,
                        "raw_dirs": [],
                        "has_parquet": False,
                        "has_journals": False,
                        "trade_count": 0,
                    }
                date_map[canonical_date]["raw_dirs"].append(entry_path)

                # Check contents
                try:
                    files = os.listdir(entry_path)
                    for f in files:
                        fp = os.path.join(entry_path, f)
                        if f.endswith(".parquet") and os.path.getsize(fp) > 0:
                            date_map[canonical_date]["has_parquet"] = True
                        if (f.startswith("journal_") or "trade" in f.lower()) and os.path.getsize(fp) > 0:
                            date_map[canonical_date]["has_journals"] = True
                except Exception:
                    pass

        # Also check logs dir
        if os.path.exists(self.logs_dir):
            for f in os.listdir(self.logs_dir):
                if f.endswith(".jsonl") or f.endswith(".csv"):
                    for d in date_map:
                        if d in f or d.replace("-", "") in f:
                            date_map[d]["has_journals"] = True

        dates_sorted = sorted(date_map.values(), key=lambda x: x["date"], reverse=False)
        return dates_sorted

    def _normalize_date(self, s: str) -> Optional[str]:
        """Convert YYYYMMDD or YYYY-MM-DD to canonical YYYY-MM-DD."""
        if len(s) == 8 and s.isdigit():
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return s
        return None

    def get_historical_overview(self, target_date: str) -> Dict[str, Any]:
        """
        Load and compute mode and track performance for the requested historical date.
        """
        canonical_date = self._normalize_date(target_date) or target_date
        if canonical_date in self._overview_cache:
            return self._overview_cache[canonical_date]

        trades_by_mode_track = self._load_trades_for_date(canonical_date)

        modes_dict: Dict[str, Dict[str, Any]] = {}
        tot_trades = 0
        tot_wins = 0
        tot_be = 0
        tot_losses = 0
        tot_gross = 0.0
        tot_brokerage = 0.0
        tot_net = 0.0

        for (mode, track), trades in sorted(trades_by_mode_track.items()):
            if mode not in modes_dict:
                modes_dict[mode] = {"tracks": {}, "summary": self._empty_track_stat(mode, "ALL")}

            stats = self._compute_historical_track_stats(mode, track, trades)
            modes_dict[mode]["tracks"][track] = stats

            m_sum = modes_dict[mode]["summary"]
            m_sum["trades"] += stats["trades"]
            m_sum["wins"] += stats["wins"]
            m_sum["breakevens"] += stats["breakevens"]
            m_sum["losses"] += stats["losses"]
            m_sum["gross_pnl"] += stats["gross_pnl"]
            m_sum["brokerage"] += stats["brokerage"]
            m_sum["net_pnl"] += stats["net_pnl"]
            m_sum["win_rate"] = (m_sum["wins"] / m_sum["trades"] * 100.0) if m_sum["trades"] > 0 else 0.0

            tot_trades += stats["trades"]
            tot_wins += stats["wins"]
            tot_be += stats["breakevens"]
            tot_losses += stats["losses"]
            tot_gross += stats["gross_pnl"]
            tot_brokerage += stats["brokerage"]
            tot_net += stats["net_pnl"]

        active_track_names = set(track for (mode, track) in trades_by_mode_track.keys())
        n_active = len(active_track_names) if active_track_names else 1

        overall = {
            "trades": tot_trades,
            "wins": tot_wins,
            "breakevens": tot_be,
            "losses": tot_losses,
            "win_rate": round((tot_wins / tot_trades * 100.0) if tot_trades > 0 else 0.0, 1),
            "gross_pnl": round(tot_gross, 2),
            "brokerage": round(tot_brokerage, 2),
            "net_pnl": round(tot_net, 2),
            "avg_track_gross_pnl": round(tot_gross / n_active, 2),
            "avg_track_net_pnl": round(tot_net / n_active, 2),
            "raw_total_net": round(tot_net, 2),
            "active_tracks_count": n_active,
        }

        res = {
            "date": canonical_date,
            "modes": modes_dict,
            "overall": overall,
        }
        self._overview_cache[canonical_date] = res
        return res

    def get_historical_track_detail(self, target_date: str, mode: str, track: str) -> Dict[str, Any]:
        """
        Return full trade list and cumulative intraday curve for a specific historical track.
        """
        canonical_date = self._normalize_date(target_date) or target_date
        cache_key = (canonical_date, mode, track)
        if cache_key in self._track_cache:
            return self._track_cache[cache_key]

        trades_by_mode_track = self._load_trades_for_date(canonical_date)
        key = (mode, track)
        trades = trades_by_mode_track.get(key, [])
        stats = self._compute_historical_track_stats(mode, track, trades)

        # Format trade rows
        formatted = []
        for tr in sorted(trades, key=lambda t: float(t.get("entry_time") or 0.0)):
            tr_copy = dict(tr)
            e_ts = float(tr.get("entry_time") or 0.0)
            x_ts = float(tr.get("exit_time") or 0.0)
            tr_copy["entry_time_str"] = datetime.fromtimestamp(e_ts, tz=IST).strftime("%H:%M:%S") if e_ts > 0 else "-"
            tr_copy["exit_time_str"] = datetime.fromtimestamp(x_ts, tz=IST).strftime("%H:%M:%S") if x_ts > 0 else "-"

            entry_p = float(tr.get("entry_price", 0.0))
            exit_p = float(tr["exit_price"]) if tr.get("exit_price") is not None else 0.0
            pnl = float(tr.get("realized_pnl", 0.0))
            ret_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0
            fee = float(tr.get("metadata", {}).get("brokerage", 60.0))

            tr_copy["pnl"] = round(pnl, 2)
            tr_copy["return_pct"] = round(ret_pct * 100.0, 2)
            tr_copy["brokerage"] = round(fee, 2)
            tr_copy["net_pnl"] = round(pnl - fee, 2)
            tr_copy["date"] = canonical_date

            u_entry = (
                tr.get("underlying_entry_price")
                or tr.get("metadata", {}).get("underlying_entry_price")
                or tr.get("metadata", {}).get("trigger_price")
                or tr.get("trigger_price")
            )
            if u_entry is not None:
                tr_copy["underlying_entry_price"] = float(u_entry)
                tr_copy["trigger_price"] = float(u_entry)

            formatted.append(tr_copy)

        # Performance curve across the session
        curve = self._build_historical_curve(trades)

        res = {
            "date": canonical_date,
            "mode": mode,
            "track": track,
            "stats": stats,
            "trades": formatted,
            "performance_curve": curve,
        }
        self._track_cache[cache_key] = res
        return res

    def _load_trades_for_date(self, canonical_date: str) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
        """Read and cache trade records for a session date."""
        if canonical_date in self._date_cache:
            return self._date_cache[canonical_date]

        cand_dirs = [
            os.path.join(self.recordings_dir, canonical_date),
            os.path.join(self.recordings_dir, canonical_date.replace("-", "")),
        ]

        trades_by_mode_track: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        modes = ["paper", "paper_delayed", "live", "sim"]

        for c_dir in cand_dirs:
            if not os.path.exists(c_dir):
                continue
            for fname in sorted(os.listdir(c_dir)):
                fpath = os.path.join(c_dir, fname)
                if fname.startswith("journal_") and fname.endswith(".jsonl"):
                    stem = fname[len("journal_"):-len(".jsonl")]
                    matched_mode = None
                    matched_track = None
                    for cand in sorted(modes, key=len, reverse=True):
                        if stem.startswith(cand + "_"):
                            matched_mode = cand
                            matched_track = stem[len(cand) + 1:]
                            break
                    if not matched_mode:
                        matched_mode = "paper"
                        matched_track = stem

                    trades = self._parse_journal_file(fpath, matched_mode, matched_track)
                    key = (matched_mode, matched_track)
                    if key not in trades_by_mode_track:
                        trades_by_mode_track[key] = []
                    trades_by_mode_track[key].extend(trades)

                elif fname == "live_trades.csv":
                    csv_trades = self._parse_live_trades_csv(fpath)
                    for tr in csv_trades:
                        k = (tr.get("mode", "live"), tr.get("track", "fast_momentum"))
                        if k not in trades_by_mode_track:
                            trades_by_mode_track[k] = []
                        trades_by_mode_track[k].append(tr)

        self._date_cache[canonical_date] = trades_by_mode_track
        return trades_by_mode_track

    def _parse_journal_file(self, filepath: str, mode: str, track: str) -> List[Dict[str, Any]]:
        trades_map: Dict[str, Dict[str, Any]] = {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        event = record.get("event")
                        data = record.get("data", {})
                        trade_id = data.get("trade_id")
                        if not trade_id:
                            continue

                        if trade_id not in trades_map:
                            trades_map[trade_id] = {
                                "trade_id": trade_id,
                                "mode": mode,
                                "track": track,
                                "symbol": data.get("symbol", ""),
                                "token": normalize_instrument_token(data.get("token", "")),
                                "option_type": data.get("option_type", ""),
                                "strike": float(data.get("strike", 0.0)),
                                "lot_size": int(data.get("lot_size", 0)),
                                "quantity": int(data.get("quantity", 0)),
                                "entry_price": float(data.get("entry_price", 0.0) or data.get("entry_ask", 0.0)),
                                "entry_time": float(data.get("entry_time", 0.0) or record.get("timestamp", 0.0)),
                                "target_price": float(data.get("target_price", 0.0)),
                                "stop_loss": float(data.get("stop_loss", 0.0)),
                                "status": data.get("status", "OPEN"),
                                "entry_reason": data.get("entry_reason") or data.get("metadata", {}).get("entry_reason") or data.get("metadata", {}).get("archetype") or "-",
                                "origin_track": data.get("origin_track") or data.get("metadata", {}).get("origin_track") or data.get("metadata", {}).get("sub_track") or track,
                                "exit_price": data.get("exit_price"),
                                "exit_time": data.get("exit_time"),
                                "exit_reason": data.get("exit_reason"),
                                "realized_pnl": float(data.get("realized_pnl", 0.0)),
                                "metadata": data.get("metadata", {}),
                            }
                        else:
                            IMMUTABLE_FIELDS = {
                                "trade_id", "mode", "track", "symbol", "token",
                                "option_type", "strike", "lot_size", "quantity",
                                "entry_ask", "entry_time", "entry_reason", "origin_track",
                            }
                            tr = trades_map[trade_id]
                            for k, v in data.items():
                                if v is None:
                                    continue
                                if k == "token":
                                    tr[k] = normalize_instrument_token(v)
                                elif k == "metadata" and isinstance(v, dict):
                                    merged_meta = dict(tr.get("metadata") or {})
                                    merged_meta.update(v)
                                    tr["metadata"] = merged_meta
                                elif k == "entry_price":
                                    if event == "TRADE_FILLED" and float(v) > 0.0:
                                        tr["entry_price"] = float(v)
                                elif k == "entry_time":
                                    if event == "TRADE_FILLED" and float(v) > 0.0:
                                        tr["entry_time"] = float(v)
                                elif k not in IMMUTABLE_FIELDS:
                                    tr[k] = v

                        if event == "TRADE_EXIT" or data.get("status") == "CLOSED":
                            trades_map[trade_id]["status"] = "CLOSED"
                            if data.get("exit_price") is not None:
                                trades_map[trade_id]["exit_price"] = float(data["exit_price"])
                            if data.get("exit_time") is not None:
                                trades_map[trade_id]["exit_time"] = float(data["exit_time"])
                            if data.get("exit_reason"):
                                trades_map[trade_id]["exit_reason"] = data["exit_reason"]
                            if data.get("realized_pnl") is not None:
                                trades_map[trade_id]["realized_pnl"] = float(data["realized_pnl"])
                    except Exception:
                        pass
        except Exception:
            pass

        return list(trades_map.values())

    def _parse_live_trades_csv(self, filepath: str) -> List[Dict[str, Any]]:
        trades = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tr = {
                        "trade_id": row.get("trade_id") or f"CSV_{len(trades)}",
                        "mode": row.get("mode", "live"),
                        "track": row.get("track", "fast_momentum"),
                        "symbol": row.get("symbol", ""),
                        "token": normalize_instrument_token(row.get("token", "")),
                        "option_type": row.get("option_type", ""),
                        "strike": float(row.get("strike", 0.0) or 0.0),
                        "quantity": int(row.get("quantity", 0) or 0),
                        "lot_size": int(row.get("lot_size", 0) or 0),
                        "entry_price": float(row.get("entry_price", 0.0) or 0.0),
                        "exit_price": float(row.get("exit_price", 0.0) or 0.0),
                        "entry_time": float(row.get("entry_time", 0.0) or 0.0),
                        "exit_time": float(row.get("exit_time", 0.0) or 0.0),
                        "status": "CLOSED" if float(row.get("exit_price", 0.0) or 0.0) > 0 else "OPEN",
                        "exit_reason": row.get("exit_reason", "TARGET_HIT"),
                        "realized_pnl": float(row.get("pnl", 0.0) or row.get("realized_pnl", 0.0) or 0.0),
                        "metadata": {"brokerage": float(row.get("brokerage", 60.0) or 60.0)},
                    }
                    trades.append(tr)
        except Exception:
            pass
        return trades

    def _compute_historical_track_stats(self, mode: str, track: str, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        trades_count = len(trades)
        wins = 0
        breakevens = 0
        losses = 0
        gross_pnl = 0.0
        brokerage = 0.0

        for tr in trades:
            entry_p = float(tr.get("entry_price", 0.0))
            exit_p = float(tr["exit_price"]) if tr.get("exit_price") is not None else 0.0
            realized = float(tr.get("realized_pnl", 0.0))
            fee = float(tr.get("metadata", {}).get("brokerage", 60.0))
            gross_pnl += realized
            brokerage += fee

            ret_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0
            if ret_pct > 0.025:
                wins += 1
            elif ret_pct >= -1e-6:
                breakevens += 1
            else:
                losses += 1

        win_rate = (wins / trades_count * 100.0) if trades_count > 0 else 0.0
        net_pnl = gross_pnl - brokerage
        start_budget = 250000.0
        end_budget = round(start_budget + net_pnl, 2)

        return {
            "mode": mode,
            "track": track,
            "trades": trades_count,
            "wins": wins,
            "breakevens": breakevens,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "gross_pnl": round(gross_pnl, 2),
            "brokerage": round(brokerage, 2),
            "net_pnl": round(net_pnl, 2),
            "start_budget": start_budget,
            "end_budget": end_budget,
        }

    def _build_historical_curve(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Cumulative realized PnL curve plotted across time with net and gross options."""
        if not trades:
            return []

        # Filter trades with exit times
        closed_trades = [tr for tr in trades if float(tr.get("exit_time") or 0.0) > 0]
        if not closed_trades:
            return []

        closed_sorted = sorted(closed_trades, key=lambda t: float(t["exit_time"]))
        curve = []
        cum_net = 0.0
        cum_gross = 0.0

        # Start anchor
        first_ts = float(closed_sorted[0]["exit_time"])
        curve.append({
            "time": datetime.fromtimestamp(first_ts - 60.0, tz=IST).strftime("%H:%M:%S"),
            "timestamp": round(first_ts - 60.0, 1),
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "pnl": 0.0,
        })

        for tr in closed_sorted:
            x_ts = float(tr["exit_time"])
            pnl = float(tr.get("realized_pnl", 0.0))
            fee = float(tr.get("metadata", {}).get("brokerage", 60.0))
            cum_net += (pnl - fee)
            cum_gross += pnl
            curve.append({
                "time": datetime.fromtimestamp(x_ts, tz=IST).strftime("%H:%M:%S"),
                "timestamp": round(x_ts, 1),
                "net_pnl": round(cum_net, 2),
                "gross_pnl": round(cum_gross, 2),
                "pnl": round(cum_net, 2),
            })

        return curve

    def _empty_track_stat(self, mode: str, track: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "track": track,
            "trades": 0,
            "wins": 0,
            "breakevens": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "brokerage": 0.0,
            "net_pnl": 0.0,
        }
