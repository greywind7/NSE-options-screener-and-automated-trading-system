"""
Universal Multi-Mode Dashboard: Real-time Terminal Telemetry Engine.
Standardized across all strategies.
Displays scoring alerts with elapsed time, RMS strike depth, and mode-separated
multi-track performance matrices (Paper, Paper-Delayed, Live) read from journals.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import zmq

from core.common.ipc_schemas import unpack_message
from core.common.time_utils import get_market_date_str

DEFAULT_ALERTS_SUB_IPC = "ipc:///tmp/scoring_alerts_pub.ipc"
DEFAULT_RMS_SUB_IPC = "ipc:///tmp/rms_strike_pub.ipc"
DEFAULT_RECORDINGS_DIR = "recordings"


class TerminalDashboard:
    def __init__(
        self,
        alerts_ipc: str = DEFAULT_ALERTS_SUB_IPC,
        rms_ipc: str = DEFAULT_RMS_SUB_IPC,
        recordings_dir: str = DEFAULT_RECORDINGS_DIR,
        modes: Optional[List[str]] = None,
        tracks: Optional[List[str]] = None,
        ctx: Optional[zmq.Context] = None,
    ):
        self.alerts_ipc = alerts_ipc
        self.rms_ipc = rms_ipc
        self.recordings_dir = recordings_dir
        self.running = False

        self.latest_alert: Optional[Dict[str, Any]] = None
        self.latest_opp: Optional[Dict[str, Any]] = None

        # Mode -> Track -> Stats
        self.modes = [m.strip().lower() for m in modes if m.strip()] if modes else ["paper", "paper_delayed", "live"]
        if tracks is not None:
            self.tracks = [t.strip() for t in tracks if t.strip()]
        else:
            try:
                from common.tracks.factory import get_enabled_track_names
                self.tracks = get_enabled_track_names()
            except Exception:
                self.tracks = ["fast_momentum_ATM", "fast_momentum_ITM", "fixntrend"]

        self.track_stats: Dict[str, Dict[str, Dict[str, Any]]] = {
            m: {
                t: {
                    "open": 0,
                    "closed": 0,
                    "win_rate": 0.0,
                    "net_pnl": 0.0,
                    "max_win": 0.0,
                    "max_loss": 0.0,
                }
                for t in self.tracks
            }
            for m in self.modes
        }

        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        # Alert subscriber
        self.alert_sub = self.ctx.socket(zmq.SUB)
        self.alert_sub.setsockopt(zmq.LINGER, 0)
        self.alert_sub.setsockopt(zmq.SUBSCRIBE, b"ALERT.")
        self.alert_sub.connect(self.alerts_ipc)

        # RMS Opportunity subscriber
        self.rms_sub = self.ctx.socket(zmq.SUB)
        self.rms_sub.setsockopt(zmq.LINGER, 0)
        self.rms_sub.setsockopt(zmq.SUBSCRIBE, b"OPP.")
        self.rms_sub.connect(self.rms_ipc)

    def scan_journals(self) -> None:
        """Scan today's journal files and aggregate trackwise metrics per mode."""
        today_str = get_market_date_str()
        session_dir = os.path.join(self.recordings_dir, today_str)
        if not os.path.exists(session_dir):
            return

        for filename in sorted(os.listdir(session_dir)):
            if filename.startswith("journal_") and filename.endswith(".jsonl"):
                stem = filename[len("journal_"):-len(".jsonl")]
                matched_mode = None
                matched_track = None
                # Check candidate modes in descending order of length so "paper_delayed" matches before "paper"
                for candidate in sorted(self.modes, key=len, reverse=True):
                    if stem.startswith(candidate + "_"):
                        matched_mode = candidate
                        matched_track = stem[len(candidate) + 1:]
                        break

                if matched_mode and matched_mode in self.modes and matched_track:
                    if matched_track in self.tracks:
                        self._parse_journal_file(os.path.join(session_dir, filename), matched_mode, matched_track)

    def _parse_journal_file(self, filepath: str, mode: str, track: str) -> None:
        """Read and compute trade stats for a given mode and track."""
        open_trades = 0
        closed_trades = 0
        wins = 0
        losses = 0
        net_pnl = 0.0
        max_win = 0.0
        max_loss = 0.0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        event = record.get("event")
                        data = record.get("data", {})
                        status = data.get("status")

                        if event == "TRADE_EXIT" or status == "CLOSED":
                            closed_trades += 1
                            pnl = float(data.get("realized_pnl", 0.0))
                            net_pnl += pnl
                            if pnl > 0:
                                wins += 1
                                max_win = max(max_win, pnl)
                            elif pnl < 0:
                                losses += 1
                                max_loss = min(max_loss, pnl)
                        elif event in ("TRADE_ENTRY_SUBMITTED", "TRADE_FILLED") and status == "OPEN":
                            open_trades += 1
                    except Exception:
                        pass
        except Exception:
            pass

        win_rate = (wins / closed_trades * 100.0) if closed_trades > 0 else 0.0

        self.track_stats[mode][track] = {
            "open": open_trades,
            "closed": closed_trades,
            "win_rate": win_rate,
            "net_pnl": net_pnl,
            "max_win": max_win,
            "max_loss": max_loss,
        }

    def render(self, clear: bool = True) -> str:
        """Render standardized terminal dashboard string."""
        now = time.time()
        now_dt = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

        matrix_header = f"  {'TRACK':<28} | {'OPEN':<6} | {'CLOSED':<8} | {'WIN %':<8} | {'NET PNL (INR)':<15} | {'MAX WIN':<11} | {'MAX LOSS':<11}"
        border_width = len(matrix_header)

        lines = []
        if clear and sys.stdout.isatty():
            lines.append("\033[2J\033[H")

        lines.append("=" * border_width)
        lines.append(f" TRADING SYSTEM SPEC 2.0 - UNIVERSAL STRATEGY DASHBOARD | TIME: {now_dt}")
        lines.append("=" * border_width)

        # 1. Alert Telemetry Section
        lines.append("\n[ SCORING ENGINE 3.0 ALERT TELEMETRY ]")
        if self.latest_alert:
            a = self.latest_alert
            created_ts = a.get("timestamp", now)
            created_str = datetime.fromtimestamp(created_ts).strftime("%H:%M:%S")
            elapsed_sec = int(now - created_ts)
            lines.append(
                f"  Symbol: {a.get('symbol', 'N/A'):<12} | Archetype: {a.get('archetype', 'N/A'):<22} | "
                f"Direction: {a.get('direction', 'N/A')}"
            )
            lines.append(
                f"  Trigger Price: ₹{float(a.get('trigger_price', 0.0)):<9.2f} | "
                f"rVOL: {float(a.get('rvol', 0.0)):.2f}x | CLV: {float(a.get('clv', 0.0)):.2f} | "
                f"Created: {created_str} ({elapsed_sec}s ago)"
            )
        else:
            lines.append("  No active alerts triggered yet. Monitoring 221 F&O universe...")

        # 2. Available Strikes Section
        lines.append("\n[ RMS AVAILABLE STRIKES & EXECUTION DEPTH ]")
        if self.latest_opp and self.latest_opp.get("strikes"):
            strikes = self.latest_opp["strikes"]
            rms_header = f"  {'ROLE':<8} | {'TOKEN':<28} | {'BID':<10} | {'ASK':<10} | {'SPREAD':<10} | {'LOT':<6}"
            lines.append(rms_header)
            lines.append("  " + "-" * (len(rms_header) - 2))
            for role in ("ATM", "ITM1", "OTM1"):
                if role in strikes:
                    s = strikes[role]
                    bid = float(s.get("bid", 0.0))
                    ask = float(s.get("ask", 0.0))
                    spread = ask - bid
                    lot_val = s.get("lot_size", 1)
                    lines.append(
                        f"  {role:<8} | {s.get('token', ''):<28} | {f'₹{bid:.2f}':<10} | {f'₹{ask:.2f}':<10} | {f'₹{spread:.2f}':<10} | {lot_val:<6}"
                    )
        else:
            lines.append("  Standby for actionable opportunity strikes...")

        # 3. Mode-Separated Multi-Track Performance Matrices
        mode_titles = {
            "paper": "[ PAPER EXECUTION MATRIX ]",
            "paper_delayed": "[ PAPER-DELAYED EXECUTION MATRIX ]",
            "live": "[ LIVE BROKER MATRIX ]",
        }

        for m in self.modes:
            lines.append(f"\n{mode_titles.get(m, f'[ {m.upper()} MATRIX ]')}")
            tracks_data = self.track_stats.get(m, {})
            if self.tracks:
                lines.append(matrix_header)
                lines.append("  " + "-" * (len(matrix_header) - 2))
                for tr_name in self.tracks:
                    stats = tracks_data.get(tr_name, {
                        "open": 0, "closed": 0, "win_rate": 0.0, "net_pnl": 0.0, "max_win": 0.0, "max_loss": 0.0
                    })
                    pnl_color = "+" if stats["net_pnl"] >= 0 else ""
                    pnl_str = f"₹{pnl_color}{stats['net_pnl']:,.2f}"
                    max_win_str = f"₹{stats['max_win']:,.2f}"
                    max_loss_str = f"₹{stats['max_loss']:,.2f}"
                    win_str = f"{stats['win_rate']:.1f}%"
                    lines.append(
                        f"  {tr_name:<28} | {stats['open']:<6} | {stats['closed']:<8} | {win_str:>8} | "
                        f"{pnl_str:>15} | {max_win_str:>11} | {max_loss_str:>11}"
                    )
            else:
                lines.append("  No active tracks configured.")

        lines.append("\n" + "=" * border_width)
        output_str = "\n".join(lines)
        return output_str
        output_str = "\n".join(lines)
        return output_str

    def update_from_network(self) -> None:
        """Non-blocking check for new ZeroMQ messages."""
        poller = zmq.Poller()
        poller.register(self.alert_sub, zmq.POLLIN)
        poller.register(self.rms_sub, zmq.POLLIN)

        events = dict(poller.poll(timeout=10))
        if self.alert_sub in events:
            _, payload = self.alert_sub.recv_multipart()
            self.latest_alert = unpack_message(payload)

        if self.rms_sub in events:
            _, payload = self.rms_sub.recv_multipart()
            self.latest_opp = unpack_message(payload)

    def start(self, refresh_interval: float = 1.0) -> None:
        """Run terminal telemetry dashboard refresh loop."""
        self.running = True
        print(f"[DASHBOARD] Active | Alerts: {self.alerts_ipc} | RMS: {self.rms_ipc}")

        while self.running:
            try:
                self.update_from_network()
                self.scan_journals()
                rendered = self.render(clear=True)
                print(rendered, flush=True)
                time.sleep(refresh_interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[DASHBOARD] Error: {e}", file=sys.stderr)
                time.sleep(1.0)

    def stop(self) -> None:
        self.running = False
        try:
            self.alert_sub.close(linger=0)
            self.rms_sub.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        print("[DASHBOARD] Stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Universal Strategy Telemetry Dashboard")
    parser.add_argument("--alerts-ipc", default=DEFAULT_ALERTS_SUB_IPC, help="Scoring alerts PUB address")
    parser.add_argument("--rms-ipc", default=DEFAULT_RMS_SUB_IPC, help="RMS opportunities PUB address")
    parser.add_argument("--recordings-dir", default=DEFAULT_RECORDINGS_DIR, help="Recordings root dir")
    parser.add_argument("--modes", "--mode", dest="modes", default=None, help="Execution mode(s) to display (comma-separated, e.g. 'paper,paper_delayed')")
    parser.add_argument("--tracks", "--track", dest="tracks", default=None, help="Strategy tracks to monitor (comma-separated, e.g. 'fast_momentum_ATM,fast_momentum_ITM,fixntrend')")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval seconds")
    args = parser.parse_args()

    active_modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()] if args.modes else None
    active_tracks = [t.strip() for t in args.tracks.split(",") if t.strip()] if args.tracks else None
    dashboard = TerminalDashboard(
        alerts_ipc=args.alerts_ipc,
        rms_ipc=args.rms_ipc,
        recordings_dir=args.recordings_dir,
        modes=active_modes,
        tracks=active_tracks,
    )

    def sig_handler(sig, frame):
        dashboard.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    dashboard.start(refresh_interval=args.refresh)


if __name__ == "__main__":
    main()

