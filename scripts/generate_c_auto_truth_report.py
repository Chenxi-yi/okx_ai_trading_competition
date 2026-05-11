#!/usr/bin/env python3
"""Generate a source-of-truth report for C-Auto paper and micro-live runs.

Paper data is reported as simulated execution, with local market-data freshness
checks. Micro-live data is reconciled from OKX private endpoints and should be
treated as the money source of truth.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"
LIVE_DIR = ENGINE_DIR / "logs" / "c_auto_v2_micro_live"
REPORT_DIR = ENGINE_DIR / "logs" / "truth_reports"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reconcile C-Auto paper state and OKX micro-live truth")
    p.add_argument("--paper-state-id", default="fixed1000_conservative")
    p.add_argument("--live-state-id", default="micro_live_competition")
    p.add_argument("--environment", default="competition")
    p.add_argument("--profile", default="live")
    p.add_argument("--since", default="", help="UTC ISO lower bound. Defaults to earliest local micro-live event.")
    p.add_argument("--limit", type=int, default=100)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    paper = _paper_report(args)
    live = _live_report(args)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truth_policy": {
            "paper": "simulation_only; execution prices come from local market-data snapshots and are not exchange fills",
            "micro_live": "OKX private endpoints are authoritative for fills, positions, fees, funding, realized PnL, and attached algo orders",
        },
        "paper": paper,
        "micro_live": live,
        "issues": _issues(paper, live),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORT_DIR / f"c_auto_truth_report_{stamp}.json"
    md_path = REPORT_DIR / f"c_auto_truth_report_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    md_path.write_text(_markdown(report) + "\n")
    print(json.dumps({"ok": True, "json": str(json_path), "markdown": str(md_path), "summary": report["micro_live"]["summary"]}, ensure_ascii=False, indent=2))
    return 0


def _paper_report(args: argparse.Namespace) -> dict[str, Any]:
    prefix = f"{args.paper_state_id}_{args.environment}"
    state = _read_json(PAPER_DIR / f"{prefix}.json")
    ledger = _read_jsonl(PAPER_DIR / f"{prefix}_ledger.jsonl")
    equity = _read_jsonl(PAPER_DIR / f"{prefix}_equity.jsonl")
    unique_ledger = _dedupe(ledger)
    exits = [row for row in unique_ledger if row.get("event") == "exit"]
    return {
        "source": "local_paper_simulation",
        "state_path": str(PAPER_DIR / f"{prefix}.json"),
        "ledger_path": str(PAPER_DIR / f"{prefix}_ledger.jsonl"),
        "state_available": bool(state),
        "timestamp": state.get("timestamp"),
        "nav": _num(state.get("nav")),
        "realized_pnl": _num(state.get("realized_pnl")),
        "unrealized_pnl": _num(state.get("unrealized_pnl")),
        "total_return": _num((state.get("metrics") or {}).get("total_return")),
        "max_drawdown": _num((state.get("metrics") or {}).get("max_drawdown")),
        "open_positions": state.get("positions") or {},
        "freshness": state.get("freshness") or {},
        "validation": {
            "execution_truth": "simulated_not_exchange",
            "market_data_freshness_passed": bool((state.get("freshness") or {}).get("passed")),
            "latest_market_ts": (state.get("freshness") or {}).get("latest_market_ts"),
            "fresh_symbols": (state.get("freshness") or {}).get("fresh_symbols"),
            "ledger_rows": len(ledger),
            "unique_ledger_events": len(unique_ledger),
            "equity_rows": len(equity),
        },
        "summary": {
            "entries": sum(1 for row in unique_ledger if row.get("event") == "entry"),
            "exits": len(exits),
            "wins": sum(1 for row in exits if (_num(row.get("pnl")) or 0.0) > 0),
            "losses": sum(1 for row in exits if (_num(row.get("pnl")) or 0.0) < 0),
            "exit_pnl_sum": sum(_num(row.get("pnl")) or 0.0 for row in exits),
        },
    }


def _live_report(args: argparse.Namespace) -> dict[str, Any]:
    prefix = f"{args.live_state_id}_{args.environment}"
    state = _read_json(LIVE_DIR / f"{prefix}.json")
    ledger = _dedupe(_read_jsonl(LIVE_DIR / f"{prefix}_ledger.jsonl"))
    since = _parse_ms(args.since) if args.since else _earliest_event_ms(ledger)
    local_symbols = sorted({str(row.get("symbol")) for row in ledger if row.get("symbol") and str(row.get("symbol")) != "None"})
    inst_ids = sorted({_symbol_to_inst_id(sym) for sym in local_symbols} | {str(pos.get("inst_id")) for pos in (state.get("positions") or {}).values() if pos.get("inst_id")})

    positions = _okx(args.profile, ["account", "positions", "--instType", "SWAP"])
    history = _okx(args.profile, ["account", "positions-history", "--instType", "SWAP", "--limit", str(args.limit)])
    bills = _okx(args.profile, ["account", "bills", "--instType", "SWAP", "--limit", str(args.limit)])
    open_positions = [_row for _row in _as_rows(positions) if _row.get("instId") in inst_ids and abs(_num(_row.get("pos")) or 0.0) > 0]
    closed_positions = [
        row for row in _as_rows(history)
        if row.get("instId") in inst_ids and row.get("mgnMode") == "isolated" and _ms(row.get("cTime")) >= since
    ]
    related_bills = [
        row for row in _as_rows(bills)
        if row.get("instId") in inst_ids and row.get("mgnMode") == "isolated" and _ms(row.get("ts")) >= since
    ]
    algos = {inst_id: _as_rows(_okx(args.profile, ["swap", "algo", "orders", "--instId", inst_id])) for inst_id in inst_ids}
    fills = {inst_id: _as_rows(_okx(args.profile, ["swap", "fills", "--instId", inst_id])) for inst_id in inst_ids}

    open_impact = sum((_num(row.get("upl")) or 0.0) + (_num(row.get("realizedPnl")) or 0.0) for row in open_positions)
    closed_realized = sum(_num(row.get("realizedPnl")) or 0.0 for row in closed_positions)
    bill_pnl_fee_funding = sum((_num(row.get("pnl")) or 0.0) + (_num(row.get("fee")) or 0.0) for row in related_bills)
    state_positions = state.get("positions") or {}
    return {
        "source": "okx_private_endpoints",
        "state_path": str(LIVE_DIR / f"{prefix}.json"),
        "state_available": bool(state),
        "since_ms": since,
        "since_utc": datetime.fromtimestamp(since / 1000, tz=timezone.utc).isoformat() if since else None,
        "local_symbols": local_symbols,
        "inst_ids": inst_ids,
        "summary": {
            "state_nav": _num(state.get("nav")),
            "state_realized_pnl": _num(state.get("realized_pnl")),
            "state_unrealized_pnl": _num(state.get("unrealized_pnl")),
            "okx_closed_realized_pnl": closed_realized,
            "okx_open_position_impact": open_impact,
            "okx_closed_plus_open_impact": closed_realized + open_impact,
            "okx_related_bill_pnl_fee_sum": bill_pnl_fee_funding,
            "open_positions": len(open_positions),
            "closed_positions": len(closed_positions),
            "related_bills": len(related_bills),
        },
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "related_bills": related_bills,
        "algo_orders": algos,
        "fills": fills,
        "validation": _live_validation(state_positions, open_positions, algos, fills),
        "raw_command_status": {
            "positions_ok": not isinstance(positions, dict) or "error" not in positions,
            "positions_history_ok": not isinstance(history, dict) or "error" not in history,
            "bills_ok": not isinstance(bills, dict) or "error" not in bills,
        },
    }


def _live_validation(state_positions: dict[str, Any], okx_positions: list[dict[str, Any]], algos: dict[str, list[dict[str, Any]]], fills: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    by_inst = {row.get("instId"): row for row in okx_positions}
    for symbol, pos in state_positions.items():
        inst_id = str(pos.get("inst_id") or _symbol_to_inst_id(symbol))
        okx_pos = by_inst.get(inst_id)
        live_algos = [row for row in algos.get(inst_id, []) if str(row.get("state") or "").lower() == "live"]
        fill_rows = fills.get(inst_id, [])
        local_contracts = abs(_num(pos.get("contracts")) or 0.0)
        okx_contracts = abs(_num((okx_pos or {}).get("pos")) or 0.0)
        local_entry = _num(pos.get("entry_price"))
        okx_entry = _num((okx_pos or {}).get("avgPx"))
        out.append({
            "symbol": symbol,
            "inst_id": inst_id,
            "position_exists_on_okx": bool(okx_pos),
            "contracts_match": bool(okx_pos) and abs(local_contracts - okx_contracts) < 1e-9,
            "local_contracts": local_contracts,
            "okx_contracts": okx_contracts,
            "entry_price_match": bool(local_entry and okx_entry) and abs(local_entry - okx_entry) <= max(abs(okx_entry) * 1e-8, 1e-12),
            "local_entry_price": local_entry,
            "okx_avg_px": okx_entry,
            "live_algo_count": len(live_algos),
            "has_live_stop": any(bool(row.get("slTriggerPx")) for row in live_algos),
            "has_live_take_profit": any(bool(row.get("tpTriggerPx")) for row in live_algos),
            "fill_count": len(fill_rows),
        })
    return out


def _issues(paper: dict[str, Any], live: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not paper.get("validation", {}).get("market_data_freshness_passed"):
        issues.append("paper market-data freshness gate is not passing")
    for item in live.get("validation", []):
        if not item.get("position_exists_on_okx"):
            issues.append(f"{item['inst_id']} exists locally but not on OKX")
        if item.get("position_exists_on_okx") and not item.get("contracts_match"):
            issues.append(f"{item['inst_id']} local contracts do not match OKX")
        if item.get("position_exists_on_okx") and not item.get("entry_price_match"):
            issues.append(f"{item['inst_id']} local entry price does not match OKX avgPx")
        if item.get("position_exists_on_okx") and not item.get("has_live_stop"):
            issues.append(f"{item['inst_id']} has no live OKX stop algo")
        if item.get("position_exists_on_okx") and not item.get("has_live_take_profit"):
            issues.append(f"{item['inst_id']} has no live OKX take-profit algo")
    return issues


def _markdown(report: dict[str, Any]) -> str:
    paper = report["paper"]
    live = report["micro_live"]
    lines = [
        "# C-Auto Truth Report",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Paper",
        "",
        f"- Source: `{paper['source']}`",
        f"- Timestamp: `{paper.get('timestamp')}`",
        f"- NAV: `{paper.get('nav')}`",
        f"- Realized PnL: `{paper.get('realized_pnl')}`",
        f"- Unrealized PnL: `{paper.get('unrealized_pnl')}`",
        f"- Return: `{paper.get('total_return')}`",
        f"- Freshness passed: `{paper.get('validation', {}).get('market_data_freshness_passed')}`",
        f"- Unique ledger events: `{paper.get('validation', {}).get('unique_ledger_events')}`",
        "",
        "## Micro Live",
        "",
        f"- Source: `{live['source']}`",
        f"- Since: `{live.get('since_utc')}`",
        f"- State NAV: `{live['summary'].get('state_nav')}`",
        f"- OKX closed realized PnL: `{live['summary'].get('okx_closed_realized_pnl')}`",
        f"- OKX open impact: `{live['summary'].get('okx_open_position_impact')}`",
        f"- OKX closed + open impact: `{live['summary'].get('okx_closed_plus_open_impact')}`",
        f"- Open positions: `{live['summary'].get('open_positions')}`",
        f"- Closed positions: `{live['summary'].get('closed_positions')}`",
        "",
        "## Validation",
        "",
    ]
    for item in live.get("validation", []):
        lines.append(
            f"- `{item['inst_id']}` position={item['position_exists_on_okx']} contracts_match={item['contracts_match']} "
            f"entry_match={item['entry_price_match']} stop={item['has_live_stop']} tp={item['has_live_take_profit']}"
        )
    lines.extend(["", "## Issues", ""])
    if report.get("issues"):
        lines.extend(f"- {issue}" for issue in report["issues"])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _okx(profile: str, args: list[str]) -> Any:
    cmd = ["okx", "--profile", profile, "--json"] + args
    last_error = ""
    for attempt in range(3):
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=45)
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout) if proc.stdout.strip() else []
            except Exception:
                return {"error": "json_parse_failed", "stdout": proc.stdout[-2000:], "argv": cmd}
        last_error = proc.stderr.strip() or proc.stdout.strip()
        time.sleep(0.5 * (attempt + 1))
    return {"error": last_error, "argv": cmd}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if "error" in value:
            return []
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _ms(value: Any) -> int:
    try:
        return int(float(value))
    except Exception:
        return 0


def _parse_ms(value: str) -> int:
    if value.isdigit():
        return int(value)
    text = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def _earliest_event_ms(ledger: list[dict[str, Any]]) -> int:
    values = []
    for row in ledger:
        ts = str(row.get("ts") or "")
        if not ts:
            continue
        try:
            values.append(int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000))
        except Exception:
            pass
    return min(values) if values else 0


def _symbol_to_inst_id(symbol: str) -> str:
    return symbol.replace("/USDT", "").replace(":USDT", "").replace("/", "-") + "-USDT-SWAP"


if __name__ == "__main__":
    raise SystemExit(main())
