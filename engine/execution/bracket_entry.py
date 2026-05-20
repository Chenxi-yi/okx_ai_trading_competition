"""Live entry execution helpers owned by the execution layer."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

from contracts import OrderIntent
from kit import KitClient, KitClientConfig, KitExecutionGateway


def place_entry_with_brackets(
    *,
    inst_id: str,
    side: str,
    size_contracts: float,
    leverage: float,
    stop_price: float,
    target_price: float | None,
    profile: str,
    environment: str,
    strategy_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Place an entry and attached bracket metadata through Agent Trade Kit."""

    if bool(dry_run):
        return {
            "ok": True,
            "stage": "dry_run",
            "leverage": {"ok": True, "dry_run": True},
            "take_profit_attached": target_price is not None,
            "place": {
                "ok": True,
                "dry_run": True,
                "inst_id": inst_id,
                "side": side,
                "size_contracts": size_contracts,
                "stop_price": stop_price,
                "target_price": target_price,
            },
        }

    order_side = "buy" if side == "long" else "sell"
    gateway = _kit_gateway(profile, environment)
    lev = gateway.set_leverage(inst_id, leverage, mgn_mode="isolated", profile=profile)
    if not lev.ok:
        return {
            "ok": False,
            "stage": "set_leverage",
            "error": lev.error,
            "leverage": {"ok": lev.ok, "argv": lev.argv, "data": lev.data, "error": lev.error},
        }
    order = OrderIntent(
        decision_id=f"{strategy_id}_{int(time.time() * 1000)}",
        inst_id=inst_id,
        side=order_side,
        size_contracts=float(size_contracts),
        order_type="market",
        timestamp=datetime.now(timezone.utc),
        profile=profile,
        leverage=float(leverage),
        metadata={
            "strategy_id": strategy_id,
            "environment": environment,
            "td_mode": "isolated",
            "attach_brackets": True,
            "target": _fmt(float(target_price)) if target_price is not None and _valid_number(target_price) else None,
            "stop": _fmt(float(stop_price)),
            "trigger_px_type": "mark",
            "source": "execution.bracket_entry",
        },
    )
    place = gateway.place_order(order)
    place_row = {"ok": place.ok, "argv": place.argv, "data": place.data, "error": place.error}
    lev_row = {"ok": lev.ok, "argv": lev.argv, "data": lev.data, "error": lev.error}
    if not place.ok:
        return {"ok": False, "stage": "place_entry_with_brackets", "error": place.error, "leverage": lev_row, "place": place_row}
    return {
        "ok": True,
        "stage": "placed",
        "source": "execution.bracket_entry",
        "leverage": lev_row,
        "place": place_row,
        "take_profit_attached": target_price is not None,
    }


def _kit_gateway(profile: str, environment: str) -> KitExecutionGateway:
    live_enabled = os.environ.get("LIVE_TRADING", "false").lower() == "true"
    return KitExecutionGateway(
        KitClient(KitClientConfig(default_profile=profile, live_enabled=live_enabled)),
        profile=profile,
        allow_live=live_enabled,
        environment=environment,
    )


def _valid_number(value: Any) -> bool:
    try:
        out = float(value)
    except Exception:
        return False
    return math.isfinite(out)


def _fmt(value: float) -> str:
    return f"{value:.12g}"
