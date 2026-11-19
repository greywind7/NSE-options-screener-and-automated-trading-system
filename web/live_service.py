"""
Live Service: Real-time telemetry, options socket listener, MTM tracker, and intraday indexer.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import polars as pl
import zmq
import zmq.asyncio

from core.common.ipc_schemas import unpack_message
from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST, get_market_date_str

DEFAULT_FEED_IPC = "ipc:///tmp/feed_options.ipc"
DEFAULT_STREAM_IPC = "ipc:///tmp/quotes_stream.ipc"


class LiveService:
    def __init__(
        self,
        recordings_dir: str = "recordings",
        feed_ipc: str = DEFAULT_FEED_IPC,
        stream_ipc: str = DEFAULT_STREAM_IPC,
    ):
        self.recordings_dir = recordings_dir
        self.feed_ipc = feed_ipc
        self.stream_ipc = stream_ipc
        self.running = False

        # Latest Value Cache for options quotes: normalized_token -> {bid, ask, ltp, ts}
        self.lvc: Dict[str, Dict[str, Any]] = {}
        self.active_open_tokens: Set[str] = set()

        self._zmq_ctx: Optional[zmq.asyncio.Context] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._cache_date: str = ""
        self._last_scan_ts: float = 0.0

        # Cached parsed state
        self._cached_trades_by_mode_track: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._journal_mtimes: Dict[str, float] = {}

    async def start(self) -> None:
        """Start async background listener for active options ZeroMQ sockets."""
        self.running = True
        self._zmq_ctx = zmq.asyncio.Context()
        self._listener_task = asyncio.create_task(self._listen_options_stream())

    async def stop(self) -> None:
        """Stop background tasks and clean up sockets."""
        self.running = False
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._zmq_ctx:
            self._zmq_ctx.term()

    async def _listen_options_stream(self) -> None:
        """Subscribe to live option depth from feed_options.ipc or quotes_stream.ipc."""
        sub = self._zmq_ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.LINGER, 0)
        sub.setsockopt(zmq.RCVHWM, 50000)

        connected_feed = False
        connected_stream = False

        while self.running:
            # Check and connect to available IPC sockets dynamically
            if not connected_feed and os.path.exists(self.feed_ipc.replace("ipc://", "")):
                try:
                    sub.connect(self.feed_ipc)
                    sub.setsockopt(zmq.SUBSCRIBE, b"TICK.OPT")
                    connected_feed = True
                except Exception:
                    pass

            if not connected_stream and os.path.exists(self.stream_ipc.replace("ipc://", "")):
                try:
                    sub.connect(self.stream_ipc)
                    sub.setsockopt(zmq.SUBSCRIBE, b"QUOTE.")
                    connected_stream = True
                except Exception:
                    pass

            if not (connected_feed or connected_stream):
                await asyncio.sleep(1.0)
                continue

            try:
                frames = await asyncio.wait_for(sub.recv_multipart(), timeout=1.0)
                raw_bytes = None
                if len(frames) >= 2:
                    raw_bytes = frames[1]
                elif len(frames) == 1 and len(frames[0]) > 8:
                    raw_bytes = frames[0][8:]

                if raw_bytes:
                    try:
                        data = unpack_message(raw_bytes)
                        token = normalize_instrument_token(data.get("token") or data.get("symbol", ""))
                        if token:
                            ltp = float(data.get("ltp", 0.0))
                            bid = float(data.get("bid", ltp))
                            ask = float(data.get("ask", ltp))
                            ts = float(data.get("exchange_timestamp") or data.get("received_timestamp") or time.time())
                            self.lvc[token] = {
                                "token": token,
                                "symbol": data.get("symbol", ""),
                                "ltp": ltp,
                                "bid": bid,
                                "ask": ask,
                                "timestamp": ts,
                            }
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                # Idle heartbeat check
                pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

        try:
            sub.close()
        except Exception:
            pass

    def get_today_date_str(self) -> str:
        """Get formatted session date for today."""
        # Find latest date directory in recordings
        today = get_market_date_str()
        rec_today = os.path.join(self.recordings_dir, today)
        if os.path.exists(rec_today):
            return today

        # Look for most recent date in recordings
        if os.path.exists(self.recordings_dir):
            dates = sorted([
                d for d in os.listdir(self.recordings_dir)
                if os.path.isdir(os.path.join(self.recordings_dir, d)) and ("-" in d or len(d) == 8)
            ], reverse=True)
            if dates:
                return dates[0]
        return today

    def scan_live_journals(self, force: bool = False) -> Dict[str, Any]:
        """
        Scan live journal files for the current session date.
        Reconstructs all open and closed trades and groups performance by mode and track.
        Caches parsed state using file mtimes to prevent disk polling on every click.
        """
        now = time.time()
        date_str = self.get_today_date_str()
        session_dir = os.path.join(self.recordings_dir, date_str)
        if not os.path.exists(session_dir):
            # Try non-hyphenated variant
            alt_dir = os.path.join(self.recordings_dir, date_str.replace("-", ""))
            if os.path.exists(alt_dir):
                session_dir = alt_dir

        if not os.path.exists(session_dir):
            return {"date": date_str, "modes": {}, "overall": self._empty_overall()}

        # Check file mtimes in session_dir
        current_mtimes: Dict[str, float] = {}
        for fname in sorted(os.listdir(session_dir)):
            if fname.startswith("journal_") and fname.endswith(".jsonl"):
                fpath = os.path.join(session_dir, fname)
                try:
                    current_mtimes[fname] = os.path.getmtime(fpath)
                except OSError:
                    pass

        # If cache exists and mtimes haven't changed and date matches, reuse cached parsed trades
        if (
            not force
            and self._cached_trades_by_mode_track
            and self._cache_date == date_str
            and current_mtimes == self._journal_mtimes
        ):
            # Fast in-memory recalculation for live MTM quotes
            self._sync_lvc_from_flushed_parquet(session_dir, self.active_open_tokens)
            return self._build_live_summary()

        modes = ["paper", "paper_delayed", "live", "sim"]
        trades_by_mode_track: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        all_open_tokens: Set[str] = set()

        # Read journal files in session_dir
        for fname in sorted(os.listdir(session_dir)):
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

                fpath = os.path.join(session_dir, fname)
                trades = self._read_journal_trades(fpath, matched_mode, matched_track)
                key = (matched_mode, matched_track)
                trades_by_mode_track[key] = trades

                for tr in trades:
                    if tr.get("status") == "OPEN":
                        tok = normalize_instrument_token(tr.get("token", ""))
                        if tok:
                            all_open_tokens.add(tok)

        self.active_open_tokens = all_open_tokens
        self._cached_trades_by_mode_track = trades_by_mode_track
        self._journal_mtimes = current_mtimes
        self._cache_date = date_str
        self._last_scan_ts = now

        # Also populate LVC from latest flushed parquet if token not yet present in LVC
        self._sync_lvc_from_flushed_parquet(session_dir, all_open_tokens)

        return self._build_live_summary()

    def _sync_lvc_from_flushed_parquet(self, session_dir: str, open_tokens: Set[str]) -> None:
        """Ingest the latest quotes from flushed option_depth.parquet for tokens missing from LVC."""
        missing = [tok for tok in open_tokens if tok not in self.lvc]
        if not missing:
            return

        pq_path = os.path.join(session_dir, "option_depth.parquet")
        if not os.path.exists(pq_path):
            return

        try:
            # Selective filter using Polars for high speed
            df = pl.scan_parquet(pq_path).filter(pl.col("token").is_in(missing)).collect()
            if len(df) > 0:
                for tok in missing:
                    sub_df = df.filter(pl.col("token") == tok)
                    if len(sub_df) > 0:
                        last_row = sub_df.row(-1, named=True)
                        ltp = float(last_row.get("ltp", 0.0))
                        bid = float(last_row.get("bid", ltp))
                        ask = float(last_row.get("ask", ltp))
                        ts = float(last_row.get("exchange_timestamp") or last_row.get("received_timestamp") or 0.0)
                        self.lvc[tok] = {
                            "token": tok,
                            "symbol": last_row.get("symbol", ""),
                            "ltp": ltp,
                            "bid": bid,
                            "ask": ask,
                            "timestamp": ts,
                        }
        except Exception:
            pass

    def _read_journal_trades(self, filepath: str, mode: str, track: str) -> List[Dict[str, Any]]:
        """Parse journal JSONL and reconstruct complete trade lifecycle."""
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
                                "highest_price": float(data.get("highest_price", 0.0)),
                                "current_bid": float(data.get("current_bid", 0.0)),
                                "status": data.get("status", "OPEN"),
                                "entry_reason": data.get("entry_reason") or data.get("metadata", {}).get("entry_reason") or data.get("metadata", {}).get("archetype") or "-",
                                "origin_track": data.get("origin_track") or data.get("metadata", {}).get("origin_track") or data.get("metadata", {}).get("sub_track") or track,
                                "exit_price": data.get("exit_price"),
                                "exit_time": data.get("exit_time"),
                                "exit_reason": data.get("exit_reason"),
                                "realized_pnl": float(data.get("realized_pnl", 0.0)),
                                "ratchet_level": int(data.get("ratchet_level", 0)),
                                "metadata": data.get("metadata", {}),
                            }
                        else:
                            tr = trades_map[trade_id]
                            for k, v in data.items():
                                if v is not None:
                                    if k == "token":
                                        tr[k] = normalize_instrument_token(v)
                                    else:
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

    def _build_live_summary(self) -> Dict[str, Any]:
        """Aggregate current trades into live summary grouped by mode and track."""
        modes_dict: Dict[str, Dict[str, Any]] = {}
        tot_trades = 0
        tot_open = 0
        tot_wins = 0
        tot_be = 0
        tot_losses = 0
        tot_gross = 0.0
        tot_brokerage = 0.0
        tot_mtm = 0.0
        tot_net = 0.0

        for (mode, track), trades in sorted(self._cached_trades_by_mode_track.items()):
            if mode not in modes_dict:
                modes_dict[mode] = {"tracks": {}, "summary": self._empty_track_stat(mode, "ALL")}

            # Compute stats for this track
            stats = self._compute_track_stats(mode, track, trades)
            modes_dict[mode]["tracks"][track] = stats

            # Aggregate to mode summary
            m_sum = modes_dict[mode]["summary"]
            m_sum["trades"] += stats["trades"]
            m_sum["open"] += stats["open"]
            m_sum["closed"] += stats["closed"]
            m_sum["wins"] += stats["wins"]
            m_sum["breakevens"] += stats["breakevens"]
            m_sum["losses"] += stats["losses"]
            m_sum["gross_pnl"] += stats["gross_pnl"]
            m_sum["brokerage"] += stats["brokerage"]
            m_sum["mtm_pnl"] += stats["mtm_pnl"]
            m_sum["net_pnl"] += stats["net_pnl"]
            m_sum["win_rate"] = (m_sum["wins"] / m_sum["closed"] * 100.0) if m_sum["closed"] > 0 else 0.0

            # Overall
            tot_trades += stats["trades"]
            tot_open += stats["open"]
            tot_wins += stats["wins"]
            tot_be += stats["breakevens"]
            tot_losses += stats["losses"]
            tot_gross += stats["gross_pnl"]
            tot_brokerage += stats["brokerage"]
            tot_mtm += stats["mtm_pnl"]
            tot_net += stats["net_pnl"]

        tot_closed = tot_trades - tot_open
        active_track_names = set(track for (mode, track) in self._cached_trades_by_mode_track.keys())
        n_active = len(active_track_names) if active_track_names else 1

        overall = {
            "trades": tot_trades,
            "open": tot_open,
            "closed": tot_closed,
            "wins": tot_wins,
            "breakevens": tot_be,
            "losses": tot_losses,
            "win_rate": (tot_wins / tot_closed * 100.0) if tot_closed > 0 else 0.0,
            "gross_pnl": round(tot_gross / n_active, 2),
            "brokerage": round(tot_brokerage, 2),
            "mtm_pnl": round(tot_mtm / n_active, 2),
            "net_pnl": round(tot_net / n_active, 2),
            "raw_total_net": round(tot_net, 2),
            "active_tracks_count": n_active,
        }

        return {
            "date": self._cache_date,
            "timestamp": time.time(),
            "time_ist": datetime.now(IST).strftime("%H:%M:%S"),
            "modes": modes_dict,
            "overall": overall,
        }

    def _compute_track_stats(self, mode: str, track: str, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute real-time PnL, MTM, and win rate for a track."""
        trades_count = len(trades)
        open_count = 0
        closed_count = 0
        wins = 0
        breakevens = 0
        losses = 0
        gross_pnl = 0.0
        brokerage = 0.0
        mtm_pnl = 0.0

        for tr in trades:
            b_fee = float(tr.get("metadata", {}).get("brokerage", 60.0))
            if tr.get("status") == "OPEN":
                open_count += 1
                tok = tr.get("token", "")
                quote = self.lvc.get(tok, {})
                current_bid = float(quote.get("bid") or quote.get("ltp") or tr.get("current_bid") or tr.get("entry_price") or 0.0)
                entry_p = float(tr.get("entry_price", 0.0))
                qty = int(tr.get("quantity", 0))

                unrealized = (current_bid - entry_p) * qty
                tr["current_bid"] = current_bid
                tr["mtm"] = round(unrealized, 2)
                mtm_pnl += unrealized
                # Single side brokerage for open
                brokerage += (b_fee / 2.0)
            else:
                closed_count += 1
                entry_p = float(tr.get("entry_price", 0.0))
                exit_p = float(tr.get("exit_price") or entry_p)
                realized = float(tr.get("realized_pnl", 0.0))
                gross_pnl += realized
                brokerage += b_fee

                ret_pct = (exit_p - entry_p) / entry_p if entry_p > 0 else 0.0
                if ret_pct > 0.025:
                    wins += 1
                elif ret_pct >= -1e-6:
                    breakevens += 1
                else:
                    losses += 1

        win_rate = (wins / closed_count * 100.0) if closed_count > 0 else 0.0
        net_pnl = (gross_pnl - brokerage) + mtm_pnl
        start_budget = 250000.0
        current_budget = round(start_budget + net_pnl, 2)

        return {
            "mode": mode,
            "track": track,
            "trades": trades_count,
            "open": open_count,
            "closed": closed_count,
            "wins": wins,
            "breakevens": breakevens,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "gross_pnl": round(gross_pnl, 2),
            "brokerage": round(brokerage, 2),
            "mtm_pnl": round(mtm_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "start_budget": start_budget,
            "current_budget": current_budget,
        }

    def get_track_detail(self, mode: str, track: str) -> Dict[str, Any]:
        """
        Return all trades and the cumulative intraday performance curve for a specific track.
        """
        self.scan_live_journals(force=False)
        key = (mode, track)
        trades = self._cached_trades_by_mode_track.get(key, [])
        stats = self._compute_track_stats(mode, track, trades)

        # Build chronological trade list with formatted times
        formatted_trades = []
        for tr in sorted(trades, key=lambda t: float(t.get("entry_time") or 0.0)):
            tr_copy = dict(tr)
            e_ts = float(tr.get("entry_time") or 0.0)
            x_ts = float(tr.get("exit_time") or 0.0)
            tr_copy["entry_time_str"] = datetime.fromtimestamp(e_ts, tz=IST).strftime("%H:%M:%S") if e_ts > 0 else "-"
            tr_copy["exit_time_str"] = datetime.fromtimestamp(x_ts, tz=IST).strftime("%H:%M:%S") if x_ts > 0 else "-"
            tr_copy["date"] = self._cache_date

            entry_p = float(tr.get("entry_price", 0.0))
            if tr.get("status") == "OPEN":
                tok = tr.get("token", "")
                quote = self.lvc.get(tok, {})
                q_ts = float(quote.get("timestamp") or 0.0)
                if q_ts > 0:
                    tr_copy["last_quote_time_str"] = datetime.fromtimestamp(q_ts, tz=IST).strftime("%H:%M:%S")
                    tr_copy["last_quote_age_sec"] = max(0, round(time.time() - q_ts, 1))
                else:
                    tr_copy["last_quote_time_str"] = "-"
                    tr_copy["last_quote_age_sec"] = None

                cur_p = float(quote.get("bid") or quote.get("ltp") or tr.get("current_bid") or entry_p)
                pnl = float(tr.get("mtm", 0.0))
                ret_pct = (cur_p - entry_p) / entry_p if entry_p > 0 else 0.0
            else:
                cur_p = float(tr.get("exit_price") or entry_p)
                pnl = float(tr.get("realized_pnl", 0.0))
                ret_pct = (cur_p - entry_p) / entry_p if entry_p > 0 else 0.0
                tr_copy["last_quote_time_str"] = "-"
                tr_copy["last_quote_age_sec"] = None

            tr_copy["pnl"] = round(pnl, 2)
            tr_copy["return_pct"] = round(ret_pct * 100.0, 2)
            tr_copy["brokerage"] = float(tr.get("metadata", {}).get("brokerage", 60.0))
            tr_copy["net_pnl"] = round(pnl - tr_copy["brokerage"], 2) if tr.get("status") == "CLOSED" else round(pnl - (tr_copy["brokerage"] / 2.0), 2)
            formatted_trades.append(tr_copy)

        # Build intraday cumulative performance curve
        curve = self._build_intraday_curve(trades)

        return {
            "mode": mode,
            "track": track,
            "stats": stats,
            "trades": formatted_trades,
            "performance_curve": curve,
        }

    def _build_intraday_curve(self, trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Generate time-indexed points for cumulative PnL across the trading day.
        Sampling at trade lifecycle events and 3-minute intervals.
        Provides both net_pnl (including brokerage) and gross_pnl.
        """
        if not trades:
            return []

        # Collect event timestamps
        ts_points: Set[float] = set()
        for tr in trades:
            e_ts = float(tr.get("entry_time") or 0.0)
            x_ts = float(tr.get("exit_time") or 0.0)
            if e_ts > 0:
                ts_points.add(e_ts)
            if x_ts > 0:
                ts_points.add(x_ts)

        # Include start of day (09:15) and current time
        sorted_ts = sorted(ts_points)
        if not sorted_ts:
            return []

        min_ts = sorted_ts[0]
        max_ts = max(sorted_ts[-1], time.time())

        # Build 3 minute interval ticks
        step = 180.0
        t = min_ts
        while t <= max_ts:
            ts_points.add(t)
            t += step
        ts_points.add(max_ts)

        eval_times = sorted(ts_points)
        curve = []
        cum_closed_net = 0.0
        cum_closed_gross = 0.0
        processed_exits = set()

        for cur_t in eval_times:
            # Check newly closed trades up to cur_t
            for tr in trades:
                tid = tr.get("trade_id")
                if tid in processed_exits:
                    continue
                if tr.get("status") == "CLOSED":
                    x_ts = float(tr.get("exit_time") or 0.0)
                    if x_ts <= cur_t:
                        pnl = float(tr.get("realized_pnl", 0.0))
                        fee = float(tr.get("metadata", {}).get("brokerage", 60.0))
                        cum_closed_net += (pnl - fee)
                        cum_closed_gross += pnl
                        processed_exits.add(tid)

            # Check open trades MTM at cur_t
            cur_mtm = 0.0
            for tr in trades:
                if tr.get("status") == "OPEN":
                    e_ts = float(tr.get("entry_time") or 0.0)
                    if e_ts <= cur_t:
                        cur_mtm += float(tr.get("mtm", 0.0))

            total_net = cum_closed_net + cur_mtm
            total_gross = cum_closed_gross + cur_mtm
            dt_str = datetime.fromtimestamp(cur_t, tz=IST).strftime("%H:%M:%S")
            curve.append({
                "time": dt_str,
                "timestamp": round(cur_t, 1),
                "net_pnl": round(total_net, 2),
                "gross_pnl": round(total_gross, 2),
                "pnl": round(total_net, 2),
            })

        return curve

    def _empty_track_stat(self, mode: str, track: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "track": track,
            "trades": 0,
            "open": 0,
            "closed": 0,
            "wins": 0,
            "breakevens": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "brokerage": 0.0,
            "mtm_pnl": 0.0,
            "net_pnl": 0.0,
        }

    def _empty_overall(self) -> Dict[str, Any]:
        return {
            "trades": 0,
            "open": 0,
            "closed": 0,
            "wins": 0,
            "breakevens": 0,
            "losses": 0,
            "win_rate": 0.0,
            "gross_pnl": 0.0,
            "brokerage": 0.0,
            "mtm_pnl": 0.0,
            "net_pnl": 0.0,
        }
