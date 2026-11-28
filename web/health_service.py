"""
System Health & Day's Log Inspector Service.
Inspects OS process status, IPC sockets, Shared Memory freshness, and today's structured logs.
Escalates overall status immediately to RED when any ERROR/CRITICAL log appears today
or any registered process dies unexpectedly.
"""
from __future__ import annotations

from datetime import datetime
import glob
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

from core.common.time_utils import IST
from core.config.config_loader import get_config_manager
from core.ops.operations import get_freeze_status
from scripts.run_distributed import infer_process_tier, is_pid_alive, load_pids

STRUCTURED_LOG_RE = re.compile(
    r"^\[(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+IST\]\s+"
    r"\[(?P<level>[A-Z]+)\]\s+\[(?P<logger>[^\]]+)\]\s+(?P<message>.*)$"
)

DEFAULT_IPC_PATHS = [
    "/tmp/feed_equity.ipc",
    "/tmp/feed_options.ipc",
    "/tmp/quotes_router.ipc",
    "/tmp/quotes_heartbeat.pub",
    "/tmp/scoring_alerts_pub.ipc",
    "/tmp/rms_strike_pub.ipc",
]


def _normalize_level(raw_level: str) -> str:
    lvl = (raw_level or "INFO").upper()
    if lvl in ("WARNING", "WARN"):
        return "WARN"
    if lvl in ("CRITICAL", "FATAL", "ERR", "ERROR"):
        return "ERROR"
    return lvl


