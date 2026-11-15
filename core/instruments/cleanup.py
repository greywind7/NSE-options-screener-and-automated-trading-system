"""
infrastructure/instruments/cleanup.py
Pre-market and Post-market preparation document maintenance utility.

Safely prunes stale dated instrument files, filtered universe files, and subscription manifests
from prior trading sessions while strictly preserving historical volume baseline index files
(index_YYYYMMDD.json) required for backtesting, relative volume calculations, and historical simulations.
"""

import os
import re
import sys
from datetime import datetime
from typing import List, Optional, Tuple


def prune_old_preparation_files(
    keep_session_date: Optional[str] = None,
    data_dir: str = "data",
    silent: bool = False,
) -> List[Tuple[str, float]]:
    """
    Remove stale dated preparation files older than keep_session_date.

    Pruned files (if date < keep_session_date):
      - instrument_YYYYMMDD.csv
      - filtered_YYYYMMDD.csv
      - filtered_mcx_YYYYMMDD.csv
      - subscription_manifest_YYYYMMDD.json
      - subscription_manifest_mcx_YYYYMMDD.json

    STRICTLY PRESERVED (Never pruned):
      - Canonical files: instrument.csv, filtered.csv, filtered_mcx.csv,
        subscription_manifest.json, subscription_manifest_mcx.json, index.json, index_mcx.json
      - ALL volume baseline indices: index_YYYYMMDD.json, index_mcx_YYYYMMDD.json
        (Required for 30-day baseline RVOL calculations and historical simulations)
      - Current session date files (session_date >= keep_session_date)

    Returns:
        List of tuples: [(pruned_file_path, size_in_bytes), ...]
    """
    if not os.path.exists(data_dir):
        return []

    target_date = keep_session_date or datetime.now().strftime("%Y%m%d")

    # Regex patterns for files eligible for retirement
    prunable_patterns = [
        re.compile(r"^instrument_(\d{8})\.csv$"),
        re.compile(r"^filtered_(\d{8})\.csv$"),
        re.compile(r"^filtered_mcx_(\d{8})\.csv$"),
        re.compile(r"^subscription_manifest_(\d{8})\.json$"),
        re.compile(r"^subscription_manifest_mcx_(\d{8})\.json$"),
    ]

    pruned: List[Tuple[str, float]] = []

    try:
        entries = sorted(os.listdir(data_dir))
    except Exception as e:
        if not silent:
            print(f"⚠️ Error reading directory '{data_dir}': {e}", file=sys.stderr)
        return []

    for fname in entries:
        # STRICT IMMUNITY 1: Volume index files are NEVER scrapped
        if "index" in fname.lower():
            continue

        # STRICT IMMUNITY 2: Canonical un-dated reference files are NEVER touched
        if fname in (
            "instrument.csv",
            "filtered.csv",
            "filtered_mcx.csv",
            "subscription_manifest.json",
            "subscription_manifest_mcx.json",
            "index.json",
            "index_mcx.json",
        ):
            continue

        # Check against prunable patterns
        for pattern in prunable_patterns:
            match = pattern.match(fname)
            if match:
                f_date = match.group(1)
                # Retire if strictly older than keep_session_date
                if f_date < target_date:
                    fpath = os.path.join(data_dir, fname)
                    try:
                        sz = os.path.getsize(fpath)
                        os.remove(fpath)
                        pruned.append((fpath, sz))
                    except Exception as err:
                        if not silent:
                            print(f"⚠️ Failed to remove stale file '{fpath}': {err}", file=sys.stderr)
                break

    if pruned and not silent:
        total_mb = sum(sz for _, sz in pruned) / (1024 * 1024)
        print(f"🧹 [MAINTENANCE] Pruned {len(pruned)} stale preparation file(s) older than {target_date} ({total_mb:.2f} MB freed).")
        for fpath, sz in pruned:
            print(f"   - Removed: {fpath} ({sz / (1024 * 1024):.2f} MB)")

    return pruned

