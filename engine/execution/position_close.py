"""Position close execution helpers owned by the execution layer."""

from __future__ import annotations

import os
from typing import Any

from kit import KitClient, KitClientConfig, KitExecutionGateway


def close_position_via_kit(
    *,
    inst_id: str,
    profile: str,
    environment: str,
    mgn_mode: str,
    pos_side: str = "net",
    dry_run: bool = False,
    cancel_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(dry_run):
        row: dict[str, Any] = {
            "dry_run": True,
            "source": "execution.position_close",
            "action": "close_position",
            "inst_id": inst_id,
            "profile": profile,
        }
        if cancel_probe is not None:
            row["cancel_probe"] = cancel_probe
        return row
    gateway = _kit_gateway(profile, environment)
    result = gateway.close_position(inst_id, mgn_mode=mgn_mode, pos_side=pos_side, profile=profile)
    row = {
        "source": "execution.position_close",
        "ok": result.ok,
        "argv": result.argv,
        "data": result.data,
        "error": result.error,
    }
    if cancel_probe is not None:
        row["cancel_probe"] = cancel_probe
    return row


def _kit_gateway(profile: str, environment: str) -> KitExecutionGateway:
    live_enabled = os.environ.get("LIVE_TRADING", "false").lower() == "true"
    return KitExecutionGateway(
        KitClient(KitClientConfig(default_profile=profile, live_enabled=live_enabled)),
        profile=profile,
        allow_live=live_enabled,
        environment=environment,
    )
