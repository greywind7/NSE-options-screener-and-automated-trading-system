"""
POSIX Shared Memory & High-Performance Memory-Mapped Layout Specification.
Pre-allocates 221 symbols x 75 bars x 48 bytes (777 KB) in Shared Memory,
fitting entirely inside the CPU's L2 cache.
Implements Seqlock memory synchronization for lock-free multi-process reading.
Provides unified interface supporting both POSIX SharedMemory and /tmp mmap fallback
(providing identical microsecond latency while guaranteeing compatibility across macOS/Linux/sandboxes).
"""
import json
import mmap
import os
import signal
import sys
from multiprocessing.shared_memory import SharedMemory
from typing import Dict, List, Optional, Tuple, Union
import numpy as np

SHM_NAME = "shm_ohlc_221"
NUM_SYMBOLS = 221
NUM_BARS = 75  # 5-minute bars from 09:15 to 15:30 IST

SHM_SIGNAL_DETACH = getattr(signal, "SIGUSR1", 10)
SHM_SIGNAL_REATTACH = getattr(signal, "SIGUSR2", 12)
SHM_CONSUMER_ROLES = {"scoring_engine", "web_server"}

# Exact 48-byte struct layout adhering to LOW_LEVEL_DESIGN_2_0 (Section 3.1)
CANDLE_DTYPE = np.dtype(
    [
        ("timestamp", "<i8"),       # 8 bytes: Unix timestamp in seconds (int64)
        ("open", "<f4"),            # 4 bytes: Open price (float32)
        ("high", "<f4"),            # 4 bytes: High price (float32)
        ("low", "<f4"),             # 4 bytes: Low price (float32)
        ("close", "<f4"),           # 4 bytes: Close / Current LTP (float32)
        ("volume", "<u8"),          # 8 bytes: Cumulative bar volume (uint64)
        ("rvol", "<f4"),            # 4 bytes: Strict Current Candle rVOL (float32)
        ("atr", "<f4"),             # 4 bytes: 14-period Average True Range (float32)
        ("is_closed", "?"),         # 1 byte:  Boolean flag (True if bar finished, False if active)
        ("reserved", "<u1", (7,)),  # 7 bytes: Explicit padding to align struct to 48 bytes
    ],
    align=False,
)

assert CANDLE_DTYPE.itemsize == 48, f"CANDLE_DTYPE itemsize must be 48 bytes, got {CANDLE_DTYPE.itemsize}"

# Seqlock synchronization header: 16 bytes per symbol
SEQLOCK_DTYPE = np.dtype(
    [
        ("sequence_id", "<u8"),     # 8 bytes: monotonically increasing counter (odd = updating)
        ("last_update_ns", "<i8"),  # 8 bytes: nanosecond timestamp of last tick
    ],
    align=False,
)

assert SEQLOCK_DTYPE.itemsize == 16, f"SEQLOCK_DTYPE itemsize must be 16 bytes, got {SEQLOCK_DTYPE.itemsize}"

CANDLE_BYTES = NUM_SYMBOLS * NUM_BARS * CANDLE_DTYPE.itemsize  # 795,600 bytes
HEADER_BYTES = NUM_SYMBOLS * SEQLOCK_DTYPE.itemsize             # 3,536 bytes
TOTAL_SHM_SIZE = HEADER_BYTES + CANDLE_BYTES                   # 799,136 bytes (< 781 KB)


class MmapSharedMemoryAdapter:
    """Fallback adapter providing buffer interface identical to multiprocessing.shared_memory.SharedMemory."""
    def __init__(self, name: str, size: int, create: bool = False):
        clean_name = name.lstrip("/")
        self.file_path = os.path.join("/tmp", f"{clean_name}.shm")
        self.size = size
        self.name = name

        if create:
            # Create file truncated to size
            with open(self.file_path, "wb") as f:
                f.truncate(size)
            self._file = open(self.file_path, "r+b")
            self._mmap = mmap.mmap(self._file.fileno(), size)
        else:
            if not os.path.exists(self.file_path):
                raise FileNotFoundError(f"Shared memory file not found: {self.file_path}")
            self._file = open(self.file_path, "r+b")
            self._mmap = mmap.mmap(self._file.fileno(), size)

    @property
    def buf(self) -> memoryview:
        return memoryview(self._mmap)

    def close(self) -> None:
        if hasattr(self, "_mmap") and self._mmap is not None:
            try:
                self._mmap.close()
            except Exception:
                pass
            self._mmap = None
        if hasattr(self, "_file") and self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def unlink(self) -> None:
        self.close()
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
        except Exception:
            pass


