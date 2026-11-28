"""
Append-Only Fixed-Slot Disk Cache for Historical Simulations.

Stores per-(date, mode, track_name, param_hash) simulation results on disk under
`cache/simulations/<YYYY-MM-DD>.dat` with a companion metadata index
`cache/simulations/<YYYY-MM-DD>.idx.json`.

Design properties:
- Both `paper` and `paper_delayed` modes are cached per `(date, mode, track_name)`.
- Requests running a subset of previously cached tracks, or changing parameters on a
  subset of tracks, reuse every unchanged track from disk and only run
  `ColumnarSimEngine` for missed or Cold-Run tracks.
- Every record written to `<date>.dat` uses a standardized fixed-width 256-byte binary
  header and fixed-size (`SLOT_BYTES`) NUL-padded compressed payload frame so
  in-place overwrites during Cold Runs never corrupt or shift surrounding records.
- Heavy candle arrays (`underlying_candles`, `option_candles`) are omitted from disk
  records; chart modals fetch candles lazily via `/api/chart/candles`.
"""
from __future__ import annotations

import contextlib
from copy import deepcopy
from datetime import datetime
import fcntl
import glob
import hashlib
import json
import os
import re
import struct
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
import zlib

from core.common.logging_utils import get_logger
from core.common.time_utils import IST
from core.config.config_loader import get_config_manager
from common.tracks.factory import _get_enabled_tracks_spec, _get_momentum_config, _parse_composition

_logger = get_logger("SIM_DISK_CACHE")

CACHE_MAGIC = b"AGSIMC01"
CACHE_VERSION = 1
HEADER_BYTES = 256
# Fixed slot frame size (256 KiB): accommodates compressed JSON for thousands of trades
DEFAULT_SLOT_BYTES = 262144
# <8s magic, I version, 64s cache_key_hex, 16s mode, 48s track_name, Q updated_at_ns,
# I cold_run_count, I payload_len, I crc32, I slot_bytes, 92s reserved = 256 bytes
HEADER_STRUCT = struct.Struct("<8sI64s16s48sQIIII92s")
assert HEADER_STRUCT.size == HEADER_BYTES


