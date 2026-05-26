#!/usr/bin/env python3
"""Summarize candidate strategy evidence without changing runtime registration."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    payload = {
        "generated_from": str(ROOT),
        "strategies": [
            btc_daily(),
            btc_weekly(),
            trend_pullback(),
            c_auto_signal_family("deriv_crowding_reversal", "Crowding Reversal"),
            c_auto_signal_family("daily_fib_support_rebound_long", "Daily Fib Support Rebound"),
            c_auto_signal_family("deriv_oi_compression_breakout", "OI Compression Breakout"),
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def btc_daily() -> dict[str, Any]:
    path = ROOT / "engine/data/research/btc_daily_swing/btc_daily_swing_breakout80_2x_20260515/summary.json"
    data = read_json(path)
    return {
        "id": "btc_daily_breakout_swing",
        "name": "BTC Daily Breakout Swing",
        "evidence_type": "research_backtest",
        "source": rel(path),
        "registered_in_strategy_registry": True,
        "status": "paper_registered",
        "metrics": select(data, "start", "end", "total_return", "annualized_return", "max_drawdown", "sharpe_like", "trades", "win_rate"),
        "sample_warning": "low_trade_count" if float(data.get("trades") or 0) < 30 else "",
    }


def btc_weekly() -> dict[str, Any]:
    path = ROOT / "engine/data/research/btc_weekly_swing_scalein/btc_weekly_swing_scalein_return_20260515/summary.json"
    data = read_json(path)
    return {
        "id": "btc_weekly_swing_3x",
        "name": "BTC Weekly Swing 3x",
        "evidence_type": "research_backtest",
        "source": rel(path),
        "registered_in_strategy_registry": True,
        "status": "paper_registered",
        "metrics": select(data, "start", "end", "total_return", "annualized_return", "max_drawdown", "sharpe_like", "trades", "win_rate"),
        "sample_warning": "very_low_trade_count" if float(data.get("trades") or 0) < 10 else "",
    }


def trend_pullback() -> dict[str, Any]:
    registry = read_json(ROOT / "engine/config/strategy_registry.json")
    rows = []
    for perf in registry.get("performance", []) if isinstance(registry, dict) else []:
        strategy_id = str(perf.get("strategy_id") or "")
        if not strategy_id.startswith("trend_pullback_reversal_"):
            continue
        rows.append(
            {
                "id": strategy_id,
                "parameter_set_id": perf.get("parameter_set_id"),
                "source": perf.get("decision_journal_path"),
                "start": perf.get("start"),
                "end": perf.get("end"),
                **dict(perf.get("metrics") or {}),
            }
        )
    rows.sort(key=lambda row: str(row.get("id")))
    return {
        "id": "trend_pullback_reversal",
        "name": "Trend Pullback Reversal",
        "evidence_type": "path_simulated_backtest",
        "registered_in_strategy_registry": True,
        "status": "three_live_competition_variants_registered",
        "variants": rows,
    }


def c_auto_signal_family(strategy_id: str, name: str) -> dict[str, Any]:
    live_review = read_json(ROOT / "engine/data/research/c_auto_live_review/c_auto_live_personal_20260516/summary.json")
    live_rows = []
    for key in ("by_family_side", "by_regime_side"):
        rows = live_review.get(key) if isinstance(live_review, dict) else None
        if isinstance(rows, list):
            live_rows.extend(row for row in rows if str(row.get("signal_family") or "") == strategy_id)
    ledger_rows = family_from_ledgers(strategy_id)
    candidate_rows = family_from_candidate_history(strategy_id)
    return {
        "id": strategy_id,
        "name": name,
        "evidence_type": "committee_signal_family",
        "registered_in_strategy_registry": False,
        "status": "not_independent_runtime_strategy",
        "live_review_rows": live_rows,
        "ledger_summary": ledger_rows,
        "candidate_history_summary": candidate_rows,
    }


def family_from_ledgers(strategy_id: str) -> dict[str, Any]:
    paths = [
        ROOT / "engine/logs/c_auto_v2_paper/fixed1000_conservative_personal_ledger.jsonl",
        ROOT / "engine/logs/c_auto_v2_micro_live/micro_live_personal_personal_ledger.jsonl",
        ROOT / "engine/logs/c_auto_v2_micro_live/micro_live_competition_competition_ledger.jsonl",
    ]
    entries: dict[str, dict[str, Any]] = {}
    exits_by_source = defaultdict(list)
    for path in paths:
        source = rel(path)
        for row in iter_jsonl(path):
            if row.get("event") == "entry" and str(row.get("reason") or "") == strategy_id:
                key = f"{source}|{row.get('decision_id') or row.get('symbol')}|{row.get('ts')}"
                entries[key] = row | {"source": source}
            elif row.get("event") == "exit":
                exits_by_source[source].append(row)
    matched = []
    for entry in entries.values():
        source = entry["source"]
        symbol = entry.get("symbol")
        entry_ts = str(entry.get("ts") or "")
        exit_row = next(
            (
                row
                for row in exits_by_source[source]
                if row.get("symbol") == symbol and str(row.get("ts") or "") >= entry_ts
            ),
            None,
        )
        if exit_row:
            matched.append(exit_row | {"entry_ts": entry_ts, "source": source, "entry_side": entry.get("side")})
    pnl = sum(float(row.get("pnl") or 0.0) for row in matched)
    wins = sum(1 for row in matched if float(row.get("pnl") or 0.0) > 0)
    return {
        "entries": len(entries),
        "closed_matches": len(matched),
        "wins": wins,
        "win_rate": wins / len(matched) if matched else None,
        "pnl": pnl,
        "sources": sorted({row["source"] for row in entries.values()} | {row["source"] for row in matched}),
    }


def family_from_candidate_history(strategy_id: str) -> dict[str, Any]:
    paths = [
        ROOT / "engine/logs/c_auto_v2_micro_live/micro_live_personal_personal_candidate_history.jsonl",
        ROOT / "engine/logs/c_auto_v2_micro_live/micro_live_competition_competition_candidate_history.jsonl",
    ]
    total = 0
    eligible = 0
    scans = 0
    for path in paths:
        for row in iter_jsonl(path):
            scans += 1
            for candidate in row.get("candidates", []) if isinstance(row.get("candidates"), list) else []:
                family = str(candidate.get("signal_family") or "")
                is_family = family == strategy_id
                if strategy_id == "daily_fib_support_rebound_long":
                    is_family = bool(candidate.get("daily_fib_eligible"))
                if not is_family:
                    continue
                total += 1
                if candidate.get("eligible"):
                    eligible += 1
    return {"scans": scans, "candidates": total, "eligible_candidates": eligible}


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def iter_jsonl(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            yield row


def select(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: data.get(key) for key in keys}


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
