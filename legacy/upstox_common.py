#!/usr/bin/env python3
"""
upstox_common.py
================
Shared utilities for Upstox API v2/v3 interactions:
- Automatic .env file loader (pure Python, zero dependencies).
- Robust HTTP client with rate-limit backoff (HTTP 429).
- Instrument file parser (loads .csv, .json, or .txt keys produced by filter_instruments.py).
"""

from __future__ import annotations

import csv
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
    """Resolve file path by checking given path, fallback directory, and base filename."""
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


def log_debug(message: str, debug_file: Optional[str] = "debug.log") -> None:
    """Append a debug message with timestamp to the specified debug log file."""
    if not debug_file:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        with open(debug_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] {message}\n")
    except Exception:
        pass


def load_env(env_path: str = ".env") -> Dict[str, str]:
    """
    Load environment variables from a .env file into os.environ.
    Does not require external packages like python-dotenv.
    """
    env_vars: Dict[str, str] = {}
    if not os.path.exists(env_path):
        return env_vars

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Ignore empty lines and comments
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env_vars[key] = val
                # Set in current os.environ if not already defined
                if key not in os.environ:
                    os.environ[key] = val
    return env_vars


def get_access_token() -> str:
    """
    Retrieve UPSTOX_ACCESS_TOKEN from environment or .env file.
    Raises ValueError with clear instructions if missing or placeholder.
    """
    load_env(".env")
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token or token == "your_access_token_here":
        print("\n" + "=" * 65, file=sys.stderr)
        print("ERROR: UPSTOX_ACCESS_TOKEN is missing or not set in .env!", file=sys.stderr)
        print("Please edit your '.env' file and paste your valid access token:", file=sys.stderr)
        print("  UPSTOX_ACCESS_TOKEN=your_token_here", file=sys.stderr)
        print("=" * 65 + "\n", file=sys.stderr)
        sys.exit(1)
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
    Execute an HTTP GET request against Upstox API with automatic rate-limit backoff.
    Optionally dumps raw requests and responses to debug_file if provided.
    """
    masked_token = access_token[:8] + "..." + access_token[-4:] if len(access_token) > 12 else "***"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "Upstox-RVOL-Scanner/1.0",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")

    if debug_file:
        log_debug(f"REQUEST -> GET {url} | Auth: Bearer {masked_token}", debug_file)

    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                if debug_file:
                    log_debug(f"RESPONSE [{response.status}] <- {url}\nRAW BODY:\n{payload}\n{'-'*60}", debug_file)
                return json.loads(payload)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if debug_file:
                log_debug(f"HTTP ERROR [{e.code}] for {url}: {e.reason}\nERROR BODY:\n{error_body}\n{'-'*60}", debug_file)

            # 429 Too Many Requests -> Rate limit backoff
            if e.code == 429 and attempt < max_retries - 1:
                sleep_time = backoff_factor * (2 ** attempt)
                # Check for Retry-After header
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_time = float(retry_after)
                    except ValueError:
                        pass
                if debug_file:
                    log_debug(f"Rate limited (429). Retrying in {sleep_time:.1f}s (Attempt {attempt+1}/{max_retries})", debug_file)
                time.sleep(sleep_time)
                continue

            raise RuntimeError(f"HTTP {e.code} Error for {url}: {e.reason}. Body: {error_body}") from e
        except urllib.error.URLError as e:
            if debug_file:
                log_debug(f"CONNECTION ERROR for {url}: {e.reason} (Attempt {attempt+1}/{max_retries})", debug_file)
            if attempt < max_retries - 1:
                time.sleep(backoff_factor * (attempt + 1))
                continue
            raise RuntimeError(f"Connection failed for {url}: {e.reason}") from e

    raise RuntimeError(f"Failed to fetch {url} after {max_retries} retries.")


def load_input_instruments(file_path: str) -> List[Dict[str, Any]]:
    """
    Load instruments from a file generated by filter_instruments.py (.csv, .json, or .txt).
    Returns a list of dicts with at least 'upstox_instrument_key' and descriptive fields.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Input file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    instruments: List[Dict[str, Any]] = []

    if ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        instruments.append({"upstox_instrument_key": item.strip()})
                    elif isinstance(item, dict):
                        instruments.append(item)
    elif ext == ".csv":
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                instruments.append(dict(row))
    else:
        # Plain text file with one key per line
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    instruments.append({"upstox_instrument_key": line})

_MASTER_CACHE: Dict[str, Any] = {}

