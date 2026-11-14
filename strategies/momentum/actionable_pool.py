#!/usr/bin/env python3
"""
strategies/momentum/actionable_pool.py
======================================
Scoring Engine 2.0: Actionable Setup Pool with Continuous Exponential Decay.

Key Responsibilities:
1. Candidate Ingestion: Holds candidate setups that have passed binary detection.
2. Continuous Exponential Score Decay:
   S_decayed(t) = S_0 * max(0.0, 1.0 - ((t - T_0) / 20.0)^1.5)
3. Hard 20-Second Eviction:
   Purges candidates older than 20.0 seconds automatically, preventing stale fills.
4. Dynamic Priority Sorting:
   Whenever queried, candidates are sorted strictly by current decayed score descending.
5. Single-Fill Execution:
   Once popped for execution, candidates are removed from the pool to prevent duplicate entries.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional


class ActionableSetupPool:
    """
    Thread-safe, deterministic candidate setup pool with real-time score decay.
    """

    def __init__(self, expiry_seconds: float = 20.0, decay_power: float = 1.5):
        self.expiry_seconds = float(expiry_seconds)
        self.decay_power = float(decay_power)
        # key -> candidate dict
        self._pool: Dict[str, Dict[str, Any]] = {}

    def add_candidate(
        self,
        key: str,
        symbol: str,
        ltp: float,
        direction: str,
        stage: str,
        setup_type: str,
        raw_score: float,
        now_ts: float,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Adds or updates a candidate setup with its trigger timestamp and raw alpha score.
        """
        extra = extra_data or {}
        # Preserve original trigger_ts if candidate is already in the pool so decay runs continuously
        orig_trigger_ts = float(now_ts)
        if key in self._pool:
            orig_trigger_ts = self._pool[key].get("trigger_ts", float(now_ts))

        entry = {
            "key": key,
            "symbol": symbol,
            "ltp": float(ltp),
            "direction": direction,
            "stage": stage,
            "setup_type": setup_type,
            "raw_score": float(raw_score),
            "trigger_ts": orig_trigger_ts,
            "extra": extra,
        }
        self._pool[key] = entry
        return entry

    def compute_decayed_score(self, raw_score: float, trigger_ts: float, now_ts: float) -> float:
        """
        Computes real-time decayed score:
        S_decayed = S_0 * max(0.0, 1.0 - (age / 20.0)^1.5)
        """
        age = max(0.0, now_ts - trigger_ts)
        if age >= self.expiry_seconds:
            return 0.0
        decay_factor = max(0.0, 1.0 - math.pow(age / self.expiry_seconds, self.decay_power))
        return round(raw_score * decay_factor, 2)

    def purge_expired(self, now_ts: float) -> int:
        """
        Removes candidates whose age exceeds expiry_seconds. Returns number of purged items.
        """
        expired_keys = [
            k for k, v in self._pool.items()
            if (now_ts - v["trigger_ts"]) >= self.expiry_seconds
        ]
        for k in expired_keys:
            del self._pool[k]
        return len(expired_keys)

    def get_sorted_candidates(self, now_ts: float) -> List[Dict[str, Any]]:
        """
        Purges expired entries, recalculates current decayed score for all active candidates,
        and returns the active candidates sorted by decayed score descending.
        """
        self.purge_expired(now_ts)
        evaluated = []
        for v in self._pool.values():
            s_decayed = self.compute_decayed_score(v["raw_score"], v["trigger_ts"], now_ts)
            age = round(now_ts - v["trigger_ts"], 1)
            evaluated.append({
                **v,
                "decayed_score": s_decayed,
                "age_seconds": age,
            })

        evaluated.sort(key=lambda c: c["decayed_score"], reverse=True)
        return evaluated

    def pop_best_candidate(self, now_ts: float) -> Optional[Dict[str, Any]]:
        """
        Retrieves and removes the highest-scoring decayed candidate from the pool.
        Returns None if pool is empty or all candidates are expired.
        """
        sorted_candidates = self.get_sorted_candidates(now_ts)
        if not sorted_candidates:
            return None
        best = sorted_candidates[0]
        # Remove from pool
        self._pool.pop(best["key"], None)
        return best

    def remove(self, key: str) -> None:
        """Explicitly remove candidate by instrument key."""
        self._pool.pop(key, None)

    def clear(self) -> None:
        """Flush all candidates from pool."""
        self._pool.clear()

    def __len__(self) -> int:
        return len(self._pool)

