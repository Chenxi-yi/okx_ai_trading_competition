#!/usr/bin/env python3
"""Build a training universe by excluding recently listed symbols."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ID = "rebuild_181_ohlcv_only_5m_15m_1h_4h_1d_20230101_20260507"
DEFAULT_SOURCE_UNIVERSE = ROOT / "engine/data/universe/okx_usdt_swap_ge2m_20260507/manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter a universe by listing age inferred from downloaded OHLCV records")
    parser.add_argument("--training-run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--source-universe", default=str(DEFAULT_SOURCE_UNIVERSE))
    parser.add_argument("--cutoff", required=True, help="Drop symbols whose first available bar is on/after this YYYY-MM-DD date")
    parser.add_argument("--output-id", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    training_dir = ROOT / "engine/data/training_history" / args.training_run_id
    progress_path = training_dir / "progress.jsonl"
    training_manifest_path = training_dir / "manifest.json"
    source_universe_path = Path(args.source_universe)
    if not source_universe_path.is_absolute():
        source_universe_path = ROOT / source_universe_path

    training_manifest = _read_json(training_manifest_path)
    source_universe = _read_json(source_universe_path)
    latest = _latest_records(progress_path)
    cutoff = str(args.cutoff)
    output_id = args.output_id or f"{source_universe.get('run_id', source_universe_path.parent.name)}_listed_before_{cutoff.replace('-', '')}"
    output_dir = ROOT / "engine/data/universe" / output_id
    output_dir.mkdir(parents=True, exist_ok=True)

    first_by_symbol: dict[str, str | None] = {}
    latest_status: dict[str, dict[str, str]] = {}
    for symbol in source_universe.get("symbols", []):
        firsts: list[str] = []
        latest_status[symbol] = {}
        for timeframe in training_manifest.get("timeframes", []):
            record = latest.get((symbol, timeframe))
            if not record:
                latest_status[symbol][timeframe] = "missing"
                continue
            latest_status[symbol][timeframe] = str(record.get("status") or "unknown")
            first_ts = record.get("first_ts")
            if first_ts:
                firsts.append(str(first_ts)[:10])
        first_by_symbol[symbol] = min(firsts) if firsts else None

    removed_recent = [
        {
            "symbol": symbol,
            "first_available_ts": first_by_symbol[symbol],
            "reason": f"first_available_ts >= {cutoff}",
        }
        for symbol in source_universe.get("symbols", [])
        if first_by_symbol.get(symbol) and str(first_by_symbol[symbol]) >= cutoff
    ]
    removed_unknown = [
        {"symbol": symbol, "first_available_ts": None, "reason": "no first_ts in training progress"}
        for symbol in source_universe.get("symbols", [])
        if not first_by_symbol.get(symbol)
    ]
    removed = removed_recent + removed_unknown
    removed_symbols = {item["symbol"] for item in removed}
    symbols = [symbol for symbol in source_universe.get("symbols", []) if symbol not in removed_symbols]

    manifest: dict[str, Any] = {
        "run_id": output_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_universe": _relpath(source_universe_path),
        "source_training_run_id": args.training_run_id,
        "source_training_path": _relpath(training_dir),
        "selection_rule": f"{source_universe.get('selection_rule', 'source universe')} + exclude first_available_ts >= {cutoff}",
        "listing_cutoff": cutoff,
        "start": training_manifest.get("start"),
        "end": training_manifest.get("end"),
        "timeframes": training_manifest.get("timeframes"),
        "count": len(symbols),
        "symbols": symbols,
        "inst_ids": [symbol.replace("/", "-") + "-SWAP" for symbol in symbols],
        "removed_count": len(removed),
        "removed_recent_count": len(removed_recent),
        "removed_unknown_count": len(removed_unknown),
        "removed": removed,
        "first_available_by_symbol": first_by_symbol,
        "latest_status_by_symbol": latest_status,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output_dir / "symbols.txt").write_text("\n".join(symbols) + "\n")
    (output_dir / "removed_recent.txt").write_text("\n".join(item["symbol"] for item in removed_recent) + "\n")
    print(json.dumps({"output_dir": _relpath(output_dir), "kept": len(symbols), "removed": len(removed)}, indent=2))
    return 0


def _latest_records(progress_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for line in progress_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        latest[(str(record.get("symbol")), str(record.get("timeframe")))] = record
    return latest


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
