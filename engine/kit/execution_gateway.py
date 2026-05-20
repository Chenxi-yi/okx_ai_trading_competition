"""Execution gateway backed by OKX Agent Trade Kit CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from contracts import Fill, OrderIntent
except ModuleNotFoundError:
    from engine.contracts import Fill, OrderIntent

from .client import KitClient
from .schemas import KitCommand, KitResult


@dataclass(frozen=True)
class KitExecutionGateway:
    client: KitClient
    profile: str = "demo"
    allow_live: bool = False
    environment: str = ""

    def place_order(self, order: OrderIntent) -> KitResult:
        td_mode = str(order.metadata.get("td_mode") or order.metadata.get("mgnMode") or "cross")
        args = [
            "--instId",
            order.inst_id,
            "--side",
            order.side,
            "--ordType",
            order.order_type,
            "--sz",
            _format_size(order.size_contracts),
            "--posSide",
            "net",
            "--tdMode",
            td_mode,
        ]
        if order.limit_price is not None:
            args.extend(["--px", str(order.limit_price)])
        if bool(order.metadata.get("attach_brackets", False)):
            target = order.metadata.get("target")
            stop = order.metadata.get("stop")
            trigger_type = str(order.metadata.get("trigger_px_type") or "mark")
            if target is not None:
                args.extend(["--tpTriggerPx", str(target), "--tpOrdPx=-1", "--tpTriggerPxType", trigger_type])
            if stop is not None:
                args.extend(["--slTriggerPx", str(stop), "--slOrdPx=-1", "--slTriggerPxType", trigger_type])
        if order.reduce_only:
            # CLI place help does not expose reduceOnly for normal orders in 1.2.7.
            # Use close/algo reduce-only paths for exchange-side reduce-only when needed.
            pass
        profile = order.profile or self.profile
        return self.client.run(
            KitCommand(
                "swap",
                "place",
                tuple(args),
                profile=profile,
                allow_live=self.allow_live,
                metadata={
                    "decision_id": order.decision_id,
                    "inst_id": order.inst_id,
                    "environment": order.metadata.get("environment") or self.environment,
                },
            )
        )

    def close_position(self, inst_id: str, mgn_mode: str = "cross", pos_side: str = "net", profile: str | None = None) -> KitResult:
        return self.client.run(
            KitCommand(
                "swap",
                "close",
                ("--instId", inst_id, "--mgnMode", mgn_mode, "--posSide", pos_side),
                profile=profile or self.profile,
                allow_live=self.allow_live,
                metadata={"inst_id": inst_id, "environment": self.environment},
            )
        )

    def cancel_order(self, inst_id: str, order_id: str, profile: str | None = None) -> KitResult:
        return self.client.run(
            KitCommand(
                "swap",
                "cancel",
                (inst_id, "--ordId", order_id),
                profile=profile or self.profile,
                allow_live=self.allow_live,
                metadata={"inst_id": inst_id, "order_id": order_id, "environment": self.environment},
            )
        )

    def set_leverage(self, inst_id: str, leverage: float, mgn_mode: str = "cross", profile: str | None = None) -> KitResult:
        return self.client.run(
            KitCommand(
                "swap",
                "leverage",
                ("--instId", inst_id, "--lever", str(leverage), "--mgnMode", mgn_mode),
                profile=profile or self.profile,
                allow_live=self.allow_live,
                metadata={"inst_id": inst_id, "leverage": leverage, "environment": self.environment},
            )
        )

    def place_protective_stop(
        self,
        inst_id: str,
        side: str,
        size_contracts: float,
        stop_trigger_px: float,
        profile: str | None = None,
    ) -> KitResult:
        return self.client.run(
            KitCommand(
                "swap",
                "algo",
                (
                    "place",
                    "--instId",
                    inst_id,
                    "--side",
                    side,
                    "--sz",
                    _format_size(size_contracts),
                    "--ordType",
                    "conditional",
                    "--slTriggerPx",
                    str(stop_trigger_px),
                    "--slOrdPx=-1",
                    "--posSide",
                    "net",
                    "--tdMode",
                    "cross",
                    "--reduceOnly",
                ),
                profile=profile or self.profile,
                allow_live=self.allow_live,
                metadata={"inst_id": inst_id, "stop_trigger_px": stop_trigger_px, "environment": self.environment},
            )
        )

    def fill_from_result(self, order: OrderIntent, result: KitResult, mark_price: float) -> Fill:
        data = result.data
        item: dict[str, Any] = {}
        if isinstance(data, list) and data:
            item = dict(data[0])
        elif isinstance(data, dict):
            item = dict(data)
        order_id = str(item.get("ordId") or item.get("id") or "")
        fill_price = float(item.get("avgPx") or item.get("fillPx") or item.get("px") or mark_price or 0.0)
        return Fill(
            decision_id=order.decision_id,
            inst_id=order.inst_id,
            side=order.side,
            fill_price=fill_price,
            fill_size=order.size_contracts if result.ok else 0.0,
            fee=0.0,
            timestamp=order.timestamp,
            order_id=order_id,
            status="filled" if result.ok else "error",
            raw={"kit": item, "argv": result.argv},
            error=None if result.ok else result.error,
        )


def _format_size(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")
