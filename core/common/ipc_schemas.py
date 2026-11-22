"""
ZeroMQ Msgpack Wire Schemas and Binary Serialization Protocols.
Standardizes messaging contracts between Ingestion, Scoring, RMS, and Execution Superstructures.
"""
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
import msgpack


@dataclass
class EquityTickEvent:
    token: str
    symbol: str
    ltp: float
    volume: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    exchange_timestamp: float = 0.0
    received_timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OptionDepthTickEvent:
    token: str
    symbol: str
    ltp: float
    volume: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    exchange_timestamp: float = 0.0
    received_timestamp: float = 0.0
    bids: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]
    asks: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TickEvent:
    token: str
    symbol: str
    ltp: float
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    exchange_timestamp: float = 0.0
    received_timestamp: float = 0.0
    volume: int = 0
    bids: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]
    asks: List[List[float]] = field(default_factory=list)  # [[price, qty, orders], ...]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteSnapshotRequest:
    action: str = "GET_QUOTES"  # "GET_QUOTES" or "GET_SNAPSHOT"
    tokens: List[str] = field(default_factory=list)
    best_depth: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteSnapshotResponse:
    status: str
    quotes: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteSubscriptionRequest:
    action: str = "SUBSCRIBE"  # "SUBSCRIBE" or "UNSUBSCRIBE"
    tokens: List[str] = field(default_factory=list)
    client_id: str = ""  # Optional client/mode identifier (e.g. "paper", "paper_delayed", "live")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteSubscriptionResponse:
    status: str
    subscribed_tokens: List[str] = field(default_factory=list)
    ref_counts: Dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteHeartbeat:
    type: str = "HEARTBEAT"
    timestamp: float = 0.0
    active_quotes_count: int = 0
    subscribed_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuoteDepthUpdate:
    token: str
    symbol: str
    ltp: float
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    timestamp: float
    bids: List[List[float]] = field(default_factory=list)
    asks: List[List[float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SetupAlert:
    alert_id: str
    symbol: str
    direction: str  # BULL or BEAR
    archetype: str  # FLAG_BULL_BREAKOUT, PURE_BULL_BREAKOUT, etc.
    trigger_price: float
    rvol: float
    clv: float
    atr: float
    flag_high: float
    flag_low: float
    timestamp: float
    pole_high: Optional[float] = None
    pole_low: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActionableOpportunity:
    opp_id: str
    alert_id: str
    symbol: str
    direction: str
    archetype: str
    trigger_price: float
    strikes: Dict[str, Any]
    timestamp: float
    flag_high: Optional[float] = None
    flag_low: Optional[float] = None
    pole_high: Optional[float] = None
    pole_low: Optional[float] = None
    atr: Optional[float] = None
    rvol: Optional[float] = None
    clv: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRequest:
    order_id: str
    mode: str
    track: str
    instrument_token: str
    symbol: str
    transaction_type: str  # BUY or SELL
    order_type: str        # MARKET, LIMIT, SL
    quantity: int
    price: float
    trigger_price: float
    timestamp: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderResult:
    order_id: str
    status: str  # FILLED, REJECTED, CANCELLED
    fill_price: float
    fill_quantity: int
    fill_timestamp: float
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def pack_message(data: Any) -> bytes:
    """Serialize dictionary or dataclass to binary msgpack."""
    if hasattr(data, "to_dict"):
        payload = data.to_dict()
    elif isinstance(data, dict):
        payload = data
    else:
        payload = asdict(data) if hasattr(data, "__dataclass_fields__") else data
    return msgpack.packb(payload, use_bin_type=True)


def unpack_message(raw_bytes: bytes) -> Dict[str, Any]:
    """Deserialize binary msgpack payload into Python dictionary."""
    return msgpack.unpackb(raw_bytes, raw=False)

