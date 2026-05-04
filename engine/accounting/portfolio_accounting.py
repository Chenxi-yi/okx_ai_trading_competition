"""Fill-driven portfolio accounting for backtest and paper modes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping

from contracts import Fill, OrderIntent, PortfolioState, Position


@dataclass(frozen=True)
class AccountingConfig:
    initial_nav_usdt: float = 10_000.0
    min_position_contracts: float = 1e-9


class PortfolioAccounting:
    """Maintains PortfolioState from fills and mark prices.

    This is intentionally simple but contract-aware. It is the shared accounting
    kernel for the new backtest loop and paper runner; live account state should
    still be reconciled against OKX before trusting local state.
    """

    def __init__(self, config: AccountingConfig | None = None):
        self.config = config or AccountingConfig()
        now = datetime.now(timezone.utc)
        self.cash_usdt = float(self.config.initial_nav_usdt)
        self.positions: dict[str, Position] = {}
        self.realized_pnl_usdt = 0.0
        self.total_fees_usdt = 0.0
        self.total_funding_usdt = 0.0
        self.unrealized_pnl_usdt = 0.0
        self.strategy_realized_pnl: dict[str, float] = {}
        self.strategy_fees: dict[str, float] = {}
        self.strategy_funding: dict[str, float] = {}
        self.last_state = PortfolioState(timestamp=now, nav_usdt=self.cash_usdt, free_usdt=self.cash_usdt)

    def state(self, timestamp: datetime | None = None, mark_prices: Mapping[str, float] | None = None) -> PortfolioState:
        timestamp = timestamp or datetime.now(timezone.utc)
        mark_prices = mark_prices or {}
        unrealized = self._unrealized(mark_prices)
        nav = max(0.0, self.cash_usdt + unrealized)
        used = self._per_strategy_used(mark_prices)
        self.unrealized_pnl_usdt = unrealized
        self.last_state = PortfolioState(
            timestamp=timestamp,
            nav_usdt=nav,
            free_usdt=max(0.0, self.cash_usdt),
            positions=dict(self.positions),
            per_strategy_used=used,
            realized_pnl_usdt=self.realized_pnl_usdt,
            unrealized_pnl_usdt=unrealized,
            total_fees_usdt=self.total_fees_usdt,
            metadata={"total_funding_usdt": self.total_funding_usdt},
        )
        return self.last_state

    def apply_funding(
        self,
        timestamp: datetime,
        mark_prices: Mapping[str, float],
        funding_rates: Mapping[str, float],
    ) -> float:
        cashflow = 0.0
        for inst_id, position in self.positions.items():
            strategy_id = self._strategy_id(position)
            price = float(mark_prices.get(inst_id, mark_prices.get(position.symbol, position.entry_price)))
            rate = float(funding_rates.get(inst_id, funding_rates.get(position.symbol, 0.0)) or 0.0)
            ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
            signed = position.size_contracts if position.side == "long" else -position.size_contracts
            signed_notional = signed * ct_val * price
            flow = -signed_notional * rate
            cashflow += flow
            self.strategy_funding[strategy_id] = self.strategy_funding.get(strategy_id, 0.0) + (-flow)
        self.cash_usdt += cashflow
        self.total_funding_usdt += -cashflow
        self.state(timestamp, mark_prices)
        return cashflow

    def apply_fill(self, fill: Fill, order: OrderIntent, timestamp: datetime | None = None) -> PortfolioState:
        timestamp = timestamp or fill.timestamp
        if fill.status == "error" or fill.fill_size <= 0 or fill.fill_price <= 0:
            return self.state(timestamp)

        ct_val = float(order.metadata.get("ct_val", 1.0) or 1.0)
        strategy_id = str(order.metadata.get("strategy_id") or "unknown")
        signed_contracts = fill.fill_size if fill.side == "buy" else -fill.fill_size
        # USDT-margined perpetuals do not spend the full notional on entry.
        # Cash changes through fees, realized PnL, funding, and deposits.
        self.cash_usdt -= fill.fee
        self.total_fees_usdt += fill.fee
        self.strategy_fees[strategy_id] = self.strategy_fees.get(strategy_id, 0.0) + fill.fee

        current = self.positions.get(fill.inst_id)
        if current is None:
            self._open_position(fill, order, signed_contracts, timestamp)
            return self.state(timestamp, {fill.inst_id: fill.fill_price})

        current_signed = current.size_contracts if current.side == "long" else -current.size_contracts
        new_signed = current_signed + signed_contracts
        if current_signed and (current_signed > 0 > signed_contracts or current_signed < 0 < signed_contracts):
            closed = min(abs(current_signed), abs(signed_contracts))
            realized = self._closed_pnl(current, fill.fill_price, closed, ct_val)
            self.cash_usdt += realized
            self.realized_pnl_usdt += realized
            owner = self._strategy_id(current)
            self.strategy_realized_pnl[owner] = self.strategy_realized_pnl.get(owner, 0.0) + realized

        if abs(new_signed) < self.config.min_position_contracts:
            self.positions.pop(fill.inst_id, None)
        elif current_signed == 0 or (current_signed > 0) == (new_signed > 0):
            self.positions[fill.inst_id] = self._merged_position(current, fill, order, current_signed, signed_contracts, new_signed)
        else:
            self._open_position(fill, order, new_signed, timestamp)
        return self.state(timestamp, {fill.inst_id: fill.fill_price})

    def _open_position(self, fill: Fill, order: OrderIntent, signed_contracts: float, timestamp: datetime) -> None:
        side = "long" if signed_contracts > 0 else "short"
        self.positions[fill.inst_id] = Position(
            symbol=fill.inst_id,
            inst_id=fill.inst_id,
            side=side,
            entry_price=fill.fill_price,
            size_contracts=abs(signed_contracts),
            opened_at=timestamp,
            decision_id=fill.decision_id,
            target=float(order.metadata["target"]) if order.metadata.get("target") is not None else None,
            stop=float(order.metadata["stop"]) if order.metadata.get("stop") is not None else None,
            metadata={
                "strategy_id": order.metadata.get("strategy_id"),
                "ct_val": order.metadata.get("ct_val", 1.0),
                "horizon_sec": order.metadata.get("horizon_sec"),
            },
        )

    def _merged_position(
        self,
        current: Position,
        fill: Fill,
        order: OrderIntent,
        current_signed: float,
        signed_contracts: float,
        new_signed: float,
    ) -> Position:
        same_direction_add = (current_signed > 0 and signed_contracts > 0) or (current_signed < 0 and signed_contracts < 0)
        if same_direction_add:
            total = abs(current_signed) + abs(signed_contracts)
            entry = ((current.entry_price * abs(current_signed)) + (fill.fill_price * abs(signed_contracts))) / total
        else:
            entry = current.entry_price
        return replace(
            current,
            side="long" if new_signed > 0 else "short",
            entry_price=entry,
            size_contracts=abs(new_signed),
            decision_id=fill.decision_id,
            target=None if order.metadata.get("partial_exit") else current.target,
            metadata={**dict(current.metadata), "strategy_id": order.metadata.get("strategy_id")},
        )

    @staticmethod
    def _closed_pnl(position: Position, exit_price: float, closed_contracts: float, ct_val: float) -> float:
        if position.side == "long":
            return (exit_price - position.entry_price) * closed_contracts * ct_val
        return (position.entry_price - exit_price) * closed_contracts * ct_val

    def _unrealized(self, mark_prices: Mapping[str, float]) -> float:
        pnl = 0.0
        for inst_id, position in self.positions.items():
            price = float(mark_prices.get(inst_id, mark_prices.get(position.symbol, position.entry_price)))
            ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
            if position.side == "long":
                pnl += (price - position.entry_price) * position.size_contracts * ct_val
            else:
                pnl += (position.entry_price - price) * position.size_contracts * ct_val
        return pnl

    def _per_strategy_used(self, mark_prices: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for inst_id, position in self.positions.items():
            strategy_id = str(position.metadata.get("strategy_id") or "unknown")
            price = float(mark_prices.get(inst_id, mark_prices.get(position.symbol, position.entry_price)))
            ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
            out[strategy_id] = out.get(strategy_id, 0.0) + abs(position.size_contracts * ct_val * price)
        return out

    def strategy_unrealized(self, mark_prices: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for inst_id, position in self.positions.items():
            strategy_id = self._strategy_id(position)
            price = float(mark_prices.get(inst_id, mark_prices.get(position.symbol, position.entry_price)))
            ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
            if position.side == "long":
                pnl = (price - position.entry_price) * position.size_contracts * ct_val
            else:
                pnl = (position.entry_price - price) * position.size_contracts * ct_val
            out[strategy_id] = out.get(strategy_id, 0.0) + pnl
        return out

    @staticmethod
    def _strategy_id(position: Position) -> str:
        return str(position.metadata.get("strategy_id") or "unknown")
