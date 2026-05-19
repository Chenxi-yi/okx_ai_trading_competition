#!/usr/bin/env python3
"""Summarize live C-Auto ledger performance by regime, side, and signal family."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "engine" / "logs" / "c_auto_v2_micro_live"
OUT_ROOT = ROOT / "engine" / "data" / "research" / "c_auto_live_review"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze C-Auto live ledger by decision regime/side/family")
    p.add_argument("--state-id", default="micro_live_personal")
    p.add_argument("--environment", default="personal")
    p.add_argument("--start", default="")
    p.add_argument("--out-id", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    prefix = f"{args.state_id}_{args.environment}"
    ledger_path = LOG_DIR / f"{prefix}_ledger.jsonl"
    state_path = LOG_DIR / f"{prefix}.json"
    if not ledger_path.exists():
        raise FileNotFoundError(ledger_path)
    ledger = _read_jsonl(ledger_path)
    state = _read_json(state_path)
    trades = _pair_trades(ledger, args.start)
    out_dir = OUT_ROOT / (args.out_id or f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_df = pd.DataFrame(trades)
    if not trades_df.empty:
        trades_df.to_csv(out_dir / "paired_trades.csv", index=False)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger_path": str(ledger_path.relative_to(ROOT)),
        "state_path": str(state_path.relative_to(ROOT)) if state_path.exists() else None,
        "state_id": args.state_id,
        "environment": args.environment,
        "start": args.start,
        "overall": _summarize(trades_df),
        "by_side": _group(trades_df, ["side"]),
        "by_regime": _group(trades_df, ["regime"]),
        "by_family": _group(trades_df, ["signal_family"]),
        "by_regime_side": _group(trades_df, ["regime", "side"]),
        "by_family_side": _group(trades_df, ["signal_family", "side"]),
        "open_positions": _open_positions(state),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_markdown(summary))
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), **summary}, indent=2, sort_keys=True))
    return 0


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _pair_trades(ledger: list[dict[str, Any]], start: str) -> list[dict[str, Any]]:
    start_ts = pd.Timestamp(start, tz="UTC") if start else None
    open_by_symbol: dict[str, dict[str, Any]] = {}
    rows = []
    for event in ledger:
        ts = _ts(event.get("ts"))
        if start_ts is not None and ts is not None and ts < start_ts:
            continue
        name = str(event.get("event") or "").lower()
        symbol = str(event.get("symbol") or "")
        if not symbol:
            continue
        if name == "entry":
            open_by_symbol[symbol] = event
            continue
        if "exit" not in name and name not in {"manual_close", "flatten"}:
            continue
        pnl = _float(event.get("pnl"))
        if pnl is None:
            continue
        entry = open_by_symbol.pop(symbol, {})
        row = {
            "entry_ts": entry.get("ts"),
            "exit_ts": event.get("ts"),
            "symbol": symbol,
            "side": str(event.get("side") or entry.get("side") or ""),
            "regime": str(event.get("btc_regime_6") or entry.get("btc_regime_6") or entry.get("regime") or ""),
            "signal_family": str(event.get("signal_family") or entry.get("signal_family") or entry.get("reason") or ""),
            "exit_reason": str(event.get("reason") or name),
            "entry_price": _float(entry.get("entry_price") or entry.get("price")),
            "exit_price": _float(event.get("exit_price") or event.get("price")),
            "notional": _float(event.get("notional_usdt") or event.get("notional") or entry.get("notional_usdt") or entry.get("notional")),
            "pnl": pnl,
            "net_return": _float(event.get("net_return")),
        }
        rows.append(row)
    return rows


def _ts(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        return pd.Timestamp(value, tz="UTC") if pd.Timestamp(value).tzinfo is None else pd.Timestamp(value).tz_convert("UTC")
    except Exception:
        return None


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "pnl": 0.0, "avg_pnl": None}
    pnl = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
    wins = int((pnl > 0).sum())
    losses = int((pnl < 0).sum())
    return {
        "trades": int(len(df)),
        "wins": wins,
        "losses": losses,
        "win_rate": float(wins / len(df)) if len(df) else None,
        "pnl": float(pnl.sum()),
        "avg_pnl": float(pnl.mean()) if len(pnl) else None,
        "worst_pnl": float(pnl.min()) if len(pnl) else None,
        "best_pnl": float(pnl.max()) if len(pnl) else None,
    }


def _group(df: pd.DataFrame, cols: list[str]) -> list[dict[str, Any]]:
    if df.empty:
        return []
    rows = []
    for key, sample in df.groupby(cols, dropna=False):
        if not isinstance(key, tuple):
            key = (key,)
        row = {col: str(value) for col, value in zip(cols, key)}
        row.update(_summarize(sample))
        rows.append(row)
    rows.sort(key=lambda item: (float(item.get("pnl") or 0.0), -int(item.get("trades") or 0)))
    return rows


def _open_positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    positions = state.get("positions")
    if not isinstance(positions, dict):
        return []
    out = []
    for symbol, pos in positions.items():
        if isinstance(pos, dict):
            out.append(
                {
                    "symbol": symbol,
                    "side": pos.get("side"),
                    "signal_family": pos.get("signal_family") or pos.get("reason"),
                    "unrealized_pnl": _float(pos.get("unrealized_pnl")),
                    "entry_price": _float(pos.get("entry_price")),
                }
            )
    return out


def _markdown(summary: dict[str, Any]) -> str:
    lines = ["# C-Auto Live Performance Review", "", f"Generated: {summary['generated_at']}", ""]
    overall = summary["overall"]
    lines.append(
        "Overall: {trades} trades, win_rate={win_rate}, pnl={pnl:.4f}U".format(
            trades=overall.get("trades", 0),
            win_rate="n/a" if overall.get("win_rate") is None else f"{overall['win_rate']:.2%}",
            pnl=float(overall.get("pnl") or 0.0),
        )
    )
    for section, cols in (("by_regime_side", ["regime", "side"]), ("by_family_side", ["signal_family", "side"])):
        lines.extend(["", f"## {section}", "", "| " + " | ".join(cols) + " | Trades | Win | PnL |", "|---" * (len(cols) + 3) + "|"])
        for row in summary.get(section, []):
            keys = " | ".join(str(row.get(col) or "") for col in cols)
            win = "n/a" if row.get("win_rate") is None else f"{row['win_rate']:.1%}"
            lines.append(f"| {keys} | {row.get('trades')} | {win} | {float(row.get('pnl') or 0.0):.4f} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
