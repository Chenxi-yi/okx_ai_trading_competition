#!/usr/bin/env python3
"""Lottery-style monster coin backtest with fixed per-trade risk budget.

V1 uses historical 5m OHLCV and the existing monster score. It intentionally
does not use live-only orderbook/OI features because those cannot be backfilled
from OKX public API.
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

from build_monster_dataset import DEFAULT_HISTORY_MANIFEST, OUT_ROOT, BARS, _load_symbol_data, _market_panels, _relpath, _sample_row  # noqa: E402
from score_monster_watchlist import _score_row, _select_score_features  # noqa: E402

DEFAULT_TRAINING = OUT_ROOT / "monster_samples_clustered_5m_v1" / "samples.parquet"
DEFAULT_FEATURE_SUMMARY = OUT_ROOT / "monster_samples_clustered_5m_v1" / "feature_summary.csv"


@dataclass
class Position:
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    entry_price: float
    qty: float
    notional: float
    initial_margin: float
    risk_budget: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    runner_stop_price: float
    peak_favorable_price: float
    score: float
    reasons: str
    tp1_done: bool = False
    tp2_done: bool = False
    realized_pnl: float = 0.0
    fees: float = 0.0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest fixed-risk monster lottery strategy")
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--training-samples", default=str(DEFAULT_TRAINING))
    p.add_argument("--feature-summary", default=str(DEFAULT_FEATURE_SUMMARY))
    p.add_argument("--dataset-id", default="monster_lottery_backtest_v1")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--rebalance-minutes", type=int, default=240)
    p.add_argument("--initial-capital", type=float, default=1000.0)
    p.add_argument("--risk-budget", type=float, default=20.0)
    p.add_argument("--max-open-risk", type=float, default=60.0)
    p.add_argument("--max-positions", type=int, default=3)
    p.add_argument("--leverage", type=float, default=5.0)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--tp1", type=float, default=0.30)
    p.add_argument("--tp1-fraction", type=float, default=0.35)
    p.add_argument("--tp2", type=float, default=0.80)
    p.add_argument("--tp2-fraction", type=float, default=0.35)
    p.add_argument("--runner-trailing", type=float, default=0.25)
    p.add_argument("--max-hold-hours", type=float, default=120.0)
    p.add_argument("--long-score", type=float, default=0.90)
    p.add_argument("--short-score", type=float, default=0.88)
    p.add_argument("--short-pump-24h", type=float, default=0.60)
    p.add_argument("--short-break-1h", type=float, default=-0.08)
    p.add_argument("--max-long-ret-1h", type=float, default=0.30)
    p.add_argument("--cooldown-hours", type=float, default=24.0)
    p.add_argument("--feature-count", type=int, default=25)
    p.add_argument("--fee-bps-per-side", type=float, default=4.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=4.0)
    p.add_argument("--progress-every", type=int, default=10000)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.rebalance_minutes % 5 != 0:
        raise SystemExit("--rebalance-minutes must be a multiple of 5")
    data_manifest = json.loads(Path(args.history_manifest).read_text())
    data = _load_symbol_data(data_manifest["symbols"], args.timeframe)
    close_panel = pd.concat({sym: item.frame["close"] for sym, item in data.items()}, axis=1).sort_index()
    market = _market_panels(close_panel)
    training = pd.read_parquet(args.training_samples)
    feature_summary = pd.read_csv(args.feature_summary)
    score_features = _select_score_features(feature_summary, training, args.feature_count)

    start_ts = max(_as_utc(args.start), close_panel.index.min() + pd.Timedelta(minutes=5 * BARS["7d"]))
    end_ts = _as_utc(args.end) if args.end else close_panel.index.max()
    timeline = close_panel.loc[(close_panel.index >= start_ts) & (close_panel.index <= end_ts)].index
    if len(timeline) < 2:
        raise SystemExit("Empty backtest timeline")

    state = _run(args, data, close_panel, market, training, score_features, timeline)
    out_dir = OUT_ROOT / args.dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    trades = pd.DataFrame(state["trades"])
    equity = pd.DataFrame(state["equity"])
    signals = pd.DataFrame(state["signals"])
    metrics = _metrics(equity, trades, args)

    trades.to_csv(out_dir / "trades.csv", index=False)
    equity.to_csv(out_dir / "equity_curve.csv", index=False)
    signals.to_csv(out_dir / "signals.csv", index=False)
    if not trades.empty:
        trades.to_parquet(out_dir / "trades.parquet")
    if not equity.empty:
        equity.to_parquet(out_dir / "equity_curve.parquet")
    if not signals.empty:
        signals.to_parquet(out_dir / "signals.parquet")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True, default=str))
    payload = {
        "dataset_id": args.dataset_id,
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "mode": "fixed-risk lottery v1",
        "history_manifest": _relpath(Path(args.history_manifest)),
        "training_samples": _relpath(Path(args.training_samples)),
        "feature_summary": _relpath(Path(args.feature_summary)),
        "symbols_loaded": len(data),
        "timeline_start": timeline[0].isoformat(),
        "timeline_end": timeline[-1].isoformat(),
        "parameters": vars(args),
        "score_features": score_features,
        "metrics": metrics,
        "artifacts": {
            "trades": _relpath(out_dir / "trades.csv"),
            "equity": _relpath(out_dir / "equity_curve.csv"),
            "signals": _relpath(out_dir / "signals.csv"),
            "metrics": _relpath(out_dir / "metrics.json"),
        },
        "caveats": [
            "V1 uses OHLCV monster score only; live orderbook/OI gates cannot be historically replayed yet.",
            "Perp liquidation is approximated by stop distance and fixed risk budget, not exact exchange margin engine.",
            "Short entries are heuristic blowoff-breakdown rules and need live OI/orderbook confirmation before production.",
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _run(
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
    pending: list[dict[str, Any]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    fee_rate = args.fee_bps_per_side / 10000.0
    slip_rate = args.slippage_bps_per_side / 10000.0
    rebalance_bars = max(1, args.rebalance_minutes // 5)
    cooldown = pd.Timedelta(hours=args.cooldown_hours)

    for i, ts in enumerate(timeline):
        cash = _open_pending(ts, pending, data, positions, cash, fee_rate, slip_rate, args)
        cash = _update_positions(ts, data, positions, trades, cash, fee_rate, slip_rate, args)
        nav = _nav(ts, close_panel, cash, positions)
        open_risk = sum(pos.risk_budget for pos in positions.values()) + sum(x["risk_budget"] for x in pending)

        if i % rebalance_bars == 0 and cash > args.risk_budget and open_risk < args.max_open_risk:
            candidates = _signals(ts, data, market, training, score_features, positions, pending, cooldown_until, args)
            signals.extend(candidates)
            slots = max(0, args.max_positions - len(positions) - len(pending))
            for row in candidates[:slots]:
                if open_risk + args.risk_budget > args.max_open_risk or cash <= args.risk_budget:
                    break
                pending.append({"decision_ts": ts, "signal": row, "risk_budget": args.risk_budget})
                cooldown_until[row["symbol"]] = ts + cooldown
                open_risk += args.risk_budget

        equity.append({"ts": ts.isoformat(), "cash": cash, "nav": nav, "open_positions": len(positions), "open_risk": open_risk})
        if args.progress_every and i > 0 and i % args.progress_every == 0:
            print(f"{ts.isoformat()} bars={i}/{len(timeline)} nav={nav:.2f} cash={cash:.2f} open={len(positions)} trades={len(trades)}", flush=True)

    final_ts = timeline[-1]
    for sym in list(positions):
        pos = positions.pop(sym)
        price = _bar_value(data[sym].frame, final_ts, "close") or pos.entry_price
        cash = _close(pos, final_ts, _slip_exit(price, pos.side, slip_rate), "end_of_backtest", cash, fee_rate, trades)
    if equity:
        equity[-1]["cash"] = cash
        equity[-1]["nav"] = cash
        equity[-1]["open_positions"] = 0
        equity[-1]["open_risk"] = 0.0
    return {"trades": trades, "equity": equity, "signals": signals}


def _signals(
    ts: pd.Timestamp,
    data: dict[str, Any],
    market: dict[str, Any],
    training: pd.DataFrame,
    score_features: list[dict[str, Any]],
    positions: dict[str, Position],
    pending: list[dict[str, Any]],
    cooldown_until: dict[str, pd.Timestamp],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    blocked = set(positions) | {x["signal"]["symbol"] for x in pending}
    rows = []
    for sym, item in data.items():
        if sym in blocked or cooldown_until.get(sym, pd.Timestamp("1970-01-01", tz="UTC")) > ts:
            continue
        row = _sample_row(sym, ts, item, market, require_forward=False)
        if not row or row.get("market_event_flag") != 0:
            continue
        row.update(_score_row(row, training, score_features))
        score = float(row.get("monster_score_adj") or 0.0)
        side = None
        if score >= args.long_score and (row.get("ret_1h") is None or row["ret_1h"] <= args.max_long_ret_1h):
            side = "long"
        if (
            score >= args.short_score
            and row.get("ret_24h") is not None
            and row["ret_24h"] >= args.short_pump_24h
            and row.get("ret_1h") is not None
            and row["ret_1h"] <= args.short_break_1h
        ):
            side = "short"
        if not side:
            continue
        rows.append(
            {
                "decision_ts": ts.isoformat(),
                "symbol": sym,
                "side": side,
                "score": score,
                "ret_1h": row.get("ret_1h"),
                "ret_6h": row.get("ret_6h"),
                "ret_24h": row.get("ret_24h"),
                "rvol_6h": row.get("rvol_6h"),
                "range_pct_6h": row.get("range_pct_6h"),
                "trigger_reasons": row.get("trigger_reasons", ""),
            }
        )
    return sorted(rows, key=lambda x: (x["side"] != "short", -x["score"]))


def _open_pending(
    ts: pd.Timestamp,
    pending: list[dict[str, Any]],
    data: dict[str, Any],
    positions: dict[str, Position],
    cash: float,
    fee_rate: float,
    slip_rate: float,
    args: argparse.Namespace,
) -> float:
    keep = []
    for item in pending:
        signal = item["signal"]
        sym = signal["symbol"]
        if sym in positions:
            continue
        if ts <= pd.Timestamp(item["decision_ts"]):
            keep.append(item)
            continue
        open_px = _bar_value(data[sym].frame, ts, "open")
        if open_px is None:
            keep.append(item)
            continue
        risk_budget = float(item["risk_budget"])
        if cash <= risk_budget:
            continue
        side = signal["side"]
        entry_price = _slip_entry(open_px, side, slip_rate)
        stop_dist = args.stop_loss
        notional_by_risk = risk_budget / stop_dist
        notional_by_margin = risk_budget * args.leverage
        notional = min(notional_by_risk, notional_by_margin)
        initial_margin = notional / args.leverage
        fee = notional * fee_rate
        if cash < initial_margin + fee:
            continue
        qty = notional / entry_price
        cash -= initial_margin + fee
        stop_price = entry_price * (1.0 - args.stop_loss) if side == "long" else entry_price * (1.0 + args.stop_loss)
        tp1_price = entry_price * (1.0 + args.tp1) if side == "long" else entry_price * (1.0 - args.tp1)
        tp2_price = entry_price * (1.0 + args.tp2) if side == "long" else entry_price * (1.0 - args.tp2)
        positions[sym] = Position(
            symbol=sym,
            side=side,
            entry_ts=ts,
            entry_price=entry_price,
            qty=qty,
            notional=notional,
            initial_margin=initial_margin,
            risk_budget=risk_budget,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            runner_stop_price=entry_price,
            peak_favorable_price=entry_price,
            score=float(signal["score"]),
            reasons=signal.get("trigger_reasons", ""),
            fees=fee,
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
        favorable = high if pos.side == "long" else low
        pos.peak_favorable_price = max(pos.peak_favorable_price, favorable) if pos.side == "long" else min(pos.peak_favorable_price, favorable)

        stop_hit = low <= pos.stop_price if pos.side == "long" else high >= pos.stop_price
        if stop_hit:
            positions.pop(sym)
            cash = _close(pos, ts, _slip_exit(pos.stop_price, pos.side, slip_rate), "hard_stop", cash, fee_rate, trades)
            continue

        tp1_hit = high >= pos.tp1_price if pos.side == "long" else low <= pos.tp1_price
        if tp1_hit and not pos.tp1_done:
            cash = _partial(pos, ts, pos.tp1_price, args.tp1_fraction, "tp1", cash, fee_rate, slip_rate, trades)
            pos.tp1_done = True
            pos.stop_price = pos.entry_price
        tp2_hit = high >= pos.tp2_price if pos.side == "long" else low <= pos.tp2_price
        if tp2_hit and not pos.tp2_done:
            cash = _partial(pos, ts, pos.tp2_price, args.tp2_fraction, "tp2", cash, fee_rate, slip_rate, trades)
            pos.tp2_done = True

        trail = pos.peak_favorable_price * (1.0 - args.runner_trailing) if pos.side == "long" else pos.peak_favorable_price * (1.0 + args.runner_trailing)
        pos.runner_stop_price = max(pos.stop_price, trail) if pos.side == "long" else min(pos.stop_price, trail)
        runner_hit = low <= pos.runner_stop_price if pos.side == "long" else high >= pos.runner_stop_price
        time_hit = ts - pos.entry_ts >= pd.Timedelta(hours=args.max_hold_hours)
        if runner_hit or time_hit:
            positions.pop(sym)
            raw = pos.runner_stop_price if runner_hit else close
            cash = _close(pos, ts, _slip_exit(raw, pos.side, slip_rate), "runner_stop" if runner_hit else "time_exit", cash, fee_rate, trades)
    return cash


def _partial(pos: Position, ts: pd.Timestamp, raw_price: float, fraction: float, reason: str, cash: float, fee_rate: float, slip_rate: float, trades: list[dict[str, Any]]) -> float:
    fraction = max(0.0, min(fraction, 1.0))
    qty = pos.qty * fraction
    if qty <= 0:
        return cash
    exit_price = _slip_exit(raw_price, pos.side, slip_rate)
    pnl = _pnl(pos.side, pos.entry_price, exit_price, qty)
    exit_fee = qty * exit_price * fee_rate
    margin_release = pos.initial_margin * fraction
    cash += margin_release + pnl - exit_fee
    pos.qty -= qty
    pos.notional *= 1.0 - fraction
    pos.initial_margin *= 1.0 - fraction
    pos.realized_pnl += pnl - exit_fee
    pos.fees += exit_fee
    trades.append(_trade_row(pos, ts, exit_price, qty, pnl - exit_fee, reason, partial=True))
    return cash


def _close(pos: Position, ts: pd.Timestamp, exit_price: float, reason: str, cash: float, fee_rate: float, trades: list[dict[str, Any]]) -> float:
    pnl = _pnl(pos.side, pos.entry_price, exit_price, pos.qty)
    exit_fee = pos.qty * exit_price * fee_rate
    net = pnl - exit_fee
    cash += pos.initial_margin + net
    pos.realized_pnl += net
    pos.fees += exit_fee
    trades.append(_trade_row(pos, ts, exit_price, pos.qty, net, reason, partial=False))
    return cash


def _trade_row(pos: Position, ts: pd.Timestamp, exit_price: float, qty: float, pnl: float, reason: str, partial: bool) -> dict[str, Any]:
    notional = qty * pos.entry_price
    return {
        "symbol": pos.symbol,
        "side": pos.side,
        "entry_ts": pos.entry_ts.isoformat(),
        "exit_ts": ts.isoformat(),
        "entry_price": pos.entry_price,
        "exit_price": exit_price,
        "qty": qty,
        "entry_notional": notional,
        "pnl": pnl,
        "return_on_notional": pnl / notional if notional else 0.0,
        "return_on_risk_budget": pnl / pos.risk_budget if pos.risk_budget else 0.0,
        "hold_hours": float((ts - pos.entry_ts) / pd.Timedelta(hours=1)),
        "exit_reason": reason,
        "partial": partial,
        "entry_score": pos.score,
        "trigger_reasons": pos.reasons,
    }


def _nav(ts: pd.Timestamp, close_panel: pd.DataFrame, cash: float, positions: dict[str, Position]) -> float:
    nav = cash
    for sym, pos in positions.items():
        price = close_panel.at[ts, sym] if sym in close_panel.columns and ts in close_panel.index and not pd.isna(close_panel.at[ts, sym]) else pos.entry_price
        nav += pos.initial_margin + _pnl(pos.side, pos.entry_price, float(price), pos.qty)
    return float(nav)


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if equity.empty:
        return {}
    nav = pd.Series(pd.to_numeric(equity["nav"], errors="coerce").to_numpy(), index=pd.to_datetime(equity["ts"], utc=True))
    dd = nav / nav.cummax() - 1.0
    pnl = pd.to_numeric(trades["pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    risk_ret = pd.to_numeric(trades["return_on_risk_budget"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    losers = pnl[pnl < 0]
    winners = pnl[pnl > 0]
    return {
        "initial_capital": args.initial_capital,
        "final_nav": float(nav.iloc[-1]),
        "total_return": float(nav.iloc[-1] / args.initial_capital - 1.0),
        "max_drawdown": float(dd.min()),
        "trade_events": int(len(trades)),
        "win_rate_events": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "gross_profit": float(winners.sum()) if len(winners) else 0.0,
        "gross_loss": float(abs(losers.sum())) if len(losers) else 0.0,
        "profit_factor": float(winners.sum() / abs(losers.sum())) if len(losers) and abs(losers.sum()) > 0 else None,
        "median_return_on_risk_budget": float(risk_ret.median()) if len(risk_ret) else 0.0,
        "best_return_on_risk_budget": float(risk_ret.max()) if len(risk_ret) else 0.0,
        "worst_return_on_risk_budget": float(risk_ret.min()) if len(risk_ret) else 0.0,
        "avg_loss_usdt": float(losers.mean()) if len(losers) else 0.0,
        "avg_win_usdt": float(winners.mean()) if len(winners) else 0.0,
        "max_consecutive_losing_events": _max_consecutive_losses(pnl),
    }


def _max_consecutive_losses(pnl: pd.Series) -> int:
    best = cur = 0
    for value in pnl:
        if value < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    return (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty


def _slip_entry(price: float, side: str, slip: float) -> float:
    return price * (1.0 + slip) if side == "long" else price * (1.0 - slip)


def _slip_exit(price: float, side: str, slip: float) -> float:
    return price * (1.0 - slip) if side == "long" else price * (1.0 + slip)


def _bar_value(df: pd.DataFrame, ts: pd.Timestamp, col: str) -> float | None:
    if ts not in df.index:
        return None
    value = df.at[ts, col]
    return None if pd.isna(value) else float(value)


def _as_utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


if __name__ == "__main__":
    raise SystemExit(main())
