"""Instrument metadata snapshots for OKX USDT swaps."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from contracts import InstrumentSpec
except ModuleNotFoundError:
    from engine.contracts import InstrumentSpec


def inst_id_from_symbol(symbol: str) -> str:
    if symbol.endswith("-USDT-SWAP"):
        return symbol
    return symbol.replace("/", "-").replace(":USDT", "") + "-SWAP"


def symbol_from_inst_id(inst_id: str) -> str:
    return inst_id.replace("-USDT-SWAP", "/USDT")


def instrument_from_ccxt_market(symbol: str, market: Mapping[str, Any], timestamp: datetime | None = None) -> InstrumentSpec:
    info = dict(market.get("info") or {})
    inst_id = str(info.get("instId") or inst_id_from_symbol(symbol))
    limits = dict(market.get("limits") or {})
    amount_limits = dict(limits.get("amount") or {})
    precision = dict(market.get("precision") or {})
    return InstrumentSpec(
        inst_id=inst_id,
        symbol=symbol.split(":")[0],
        ct_val=float(info.get("ctVal") or market.get("contractSize") or 1.0),
        lot_sz=float(info.get("lotSz") or precision.get("amount") or 1.0),
        min_sz=float(info.get("minSz") or amount_limits.get("min") or 1.0),
        tick_sz=float(info["tickSz"]) if info.get("tickSz") else None,
        max_mkt_sz=float(info["maxMktSz"]) if info.get("maxMktSz") else None,
        max_leverage=float(info["lever"]) if info.get("lever") else None,
        active=bool(market.get("active", True)),
        source="ccxt_okx_market",
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def write_instrument_snapshot(instruments: Mapping[str, InstrumentSpec], path: Path | str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instruments": {key: _instrument_to_dict(value) for key, value in sorted(instruments.items())},
    }
    out.write_text(json.dumps(payload, indent=2, sort_keys=True))


def read_instrument_snapshot(path: Path | str) -> dict[str, InstrumentSpec]:
    payload = json.loads(Path(path).read_text())
    return {
        key: _instrument_from_dict(value)
        for key, value in payload.get("instruments", {}).items()
    }


def _instrument_to_dict(spec: InstrumentSpec) -> dict[str, Any]:
    return {
        "inst_id": spec.inst_id,
        "symbol": spec.symbol,
        "ct_val": spec.ct_val,
        "lot_sz": spec.lot_sz,
        "min_sz": spec.min_sz,
        "tick_sz": spec.tick_sz,
        "max_mkt_sz": spec.max_mkt_sz,
        "max_leverage": spec.max_leverage,
        "active": spec.active,
        "source": spec.source,
        "timestamp": spec.timestamp.isoformat() if spec.timestamp else None,
    }


def _instrument_from_dict(data: Mapping[str, Any]) -> InstrumentSpec:
    ts = data.get("timestamp")
    return InstrumentSpec(
        inst_id=str(data["inst_id"]),
        symbol=str(data["symbol"]),
        ct_val=float(data["ct_val"]),
        lot_sz=float(data["lot_sz"]),
        min_sz=float(data["min_sz"]),
        tick_sz=float(data["tick_sz"]) if data.get("tick_sz") is not None else None,
        max_mkt_sz=float(data["max_mkt_sz"]) if data.get("max_mkt_sz") is not None else None,
        max_leverage=float(data["max_leverage"]) if data.get("max_leverage") is not None else None,
        active=bool(data.get("active", True)),
        source=str(data.get("source", "snapshot")),
        timestamp=datetime.fromisoformat(ts) if ts else None,
    )