def canonicalize_date(raw_date: str) -> str:
    """Normalize YYYYMMDD or YYYY-MM-DD into YYYY-MM-DD."""
    s = str(raw_date or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _encode_fixed_str(val: str, width: int) -> bytes:
    raw = str(val or "").encode("utf-8")[:width]
    return raw.ljust(width, b"\x00")


def strip_chart_data_from_trades(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return compact copies of trades with heavy candle arrays stripped."""
    cleaned: List[Dict[str, Any]] = []
    for tr in trades or []:
        t_copy = dict(tr)
        t_copy.pop("underlying_candles", None)
        t_copy.pop("option_candles", None)
        cleaned.append(t_copy)
    return cleaned


def strip_chart_data_from_details(details: Dict[str, Any]) -> Dict[str, Any]:
    """Strip heavy candle series from a single (mode, track) detail dict."""
    if not isinstance(details, dict):
        return {"trades": [], "curve": []}
    return {
        "trades": strip_chart_data_from_trades(details.get("trades") or []),
        "curve": list(details.get("curve") or []),
    }


def _file_fingerprint(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return None
    try:
        st = os.stat(path)
        return {
            "name": os.path.basename(path),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        }
    except OSError:
        return None


def compute_session_data_fingerprint(target_date: str, recordings_dir: str = "recordings") -> Dict[str, Any]:
    """Fingerprint input parquet/manifest/index files used to simulate target_date."""
    c_date = canonicalize_date(target_date)
    d_nodash = c_date.replace("-", "")
    rec_cands = [
        os.path.join(recordings_dir, c_date),
        os.path.join(recordings_dir, d_nodash),
    ]
    rec_files: List[Dict[str, Any]] = []
    for rdir in rec_cands:
        if os.path.isdir(rdir):
            for fname in sorted(os.listdir(rdir)):
                if fname.endswith(".parquet") and ("equity_ticks" in fname or "option_depth" in fname or "market_events" in fname):
                    fp = _file_fingerprint(os.path.join(rdir, fname))
                    if fp:
                        rec_files.append(fp)
            if rec_files:
                break

    manifest_fp = None
    for mp in [
        f"data/manifest/subscription_manifest_{d_nodash}.json",
        f"data/subscription_manifest_{d_nodash}.json",
        "data/manifest/subscription_manifest.json",
        "data/subscription_manifest.json",
    ]:
        fp = _file_fingerprint(mp)
        if fp:
            manifest_fp = fp
            break

    index_fp = None
    for ip in [
        f"data/index/index_{d_nodash}.json",
        f"data/index_{d_nodash}.json",
        "data/index/index.json",
        "data/index.json",
    ]:
        fp = _file_fingerprint(ip)
        if fp:
            index_fp = fp
            break

    return {
        "recordings": rec_files,
        "manifest": manifest_fp,
        "index": index_fp,
    }


def _normalize_global_settings_for_mode(mode: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract canonical global settings relevant to `mode`.
    Changes to paper_delayed latency parameters only invalidate `paper_delayed`, not `paper`.
    """
    glob = params.get("global_settings") or {}
    cfg_mgr = get_config_manager()
    base_scoring = dict(cfg_mgr.get_scoring_config() or {})
    base_risk = dict(cfg_mgr.get_risk_config() or {})
    mom = (cfg_mgr.strategy.get("strategies", {}).get("momentum") or {}) if isinstance(cfg_mgr.strategy, dict) else {}
    base_scale = dict(mom.get("option_convexity_scaling") or {})

    shared = {
        "capital": float(glob.get("capital", 250000.0)),
        "brokerage_per_order": float(glob.get("brokerage_per_order", mom.get("brokerage_per_order", 30.0))),
        "min_pole_pct": float(glob.get("min_pole_pct", base_scoring.get("min_pole_pct", 0.005))),
        "min_candle_elapsed_sec": float(glob.get("min_candle_elapsed_sec", base_scoring.get("min_candle_elapsed_sec", 15.0))),
        "flag_pole_buffer_pct": float(glob.get("flag_pole_buffer_pct", base_scoring.get("flag_pole_buffer_pct", 0.25))),
        "pure_3bar_expansion_pct": float(glob.get("pure_3bar_expansion_pct", base_scoring.get("pure_3bar_expansion_pct", 0.0045))),
        "max_alerts_per_symbol_per_day": (
            int(glob["max_alerts_per_symbol_per_day"])
            if glob.get("max_alerts_per_symbol_per_day") is not None
            else base_scoring.get("max_alerts_per_symbol_per_day", 3)
        ),
        "entry_freeze_time": str(glob.get("entry_freeze_time", mom.get("entry_freeze_time", "15:00"))),
        "eod_squareoff_time": str(glob.get("eod_squareoff_time", mom.get("eod_squareoff_time", "15:15"))),
        "enable_circuit_breaker": bool(glob.get("enable_circuit_breaker", base_risk.get("enable_circuit_breaker", False))),
        "daily_loss_limit_inr": float(glob.get("daily_loss_limit_inr", base_risk.get("daily_loss_limit_inr", 10000.0))),
        "max_spread_pct": float(glob.get("max_spread_pct", base_risk.get("max_spread_pct", 0.03))),
        "atr_spread_gate_enabled": bool(glob.get("atr_spread_gate_enabled", base_risk.get("atr_spread_gate_enabled", True))),
        "atr_spread_gate_mult": float(glob.get("atr_spread_gate_mult", base_risk.get("atr_spread_gate_mult", 2.0))),
        "opt_scale_enabled": bool(glob.get("opt_scale_enabled", base_scale.get("enabled", True))),
        "option_convexity_scaling": base_scale,
    }
    if mode == "paper_delayed":
        shared["min_delay_ms"] = float(glob.get("min_delay_ms", 200.0))
        shared["max_delay_ms"] = float(glob.get("max_delay_ms", 500.0))
    return shared


def resolve_track_cache_specs(
    target_date: str,
    params: Dict[str, Any],
    strategy_cfg: Dict[str, Any],
    recordings_dir: str = "recordings",
) -> List[Dict[str, Any]]:
    """
    Resolve every concrete (mode, track_name) item to be simulated or fetched from disk cache,
    along with its canonical SHA-256 cache_key.
    """
    c_date = canonicalize_date(target_date)
    glob = params.get("global_settings") or {}
    modes = [str(m).strip() for m in (glob.get("modes") or ["paper", "paper_delayed"]) if str(m).strip()]

    # Use strategy_cfg as authoritative when it explicitly defines enabled_tracks (avoids overwriting
    # user track toggles or parameter overrides with the static preset JSON file).
    s_mom = (strategy_cfg or {}).get("strategies", {}).get("momentum", {}) if isinstance(strategy_cfg, dict) else {}
    has_explicit_spec = bool(s_mom and _get_enabled_tracks_spec(s_mom))
    mom_cfg = _get_momentum_config(
        strategy_cfg,
        track_profile=None if has_explicit_spec else params.get("track_profile"),
    )
    enabled_spec = _get_enabled_tracks_spec(mom_cfg) or []
    tracks_dict = mom_cfg.get("tracks") or {}

    track_descriptors: List[Dict[str, Any]] = []
    seen_track_names = set()

    for entry in enabled_spec:
        if not isinstance(entry, dict):
            t_name = str(entry)
            if t_name in seen_track_names:
                continue
            seen_track_names.add(t_name)
            track_descriptors.append({
                "track_name": t_name,
                "kind": "base",
                "base_name": t_name,
                "moneyness": None,
                "track_config": deepcopy(tracks_dict.get(t_name, {})),
            })
            continue

        t_type = str(entry.get("type", "base")).lower()
        t_name = str(entry.get("name") or "").strip()
        if not t_name:
            continue

        if t_type == "base":
            t_modes = entry.get("modes") or []
            p_clean = deepcopy(tracks_dict.get(t_name, {}))
            if t_modes:
                for m_mode in t_modes:
                    full_name = f"{t_name}_{m_mode}"
                    if full_name in seen_track_names:
                        continue
                    seen_track_names.add(full_name)
                    track_descriptors.append({
                        "track_name": full_name,
                        "kind": "base",
                        "base_name": t_name,
                        "moneyness": m_mode,
                        "track_config": p_clean,
                    })
            else:
                if t_name not in seen_track_names:
                    seen_track_names.add(t_name)
                    track_descriptors.append({
                        "track_name": t_name,
                        "kind": "base",
                        "base_name": t_name,
                        "moneyness": None,
                        "track_config": p_clean,
                    })
        elif t_type == "composite":
            if t_name in seen_track_names:
                continue
            seen_track_names.add(t_name)
            comp_items = _parse_composition(entry.get("composition", []))
            sub_params = {
                sub_name: deepcopy(tracks_dict.get(sub_name, {}))
                for sub_name, _ in comp_items
            }
            comp_spec = {
                "name": t_name,
                "shared_pool": bool(entry.get("shared_pool", True)),
                "capital": entry.get("capital"),
                "composition": entry.get("composition", []),
                "sub_track_params": sub_params,
            }
            track_descriptors.append({
                "track_name": t_name,
                "kind": "composite",
                "base_name": t_name,
                "moneyness": None,
                "track_config": comp_spec,
            })

    data_fp = compute_session_data_fingerprint(c_date, recordings_dir=recordings_dir)
    specs: List[Dict[str, Any]] = []

    for mode in modes:
        mode_glob = _normalize_global_settings_for_mode(mode, params)
        for desc in track_descriptors:
            key_payload = {
                "cache_schema": 1,
                "date": c_date,
                "mode": mode,
                "track_name": desc["track_name"],
                "kind": desc["kind"],
                "track_config": desc["track_config"],
                "global": mode_glob,
                "data_fingerprint": data_fp,
            }
            canon_json = json.dumps(key_payload, sort_keys=True, separators=(",", ":"))
            cache_key = hashlib.sha256(canon_json.encode("utf-8")).hexdigest()
            specs.append({
                "date": c_date,
                "mode": mode,
                "track_name": desc["track_name"],
                "kind": desc["kind"],
                "base_name": desc["base_name"],
                "moneyness": desc["moneyness"],
                "cache_key": cache_key,
                "track_config": desc["track_config"],
            })

    return specs


def build_params_subset_for_tracks(
    params: Dict[str, Any],
    mode: str,
    target_track_names: List[str],
) -> Dict[str, Any]:
    """
    Build a trimmed copy of `params` enabling ONLY `target_track_names` for `mode`.
    Preserves non-enabled base_tracks parameter definitions when needed for composites.
    """
    subset = deepcopy(params)
    glob = dict(subset.get("global_settings") or {})
    glob["modes"] = [mode]
    subset["global_settings"] = glob

    wanted = set(target_track_names)

    # Filter base_tracks while retaining parameter dict definitions
    orig_base = subset.get("base_tracks") or {}
    new_base: Dict[str, Any] = {}
    for b_name, b_cfg in orig_base.items():
        b_copy = deepcopy(b_cfg)
        b_modes = list(b_cfg.get("modes") or [])
        if b_cfg.get("enabled", True):
            if b_modes:
                kept_modes = [mm for mm in b_modes if f"{b_name}_{mm}" in wanted]
                if kept_modes:
                    b_copy["enabled"] = True
                    b_copy["modes"] = kept_modes
                else:
                    b_copy["enabled"] = False
            else:
                b_copy["enabled"] = (b_name in wanted)
        new_base[b_name] = b_copy
    subset["base_tracks"] = new_base

    # Filter composite_tracks
    new_composites = []
    for comp in subset.get("composite_tracks") or []:
        c_copy = deepcopy(comp)
        if comp.get("enabled", True) and comp.get("name") in wanted:
            c_copy["enabled"] = True
            new_composites.append(c_copy)
    subset["composite_tracks"] = new_composites

    # Filter custom_tracks
    new_custom = []
    for cust in subset.get("custom_tracks") or []:
        c_type = cust.get("type", "base")
        if c_type == "composite":
            if cust.get("name") in wanted:
                new_custom.append(deepcopy(cust))
        else:
            arch = cust.get("archetype", "fast_momentum")
            c_mode = cust.get("mode", "ATM")
            if f"{arch}_{c_mode}" in wanted or cust.get("name") in wanted:
                new_custom.append(deepcopy(cust))
    subset["custom_tracks"] = new_custom

    return subset


class SimDiskCache:
    """
    Per-date append-only fixed-slot binary cache (`<date>.dat`) + metadata index (`<date>.idx.json`).

    All records written to `<date>.dat` use a standardized 256-byte binary header and fixed
    frame size (`SLOT_BYTES`) so cold runs can overwrite existing results safely in-place without
    corrupting adjacent records.
    """

    def __init__(self, cache_dir: str = "cache/simulations", slot_bytes: int = DEFAULT_SLOT_BYTES):
        self.cache_dir = cache_dir
        self.slot_bytes = max(HEADER_BYTES + 1024, int(slot_bytes))
        os.makedirs(self.cache_dir, exist_ok=True)

    def _paths(self, target_date: str) -> Tuple[str, str, str]:
        c_date = canonicalize_date(target_date)
        return (
            os.path.join(self.cache_dir, f"{c_date}.dat"),
            os.path.join(self.cache_dir, f"{c_date}.idx.json"),
            os.path.join(self.cache_dir, f"{c_date}.lock"),
        )

    @contextlib.contextmanager
    def _locked(self, target_date: str) -> Iterator[None]:
        os.makedirs(self.cache_dir, exist_ok=True)
        _, _, lock_path = self._paths(target_date)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _load_index_unlocked(self, target_date: str) -> Dict[str, Any]:
        c_date = canonicalize_date(target_date)
        _, idx_path, _ = self._paths(c_date)
        if not os.path.exists(idx_path):
            return {
                "schema_version": CACHE_VERSION,
                "date": c_date,
                "slot_bytes": self.slot_bytes,
                "header_bytes": HEADER_BYTES,
                "entries": {},
            }
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
                raise ValueError("Invalid index structure")
            return data
        except Exception as e:
            _logger.warning(f"Sim cache index corrupted for {c_date}, reinitializing: {e}")
            return {
                "schema_version": CACHE_VERSION,
                "date": c_date,
                "slot_bytes": self.slot_bytes,
                "header_bytes": HEADER_BYTES,
                "entries": {},
            }

    def _save_index_unlocked(self, target_date: str, index_data: Dict[str, Any]) -> None:
        c_date = canonicalize_date(target_date)
        _, idx_path, _ = self._paths(c_date)
        os.makedirs(self.cache_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=f".idx_{c_date}_", suffix=".json", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(index_data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, idx_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def has_key(self, target_date: str, cache_key: str) -> bool:
        with self._locked(target_date):
            idx = self._load_index_unlocked(target_date)
            return cache_key in idx.get("entries", {})

    def get_index(self, target_date: str) -> Dict[str, Any]:
        """Return a copy of the metadata index for target_date."""
        with self._locked(target_date):
            return deepcopy(self._load_index_unlocked(target_date))

    def get(self, target_date: str, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached (matrix_stats, details) for cache_key, verifying binary header & CRC32."""
        c_date = canonicalize_date(target_date)
        with self._locked(c_date):
            idx = self._load_index_unlocked(c_date)
            meta = idx.get("entries", {}).get(cache_key)
            if not meta:
                return None

            dat_path, _, _ = self._paths(c_date)
            if not os.path.exists(dat_path):
                return None

            offset = int(meta.get("offset", -1))
            slot_bytes = int(meta.get("slot_bytes", self.slot_bytes))
            if offset < 0 or slot_bytes < HEADER_BYTES:
                return None

            try:
                with open(dat_path, "rb") as df:
                    df.seek(offset)
                    frame = df.read(slot_bytes)
                if len(frame) < HEADER_BYTES:
                    return None

                (
                    magic,
                    version,
                    key_b,
                    _mode_b,
                    _track_b,
                    _updated_ns,
                    cold_count,
                    payload_len,
                    crc_expected,
                    frame_slot_bytes,
                    _reserved,
                ) = HEADER_STRUCT.unpack(frame[:HEADER_BYTES])

                if magic != CACHE_MAGIC or version != CACHE_VERSION:
                    _logger.warning(f"Sim cache header magic/version mismatch for {c_date} ({cache_key[:10]})")
                    return None

                stored_key = key_b.rstrip(b"\x00").decode("ascii", errors="ignore")
                if stored_key != cache_key:
                    _logger.warning(f"Sim cache key mismatch at offset {offset} ({c_date})")
                    return None

                if payload_len <= 0 or HEADER_BYTES + payload_len > len(frame):
                    return None

                payload_bytes = frame[HEADER_BYTES:HEADER_BYTES + payload_len]
                if (zlib.crc32(payload_bytes) & 0xFFFFFFFF) != crc_expected:
                    _logger.warning(f"Sim cache CRC32 mismatch for {c_date} ({cache_key[:10]})")
                    return None

                raw_json = zlib.decompress(payload_bytes).decode("utf-8")
                record = json.loads(raw_json)
                record["_cache_meta"] = {
                    "cache_key": cache_key,
                    "offset": offset,
                    "slot_bytes": frame_slot_bytes,
                    "payload_len": payload_len,
                    "cold_run_count": cold_count,
                    "updated_at": meta.get("updated_at"),
                }
                return record
            except Exception as e:
                _logger.warning(f"Sim cache read failure for {c_date} ({cache_key[:10]}): {e}")
                return None

    def put(
        self,
        target_date: str,
        cache_key: str,
        mode: str,
        track_name: str,
        matrix_stats: Dict[str, Any],
        details: Dict[str, Any],
        is_cold_run: bool = False,
        track_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Store or update a `(mode, track_name, cache_key)` result in `<date>.dat` and `<date>.idx.json`.
        Strips heavy candle series from `details` before serializing.
        """
        c_date = canonicalize_date(target_date)
        compact_details = strip_chart_data_from_details(details)
        record_payload = {
            "date": c_date,
            "mode": mode,
            "track": track_name,
            "matrix": dict(matrix_stats or {}),
            "details": compact_details,
        }
        raw_bytes = json.dumps(record_payload, separators=(",", ":")).encode("utf-8")
        compressed = zlib.compress(raw_bytes, level=6)
        payload_len = len(compressed)
        crc_val = zlib.crc32(compressed) & 0xFFFFFFFF
        required_bytes = HEADER_BYTES + payload_len

        with self._locked(c_date):
            idx = self._load_index_unlocked(c_date)
            entries = idx.setdefault("entries", {})
            existing = entries.get(cache_key)
            dat_path, _, _ = self._paths(c_date)

            if not os.path.exists(dat_path):
                with open(dat_path, "wb") as df:
                    df.flush()

            file_size = os.path.getsize(dat_path)

            if existing and required_bytes <= int(existing.get("slot_bytes", 0)):
                # Standardized fixed slot length allows deterministic in-place overwrite
                offset = int(existing["offset"])
                slot_bytes = int(existing["slot_bytes"])
                cold_count = int(existing.get("cold_run_count", 0)) + (1 if is_cold_run else 0)
            else:
                # Allocate aligned fixed slot at EOF (append-only)
                remainder = file_size % self.slot_bytes
                offset = file_size if remainder == 0 else file_size + (self.slot_bytes - remainder)
                units = max(1, (required_bytes + self.slot_bytes - 1) // self.slot_bytes)
                slot_bytes = units * self.slot_bytes
                cold_count = (
                    (int(existing.get("cold_run_count", 0)) + (1 if is_cold_run else 0))
                    if existing
                    else (1 if is_cold_run else 0)
                )

            now_ns = time.time_ns()
            now_iso = datetime.now(tz=IST).isoformat(timespec="seconds")

            header = HEADER_STRUCT.pack(
                CACHE_MAGIC,
                CACHE_VERSION,
                _encode_fixed_str(cache_key, 64),
                _encode_fixed_str(mode, 16),
                _encode_fixed_str(track_name, 48),
                now_ns,
                cold_count,
                payload_len,
                crc_val,
                slot_bytes,
                b"\x00" * 92,
            )
            pad_len = slot_bytes - HEADER_BYTES - payload_len
            frame = header + compressed + (b"\x00" * pad_len)
            assert len(frame) == slot_bytes

            with open(dat_path, "r+b") as df:
                df.seek(offset)
                df.write(frame)
                df.flush()
                os.fsync(df.fileno())

            meta_entry = {
                "cache_key": cache_key,
                "date": c_date,
                "mode": mode,
                "track": track_name,
                "offset": offset,
                "slot_bytes": slot_bytes,
                "payload_len": payload_len,
                "crc32": crc_val,
                "cold_run_count": cold_count,
                "created_at": existing.get("created_at", now_iso) if existing else now_iso,
                "updated_at": now_iso,
                "trades_count": len(compact_details.get("trades") or []),
                "net_pnl": float((matrix_stats or {}).get("net_pnl", 0.0)),
                "win_rate": float((matrix_stats or {}).get("win_rate", 0.0)),
                "param_summary": track_config or {},
            }
            entries[cache_key] = meta_entry
            idx["updated_at"] = now_iso
            self._save_index_unlocked(c_date, idx)
            return meta_entry
