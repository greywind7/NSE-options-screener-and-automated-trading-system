#!/usr/bin/env python3
"""
infrastructure/auth/upstox_auth.py
==================================
Upstox Authentication & Robust HTTP Client (Pure Python, Zero Dependencies):
1. Loads environment variables from .env into os.environ.
2. Manages UPSTOX_ACCESS_TOKEN (for Live Market Data & Websocket Ingestion).
3. Manages UPSTOX_SANDBOX_ACCESS_TOKEN (for Upstox Sandbox Order Execution).
4. HTTP GET and POST clients with exponential rate-limit backoff (HTTP 429)
   and retry handling.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union


def safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to int, handling None, null, floats, strings, and errors."""
    if val is None:
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float, handling None, null, strings, and errors."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def resolve_file_path(filename: str, fallback_dir: str = "data") -> str:
    """Resolve file path checking path, fallback dir, and basename."""
    if not filename:
        return filename
    if os.path.exists(filename):
        return filename
    cand = os.path.join(fallback_dir, os.path.basename(filename))
    if os.path.exists(cand):
        return cand
    cand2 = os.path.join(fallback_dir, filename)
    if os.path.exists(cand2):
        return cand2
    return filename


_debug_log_counter = 0


def prune_debug_log(
    debug_file: str = "logs/exec_logs/debug.log",
    max_bytes: int = 20 * 1024 * 1024,   # 20 MB max
    retain_bytes: int = 2 * 1024 * 1024,  # retain last 2 MB
) -> bool:
    """
    Truncate debug log file to the last retain_bytes if it exceeds max_bytes.
    Performs fast reverse seek without loading large files into memory.
    """
    if not debug_file or not os.path.exists(debug_file):
        return False
    try:
        sz = os.path.getsize(debug_file)
        if sz <= max_bytes:
            return False

        with open(debug_file, "rb") as f:
            f.seek(-min(retain_bytes, sz), os.SEEK_END)
            tail = f.read()

        # Find first clean newline so we don't start with partial log line
        nl = tail.find(b"\n")
        if nl != -1 and nl + 1 < len(tail):
            tail = tail[nl + 1:]

        with open(debug_file, "wb") as f:
            f.write(tail)
        return True
    except Exception:
        return False


def log_debug(message: str, debug_file: Optional[str] = "logs/exec_logs/debug.log") -> None:
    """Append a debug message with timestamp to log file with periodic auto-pruning."""
    global _debug_log_counter
    if not debug_file:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(debug_file)), exist_ok=True)
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] {message}\n")
        _debug_log_counter += 1
        if _debug_log_counter % 1000 == 0:
            prune_debug_log(debug_file)
    except Exception:
        pass


def load_env(env_path: str = ".env") -> Dict[str, str]:
    """Load environment variables from .env file into os.environ."""
    env_vars: Dict[str, str] = {}
    if not os.path.exists(env_path):
        return env_vars

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env_vars[key] = val
                if key not in os.environ:
                    os.environ[key] = val
    return env_vars


def get_access_token() -> str:
    """
    Retrieve UPSTOX_ACCESS_TOKEN (for Live Feeds / Market Data).
    """
    load_env(".env")
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    return token


def get_sandbox_token() -> str:
    """
    Retrieve UPSTOX_SANDBOX_ACCESS_TOKEN (for Upstox Sandbox Order Execution).
    Must be generated from an Upstox Sandbox Developer App.
    """
    load_env(".env")
    token = os.environ.get("UPSTOX_SANDBOX_ACCESS_TOKEN", "").strip()
    if token == "your_sandbox_access_token_here":
        return ""
    return token


