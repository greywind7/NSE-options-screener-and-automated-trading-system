#!/usr/bin/env python3
"""
strategies/base.py
==================
Abstract Strategy Interface defining the standard lifecycle for all trading strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseStrategy(ABC):
    """
    Standard lifecycle interface that all trading strategies must implement.
    Infrastructure and replay engines communicate with strategies strictly via these methods.
    """

    @abstractmethod
    def initialize(self, context: Dict[str, Any]) -> None:
        """Initialize strategy with configuration, universe metadata, and historical baselines."""
        pass

    @abstractmethod
    def on_market_event(self, event: Dict[str, Any]) -> None:
        """Process incoming normalized MarketTick, OptionDepth, or SystemEvent."""
        pass

    @abstractmethod
    def on_timer(self, timestamp: float) -> None:
        """Periodic timer event (e.g. for rolling calculations, decay, or dashboard refresh)."""
        pass

    @abstractmethod
    def on_order_update(self, order_update: Dict[str, Any]) -> None:
        """Handle order status changes (filled, cancelled, rejected)."""
        pass

    @abstractmethod
    def shutdown(self) -> None:
        """Perform graceful shutdown, close positions if required, and flush state."""
        pass

