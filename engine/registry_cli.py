#!/usr/bin/env python3
"""Command-line access to Strategy Office."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from registry import ParameterSet, PerformanceRecord, RiskBudget, StrategyRecord, StrategyRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description="Strategy Office registry CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List registered strategies")
    p_list.add_argument("--status")
    p_list.add_argument("--book")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show one strategy")
    p_show.add_argument("strategy_id")

    p_register = sub.add_parser("register-strategy", help="Register or update a strategy")
    p_register.add_argument("strategy_id")
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--book", required=True, choices=["core", "tactical", "speculative"])
    p_register.add_argument("--module", required=True)
    p_register.add_argument("--class-name", required=True)
    p_register.add_argument("--status", default="research", choices=["idea", "research", "backtest", "paper", "live", "paused", "retired"])
    p_register.add_argument("--version", default="1.0.0")
    p_register.add_argument("--default-parameter-set-id")
    p_register.add_argument("--risk-budget-json", required=True)
    p_register.add_argument("--description", default="")
    p_register.add_argument("--owner", default="personal")
    p_register.add_argument("--tags", default="", help="Comma-separated tags")
    p_register.add_argument("--notes", default="")
    p_register.add_argument("--live-enabled", action="store_true")
    p_register.add_argument("--live-allocation-pct", type=float, default=0.0)

    p_params = sub.add_parser("add-parameter-set", help="Add or update a parameter set")
    p_params.add_argument("strategy_id")
    p_params.add_argument("parameter_set_id")
    p_params.add_argument("--version", default="1")
    p_params.add_argument("--params-json", default="{}")
    p_params.add_argument("--params-file")
    p_params.add_argument("--notes", default="")
    p_params.add_argument("--parent-parameter-set-id")
    p_params.add_argument("--make-default", action="store_true")

    p_perf = sub.add_parser("add-performance", help="Add a performance record")
    p_perf.add_argument("strategy_id")
    p_perf.add_argument("parameter_set_id")
    p_perf.add_argument("--mode", required=True, choices=["backtest", "paper", "live", "stress", "monte_carlo"])
    p_perf.add_argument("--start", required=True)
    p_perf.add_argument("--end", required=True)
    p_perf.add_argument("--metrics-json", required=True)
    p_perf.add_argument("--costs-json", default="{}")
    p_perf.add_argument("--dataset-id")
    p_perf.add_argument("--journal-path")
    p_perf.add_argument("--notes", default="")

    p_reg_bt = sub.add_parser("register-backtest-result", help="Register a ProBacktestEngine artifact directory")
    p_reg_bt.add_argument("strategy_id")
    p_reg_bt.add_argument("parameter_set_id")
    p_reg_bt.add_argument("artifact_dir")
    p_reg_bt.add_argument("--dataset-id")
    p_reg_bt.add_argument("--notes", default="")

    p_promote = sub.add_parser("promote", help="Promote strategy status")
    p_promote.add_argument("strategy_id")
    p_promote.add_argument("to_status", choices=["idea", "research", "backtest", "paper", "live", "paused", "retired"])
    p_promote.add_argument("--reason", required=True)
    p_promote.add_argument("--evidence", default="", help="Comma-separated performance record ids")

    p_alloc = sub.add_parser("set-live-allocation", help="Set live allocation for a live strategy")
    p_alloc.add_argument("strategy_id")
    p_alloc.add_argument("--pct", type=float, required=True)
    p_alloc.add_argument("--enabled", action="store_true")

    args = parser.parse_args()
    reg = StrategyRegistry()

    try:
        if args.cmd == "list":
            records = reg.list_strategies(status=args.status, book=args.book)
            if args.json:
                print(json.dumps([r.to_dict() for r in records], indent=2, sort_keys=True))
            else:
                for r in records:
                    live = "LIVE" if r.live_enabled else "-"
                    print(f"{r.strategy_id:36s} {r.book:11s} {r.status:9s} alloc={r.live_allocation_pct:5.2%} {live}  {r.name}")
            return 0

        if args.cmd == "show":
            record = reg.get_strategy(args.strategy_id)
            payload = record.to_dict()
            payload["parameter_sets"] = [p.to_dict() for p in reg.parameter_sets_for(args.strategy_id)]
            payload["performance"] = [p.to_dict() for p in reg.performance_for(args.strategy_id)]
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.cmd == "register-strategy":
            if args.live_enabled and args.status != "live":
                raise ValueError("--live-enabled is only allowed when --status live")
            risk_budget = RiskBudget.from_dict(json.loads(args.risk_budget_json))
            tags = tuple(item.strip() for item in args.tags.split(",") if item.strip())
            record = StrategyRecord(
                strategy_id=args.strategy_id,
                name=args.name,
                book=args.book,
                status=args.status,
                version=args.version,
                module=args.module,
                class_name=args.class_name,
                default_parameter_set_id=args.default_parameter_set_id,
                risk_budget=risk_budget,
                live_enabled=bool(args.live_enabled),
                live_allocation_pct=max(0.0, float(args.live_allocation_pct)),
                description=args.description,
                owner=args.owner,
                tags=tags,
                notes=args.notes,
            )
            saved = reg.upsert_strategy(record)
            print(json.dumps(saved.to_dict(), indent=2, sort_keys=True))
            return 0

        if args.cmd == "add-parameter-set":
            params = _load_params(args.params_json, args.params_file)
            parameter_set = ParameterSet(
                parameter_set_id=args.parameter_set_id,
                strategy_id=args.strategy_id,
                version=args.version,
                params=params,
                notes=args.notes,
                parent_parameter_set_id=args.parent_parameter_set_id,
            )
            saved = reg.add_parameter_set(parameter_set, make_default=args.make_default)
            print(json.dumps(saved.to_dict(), indent=2, sort_keys=True))
            return 0

        if args.cmd == "add-performance":
            record = PerformanceRecord(
                record_id=f"perf-{uuid4()}",
                strategy_id=args.strategy_id,
                parameter_set_id=args.parameter_set_id,
                mode=args.mode,
                start=args.start,
                end=args.end,
                metrics=json.loads(args.metrics_json),
                costs=json.loads(args.costs_json),
                dataset_id=args.dataset_id,
                decision_journal_path=args.journal_path,
                notes=args.notes,
            )
            reg.add_performance(record)
            print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
            return 0

        if args.cmd == "register-backtest-result":
            artifact_dir = Path(args.artifact_dir)
            summary_path = artifact_dir / "summary.json"
            manifest_path = artifact_dir / "manifest.json"
            if not summary_path.exists():
                raise FileNotFoundError(f"Missing summary.json in {artifact_dir}")
            if not manifest_path.exists():
                raise FileNotFoundError(f"Missing manifest.json in {artifact_dir}")
            summary = json.loads(summary_path.read_text())
            manifest = json.loads(manifest_path.read_text())
            record = PerformanceRecord(
                record_id=f"perf-{uuid4()}",
                strategy_id=args.strategy_id,
                parameter_set_id=args.parameter_set_id,
                mode="backtest",
                start=str(manifest.get("start") or ""),
                end=str(manifest.get("end") or ""),
                metrics=summary,
                costs={
                    "total_fees_usdt": summary.get("total_fees_usdt", 0.0),
                    "total_funding_usdt": summary.get("total_funding_usdt", 0.0),
                },
                dataset_id=args.dataset_id,
                decision_journal_path=str(artifact_dir),
                notes=args.notes or f"Registered ProBacktest result {manifest.get('result_id', artifact_dir.name)}",
            )
            reg.add_performance(record)
            print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
            return 0

        if args.cmd == "promote":
            evidence = tuple(item.strip() for item in args.evidence.split(",") if item.strip())
            promotion = reg.promote(args.strategy_id, args.to_status, args.reason, evidence_record_ids=evidence)
            print(json.dumps(promotion.to_dict(), indent=2, sort_keys=True))
            return 0

        if args.cmd == "set-live-allocation":
            record = reg.set_live_allocation(args.strategy_id, args.pct, enabled=args.enabled)
            print(json.dumps(record.to_dict(), indent=2, sort_keys=True))
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1

    return 1


def _load_params(params_json: str, params_file: str | None) -> dict:
    if params_file:
        with Path(params_file).open() as f:
            return json.load(f)
    return json.loads(params_json)


if __name__ == "__main__":
    raise SystemExit(main())
