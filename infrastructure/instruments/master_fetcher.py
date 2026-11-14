#!/usr/bin/env python3
"""
infrastructure/instruments/master_fetcher.py
============================================
Retrieves and validates the authoritative Upstox / Indian Exchanges Instrument Master.

If not present locally or if force_download is requested, it downloads the master database
from Upstox's public CDN:
    https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz
or checks local fallback paths in data/instrument.csv.
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import urllib.request
from datetime import datetime
from typing import Optional


UPSTOX_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"


def run_master_fetcher(
    force_download: bool = False,
    target_csv: str = "data/instrument.csv",
    timeout_sec: int = 30,
) -> Optional[str]:
    """
    Ensure the instrument master CSV is present and non-empty.
    Returns the path to the verified CSV file, or None on failure.
    """
    os.makedirs(os.path.dirname(os.path.abspath(target_csv)), exist_ok=True)

    # 1. If file already exists and force_download is False, validate it
    if os.path.exists(target_csv) and not force_download:
        sz = os.path.getsize(target_csv)
        if sz > 100_000:  # Must be at least 100KB to be a valid instrument master
            print(f"  ✓ Found existing Instrument Master at '{target_csv}' ({sz / (1024 * 1024):.2f} MB)")
            return target_csv
        else:
            print(f"  ⚠️ Existing master file '{target_csv}' is incomplete or truncated ({sz} bytes). Re-downloading...")

    # 2. Download from Upstox CDN
    gz_temp_path = f"{target_csv}.gz.tmp"
    csv_temp_path = f"{target_csv}.tmp"

    print(f"  ⬇️ Downloading fresh instrument master from {UPSTOX_MASTER_URL}...")
    try:
        req = urllib.request.Request(
            UPSTOX_MASTER_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; UpstoxAlgoToolkit/2.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as response, open(gz_temp_path, "wb") as out_gz:
            shutil.copyfileobj(response, out_gz)

        print("  📦 Decompressing instrument master archive...")
        with gzip.open(gz_temp_path, "rb") as f_in, open(csv_temp_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        # Replace target file atomically
        if os.path.exists(target_csv):
            os.remove(target_csv)
        os.rename(csv_temp_path, target_csv)

        if os.path.exists(gz_temp_path):
            os.remove(gz_temp_path)

        final_sz = os.path.getsize(target_csv)
        print(f"  ✓ Downloaded and saved Instrument Master to '{target_csv}' ({final_sz / (1024 * 1024):.2f} MB)")
        return target_csv

    except Exception as e:
        print(f"  ⚠️ Warning: Failed to download from Upstox CDN: {e}", file=sys.stderr)
        # Check if an existing copy is available
        if os.path.exists(target_csv) and os.path.getsize(target_csv) > 0:
            print(f"  ✓ Falling back to existing local copy at '{target_csv}'")
            return target_csv
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch Upstox Instrument Master Database")
    parser.add_argument("--force", action="store_true", help="Force fresh download from CDN")
    parser.add_argument("--output", default="data/instrument.csv", help="Target CSV file path")
    args = parser.parse_args()

    res = run_master_fetcher(force_download=args.force, target_csv=args.output)
    if not res:
        sys.exit(1)


if __name__ == "__main__":
    main()

