#!/usr/bin/env python3
"""Chronological backtest for the monster-coin watchlist strategy.

This intentionally reuses the same feature construction and scoring helpers as
the live watchlist scanner, then simulates entries/exits on 5m bars.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_monster_dataset import (  # noqa: E402
    DEFAULT_HISTORY_MANIFEST,
    OUT_ROOT,
    BARS,
    _load_symbol_data,
    _market_panels,
    _relpath,
    _sample_row,
)
from score_monster_watchlist import _score_row, _select_score_features  # noqa: E402

DEFAULT_TRAINING = OUT_ROOT / "monster_samples_clustered_5m_v1" / "samples.parquet"
DEFAULT_FEATURE_SUMMARY = OUT_ROOT / "monster_samples_clustered_5m_v1" / "feature_summary.csv"


@dataclass
class Position:
    symbol: str
    entry_ts: pd.Timestamp
    entry_price: float
    qty: float
    notional: float
    entry_fee: float
    stop_price: float
    take_profit_price: float
    peak_price: float
    score: float
    reasons: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest monster-coin watchlist strategy chronologically")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--training-samples", default=str(DEFAULT_TRAINING))
    p.add_argument("--feature-summary", default=str(DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--dataset-id", default="monster_backtest_v1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--rebalance-minutes", type=int, default=60)
    p.add_argument("--feature-count", type=int, default=25)
    p.add_argument("--score-threshold", type=float, default=0.75)
    p.add_argument("--max-ret-1h", type=float, default=0.25)
    p.add_argument("--min-volume-24h-proxy", type=float, default=0.0)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--capital-per-trade", type=float, default=0.15)
    p.add_argument("--max-positions", type=int, default=3)
    p.add_argument("--fee-bps-per-side", type=float, default=4.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=4.0)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--take-profit", type=float, default=0.25)
    p.add_argument("--trailing-stop", type=float, default=0.12)
    p.add_argument("--max-hold-hours", type=float, default=72.0)
    p.add_argument("--cooldown-hours", type=float, default=24.0)
    p.add_argument("--progress-every", type=int, default=2000)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.rebalance_minutes % 5 != 0:
        raise SystemExit("--rebalance-minutes must be a multiple of 5")

    created = pd.Timestamp.utcnow()
    manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(manifest["symbols"], args.timeframe)
    if not data:
        raise SystemExit("No symbol data loaded")

    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)
    training = pd.read_parquet(args.training_samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, training, args.feature_count)
    if not score_features:
        raise SystemExit("No score features selected")

    start_ts = _as_utc(args.start) if args.start else close_panel.index.min()
    end_ts = _as_utc(args.end) if args.end else close_panel.index.max()
    warmup_ts = close_panel.index.min() + pd.Timedelta(minutes=5 * BARS["7d"])
    start_ts = max(start_ts, warmup_ts)
    timeline = close_panel.loc[(close_panel.index >= start_ts) & (close_panel.index <= end_ts)].index
    if len(timeline) < 2:
        raise SystemExit("Backtest timeline is empty after start/end filtering")

    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)

    state = _run_backtest(args, data, close_panel, market, training, score_features, timeline)
    trades = pd.DataFrame(state["trades"])
    equity = pd.DataFrame(state["equity"])
    signals = pd.DataFrame(state["signals"])
    metrics = _metrics(equity, trades, args)

    trades_csv = out_dir / "trades.csv"
    trades_parquet = out_dir / "trades.parquet"
    equity_csv = out_dir / "equity_curve.csv"
    equity_parquet = out_dir / "equity_curve.parquet"
    signals_csv = out_dir / "signals.csv"
    signals_parquet = out_dir / "signals.parquet"
    metrics_path = out_dir / "metrics.json"
    manifest_path = out_dir / "manifest.json"

    trades.to_csv(trades_csv, index=False)
    equity.to_csv(equity_csv, index=False)
    signals.to_csv(signals_csv, index=False)
    if not trades.empty:
        trades.to_parquet(trades_parquet)
    if not equity.empty:
        equity.to_parquet(equity_parquet)
    if not signals.empty:
        signals.to_parquet(signals_parquet)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True, default=str))

    payload = {
        "dataset_id": args.dataset_id,
        "created_at": created.isoformat(),
        "history_manifest": _relpath(Path(args.history_manifest)),
        "training_samples": _relpath(Path(args.training_samples)),
        "feature_summary": _relpath(Path(args.feature_summary)),
        "symbols_loaded": len(data),
        "timeline_start": timeline[0].isoformat(),
        "timeline_end": timeline[-1].isoformat(),
        "bar_count": int(len(timeline)),
        "score_features": score_features,
        "assumptions": {
            "entry_timing": "signal at decision bar close, enter next available 5m open with slippage",
            "exit_priority": "conservative: stop/trailing stop before take-profit when both hit same bar",
            "mode": "long-only notional allocation; no funding or borrow costs in v1",
            "volume_gate": "historical volume is exchange feed volume and used only as a proxy",
        },
        "parameters": vars(args),
        "artifacts": {
            "trades_csv": _relpath(trades_csv),
            "equity_curve_csv": _relpath(equity_csv),
            "signals_csv": _relpath(signals_csv),
            "metrics_json": _relpath(metrics_path),
        },
        "metrics": metrics,
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _run_backtest(
    args: argparse.Namespace,
    data: dict[str, Any],
    close_panel: pd.DataFrame,
    market: dict[str, Any],
    training: pd.DataFrame,
    score_features: list[dict[str, Any]],
    timeline: pd.DatetimeIndex,
) -> dict[str, list[dict[str, Any]]]:
    cash = float(args.initial_capital)
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, pd.Timestamp] = {}
    pending_entries: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    fee_rate = args.fee_bps_per_side / 10000.0
    slip_rate = args.slippage_bps_per_side / 10000.0
    rebalance_bars = max(1, args.rebalance_minutes // 5)
    max_hold = pd.Timedelta(hours=args.max_hold_hours)
    cooldown = pd.Timedelta(hours=args.cooldown_hours)

    for i, ts in enumerate(timeline):
        cash = _open_pending_entries(ts, pending_entries, data, positions, cash, fee_rate, slip_rate, args)
        cash = _update_positions(ts, data, positions, trades, cash, fee_rate, slip_rate, max_hold, args)
        nav = _mark_nav(ts, close_panel, cash, positions)

        if i % rebalance_bars == 0 and nav > 0:
            candidates = _score_candidates(
                ts=ts,
                args=args,
                data=data,
                market=market,
                training=training,
                score_features=score_features,
                positions=positions,
                pending_entries=pending_entries,
                cooldown_until=cooldown_until,
            )
            signal_rows.extend(candidates)
            slots = max(0, args.max_positions - len(positions) - len(pending_entries))
            for row in candidates[:slots]:
                notional = min(cash * 0.95, nav * args.capital_per_trade)
                if notional <= 1.0:
                    break
                pending_entries.append({"decision_ts": ts, "notional": notional, "signal": row})
                cooldown_until[row["symbol"]] = ts + cooldown

        equity_rows.append(
            {
                "ts": ts.isoformat(),
                "cash": cash,
                "nav": nav,
                "open_positions": len(positions),
                "pending_entries": len(pending_entries),
            }
        )
        if args.progress_every and i > 0 and i % args.progress_every == 0:
            print(
                f"{ts.isoformat()} bars={i}/{len(timeline)} nav={nav:.2f} "
                f"cash={cash:.2f} open={len(positions)} trades={len(trades)}",
                flush=True,
            )

    final_ts = timeline[-1]
    for sym in list(positions):
        pos = positions.pop(sym)
        price = _bar_value(data[sym].frame, final_ts, "close")
        if price is None:
            price = pos.entry_price
        exit_price = price * (1.0 - slip_rate)
        cash = _close_position(pos, final_ts, exit_price, "end_of_backtest", cash, fee_rate, trades)
    if equity_rows:
        equity_rows[-1]["cash"] = cash
        equity_rows[-1]["nav"] = cash
        equity_rows[-1]["open_positions"] = 0
        equity_rows[-1]["pending_entries"] = 0

    return {"trades": trades, "equity": equity_rows, "signals": signal_rows}


def _score_candidates(
    ts: pd.Timestamp,
    args: argparse.Namespace,
    data: dict[str, Any],
    market: dict[str, Any],
    training: pd.DataFrame,
    score_features: list[dict[str, Any]],
    positions: dict[str, Position],
    pending_entries: list[dict[str, Any]],
    cooldown_until: dict[str, pd.Timestamp],
) -> list[dict[str, Any]]:
    blocked = set(positions) | {x["signal"]["symbol"] for x in pending_entries}
    rows: list[dict[str, Any]] = []
    for sym, item in data.items():
        if sym in blocked or cooldown_until.get(sym, pd.Timestamp("1970-01-01", tz="UTC")) > ts:
            continue
        row = _sample_row(sym, ts, item, market, require_forward=False)
        if not row:
            continue
        scored = _score_row(row, training, score_features)
        row.update(scored)
        if row.get("market_event_flag") != 0:
            continue
        if row.get("monster_score_adj", 0.0) < args.score_threshold:
            continue
        if row.get("ret_1h") is not None and row["ret_1h"] > args.max_ret_1h:
            continue
        if args.min_volume_24h_proxy > 0 and (row.get("volume_sum_24h") or 0.0) < args.min_volume_24h_proxy:
            continue
        rows.append(
            {
                "decision_ts": ts.isoformat(),
                "symbol": sym,
                "monster_score": float(row["monster_score"]),
                "monster_score_adj": float(row["monster_score_adj"]),
                "ret_1h": row.get("ret_1h"),
                "ret_6h": row.get("ret_6h"),
                "ret_24h": row.get("ret_24h"),
                "rvol_6h": row.get("rvol_6h"),
                "rvol_24h": row.get("rvol_24h"),
                "range_pct_6h": row.get("range_pct_6h"),
                "volume_sum_24h": row.get("volume_sum_24h"),
                "cs_rank_ret_6h": row.get("cs_rank_ret_6h"),
                "cs_rank_ret_24h": row.get("cs_rank_ret_24h"),
                "trigger_reasons": row.get("trigger_reasons", ""),
            }
        )
    return sorted(rows, key=lambda x: x["monster_score_adj"], reverse=True)


def _open_pending_entries(
    ts: pd.Timestamp,
    pending: list[dict[str, Any]],
    data: dict[str, Any],
    positions: dict[str, Position],
    cash: float,
    fee_rate: float,
    slip_rate: float,
    args: argparse.Namespace,
) -> float:
    keep: list[dict[str, Any]] = []
    for entry in pending:
        signal = entry["signal"]
        sym = signal["symbol"]
        if sym in positions:
            continue
        open_px = _bar_value(data[sym].frame, ts, "open")
        if open_px is None or ts <= pd.Timestamp(entry["decision_ts"]):
            keep.append(entry)
            continue
        notional = min(float(entry["notional"]), cash * 0.95)
        if notional <= 1.0:
            continue
        entry_price = open_px * (1.0 + slip_rate)
        fee = notional * fee_rate
        qty = notional / entry_price
        cash -= notional + fee
        positions[sym] = Position(
            symbol=sym,
            entry_ts=ts,
            entry_price=entry_price,
            qty=qty,
            notional=notional,
            entry_fee=fee,
            stop_price=entry_price * (1.0 - args.stop_loss),
            take_profit_price=entry_price * (1.0 + args.take_profit),
            peak_price=entry_price,
            score=float(signal["monster_score_adj"]),
            reasons=signal.get("trigger_reasons", ""),
        )
    pending[:] = keep
    return cash


def _update_positions(
    ts: pd.Timestamp,
    data: dict[str, Any],
    positions: dict[str, Position],
    trades: list[dict[str, Any]],
    cash: float,
    fee_rate: float,
    slip_rate: float,
    max_hold: pd.Timedelta,
    args: argparse.Namespace,
) -> float:
    for sym in list(positions):
        pos = positions[sym]
        if ts <= pos.entry_ts:
            continue
        df = data[sym].frame
        high = _bar_value(df, ts, "high")
        low = _bar_value(df, ts, "low")
        close = _bar_value(df, ts, "close")
        if high is None or low is None or close is None:
            continue
        pos.peak_price = max(pos.peak_price, high)
        trailing_price = pos.peak_price * (1.0 - args.trailing_stop)
        stop_price = max(pos.stop_price, trailing_price)
        exit_reason = None
        raw_exit_price = None
        if low <= stop_price:
            exit_reason = "stop_or_trailing"
            raw_exit_price = stop_price
        elif high >= pos.take_profit_price:
            exit_reason = "take_profit"
            raw_exit_price = pos.take_profit_price
        elif ts - pos.entry_ts >= max_hold:
            exit_reason = "time_exit"
            raw_exit_price = close
        if exit_reason is None:
            continue
        positions.pop(sym)
        cash = _close_position(pos, ts, raw_exit_price * (1.0 - slip_rate), exit_reason, cash, fee_rate, trades)
    return cash


def _close_position(
    pos: Position,
    ts: pd.Timestamp,
    exit_price: float,
    reason: str,
    cash: float,
    fee_rate: float,
    trades: list[dict[str, Any]],
) -> float:
    proceeds = pos.qty * exit_price
    exit_fee = proceeds * fee_rate
    cash += proceeds - exit_fee
    pnl = proceeds - exit_fee - pos.notional - pos.entry_fee
    ret = pnl / pos.notional if pos.notional else 0.0
    trades.append(
        {
            "symbol": pos.symbol,
            "entry_ts": pos.entry_ts.isoformat(),
            "exit_ts": ts.isoformat(),
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "qty": pos.qty,
            "notional": pos.notional,
            "entry_fee": pos.entry_fee,
            "exit_fee": exit_fee,
            "pnl": pnl,
            "return": ret,
            "hold_hours": float((ts - pos.entry_ts) / pd.Timedelta(hours=1)),
            "exit_reason": reason,
            "entry_score": pos.score,
            "trigger_reasons": pos.reasons,
        }
    )
    return cash


def _mark_nav(ts: pd.Timestamp, close_panel: pd.DataFrame, cash: float, positions: dict[str, Position]) -> float:
    nav = cash
    for sym, pos in positions.items():
        price = None
        if sym in close_panel.columns and ts in close_panel.index:
            value = close_panel.at[ts, sym]
            if not pd.isna(value):
                price = float(value)
        nav += pos.qty * (price if price is not None else pos.entry_price)
    return float(nav)


def _bar_value(df: pd.DataFrame, ts: pd.Timestamp, col: str) -> float | None:
    if ts not in df.index:
        return None
    value = df.at[ts, col]
    if pd.isna(value):
        return None
    return float(value)


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if equity.empty:
        return {}
    nav = pd.Series(pd.to_numeric(equity["nav"], errors="coerce").to_numpy(), index=pd.to_datetime(equity["ts"], utc=True))
    rets = nav.pct_change().dropna()
    running_peak = nav.cummax()
    dd = nav / running_peak - 1.0
    trade_pnls = pd.to_numeric(trades["pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    trade_returns = pd.to_numeric(trades["return"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    gross_profit = float(trade_pnls[trade_pnls > 0].sum()) if len(trade_pnls) else 0.0
    gross_loss = float(abs(trade_pnls[trade_pnls < 0].sum())) if len(trade_pnls) else 0.0
    periods = _periods_per_year(nav.index)
    ann_ret = _annualized_return(rets, periods)
    ann_vol = float(rets.std() * math.sqrt(periods)) if len(rets) else 0.0
    return {
        "initial_capital": args.initial_capital,
        "final_nav": float(nav.iloc[-1]),
        "total_return": float(nav.iloc[-1] / args.initial_capital - 1.0),
        "max_drawdown": float(dd.min()),
        "bar_count": int(len(nav)),
        "trade_count": int(len(trades)),
        "win_rate": float((trade_pnls > 0).mean()) if len(trade_pnls) else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_trade_return": float(trade_returns.mean()) if len(trade_returns) else 0.0,
        "median_trade_return": float(trade_returns.median()) if len(trade_returns) else 0.0,
        "best_trade_return": float(trade_returns.max()) if len(trade_returns) else 0.0,
        "worst_trade_return": float(trade_returns.min()) if len(trade_returns) else 0.0,
        "avg_hold_hours": float(pd.to_numeric(trades["hold_hours"], errors="coerce").mean()) if not trades.empty else 0.0,
        "annualized_return": ann_ret,
        "annualized_volatility": ann_vol,
        "sharpe": (ann_ret / ann_vol) if ann_vol else 0.0,
        "exposure_fraction": float((pd.to_numeric(equity["open_positions"], errors="coerce") > 0).mean()),
    }


def _annualized_return(rets: pd.Series, periods: int) -> float:
    if rets.empty:
        return 0.0
    total = float((1.0 + rets).prod())
    if total <= 0:
        return -1.0
    return total ** (periods / len(rets)) - 1.0


def _periods_per_year(index: pd.DatetimeIndex) -> int:
    if len(index) < 2:
        return 365
    delta = index.to_series().diff().dropna().median()
    if delta <= pd.Timedelta(0):
        return 365
    return max(1, int(round(pd.Timedelta(days=365) / delta)))


def _as_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


if __name__ == "__main__":
    raise SystemExit(main())
