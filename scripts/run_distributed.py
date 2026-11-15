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

PIDS_FILE = "logs/pids.json"


def load_pids() -> Dict[str, Any]:
    if not os.path.exists(PIDS_FILE):
        return {}
    try:
        with open(PIDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_pids(pids: Dict[str, Any]) -> None:
    os.makedirs("logs", exist_ok=True)
    with open(PIDS_FILE, "w", encoding="utf-8") as f:
        json.dump(pids, f, indent=2)


def register_process(role: str, pid: int, cmd: List[str]) -> None:
    pids = load_pids()
    pids[role] = {
        "pid": pid,
        "cmd": cmd,
        "started_at": datetime_now_iso(),
    }
    save_pids(pids)


def datetime_now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def unregister_pids(pids_to_remove: List[int]) -> None:
    """Remove specified PIDs from logs/pids.json without touching other registered processes."""
    pids = load_pids()
    pid_set = set(pids_to_remove)
    updated = {role: info for role, info in pids.items() if info.get("pid") not in pid_set}
    if not updated and os.path.exists(PIDS_FILE):
        try:
            os.remove(PIDS_FILE)
        except Exception:
            pass
    else:
        save_pids(updated)


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


def kill_all_registered() -> int:
    """Terminate all registered background processes cleanly."""
    pids = load_pids()
    killed = 0
    for role, info in list(pids.items()):
        pid = info.get("pid")
        if pid and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[SUPERVISOR] Sent SIGTERM to {role} (PID: {pid})")
                killed += 1
            except Exception as e:
                print(f"[SUPERVISOR] Error killing {role} (PID {pid}): {e}", file=sys.stderr)

    # Wait for processes to exit
    time.sleep(1.0)

    # Force kill if still alive
    for role, info in list(pids.items()):
        pid = info.get("pid")
        if pid and is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[SUPERVISOR] Sent SIGKILL to stubborn {role} (PID: {pid})")
            except Exception:
                pass

    if os.path.exists(PIDS_FILE):
        try:
            os.remove(PIDS_FILE)
        except Exception:
            pass
    return killed


def spawn_process(
    cmd: List[str],
    role: str,
    log_file: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.Popen:
    """Spawn child process and register PID."""
    os.makedirs("logs", exist_ok=True)
    custom_env = dict(os.environ)
    custom_env["PYTHONPATH"] = os.path.abspath(".")
    if env:
        custom_env.update(env)

    if log_file:
        stdout_dest = open(log_file, "a", encoding="utf-8")
        stderr_dest = subprocess.STDOUT
    else:
        stdout_dest = None
        stderr_dest = None

    proc = subprocess.Popen(
        cmd,
        stdout=stdout_dest,
        stderr=stderr_dest,
        env=custom_env,
        preexec_fn=os.setsid if hasattr(os, "setsid") else None,
    )
    register_process(role, proc.pid, cmd)
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
        print("\n[SUPERVISOR] Teardown signal received. Terminating child processes...")
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        while True:
            all_dead = True
            for p in procs:
                if p.poll() is None:
                    all_dead = False
                    break
            if all_dead:
                print("[SUPERVISOR] All supervised child processes have terminated.")
                cleanup()
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[SUPERVISOR] Interrupted by user. Terminating child processes...")
        cleanup()
    return 0
