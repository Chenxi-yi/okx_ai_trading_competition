#!/usr/bin/env python3
"""Append reviewed ownership repair events.

This script never edits or deletes historical journal rows. It appends explicit
repair events that reconciliation can replay and audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from accounting import LiveOwnershipJournal  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append reviewed live ownership repair events")
    parser.add_argument("--environment", choices=["personal", "competition"], required=True)
    parser.add_argument("--okx-profile", default="")
    parser.add_argument("--action", choices=["external-exit", "adopt-orphan", "transfer-in", "transfer-out"], required=True)
    parser.add_argument("--inst-id", action="append", required=True)
    parser.add_argument("--reviewed-by", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--strategy-id", default="")
    parser.add_argument("--decision-id", default="")
    parser.add_argument("--side", default="")
    parser.add_argument("--filled-contracts", type=float, default=0.0)
    parser.add_argument("--fill-price", type=float, default=0.0)
    parser.add_argument("--source-environment", default="")
    parser.add_argument("--target-environment", default="")
    parser.add_argument("--metadata-json", default="{}")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    profile = args.okx_profile or ("live" if args.environment == "competition" else args.environment)
    metadata = _metadata(args.metadata_json)
    journal = LiveOwnershipJournal.from_engine_dir(ENGINE_DIR, args.environment, profile)
    written: list[str] = []
    for inst_id in args.inst_id:
        if args.action == "external-exit":
            path = journal.append_external_exit(
                inst_id=inst_id,
                reason=args.reason,
                reviewed_by=args.reviewed_by,
                decision_id=args.decision_id,
                strategy_id=args.strategy_id,
                metadata=metadata,
            )
        elif args.action == "adopt-orphan":
            path = journal.append_adoption(
                inst_id=inst_id,
                strategy_id=args.strategy_id,
                reason=args.reason,
                reviewed_by=args.reviewed_by,
                decision_id=args.decision_id,
                side=args.side,
                filled_contracts=args.filled_contracts,
                fill_price=args.fill_price,
                metadata=metadata,
            )
        else:
            direction = "in" if args.action == "transfer-in" else "out"
            path = journal.append_transfer(
                inst_id=inst_id,
                direction=direction,
                reason=args.reason,
                reviewed_by=args.reviewed_by,
                target_environment=args.target_environment,
                source_environment=args.source_environment,
                decision_id=args.decision_id,
                strategy_id=args.strategy_id,
                side=args.side,
                filled_contracts=args.filled_contracts,
                fill_price=args.fill_price,
                metadata=metadata,
            )
        written.append(str(path))
    payload = {
        "ok": True,
        "environment": args.environment,
        "okx_profile": profile,
        "action": args.action,
        "inst_ids": list(args.inst_id),
        "journal_paths": sorted(set(written)),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"ok: appended {args.action} for {len(args.inst_id)} instrument(s)")
    return 0


def _metadata(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --metadata-json: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--metadata-json must decode to an object")
    return data


if __name__ == "__main__":
    raise SystemExit(main())
