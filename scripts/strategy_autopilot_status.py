#!/usr/bin/env python3
"""Print current strategy autopilot status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AUTOPILOT_DIR = ROOT / "engine" / "research" / "autopilot"
PAPER_DIR = ROOT / "engine" / "logs" / "smartmoney_consensus_paper"


def main() -> int:
    latest = _read_json(AUTOPILOT_DIR / "latest.json")
    status = _read_json(AUTOPILOT_DIR / "status.json")
    print("Strategy Autopilot")
    if status:
        print(f"  updated_at: {status.get('updated_at')}")
        print(f"  last_run_id: {status.get('last_run_id')}")
        print(f"  summary: {status.get('summary')}")
    else:
        print("  status: no run yet")
    print("")
    print("Ranking")
    for idx, row in enumerate(latest.get("ranking") or [], start=1):
        print(
            f"  {idx}. {row.get('candidate_id')} "
            f"passed={row.get('passed')} return={_pct(row.get('total_return_pct'))} "
            f"dd={_pct(row.get('max_drawdown_pct'))} win={_pct(row.get('win_rate'))} trades={row.get('trades')}"
        )
    print("")
    print("Paper Shadows")
    for path in sorted(PAPER_DIR.glob("*_competition.json")):
        state = _read_json(path)
        if not state:
            continue
        print(
            f"  {path.stem}: ts={state.get('timestamp')} nav={_num(state.get('nav')):.4f} "
            f"realized={_num(state.get('realized_pnl')):.4f} unreal={_num(state.get('unrealized_pnl')):.4f} "
            f"positions={len(state.get('positions') or {})}"
        )
    if latest.get("live_promotion_requests"):
        print("")
        print("Live Requests")
        for req in latest["live_promotion_requests"]:
            print(f"  {req.get('candidate_id')}: {req.get('status')} ({req.get('requested_environment')})")
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _num(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _pct(value: Any) -> str:
    return f"{_num(value) * 100:.2f}%"


if __name__ == "__main__":
    raise SystemExit(main())
