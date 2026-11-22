"""
Common Interfaces & Data Structures for Multi-Track Execution.
Defines ExecutionContext, TradeState, and Track contracts.
"""
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional
import time

from core.common.ipc_schemas import ActionableOpportunity, OrderRequest, OrderResult


@dataclass
class TradeState:
    trade_id: str
    mode: str
    track: str
    symbol: str
    token: str
    option_type: str
    strike: float
    lot_size: int
    quantity: int
    entry_ask: float
    entry_price: float
    entry_time: float
    target_price: float
    stop_loss: float
    highest_price: float
    current_bid: float
    status: str = "OPEN"  # OPEN, CLOSED
    entry_reason: Optional[str] = None
    origin_track: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: float = 0.0
    ratchet_level: int = 0
    underlying_entry_price: Optional[float] = None
    underlying_exit_price: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExecutionContext(ABC):
    """Execution context provided by Superstructures to Tracks."""

    @property
    @abstractmethod
    def mode(self) -> str:
        """Execution mode: 'paper', 'paper_delayed', or 'live'."""
        pass

    @abstractmethod
    def is_frozen(self) -> bool:
        """Check whether execution is frozen (in-memory atomic flag)."""
        pass

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> None:
        """Submit an order request to the superstructure."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""
        pass

    @abstractmethod
    def record_trade_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Record trade event to single-owner journal."""
        pass

    def subscribe_quotes(self, tokens: List[str]) -> None:
        """Subscribe to live quotes stream from Quotes Broker."""
        pass

    def unsubscribe_quotes(self, tokens: List[str]) -> None:
        """Unsubscribe from live quotes stream from Quotes Broker."""
        pass

    def get_quote(self, token: str) -> Optional[Dict[str, Any]]:
        """Retrieve latest snapshot quote for contract token."""
        return None