class HealthService:
    """Computes live system health (GREEN/YELLOW/RED) and serves day-filtered structured logs."""

    def __init__(
        self,
        logs_dir: str = "logs",
        pids_file: Optional[str] = None,
        ipc_paths: Optional[List[str]] = None,
        shm_name: Optional[str] = None,
        skip_system_probes: Optional[bool] = None,
    ):
        self.logs_dir = logs_dir
        self.pids_file = pids_file or os.path.join(logs_dir, "pids.json")
        if ipc_paths is not None:
            self.ipc_paths = ipc_paths
        else:
            try:
                ipc_cfg = get_config_manager().get_ipc_endpoints()
                resolved_ipcs = []
                for k in (
                    "feed_equity_ipc",
                    "feed_options_ipc",
                    "quotes_router_ipc",
                    "heartbeat_ipc",
                    "scoring_alerts_ipc",
                    "rms_strike_ipc",
                ):
                    ep = ipc_cfg.get(k)
                    if ep and ep.startswith("ipc://"):
                        resolved_ipcs.append(ep[6:])
                self.ipc_paths = resolved_ipcs or list(DEFAULT_IPC_PATHS)
            except Exception:
                self.ipc_paths = list(DEFAULT_IPC_PATHS)
        if shm_name is not None:
            self.shm_name = shm_name
        else:
            try:
                self.shm_name = str(
                    get_config_manager().system.get("system", {}).get("shm", {}).get("shm_name", "shm_ohlc_221")
                )
            except Exception:
                self.shm_name = "shm_ohlc_221"
        if skip_system_probes is None:
            self.skip_system_probes = os.environ.get("TRADING_TEST_MODE") == "1"
        else:
            self.skip_system_probes = bool(skip_system_probes)

    def _query_ps_metrics(self, pids: List[int]) -> Dict[int, Dict[str, Any]]:
        """Batch-query CPU%, RSS(MB), elapsed uptime via `ps` for running PIDs."""
        metrics: Dict[int, Dict[str, Any]] = {}
        clean_pids = [int(p) for p in pids if p and int(p) > 0]
        if not clean_pids:
            return metrics
        try:
            pid_arg = ",".join(str(p) for p in clean_pids)
            proc = subprocess.run(
                ["ps", "-o", "pid=,pcpu=,rss=,etime=,stat=", "-p", pid_arg],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode not in (0, 1):
                return metrics
            for line in proc.stdout.splitlines():
                parts = line.strip().split(None, 4)
                if len(parts) < 5:
                    continue
                try:
                    pid = int(parts[0])
                    cpu_pct = float(parts[1])
                    rss_mb = round(float(parts[2]) / 1024.0, 1)
                    uptime = parts[3]
                    stat = parts[4]
                    metrics[pid] = {
                        "cpu_pct": round(cpu_pct, 1),
                        "rss_mb": rss_mb,
                        "uptime": uptime,
                        "stat": stat,
                    }
                except (ValueError, TypeError):
                    continue
        except Exception:
            pass
        return metrics

    def _parse_today_log_file(
        self,
        filepath: str,
        target_date: str,
        tail_limit: int = 2000,
    ) -> Dict[str, Any]:
        """Parse a log file for today's entries and error/warn counts."""
        filename = os.path.basename(filepath)
        error_count = 0
        warn_count = 0
        info_count = 0
        entries: List[Dict[str, Any]] = []

        if not os.path.exists(filepath):
            return {
                "file": filename,
                "error_count": 0,
                "warn_count": 0,
                "info_count": 0,
                "entries": [],
            }

        try:
            file_date = datetime.fromtimestamp(os.path.getmtime(filepath), tz=IST).strftime("%Y-%m-%d")
        except Exception:
            file_date = target_date

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines()[-tail_limit:]
        except Exception:
            raw_lines = []

        last_seen_date: Optional[str] = None
        for idx, raw in enumerate(raw_lines):
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            m = STRUCTURED_LOG_RE.match(line)
            if m:
                line_date = m.group("date")
                last_seen_date = line_date
                if line_date != target_date:
                    continue
                lvl = _normalize_level(m.group("level"))
                entry = {
                    "file": filename,
                    "date": line_date,
                    "time": m.group("time"),
                    "level": lvl,
                    "logger": m.group("logger"),
                    "message": m.group("message"),
                    "raw": line,
                    "line_no": idx + 1,
                }
            else:
                # Unstructured line (e.g. Python Traceback / third-party stderr)
                inferred_date = last_seen_date or (file_date if file_date == target_date else None)
                if inferred_date != target_date:
                    continue
                upper = line.upper()
                if (
                    "[ERROR]" in upper
                    or "[CRITICAL]" in upper
                    or "TRACEBACK (MOST RECENT CALL LAST)" in upper
                    or re.match(r"^[A-Z][a-zA-Z]*(?:Error|Exception):", line)
                ):
                    lvl = "ERROR"
                elif "[WARN]" in upper or "[WARNING]" in upper:
                    lvl = "WARN"
                else:
                    lvl = "INFO"
                entry = {
                    "file": filename,
                    "date": target_date,
                    "time": "-",
                    "level": lvl,
                    "logger": filename[:-4].upper(),
                    "message": line,
                    "raw": line,
                    "line_no": idx + 1,
                }

            if lvl == "ERROR":
                error_count += 1
            elif lvl == "WARN":
                warn_count += 1
            else:
                info_count += 1
            entries.append(entry)

        return {
            "file": filename,
            "error_count": error_count,
            "warn_count": warn_count,
            "info_count": info_count,
            "entries": entries,
        }

    def _list_log_files(self) -> List[str]:
        if not os.path.exists(self.logs_dir):
            return []
        files: List[str] = []
        seen_names = set()
        for pat in [
            os.path.join(self.logs_dir, "exec_logs", "*.log"),
            os.path.join(self.logs_dir, "*.log"),
        ]:
            for f in sorted(glob.glob(pat)):
                if os.path.isfile(f):
                    b = os.path.basename(f)
                    if b not in seen_names:
                        seen_names.add(b)
                        files.append(f)
        return sorted(files, key=lambda p: os.path.basename(p))

    def _probe_ipc_and_shm(self, candle_manager_alive: Optional[bool] = None) -> Dict[str, Any]:
        if self.skip_system_probes:
            return {
                "ipc_sockets": [],
                "shm": {
                    "name": self.shm_name,
                    "active": False,
                    "backend": "skipped",
                    "reason": "Skipped in TRADING_TEST_MODE",
                    "skipped": True,
                },
            }

        ipc_list = []
        for path in self.ipc_paths:
            exists = os.path.exists(path)
            ipc_list.append({
                "path": path,
                "name": os.path.basename(path),
                "active": exists,
            })

        shm_active = False
        backend = "offline"
        clean_name = self.shm_name.lstrip("/")
        mmap_path = os.path.join("/tmp", f"{clean_name}.shm")

        if candle_manager_alive is None:
            pids = load_pids(self.pids_file)
            cm_info = pids.get("candle_manager") or {}
            cm_pid = int(cm_info.get("pid") or 0)
            candle_manager_alive = bool(cm_pid > 0 and is_pid_alive(cm_pid))

        if not candle_manager_alive:
            reason = "CandleManager inactive (market offline or Core tier stopped)"
            try:
                from core.candles.shm_layout import (
                    SHM_SIGNAL_DETACH,
                    shm_exists,
                    signal_shm_consumers,
                    unlink_shm,
                )
                if shm_exists(self.shm_name):
                    signaled = signal_shm_consumers(SHM_SIGNAL_DETACH, pids_file=self.pids_file)
                    unlink_shm(self.shm_name)
                    backend = "stale_cleared"
                    reason = (
                        f"CandleManager inactive; cleared stale SHM '{self.shm_name}'"
                        + (f" & signaled consumers {signaled} to detach" if signaled else "")
                    )
            except Exception:
                pass
            return {
                "ipc_sockets": ipc_list,
                "shm": {
                    "name": self.shm_name,
                    "path": mmap_path,
                    "active": False,
                    "backend": backend,
                    "reason": reason,
                    "skipped": False,
                },
            }

        reason = f"CandleManager running, but SHM '{self.shm_name}' is not mounted yet"

        # 1. Check mmap backing file (default runtime mechanism on macOS/Linux via shm_layout.py)
        try:
            if os.path.exists(mmap_path) and os.path.getsize(mmap_path) > 0:
                shm_active = True
                backend = "mmap_tmp"
                reason = f"Active mmap backing at {mmap_path} (CandleManager PID alive)"
        except Exception:
            pass

        # 2. Check kernel POSIX SharedMemory if FORCE_POSIX_SHM is used
        if not shm_active:
            try:
                from multiprocessing import shared_memory
                shm = shared_memory.SharedMemory(name=self.shm_name, create=False)
                if shm.size > 0:
                    shm_active = True
                    backend = "posix_shm"
                    reason = f"Active POSIX SharedMemory '{self.shm_name}' (CandleManager PID alive)"
                shm.close()
            except Exception:
                pass

        return {
            "ipc_sockets": ipc_list,
            "shm": {
                "name": self.shm_name,
                "path": mmap_path,
                "active": shm_active,
                "backend": backend,
                "reason": reason,
                "skipped": False,
            },
        }

    def get_status(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """Compute full system health status (GREEN / YELLOW / RED), process table, IPC/SHM, and today's log summary."""
        today_str = target_date or datetime.now(tz=IST).strftime("%Y-%m-%d")
        pids = load_pids(self.pids_file)

        alive_map: Dict[str, bool] = {}
        alive_pids: List[int] = []
        for role, info in pids.items():
            pid = int(info.get("pid") or 0)
            alive = is_pid_alive(pid) if pid > 0 else False
            alive_map[role] = alive
            if alive:
                alive_pids.append(pid)

        ps_metrics = self._query_ps_metrics(alive_pids)

        # Parse all *.log files for today
        log_files = self._list_log_files()
        parsed_by_file: Dict[str, Dict[str, Any]] = {}
        total_errors_today = 0
        total_warns_today = 0
        recent_errors: List[Dict[str, Any]] = []
        log_file_summaries: List[Dict[str, Any]] = []

        for lpath in log_files:
            parsed = self._parse_today_log_file(lpath, today_str)
            fname = parsed["file"]
            parsed_by_file[fname] = parsed
            total_errors_today += parsed["error_count"]
            total_warns_today += parsed["warn_count"]
            recent_errors.extend([e for e in parsed["entries"] if e["level"] == "ERROR"])
            try:
                sz_kb = round(os.path.getsize(lpath) / 1024.0, 1)
                mtime_str = datetime.fromtimestamp(os.path.getmtime(lpath), tz=IST).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                sz_kb = 0.0
                mtime_str = "-"
            log_file_summaries.append({
                "file": fname,
                "size_kb": sz_kb,
                "mtime": mtime_str,
                "today_errors": parsed["error_count"],
                "today_warns": parsed["warn_count"],
                "today_infos": parsed["info_count"],
            })

        processes: List[Dict[str, Any]] = []
        dead_roles: List[str] = []
        for role in sorted(pids.keys()):
            info = pids[role]
            pid = int(info.get("pid") or 0)
            alive = alive_map.get(role, False)
            if not alive:
                dead_roles.append(role)
            pm = ps_metrics.get(pid, {})
            log_file = info.get("log_file") or os.path.join(self.logs_dir, f"{role}.log")
            log_name = os.path.basename(log_file)
            log_stats = parsed_by_file.get(log_name, {})
            processes.append({
                "role": role,
                "tier": info.get("tier") or infer_process_tier(role),
                "pid": pid,
                "alive": alive,
                "status": "RUNNING" if alive else "DEAD",
                "cpu_pct": pm.get("cpu_pct", 0.0) if alive else 0.0,
                "rss_mb": pm.get("rss_mb", 0.0) if alive else 0.0,
                "uptime": pm.get("uptime", "-") if alive else "-",
                "started_at": info.get("started_at", "-"),
                "log_file": log_name,
                "today_errors": int(log_stats.get("error_count", 0)),
                "today_warns": int(log_stats.get("warn_count", 0)),
            })

        freeze_state = get_freeze_status(logs_dir=self.logs_dir)
        any_frozen = any(bool(v) for v in freeze_state.values())

        feed_health: Optional[Dict[str, Any]] = None
        feed_status_path = os.path.join(self.logs_dir, "feed_status.json")
        if os.path.exists(feed_status_path):
            try:
                import json
                with open(feed_status_path, "r", encoding="utf-8") as f:
                    raw_fh = json.load(f)
                fh_date = datetime.fromtimestamp(float(raw_fh.get("timestamp") or 0.0), tz=IST).strftime("%Y-%m-%d")
                if fh_date == today_str:
                    feed_health = raw_fh
            except Exception:
                pass
        feed_stale = bool(
            feed_health
            and feed_health.get("stale")
            and alive_map.get("feed_handler", True)
        )

        reasons: List[str] = []
        if total_errors_today > 0 or dead_roles:
            overall = "RED"
            if total_errors_today > 0:
                reasons.append(f"{total_errors_today} ERROR/CRITICAL log line(s) detected today ({today_str})")
            if dead_roles:
                reasons.append(f"{len(dead_roles)} registered process(es) DEAD: {', '.join(dead_roles)}")
        elif total_warns_today > 0 or any_frozen or feed_stale:
            overall = "YELLOW"
            if total_warns_today > 0:
                reasons.append(f"{total_warns_today} WARN log line(s) detected today")
            if feed_stale:
                stale_cids = feed_health.get("stale_connections", []) if feed_health else []
                reasons.append(f"FeedHandler reports stale WebSocket connection(s): {stale_cids}")
            if any_frozen:
                frozen_modes = [k.upper() for k, v in freeze_state.items() if v and k != "all"]
                reasons.append(f"Execution frozen for: {', '.join(frozen_modes) or 'ALL'}")
        else:
            overall = "GREEN"
            reasons.append("All active processes healthy and zero errors logged today")

        recent_errors_sorted = sorted(recent_errors, key=lambda x: x.get("time", ""), reverse=True)[:15]
        infra = self._probe_ipc_and_shm(candle_manager_alive=alive_map.get("candle_manager", False))

        return {
            "date": today_str,
            "timestamp": datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "overall_status": overall,
            "reasons": reasons,
            "summary": {
                "registered_processes": len(processes),
                "running_processes": sum(1 for p in processes if p["alive"]),
                "dead_processes": len(dead_roles),
                "today_errors": total_errors_today,
                "today_warns": total_warns_today,
                "log_files_count": len(log_file_summaries),
            },
            "freeze_state": freeze_state,
            "feed_health": feed_health,
            "processes": processes,
            "ipc_sockets": infra["ipc_sockets"],
            "shm": infra["shm"],
            "log_files": log_file_summaries,
            "recent_errors": recent_errors_sorted,
        }

    def get_logs(
        self,
        file: Optional[str] = None,
        level: str = "ALL",
        query: str = "",
        date_str: Optional[str] = None,
        limit: int = 300,
    ) -> Dict[str, Any]:
        """Return day-filtered log lines across a selected log file or all log files."""
        today_str = date_str or datetime.now(tz=IST).strftime("%Y-%m-%d")
        lvl_filter = (level or "ALL").strip().upper()
        q_lower = (query or "").strip().lower()
        max_rows = max(1, min(2000, int(limit or 300)))

        target_files: List[str] = []
        if file and file.upper() != "ALL":
            safe_name = os.path.basename(file)
            for cand in [
                os.path.join(self.logs_dir, "exec_logs", safe_name),
                os.path.join(self.logs_dir, safe_name),
            ]:
                if os.path.exists(cand) and os.path.isfile(cand):
                    target_files.append(cand)
                    break
        else:
            target_files = self._list_log_files()

        all_entries: List[Dict[str, Any]] = []
        err_total = 0
        warn_total = 0
        info_total = 0

        for fpath in target_files:
            parsed = self._parse_today_log_file(fpath, today_str)
            err_total += parsed["error_count"]
            warn_total += parsed["warn_count"]
            info_total += parsed["info_count"]
            for entry in parsed["entries"]:
                if lvl_filter != "ALL" and entry["level"] != lvl_filter:
                    continue
                if q_lower:
                    hay = f"{entry['file']} {entry['logger']} {entry['message']}".lower()
                    if q_lower not in hay:
                        continue
                all_entries.append(entry)

        # Sort newest first when aggregating multiple files; keep reverse chronological order
        all_entries.sort(key=lambda e: (e.get("time", ""), e.get("line_no", 0)), reverse=True)
        sliced = all_entries[:max_rows]

        return {
            "date": today_str,
            "file": file or "ALL",
            "level": lvl_filter,
            "query": query,
            "counts": {
                "errors": err_total,
                "warns": warn_total,
                "infos": info_total,
                "matched": len(all_entries),
            },
            "entries": sliced,
        }
