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
from arbitration.thesis_exit import evaluate_position_thesis  # noqa: E402
from arbitration.leverage_policy import (  # noqa: E402
    CommitteeLeverageInputs,
    compute_committee_leverage_policy,
    infer_kit_alignment,
)

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
    p.add_argument("--data-readiness-wait-sec", type=float, default=600.0)
    p.add_argument("--data-readiness-poll-sec", type=float, default=15.0)
    p.add_argument("--max-positions", type=int, default=15)
    p.add_argument("--paper-max-positions-per-strategy", type=int, default=5)
    p.add_argument("--rebalance-hours", type=int, default=6)
    p.add_argument("--base-risk", type=float, default=0.06)
    p.add_argument("--default-leverage", type=float, default=1.0)
    p.add_argument("--max-leverage", type=float, default=1.0)
    p.add_argument("--allow-aggressive-leverage", action="store_true")
    p.add_argument("--paper-force-kit-confirmation", action="store_true")
    p.add_argument("--post-exit-cooldown-hours", type=float, default=4.0)
    p.add_argument("--thesis-exit-enabled", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--thesis-min-hold-hours", type=float, default=1.0)
    p.add_argument("--thesis-score-retain", type=float, default=0.60)
    p.add_argument("--thesis-min-score", type=float, default=0.0001)
    p.add_argument("--short-loss-cooldown-hours", type=float, default=12.0)
    p.add_argument("--short-loss-lookback-hours", type=float, default=24.0)
    p.add_argument("--short-loss-cooldown-min-losses", type=int, default=2)
    p.add_argument("--max-gross-leverage", type=float, default=0.25)
    p.add_argument("--max-position-nav-loss-pct", type=float, default=0.0015)
    p.add_argument("--max-stop-margin-loss-pct", type=float, default=0.15)
    p.add_argument("--min-score-quantile", type=float, default=0.90)
    p.add_argument("--min-volume-usd", type=float, default=100_000.0)
    p.add_argument("--disable-trend-pullback-paper", action="store_true")
    p.add_argument("--disable-daily-fib-paper", action="store_true")
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
    latest_features, readiness_wait = _build_ready_latest_features(args)
    predictions = _predict_policy(policy, train_features, train_labels, latest_features, args)
    scored = _build_portfolio_scores(
        predictions,
        enable_trend_pullback=not bool(args.disable_trend_pullback_paper),
        enable_daily_fib=not bool(args.disable_daily_fib_paper),
    )
    now_ts = pd.Timestamp(scored.index.get_level_values("timestamp").max())
    freshness = _freshness_report(latest_features, now_ts, args)
    if readiness_wait:
        freshness["readiness_wait"] = readiness_wait
    previous = _load_live_state(args)
    positions, ledger, realized_nav = _close_due_live(previous, latest_features, now_ts, args)
    positions = _enrich_live_positions(positions, latest_features, args)
    positions, thesis_events, realized_nav = _enforce_thesis_exits(args, positions, scored, latest_features, now_ts, realized_nav)
    ledger.extend(thesis_events)
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
        risk_events = list(previous.get("ledger_tail", [])) + ledger
        cooldown_symbols = _recent_exit_symbols(risk_events, now_ts, float(args.post_exit_cooldown_hours))
        new_positions, new_events = _open_live_positions(scored, positions, now_ts, args, risk_events, cooldown_symbols)
        positions.update(new_positions)
        ledger.extend(new_events)
    elif not freshness_ok:
        wait_status = str((freshness.get("readiness_wait") or {}).get("status") or "")
        prefix = "data_readiness_timeout" if wait_status == "timeout" else "freshness_gate_failed"
        ledger.append(
            {
                "ts": now_ts.isoformat(),
                "event": "skip",
                "symbol": None,
                "side": None,
                "reason": prefix + ":" + ",".join(freshness.get("reasons") or []),
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


def _build_ready_latest_features(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, float(getattr(args, "data_readiness_wait_sec", 0.0)))
    poll = max(1.0, float(getattr(args, "data_readiness_poll_sec", 15.0)))
    attempts = 0
    last_features: pd.DataFrame | None = None
    last_freshness: dict[str, Any] = {}
    while True:
        attempts += 1
        latest = _build_latest_features(args)
        latest_ts = pd.Timestamp(latest.index.get_level_values("timestamp").max())
        freshness = _freshness_report(latest, latest_ts, args)
        last_features = latest
        last_freshness = freshness
        if bool(freshness.get("passed")):
            return latest, {
                "waited": attempts > 1,
                "attempts": attempts,
                "status": "ready",
                "freshness": freshness,
            }
        if attempts == 1 and str(",".join(freshness.get("reasons") or [])).find("market_age_sec") >= 0:
            _refresh_latest_feature_universe(args, latest)
        if time.monotonic() >= deadline:
            return latest, {
                "waited": attempts > 1,
                "attempts": attempts,
                "status": "timeout",
                "freshness": last_freshness,
            }
        time.sleep(poll)


def _refresh_latest_feature_universe(args: argparse.Namespace, latest: pd.DataFrame) -> None:
    try:
        symbols = sorted(str(sym) for sym in latest.index.get_level_values("symbol").unique())
    except Exception:
        symbols = []
    if "BTC/USDT" not in symbols:
        symbols.insert(0, "BTC/USDT")
    end = pd.Timestamp.now(tz="UTC").floor("1h")
    limit = int(getattr(args, "refresh_max_symbols", 0) or getattr(args, "max_symbols", 0) or len(symbols))
    for symbol in symbols[: max(1, limit)]:
        _refresh_ohlcv_cache(symbol, "1h", end, force=True)


def _refresh_ohlcv_cache(symbol: str, timeframe: str, end: pd.Timestamp, force: bool = False) -> None:
    start = (end - pd.Timedelta(days=3)).isoformat()
    try:
        fetch_ohlcv(
            symbol,
            start=start,
            end=end.isoformat(),
            mode="futures",
            timeframe=timeframe,
            use_cache=True,
            sandbox=False,
            fallback_to_stale=not force,
            fallback_to_yfinance=False,
            include_funding=False,
            cache_end_tolerance=pd.Timedelta("5min") if timeframe == "1h" else pd.Timedelta("10min"),
        )
    except Exception as exc:
        _record_refresh_failure(symbol, timeframe, end, exc)


def _record_refresh_failure(symbol: str, timeframe: str, end: pd.Timestamp, exc: Exception) -> None:
    try:
        path = BASE_DIR / "logs" / "data_refresh" / "c_auto_inline_refresh_errors.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "target_end": end.isoformat(),
                "error": str(exc),
            }, ensure_ascii=False, sort_keys=True) + "\n")
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
        "ret_1",
        "ret_1_abs",
        "ret_3",
        "range_pct",
        "close_to_high",
        "close_to_low",
        "h4_ret_1",
        "h4_ret_6",
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


