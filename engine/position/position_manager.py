"""Position lifecycle layer between portfolio construction and risk."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Iterable, Literal, Mapping

from contracts import Decision, MarketState, PortfolioState, Position, Signal

PositionAction = Literal["open", "add", "reduce", "close", "reverse", "hold", "reject"]


@dataclass(frozen=True)
class PositionIntent:
    decision_id: str
    strategy_id: str
    symbol: str
    inst_id: str
    action: PositionAction
    side: str
    size_usdt: float
    reduce_only: bool
    reason: str
    timestamp: datetime
    current_size_contracts: float = 0.0
    target_size_contracts: float | None = None
    metadata: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PositionPlan:
    decisions: tuple[Decision, ...]
    intents: tuple[PositionIntent, ...]
    held: tuple[PositionIntent, ...] = ()
    rejected: tuple[PositionIntent, ...] = ()


@dataclass(frozen=True)
class PositionManagerConfig:
    allow_same_side_add: bool = False
    allow_immediate_reverse: bool = False
    max_same_symbol_strategy_positions: int = 1
    min_close_notional_usdt: float = 1.0
    conflict_policy: str = "winner_takes_symbol"
    partial_target_exit_pct: float = 1.0
    min_partial_contracts: float = 1.0


class PositionManager:
    """Converts portfolio decisions into position-change decisions.

    The default policy is intentionally conservative for a personal account:
    repeated same-direction signals hold the existing position, and opposite
    signals close existing exposure before any reverse entry is allowed.
    """

    def __init__(self, config: PositionManagerConfig | None = None):
        self.config = config or PositionManagerConfig()

    def plan(
        self,
        decisions: tuple[Decision, ...] | list[Decision],
        portfolio: PortfolioState,
        market: MarketState,
        mark_prices: Mapping[str, float],
    ) -> PositionPlan:
        out: list[Decision] = []
        intents: list[PositionIntent] = []
        held: list[PositionIntent] = []
        rejected: list[PositionIntent] = []
        eligible, conflict_held = self._resolve_decision_conflicts(decisions, market)
        held.extend(conflict_held)
        for decision in eligible:
            position = self._position_for(decision.signal.symbol, portfolio, market)
            if decision.metadata.get("reduce_only") and decision.metadata.get("close_contracts") is not None:
                inst_id = self._inst_id(decision.signal.symbol, market)
                intent = self._build_intent(
                    decision,
                    inst_id,
                    "close",
                    decision.size_usdt,
                    True,
                    str(decision.metadata.get("exit_reason") or "reduce-only exit"),
                    datetime.now(timezone.utc),
                    position,
                )
                intents.append(intent)
                out.append(decision)
                continue
            intent, adjusted = self._intent_for(decision, position, market, mark_prices)
            if intent.action == "hold":
                held.append(intent)
                continue
            if intent.action == "reject":
                rejected.append(intent)
                continue
            intents.append(intent)
            out.append(adjusted)
        return PositionPlan(decisions=tuple(out), intents=tuple(intents), held=tuple(held), rejected=tuple(rejected))

    def _resolve_decision_conflicts(
        self,
        decisions: Iterable[Decision],
        market: MarketState,
    ) -> tuple[list[Decision], list[PositionIntent]]:
        rows = list(decisions)
        if self.config.conflict_policy != "winner_takes_symbol":
            return rows, []
        grouped: dict[str, list[Decision]] = defaultdict(list)
        for decision in rows:
            grouped[self._inst_id(decision.signal.symbol, market)].append(decision)

        winners: list[Decision] = []
        held: list[PositionIntent] = []
        now = datetime.now(timezone.utc)
        for inst_id, group in grouped.items():
            if len(group) == 1:
                winners.append(group[0])
                continue
            ranked = sorted(group, key=self._decision_rank_key, reverse=True)
            winners.append(ranked[0])
            for loser in ranked[1:]:
                held.append(
                    self._build_intent(
                        loser,
                        inst_id,
                        "hold",
                        0.0,
                        False,
                        f"same-symbol conflict held; winner={ranked[0].signal.strategy_id}",
                        now,
                    )
                )
        return winners, held

    def exit_decisions(
        self,
        portfolio: PortfolioState,
        market: MarketState,
        mark_prices: Mapping[str, float],
    ) -> tuple[Decision, ...]:
        """Create reduce-only close decisions for target/stop/time-stop exits."""
        out: list[Decision] = []
        now = market.timestamp
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        for position in portfolio.positions.values():
            mark = float(mark_prices.get(position.inst_id, mark_prices.get(position.symbol, position.entry_price)))
            reason = self._exit_reason(position, mark, now)
            if reason is None:
                continue
            side = "short" if position.side == "long" else "long"
            close_contracts = self._exit_contracts(position, reason)
            if close_contracts <= 0:
                continue
            ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
            close_notional = abs(close_contracts * ct_val * mark)
            signal = Signal(
                strategy_id=str(position.metadata.get("strategy_id") or "position_exit"),
                symbol=position.inst_id,
                side=side,
                timestamp=now,
                entry=mark,
                target=None,
                stop=None,
                horizon_sec=0,
                p_target=1.0,
                adverse_pct_estimate=0.0,
                confidence=1.0,
                metadata={"exit_reason": reason, "source": "PositionManager"},
            )
            out.append(
                Decision(
                    signal=signal,
                    size_usdt=close_notional,
                    reason=f"position exit: {reason}",
                    timestamp=now,
                    metadata={
                        "position_action": "close",
                        "reduce_only": True,
                        "exit_reason": reason,
                        "current_position_side": position.side,
                        "current_position_contracts": position.size_contracts,
                        "close_contracts": close_contracts,
                        "close_notional_usdt": close_notional,
                        "partial_exit": close_contracts < position.size_contracts,
                    },
                )
            )
        return tuple(out)

    def _intent_for(
        self,
        decision: Decision,
        position: Position | None,
        market: MarketState,
        mark_prices: Mapping[str, float],
    ) -> tuple[PositionIntent, Decision]:
        now = datetime.now(timezone.utc)
        inst_id = self._inst_id(decision.signal.symbol, market)
        desired_side = decision.signal.side
        if position is None:
            return self._build_intent(decision, inst_id, "open", decision.size_usdt, False, "open new position", now), decision

        current_notional = self._position_notional(position, mark_prices)
        if position.side == desired_side:
            if not self.config.allow_same_side_add:
                intent = self._build_intent(
                    decision,
                    inst_id,
                    "hold",
                    0.0,
                    False,
                    "same-side position already open; duplicate entry held",
                    now,
                    position,
                )
                return intent, decision
            adjusted = replace(
                decision,
                metadata={**dict(decision.metadata), "position_action": "add", "reduce_only": False},
            )
            return self._build_intent(decision, inst_id, "add", decision.size_usdt, False, "add to same-side position", now, position), adjusted

        close_notional = max(0.0, current_notional)
        if close_notional < self.config.min_close_notional_usdt:
            intent = self._build_intent(
                decision,
                inst_id,
                "reject",
                0.0,
                False,
                "opposite position exists but close notional is below minimum",
                now,
                position,
            )
            return intent, decision

        action: PositionAction = "reverse" if self.config.allow_immediate_reverse else "close"
        adjusted = replace(
            decision,
            size_usdt=close_notional,
            reason=f"{decision.reason}; position manager {action} existing {position.side}",
            metadata={
                **dict(decision.metadata),
                "position_action": action,
                "reduce_only": not self.config.allow_immediate_reverse,
                "current_position_side": position.side,
                "current_position_contracts": position.size_contracts,
                "close_contracts": position.size_contracts,
                "close_notional_usdt": close_notional,
            },
        )
        intent = self._build_intent(
            decision,
            inst_id,
            action,
            close_notional,
            not self.config.allow_immediate_reverse,
            f"{action} existing {position.side} before new {desired_side}",
            now,
            position,
        )
        return intent, adjusted

    def _build_intent(
        self,
        decision: Decision,
        inst_id: str,
        action: PositionAction,
        size_usdt: float,
        reduce_only: bool,
        reason: str,
        timestamp: datetime,
        position: Position | None = None,
    ) -> PositionIntent:
        return PositionIntent(
            decision_id=decision.decision_id,
            strategy_id=decision.signal.strategy_id,
            symbol=decision.signal.symbol,
            inst_id=inst_id,
            action=action,
            side=decision.signal.side,
            size_usdt=float(size_usdt),
            reduce_only=reduce_only,
            reason=reason,
            timestamp=timestamp,
            current_size_contracts=position.size_contracts if position else 0.0,
            metadata={"signal_confidence": decision.signal.confidence},
        )

    @staticmethod
    def _position_notional(position: Position, mark_prices: Mapping[str, float]) -> float:
        price = float(mark_prices.get(position.inst_id, mark_prices.get(position.symbol, position.entry_price)))
        ct_val = float(position.metadata.get("ct_val", 1.0) or 1.0)
        return abs(position.size_contracts * ct_val * price)

    @staticmethod
    def _decision_rank_key(decision: Decision) -> tuple[float, float, float]:
        ev = decision.signal.forward_ev
        kelly = decision.signal.kelly_fraction
        return (
            ev if ev is not None else -1e9,
            kelly if kelly is not None else 0.0,
            decision.signal.confidence,
        )

    @staticmethod
    def _exit_reason(position: Position, mark: float, now: datetime) -> str | None:
        if mark <= 0:
            return None
        if position.side == "long":
            if position.target is not None and mark >= position.target:
                return "target_hit"
            if position.stop is not None and mark <= position.stop:
                return "stop_hit"
        else:
            if position.target is not None and mark <= position.target:
                return "target_hit"
            if position.stop is not None and mark >= position.stop:
                return "stop_hit"
        if position.time_stop is not None:
            stop_time = position.time_stop
            if stop_time.tzinfo is None:
                stop_time = stop_time.replace(tzinfo=timezone.utc)
            if now >= stop_time:
                return "time_stop"
        horizon_sec = position.metadata.get("horizon_sec")
        if horizon_sec:
            opened_at = position.opened_at
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            if now >= opened_at + timedelta(seconds=float(horizon_sec)):
                return "time_stop"
        return None

    def _exit_contracts(self, position: Position, reason: str) -> float:
        if reason != "target_hit":
            return position.size_contracts
        pct = max(0.0, min(1.0, float(self.config.partial_target_exit_pct)))
        contracts = position.size_contracts * pct
        if contracts < self.config.min_partial_contracts:
            contracts = position.size_contracts
        return min(position.size_contracts, contracts)

    @staticmethod
    def _position_for(symbol: str, portfolio: PortfolioState, market: MarketState) -> Position | None:
        if symbol in portfolio.positions:
            return portfolio.positions[symbol]
        inst_id = PositionManager._inst_id(symbol, market)
        return portfolio.positions.get(inst_id)

    @staticmethod
    def _inst_id(symbol: str, market: MarketState) -> str:
        direct = market.instruments.get(symbol)
        if direct:
            return direct.inst_id
        return symbol if symbol.endswith("-USDT-SWAP") else symbol.replace("/", "-") + "-SWAP"
