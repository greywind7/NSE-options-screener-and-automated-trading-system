#!/usr/bin/env python3
"""
execution/base.py
=================
Abstract Execution Adapter Interface.

Separates strategy computation from order execution mechanics.
Whether orders run against Upstox Live, Upstox Sandbox, or Historical Replay,
the strategy interacts solely through this standard interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseExecutionAdapter(ABC):
    """
    Abstract Broker Execution Adapter.
    """

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        instrument_token: str,
        quantity: int,
        transaction_type: str,  # "BUY" or "SELL"
        order_type: str = "MARKET",
        product: str = "I",     # "I" (Intraday) or "D" (Delivery)
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: Optional[str] = None,
        market_quote: Optional[Dict[str, Any]] = None,
        stop_loss_price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Submit order to broker or simulation adapter."""
        pass

    def update_stop_loss(
        self,
        symbol: str,
        instrument_token: str,
        new_stop_loss_price: float,
    ) -> Dict[str, Any]:
        """Update trailing stop-loss price on active position (optional implementation)."""
        return {"status": "NOT_SUPPORTED"}

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        order_type: str = "LIMIT",
        validity: str = "DAY",
        disclosed_quantity: int = 0,
    ) -> Dict[str, Any]:
        """Modify open order parameters (v3)."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel open pending order."""
        pass

    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Query current lifecycle status of an order."""
        pass

    @abstractmethod
    def get_positions(self) -> List[Dict[str, Any]]:
        """Retrieve current open positions."""
        pass

    @abstractmethod
    def shutdown(self) -> None:
        """Cancel working orders and release connection resources."""
        pass