def http_get_json(
    url: str,
    access_token: str,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    timeout: int = 20,
    debug_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute HTTP GET request against Upstox API with rate-limit backoff (HTTP 429).
    """
    masked_token = access_token[:8] + "..." + access_token[-4:] if len(access_token) > 12 else "***"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Upstox-Algo-Toolkit/2.0",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")

    if debug_file:
        log_debug(f"REQUEST -> GET {url} | Auth: Bearer {masked_token}", debug_file)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                if debug_file:
                    log_debug(f"RESPONSE [{response.status}] <- {url}\n{payload}", debug_file)
                return json.loads(payload)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if debug_file:
                log_debug(f"HTTP ERROR [{e.code}] for {url}: {e.reason}\nBODY: {error_body}", debug_file)

            if e.code == 429 and attempt < max_retries - 1:
                sleep_time = backoff_factor * (2 ** attempt)
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_time = max(sleep_time, float(retry_after))
                    except ValueError:
                        pass
                time.sleep(sleep_time)
                continue

            return {"status": "error", "code": e.code, "message": str(e.reason), "body": error_body}
        except Exception as ex:
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            return {"status": "error", "message": str(ex)}

    return {"status": "error", "message": "Max retries exceeded"}


def http_post_json(
    url: str,
    payload_dict: Dict[str, Any],
    access_token: str,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    timeout: int = 20,
    debug_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute HTTP POST request against Upstox API with JSON payload and 429 backoff.
    """
    masked_token = access_token[:8] + "..." + access_token[-4:] if len(access_token) > 12 else "***"
    data_bytes = json.dumps(payload_dict).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Upstox-Algo-Toolkit/2.0",
    }
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

    if debug_file:
        log_debug(f"REQUEST -> POST {url} | Body: {json.dumps(payload_dict)} | Auth: Bearer {masked_token}", debug_file)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_payload = response.read().decode("utf-8")
                if debug_file:
                    log_debug(f"RESPONSE [{response.status}] <- {url}\n{res_payload}", debug_file)
                return json.loads(res_payload)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if debug_file:
                log_debug(f"HTTP ERROR [{e.code}] for POST {url}: {e.reason}\nBODY: {error_body}", debug_file)

            if e.code == 429 and attempt < max_retries - 1:
                sleep_time = backoff_factor * (2 ** attempt)
                time.sleep(sleep_time)
                continue

            return {"status": "error", "code": e.code, "message": str(e.reason), "body": error_body}
        except Exception as ex:
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            return {"status": "error", "message": str(ex)}

    return {"status": "error", "message": "Max retries exceeded"}


def http_put_json(
    url: str,
    payload_dict: Dict[str, Any],
    access_token: str,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    timeout: int = 20,
    debug_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute HTTP PUT request against Upstox API with JSON payload and 429 backoff (v3 Order Modify).
    """
    masked_token = access_token[:8] + "..." + access_token[-4:] if len(access_token) > 12 else "***"
    data_bytes = json.dumps(payload_dict).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Upstox-Algo-Toolkit/2.0",
    }
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="PUT")

    if debug_file:
        log_debug(f"REQUEST -> PUT {url} | Body: {json.dumps(payload_dict)} | Auth: Bearer {masked_token}", debug_file)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_payload = response.read().decode("utf-8")
                if debug_file:
                    log_debug(f"RESPONSE [{response.status}] <- {url}\n{res_payload}", debug_file)
                return json.loads(res_payload)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if debug_file:
                log_debug(f"HTTP ERROR [{e.code}] for PUT {url}: {e.reason}\nBODY: {error_body}", debug_file)

            if e.code == 429 and attempt < max_retries - 1:
                sleep_time = backoff_factor * (2 ** attempt)
                time.sleep(sleep_time)
                continue

            return {"status": "error", "code": e.code, "message": str(e.reason), "body": error_body}
        except Exception as ex:
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            return {"status": "error", "message": str(ex)}

    return {"status": "error", "message": "Max retries exceeded"}


def http_delete_json(
    url: str,
    access_token: str,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    timeout: int = 20,
    debug_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute HTTP DELETE request against Upstox API with 429 backoff (v3 Order Cancel).
    """
    masked_token = access_token[:8] + "..." + access_token[-4:] if len(access_token) > 12 else "***"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Upstox-Algo-Toolkit/2.0",
    }
    req = urllib.request.Request(url, headers=headers, method="DELETE")

    if debug_file:
        log_debug(f"REQUEST -> DELETE {url} | Auth: Bearer {masked_token}", debug_file)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_payload = response.read().decode("utf-8")
                if debug_file:
                    log_debug(f"RESPONSE [{response.status}] <- {url}\n{res_payload}", debug_file)
                return json.loads(res_payload)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if debug_file:
                log_debug(f"HTTP ERROR [{e.code}] for DELETE {url}: {e.reason}\nBODY: {error_body}", debug_file)

            if e.code == 429 and attempt < max_retries - 1:
                sleep_time = backoff_factor * (2 ** attempt)
                time.sleep(sleep_time)
                continue

            return {"status": "error", "code": e.code, "message": str(e.reason), "body": error_body}
        except Exception as ex:
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (2 ** attempt))
                continue
            return {"status": "error", "message": str(ex)}

    return {"status": "error", "message": "Max retries exceeded"}
