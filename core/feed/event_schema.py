#!/usr/bin/env python3
"""
infrastructure/market_data/event_schema.py
==========================================
Normalized Event Schemas for Market Data Distribution & Recording.

All raw incoming broker ticks (Protobuf or REST) are parsed and normalized into
standard Python event dictionaries adhering to these schemas before distribution
over local IPC or writing to raw session recordings.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MarketTick:
    """Normalized tick update for an underlying equity or option contract."""
    instrument_key: str
    symbol: str
    ltp: float
    volume: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0  # Previous day close
    net_change: float = 0.0
    pct_change: float = 0.0
    vwap: float = 0.0
    bid: float = 0.0    # Best bid price
    bid_qty: int = 0
    ask: float = 0.0    # Best ask price
    ask_qty: int = 0
    exchange_timestamp: int = 0      # Epoch milliseconds reported by exchange
    received_timestamp: float = 0.0  # Local system epoch seconds on receipt
    sequence_number: int = 0
    event_type: str = "TICK"
    source: str = "stream"           # 'stream' (WebSocket) or 'poll' (REST)

    def __post_init__(self):
        if not self.received_timestamp:
            self.received_timestamp = time.time()
        if self.close > 0 and self.net_change == 0.0 and self.ltp > 0:
            self.net_change = round(self.ltp - self.close, 2)
        if self.close > 0 and self.pct_change == 0.0:
            self.pct_change = round((self.net_change / self.close) * 100, 2)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OptionDepth:
    """5-level market depth / order book update for option contracts."""
    instrument_key: str
    symbol: str
    bids: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]
    asks: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]
    bid: float = 0.0
    bid_qty: int = 0
    ask: float = 0.0
    ask_qty: int = 0
    ltp: float = 0.0
    exchange_timestamp: int = 0
    received_timestamp: float = 0.0
    sequence_number: int = 0
    event_type: str = "DEPTH"

    def __post_init__(self):
        if not self.received_timestamp:
            self.received_timestamp = time.time()
        if not self.bid and self.bids:
            self.bid = float(self.bids[0][0])
            self.bid_qty = int(self.bids[0][1]) if len(self.bids[0]) > 1 else 0
        if not self.ask and self.asks:
            self.ask = float(self.asks[0][0])
            self.ask_qty = int(self.asks[0][1]) if len(self.asks[0]) > 1 else 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SystemEvent:
    """Lifecycle and connection state event."""
    code: str  # CONNECTED, DISCONNECTED, RECONNECTED, GAP_DETECTED, HEARTBEAT, SHUTDOWN
    message: str = ""
    timestamp: float = 0.0
    sequence_number: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)
    event_type: str = "SYSTEM"

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

