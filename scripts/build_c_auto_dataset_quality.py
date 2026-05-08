#!/usr/bin/env python3
"""Build C-Auto raw dataset quality reports."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from config.settings import BASE_DIR
from data.catalog import DataCatalog, DatasetRecord


DEFAULT_OHLCV_RUN = "c_auto_universe_vol5m_5m_15m_20240101_20260505"
DEFAULT_HTF_RUN = "c_auto_universe_vol5m_1h_4h_1d_20240101_20260505"
DEFAULT_DERIV_RUN = "c_auto_deriv_vol5m_5m_20240101_20260505"
DEFAULT_SNAPSHOT_RUN = "c_auto_market_quality_snapshot_20260505"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Summarize C-Auto raw data quality")
    p.add_argument("--ohlcv-run-id", default=DEFAULT_OHLCV_RUN)
    p.add_argument("--htf-run-id", default=DEFAULT_HTF_RUN)
    p.add_argument("--deriv-run-id", default=DEFAULT_DERIV_RUN)
    p.add_argument("--snapshot-run-id", default=DEFAULT_SNAPSHOT_RUN)
    p.add_argument("--symbols-manifest", default="", help="Optional universe manifest used to filter symbols")
    p.add_argument("--dataset-id", default=None)
    p.add_argument("--min-train-1h-rows", type=int, default=2160, help="90 days of 1h bars")
    p.add_argument("--min-long-train-1h-rows", type=int, default=4320, help="180 days of 1h bars")
    p.add_argument("--allow-missing-derivatives", action="store_true", help="Build OHLCV-only quality when derivatives runs are absent")
    p.add_argument("--register-catalog", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dataset_id = args.dataset_id or f"c_auto_dataset_quality_{_stamp()}"
    out_dir = BASE_DIR / "data" / "quality" / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)

    training_dir = BASE_DIR / "data" / "training_history"
    deriv_dir = BASE_DIR / "data" / "derivatives_structure"
    ohlcv_dir = training_dir / args.ohlcv_run_id
    htf_dir = training_dir / args.htf_run_id
    deriv_run_dir = deriv_dir / args.deriv_run_id
    snapshot_run_dir = deriv_dir / args.snapshot_run_id

    ohlcv_manifest = _read_json(ohlcv_dir / "manifest.json")
    htf_manifest = _read_json(htf_dir / "manifest.json")
    deriv_manifest = _read_json(deriv_run_dir / "manifest.json", required=not args.allow_missing_derivatives)
    snapshot_manifest = _read_json(snapshot_run_dir / "manifest.json", required=not args.allow_missing_derivatives)
    symbols = list(dict.fromkeys(ohlcv_manifest.get("symbols") or htf_manifest.get("symbols") or []))
    if args.symbols_manifest:
        symbols_manifest = _read_json(Path(args.symbols_manifest), required=True)
        allowed = {str(symbol) for symbol in symbols_manifest.get("symbols", [])}
        symbols = [symbol for symbol in symbols if symbol in allowed]

    progress_raw = pd.concat(
        [
            _read_progress(ohlcv_dir / "progress.jsonl", "ohlcv"),
            _read_progress(htf_dir / "progress.jsonl", "ohlcv"),
            _read_progress(deriv_run_dir / "progress.jsonl", "derivatives"),
            _read_progress(snapshot_run_dir / "progress.jsonl", "snapshot"),
        ],
        ignore_index=True,
    )
    progress = _latest_progress(progress_raw)

    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        safe = _safe_symbol(symbol)
        row: dict[str, Any] = {"symbol": symbol, "safe_symbol": safe}
        symbol_jobs = progress[progress["symbol"] == symbol] if not progress.empty else pd.DataFrame()
        row["failed_jobs"] = int((symbol_jobs.get("status") == "failed").sum()) if not symbol_jobs.empty else 0
        row["ok_jobs"] = int((symbol_jobs.get("status") == "ok").sum()) if not symbol_jobs.empty else 0

        for timeframe in ("5m", "15m", "1h", "4h", "1d"):
            _merge_timeframe_stats(row, symbol_jobs, timeframe)

        for kind in ("funding", "open_interest", "long_short"):
            path = deriv_run_dir / safe / f"{kind}_5m.parquet"
            row[f"{kind}_present"] = path.exists()
            row[f"{kind}_rows"] = _row_count(path)

        for kind in ("instrument", "ticker", "orderbook", "trades"):
            path = snapshot_run_dir / safe / f"{kind}_snapshot.parquet"
            row[f"{kind}_snapshot_present"] = path.exists()
            row[f"{kind}_snapshot_rows"] = _row_count(path)

        row["train_eligible_90d"] = int(row.get("1h_rows") or 0) >= args.min_train_1h_rows
        row["train_eligible_180d"] = int(row.get("1h_rows") or 0) >= args.min_long_train_1h_rows
        has_core_ohlcv = bool(
            row.get("5m_status") == "ok"
            and row.get("15m_status") == "ok"
            and row.get("1h_status") == "ok"
        )
        has_derivatives = bool(
            row.get("funding_present")
            and row.get("open_interest_present")
            and row.get("long_short_present")
            and row.get("instrument_snapshot_present")
            and row.get("ticker_snapshot_present")
            and row.get("orderbook_snapshot_present")
        )
        row["has_core_ohlcv"] = has_core_ohlcv
        row["has_derivatives"] = has_derivatives
        row["has_core_inputs"] = bool(has_core_ohlcv and (has_derivatives or args.allow_missing_derivatives))
        rows.append(row)

    quality = pd.DataFrame(rows).sort_values(["train_eligible_90d", "1h_rows", "symbol"], ascending=[False, False, True])
    failures = progress[progress["status"] == "failed"].copy() if not progress.empty else pd.DataFrame()
    summary = _summary(
        quality,
        failures,
        manifests={
            "ohlcv": ohlcv_manifest,
            "higher_timeframe": htf_manifest,
            "derivatives": deriv_manifest,
            "snapshot": snapshot_manifest,
        },
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "inputs": {
            "ohlcv_run_id": args.ohlcv_run_id,
            "htf_run_id": args.htf_run_id,
            "deriv_run_id": args.deriv_run_id,
            "snapshot_run_id": args.snapshot_run_id,
            "symbols_manifest": args.symbols_manifest,
        },
        "allow_missing_derivatives": bool(args.allow_missing_derivatives),
        "thresholds": {
            "min_train_1h_rows": args.min_train_1h_rows,
            "min_long_train_1h_rows": args.min_long_train_1h_rows,
        },
        "summary": summary,
        "artifacts": {
            "symbol_quality_parquet": "symbol_quality.parquet",
            "symbol_quality_csv": "symbol_quality.csv",
            "failures_csv": "failures.csv",
            "summary_json": "summary.json",
        },
    }

    _write_frame(quality, out_dir / "symbol_quality.parquet")
    quality.to_csv(out_dir / "symbol_quality.csv", index=False)
    failures.to_csv(out_dir / "failures.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))

    if args.register_catalog:
        DataCatalog().register(
            DatasetRecord(
                dataset_id=dataset_id,
                kind="research",
                source="derived",
                path=str(out_dir),
                timeframe="5m,15m,1h,4h,1d",
                symbols=tuple(symbols),
                start=str(ohlcv_manifest.get("start") or htf_manifest.get("start") or ""),
                end=str(ohlcv_manifest.get("end") or htf_manifest.get("end") or ""),
                rows=int(len(quality)),
                status=summary["status"],
                metadata=summary,
            )
        )

    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if summary["status"] in {"ok", "warn"} else 1


def _read_json(path: Path, required: bool = True) -> dict[str, Any]:
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        if not required:
            return {}
        raise FileNotFoundError(path)
    return json.loads(path.read_text())


def _read_progress(path: Path, source: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return pd.DataFrame()
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            record["source"] = source
            rows.append(record)
    return pd.DataFrame(rows)


def _latest_progress(progress: pd.DataFrame) -> pd.DataFrame:
    if progress.empty:
        return progress
    out = progress.copy()
    for col in ("symbol", "timeframe", "kind", "source"):
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].fillna("").astype(str)
    if "ts" in out.columns:
        out["_ts"] = pd.to_datetime(out["ts"], utc=True, errors="coerce")
        out = out.sort_values("_ts", na_position="first")
    key = ["source", "symbol", "timeframe", "kind"]
    return out.drop_duplicates(key, keep="last").drop(columns=["_ts"], errors="ignore")


def _merge_timeframe_stats(row: dict[str, Any], symbol_jobs: pd.DataFrame, timeframe: str) -> None:
    prefix = timeframe
    if symbol_jobs.empty or "timeframe" not in symbol_jobs:
        row[f"{prefix}_status"] = "missing"
        row[f"{prefix}_rows"] = 0
        row[f"{prefix}_coverage"] = 0.0
        row[f"{prefix}_first_ts"] = None
        row[f"{prefix}_last_ts"] = None
        return
    frame = symbol_jobs[symbol_jobs["timeframe"] == timeframe]
    if frame.empty:
        row[f"{prefix}_status"] = "missing"
        row[f"{prefix}_rows"] = 0
        row[f"{prefix}_coverage"] = 0.0
        row[f"{prefix}_first_ts"] = None
        row[f"{prefix}_last_ts"] = None
        return
    record = frame.iloc[-1].to_dict()
    row[f"{prefix}_status"] = str(record.get("status") or "unknown")
    row[f"{prefix}_rows"] = _safe_int(record.get("rows"))
    row[f"{prefix}_coverage"] = _safe_float(record.get("coverage"))
    row[f"{prefix}_first_ts"] = record.get("first_ts")
    row[f"{prefix}_last_ts"] = record.get("last_ts")
    row[f"{prefix}_error"] = record.get("error")


def _row_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return int(len(pd.read_parquet(path)))
    except Exception:
        return 0


def _safe_int(value: Any) -> int:
    if value is None or pd.isna(value):
        return 0
    return int(value)


def _safe_float(value: Any) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def _summary(quality: pd.DataFrame, failures: pd.DataFrame, manifests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = int(len(quality))
    failed_jobs = int(len(failures))
    core_ready = int(quality["has_core_inputs"].sum()) if total else 0
    eligible_90d = int(quality["train_eligible_90d"].sum()) if total else 0
    eligible_180d = int(quality["train_eligible_180d"].sum()) if total else 0
    status = "ok"
    if total == 0 or eligible_90d < 30:
        status = "failed"
    elif failed_jobs or core_ready < total:
        status = "warn"
    return {
        "status": status,
        "symbols": total,
        "core_ready_symbols": core_ready,
        "train_eligible_90d_symbols": eligible_90d,
        "train_eligible_180d_symbols": eligible_180d,
        "failed_jobs": failed_jobs,
        "jobs": {name: manifest.get("summary", {}) for name, manifest in manifests.items()},
        "failed_job_symbols": sorted(failures["symbol"].dropna().unique().tolist()) if not failures.empty else [],
    }


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
