"""Dataset manifest helpers for research artifacts."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd


def build_dataset_manifest(
    dataset_id: str,
    out_dir: Path,
    symbols: Iterable[str],
    loaded_symbols: Iterable[str],
    start: str,
    end: str,
    timeframe: str,
    mode: str,
    label_col: str,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    validation_report: Dict,
    feature_registry: Dict,
    label_registry: Dict,
    cost_assumptions: Optional[Dict] = None,
) -> Dict:
    files = _artifact_files(out_dir)
    common = features.index.intersection(labels.index)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": dataset_id,
        "code_version": _git_sha(),
        "input": {
            "symbols": list(symbols),
            "loaded_symbols": sorted(loaded_symbols),
            "start": start,
            "end": end,
            "timeframe": timeframe,
            "mode": mode,
            "label_col": label_col,
            "cost_assumptions": cost_assumptions or {},
        },
        "shape": {
            "rows": int(len(common)),
            "feature_rows": int(len(features)),
            "label_rows": int(len(labels)),
            "n_features": int(features.shape[1]),
            "n_labels": int(labels.shape[1]),
        },
        "time_bounds": {
            "features": _time_bounds(features),
            "labels": _time_bounds(labels),
        },
        "validation": validation_report,
        "feature_registry": feature_registry,
        "label_registry": label_registry,
        "artifacts": files,
        "artifact_fingerprints": {name: _sha256_file(out_dir / name) for name in files.values()},
    }


def _artifact_files(out_dir: Path) -> Dict[str, str]:
    keys = {
        "features": ("features.parquet", "features.pkl"),
        "labels": ("labels.parquet", "labels.pkl"),
        "ic_summary": ("ic_summary.parquet", "ic_summary.pkl"),
        "validation": ("validation.json",),
        "feature_registry": ("feature_registry.json",),
        "label_registry": ("label_registry.json",),
        "walk_forward_folds": ("walk_forward_folds.json",),
        "metadata": ("metadata.json",),
    }
    found: Dict[str, str] = {}
    for key, candidates in keys.items():
        for name in candidates:
            if (out_dir / name).exists():
                found[key] = name
                break
    return found


def _time_bounds(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    if df.empty:
        return {"min": None, "max": None}
    idx = df.index.get_level_values("timestamp") if isinstance(df.index, pd.MultiIndex) else df.index
    return {"min": str(idx.min()), "max": str(idx.max())}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    return result.stdout.strip() or None
