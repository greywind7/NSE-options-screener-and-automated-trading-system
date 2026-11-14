#!/usr/bin/env python3
"""
infrastructure/recorder/recording_schema.py
===========================================
Versioned Data Recording Envelope Schema (v2.0).

Defines the structure for raw tick capture, option depth snapshots, and session metadata.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "2.0"


def make_session_header(
    session_date: str,
    manifest_summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate session initialization header event."""
    return {
        "event_type": "SESSION_START",
        "schema_version": SCHEMA_VERSION,
        "session_date": session_date,
        "started_at": datetime.now().isoformat(),
        "summary": manifest_summary,
    }


def make_session_footer(
    session_date: str,
    total_ticks: int,
    total_depth_updates: int,
    gaps_detected: int,
) -> Dict[str, Any]:
    """Generate session termination summary event."""
    return {
        "event_type": "SESSION_END",
        "schema_version": SCHEMA_VERSION,
        "session_date": session_date,
        "ended_at": datetime.now().isoformat(),
        "stats": {
            "total_ticks": total_ticks,
            "total_depth_updates": total_depth_updates,
            "gaps_detected": gaps_detected,
        },
    }

