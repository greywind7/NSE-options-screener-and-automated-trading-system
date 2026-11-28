"""
Distributed Process Orchestrator & Multi-Process Supervisor.
Manages PID tracking, subprocess spawning, and graceful process group teardown (CRIT-005).
"""
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from core.common.logging_utils import get_logger

logger = get_logger("SUPERVISOR")
PIDS_FILE = "logs/pids.json"


def load_pids(pids_file: str = PIDS_FILE) -> Dict[str, Any]:
    if not os.path.exists(pids_file):
        return {}
    try:
        with open(pids_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pids(pids: Dict[str, Any], pids_file: str = PIDS_FILE) -> None:
    parent = os.path.dirname(os.path.abspath(pids_file))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(pids_file, "w", encoding="utf-8") as f:
        json.dump(pids, f, indent=2)


def infer_process_tier(role: str) -> str:
    """Infer architectural tier for a registered process role."""
    r = (role or "").lower()
    if any(k in r for k in ("feed", "candle", "quotes", "rms", "recorder")):
        return "CORE"
    if "scoring" in r or "strategy" in r:
        return "STRATEGY"
    if r.startswith("exec_"):
        return "EXECUTION"
    return "SYSTEM"


def register_process(
    role: str,
    pid: int,
    cmd: List[str],
    log_file: Optional[str] = None,
    tier: Optional[str] = None,
    pids_file: str = PIDS_FILE,
) -> None:
    pids = load_pids(pids_file)
    pids[role] = {
        "pid": pid,
        "cmd": cmd,
        "log_file": log_file or f"logs/{role}.log",
        "tier": tier or infer_process_tier(role),
        "started_at": datetime_now_iso(),
    }
    save_pids(pids, pids_file)


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def unregister_pids(pids_to_remove: List[int], pids_file: str = PIDS_FILE) -> None:
    """Remove specified PIDs from pids_file without touching other registered processes."""
    pids = load_pids(pids_file)
    pid_set = set(pids_to_remove)
    updated = {role: info for role, info in pids.items() if info.get("pid") not in pid_set}
    if not updated and os.path.exists(pids_file):
        try:
            os.remove(pids_file)
        except Exception:
            pass
    else:
        save_pids(updated, pids_file)


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists and is running, but signal permission is restricted (macOS sandbox / different user)
        return True
    except ProcessLookupError:
        # Process does not exist (ESRCH)
        return False
    except OSError as e:
        import errno
        if e.errno == errno.EPERM:
            return True
        return False


def kill_roles(roles: Optional[List[str]] = None, pids_file: str = PIDS_FILE) -> int:
    """Terminate specified registered roles (or all registered roles if roles is None)."""
    pids = load_pids(pids_file)
    if not pids:
        return 0
    role_filter = set(roles) if roles is not None else None
    targets = [(role, info) for role, info in list(pids.items()) if role_filter is None or role in role_filter]
    if not targets:
        return 0

    killed = 0
    removed_pids: List[int] = []
    for role, info in targets:
        pid = info.get("pid")
        if pid:
            removed_pids.append(pid)
        if pid and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Sent SIGTERM to {role} (PID: {pid})")
                killed += 1
            except Exception as e:
                logger.error(f"Error killing {role} (PID {pid}): {e}")

    if killed > 0:
        time.sleep(0.5)

    for role, info in targets:
        pid = info.get("pid")
        if pid and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Sent SIGKILL to stubborn {role} (PID: {pid})")
            except Exception:
                pass

    if role_filter is None:
        if os.path.exists(pids_file):
            try:
                os.remove(pids_file)
            except Exception:
                pass
    else:
        remaining = {r: inf for r, inf in pids.items() if r not in role_filter}
        if not remaining and os.path.exists(pids_file):
            try:
                os.remove(pids_file)
            except Exception:
                pass
        else:
            save_pids(remaining, pids_file)
    return killed


def kill_all_registered(pids_file: str = PIDS_FILE) -> int:
    """Terminate all registered background processes cleanly."""
    return kill_roles(roles=None, pids_file=pids_file)


def spawn_process(
    cmd: List[str],
    role: str,
    log_file: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    tier: Optional[str] = None,
    pids_file: str = PIDS_FILE,
) -> subprocess.Popen:
    """Spawn child process with unbuffered output and register PID/tier metadata."""
    parent = os.path.dirname(os.path.abspath(pids_file))
    if parent:
        os.makedirs(parent, exist_ok=True)
    custom_env = dict(os.environ)
    custom_env["PYTHONPATH"] = os.path.abspath(".")
    custom_env["PYTHONUNBUFFERED"] = "1"
    if env:
        custom_env.update(env)

    resolved_log = log_file or f"logs/{role}.log"
    if resolved_log:
        os.makedirs(os.path.dirname(os.path.abspath(resolved_log)), exist_ok=True)
        stdout_dest = open(resolved_log, "a", encoding="utf-8")
        stderr_dest = subprocess.STDOUT
    else:
        stdout_dest = None
        stderr_dest = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_dest,
            stderr=stderr_dest,
            env=custom_env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
    finally:
        if stdout_dest is not None:
            stdout_dest.close()

    register_process(role, proc.pid, cmd, log_file=resolved_log, tier=tier, pids_file=pids_file)
    return proc


def supervise_processes(procs: List[subprocess.Popen]) -> int:
    """Supervise a list of active child processes until interrupted."""
    supervised_pids = [p.pid for p in procs if p and p.pid]

    def cleanup():
        for p in procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        time.sleep(0.5)
        for p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
        unregister_pids(supervised_pids)

    def sig_handler(sig, frame):
        logger.info("Teardown signal received. Terminating child processes...")
        cleanup()
        sys.exit(0)

    def fwd_usr_signal(sig, frame):
        for p in procs:
            if p and p.poll() is None:
                try:
                    os.kill(p.pid, sig)
                except Exception:
                    pass

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, fwd_usr_signal)
    if hasattr(signal, "SIGUSR2"):
        signal.signal(signal.SIGUSR2, fwd_usr_signal)

    try:
        while True:
            all_dead = True
            for p in procs:
                if p.poll() is None:
                    all_dead = False
                    break
            if all_dead:
                logger.info("All supervised child processes have terminated.")
                cleanup()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Terminating child processes...")
        cleanup()
    return 0
