"""Append-only live ownership journal.

The live exchange is the source of truth for positions. This journal records
which strategy/decision created each exchange action so reconciliation can
rebuild ownership without trusting a strategy-local JSON cache.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from contracts import ApprovedTradePlan, CandidateTrade, ExecutionReceipt, ReconciliationSnapshot


JOURNAL_VERSION = 1


class LiveOwnershipJournal:
    """Records candidate -> plan -> execution -> reconciliation events."""

    def __init__(
        self,
        base_dir: Path | str,
        environment: str,
        okx_profile: str,
        file_name: str = "ownership.jsonl",
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.environment = str(environment)
        self.okx_profile = str(okx_profile)
        self.path = self.base_dir / self.environment / file_name
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_engine_dir(cls, engine_dir: Path | str, environment: str, okx_profile: str) -> "LiveOwnershipJournal":
        return cls(Path(engine_dir) / "logs" / "ownership", environment, okx_profile)

    def append_candidate(self, candidate: CandidateTrade, metadata: Mapping[str, Any] | None = None) -> Path:
        return self.append_event("candidate", {"candidate": candidate, "metadata": dict(metadata or {})})

    def append_plan(self, plan: ApprovedTradePlan, metadata: Mapping[str, Any] | None = None) -> Path:
        return self.append_event("approved_plan", {"plan": plan, "metadata": dict(metadata or {})})

    def append_execution(self, receipt: ExecutionReceipt, metadata: Mapping[str, Any] | None = None) -> Path:
        return self.append_event("execution", {"receipt": receipt, "metadata": dict(metadata or {})})

    def append_close(
        self,
        *,
        strategy_id: str,
        inst_id: str,
        decision_id: str = "",
        reason: str = "",
        result: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Path:
        return self.append_event(
            "close",
            {
                "strategy_id": strategy_id,
                "decision_id": decision_id,
                "inst_id": inst_id,
                "reason": reason,
                "result": dict(result or {}),
                "metadata": dict(metadata or {}),
            },
        )

    def append_reconciliation(self, snapshot: ReconciliationSnapshot) -> Path:
        return self.append_event("reconciliation", {"snapshot": snapshot})

    def append_event(self, event_type: str, payload: Mapping[str, Any]) -> Path:
        row = {
            "journal_version": JOURNAL_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": str(event_type),
            "environment": self.environment,
            "okx_profile": self.okx_profile,
            **dict(payload),
        }
        with self.path.open("a") as f:
            f.write(json.dumps(_to_jsonable(row), sort_keys=True, separators=(",", ":")) + "\n")
        return self.path

    def iter_events(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return ()
        return _iter_jsonl(self.path)

    def rebuild_open_ownership(self) -> dict[str, dict[str, Any]]:
        """Replay ownership records into an inst_id-indexed open position map."""

        open_by_inst: dict[str, dict[str, Any]] = {}
        plan_by_decision: dict[str, dict[str, Any]] = {}
        for event in self.iter_events():
            event_type = str(event.get("event") or "")
            if event_type == "approved_plan":
                plan = event.get("plan") if isinstance(event.get("plan"), dict) else {}
                decision_id = str(plan.get("decision_id") or "")
                if decision_id:
                    plan_by_decision[decision_id] = plan
                continue
            if event_type == "execution":
                receipt = event.get("receipt") if isinstance(event.get("receipt"), dict) else {}
                status = str(receipt.get("status") or "")
                if status not in {"filled", "partial", "submitted", "unknown"}:
                    continue
                inst_id = str(receipt.get("inst_id") or "")
                decision_id = str(receipt.get("decision_id") or "")
                if not inst_id:
                    continue
                plan = plan_by_decision.get(decision_id, {})
                candidate = plan.get("candidate") if isinstance(plan.get("candidate"), dict) else {}
                open_by_inst[inst_id] = {
                    "inst_id": inst_id,
                    "decision_id": decision_id,
                    "strategy_id": str(candidate.get("strategy_id") or ""),
                    "side": str(candidate.get("side") or ""),
                    "opened_at": receipt.get("filled_at") or receipt.get("submitted_at") or event.get("ts"),
                    "filled_contracts": _float(receipt.get("filled_contracts")),
                    "fill_price": _float(receipt.get("fill_price")),
                    "fee_usdt": _float(receipt.get("fee_usdt")),
                    "order_ids": receipt.get("order_ids") if isinstance(receipt.get("order_ids"), dict) else {},
                    "plan": plan,
                }
                continue
            if event_type in {"close", "external_exit"}:
                inst_id = str(event.get("inst_id") or "")
                if inst_id:
                    open_by_inst.pop(inst_id, None)
        return open_by_inst


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0
