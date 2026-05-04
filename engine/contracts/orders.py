"""Order and fill contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

OrderStatus = Literal["pending", "submitted", "open", "filled", "cancelled", "rejected", "error"]
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class OrderIntent:
    decision_id: str
    inst_id: str
    side: OrderSide
    size_contracts: float
    order_type: str
    timestamp: datetime
    profile: str
    limit_price: float | None = None
    leverage: float | None = None
    reduce_only: bool = False
    client_tag: str = "agentTradeKit"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Fill:
    decision_id: str
    inst_id: str
    side: OrderSide
    fill_price: float
    fill_size: float
    fee: float
    timestamp: datetime
    order_id: str = ""
    status: OrderStatus = "filled"
    raw: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
