#!/usr/bin/env python3
"""Delayed-execution portfolio backtest for the C-Auto v2 sleeve policy."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
DEFAULT_POLICY = ENGINE_DIR / "strategies" / "specs" / "c_auto_v2_regime_policy.json"
DEFAULT_OUT_ID = "c_auto_v2_portfolio_backtest_v1"


@dataclass
class Position:
    symbol: str
    side: str
    regime: str
    score: float
    signal_ts: pd.Timestamp
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    notional: float
    horizon_hours: int
    fold_ids: dict[str, int]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto v2 delayed-execution portfolio backtest")
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--dataset-id", default="")
    p.add_argument("--out-id", default=DEFAULT_OUT_ID)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--max-positions", type=int, default=5)
    p.add_argument("--rebalance-hours", type=int, default=6)
    p.add_argument("--entry-delay-hours", type=int, default=1)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--base-risk", type=float, default=0.18, help="NAV fraction per new position before regime scalar")
    p.add_argument("--min-score-quantile", type=float, default=0.80)
    p.add_argument("--min-volume-usd", type=float, default=100000.0)
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    policy_path = Path(args.policy)
    policy = json.loads(policy_path.read_text())
    dataset_id = args.dataset_id or str(policy["dataset_id"])
    dataset_dir = ENGINE_DIR / "data" / "features" / dataset_id
    out_dir = ENGINE_DIR / "data" / "research" / "c_auto" / args.out_id
    out_dir.mkdir(parents=True, exist_ok=True)

    features = _read_frame(dataset_dir / "features.parquet", dataset_dir / "features.pkl")
    features = features.sort_index()
    if args.start:
        features = _slice_ts(features, start=args.start, end=args.end)
    elif args.end:
        features = _slice_ts(features, start="", end=args.end)

    predictions = _load_policy_predictions(policy)
    if predictions.empty:
        raise RuntimeError("No policy predictions found. Run sleeve experiments first.")
    predictions = predictions.loc[predictions.index.intersection(features.index)].copy()
    predictions = predictions.join(
        features[
            [
                "close",
                "volume_usd",
                "funding_z_24",
                "funding_rate",
                "oi_z_24",
                "ls_z_24",
                "train_eligible_90d",
                "btc_regime_6",
            ]
        ],
        how="left",
    )
    predictions = _build_portfolio_scores(predictions)

    folds = _load_folds(dataset_dir)
    result = _simulate(predictions, args)
    equity = pd.DataFrame(result["equity"])
    trades = pd.DataFrame(result["trades"])
    metrics = _metrics(equity, trades, args)
    by_regime = _group_metrics(trades, "regime")
    by_side = _group_metrics(trades, "side")
    leakage = _leakage_check(trades, folds)

    _write_frame(equity, out_dir / "equity_curve.parquet")
    _write_frame(trades, out_dir / "trades.parquet")
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    trades.to_csv(out_dir / "trades.csv", index=False)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backtest_id": args.out_id,
        "policy_id": policy.get("policy_id"),
        "policy_path": str(policy_path),
        "dataset_id": dataset_id,
        "inputs": {
            "initial_capital": args.initial_capital,
            "max_positions": args.max_positions,
            "rebalance_hours": args.rebalance_hours,
            "entry_delay_hours": args.entry_delay_hours,
            "fee_bps_per_side": args.fee_bps_per_side,
            "slippage_bps_per_side": args.slippage_bps_per_side,
            "base_risk": args.base_risk,
            "min_score_quantile": args.min_score_quantile,
            "min_volume_usd": args.min_volume_usd,
            "start": args.start,
            "end": args.end,
        },
        "metrics": metrics,
        "by_regime": by_regime,
        "by_side": by_side,
        "leakage_check": leakage,
        "artifacts": {
            "equity_curve": "equity_curve.csv",
            "trades": "trades.csv",
            "summary": "summary.md",
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out_dir / "summary.md").write_text(_summary_markdown(manifest))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _read_frame(parquet: Path, pkl: Path) -> pd.DataFrame:
    if parquet.exists():
        return pd.read_parquet(parquet)
    if pkl.exists():
        return pd.read_pickle(pkl)
    raise FileNotFoundError(f"Missing {parquet} or {pkl}")


def _load_folds(dataset_dir: Path) -> dict[int, dict[str, pd.Timestamp]]:
    path = dataset_dir / "walk_forward_folds.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    out: dict[int, dict[str, pd.Timestamp]] = {}
    for row in raw:
        fold = int(row["fold"])
        out[fold] = {
            "train_start": pd.Timestamp(row["train_start"]),
            "train_end": pd.Timestamp(row["train_end"]),
            "test_start": pd.Timestamp(row["test_start"]),
            "test_end": pd.Timestamp(row["test_end"]),
        }
    return out


def _slice_ts(df: pd.DataFrame, *, start: str, end: str) -> pd.DataFrame:
    ts = pd.to_datetime(df.index.get_level_values("timestamp"), utc=True)
    mask = pd.Series(True, index=df.index)
    if start:
        mask &= ts >= pd.Timestamp(start, tz="UTC")
    if end:
        mask &= ts <= pd.Timestamp(end, tz="UTC")
    return df.loc[mask.to_numpy()]


def _load_policy_predictions(policy: dict[str, Any]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for sleeve in policy.get("sleeves", []):
        sleeve_id = str(sleeve["sleeve_id"])
        for exp in sleeve.get("experiments", []):
            exp_id = str(exp["experiment_id"])
            path = ENGINE_DIR / "data" / "research" / "c_auto" / exp_id / "predictions.parquet"
            alt = path.with_suffix(".pkl")
            if not path.exists() and not alt.exists():
                continue
            pred = _read_frame(path, alt)
            col = _score_col(sleeve_id, str(exp["regime"]), str(exp["label_col"]))
            keep = ["prediction"]
            if "fold" in pred.columns:
                keep.append("fold")
            frame = pred[keep].rename(columns={"prediction": col, "fold": f"{col}__fold"})
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1).sort_index()
    return out.loc[:, ~out.columns.duplicated()]


def _score_col(sleeve_id: str, regime: str, label_col: str) -> str:
    side = "short" if "_short_" in label_col else "long"
    horizon = "".join(ch for ch in label_col.split("_")[-1] if ch.isdigit())
    return f"{sleeve_id}__{regime}__{side}{horizon}"


def _build_portfolio_scores(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
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
    funding_z = pd.to_numeric(df["funding_z_24"], errors="coerce").fillna(0.0)
    oi_z = pd.to_numeric(df["oi_z_24"], errors="coerce").fillna(0.0)
    ls_z = pd.to_numeric(df["ls_z_24"], errors="coerce").fillna(0.0)
    funding = pd.to_numeric(df["funding_rate"], errors="coerce").fillna(0.0)
    side = df["side"].astype(str)
    late_long = (side == "long") & (funding_z > 2.5) & (oi_z > 1.5) & (ls_z > 1.5)
    crowded_short = (side == "short") & (funding_z < -2.5) & (oi_z > 1.5) & (ls_z < -1.5)
    extreme_funding = ((side == "long") & (funding > 0.0015)) | ((side == "short") & (funding < -0.0015))
    return late_long | crowded_short | extreme_funding


def _simulate(scores: pd.DataFrame, args: argparse.Namespace) -> dict[str, list[dict[str, Any]]]:
    close = scores["close"].unstack("symbol").sort_index()
    timeline = pd.DatetimeIndex(close.index).sort_values()
    score_ts = pd.DatetimeIndex(scores.index.get_level_values("timestamp").unique()).sort_values()
    active: dict[str, Position] = {}
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    realized_nav = float(args.initial_capital)
    fee_slip_rate = 2.0 * (args.fee_bps_per_side + args.slippage_bps_per_side) / 10000.0

    for ts in score_ts:
        if ts not in timeline:
            continue
        realized_nav, closed = _close_due(ts, active, close, realized_nav, fee_slip_rate)
        trades.extend(closed)
        mtm_nav = _mark_to_market_nav(ts, active, close, realized_nav, fee_slip_rate)
        if _is_rebalance_ts(ts, args.rebalance_hours):
            group = scores.xs(ts, level="timestamp", drop_level=False)
            candidates = _select_candidates(ts, group, close, active, args)
            for row in candidates:
                if len(active) >= args.max_positions:
                    break
                symbol = str(row["symbol"])
                if symbol in active:
                    continue
                entry_ts = _offset_ts(timeline, ts, args.entry_delay_hours)
                exit_ts = _offset_ts(timeline, entry_ts, int(row["horizon_hours"]))
                if entry_ts is None or exit_ts is None:
                    continue
                entry_price = _price(close, entry_ts, symbol)
                exit_price = _price(close, exit_ts, symbol)
                if not _valid_price(entry_price) or not _valid_price(exit_price):
                    continue
                notional = mtm_nav * float(args.base_risk) * float(row["risk_scalar"])
                if notional <= 0:
                    continue
                active[symbol] = Position(
                    symbol=symbol,
                    side=str(row["side"]),
                    regime=str(row["regime"]),
                    score=float(row["score"]),
                    signal_ts=ts,
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    entry_price=float(entry_price),
                    notional=float(notional),
                    horizon_hours=int(row["horizon_hours"]),
                    fold_ids=dict(row["fold_ids"]),
                )
                mtm_nav = _mark_to_market_nav(ts, active, close, realized_nav, fee_slip_rate)
        mtm_nav = _mark_to_market_nav(ts, active, close, realized_nav, fee_slip_rate)
        equity.append(
            {
                "ts": ts.isoformat(),
                "nav": mtm_nav,
                "nav_mtm": mtm_nav,
                "realized_nav": realized_nav,
                "unrealized_pnl": mtm_nav - realized_nav,
                "open_positions": len(active),
                "gross_exposure": sum(pos.notional for pos in active.values()),
            }
        )

    if len(timeline):
        final_ts = timeline[-1]
        realized_nav, closed = _close_due(final_ts, active, close, realized_nav, fee_slip_rate, force=True)
        trades.extend(closed)
        equity.append(
            {
                "ts": final_ts.isoformat(),
                "nav": realized_nav,
                "nav_mtm": realized_nav,
                "realized_nav": realized_nav,
                "unrealized_pnl": 0.0,
                "open_positions": len(active),
                "gross_exposure": sum(pos.notional for pos in active.values()),
            }
        )
    return {"equity": equity, "trades": trades}


def _is_rebalance_ts(ts: pd.Timestamp, rebalance_hours: int) -> bool:
    if rebalance_hours <= 1:
        return True
    return int(ts.hour) % rebalance_hours == 0


def _offset_ts(timeline: pd.DatetimeIndex, ts: pd.Timestamp, hours: int) -> pd.Timestamp | None:
    pos = timeline.searchsorted(ts)
    if pos >= len(timeline) or timeline[pos] != ts:
        return None
    target = pos + int(hours)
    if target >= len(timeline):
        return None
    return pd.Timestamp(timeline[target])


def _select_candidates(
    ts: pd.Timestamp,
    group: pd.DataFrame,
    close: pd.DataFrame,
    active: dict[str, Position],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    g = group.reset_index()
    g = g[g["eligible"].astype(bool)].copy()
    g = g[~g["symbol"].isin(active)]
    if g.empty:
        return []
    g["volume_usd"] = pd.to_numeric(g["volume_usd"], errors="coerce").fillna(0.0)
    g = g[g["volume_usd"] >= float(args.min_volume_usd)]
    if g.empty:
        return []
    threshold = g.groupby("side")["score"].transform(lambda s: s.quantile(float(args.min_score_quantile)))
    g = g[g["score"] >= threshold].copy()
    if g.empty:
        return []
    g["entry_ts"] = _offset_ts(pd.DatetimeIndex(close.index), ts, int(args.entry_delay_hours))
    g = g[g["entry_ts"].notna()]
    g = g.sort_values("score", ascending=False)
    rows = []
    for _, row in g.iterrows():
        fold_ids = _row_fold_ids(row)
        rows.append(
            {
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "regime": str(row["btc_regime_6"]),
                "score": float(row["score"]),
                "horizon_hours": int(row["horizon_hours"]),
                "risk_scalar": float(row["risk_scalar"]),
                "fold_ids": fold_ids,
            }
        )
    return rows


def _row_fold_ids(row: pd.Series) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in row.items():
        if not str(key).endswith("__fold"):
            continue
        if pd.isna(value):
            continue
        out[str(key).removesuffix("__fold")] = int(value)
    return out


def _close_due(
    ts: pd.Timestamp,
    active: dict[str, Position],
    close: pd.DataFrame,
    nav: float,
    fee_slip_rate: float,
    force: bool = False,
) -> tuple[float, list[dict[str, Any]]]:
    closed: list[dict[str, Any]] = []
    for symbol in list(active):
        pos = active[symbol]
        if not force and ts < pos.exit_ts:
            continue
        exit_ts = pos.exit_ts if not force else ts
        exit_price = _price(close, exit_ts, symbol)
        if not _valid_price(exit_price):
            continue
        raw = float(exit_price) / pos.entry_price - 1.0
        gross_ret = raw if pos.side == "long" else -raw
        net_ret = gross_ret - fee_slip_rate
        pnl = pos.notional * net_ret
        cost_usd = pos.notional * fee_slip_rate
        nav += pnl
        closed.append(
            {
                "signal_ts": pos.signal_ts.isoformat(),
                "entry_ts": pos.entry_ts.isoformat(),
                "exit_ts": pd.Timestamp(exit_ts).isoformat(),
                "symbol": pos.symbol,
                "side": pos.side,
                "regime": pos.regime,
                "horizon_hours": pos.horizon_hours,
                "score": pos.score,
                "entry_price": pos.entry_price,
                "exit_price": float(exit_price),
                "notional": pos.notional,
                "fold_ids": json.dumps(pos.fold_ids, sort_keys=True),
                "gross_return": gross_ret,
                "net_return": net_ret,
                "pnl": pnl,
                "cost_usd": cost_usd,
                "exit_reason": "forced_end" if force else "horizon",
            }
        )
        active.pop(symbol)
    return nav, closed


def _mark_to_market_nav(
    ts: pd.Timestamp,
    active: dict[str, Position],
    close: pd.DataFrame,
    realized_nav: float,
    fee_slip_rate: float,
) -> float:
    nav = float(realized_nav)
    for pos in active.values():
        px = _price(close, ts, pos.symbol)
        if not _valid_price(px):
            continue
        raw = float(px) / pos.entry_price - 1.0
        gross_ret = raw if pos.side == "long" else -raw
        nav += pos.notional * (gross_ret - fee_slip_rate)
    return nav


def _price(close: pd.DataFrame, ts: pd.Timestamp, symbol: str) -> float:
    try:
        return float(close.at[ts, symbol])
    except Exception:
        return float("nan")


def _valid_price(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if equity.empty:
        return {"status": "empty"}
    eq = equity.copy()
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.sort_values("ts")
    nav_col = "nav_mtm" if "nav_mtm" in eq.columns else "nav"
    nav = pd.to_numeric(eq[nav_col], errors="coerce")
    peak = nav.cummax()
    dd = nav / peak - 1.0
    rets = nav.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = 0.0
    if len(rets) > 2 and float(rets.std()) > 0:
        sharpe = float(rets.mean() / rets.std() * math.sqrt(365 * 24 / max(1, args.rebalance_hours)))
    total_return = float(nav.iloc[-1] / float(args.initial_capital) - 1.0)
    days = max(1e-9, (eq["ts"].iloc[-1] - eq["ts"].iloc[0]).total_seconds() / 86400.0)
    annualized = (1.0 + total_return) ** (365.0 / days) - 1.0 if total_return > -1 else -1.0

    if trades.empty:
        trade_metrics = {
            "trades": 0,
            "win_rate": 0.0,
            "avg_net_return": 0.0,
            "avg_pnl": 0.0,
            "total_cost_usd": 0.0,
            "turnover_usd": 0.0,
        }
    else:
        trade_metrics = {
            "trades": int(len(trades)),
            "win_rate": float((pd.to_numeric(trades["pnl"], errors="coerce") > 0).mean()),
            "avg_net_return": float(pd.to_numeric(trades["net_return"], errors="coerce").mean()),
            "avg_pnl": float(pd.to_numeric(trades["pnl"], errors="coerce").mean()),
            "total_cost_usd": float(pd.to_numeric(trades["cost_usd"], errors="coerce").sum()),
            "turnover_usd": float(pd.to_numeric(trades["notional"], errors="coerce").sum()),
        }

    return {
        "status": "ok",
        "start": eq["ts"].iloc[0].isoformat(),
        "end": eq["ts"].iloc[-1].isoformat(),
        "days": float(days),
        "initial_nav": float(args.initial_capital),
        "final_nav": float(nav.iloc[-1]),
        "final_realized_nav": float(pd.to_numeric(eq.get("realized_nav", nav), errors="coerce").iloc[-1]),
        "total_return": total_return,
        "annualized_return": float(annualized),
        "max_drawdown": float(dd.min()),
        "sharpe_like": sharpe,
        "avg_open_positions": float(pd.to_numeric(eq["open_positions"], errors="coerce").mean()),
        "max_open_positions": int(pd.to_numeric(eq["open_positions"], errors="coerce").max()),
        **trade_metrics,
    }


def _leakage_check(trades: pd.DataFrame, folds: dict[int, dict[str, pd.Timestamp]]) -> dict[str, Any]:
    if trades.empty:
        return {"status": "empty", "checked_trades": 0, "violations": 0}
    if not folds:
        return {"status": "missing_folds", "checked_trades": 0, "violations": 0}
    checked = 0
    missing_fold = 0
    violations: list[dict[str, Any]] = []
    for _, row in trades.iterrows():
        signal_ts = pd.Timestamp(row["signal_ts"])
        try:
            fold_ids = json.loads(row.get("fold_ids", "{}"))
        except Exception:
            fold_ids = {}
        if not fold_ids:
            missing_fold += 1
            continue
        for source, fold_raw in fold_ids.items():
            checked += 1
            fold = int(fold_raw)
            meta = folds.get(fold)
            if meta is None:
                violations.append({"source": source, "fold": fold, "signal_ts": signal_ts.isoformat(), "reason": "unknown_fold"})
                continue
            if not (meta["test_start"] <= signal_ts <= meta["test_end"]):
                violations.append(
                    {
                        "source": source,
                        "fold": fold,
                        "signal_ts": signal_ts.isoformat(),
                        "test_start": meta["test_start"].isoformat(),
                        "test_end": meta["test_end"].isoformat(),
                        "reason": "signal_outside_test_window",
                    }
                )
    status = "ok" if not violations and missing_fold == 0 else "warn"
    return {
        "status": status,
        "checked_trades": int(len(trades)),
        "checked_fold_refs": checked,
        "missing_fold_trades": missing_fold,
        "violations": len(violations),
        "violation_examples": violations[:20],
    }


def _group_metrics(trades: pd.DataFrame, col: str) -> dict[str, Any]:
    if trades.empty or col not in trades.columns:
        return {}
    out: dict[str, Any] = {}
    for key, group in trades.groupby(col):
        pnl = pd.to_numeric(group["pnl"], errors="coerce")
        out[str(key)] = {
            "trades": int(len(group)),
            "pnl": float(pnl.sum()),
            "win_rate": float((pnl > 0).mean()),
            "avg_net_return": float(pd.to_numeric(group["net_return"], errors="coerce").mean()),
        }
    return out


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:
        df.to_pickle(path.with_suffix(".pkl"))


def _summary_markdown(manifest: dict[str, Any]) -> str:
    m = manifest["metrics"]
    lines = [
        f"# {manifest['backtest_id']}",
        "",
        f"Created: {manifest['created_at']}",
        f"Policy: `{manifest['policy_id']}`",
        f"Dataset: `{manifest['dataset_id']}`",
        "",
        "## Metrics",
        "",
        f"- Final NAV: {m.get('final_nav', 0):.2f}",
        f"- Total return: {m.get('total_return', 0):.2%}",
        f"- Max drawdown: {m.get('max_drawdown', 0):.2%}",
        f"- Trades: {m.get('trades', 0)}",
        f"- Win rate: {m.get('win_rate', 0):.2%}",
        f"- Avg net return/trade: {m.get('avg_net_return', 0):.4%}",
        f"- Total costs: {m.get('total_cost_usd', 0):.2f}",
        f"- Leakage check: {manifest.get('leakage_check', {}).get('status', 'unknown')}",
        "",
        "## By Regime",
        "",
        "| Regime | Trades | PnL | Win Rate | Avg Net Return |",
        "|---|---:|---:|---:|---:|",
    ]
    for regime, row in sorted(manifest.get("by_regime", {}).items()):
        lines.append(
            f"| `{regime}` | {row['trades']} | {row['pnl']:.2f} | {row['win_rate']:.2%} | {row['avg_net_return']:.4%} |"
        )
    lines.extend(["", "## By Side", "", "| Side | Trades | PnL | Win Rate | Avg Net Return |", "|---|---:|---:|---:|---:|"])
    for side, row in sorted(manifest.get("by_side", {}).items()):
        lines.append(
            f"| `{side}` | {row['trades']} | {row['pnl']:.2f} | {row['win_rate']:.2%} | {row['avg_net_return']:.4%} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