def create_or_attach_shm(name: str = SHM_NAME, create: bool = False) -> Union[SharedMemory, MmapSharedMemoryAdapter]:
    """
    Create or attach to the shared memory segment.
    Uses memory-mapped file adapter (/tmp/{clean_name}.shm) for cross-process stability
    and sandbox immunity across independent CLI lifecycles on macOS/Linux.
    Falls back to multiprocessing.shared_memory.SharedMemory if explicitly forced.
    """
    if os.environ.get("FORCE_POSIX_SHM"):
        try:
            if create:
                try:
                    s = SharedMemory(name=name, create=False)
                    s.close()
                    s.unlink()
                except FileNotFoundError:
                    pass
                return SharedMemory(name=name, create=True, size=TOTAL_SHM_SIZE)
            else:
                return SharedMemory(name=name, create=False)
        except (PermissionError, OSError):
            pass
    return MmapSharedMemoryAdapter(name=name, size=TOTAL_SHM_SIZE, create=create)


def get_shm_views(shm: Union[SharedMemory, MmapSharedMemoryAdapter]) -> Tuple[np.ndarray, np.ndarray]:
    """
    Return NumPy structured array views over the shared memory buffer.
    Returns:
        headers: shape (221,) of SEQLOCK_DTYPE
        candles: shape (221, 75) of CANDLE_DTYPE
    """
    buf = shm.buf
    headers = np.ndarray((NUM_SYMBOLS,), dtype=SEQLOCK_DTYPE, buffer=buf, offset=0)
    candles = np.ndarray((NUM_SYMBOLS, NUM_BARS), dtype=CANDLE_DTYPE, buffer=buf, offset=HEADER_BYTES)
    return headers, candles


def unlink_shm(name: str = SHM_NAME) -> None:
    """Safely unlink shared memory segment or mmap backing file."""
    # Attempt POSIX unlink
    try:
        s = SharedMemory(name=name, create=False)
        s.close()
        s.unlink()
    except Exception:
        pass
    
    # Attempt mmap file unlink
    clean_name = name.lstrip("/")
    path = os.path.join("/tmp", f"{clean_name}.shm")
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def shm_exists(name: str = SHM_NAME) -> bool:
    """Return True if an mmap backing file or POSIX SharedMemory exists for `name`."""
    clean_name = name.lstrip("/")
    path = os.path.join("/tmp", f"{clean_name}.shm")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
    except OSError:
        pass

    try:
        s = SharedMemory(name=name, create=False)
        exists = s.size > 0
        s.close()
        return exists
    except Exception:
        return False


def find_shm_consumer_pids(
    pids_file: str = os.path.join("logs", "pids.json"),
) -> List[Tuple[str, int]]:
    """Discover live registered processes that consume Shared Memory."""
    if not pids_file or not os.path.exists(pids_file):
        return []
    try:
        with open(pids_file, "r", encoding="utf-8") as f:
            pids_data = json.load(f)
        if not isinstance(pids_data, dict):
            return []
    except Exception:
        return []

    consumers: List[Tuple[str, int]] = []
    self_pid = os.getpid()
    for role, info in pids_data.items():
        if not isinstance(info, dict):
            continue
        is_consumer = (
            role in SHM_CONSUMER_ROLES
            or role.startswith("scoring")
            or role.startswith("strategy")
            or bool(info.get("shm_consumer", False))
        )
        if not is_consumer:
            continue
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid <= 0 or pid == self_pid:
            continue
        try:
            os.kill(pid, 0)
            consumers.append((role, pid))
        except OSError:
            continue
    return consumers


def signal_shm_consumers(
    sig: int,
    pids_file: str = os.path.join("logs", "pids.json"),
) -> List[int]:
    """Send POSIX signal `sig` (`SHM_SIGNAL_DETACH` / `SHM_SIGNAL_REATTACH`) to active SHM consumers."""
    signaled: List[int] = []
    for _role, pid in find_shm_consumer_pids(pids_file=pids_file):
        try:
            os.kill(pid, sig)
            signaled.append(pid)
        except OSError:
            continue
    return signaled