def _build_portfolio_scores(
    df: pd.DataFrame,
    *,
    enable_trend_pullback: bool = False,
    enable_daily_fib: bool = False,
) -> pd.DataFrame:
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
    out["blocked_by_short_decay"] = _blocked_by_short_decay(out)
    out.loc[out["blocked_by_short_decay"], "eligible"] = False
    out["eligible"] = (
        out["score"].notna()
        & (out["horizon_hours"] > 0)
        & (pd.to_numeric(out["train_eligible_90d"], errors="coerce").fillna(0.0) > 0)
        & (pd.to_numeric(out["volume_usd"], errors="coerce").fillna(0.0) >= 0.0)
        & ~out["blocked_by_crowding"]
        & ~out["blocked_by_short_decay"]
    )
    out["trend_pullback_eligible"] = False
    out["trend_pullback_side"] = ""
    out["trend_pullback_score"] = np.nan
    if enable_trend_pullback:
        _attach_trend_pullback_reversal(out)
    out["daily_fib_eligible"] = False
    out["daily_fib_side"] = ""
    out["daily_fib_score"] = np.nan
    out["daily_fib_support"] = np.nan
    if enable_daily_fib:
        _attach_daily_fib_support_rebound(out)
    return out


def _attach_trend_pullback_reversal(out: pd.DataFrame) -> None:
    regime = out["btc_regime_6"].astype(str)
    h4_ret_6 = _numeric_col(out, "h4_ret_6")
    h4_ret_1 = _numeric_col(out, "h4_ret_1")
    ret_1 = _numeric_col(out, "ret_1")
    ret_3 = _numeric_col(out, "ret_3")
    range_pct = _numeric_col(out, "range_pct").abs()
    close_to_high = _numeric_col(out, "close_to_high", -1.0)
    ret_1_abs = _numeric_col(out, "ret_1_abs").where(lambda s: s.notna(), ret_1.abs())
    counter_limit = np.minimum(0.045, np.maximum(0.008, 4.0 * ret_1_abs.abs()))
    constructive_regime = regime.isin({"bull", "chop_long", "strong_bull"})
    controlled_pullback = (ret_3 < 0) & (ret_3.abs() <= counter_limit)
    reversal = (ret_1 > 0) & (
        (close_to_high >= -0.0015)
        | ((ret_1 > range_pct * 0.25) & (close_to_high >= -0.003))
    )
    eligible = (
        constructive_regime
        & (h4_ret_6 > 0.012)
        & (h4_ret_1 > -0.005)
        & controlled_pullback
        & reversal
        & (pd.to_numeric(out.get("train_eligible_90d"), errors="coerce").fillna(0.0) > 0)
    )
    out.loc[eligible, "trend_pullback_eligible"] = True
    out.loc[eligible, "trend_pullback_side"] = "long"
    score = (
        h4_ret_6.clip(lower=0.0, upper=0.08) * 0.45
        + ret_1.clip(lower=0.0, upper=0.04) * 0.35
        + (ret_3.abs() / counter_limit.replace(0, np.nan)).fillna(0.0).clip(0.0, 1.0) * 0.01
    )
    out.loc[eligible, "trend_pullback_score"] = score[eligible]


