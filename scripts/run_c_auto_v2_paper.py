#!/usr/bin/env python3
"""Paper runner for the C-Auto v2 fixed-notional portfolio stream.

The runner supports two modes:
- live: build the latest feature row from local/incremental market data, train
  sleeve models from the historical feature store, then update the paper book.
- replay: step through a validated portfolio backtest stream for UI smoke tests.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
sys.path.insert(0, str(ENGINE_DIR))
sys.path.insert(0, str(ROOT / "scripts"))

from config.settings import BASE_DIR, DATA_DIR  # noqa: E402
from features import build_feature_panel  # noqa: E402
from build_c_auto_feature_store import (  # noqa: E402
    DEFAULT_DERIV_RUN,
    DEFAULT_QUALITY_ID,
    DEFAULT_SNAPSHOT_RUN,
    _attach_btc_state,
    _attach_funding,
    _attach_quality_flags,
    _extra_features_for_symbol,
    _read_quality,
)
from data.fetcher import fetch_ohlcv  # noqa: E402
from arbitration.signal_committee import build_committee_signals, arbitrate_signals  # noqa: E402

try:  # noqa: E402
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
except Exception:  # pragma: no cover - optional dependency fallback
    Ridge = None
    StandardScaler = None
    make_pipeline = None

PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"
CONTROL_DIR = ENGINE_DIR / "control"
DEFAULT_SOURCE = "c_auto_v2_portfolio_backtest_fixed1000_conservative_v1"
DEFAULT_POLICY = ENGINE_DIR / "strategies" / "specs" / "c_auto_v2_regime_policy.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto v2 fixed1000 paper stream")
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="personal", choices=["personal", "demo", "competition"])
    p.add_argument("--source-mode", default="live", choices=["live", "replay"])
    p.add_argument("--source-backtest", default=DEFAULT_SOURCE)
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--dataset-id", default="c_auto_feature_store_v2")
    p.add_argument("--quality-id", default=DEFAULT_QUALITY_ID)
    p.add_argument("--deriv-run-id", default=DEFAULT_DERIV_RUN)
    p.add_argument("--snapshot-run-id", default=DEFAULT_SNAPSHOT_RUN)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--fixed-notional-capital", type=float, default=1000.0)
    p.add_argument("--max-symbols", type=int, default=80)
    p.add_argument("--refresh-max-symbols", type=int, default=30)
    p.add_argument("--lookback-days", type=int, default=240)
    p.add_argument("--max-train-rows", type=int, default=250_000)
    p.add_argument("--refresh-ohlcv", action="store_true")
    p.add_argument("--max-market-age-sec", type=float, default=2 * 3600.0)
    p.add_argument("--min-fresh-symbols", type=int, default=20)
    p.add_argument("--require-derivatives", action="store_true")
    p.add_argument("--max-positions", type=int, default=4)
    p.add_argument("--rebalance-hours", type=int, default=6)
    p.add_argument("--base-risk", type=float, default=0.06)
    p.add_argument("--min-score-quantile", type=float, default=0.90)
    p.add_argument("--min-volume-usd", type=float, default=100_000.0)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval-sec", type=float, default=300.0)
    p.add_argument("--max-cycles", type=int, default=0)
    p.add_argument("--start-from-latest", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    stop_path = CONTROL_DIR / f"c_auto_v2_paper_{args.state_id}_{args.environment}.stop"
    try:
        stop_path.unlink()
    except FileNotFoundError:
        pass

    if args.source_mode == "replay":
        return _run_replay(args, stop_path)
    return _run_live(args, stop_path)


def _run_replay(args: argparse.Namespace, stop_path: Path) -> int:
    source_dir = ENGINE_DIR / "data" / "research" / "c_auto" / args.source_backtest
    equity = _read_frame(source_dir / "equity_curve.parquet", source_dir / "equity_curve.csv")
    trades = _read_frame(source_dir / "trades.parquet", source_dir / "trades.csv")
    if equity.empty:
        raise SystemExit(f"empty equity source: {source_dir}")
    equity["ts"] = pd.to_datetime(equity["ts"], utc=True)
    equity = equity.sort_values("ts").reset_index(drop=True)
    if not trades.empty:
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)

    start_idx = max(0, len(equity) - 240) if args.start_from_latest else 0
    cycles = 0
    idx = start_idx
    while True:
        if stop_path.exists():
            _write_scheduler(args, "stopped", cycles)
            break
        if idx >= len(equity):
            idx = start_idx
        _write_state(args, equity, trades, idx)
        cycles += 1
        _write_scheduler(args, "running", cycles)
        if not args.loop:
            break
        if args.max_cycles > 0 and cycles >= args.max_cycles:
            _write_scheduler(args, "completed", cycles)
            break
        idx += 1
        time.sleep(max(1.0, float(args.interval_sec)))
    return 0


def _run_live(args: argparse.Namespace, stop_path: Path) -> int:
    cycles = 0
    while True:
        if stop_path.exists():
            _write_scheduler(args, "stopped", cycles)
            break
        try:
            state = _run_live_cycle(args)
            _append_live_outputs(args, state)
            cycles += 1
            _write_scheduler(args, "running", cycles, extra={"last_error": None, "source_mode": "live"})
        except Exception as exc:
            cycles += 1
            _write_scheduler(args, "error", cycles, extra={"last_error": str(exc), "source_mode": "live"})
            if not args.loop:
                raise
        if not args.loop:
            break
        if args.max_cycles > 0 and cycles >= args.max_cycles:
            _write_scheduler(args, "completed", cycles, extra={"source_mode": "live"})
            break
        time.sleep(max(5.0, float(args.interval_sec)))
    return 0


def _run_live_cycle(args: argparse.Namespace) -> dict[str, Any]:
    policy = json.loads(Path(args.policy).read_text())
    dataset_dir = ENGINE_DIR / "data" / "features" / args.dataset_id
    train_features = _read_frame(dataset_dir / "features.parquet", dataset_dir / "features.pkl").sort_index()
    train_labels = _read_frame(dataset_dir / "labels.parquet", dataset_dir / "labels.pkl").sort_index()
    latest_features = _build_latest_features(args)
    predictions = _predict_policy(policy, train_features, train_labels, latest_features, args)
    scored = _build_portfolio_scores(predictions)
    now_ts = pd.Timestamp(scored.index.get_level_values("timestamp").max())
    freshness = _freshness_report(latest_features, now_ts, args)
    previous = _load_live_state(args)
    positions, ledger, realized_nav = _close_due_live(previous, latest_features, now_ts, args)
    stopped_flat_restart = str(previous.get("runner_status") or "") == "stopped_flat" and not positions
    bootstrap = (
        not positions
        and (stopped_flat_restart or not previous.get("ledger_tail"))
        and abs(float(previous.get("realized_nav") or args.initial_capital) - float(args.initial_capital)) < 1e-9
    )
    last_rebalance_ts = _last_rebalance_ts(previous)
    freshness_ok = bool(freshness.get("passed"))
    should_rebalance = (
        freshness_ok
        and (bootstrap or _is_rebalance_ts(now_ts, int(args.rebalance_hours)))
        and last_rebalance_ts != now_ts.isoformat()
    )
    if should_rebalance:
        new_positions, new_events = _open_live_positions(scored, positions, now_ts, args)
        positions.update(new_positions)
        ledger.extend(new_events)
    elif not freshness_ok:
        ledger.append(
            {
                "ts": now_ts.isoformat(),
                "event": "skip",
                "symbol": None,
                "side": None,
                "reason": "freshness_gate_failed:" + ",".join(freshness.get("reasons") or []),
                "pnl": None,
                "net_return": None,
            }
        )
    positions = _enrich_live_positions(positions, latest_features, args)
    nav = _mark_live_nav(realized_nav, positions, latest_features, args)
    equity_tail = _upsert_equity_point(
        list(previous.get("equity", [])),
        {"ts": now_ts.isoformat(), "nav": nav, "open_positions": len(positions)},
    )[-240:]
    previous_ledger_tail = list(previous.get("ledger_tail", []))
    if freshness_ok:
        previous_ledger_tail = _drop_freshness_skips(previous_ledger_tail, now_ts.isoformat())
    ledger_tail = _dedupe_ledger_events(previous_ledger_tail + ledger)[-40:]
    return {
        "available": True,
        "state_id": args.state_id,
        "strategy_id": "c_auto_v2_fixed1000_conservative",
        "environment": args.environment,
        "mode": "paper",
        "source_mode": "live",
        "source_backtest": None,
        "dataset_id": args.dataset_id,
        "policy_id": policy.get("policy_id"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": now_ts.isoformat(),
        "market_age_sec": max(0.0, (datetime.now(timezone.utc) - now_ts.to_pydatetime()).total_seconds()),
        "cash": realized_nav,
        "nav": nav,
        "realized_nav": realized_nav,
        "unrealized_pnl": nav - realized_nav,
        "realized_pnl": realized_nav - float(args.initial_capital),
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in positions.values()),
        "positions": positions,
        "live_gates_enabled": True,
        "live_gate_pass_count": 1 if freshness_ok else 0,
        "freshness": freshness,
        "metrics": _live_metrics(equity_tail, args.initial_capital),
        "equity": equity_tail,
        "ledger_tail": ledger_tail,
        "last_rebalance_ts": now_ts.isoformat() if should_rebalance else last_rebalance_ts,
        "_cycle_ledger_events": ledger,
        "latest_candidates": _candidate_snapshot(scored, now_ts),
    }


def _read_frame(parquet: Path, csv_path: Path) -> pd.DataFrame:
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv_path.exists():
        if csv_path.suffix == ".pkl":
            return pd.read_pickle(csv_path)
        return pd.read_csv(csv_path)
    return pd.DataFrame()


def _build_latest_features(args: argparse.Namespace) -> pd.DataFrame:
    quality_dir = BASE_DIR / "data" / "quality" / args.quality_id
    quality = _read_quality(quality_dir)
    quality = quality[quality["has_core_inputs"].astype(bool)].copy()
    quality = quality.sort_values(["train_eligible_180d", "1h_rows"], ascending=[False, False])
    symbols = quality["symbol"].astype(str).head(int(args.max_symbols)).tolist()
    if "BTC/USDT" not in symbols:
        symbols.insert(0, "BTC/USDT")
    end = pd.Timestamp.now(tz="UTC").floor("1h")
    start = end - pd.Timedelta(days=int(args.lookback_days))
    price_data: dict[str, pd.DataFrame] = {}
    extras: list[pd.DataFrame] = []
    refresh_symbols = set(symbols[: max(0, int(args.refresh_max_symbols))])
    refresh_symbols.add("BTC/USDT")
    for symbol in symbols:
        if args.refresh_ohlcv and symbol in refresh_symbols:
            _refresh_ohlcv_cache(symbol, "1h", end)
        one_h = _load_ohlcv_cache(symbol, "1h", start, end)
        if one_h.empty:
            continue
        one_h = _attach_funding(one_h, symbol, args.deriv_run_id)
        price_data[symbol] = one_h
        extra = _extra_features_for_symbol(one_h, symbol, args.deriv_run_id, args.snapshot_run_id)
        if not extra.empty:
            extras.append(extra)
    if not price_data:
        raise RuntimeError("No live OHLCV cache loaded for C-Auto v2")
    base = build_feature_panel(price_data)
    extra = pd.concat(extras).sort_index() if extras else pd.DataFrame(index=base.index)
    extra = extra.reindex(base.index)
    features = pd.concat([base, extra], axis=1).sort_index()
    features = _attach_quality_flags(features, quality, min_train_1h_rows=2160)
    features, _ = _attach_btc_state(features)
    latest_ts = features.index.get_level_values("timestamp").max()
    latest = features.loc[features.index.get_level_values("timestamp") == latest_ts].copy()
    latest = latest[latest.index.get_level_values("symbol") != "BTC/USDT"]
    if latest.empty:
        raise RuntimeError("No latest C-Auto v2 feature rows after BTC state attachment")
    return latest


def _refresh_ohlcv_cache(symbol: str, timeframe: str, end: pd.Timestamp) -> None:
    start = (end - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        fetch_ohlcv(
            symbol,
            start=start,
            end=end.strftime("%Y-%m-%d"),
            mode="futures",
            timeframe=timeframe,
            use_cache=True,
            sandbox=False,
            fallback_to_stale=True,
            fallback_to_yfinance=False,
            include_funding=False,
        )
    except Exception:
        return


def _load_ohlcv_cache(symbol: str, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    safe = symbol.replace("/", "_").replace(":", "_")
    for ext in ("parquet", "pkl"):
        path = DATA_DIR / f"{safe}_futures_{timeframe}.{ext}"
        if not path.exists():
            continue
        df = pd.read_parquet(path) if ext == "parquet" else pd.read_pickle(path)
        df = df.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df.sort_index()
        df = df.loc[(df.index >= start) & (df.index <= end)]
        for col in ("open", "high", "low", "close", "volume"):
            if col in df:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    return pd.DataFrame()


def _predict_policy(
    policy: dict[str, Any],
    train_features: pd.DataFrame,
    train_labels: pd.DataFrame,
    latest_features: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for sleeve in policy.get("sleeves", []):
        sleeve_id = str(sleeve["sleeve_id"])
        for exp in sleeve.get("experiments", []):
            regime = str(exp["regime"])
            label_col = str(exp["label_col"])
            feature_cols = [str(col) for col in exp.get("feature_columns", [])]
            col = _score_col(sleeve_id, regime, label_col)
            pred = _predict_experiment(
                train_features=train_features,
                train_labels=train_labels,
                latest_features=latest_features,
                regime=regime,
                label_col=label_col,
                feature_cols=feature_cols,
                max_train_rows=int(args.max_train_rows),
            )
            if pred.empty:
                continue
            frames.append(pred.rename(columns={"prediction": col}))
    if not frames:
        raise RuntimeError("No C-Auto v2 sleeve predictions produced")
    out = pd.concat(frames, axis=1).sort_index()
    keep = [
        "close",
        "volume_usd",
        "funding_z_24",
        "funding_rate",
        "oi_z_24",
        "ls_z_24",
        "train_eligible_90d",
        "btc_regime_6",
    ]
    return out.join(latest_features[[col for col in keep if col in latest_features.columns]], how="left")


def _predict_experiment(
    *,
    train_features: pd.DataFrame,
    train_labels: pd.DataFrame,
    latest_features: pd.DataFrame,
    regime: str,
    label_col: str,
    feature_cols: list[str],
    max_train_rows: int,
) -> pd.DataFrame:
    feature_cols = [col for col in feature_cols if col in train_features.columns and col in latest_features.columns]
    if not feature_cols or label_col not in train_labels.columns:
        return pd.DataFrame()
    regime_mask = train_features["btc_regime_6"].astype(str) == regime if "btc_regime_6" in train_features.columns else True
    train_idx = train_features.loc[regime_mask].index.intersection(train_labels.index)
    if len(train_idx) == 0:
        return pd.DataFrame()
    train = train_features.loc[train_idx, feature_cols].join(train_labels.loc[train_idx, [label_col]], how="inner")
    train = train.dropna(subset=[label_col])
    if max_train_rows > 0 and len(train) > max_train_rows:
        train = train.tail(max_train_rows)
    latest = latest_features.loc[latest_features["btc_regime_6"].astype(str) == regime, feature_cols].copy()
    if len(train) < 400 or latest.empty:
        return pd.DataFrame(index=latest_features.index, data={"prediction": np.nan})
    x_train = train[feature_cols].apply(pd.to_numeric, errors="coerce")
    y_train = pd.to_numeric(train[label_col], errors="coerce")
    keep = y_train.notna()
    x_train = x_train.loc[keep]
    y_train = y_train.loc[keep]
    medians = x_train.median(numeric_only=True).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    x_train = x_train.replace([np.inf, -np.inf], np.nan).fillna(medians)
    x_latest = latest.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(medians).fillna(0.0)
    if Ridge is not None and StandardScaler is not None and make_pipeline is not None:
        model = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
        model.fit(x_train.to_numpy(dtype=float), y_train.to_numpy(dtype=float))
        values = model.predict(x_latest.to_numpy(dtype=float))
    else:
        corr = x_train.corrwith(y_train).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        score = (x_latest - x_train.mean()).divide(x_train.std().replace(0, np.nan)).fillna(0.0)
        values = score.mul(corr, axis=1).sum(axis=1).to_numpy(dtype=float)
        values = values / max(1.0, float(len(feature_cols))) * max(float(y_train.std() or 0.0), 0.001)
    out = pd.DataFrame(index=latest_features.index, data={"prediction": np.nan})
    out.loc[x_latest.index, "prediction"] = values
    return out


def _score_col(sleeve_id: str, regime: str, label_col: str) -> str:
    side = "short" if "_short_" in label_col else "long"
    horizon = "".join(ch for ch in label_col.split("_")[-1] if ch.isdigit())
    return f"{sleeve_id}__{regime}__{side}{horizon}"


def _build_portfolio_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in (
        "cross_section_spread__strong_bull__long24",
        "high_beta_amplification__strong_bull__long12",
        "small_account_rotation__strong_bull__long6",
        "high_beta_amplification__bear__short12",
        "cross_section_spread__bear__long24",
        "small_account_rotation__bear__short6",
        "cross_section_spread__chop_short__long24",
        "small_account_rotation__chop_short__short6",
        "cross_section_spread__bull__long24",
    ):
        if col not in out.columns:
            out[col] = np.nan
    out["side"] = ""
    out["score"] = np.nan
    out["horizon_hours"] = 0
    out["risk_scalar"] = 0.0
    out["signal_family"] = ""
    regime = out["btc_regime_6"].astype(str)
    strong = regime == "strong_bull"
    out.loc[strong, "side"] = "long"
    out.loc[strong, "horizon_hours"] = 12
    out.loc[strong, "risk_scalar"] = 1.0
    out.loc[strong, "signal_family"] = "cross_section_plus_high_beta"
    out.loc[strong, "score"] = (
        out.loc[strong, "cross_section_spread__strong_bull__long24"].fillna(0.0)
        + 0.75 * out.loc[strong, "high_beta_amplification__strong_bull__long12"].fillna(0.0)
        + 0.25 * out.loc[strong, "small_account_rotation__strong_bull__long6"].fillna(0.0)
    )
    bear = regime == "bear"
    out.loc[bear, "side"] = "short"
    out.loc[bear, "horizon_hours"] = 12
    out.loc[bear, "risk_scalar"] = 1.0
    out.loc[bear, "signal_family"] = "cross_section_plus_high_beta"
    out.loc[bear, "score"] = (
        out.loc[bear, "high_beta_amplification__bear__short12"].fillna(0.0)
        - 0.50 * out.loc[bear, "cross_section_spread__bear__long24"].fillna(0.0)
        + 0.25 * out.loc[bear, "small_account_rotation__bear__short6"].fillna(0.0)
    )
    chop_short = regime == "chop_short"
    out.loc[chop_short, "side"] = "short"
    out.loc[chop_short, "horizon_hours"] = 6
    out.loc[chop_short, "risk_scalar"] = 0.65
    out.loc[chop_short, "signal_family"] = "short_rotation"
    out.loc[chop_short, "score"] = (
        -out.loc[chop_short, "cross_section_spread__chop_short__long24"].fillna(0.0)
        + 0.35 * out.loc[chop_short, "small_account_rotation__chop_short__short6"].fillna(0.0)
    )
    bull = regime == "bull"
    out.loc[bull, "side"] = "long"
    out.loc[bull, "horizon_hours"] = 24
    out.loc[bull, "risk_scalar"] = 0.45
    out.loc[bull, "signal_family"] = "selective_bull_rank"
    out.loc[bull, "score"] = out.loc[bull, "cross_section_spread__bull__long24"].fillna(0.0)
    out["blocked_by_crowding"] = _blocked_by_crowding(out)
    out["eligible"] = (
        out["score"].notna()
        & (out["horizon_hours"] > 0)
        & (pd.to_numeric(out["train_eligible_90d"], errors="coerce").fillna(0.0) > 0)
        & (pd.to_numeric(out["volume_usd"], errors="coerce").fillna(0.0) >= 0.0)
        & ~out["blocked_by_crowding"]
    )
    return out


def _blocked_by_crowding(df: pd.DataFrame) -> pd.Series:
    funding_z = pd.to_numeric(df.get("funding_z_24"), errors="coerce").fillna(0.0)
    oi_z = pd.to_numeric(df.get("oi_z_24"), errors="coerce").fillna(0.0)
    ls_z = pd.to_numeric(df.get("ls_z_24"), errors="coerce").fillna(0.0)
    funding = pd.to_numeric(df.get("funding_rate"), errors="coerce").fillna(0.0)
    side = df["side"].astype(str)
    late_long = (side == "long") & (funding_z > 2.5) & (oi_z > 1.5) & (ls_z > 1.5)
    crowded_short = (side == "short") & (funding_z < -2.5) & (oi_z > 1.5) & (ls_z < -1.5)
    extreme_funding = ((side == "long") & (funding > 0.0015)) | ((side == "short") & (funding < -0.0015))
    return late_long | crowded_short | extreme_funding


def _write_state(args: argparse.Namespace, equity: pd.DataFrame, trades: pd.DataFrame, idx: int) -> None:
    row = equity.iloc[idx].to_dict()
    ts = pd.Timestamp(row["ts"])
    nav = float(row.get("nav_mtm", row.get("nav", args.initial_capital)) or args.initial_capital)
    realized_nav = float(row.get("realized_nav", nav) or nav)
    unrealized = float(row.get("unrealized_pnl", nav - realized_nav) or 0.0)
    open_trades = _open_trades(trades, ts)
    closed = _closed_trades(trades, ts)
    realized_pnl = realized_nav - float(args.initial_capital)
    positions = _positions(open_trades)
    ledger_tail = _ledger_tail(open_trades, closed, ts)
    equity_tail = _equity_tail(equity.iloc[: idx + 1])
    state = {
        "available": True,
        "state_id": args.state_id,
        "strategy_id": "c_auto_v2_fixed1000_conservative",
        "environment": args.environment,
        "mode": "paper",
        "source_backtest": args.source_backtest,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": ts.isoformat(),
        "cash": realized_nav,
        "nav": nav,
        "realized_nav": realized_nav,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized_pnl,
        "open_risk": sum(float(pos.get("risk_budget", 0.0)) for pos in positions.values()),
        "positions": positions,
        "live_gates_enabled": False,
        "live_gate_pass_count": 0,
        "metrics": _metrics(equity.iloc[: idx + 1], args.initial_capital),
        "equity": equity_tail,
        "ledger_tail": ledger_tail,
    }
    prefix = f"{args.state_id}_{args.environment}"
    (PAPER_DIR / f"{prefix}.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    _write_jsonl(PAPER_DIR / f"{prefix}_equity.jsonl", equity_tail[-1:])
    if ledger_tail:
        _write_jsonl(PAPER_DIR / f"{prefix}_ledger.jsonl", ledger_tail[-5:])


def _load_live_state(args: argparse.Namespace) -> dict[str, Any]:
    prefix = f"{args.state_id}_{args.environment}"
    path = PAPER_DIR / f"{prefix}.json"
    if not path.exists():
        return {
            "realized_nav": float(args.initial_capital),
            "positions": {},
            "ledger_tail": [],
            "equity": [],
        }
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"realized_nav": float(args.initial_capital), "positions": {}, "ledger_tail": [], "equity": []}
    if data.get("source_mode") != "live":
        data["realized_nav"] = float(args.initial_capital)
        data["positions"] = {}
        data["ledger_tail"] = []
        data["equity"] = []
    return data


def _close_due_live(
    state: dict[str, Any],
    latest_features: pd.DataFrame,
    now_ts: pd.Timestamp,
    args: argparse.Namespace,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], float]:
    positions = {str(k): dict(v) for k, v in dict(state.get("positions") or {}).items()}
    realized_nav = float(state.get("realized_nav") or args.initial_capital)
    ledger: list[dict[str, Any]] = []
    for symbol in list(positions):
        pos = positions[symbol]
        exit_ts = pd.Timestamp(pos.get("exit_ts"))
        if now_ts < exit_ts:
            continue
        exit_price = _latest_price(latest_features, symbol)
        entry_price = float(pos.get("entry_price") or 0.0)
        notional = float(pos.get("risk_budget") or 0.0)
        if not _valid_number(exit_price) or entry_price <= 0 or notional <= 0:
            continue
        net_return = _net_return(str(pos.get("side")), entry_price, exit_price, args)
        pnl = notional * net_return
        realized_nav += pnl
        ledger.append(
            {
                "ts": now_ts.isoformat(),
                "event": "exit",
                "symbol": symbol,
                "side": pos.get("side"),
                "reason": "horizon",
                "pnl": pnl,
                "net_return": net_return,
            }
        )
        positions.pop(symbol)
    return positions, ledger, realized_nav


def _open_live_positions(
    scored: pd.DataFrame,
    positions: dict[str, dict[str, Any]],
    now_ts: pd.Timestamp,
    args: argparse.Namespace,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    group = scored.xs(now_ts, level="timestamp", drop_level=False).reset_index()
    group = group[~group["symbol"].isin(positions)]
    group["volume_usd"] = pd.to_numeric(group["volume_usd"], errors="coerce").fillna(0.0)
    group = group[group["volume_usd"] >= float(args.min_volume_usd)]
    if group.empty:
        return {}, [
            {
                "ts": now_ts.isoformat(),
                "event": "skip",
                "symbol": None,
                "side": None,
                "reason": "no_eligible_candidates",
                "pnl": None,
                "net_return": None,
            }
        ]
    c_auto_group = group[group["eligible"].astype(bool)].copy()
    if not c_auto_group.empty:
        threshold = c_auto_group.groupby("side")["score"].transform(lambda s: s.quantile(float(args.min_score_quantile)))
        c_auto_symbols = set(c_auto_group[c_auto_group["score"] >= threshold]["symbol"].astype(str))
        group.loc[~group["symbol"].astype(str).isin(c_auto_symbols), "eligible"] = False
    signals = build_committee_signals(
        group,
        now_ts,
        base_capital=float(args.fixed_notional_capital),
        base_risk=float(args.base_risk),
        fee_slip_rate=_round_trip_cost_rate(args),
    )
    result = arbitrate_signals(
        signals,
        positions,
        now_ts,
        initial_capital=float(args.initial_capital),
        realized_nav=float(args.fixed_notional_capital),
        max_positions=int(args.max_positions),
        max_decisions=max(0, int(args.max_positions) - len(positions)),
        max_total_budget_usdt=float(args.fixed_notional_capital) * float(args.base_risk) * float(args.max_positions),
        min_ev=0.0,
    )
    opened: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    row_by_symbol = {str(row["symbol"]): row for _, row in group.iterrows()}
    for decision in result.decisions:
        signal = decision.signal
        symbol = str(signal.symbol)
        row = row_by_symbol.get(symbol, {})
        entry = float(signal.entry)
        if not _valid_number(entry) or entry <= 0:
            continue
        side = str(signal.side)
        risk_budget = float(decision.size_usdt)
        horizon = max(1, int(signal.horizon_sec / 3600))
        target_pct = abs(float(signal.metadata.get("target_pct") or 0.035))
        opened[symbol] = {
            "side": side,
            "score": float(signal.confidence),
            "expected_ev": signal.forward_ev,
            "p_target": signal.p_target,
            "decision_id": decision.decision_id,
            "committee_reason": decision.reason,
            "risk_budget": risk_budget,
            "entry_price": entry,
            "stop_price": float(signal.stop) if signal.stop is not None else None,
            "tp1_price": float(signal.target) if signal.target is not None else None,
            "tp2_price": entry * (1.0 + target_pct * 1.75) if side == "long" else entry * (1.0 - target_pct * 1.75),
            "regime": row.get("btc_regime_6") if hasattr(row, "get") else signal.metadata.get("regime"),
            "signal_family": signal.metadata.get("signal_family") or signal.strategy_id,
            "source_strategy_id": signal.strategy_id,
            "committee_metadata": dict(signal.metadata),
            "entry_ts": now_ts.isoformat(),
            "exit_ts": (now_ts + pd.Timedelta(hours=horizon)).isoformat(),
            "horizon_hours": horizon,
        }
        events.append(
            {
                "ts": now_ts.isoformat(),
                "event": "entry",
                "symbol": symbol,
                "side": side,
                "reason": signal.strategy_id,
                "pnl": None,
                "net_return": None,
                "decision_id": decision.decision_id,
                "expected_ev": signal.forward_ev,
                "p_target": signal.p_target,
            }
        )
    for note in result.notes[-8:]:
        events.append(
            {
                "ts": now_ts.isoformat(),
                "event": "committee_note",
                "symbol": None,
                "side": None,
                "reason": note,
                "pnl": None,
                "net_return": None,
            }
        )
    if not opened:
        events.append(
            {
                "ts": now_ts.isoformat(),
                "event": "skip",
                "symbol": None,
                "side": None,
                "reason": "committee_no_accepted_signals",
                "pnl": None,
                "net_return": None,
            }
        )
    return opened, events


def _mark_live_nav(realized_nav: float, positions: dict[str, dict[str, Any]], latest_features: pd.DataFrame, args: argparse.Namespace) -> float:
    nav = float(realized_nav)
    for symbol, pos in positions.items():
        mark = float(pos.get("mark_price") or _latest_price(latest_features, symbol))
        entry = float(pos.get("entry_price") or 0.0)
        notional = float(pos.get("risk_budget") or 0.0)
        if not _valid_number(mark) or entry <= 0 or notional <= 0:
            continue
        nav += notional * _net_return(str(pos.get("side")), entry, mark, args)
    return nav


def _enrich_live_positions(
    positions: dict[str, dict[str, Any]],
    latest_features: pd.DataFrame,
    args: argparse.Namespace,
) -> dict[str, dict[str, Any]]:
    enriched: dict[str, dict[str, Any]] = {}
    for symbol, raw_pos in positions.items():
        pos = dict(raw_pos)
        mark = _latest_price(latest_features, symbol)
        entry = float(pos.get("entry_price") or 0.0)
        notional = float(pos.get("risk_budget") or 0.0)
        if _valid_number(mark) and entry > 0 and notional > 0:
            net_return = _net_return(str(pos.get("side")), entry, mark, args)
            pos["mark_price"] = mark
            pos["net_return"] = net_return
            pos["unrealized_pnl"] = notional * net_return
            pos["unrealized_pct"] = net_return
            pos["mark_ts"] = latest_features.index.get_level_values("timestamp").max().isoformat()
            pos["distance_to_stop_pct"] = _distance_pct(str(pos.get("side")), mark, pos.get("stop_price"))
            pos["distance_to_tp1_pct"] = _distance_pct(str(pos.get("side")), mark, pos.get("tp1_price"))
            pos["distance_to_tp2_pct"] = _distance_pct(str(pos.get("side")), mark, pos.get("tp2_price"))
        enriched[symbol] = pos
    return enriched


def _distance_pct(side: str, mark: float, target: Any) -> float | None:
    try:
        target_price = float(target)
    except Exception:
        return None
    if not _valid_number(mark) or not _valid_number(target_price) or mark <= 0:
        return None
    if side == "short":
        return float(mark / target_price - 1.0)
    return float(target_price / mark - 1.0)


def _latest_price(latest_features: pd.DataFrame, symbol: str) -> float:
    try:
        ts = latest_features.index.get_level_values("timestamp").max()
        return float(latest_features.loc[(ts, symbol), "close"])
    except Exception:
        return float("nan")


def _net_return(side: str, entry_price: float, exit_price: float, args: argparse.Namespace) -> float:
    raw = float(exit_price) / float(entry_price) - 1.0
    gross = raw if side == "long" else -raw
    return gross - _round_trip_cost_rate(args)


def _round_trip_cost_rate(args: argparse.Namespace) -> float:
    return 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0


def _append_live_outputs(args: argparse.Namespace, state: dict[str, Any]) -> None:
    prefix = f"{args.state_id}_{args.environment}"
    state_for_file = dict(state)
    cycle_ledger_events = list(state_for_file.pop("_cycle_ledger_events", []) or [])
    (PAPER_DIR / f"{prefix}.json").write_text(json.dumps(state_for_file, indent=2, sort_keys=True))
    _append_latest_equity(PAPER_DIR / f"{prefix}_equity.jsonl", state.get("equity", [])[-1:])
    if cycle_ledger_events:
        _write_jsonl(PAPER_DIR / f"{prefix}_ledger.jsonl", cycle_ledger_events)


def _freshness_report(latest_features: pd.DataFrame, now_ts: pd.Timestamp, args: argparse.Namespace) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc)
    market_age_sec = max(0.0, (observed_at - now_ts.to_pydatetime()).total_seconds())
    try:
        latest = latest_features.xs(now_ts, level="timestamp", drop_level=False)
    except Exception:
        latest = latest_features
    symbols = sorted(str(sym) for sym in latest.index.get_level_values("symbol").unique())
    volume_col = _first_existing_col(latest, ("volume_usd", "quote_volume", "volume"))
    if len(latest) and "close" in latest and volume_col:
        close_ok = pd.to_numeric(latest["close"], errors="coerce").notna().to_numpy(dtype=bool)
        volume_ok = (pd.to_numeric(latest[volume_col], errors="coerce").fillna(0.0) > 0).to_numpy(dtype=bool)
        fresh_symbols = int(np.logical_and(close_ok, volume_ok).sum())
    else:
        fresh_symbols = 0
    derivative_present: dict[str, int] = {}
    for name in ("funding_rate", "oi_value", "ls_ratio"):
        if name in latest:
            derivative_present[name] = int(pd.to_numeric(latest[name], errors="coerce").notna().sum())
        else:
            derivative_present[name] = 0
    reasons: list[str] = []
    if market_age_sec > float(args.max_market_age_sec):
        reasons.append(f"market_age_sec>{float(args.max_market_age_sec):.0f}")
    if fresh_symbols < int(args.min_fresh_symbols):
        reasons.append(f"fresh_symbols<{int(args.min_fresh_symbols)}")
    if args.require_derivatives:
        required = max(1, min(int(args.min_fresh_symbols), len(symbols)))
        missing = [name for name, count in derivative_present.items() if count < required]
        if missing:
            reasons.append("missing_derivatives:" + "|".join(missing))
    return {
        "passed": not reasons,
        "reasons": reasons,
        "observed_at": observed_at.isoformat(),
        "latest_market_ts": now_ts.isoformat(),
        "market_age_sec": market_age_sec,
        "fresh_symbols": fresh_symbols,
        "symbol_count": len(symbols),
        "volume_column": volume_col,
        "derivative_present": derivative_present,
        "require_derivatives": bool(args.require_derivatives),
    }


def _first_existing_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for col in candidates:
        if col in df:
            return col
    return None


def _upsert_equity_point(equity: list[dict[str, Any]], point: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    target_ts = str(point.get("ts") or "")
    replaced = False
    seen: set[str] = set()
    for row in equity:
        ts = str(row.get("ts") or "")
        if not ts or ts in seen:
            continue
        seen.add(ts)
        if ts == target_ts:
            out.append(point)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(point)
    return sorted(out, key=lambda row: str(row.get("ts") or ""))


def _append_latest_equity(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    latest = rows[-1]
    latest_ts = str(latest.get("ts") or "")
    if latest_ts and _last_jsonl_ts(path) == latest_ts:
        return
    _write_jsonl(path, [latest])


def _last_jsonl_ts(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            buf = bytearray()
            while pos > 0:
                pos -= 1
                fh.seek(pos)
                char = fh.read(1)
                if char == b"\n" and buf:
                    break
                if char != b"\n":
                    buf.extend(char)
            line = bytes(reversed(buf)).decode("utf-8")
        if not line.strip():
            return None
        return str(json.loads(line).get("ts") or "")
    except Exception:
        return None


def _last_rebalance_ts(state: dict[str, Any]) -> str:
    explicit = str(state.get("last_rebalance_ts") or "")
    if explicit:
        return explicit
    timestamps: list[str] = []
    for event in state.get("ledger_tail", []) or []:
        if event.get("event") in {"entry", "skip"} and event.get("ts"):
            timestamps.append(str(event["ts"]))
    for pos in dict(state.get("positions") or {}).values():
        if pos.get("entry_ts"):
            timestamps.append(str(pos["entry_ts"]))
    return max(timestamps) if timestamps else ""


def _dedupe_ledger_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for event in events:
        key = (
            event.get("event"),
            event.get("ts"),
            event.get("symbol"),
            event.get("side"),
            event.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _drop_freshness_skips(events: list[dict[str, Any]], ts: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events:
        if (
            event.get("event") == "skip"
            and str(event.get("ts") or "") == ts
            and str(event.get("reason") or "").startswith("freshness_gate_failed:")
        ):
            continue
        out.append(event)
    return out


def _live_metrics(equity_tail: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
    navs = pd.Series([float(row.get("nav", initial_capital) or initial_capital) for row in equity_tail])
    if navs.empty:
        return {"initial_nav": initial_capital, "current_nav": initial_capital}
    peak = navs.cummax()
    dd = navs / peak - 1.0
    return {
        "initial_nav": float(initial_capital),
        "current_nav": float(navs.iloc[-1]),
        "total_return": float(navs.iloc[-1] / initial_capital - 1.0),
        "max_drawdown": float(dd.min()),
        "equity_points": int(len(navs)),
    }


def _candidate_snapshot(scored: pd.DataFrame, now_ts: pd.Timestamp) -> list[dict[str, Any]]:
    try:
        group = scored.xs(now_ts, level="timestamp", drop_level=False).reset_index()
    except Exception:
        return []
    group = group.sort_values("score", ascending=False).head(12)
    out = []
    for _, row in group.iterrows():
        out.append(
            {
                "symbol": str(row.get("symbol")),
                "side": str(row.get("side")),
                "regime": str(row.get("btc_regime_6")),
                "score": _json_float(row.get("score")),
                "eligible": bool(row.get("eligible", False)),
                "blocked_by_crowding": bool(row.get("blocked_by_crowding", False)),
            }
        )
    return out


def _open_trades(trades: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if trades.empty:
        return trades
    return trades[(trades["entry_ts"] <= ts) & (trades["exit_ts"] > ts)].copy()


def _closed_trades(trades: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    if trades.empty:
        return trades
    closed = trades[trades["exit_ts"] <= ts].copy()
    return closed.tail(20)


def _positions(open_trades: pd.DataFrame) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}
    for _, row in open_trades.iterrows():
        symbol = str(row["symbol"])
        positions[symbol] = {
            "side": row.get("side", "long"),
            "score": float(row.get("score", 0.0) or 0.0),
            "risk_budget": float(row.get("notional", 0.0) or 0.0),
            "entry_price": float(row.get("entry_price", 0.0) or 0.0),
            "stop_price": None,
            "tp1_price": None,
            "tp2_price": None,
            "regime": row.get("regime"),
            "entry_ts": pd.Timestamp(row["entry_ts"]).isoformat(),
            "exit_ts": pd.Timestamp(row["exit_ts"]).isoformat(),
            "horizon_hours": int(row.get("horizon_hours", 0) or 0),
        }
    return positions


def _ledger_tail(open_trades: pd.DataFrame, closed: pd.DataFrame, ts: pd.Timestamp) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _, row in closed.tail(10).iterrows():
        events.append(
            {
                "ts": pd.Timestamp(row["exit_ts"]).isoformat(),
                "event": "exit",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "reason": row.get("exit_reason", "horizon"),
                "pnl": float(row.get("pnl", 0.0) or 0.0),
                "net_return": float(row.get("net_return", 0.0) or 0.0),
            }
        )
    for _, row in open_trades.tail(5).iterrows():
        events.append(
            {
                "ts": ts.isoformat(),
                "event": "hold",
                "symbol": row.get("symbol"),
                "side": row.get("side"),
                "reason": row.get("regime", "open"),
                "pnl": None,
                "net_return": None,
            }
        )
    return sorted(events, key=lambda item: item["ts"])[-20:]


def _equity_tail(equity: pd.DataFrame) -> list[dict[str, Any]]:
    out = []
    for _, row in equity.tail(240).iterrows():
        nav = float(row.get("nav_mtm", row.get("nav", 0.0)) or 0.0)
        out.append(
            {
                "ts": pd.Timestamp(row["ts"]).isoformat(),
                "nav": nav,
                "open_positions": int(row.get("open_positions", 0) or 0),
            }
        )
    return out


def _metrics(equity: pd.DataFrame, initial_capital: float) -> dict[str, Any]:
    navs = pd.to_numeric(equity.get("nav_mtm", equity.get("nav")), errors="coerce").dropna()
    if navs.empty:
        return {"initial_nav": initial_capital, "current_nav": initial_capital}
    peak = navs.cummax()
    dd = navs / peak - 1.0
    return {
        "initial_nav": float(initial_capital),
        "current_nav": float(navs.iloc[-1]),
        "total_return": float(navs.iloc[-1] / initial_capital - 1.0),
        "max_drawdown": float(dd.min()),
        "equity_points": int(len(navs)),
    }


def _write_scheduler(args: argparse.Namespace, status: str, cycles: int, extra: dict[str, Any] | None = None) -> None:
    prefix = f"{args.state_id}_{args.environment}"
    payload = {
        "scheduler_status": status,
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "cycles": cycles,
        "interval_sec": args.interval_sec,
        "state_id": args.state_id,
        "environment": args.environment,
        "source_mode": args.source_mode,
    }
    if extra:
        payload.update(extra)
    (PAPER_DIR / f"{prefix}_scheduler.json").write_text(json.dumps(payload, indent=2, sort_keys=True))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _is_rebalance_ts(ts: pd.Timestamp, rebalance_hours: int) -> bool:
    if rebalance_hours <= 1:
        return True
    return int(ts.hour) % rebalance_hours == 0


def _valid_number(value: float) -> bool:
    return math.isfinite(float(value)) and not pd.isna(value)


def _json_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


if __name__ == "__main__":
    raise SystemExit(main())
