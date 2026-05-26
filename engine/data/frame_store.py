"""Central DataFrame artifact access for data readiness and refresh.

Strategy code should not carry parquet compatibility logic. Data refresh and
readiness use this module to validate and, when appropriate, repair local frame
artifacts after migration between machines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


FRAME_SUFFIXES = {".parquet", ".pkl", ".pickle", ".csv"}


@dataclass(frozen=True)
class FrameReadResult:
    path: Path
    ok: bool
    rows: int = 0
    columns: tuple[str, ...] = ()
    reader: str = ""
    error: str = ""


def read_frame(path: Path | str, *, index_utc: bool = False) -> pd.DataFrame:
    """Read a local frame artifact through data-layer compatibility rules."""
    target = Path(path)
    errors: list[str] = []
    for candidate in _candidate_paths(target):
        if not candidate.exists():
            continue
        try:
            df = _read_existing(candidate)
        except Exception as exc:
            errors.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
            continue
        return _normalize_index(df, index_utc=index_utc)
    detail = "; ".join(errors) if errors else "file not found"
    raise OSError(f"unable to read frame {target}: {detail}")


def frame_health(path: Path | str, *, index_utc: bool = False) -> FrameReadResult:
    target = Path(path)
    try:
        df, reader, resolved = _read_frame_with_reader(target, index_utc=index_utc)
    except Exception as exc:
        return FrameReadResult(path=target, ok=False, error=f"{type(exc).__name__}: {exc}")
    return FrameReadResult(
        path=resolved,
        ok=True,
        rows=int(len(df)),
        columns=tuple(str(col) for col in df.columns),
        reader=reader,
    )


def repair_frame(path: Path | str, *, index_utc: bool = False, backup: bool = True) -> FrameReadResult:
    """Rewrite a readable migrated frame into the path's native format.

    This belongs to data maintenance. It should be called by operators or data
    refresh jobs, not strategy adapters.
    """
    target = Path(path)
    df, reader, resolved = _read_frame_with_reader(target, index_utc=index_utc)
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup and target.exists() and resolved == target:
        backup_path = target.with_suffix(f"{target.suffix}.migrated-bak")
        if not backup_path.exists():
            target.replace(backup_path)
    _write_native(df, target)
    return FrameReadResult(
        path=target,
        ok=True,
        rows=int(len(df)),
        columns=tuple(str(col) for col in df.columns),
        reader=f"repair_from_{reader}",
    )


def _candidate_paths(path: Path) -> Iterable[Path]:
    yield path
    if path.suffix == ".parquet":
        yield path.with_suffix(".pkl")
        yield path.with_suffix(".pickle")
        yield path.with_suffix(".csv")


def _read_frame_with_reader(path: Path, *, index_utc: bool) -> tuple[pd.DataFrame, str, Path]:
    errors: list[str] = []
    for candidate in _candidate_paths(path):
        if not candidate.exists():
            continue
        try:
            df, reader = _read_existing_with_reader(candidate)
        except Exception as exc:
            errors.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
            continue
        return _normalize_index(df, index_utc=index_utc), reader, candidate
    detail = "; ".join(errors) if errors else "file not found"
    raise OSError(f"unable to read frame {path}: {detail}")


def _read_existing(path: Path) -> pd.DataFrame:
    return _read_existing_with_reader(path)[0]


def _read_existing_with_reader(path: Path) -> tuple[pd.DataFrame, str]:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        try:
            return pd.read_parquet(path), "pandas_parquet"
        except Exception as first_exc:
            try:
                import duckdb
            except Exception:
                raise first_exc
            try:
                df = duckdb.connect().execute("select * from read_parquet(?)", [str(path)]).fetchdf()
            except Exception:
                raise first_exc
            return _restore_common_index(df), "duckdb_parquet"
    if suffix in {".pkl", ".pickle"}:
        return pd.read_pickle(path), "pickle"
    if suffix == ".csv":
        return pd.read_csv(path), "csv"
    raise ValueError(f"unsupported frame suffix: {path.suffix}")


def _restore_common_index(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if {"timestamp", "symbol"}.issubset(out.columns):
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        return out.set_index(["timestamp", "symbol"])
    for column in ("timestamp", "date"):
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True)
            return out.set_index(column)
    return out


def _normalize_index(df: pd.DataFrame, *, index_utc: bool) -> pd.DataFrame:
    if not index_utc or df.empty:
        return df
    out = df.copy()
    try:
        out.index = pd.to_datetime(out.index, utc=True)
        return out.sort_index()
    except Exception:
        return df


def _write_native(df: pd.DataFrame, path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df.to_parquet(path)
    elif suffix in {".pkl", ".pickle"}:
        df.to_pickle(path)
    elif suffix == ".csv":
        df.to_csv(path, index=True)
    else:
        raise ValueError(f"unsupported frame suffix: {path.suffix}")
