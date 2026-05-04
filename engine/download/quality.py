"""Quality summaries for data download runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any


def build_training_history_quality(
    run_dir: Path,
    cache_dir: Path,
    manifest: dict[str, Any],
    progress: list[dict[str, Any]],
) -> dict[str, Any]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for record in progress:
        symbol = str(record.get("symbol", ""))
        timeframe = str(record.get("timeframe", ""))
        if symbol and timeframe:
            latest[(symbol, timeframe)] = record

    symbols = [str(symbol) for symbol in manifest.get("symbols", [])]
    timeframes = [str(timeframe) for timeframe in manifest.get("timeframes") or [manifest.get("timeframe") or "1h"]]
    expected_jobs = [(symbol, timeframe) for timeframe in timeframes for symbol in symbols]
    rows = 0
    ok_jobs = 0
    failed_jobs = 0
    missing_jobs: list[dict[str, str]] = []
    missing_cache_files: list[dict[str, str]] = []
    failed_records: list[dict[str, Any]] = []
    coverages: list[float] = []
    low_coverage_jobs: list[dict[str, Any]] = []
    first_ts: list[str] = []
    last_ts: list[str] = []

    for symbol, timeframe in expected_jobs:
        record = latest.get((symbol, timeframe))
        if not record:
            missing_jobs.append({"symbol": symbol, "timeframe": timeframe})
            continue
        if record.get("status") == "ok":
            ok_jobs += 1
            rows += int(record.get("rows") or 0)
            coverage = _float_or_none(record.get("coverage"))
            if coverage is not None:
                coverages.append(coverage)
                if coverage < 0.8:
                    low_coverage_jobs.append({"symbol": symbol, "timeframe": timeframe, "coverage": round(coverage, 6)})
            if record.get("first_ts"):
                first_ts.append(str(record["first_ts"]))
            if record.get("last_ts"):
                last_ts.append(str(record["last_ts"]))
            cache_path = cache_dir / _cache_filename(symbol, timeframe)
            if not cache_path.exists():
                missing_cache_files.append({"symbol": symbol, "timeframe": timeframe, "path": str(cache_path)})
        elif record.get("status") == "failed":
            failed_jobs += 1
            failed_records.append(record)

    total_jobs = len(expected_jobs) or int(manifest.get("summary", {}).get("total_jobs") or 0)
    missing_count = len(missing_jobs)
    status = str(manifest.get("status") or "")
    if status != "completed":
        validation_status = "running" if status == "running" else "created"
    elif failed_jobs or missing_count or missing_cache_files:
        validation_status = "warn"
    elif ok_jobs == total_jobs:
        validation_status = "ok"
    else:
        validation_status = "warn"

    return {
        "run_id": manifest.get("run_id") or run_dir.name,
        "dataset_type": "training_history",
        "validation_status": validation_status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "total_jobs": total_jobs,
        "ok_jobs": ok_jobs,
        "failed_jobs": failed_jobs,
        "missing_jobs": missing_count,
        "missing_cache_files": len(missing_cache_files),
        "low_coverage_jobs": len(low_coverage_jobs),
        "coverage_min": round(min(coverages), 6) if coverages else None,
        "coverage_median": round(median(coverages), 6) if coverages else None,
        "coverage_max": round(max(coverages), 6) if coverages else None,
        "first_ts": min(first_ts) if first_ts else None,
        "last_ts": max(last_ts) if last_ts else None,
        "warnings": {
            "missing_jobs": missing_jobs[:30],
            "missing_cache_files": missing_cache_files[:30],
            "low_coverage_jobs": low_coverage_jobs[:30],
            "failed_records": failed_records[:30],
        },
    }


def write_quality_summary(run_dir: Path, quality: dict[str, Any]) -> Path:
    path = run_dir / "quality_summary.json"
    path.write_text(json.dumps(quality, indent=2, sort_keys=True))
    return path


def _cache_filename(symbol: str, timeframe: str) -> str:
    base, _, quote = symbol.partition("/")
    quote = quote.split(":", 1)[0] if quote else "USDT"
    return f"{base}_{quote}_futures_{timeframe}.parquet"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
