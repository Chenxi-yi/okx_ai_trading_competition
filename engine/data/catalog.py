"""Data Foundation catalog for research, backtest, paper, and live datasets."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

import pandas as pd

try:
    from config.settings import BASE_DIR
except ModuleNotFoundError:
    from engine.config.settings import BASE_DIR

DatasetKind = Literal["raw_ohlcv", "features", "research", "backtest", "paper", "live"]
DataSource = Literal["okx_ccxt", "okx_ws", "okx_cli", "derived", "manual"]


@dataclass(frozen=True)
class DatasetRecord:
    dataset_id: str
    kind: DatasetKind
    source: DataSource
    path: str
    timeframe: str
    symbols: tuple[str, ...]
    start: str | None = None
    end: str | None = None
    rows: int = 0
    status: str = "created"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["symbols"] = list(self.symbols)
        data["metadata"] = dict(self.metadata)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DatasetRecord":
        return cls(
            dataset_id=str(data["dataset_id"]),
            kind=data.get("kind", "raw_ohlcv"),  # type: ignore[arg-type]
            source=data.get("source", "derived"),  # type: ignore[arg-type]
            path=str(data.get("path", "")),
            timeframe=str(data.get("timeframe", "")),
            symbols=tuple(data.get("symbols", ())),
            start=str(data["start"]) if data.get("start") else None,
            end=str(data["end"]) if data.get("end") else None,
            rows=int(data.get("rows", 0) or 0),
            status=str(data.get("status", "created")),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(timezone.utc).isoformat())),
            metadata=dict(data.get("metadata", {})),
        )


class DataCatalog:
    """JSON-backed catalog of data artifacts and their intended use."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else BASE_DIR / "data" / "catalog.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"version": "1.0", "datasets": []})

    def register(self, record: DatasetRecord) -> DatasetRecord:
        data = self._read()
        updated = DatasetRecord(
            **{
                **record.to_dict(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        rows = [item for item in data.get("datasets", []) if item.get("dataset_id") != record.dataset_id]
        rows.append(updated.to_dict())
        data["datasets"] = sorted(rows, key=lambda item: item["dataset_id"])
        self._write(data)
        return updated

    def get(self, dataset_id: str) -> DatasetRecord:
        for item in self._read().get("datasets", []):
            if item.get("dataset_id") == dataset_id:
                return DatasetRecord.from_dict(item)
        raise KeyError(f"Unknown dataset_id: {dataset_id}")

    def list(self, kind: str | None = None, status: str | None = None) -> list[DatasetRecord]:
        records = [DatasetRecord.from_dict(item) for item in self._read().get("datasets", [])]
        if kind:
            records = [record for record in records if record.kind == kind]
        if status:
            records = [record for record in records if record.status == status]
        return sorted(records, key=lambda record: record.dataset_id)

    def register_feature_dataset(self, dataset_id: str, dataset_dir: Path | str) -> DatasetRecord:
        path = Path(dataset_dir)
        metadata = _read_json(path / "metadata.json")
        manifest = _read_json(path / "manifest.json")
        symbols = tuple(metadata.get("loaded_symbols") or metadata.get("symbols") or manifest.get("input", {}).get("loaded_symbols", ()))
        record = DatasetRecord(
            dataset_id=dataset_id,
            kind="features",
            source="derived",
            path=str(path),
            timeframe=str(metadata.get("timeframe") or manifest.get("input", {}).get("timeframe", "")),
            symbols=symbols,
            start=str(metadata.get("start") or manifest.get("input", {}).get("start", "")),
            end=str(metadata.get("end") or manifest.get("input", {}).get("end", "")),
            rows=int(metadata.get("rows") or manifest.get("shape", {}).get("rows", 0) or 0),
            status=str(metadata.get("validation_status", "created")),
            metadata={
                "cost_assumptions": metadata.get("cost_assumptions", {}),
                "manifest": manifest.get("artifact_fingerprints", {}),
            },
        )
        return self.register(record)

    def register_raw_ohlcv_run(
        self,
        dataset_id: str,
        run_dir: Path | str,
        manifest: Mapping[str, Any],
        quality: Mapping[str, Any],
    ) -> DatasetRecord:
        path = Path(run_dir)
        symbols = tuple(str(symbol) for symbol in manifest.get("symbols", ()))
        timeframes = tuple(str(timeframe) for timeframe in manifest.get("timeframes") or [manifest.get("timeframe") or ""])
        record = DatasetRecord(
            dataset_id=dataset_id,
            kind="raw_ohlcv",
            source="okx_ccxt",
            path=str(path),
            timeframe=",".join(timeframe for timeframe in timeframes if timeframe),
            symbols=symbols,
            start=str(manifest.get("start")) if manifest.get("start") else None,
            end=str(manifest.get("end")) if manifest.get("end") else None,
            rows=int(quality.get("rows", 0) or 0),
            status=str(quality.get("validation_status") or "created"),
            metadata={
                "run_id": manifest.get("run_id") or path.name,
                "download_status": manifest.get("status"),
                "summary": dict(manifest.get("summary", {})),
                "quality": dict(quality),
            },
        )
        return self.register(record)

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def _write(self, data: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp_path.replace(self.path)


def infer_ohlcv_record(dataset_id: str, price_data: Mapping[str, pd.DataFrame], timeframe: str, path: str) -> DatasetRecord:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    rows = 0
    for df in price_data.values():
        if df is None or df.empty:
            continue
        idx = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
        starts.append(idx.min())
        ends.append(idx.max())
        rows += len(df)
    return DatasetRecord(
        dataset_id=dataset_id,
        kind="raw_ohlcv",
        source="okx_ccxt",
        path=path,
        timeframe=timeframe,
        symbols=tuple(sorted(price_data)),
        start=str(min(starts)) if starts else None,
        end=str(max(ends)) if ends else None,
        rows=rows,
        status="ok" if rows else "empty",
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