def _attach_daily_fib_support_rebound(out: pd.DataFrame) -> None:
    for idx, row in out.iterrows():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        idx_ts = idx[0] if isinstance(idx, tuple) else idx
        signal = _daily_fib_signal_for_symbol(symbol, pd.Timestamp(idx_ts), row)
        if not signal:
            continue
        out.loc[idx, "daily_fib_eligible"] = True
        out.loc[idx, "daily_fib_side"] = "long"
        out.loc[idx, "daily_fib_score"] = signal["score"]
        out.loc[idx, "daily_fib_support"] = signal["support"]


def _daily_fib_signal_for_symbol(symbol: str, now_ts: pd.Timestamp, row: pd.Series) -> dict[str, float] | None:
    safe = symbol.replace("/", "_").replace(":", "_")
    daily = _load_ohlcv_cache_by_safe(safe, "1d")
    h4 = _load_ohlcv_cache_by_safe(safe, "4h")
    if len(daily) < 80 or len(h4) < 12:
        return None
    daily = daily.loc[daily.index <= now_ts]
    h4 = h4.loc[h4.index <= now_ts]
    if len(daily) < 80 or len(h4) < 6:
        return None
    close_d = daily["close"].astype(float)
    high = daily["high"].rolling(60, min_periods=30).max().shift(1)
    low = daily["low"].rolling(60, min_periods=30).min().shift(1)
    sma = close_d.rolling(40, min_periods=20).mean()
    impulse = high / low - 1.0
    if not bool((close_d.iloc[-1] >= sma.iloc[-1]) and (impulse.iloc[-1] >= 0.12) and (high.iloc[-1] > low.iloc[-1])):
        return None
    support = float(high.iloc[-1] - 0.786 * (high.iloc[-1] - low.iloc[-1]))
    recent = h4.tail(6)
    last = recent.iloc[-1]
    prev = recent.iloc[-2]
    close = float(last["close"])
    low_4h = float(last["low"])
    open_4h = float(last["open"])
    if support <= 0 or close <= 0 or low_4h <= 0:
        return None
    touched = low_4h <= support * 1.004
    not_broken = low_4h >= support * (1.0 - 0.012)
    reclaimed = close >= support * 1.001
    distance_ok = close / support - 1.0 <= 0.012
    confirmed = ((close > open_4h) and (close > float(prev["close"]))) or (close > float(prev["high"]))
    if not (touched and not_broken and reclaimed and distance_ok and confirmed):
        return None
    row_close = float(row.get("close") or close)
    if row_close > 0 and abs(row_close / close - 1.0) > 0.035:
        return None
    score = min(0.08, max(0.0, float(impulse.iloc[-1]))) * 0.35
    score += min(0.04, max(0.0, close / support - 1.0)) * 0.35
    score += min(0.04, max(0.0, close / float(prev["close"]) - 1.0)) * 0.30
    return {"support": support, "score": max(score, 0.001)}


