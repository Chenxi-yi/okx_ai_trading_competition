#!/usr/bin/env python3
"""Autonomous research/backtest/paper-promotion orchestrator.

This script is intentionally conservative:
- It may run local research and backtests.
- It may mark candidates as paper-ready.
- It never enables live trading or places orders.
- Live promotion is written as a request that requires owner approval.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
DEFAULT_POLICY = ENGINE_DIR / "config" / "strategy_autopilot_policy.json"
OUT_DIR = ENGINE_DIR / "research" / "autopilot"
PROMOTION_REQUESTS = OUT_DIR / "promotion_requests.jsonl"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run automated strategy research pipeline")
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--run-id", default="")
    p.add_argument("--max-backtests", type=int, default=1)
    p.add_argument("--skip-research-queue", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=3600.0)
    p.add_argument("--max-cycles", type=int, default=0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cycles = 0
    while True:
        cycles += 1
        _run_once(args)
        if not args.loop or (args.max_cycles > 0 and cycles >= args.max_cycles):
            break
        time.sleep(max(60.0, float(args.interval_sec)))
    return 0


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    policy = _read_json(Path(args.policy))
    run_id = args.run_id or "autopilot_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": _now(),
        "policy_id": policy.get("policy_id"),
        "dry_run": bool(args.dry_run),
        "steps": [],
        "candidates": [],
        "paper_promotions": [],
        "live_promotion_requests": [],
    }

    if not args.skip_research_queue:
        report["steps"].append(_run_research_queue(args.dry_run))

    enabled = [c for c in policy.get("candidates", []) if c.get("enabled")]
    if args.max_backtests > 0:
        enabled = enabled[: args.max_backtests]

    for candidate in enabled:
        result = _run_candidate(candidate, run_id, policy, args.dry_run)
        report["candidates"].append(result)
        if result.get("backtest_gate", {}).get("passed"):
            paper_item = _mark_paper_ready(candidate, result, run_id, args.dry_run)
            report["paper_promotions"].append(paper_item)
            request = _maybe_live_request(candidate, result, policy, run_id, args.dry_run)
            if request:
                report["live_promotion_requests"].append(request)

    report["ranking"] = _rank_candidates(report["candidates"])
    report["summary"] = {
        "candidates_run": len(report["candidates"]),
        "paper_ready": len(report["paper_promotions"]),
        "live_requests": len(report["live_promotion_requests"]),
        "blocked": [c["candidate_id"] for c in report["candidates"] if not c.get("backtest_gate", {}).get("passed")],
    }
    report_path = OUT_DIR / f"{run_id}.json"
    md_path = OUT_DIR / f"{run_id}.md"
    if not args.dry_run:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        md_path.write_text(_markdown(report) + "\n")
        (OUT_DIR / "latest.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        (OUT_DIR / "latest.md").write_text(_markdown(report) + "\n")
        (OUT_DIR / "status.json").write_text(json.dumps({"running": bool(args.loop), "updated_at": _now(), "last_run_id": run_id, "summary": report["summary"]}, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "report": str(report_path), "markdown": str(md_path), "summary": report["summary"]}, indent=2, sort_keys=True))
    return report


def _run_research_queue(dry_run: bool) -> dict[str, Any]:
    cmd = ["python3", "scripts/run_c_auto_research_queue.py", "--limit", "4"]
    if dry_run:
        cmd.append("--dry-run")
    return _run_step("research_queue", cmd, dry_run=False)


def _run_candidate(candidate: dict[str, Any], autopilot_run_id: str, policy: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    run_id = f"{autopilot_run_id}_{candidate_id}"
    command = [str(part).format(run_id=run_id, candidate_id=candidate_id) for part in candidate.get("command", [])]
    manifest_path = ROOT / str(candidate.get("result_manifest", "")).format(run_id=run_id)
    step = _run_step(candidate_id, command, dry_run=dry_run)
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    metrics = _extract_metrics(candidate_id, manifest)
    gate = _evaluate_backtest_gate(metrics, (policy.get("gates") or {}).get("backtest_to_paper", {}))
    return {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "command": command,
        "step": step,
        "manifest_path": str(manifest_path.relative_to(ROOT)),
        "metrics": metrics,
        "backtest_gate": gate,
    }


def _run_step(name: str, cmd: list[str], dry_run: bool) -> dict[str, Any]:
    started = _now()
    if dry_run:
        return {"name": name, "status": "dry_run", "command": cmd, "started_at": started, "completed_at": _now()}
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    return {
        "name": name,
        "status": "pass" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "command": cmd,
        "started_at": started,
        "completed_at": _now(),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def _extract_metrics(candidate_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("metrics") or {}
    if candidate_id.startswith("smartmoney"):
        return {
            "total_return_pct": _num(raw.get("total_return_pct")),
            "max_drawdown_pct": abs(_num(raw.get("max_drawdown_pct"))),
            "trades": int(_num(raw.get("trades"))),
            "win_rate": _num(raw.get("win_rate")),
            "avg_net_return": _num(raw.get("avg_net_return")),
            "final_nav": _num(raw.get("final_nav")),
            "total_pnl": _num(raw.get("total_pnl")),
        }
    return {
        "total_return_pct": _num(raw.get("total_return") or raw.get("total_return_pct")),
        "max_drawdown_pct": abs(_num(raw.get("max_drawdown") or raw.get("max_drawdown_pct"))),
        "trades": int(_num(raw.get("trades") or raw.get("trade_count"))),
        "win_rate": _num(raw.get("win_rate") or raw.get("win_rate_events")),
        "avg_net_return": _num(raw.get("avg_net_return")),
        "final_nav": _num(raw.get("final_nav")),
        "total_pnl": _num(raw.get("total_pnl") or raw.get("pnl")),
    }


def _evaluate_backtest_gate(metrics: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    checks = [
        _check("min_trades", metrics.get("trades"), gate.get("min_trades", 0), metrics.get("trades", 0) >= gate.get("min_trades", 0)),
        _check("min_total_return_pct", metrics.get("total_return_pct"), gate.get("min_total_return_pct", 0.0), metrics.get("total_return_pct", 0.0) >= gate.get("min_total_return_pct", 0.0)),
        _check("min_win_rate", metrics.get("win_rate"), gate.get("min_win_rate", 0.0), metrics.get("win_rate", 0.0) >= gate.get("min_win_rate", 0.0)),
        _check("min_avg_net_return", metrics.get("avg_net_return"), gate.get("min_avg_net_return", 0.0), metrics.get("avg_net_return", 0.0) >= gate.get("min_avg_net_return", 0.0)),
        _check("max_drawdown_pct", metrics.get("max_drawdown_pct"), gate.get("max_drawdown_pct", 1.0), metrics.get("max_drawdown_pct", 1.0) <= gate.get("max_drawdown_pct", 1.0)),
    ]
    failed = [item["name"] for item in checks if not item["passed"]]
    return {"passed": not failed, "checks": checks, "failed_checks": failed}


def _mark_paper_ready(candidate: dict[str, Any], result: dict[str, Any], run_id: str, dry_run: bool) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    paper_command = [str(part).format(run_id=run_id, candidate_id=candidate_id) for part in candidate.get("paper_command", [])]
    paper_step = None
    if paper_command:
        paper_step = _run_step(f"{candidate['candidate_id']}_paper_shadow", paper_command, dry_run=dry_run)
    item = {
        "created_at": _now(),
        "autopilot_run_id": run_id,
        "candidate_id": candidate["candidate_id"],
        "source_backtest": result["manifest_path"],
        "status": "paper_ready",
        "next_action": "continue isolated paper observation; do not trade live yet",
        "metrics": result["metrics"],
        "paper_command": paper_command,
        "paper_step": paper_step,
    }
    if not dry_run:
        _append_jsonl(OUT_DIR / "paper_ready.jsonl", item)
    return item


def _maybe_live_request(candidate: dict[str, Any], result: dict[str, Any], policy: dict[str, Any], run_id: str, dry_run: bool) -> dict[str, Any] | None:
    if not result.get("backtest_gate", {}).get("passed"):
        return None
    request = {
        "created_at": _now(),
        "autopilot_run_id": run_id,
        "candidate_id": candidate["candidate_id"],
        "status": "requires_owner_approval",
        "requested_environment": "paper_shadow_first",
        "reason": "backtest gate passed; live remains blocked until paper_to_live_request gate and owner approval pass",
        "metrics": result["metrics"],
    }
    if not dry_run:
        _append_jsonl(PROMOTION_REQUESTS, request)
    return request


def _rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for item in candidates:
        m = item.get("metrics") or {}
        gate = item.get("backtest_gate") or {}
        score = (
            100.0 * _num(m.get("total_return_pct"))
            + 10.0 * _num(m.get("avg_net_return"))
            + 2.0 * _num(m.get("win_rate"))
            - 5.0 * _num(m.get("max_drawdown_pct"))
        )
        if not gate.get("passed"):
            score -= 100.0
        ranked.append(
            {
                "candidate_id": item.get("candidate_id"),
                "score": score,
                "passed": bool(gate.get("passed")),
                "total_return_pct": m.get("total_return_pct"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
                "win_rate": m.get("win_rate"),
                "trades": m.get("trades"),
            }
        )
    return sorted(ranked, key=lambda row: float(row.get("score") or 0.0), reverse=True)


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Strategy Autopilot Report",
        "",
        f"- run_id: `{report['run_id']}`",
        f"- generated_at: `{report['generated_at']}`",
        f"- policy_id: `{report.get('policy_id')}`",
        "",
        "## Candidates",
        "",
        "| candidate | passed | return | max_dd | win_rate | trades | failed_checks |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in report["candidates"]:
        m = item.get("metrics") or {}
        g = item.get("backtest_gate") or {}
        lines.append(
            f"| `{item['candidate_id']}` | {g.get('passed')} | {_pct(m.get('total_return_pct'))} | "
            f"{_pct(m.get('max_drawdown_pct'))} | {_pct(m.get('win_rate'))} | {m.get('trades', 0)} | "
            f"{', '.join(g.get('failed_checks') or []) or '-'} |"
        )
    lines.extend(["", "## Decisions", ""])
    if report["paper_promotions"]:
        for item in report["paper_promotions"]:
            lines.append(f"- `{item['candidate_id']}` marked `paper_ready`; live is still blocked.")
    else:
        lines.append("- No candidate passed the backtest-to-paper gate.")
    lines.extend(["", "## Ranking", "", "| rank | candidate | score | passed | return | max_dd | win_rate | trades |", "|---:|---|---:|---:|---:|---:|---:|---:|"])
    for idx, item in enumerate(report.get("ranking") or [], start=1):
        lines.append(
            f"| {idx} | `{item['candidate_id']}` | {_num(item.get('score')):.3f} | {item.get('passed')} | "
            f"{_pct(item.get('total_return_pct'))} | {_pct(item.get('max_drawdown_pct'))} | "
            f"{_pct(item.get('win_rate'))} | {item.get('trades', 0)} |"
        )
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _check(name: str, actual: Any, required: Any, passed: bool) -> dict[str, Any]:
    return {"name": name, "actual": actual, "required": required, "passed": bool(passed)}


def _num(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return 0.0
    return out if math.isfinite(out) else 0.0


def _pct(value: Any) -> str:
    return f"{_num(value) * 100:.2f}%"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
