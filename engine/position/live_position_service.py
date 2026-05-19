"""Live position lifecycle adapter around PositionManager.

Strategy adapters keep small local caches for continuity, but lifecycle
decisions must come from the position layer. This module translates legacy
cache rows into PortfolioState and returns reduce-only exit intents.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from contracts import InstrumentSpec, MarketState, PortfolioState, Position, PositionIntent

from .position_manager import PositionManager


@dataclass(frozen=True)
class LiveExitPlan:
    symbol: str
    inst_id: str
    side: str
    reason: str
    mark: float
    intent: PositionIntent
    raw_position: Mapping[str, Any]


class LivePositionLifecycleService:
    """Owns live stop/target/time-stop exit decisions for adapters."""

    def __init__(self, position_manager: PositionManager | None = None):
        self.position_manager = position_manager or PositionManager()

    def exit_plans(
        self,
        positions: Mapping[str, Mapping[str, Any]],
        mark_prices: Mapping[str, float],
        *,
        now: datetime,
        nav_usdt: float = 0.0,
        free_usdt: float = 0.0,
    ) -> tuple[LiveExitPlan, ...]:
        portfolio = self._portfolio(positions, mark_prices, now, nav_usdt=nav_usdt, free_usdt=free_usdt)
        market = self._market(portfolio, now)
        decisions = self.position_manager.exit_decisions(portfolio, market, mark_prices)
        plan = self.position_manager.plan(decisions, portfolio, market, mark_prices)
        raw_by_inst = {
            _inst_id_from_raw(symbol, raw): (symbol, raw)
            for symbol, raw in positions.items()
            if isinstance(raw, Mapping)
        }
        out: list[LiveExitPlan] = []
        for intent in plan.intents:
            if intent.action not in {"close", "reduce"} or not intent.reduce_only:
                continue
            symbol, raw = raw_by_inst.get(intent.inst_id, (intent.symbol, {}))
            mark = _float(mark_prices.get(intent.inst_id), _float(mark_prices.get(symbol), 0.0))
            out.append(
                LiveExitPlan(
                    symbol=symbol,
                    inst_id=intent.inst_id,
                    side=str(raw.get("side") or ""),
                    reason=str(intent.metadata.get("exit_reason") if intent.metadata else "") or intent.reason,
                    mark=mark,
                    intent=intent,
                    raw_position=raw,
                )
            )
        return tuple(out)

    def _portfolio(
        self,
        positions: Mapping[str, Mapping[str, Any]],
        mark_prices: Mapping[str, float],
        now: datetime,
        *,
        nav_usdt: float,
        free_usdt: float,
    ) -> PortfolioState:
        rows: dict[str, Position] = {}
        for symbol, raw in positions.items():
            if not isinstance(raw, Mapping):
                continue
            position = _position_from_raw(str(symbol), raw, mark_prices, now)
            if position is not None:
                rows[position.inst_id] = position
        return PortfolioState(
            timestamp=now,
            nav_usdt=float(nav_usdt),
            free_usdt=float(free_usdt),
            positions=rows,
        )

    @staticmethod
    def _market(portfolio: PortfolioState, now: datetime) -> MarketState:
        instruments = {
            position.inst_id: InstrumentSpec(
                inst_id=position.inst_id,
                symbol=position.symbol,
                ct_val=float(position.metadata.get("ct_val", 1.0) or 1.0),
                lot_sz=0.01,
                min_sz=0.01,
                max_leverage=float(position.metadata.get("leverage", 1.0) or 1.0),
                source="live_position_lifecycle",
            )
            for position in portfolio.positions.values()
        }
        return MarketState(timestamp=now, universe=tuple(instruments), instruments=instruments)


def _position_from_raw(
    symbol: str,
    raw: Mapping[str, Any],
    mark_prices: Mapping[str, float],
    now: datetime,
) -> Position | None:
    inst_id = _inst_id_from_raw(symbol, raw)
    side = str(raw.get("side") or "").lower()
    if side not in {"long", "short"}:
        return None
    entry_price = _float(raw.get("entry_price"), _float(raw.get("price"), _float(mark_prices.get(symbol), 0.0)))
    contracts = _float(raw.get("contracts"), _float(raw.get("exchange_contracts"), 0.0))
    if contracts <= 0:
        notional = _float(raw.get("notional_usdt"), _float(raw.get("notional"), 0.0))
        ct_val = _float(raw.get("ct_val"), _contract_value(symbol))
        mark = _float(mark_prices.get(inst_id), _float(mark_prices.get(symbol), entry_price))
        if mark > 0 and ct_val > 0:
            contracts = notional / (mark * ct_val)
    if entry_price <= 0 or contracts <= 0:
        return None
    opened_at = _parse_dt(raw.get("entry_ts") or raw.get("opened_at"), now)
    time_stop = _parse_optional_dt(raw.get("exit_ts") or raw.get("time_stop"))
    stop = _first_float(raw, ("stop_price", "stop"))
    target = _first_float(raw, ("tp1_price", "target"))
    ct_val = _float(raw.get("ct_val"), _contract_value(symbol))
    strategy_id = str(raw.get("source_strategy_id") or raw.get("strategy_id") or raw.get("signal_family") or "unknown")
    horizon_sec = _horizon_sec(raw)
    return Position(
        symbol=symbol,
        inst_id=inst_id,
        side=side,
        entry_price=entry_price,
        size_contracts=abs(contracts),
        opened_at=opened_at,
        decision_id=str(raw.get("decision_id") or ""),
        target=target,
        stop=stop,
        time_stop=time_stop,
        metadata={
            "strategy_id": strategy_id,
            "ct_val": ct_val,
            "horizon_sec": horizon_sec,
            "leverage": raw.get("leverage"),
        },
    )


def _inst_id_from_raw(symbol: str, raw: Mapping[str, Any]) -> str:
    value = str(raw.get("inst_id") or raw.get("instId") or "")
    if value:
        return value
    return symbol if symbol.endswith("-USDT-SWAP") else symbol.replace("_", "-").replace("/", "-") + "-SWAP"


def _first_float(raw: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if raw.get(key) is None:
            continue
        value = _float(raw.get(key), 0.0)
        if value > 0:
            return value
    return None


def _parse_dt(value: Any, default: datetime) -> datetime:
    parsed = _parse_optional_dt(value)
    return parsed or default


def _parse_optional_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _horizon_sec(raw: Mapping[str, Any]) -> float | None:
    value = _float(raw.get("horizon_sec"), 0.0)
    if value > 0:
        return value
    hours = _float(raw.get("horizon_hours"), 0.0)
    if hours > 0:
        return hours * 3600.0
    return None


def _contract_value(symbol: str) -> float:
    return {
        "BTC_USDT": 0.01,
        "BTC-USDT-SWAP": 0.01,
        "ETH_USDT": 0.1,
        "ETH-USDT-SWAP": 0.1,
    }.get(symbol, 1.0)