def _load_ohlcv_cache_by_safe(safe_symbol: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{safe_symbol}_futures_{timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _blocked_by_short_decay(df: pd.DataFrame) -> pd.Series:
    side = df["side"].astype(str)
    ret_1 = _numeric_col(df, "ret_1")
    ret_3 = _numeric_col(df, "ret_3")
    h4_ret_1 = _numeric_col(df, "h4_ret_1")
    h4_ret_6 = _numeric_col(df, "h4_ret_6")
    close_to_low = _numeric_col(df, "close_to_low", 1.0)
    latest_candle_faded = ret_1 <= -0.001
    near_intrabar_low = close_to_low <= 0.004
    no_4h_rebound = h4_ret_1 <= 0.0
    no_24h_repair = h4_ret_6 <= 0.004
    recent_bounce_contained = ret_3 <= 0.006
    decay_confirmed = (
        latest_candle_faded
        & near_intrabar_low
        & no_4h_rebound
        & no_24h_repair
        & recent_bounce_contained
    )
    return (side == "short") & ~decay_confirmed


def _numeric_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


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
        protective_exit = _protective_exit(symbol, pos, args)
        if protective_exit is None and now_ts < exit_ts:
            continue
        if protective_exit is not None:
            exit_price = protective_exit["price"]
            reason = protective_exit["reason"]
            event_ts = protective_exit["ts"]
        else:
            exit_price = _latest_price(latest_features, symbol)
            reason = "horizon"
            event_ts = now_ts
        entry_price = float(pos.get("entry_price") or 0.0)
        notional = float(pos.get("risk_budget") or 0.0)
        if not _valid_number(exit_price) or entry_price <= 0 or notional <= 0:
            continue
        net_return = _net_return(str(pos.get("side")), entry_price, exit_price, args)
        pnl = notional * net_return
        realized_nav += pnl
        ledger.append(
            {
                "ts": event_ts.isoformat(),
                "event": "exit",
                "symbol": symbol,
                "side": pos.get("side"),
                "reason": reason,
                "pnl": pnl,
                "net_return": net_return,
                "exit_price": exit_price,
                "source_strategy_id": pos.get("source_strategy_id"),
                "signal_family": pos.get("signal_family"),
            }
        )
        positions.pop(symbol)
    return positions, ledger, realized_nav


def _enforce_thesis_exits(
    args: argparse.Namespace,
    positions: dict[str, dict[str, Any]],
    scored: pd.DataFrame,
    latest_features: pd.DataFrame,
    now_ts: pd.Timestamp,
    realized_nav: float,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], float]:
    if not bool(getattr(args, "thesis_exit_enabled", True)) or not positions:
        return positions, [], realized_nav
    current_signals = _current_position_signals(args, positions, scored, now_ts)
    remaining = dict(positions)
    events: list[dict[str, Any]] = []
    nav = float(realized_nav)
    for symbol, pos in list(positions.items()):
        entry_ts = _parse_event_ts(pos.get("entry_ts"))
        if entry_ts is not None:
            held_hours = (now_ts - entry_ts).total_seconds() / 3600.0
            if held_hours < float(args.thesis_min_hold_hours):
                continue
        decision = evaluate_position_thesis(
            {"symbol": symbol, **pos},
            current_signals,
            score_retain=float(args.thesis_score_retain),
            min_score=float(args.thesis_min_score),
        )
        if not decision.should_exit:
            updated = dict(pos)
            updated["thesis_last_check"] = {
                "ts": now_ts.isoformat(),
                "reason": decision.reason,
                "current_score": decision.current_score,
                "entry_score": decision.entry_score,
            }
            remaining[symbol] = updated
            continue
        exit_price = _latest_price(latest_features, symbol)
        entry_price = float(pos.get("entry_price") or 0.0)
        notional = float(pos.get("risk_budget") or 0.0)
        if not _valid_number(exit_price) or entry_price <= 0 or notional <= 0:
            continue
        net_return = _net_return(str(pos.get("side")), entry_price, exit_price, args)
        pnl = notional * net_return
        nav += pnl
        events.append(
            {
                "ts": now_ts.isoformat(),
                "event": "exit",
                "symbol": symbol,
                "side": pos.get("side"),
                "reason": decision.reason,
                "pnl": pnl,
                "net_return": net_return,
                "exit_price": exit_price,
                "source_strategy_id": pos.get("source_strategy_id"),
                "signal_family": pos.get("signal_family"),
                "thesis": {
                    "severity": decision.severity,
                    "current_score": decision.current_score,
                    "entry_score": decision.entry_score,
                    "details": decision.details or {},
                    "contract": pos.get("thesis_contract"),
                },
            }
        )
        remaining.pop(symbol, None)
    return remaining, events, nav


def _current_position_signals(
    args: argparse.Namespace,
    positions: dict[str, dict[str, Any]],
    scored: pd.DataFrame,
    now_ts: pd.Timestamp,
) -> list[Any]:
    try:
        group = scored.xs(now_ts, level="timestamp", drop_level=False).reset_index()
    except Exception:
        return []
    group = group[group["symbol"].astype(str).isin(set(positions))].copy()
    if group.empty:
        return []
    group["volume_usd"] = pd.to_numeric(group["volume_usd"], errors="coerce").fillna(0.0)
    group = group[group["volume_usd"] >= float(args.min_volume_usd)]
    return build_committee_signals(
        group,
        now_ts,
        base_capital=float(args.fixed_notional_capital),
        base_risk=float(args.base_risk),
        fee_slip_rate=_round_trip_cost_rate(args),
    )


def _open_live_positions(
    scored: pd.DataFrame,
    positions: dict[str, dict[str, Any]],
    now_ts: pd.Timestamp,
    args: argparse.Namespace,
    risk_events: list[dict[str, Any]] | None = None,
    cooldown_symbols: set[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    group = scored.xs(now_ts, level="timestamp", drop_level=False).reset_index()
    group = group[~group["symbol"].isin(positions)]
    events: list[dict[str, Any]] = []
    blocked_by_cooldown = set(cooldown_symbols or set())
    if blocked_by_cooldown:
        group = group[~group["symbol"].astype(str).isin(blocked_by_cooldown)]
        for symbol in sorted(blocked_by_cooldown):
            events.append(
                {
                    "ts": now_ts.isoformat(),
                    "event": "committee_note",
                    "symbol": symbol,
                    "side": None,
                    "reason": f"rejected post_exit_cooldown_{float(args.post_exit_cooldown_hours):g}h",
                    "pnl": None,
                    "net_return": None,
                }
            )
    group["volume_usd"] = pd.to_numeric(group["volume_usd"], errors="coerce").fillna(0.0)
    group = group[group["volume_usd"] >= float(args.min_volume_usd)]
    if group.empty:
        return {}, events + [
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
    short_cooldown = _short_loss_cooldown_status(
        risk_events or [],
        now_ts,
        cooldown_hours=float(args.short_loss_cooldown_hours),
        lookback_hours=float(args.short_loss_lookback_hours),
        min_losses=int(args.short_loss_cooldown_min_losses),
    )
    if short_cooldown["active"]:
        short_mask = group["side"].astype(str) == "short"
        if bool(short_mask.any()):
            group.loc[short_mask, "eligible"] = False
            group.loc[short_mask, "short_entries_disabled"] = True
            events.append(
                {
                    "ts": now_ts.isoformat(),
                    "event": "committee_note",
                    "symbol": None,
                    "side": "short",
                    "reason": (
                        f"rejected portfolio_short_loss_cooldown_"
                        f"{float(args.short_loss_cooldown_hours):g}h"
                    ),
                    "pnl": None,
                    "net_return": None,
                    "short_cooldown": short_cooldown,
                }
            )
    c_auto_group = group[group["eligible"].astype(bool)].copy()
    if not c_auto_group.empty:
        threshold = c_auto_group.groupby("side")["score"].transform(lambda s: s.quantile(float(args.min_score_quantile)))
        c_auto_symbols = set(c_auto_group[c_auto_group["score"] >= threshold]["symbol"].astype(str))
        group.loc[~group["symbol"].astype(str).isin(c_auto_symbols), "eligible"] = False
    per_strategy_cap = max(1, int(getattr(args, "paper_max_positions_per_strategy", 5)))
    _limit_strategy_candidates(
        group,
        positions,
        strategy_id="trend_pullback_reversal_long",
        eligible_col="trend_pullback_eligible",
        score_col="trend_pullback_score",
        max_open=per_strategy_cap,
    )
    _limit_strategy_candidates(
        group,
        positions,
        strategy_id="daily_fib_support_rebound_long",
        eligible_col="daily_fib_eligible",
        score_col="daily_fib_score",
        max_open=per_strategy_cap,
    )
    signals = build_committee_signals(
        group,
        now_ts,
        base_capital=float(args.fixed_notional_capital),
        base_risk=float(args.base_risk),
        fee_slip_rate=_round_trip_cost_rate(args),
    )
    signals = _filter_signals_by_strategy_cap(
        signals,
        positions,
        max_open=max(1, int(getattr(args, "paper_max_positions_per_strategy", 5))),
    )
    result = arbitrate_signals(
        signals,
        positions,
        now_ts,
        initial_capital=float(args.initial_capital),
        realized_nav=float(args.fixed_notional_capital),
        max_positions=int(args.max_positions),
        max_decisions=max(0, int(args.max_positions) - len(positions)),
        max_total_budget_usdt=min(
            float(args.fixed_notional_capital) * float(args.base_risk) * float(args.max_positions),
            float(args.initial_capital) * float(args.max_gross_leverage),
        ),
        min_ev=0.0,
    )
    opened: dict[str, dict[str, Any]] = {}
    row_by_symbol = {str(row["symbol"]): row for _, row in group.iterrows()}
    for decision in result.decisions:
        signal = decision.signal
        symbol = str(signal.symbol)
        row = row_by_symbol.get(symbol, {})
        entry = float(signal.entry)
        if not _valid_number(entry) or entry <= 0:
            continue
        side = str(signal.side)
        if signal.stop is None or signal.target is None:
            events.append(
                {
                    "ts": now_ts.isoformat(),
                    "event": "entry_rejected",
                    "symbol": symbol,
                    "side": side,
                    "reason": "missing_explicit_stop_or_target",
                    "pnl": None,
                    "net_return": None,
                }
            )
            continue
        requested_notional = float(decision.size_usdt)
        leverage_policy = _leverage_policy(signal, requested_notional, args, positions)
        risk_budget = float(leverage_policy["notional_usdt"])
        if risk_budget <= 0:
            events.append(
                {
                    "ts": now_ts.isoformat(),
                    "event": "committee_note",
                    "symbol": symbol,
                    "side": side,
                    "reason": "leverage_policy_blocked_notional",
                    "pnl": None,
                    "net_return": None,
                    "requested_notional_usdt": requested_notional,
                    "leverage_policy": leverage_policy,
                }
            )
            continue
        leverage = float(leverage_policy["leverage"])
        margin_required = float(leverage_policy["margin_required_usdt"])
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
            "notional_usdt": risk_budget,
            "leverage": leverage,
            "margin_required_usdt": margin_required,
            "stop_account_loss_usdt": leverage_policy["stop_account_loss_usdt"],
            "stop_account_loss_pct": leverage_policy["stop_account_loss_pct"],
            "stop_margin_loss_pct": leverage_policy["stop_margin_loss_pct"],
            "leverage_policy": leverage_policy,
            "entry_price": entry,
            "stop_price": float(signal.stop) if signal.stop is not None else None,
            "tp1_price": float(signal.target) if signal.target is not None else None,
            "tp2_price": entry * (1.0 + target_pct * 1.75) if side == "long" else entry * (1.0 - target_pct * 1.75),
            "regime": row.get("btc_regime_6") if hasattr(row, "get") else signal.metadata.get("regime"),
            "signal_family": signal.metadata.get("signal_family") or signal.strategy_id,
            "source_strategy_id": signal.strategy_id,
            "committee_metadata": dict(signal.metadata),
            "thesis_contract": signal.metadata.get("thesis_contract"),
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
                "notional_usdt": risk_budget,
                "leverage": leverage,
                "stop_price": float(signal.stop),
                "tp1_price": float(signal.target),
                "tp2_price": opened[symbol]["tp2_price"],
                "source_strategy_id": signal.strategy_id,
                "signal_family": signal.metadata.get("signal_family") or signal.strategy_id,
                "thesis_contract": signal.metadata.get("thesis_contract"),
                "margin_required_usdt": margin_required,
                "stop_account_loss_usdt": leverage_policy["stop_account_loss_usdt"],
                "stop_account_loss_pct": leverage_policy["stop_account_loss_pct"],
                "stop_margin_loss_pct": leverage_policy["stop_margin_loss_pct"],
                "leverage_policy": leverage_policy,
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


def _leverage_policy(
    signal: Any,
    requested_notional: float,
    args: argparse.Namespace,
    positions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    stop_pct = max(abs(float(getattr(signal, "loss_pct", 0.0) or 0.0)), 0.001)
    metadata = dict(getattr(signal, "metadata", {}) or {})
    kit_disagreement, kit_confirmation = infer_kit_alignment(metadata)
    if bool(getattr(args, "paper_force_kit_confirmation", False)):
        kit_disagreement = False
        kit_confirmation = True
    side = str(getattr(signal, "side", "") or "")
    symbol = str(getattr(signal, "symbol", "") or "")
    open_positions = dict(positions or {})
    same_side_open_count = sum(1 for pos in open_positions.values() if str(pos.get("side") or "") == side)
    policy = compute_committee_leverage_policy(
        CommitteeLeverageInputs(
            requested_notional_usdt=float(requested_notional),
            nav_usdt=float(args.initial_capital),
            stop_pct=stop_pct,
            requested_leverage=max(1.0, float(args.default_leverage)),
            configured_max_leverage=max(1.0, float(args.max_leverage)),
            max_position_nav_loss_pct=max(0.0, float(args.max_position_nav_loss_pct)),
            max_stop_margin_loss_pct=max(0.001, float(args.max_stop_margin_loss_pct)),
            same_side_open_count=same_side_open_count,
            same_symbol_open=symbol in open_positions,
            kit_disagreement=kit_disagreement,
            kit_confirmation=kit_confirmation,
            allow_aggressive_leverage=bool(args.allow_aggressive_leverage),
            metadata={
                "strategy_id": getattr(signal, "strategy_id", None),
                "symbol": symbol,
                "side": side,
                "same_side_open_count": same_side_open_count,
            },
        )
    )
    policy["legacy_policy_id"] = "c_auto_v2_committee_leverage_v1"
    return policy


def _short_loss_cooldown_status(
    events: list[dict[str, Any]],
    now_ts: pd.Timestamp,
    *,
    cooldown_hours: float,
    lookback_hours: float,
    min_losses: int,
) -> dict[str, Any]:
    now = pd.Timestamp(now_ts)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")
    if cooldown_hours <= 0 or lookback_hours <= 0 or min_losses <= 0:
        return {"active": False, "losses": 0, "cooldown_until": ""}
    lookback = pd.Timedelta(hours=float(lookback_hours))
    short_losses: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event") or "") not in {"exit", "forced_exit"}:
            continue
        if str(event.get("side") or "") != "short":
            continue
        try:
            pnl = float(event.get("pnl"))
        except Exception:
            continue
        if pnl >= 0:
            continue
        ts = _parse_event_ts(event.get("ts"))
        if ts is None or now - ts > lookback:
            continue
        short_losses.append({"ts": ts, "symbol": event.get("symbol"), "pnl": pnl, "reason": event.get("reason")})
    if len(short_losses) < int(min_losses):
        return {"active": False, "losses": len(short_losses), "cooldown_until": ""}
    last_ts = max(item["ts"] for item in short_losses)
    cooldown_until = last_ts + pd.Timedelta(hours=float(cooldown_hours))
    return {
        "active": now < cooldown_until,
        "losses": len(short_losses),
        "cooldown_until": cooldown_until.isoformat(),
        "lookback_hours": float(lookback_hours),
        "cooldown_hours": float(cooldown_hours),
        "min_losses": int(min_losses),
        "recent_losses": [
            {
                "ts": item["ts"].isoformat(),
                "symbol": item["symbol"],
                "pnl": item["pnl"],
                "reason": item["reason"],
            }
            for item in short_losses[-5:]
        ],
    }


def _recent_exit_symbols(events: list[dict[str, Any]], now_ts: pd.Timestamp, cooldown_hours: float) -> set[str]:
    if cooldown_hours <= 0:
        return set()
    now = pd.Timestamp(now_ts)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")
    window = pd.Timedelta(hours=float(cooldown_hours))
    symbols: set[str] = set()
    for event in events:
        if str(event.get("event") or "") not in {"exit", "forced_exit"}:
            continue
        symbol = str(event.get("symbol") or "")
        if not symbol:
            continue
        ts = _parse_event_ts(event.get("ts"))
        if ts is not None and now - ts <= window:
            symbols.add(symbol)
    return symbols


def _parse_event_ts(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _limit_strategy_candidates(
    group: pd.DataFrame,
    positions: dict[str, dict[str, Any]],
    *,
    strategy_id: str,
    eligible_col: str,
    score_col: str,
    max_open: int,
) -> None:
    if eligible_col not in group.columns:
        return
    open_count = sum(
        1
        for pos in positions.values()
        if str(pos.get("source_strategy_id") or "") == strategy_id
    )
    slots = max(0, int(max_open) - int(open_count))
    eligible = group[eligible_col].astype(bool)
    if slots <= 0:
        group.loc[eligible, eligible_col] = False
        return
    if int(eligible.sum()) <= slots:
        return
    score = pd.to_numeric(group.loc[eligible, score_col], errors="coerce").fillna(-math.inf)
    keep_idx = set(score.nlargest(slots).index)
    drop_idx = [
        idx
        for idx in eligible[eligible].index
        if idx not in keep_idx
    ]
    group.loc[drop_idx, eligible_col] = False


def _filter_signals_by_strategy_cap(
    signals: list[Any],
    positions: dict[str, dict[str, Any]],
    max_open: int,
) -> list[Any]:
    open_count: dict[str, int] = {}
    for pos in positions.values():
        sid = str(pos.get("source_strategy_id") or "")
        if sid:
            open_count[sid] = open_count.get(sid, 0) + 1
    accepted: list[Any] = []
    new_count: dict[str, int] = {}
    for signal in sorted(signals, key=lambda s: (float(getattr(s, "confidence", 0.0) or 0.0), float(getattr(s, "forward_ev", 0.0) or 0.0)), reverse=True):
        sid = str(getattr(signal, "strategy_id", "") or "")
        if open_count.get(sid, 0) + new_count.get(sid, 0) >= int(max_open):
            continue
        accepted.append(signal)
        new_count[sid] = new_count.get(sid, 0) + 1
    return accepted


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


def _protective_exit(symbol: str, pos: dict[str, Any], args: argparse.Namespace) -> dict[str, Any] | None:
    entry_ts = pd.Timestamp(pos.get("entry_ts"))
    if entry_ts.tzinfo is None:
        entry_ts = entry_ts.tz_localize("UTC")
    else:
        entry_ts = entry_ts.tz_convert("UTC")
    end = pd.Timestamp.now(tz="UTC").floor("5min")
    if end <= entry_ts:
        return None
    bars = _load_ohlcv_cache(symbol, "5m", entry_ts, end)
    if bars.empty:
        return None
    bars = bars.loc[bars.index > entry_ts]
    if bars.empty:
        return None
    side = str(pos.get("side") or "")
    stop = _json_float(pos.get("stop_price"))
    target = _json_float(pos.get("tp1_price"))
    for ts, row in bars.iterrows():
        high = _json_float(row.get("high"))
        low = _json_float(row.get("low"))
        if side == "long":
            if stop is not None and low is not None and low <= stop:
                return {"reason": "stop", "price": stop, "ts": ts}
            if target is not None and high is not None and high >= target:
                return {"reason": "target", "price": target, "ts": ts}
        elif side == "short":
            if stop is not None and high is not None and high >= stop:
                return {"reason": "stop", "price": stop, "ts": ts}
            if target is not None and low is not None and low <= target:
                return {"reason": "target", "price": target, "ts": ts}
    return None


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
                "blocked_by_short_decay": bool(row.get("blocked_by_short_decay", False)),
                "trend_pullback_eligible": bool(row.get("trend_pullback_eligible", False)),
                "trend_pullback_score": _json_float(row.get("trend_pullback_score")),
                "daily_fib_eligible": bool(row.get("daily_fib_eligible", False)),
                "daily_fib_score": _json_float(row.get("daily_fib_score")),
                "daily_fib_support": _json_float(row.get("daily_fib_support")),
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
