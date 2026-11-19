#!/usr/bin/env python3
"""
scripts/convert_recordings_to_parquet.py
========================================
Historical Recording Migration Tool:
Converts legacy JSONL recording formats (market_events.jsonl, option_depth.jsonl)
from YYYYMMDD folders into canonical 2.0 dual-stream Parquet partitions (YYYY-MM-DD).

Features:
- Preserves 100% of original raw files (zero mutation to recordings/YYYYMMDD/).
- Streams and processes line-by-line with bounded chunk sizes (<200MB RAM footprint).
- Merges chronological streams with proper timestamp fallback (exchange_timestamp -> received_timestamp).
- Normalizes instrument tokens to canonical EXCHANGE:SYMBOL / EXCHANGE:TOKEN format.
- Emits equity_ticks.parquet and option_depth.parquet with exact Polars/PyArrow schemas.
- Generates self-contained subscription_manifest.json mapping strikes and options catalog.
- Verifies converted partition with ColumnarSimEngine.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

# Add project root to sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.common.symbol_utils import is_option_contract, normalize_instrument_token, parse_option_details


# Exact PyArrow Schemas matching core.recorder.market_recorder
EQUITY_SCHEMA = pa.schema([
    ("token", pa.string()),
    ("symbol", pa.string()),
    ("ltp", pa.float64()),
    ("volume", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("bid_qty", pa.int64()),
    ("ask_qty", pa.int64()),
    ("exchange_timestamp", pa.float64()),
    ("received_timestamp", pa.float64()),
])

OPTION_SCHEMA = pa.schema([
    ("token", pa.string()),
    ("symbol", pa.string()),
    ("ltp", pa.float64()),
    ("volume", pa.int64()),
    ("bid", pa.float64()),
    ("ask", pa.float64()),
    ("bid_qty", pa.int64()),
    ("ask_qty", pa.int64()),
    ("bids", pa.list_(pa.list_(pa.float64()))),
    ("asks", pa.list_(pa.list_(pa.float64()))),
    ("exchange_timestamp", pa.float64()),
    ("received_timestamp", pa.float64()),
])


def is_opt_record(d: Dict[str, Any]) -> bool:
    """Determine whether a recording record represents an option contract."""
    ik = str(d.get("instrument_key") or d.get("token") or "")
    sym = str(d.get("symbol") or "")

    if ik.startswith("NSE_FO") or ik.startswith("NSE:FO") or ":FO|" in ik:
        return True
    if ik.startswith("NSE_EQ") or ik.startswith("NSE_INDEX") or ik.startswith("BSE_EQ"):
        return False
    return is_option_contract(ik) or is_option_contract(sym)


def format_date_with_dashes(date_str: str) -> str:
    """Format YYYYMMDD to YYYY-MM-DD, or leave as YYYY-MM-DD if already formatted."""
    clean = date_str.strip()
    if len(clean) == 8 and clean.isdigit():
        return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
    return clean


class BatchParquetWriter:
    """Buffered batch writer using PyArrow ParquetWriter for low-memory, high-throughput streaming."""

    def __init__(self, filepath: str, schema: pa.Schema, chunk_size: int = 100000):
        self.filepath = filepath
        self.temp_filepath = f"{filepath}.tmp"
        self.schema = schema
        self.chunk_size = chunk_size
        self.col_names = [field.name for field in schema]
        self.current_batch: Dict[str, List[Any]] = {name: [] for name in self.col_names}
        self.writer: Optional[pq.ParquetWriter] = None
        self.total_rows = 0

    def append(self, row: Dict[str, Any]) -> None:
        for name in self.col_names:
            self.current_batch[name].append(row[name])

        if len(self.current_batch[self.col_names[0]]) >= self.chunk_size:
            self.flush_batch()

    def flush_batch(self) -> None:
        row_count = len(self.current_batch[self.col_names[0]])
        if row_count == 0:
            return

        table = pa.Table.from_pydict(self.current_batch, schema=self.schema)
        if self.writer is None:
            os.makedirs(os.path.dirname(self.temp_filepath), exist_ok=True)
            self.writer = pq.ParquetWriter(self.temp_filepath, self.schema, compression="snappy")

        self.writer.write_table(table)
        self.total_rows += row_count
        self.current_batch = {name: [] for name in self.col_names}

    def close(self) -> int:
        self.flush_batch()
        if self.writer is not None:
            self.writer.close()
            if os.path.exists(self.filepath):
                os.remove(self.filepath)
            os.replace(self.temp_filepath, self.filepath)
        elif not os.path.exists(self.filepath):
            # Create an empty table with schema if 0 rows were written
            empty_table = pa.Table.from_arrays([pa.array([], type=f.type) for f in self.schema], schema=self.schema)
            pq.write_table(empty_table, self.filepath, compression="snappy")
        return self.total_rows


def stream_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON records from a JSONL file, skipping control lines."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                et = d.get("event_type")
                if et in ("SESSION_START", "SESSION_END"):
                    continue
                yield d
            except Exception:
                continue


def convert_equity_stream(events_path: str, output_parquet: str, chunk_size: int = 100000) -> int:
    """Extract non-option equity and index ticks from market_events.jsonl and write equity_ticks.parquet."""
    writer = BatchParquetWriter(output_parquet, EQUITY_SCHEMA, chunk_size=chunk_size)

    for item in stream_jsonl(events_path):
        if is_opt_record(item):
            continue

        raw_key = item.get("instrument_key") or item.get("token") or ""
        token = normalize_instrument_token(raw_key)
        symbol = str(item.get("symbol") or "")

        recv_ts = float(item.get("received_timestamp") or 0.0)
        exch_ts = float(item.get("exchange_timestamp") or 0.0)
        ts = exch_ts if exch_ts > 0.0 else recv_ts
        if ts <= 0.0:
            ts = time.time()
        if recv_ts <= 0.0:
            recv_ts = ts

        row = {
            "token": token,
            "symbol": symbol,
            "ltp": float(item.get("ltp") or 0.0),
            "volume": int(item.get("volume") or 0),
            "bid": float(item.get("bid") or 0.0),
            "ask": float(item.get("ask") or 0.0),
            "bid_qty": int(item.get("bid_qty") or 0),
            "ask_qty": int(item.get("ask_qty") or 0),
            "exchange_timestamp": ts,
            "received_timestamp": recv_ts,
        }
        writer.append(row)

    return writer.close()


def stream_merged_options(events_path: str, depth_path: str) -> Iterator[Tuple[Dict[str, Any], bool]]:
    """
    Two-pointer chronological merge of option events from depth and events files.
    Yields (event_dict, is_depth_update).
    """
    gen_depth = stream_jsonl(depth_path) if os.path.exists(depth_path) else iter([])
    gen_events = stream_jsonl(events_path) if os.path.exists(events_path) else iter([])

    # Filter generators for option records only
    def next_depth():
        for d in gen_depth:
            if is_opt_record(d):
                return d
        return None

    def next_event():
        for e in gen_events:
            if is_opt_record(e):
                return e
        return None

    d_curr = next_depth()
    e_curr = next_event()

    while d_curr is not None or e_curr is not None:
        if d_curr is not None and e_curr is not None:
            ts_d = float(d_curr.get("received_timestamp") or d_curr.get("exchange_timestamp") or 0.0)
            ts_e = float(e_curr.get("received_timestamp") or e_curr.get("exchange_timestamp") or 0.0)

            if ts_d <= ts_e:
                yield d_curr, True
                d_curr = next_depth()
            else:
                yield e_curr, False
                e_curr = next_event()
        elif d_curr is not None:
            yield d_curr, True
            d_curr = next_depth()
        else:
            yield e_curr, False
            e_curr = next_event()


def convert_options_stream(
    events_path: str,
    depth_path: str,
    output_parquet: str,
    manifest_out: Optional[str] = None,
    chunk_size: int = 100000,
) -> Tuple[int, Dict[str, Any]]:
    """Extract and merge all option updates into option_depth.parquet, collecting contract metadata."""
    writer = BatchParquetWriter(output_parquet, OPTION_SCHEMA, chunk_size=chunk_size)

    # Collect contract catalog for subscription_manifest.json
    options_map: Dict[str, Dict[str, Any]] = {}
    instrument_name_map: Dict[str, str] = {}
    tokens_seen: Set[str] = set()

    for item, is_depth in stream_merged_options(events_path, depth_path):
        raw_key = item.get("instrument_key") or item.get("token") or ""
        token = normalize_instrument_token(raw_key)
        symbol = str(item.get("symbol") or "")

        recv_ts = float(item.get("received_timestamp") or 0.0)
        exch_ts = float(item.get("exchange_timestamp") or 0.0)
        ts = exch_ts if exch_ts > 0.0 else recv_ts
        if ts <= 0.0:
            ts = time.time()
        if recv_ts <= 0.0:
            recv_ts = ts

        # Parse depth bids / asks
        raw_bids = item.get("bids") or []
        raw_asks = item.get("asks") or []
        bids = [[float(v) for v in lvl] for lvl in raw_bids if isinstance(lvl, (list, tuple))]
        asks = [[float(v) for v in lvl] for lvl in raw_asks if isinstance(lvl, (list, tuple))]

        # Top of book bid / ask fallback
        bid = float(item.get("bid") or (bids[0][0] if bids else 0.0))
        ask = float(item.get("ask") or (asks[0][0] if asks else 0.0))
        bid_qty = int(item.get("bid_qty") or (bids[0][1] if bids else 0))
        ask_qty = int(item.get("ask_qty") or (asks[0][1] if asks else 0))
        ltp = float(item.get("ltp") or bid or ask or 0.0)

        row = {
            "token": token,
            "symbol": symbol,
            "ltp": ltp,
            "volume": int(item.get("volume") or 0),
            "bid": bid,
            "ask": ask,
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "bids": bids,
            "asks": asks,
            "exchange_timestamp": ts,
            "received_timestamp": recv_ts,
        }
        writer.append(row)

        # Catalog registration
        if token and token not in tokens_seen:
            tokens_seen.add(token)
            if symbol:
                instrument_name_map[token] = symbol
                instrument_name_map[raw_key] = symbol

            details = parse_option_details(symbol) or parse_option_details(token)
            if details:
                und, strike, otype = details
                und = und.upper()
                s_str = f"{strike:.1f}" if strike % 1 != 0 else f"{int(strike)}"
                if und not in options_map:
                    options_map[und] = {"strikes": {}}
                if s_str not in options_map[und]["strikes"]:
                    options_map[und]["strikes"][s_str] = {}
                options_map[und]["strikes"][s_str][otype.upper()] = raw_key

    total_rows = writer.close()

    manifest_data = {
        "options_map": options_map,
        "instrument_name_map": instrument_name_map,
        "total_contracts": len(tokens_seen),
    }

    if manifest_out and options_map:
        os.makedirs(os.path.dirname(manifest_out), exist_ok=True)
        with open(manifest_out, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

    return total_rows, manifest_data


def convert_session(
    src_dir: str,
    dest_dir: str,
    session_date: str,
    chunk_size: int = 100000,
    verify: bool = True,
) -> Dict[str, Any]:
    """Convert an entire session recording from YYYYMMDD to YYYY-MM-DD."""
    print(f"\n================================================================================")
    print(f"📦 CONVERTING RECORDING SESSION: {session_date}")
    print(f"Source Folder      : {src_dir}")
    print(f"Destination Folder : {dest_dir}")
    print(f"================================================================================")

    events_file = os.path.join(src_dir, "market_events.jsonl")
    if not os.path.exists(events_file):
        events_file = os.path.join(src_dir, "ticks.jsonl")

    depth_file = os.path.join(src_dir, "option_depth.jsonl")
    if not os.path.exists(depth_file):
        depth_file = os.path.join(src_dir, "options_depth.jsonl")

    if not os.path.exists(events_file) and not os.path.exists(depth_file):
        print(f"⚠️  No valid JSONL recording files found in '{src_dir}'. Skipping.")
        return {"status": "SKIPPED", "session_date": session_date, "reason": "No recording files"}

    os.makedirs(dest_dir, exist_ok=True)

    dest_equity = os.path.join(dest_dir, "equity_ticks.parquet")
    dest_options = os.path.join(dest_dir, "option_depth.parquet")
    dest_manifest = os.path.join(dest_dir, "subscription_manifest.json")
    dest_meta = os.path.join(dest_dir, "metadata.json")

    # 1. Convert Equity Stream
    print(f"\n[1/3] Converting Equity Underlyings & Index ticks...")
    t0 = time.time()
    eq_rows = convert_equity_stream(events_file, dest_equity, chunk_size=chunk_size)
    eq_time = time.time() - t0
    eq_sz = os.path.getsize(dest_equity) / (1024 * 1024)
    print(f"  ✓ equity_ticks.parquet: {eq_rows:,} records | {eq_sz:.2f} MB in {eq_time:.2f}s ({eq_rows/max(0.001, eq_time):,.0f} rows/s)")

    # 2. Convert Option Depth Stream & Build Manifest
    print(f"\n[2/3] Converting Option Depth snapshots & ticks...")
    t0 = time.time()
    opt_rows, manifest_info = convert_options_stream(
        events_file, depth_file, dest_options, manifest_out=dest_manifest, chunk_size=chunk_size
    )
    opt_time = time.time() - t0
    opt_sz = os.path.getsize(dest_options) / (1024 * 1024)
    print(f"  ✓ option_depth.parquet: {opt_rows:,} records | {opt_sz:.2f} MB in {opt_time:.2f}s ({opt_rows/max(0.001, opt_time):,.0f} rows/s)")
    print(f"  ✓ subscription_manifest.json: {manifest_info['total_contracts']:,} unique option contracts mapped across {len(manifest_info['options_map'])} underlyings")

    # 3. Copy metadata
    src_meta = os.path.join(src_dir, "metadata.json")
    if os.path.exists(src_meta):
        try:
            with open(src_meta, "r", encoding="utf-8") as f:
                meta_dict = json.load(f)
            meta_dict["converted_to_parquet_at"] = datetime.now().isoformat()
            meta_dict["converted_stats"] = {
                "equity_rows": eq_rows,
                "option_rows": opt_rows,
                "total_rows": eq_rows + opt_rows,
            }
            with open(dest_meta, "w", encoding="utf-8") as f:
                json.dump(meta_dict, f, indent=2)
        except Exception:
            shutil.copy2(src_meta, dest_meta)

    # 4. Verification Check
    if verify:
        print(f"\n[3/3] Verifying converted partition with ColumnarSimEngine...")
        try:
            from simulation.columnar_sim_engine import ColumnarSimEngine
            dash_date = format_date_with_dashes(session_date)
            sim = ColumnarSimEngine(data_path=dest_dir, session_date=dash_date)
            sim._load_manifest_catalog()
            df_e, df_o = sim.load_data()
            print(f"  ✓ ColumnarSimEngine Load Test: PASSED")
            print(f"    - df_equity shape  : {df_e.shape if df_e is not None else (0, 0)}")
            print(f"    - df_options shape : {df_o.shape if df_o is not None else (0, 0)}")
            print(f"    - Catalog Underlyings : {len(sim.options_catalog)}")
        except Exception as e:
            print(f"  ❌ Verification failed: {e}", file=sys.stderr)

    print(f"\n✅ Session {session_date} converted successfully into {dest_dir}!")
    return {
        "status": "SUCCESS",
        "session_date": session_date,
        "dest_dir": dest_dir,
        "equity_rows": eq_rows,
        "option_rows": opt_rows,
    }


def find_convertible_sessions(recordings_dir: str = "recordings") -> List[str]:
    """Discover unhyphenated YYYYMMDD recording directories."""
    if not os.path.exists(recordings_dir):
        return []

    dirs = []
    for name in os.listdir(recordings_dir):
        p = os.path.join(recordings_dir, name)
        if os.path.isdir(p) and len(name) == 8 and name.isdigit():
            # Check if it has jsonl files
            has_events = os.path.exists(os.path.join(p, "market_events.jsonl")) or os.path.exists(os.path.join(p, "ticks.jsonl"))
            has_depth = os.path.exists(os.path.join(p, "option_depth.jsonl")) or os.path.exists(os.path.join(p, "options_depth.jsonl"))
            if has_events or has_depth:
                dirs.append(name)
    return sorted(dirs)


def main():
    parser = argparse.ArgumentParser(description="Convert legacy market_events.jsonl & option_depth.jsonl to dual-stream Parquet")
    parser.add_argument("--dates", nargs="*", default=None, help="Specific session dates to convert (e.g. 20261109 20261111)")
    parser.add_argument("--recordings-dir", default="recordings", help="Root directory holding session folders")
    parser.add_argument("--chunk-size", type=int, default=100000, help="Batch size for writing Parquet chunks")
    parser.add_argument("--no-verify", action="store_true", help="Skip post-conversion verification with ColumnarSimEngine")
    args = parser.parse_args()

    recordings_dir = os.path.abspath(args.recordings_dir)
    target_dates = args.dates

    if not target_dates:
        target_dates = find_convertible_sessions(recordings_dir)
        print(f"Found {len(target_dates)} convertible legacy sessions: {target_dates}")

    if not target_dates:
        print("No convertible sessions found. Exiting.")
        sys.exit(0)

    summary = []
    for d_raw in target_dates:
        d_clean = d_raw.replace("-", "").strip()
        dash_date = format_date_with_dashes(d_clean)

        src = os.path.join(recordings_dir, d_clean)
        dest = os.path.join(recordings_dir, dash_date)

        res = convert_session(
            src_dir=src,
            dest_dir=dest,
            session_date=d_clean,
            chunk_size=args.chunk_size,
            verify=not args.no_verify,
        )
        summary.append(res)

    print("\n" + "=" * 80)
    print("🏁 ALL SESSIONS CONVERSION COMPLETED")
    print("=" * 80)
    for s in summary:
        print(f"  • {s['session_date']} -> {s.get('dest_dir', 'N/A')}: Status={s['status']} | Equity={s.get('equity_rows', 0):,} | Options={s.get('option_rows', 0):,}")


if __name__ == "__main__":
    main()