def get_master_lookup(master_csv_path: str = "instrument.csv") -> Dict[str, Any]:
    """
    Build a fast in-memory lookup table for Cash Equities, Indices, and Futures from instrument.csv.
    """
    if "cash_eq" in _MASTER_CACHE:
        return _MASTER_CACHE

    cash_eq: Dict[Tuple[str, str], Dict[str, Any]] = {}
    indices: Dict[Tuple[str, str], Dict[str, Any]] = {}
    near_futures: Dict[Tuple[str, str], Dict[str, Any]] = {}

    if not os.path.exists(master_csv_path):
        return {"cash_eq": cash_eq, "indices": indices, "near_futures": near_futures}

    with open(master_csv_path, mode="r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            exch = (row.get("exchange") or "NSE").strip().upper()
            seg = (row.get("segment") or "").strip().upper()
            itype = (row.get("instrument_type") or "").strip().upper()
            sym = (row.get("trading_symbol") or "").strip().upper()
            und = (row.get("underlying_symbol") or "").strip().upper()
            exp = (row.get("expiry_date") or "").strip()

            if seg == "CASH" and itype == "EQ":
                cash_eq[(exch, sym)] = row
                cash_eq[("", sym)] = row
            elif seg == "CASH" and itype == "IDX":
                indices[(exch, sym)] = row
                indices[("", sym)] = row
            elif seg == "FNO" and itype == "FUT" and und:
                # Track nearest expiry future for each underlying
                curr = near_futures.get((exch, und))
                if not curr or (exp and exp < curr.get("expiry_date", "9999-99-99")):
                    near_futures[(exch, und)] = row
                    near_futures[("", und)] = row

    _MASTER_CACHE["cash_eq"] = cash_eq
    _MASTER_CACHE["indices"] = indices
    _MASTER_CACHE["near_futures"] = near_futures
    return _MASTER_CACHE


def load_input_instruments(
    file_path: str,
    target_segment: str = "CASH",
    master_csv_path: str = "instrument.csv",
) -> List[Dict[str, Any]]:
    """
    Load instruments from a file generated by filter_instruments.py (.csv, .json, or .txt).
    If the file contains underlying symbols (like an FNO underlyings list) without direct tokens,
    it automatically resolves them against instrument.csv to the requested target_segment
    (e.g. 'CASH' for stock equity, or 'FUT' for near-month futures).
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Input file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    instruments: List[Dict[str, Any]] = []

    if ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        instruments.append({"upstox_instrument_key": item.strip()})
                    elif isinstance(item, dict):
                        instruments.append(item)
    elif ext == ".csv":
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                instruments.append(dict(row))
    else:
        # Plain text file with one key per line
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    instruments.append({"upstox_instrument_key": line})

    # Prepare master lookup if needed for token resolution
    master = get_master_lookup(master_csv_path)
    target_seg = target_segment.strip().upper() if target_segment else "CASH"

    # Ensure every entry has a valid 'upstox_instrument_key'
    valid: List[Dict[str, Any]] = []
    for item in instruments:
        key = item.get("upstox_instrument_key")
        exch = (item.get("exchange") or "NSE").strip().upper()
        seg = (item.get("segment") or "").strip().upper()
        token = item.get("exchange_token") or item.get("token")
        isin = item.get("isin")
        sym = item.get("trading_symbol")
        und = item.get("underlying_symbol") or sym

        if not key:
            # 1. Check if token and isin are already in the row
            if seg == "CASH" and isin:
                key = f"{exch}_EQ|{isin}"
            elif seg == "FNO" and token:
                key = f"{exch}_FO|{token}"
            elif seg == "COMMODITY" and token:
                key = f"MCX_FO|{token}"

            # 2. If token is missing, resolve underlying symbol against master dataset!
            if not key and und:
                und_clean = str(und).strip().upper()
                if target_seg in ("CASH", "EQ"):
                    # Resolve to Cash Equity
                    eq_row = master["cash_eq"].get((exch, und_clean)) or master["cash_eq"].get(("", und_clean))
                    if eq_row:
                        eq_isin = eq_row.get("isin")
                        eq_token = eq_row.get("exchange_token")
                        key = f"{exch}_EQ|{eq_isin}" if eq_isin else f"{exch}_EQ|{eq_token}"
                        item["trading_symbol"] = eq_row.get("trading_symbol")
                        item["name"] = eq_row.get("name") or item.get("name")
                        item["isin"] = eq_isin
                        item["exchange_token"] = eq_token
                        item["segment"] = "CASH"
                        item["instrument_type"] = "EQ"
                    else:
                        # Index resolution (NIFTY, BANKNIFTY, etc.)
                        idx_map = {
                            "NIFTY": "NSE_INDEX|Nifty 50",
                            "BANKNIFTY": "NSE_INDEX|Nifty Bank",
                            "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
                            "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
                            "SENSEX": "BSE_INDEX|SENSEX",
                            "BANKEX": "BSE_INDEX|BANKEX",
                        }
                        if und_clean in idx_map:
                            key = idx_map[und_clean]
                        else:
                            idx_row = master["indices"].get((exch, und_clean)) or master["indices"].get(("", und_clean))
                            if idx_row:
                                key = f"{exch}_INDEX|{idx_row.get('name') or und_clean}"
                elif target_seg in ("FUT", "FNO"):
                    # Resolve to near-month future contract
                    fut_row = master["near_futures"].get((exch, und_clean)) or master["near_futures"].get(("", und_clean))
                    if fut_row:
                        fut_token = fut_row.get("exchange_token")
                        key = f"{exch}_FO|{fut_token}"
                        item["trading_symbol"] = fut_row.get("trading_symbol")
                        item["name"] = fut_row.get("name") or item.get("name")
                        item["exchange_token"] = fut_token
                        item["expiry_date"] = fut_row.get("expiry_date")
                        item["segment"] = "FNO"
                        item["instrument_type"] = "FUT"

        if key:
            item["upstox_instrument_key"] = key
            valid.append(item)

    return valid
