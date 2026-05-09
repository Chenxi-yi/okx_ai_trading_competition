"""Execution routers for backtest, paper, and live modes."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

try:
    from contracts import Decision, Fill, InstrumentSpec, OrderIntent
except ModuleNotFoundError:
    from engine.contracts import Decision, Fill, InstrumentSpec, OrderIntent


class BrokerLike(Protocol):
    okx_profile: str

    def set_leverage(self, symbol: str, leverage: float) -> None:
        ...



@dataclass
class ExecutionConfig:
    profile: str = "demo"
    order_type: str = "market"
    default_leverage: float = 1.0
    taker_fee_rate: float = 0.0004
    slippage_bps: float = 2.0
    require_protective_stop: bool = True


class ExecutionRouter:
    mode = "base"

    def build_order(
        self,
        decision: Decision,
        instrument: InstrumentSpec,
        config: ExecutionConfig,
    ) -> OrderIntent:
        close_contracts = decision.metadata.get("close_contracts")
        contracts = float(close_contracts) if close_contracts is not None else self._size_to_contracts(
            decision.size_usdt,
            decision.signal.entry,
            instrument,
        )
        side = "buy" if decision.signal.side == "long" else "sell"
        requested_leverage = decision.metadata.get("leverage", decision.signal.metadata.get("leverage", config.default_leverage))
        try:
            leverage = float(requested_leverage)
        except Exception:
            leverage = float(config.default_leverage)
        return OrderIntent(
            decision_id=decision.decision_id,
            inst_id=instrument.inst_id,
            side=side,
            size_contracts=contracts,
            order_type=config.order_type,
            timestamp=datetime.now(timezone.utc),
            profile=config.profile,
            leverage=min(leverage, instrument.max_leverage or leverage),
            reduce_only=bool(decision.metadata.get("reduce_only", False)),
            metadata={
                "strategy_id": decision.signal.strategy_id,
                "position_action": decision.metadata.get("position_action"),
                "exit_reason": decision.metadata.get("exit_reason"),
                "partial_exit": decision.metadata.get("partial_exit", False),
                "target": decision.signal.target,
                "stop": decision.signal.stop,
                "leverage": leverage,
                "horizon_sec": decision.signal.horizon_sec,
                "mode": self.mode,
                "ct_val": instrument.ct_val,
            },
        )

    def execute(self, order: OrderIntent, mark_price: float) -> Fill:
        raise NotImplementedError

    @staticmethod
    def _size_to_contracts(size_usdt: float, price: float, instrument: InstrumentSpec) -> float:
        if price <= 0 or instrument.ct_val <= 0:
            return 0.0
        raw = size_usdt / (price * instrument.ct_val)
        lot = instrument.lot_sz or 1.0
        contracts = math.floor(raw / lot) * lot
        return max(instrument.min_sz, contracts) if contracts > 0 else 0.0


class BacktestExecutionRouter(ExecutionRouter):
    mode = "backtest"

    def __init__(self, config: ExecutionConfig | None = None):
        self.config = config or ExecutionConfig()

    def execute(self, order: OrderIntent, mark_price: float) -> Fill:
        slip = self.config.slippage_bps / 10_000.0
        fill_price = mark_price * (1.0 + slip if order.side == "buy" else 1.0 - slip)
        ct_val = float(order.metadata.get("ct_val", 0.0) or 0.0)
        notional = abs(fill_price * order.size_contracts * ct_val)
        fee = notional * self.config.taker_fee_rate
        return Fill(
            decision_id=order.decision_id,
            inst_id=order.inst_id,
            side=order.side,
            fill_price=fill_price,
            fill_size=order.size_contracts,
            fee=fee,
            timestamp=datetime.now(timezone.utc),
            order_id=f"bt-{order.decision_id[:8]}",
            raw={"mode": self.mode},
        )


class PaperExecutionRouter(BacktestExecutionRouter):
    mode = "paper"


class LiveExecutionRouter(ExecutionRouter):
    mode = "live"

    def __init__(self, broker: BrokerLike, config: ExecutionConfig | None = None):
        self.broker = broker
        self.config = config or ExecutionConfig(profile="demo")

    def execute(self, order: OrderIntent, mark_price: float) -> Fill:
        try:
            from kit import KitClient, KitClientConfig, KitExecutionGateway
        except ModuleNotFoundError:
            from engine.kit import KitClient, KitClientConfig, KitExecutionGateway

        symbol = order.inst_id.replace("-USDT-SWAP", "/USDT")
        try:
            stop = _metadata_float(order.metadata.get("stop"))
            if self.config.require_protective_stop and not order.reduce_only and stop is None:
                return Fill(
                    decision_id=order.decision_id,
                    inst_id=order.inst_id,
                    side=order.side,
                    fill_price=0.0,
                    fill_size=0.0,
                    fee=0.0,
                    timestamp=datetime.now(timezone.utc),
                    status="error",
                    error="protective stop required before live entry",
                )
            if order.leverage:
                self.broker.set_leverage(symbol, order.leverage)
            gateway = KitExecutionGateway(
                KitClient(
                    KitClientConfig(
                        default_profile=order.profile or self.config.profile,
                        live_enabled=os.environ.get("LIVE_TRADING", "false").lower() == "true",
                    )
                ),
                profile=order.profile or self.config.profile,
                allow_live=os.environ.get("LIVE_TRADING", "false").lower() == "true",
            )
            result = gateway.place_order(order)
            fill = gateway.fill_from_result(order, result, mark_price)
            if result.ok and not order.reduce_only and stop is not None:
                stop_result = gateway.place_protective_stop(
                    order.inst_id,
                    _protective_stop_side(order.side),
                    order.size_contracts,
                    stop,
                    profile=order.profile or self.config.profile,
                )
                raw = dict(fill.raw or {})
                raw["protective_stop"] = {
                    "ok": stop_result.ok,
                    "argv": stop_result.argv,
                    "data": stop_result.data,
                    "error": stop_result.error,
                }
                if not stop_result.ok:
                    close_result = gateway.close_position(
                        order.inst_id,
                        profile=order.profile or self.config.profile,
                    )
                    raw["protective_stop_fail_close"] = {
                        "ok": close_result.ok,
                        "argv": close_result.argv,
                        "data": close_result.data,
                        "error": close_result.error,
                    }
                    return Fill(
                        decision_id=fill.decision_id,
                        inst_id=fill.inst_id,
                        side=fill.side,
                        fill_price=fill.fill_price,
                        fill_size=fill.fill_size,
                        fee=fill.fee,
                        timestamp=fill.timestamp,
                        order_id=fill.order_id,
                        status="error",
                        raw=raw,
                        error="entry filled but protective stop placement failed; close attempted",
                    )
                return Fill(
                    decision_id=fill.decision_id,
                    inst_id=fill.inst_id,
                    side=fill.side,
                    fill_price=fill.fill_price,
                    fill_size=fill.fill_size,
                    fee=fill.fee,
                    timestamp=fill.timestamp,
                    order_id=fill.order_id,
                    status=fill.status,
                    raw=raw,
                    error=fill.error,
                )
            return fill
        except Exception as exc:
            return Fill(
                decision_id=order.decision_id,
                inst_id=order.inst_id,
                side=order.side,
                fill_price=0.0,
                fill_size=0.0,
                fee=0.0,
                timestamp=datetime.now(timezone.utc),
                status="error",
                error=str(exc),
            )


def _protective_stop_side(entry_side: str) -> str:
    return "sell" if entry_side == "buy" else "buy"


def _metadata_float(value: object) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out
