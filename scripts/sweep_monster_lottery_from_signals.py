#!/usr/bin/env python3
"""Fast lottery execution sweep from a prebuilt monster signal table."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_monster_lottery as bt  # noqa: E402
from build_monster_dataset import DEFAULT_HISTORY_MANIFEST, OUT_ROOT  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sweep lottery execution params from signal table")
    p.add_argument("--signals-id", default="monster_signal_table_2026q1q2_20260426")
    p.add_argument("--sweep-id", default=None)
    p.add_argument("--history-manifest", default=str(DEFAULT_HISTORY_MANIFEST))
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--risk-budgets", default="10,20")
    p.add_argument("--long-scores", default="0.88,0.90,0.92,0.95")
    p.add_argument("--stop-losses", default="0.08,0.10,0.15")
    p.add_argument("--tp-packs", default="0.30:0.80:0.25,0.50:1.50:0.30")
    p.add_argument("--progress", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sweep_id = args.sweep_id or f"monster_lottery_signal_sweep_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = OUT_ROOT / sweep_id
    out_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(args.signals_id)
    data = _load_price_data(args)
    timeline = _timeline(data, signals)
    configs = _configs(args)
    rows = []
    for i, cfg in enumerate(configs, start=1):
        run_args = _run_args(cfg)
        state = _run_from_signals(run_args, data, timeline, signals)
        trades = pd.DataFrame(state["trades"])
        equity = pd.DataFrame(state["equity"])
        metrics = bt._metrics(equity, trades, run_args)
        row = {"run_index": i, **cfg, **{f"metric_{k}": v for k, v in metrics.items()}}
        row["score_objective"] = _objective(metrics)
        rows.append(row)
        if args.progress:
            print(f"[{i}/{len(configs)}] nav={metrics.get('final_nav'):.2f} obj={row['score_objective']:.4f} cfg={cfg}", flush=True)
    results = pd.DataFrame(rows).sort_values("score_objective", ascending=False, na_position="last")
    results.to_csv(out_dir / "results.csv", index=False)
    results.to_parquet(out_dir / "results.parquet")
    payload = {
        "sweep_id": sweep_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "signals_id": args.signals_id,
        "runs": len(results),
        "artifacts": {"results": str((out_dir / "results.csv").relative_to(ROOT))},
        "top": results.head(20).to_dict(orient="records"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _run_from_signals(args: argparse.Namespace, data: dict[str, pd.DataFrame], timeline: pd.DatetimeIndex, signals: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    cash = args.initial_capital
    positions: dict[str, bt.Position] = {}
    pending: list[dict[str, Any]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []
    fee_rate = args.fee_bps_per_side / 10000.0
    slip_rate = args.slippage_bps_per_side / 10000.0
    cooldown = pd.Timedelta(hours=args.cooldown_hours)
    grouped = {ts: group for ts, group in signals.groupby("decision_ts_dt")}

    for ts in timeline:
        cash = bt._open_pending(ts, pending, _as_symbol_data(data), positions, cash, fee_rate, slip_rate, args)
        cash = bt._update_positions(ts, _as_symbol_data(data), positions, trades, cash, fee_rate, slip_rate, args)
        nav = _nav(ts, data, cash, positions)
        open_risk = sum(pos.risk_budget for pos in positions.values()) + sum(x["risk_budget"] for x in pending)
        if ts in grouped and cash > args.risk_budget and open_risk < args.max_open_risk:
            candidates = _select_candidates(grouped[ts], positions, pending, cooldown_until, ts, args)
            signal_rows.extend(candidates)
            slots = max(0, args.max_positions - len(positions) - len(pending))
            for row in candidates[:slots]:
                if open_risk + args.risk_budget > args.max_open_risk or cash <= args.risk_budget:
                    break
                pending.append({"decision_ts": ts, "signal": row, "risk_budget": args.risk_budget})
                cooldown_until[row["symbol"]] = ts + cooldown
                open_risk += args.risk_budget
        equity.append({"ts": ts.isoformat(), "cash": cash, "nav": nav, "open_positions": len(positions), "open_risk": open_risk})
    final_ts = timeline[-1]
    for sym in list(positions):
        pos = positions.pop(sym)
        price = _bar_value(data[sym], final_ts, "close") or pos.entry_price
        cash = bt._close(pos, final_ts, bt._slip_exit(price, pos.side, slip_rate), "end_of_backtest", cash, fee_rate, trades)
    if equity:
        equity[-1]["cash"] = cash
        equity[-1]["nav"] = cash
        equity[-1]["open_positions"] = 0
        equity[-1]["open_risk"] = 0.0
    return {"trades": trades, "equity": equity, "signals": signal_rows}


def _select_candidates(group: pd.DataFrame, positions: dict[str, bt.Position], pending: list[dict[str, Any]], cooldown_until: dict[str, pd.Timestamp], ts: pd.Timestamp, args: argparse.Namespace) -> list[dict[str, Any]]:
    blocked = set(positions) | {x["signal"]["symbol"] for x in pending}
    rows = []
    for row in group.sort_values("score", ascending=False).to_dict(orient="records"):
        sym = row["symbol"]
        if sym in blocked or cooldown_until.get(sym, pd.Timestamp("1970-01-01", tz="UTC")) > ts:
            continue
        score = float(row["score"])
        side = None
        if score >= args.long_score and (pd.isna(row.get("ret_1h")) or row.get("ret_1h") <= args.max_long_ret_1h):
            side = "long"
        if (
            score >= args.short_score
            and not pd.isna(row.get("ret_24h"))
            and row.get("ret_24h") >= args.short_pump_24h
            and not pd.isna(row.get("ret_1h"))
            and row.get("ret_1h") <= args.short_break_1h
        ):
            side = "short"
        if side:
            rows.append({"decision_ts": ts.isoformat(), "symbol": sym, "side": side, "score": score, "trigger_reasons": row.get("trigger_reasons", "")})
    return rows


def _load_signals(signals_id: str) -> pd.DataFrame:
    path = OUT_ROOT / signals_id / "signals.parquet"
    df = pd.read_parquet(path) if path.exists() else pd.read_csv(OUT_ROOT / signals_id / "signals.csv")
    df["decision_ts_dt"] = pd.to_datetime(df["decision_ts"], utc=True)
    return df


def _load_price_data(args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    manifest = json.loads(Path(args.history_manifest).read_text())
    out = {}
    for sym in manifest["symbols"]:
        safe = sym.replace("/", "_")
        files = sorted((ROOT / "engine" / "data" / "cache").glob(f"{safe}_futures_{args.timeframe}.parquet"))
        if not files:
            continue
        df = pd.read_parquet(files[0]).sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        out[sym] = df[["open", "high", "low", "close"]].dropna(subset=["close"])
    return out


def _timeline(data: dict[str, pd.DataFrame], signals: pd.DataFrame) -> pd.DatetimeIndex:
    start = signals["decision_ts_dt"].min()
    end = signals["decision_ts_dt"].max() + pd.Timedelta(hours=130)
    indexes = [df.loc[(df.index >= start) & (df.index <= end)].index for df in data.values()]
    if not indexes:
        return pd.DatetimeIndex([], tz="UTC")
    out = indexes[0]
    for idx in indexes[1:]:
        out = out.union(idx)
    return out.sort_values()


def _as_symbol_data(data: dict[str, pd.DataFrame]) -> dict[str, Any]:
    return {sym: type("Item", (), {"frame": df}) for sym, df in data.items()}


def _nav(ts: pd.Timestamp, data: dict[str, pd.DataFrame], cash: float, positions: dict[str, bt.Position]) -> float:
    nav = cash
    for sym, pos in positions.items():
        price = _bar_value(data[sym], ts, "close") or pos.entry_price
        nav += pos.initial_margin + bt._pnl(pos.side, pos.entry_price, price, pos.qty)
    return float(nav)


def _bar_value(df: pd.DataFrame, ts: pd.Timestamp, col: str) -> float | None:
    if ts not in df.index:
        return None
    value = df.at[ts, col]
    return None if pd.isna(value) else float(value)


def _configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    risks = [float(x) for x in args.risk_budgets.split(",") if x]
    scores = [float(x) for x in args.long_scores.split(",") if x]
    stops = [float(x) for x in args.stop_losses.split(",") if x]
    packs = []
    for raw in args.tp_packs.split(","):
        tp1, tp2, trail = [float(x) for x in raw.split(":")]
        packs.append({"tp1": tp1, "tp2": tp2, "runner_trailing": trail})
    return [{"risk_budget": r, "long_score": s, "stop_loss": st, **pack} for r, s, st, pack in itertools.product(risks, scores, stops, packs)]


def _run_args(cfg: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(
        initial_capital=1000.0,
        risk_budget=float(cfg["risk_budget"]),
        max_open_risk=60.0,
        max_positions=3,
        leverage=5.0,
        stop_loss=float(cfg["stop_loss"]),
        tp1=float(cfg["tp1"]),
        tp1_fraction=0.35,
        tp2=float(cfg["tp2"]),
        tp2_fraction=0.35,
        runner_trailing=float(cfg["runner_trailing"]),
        max_hold_hours=120.0,
        long_score=float(cfg["long_score"]),
        short_score=0.88,
        short_pump_24h=0.60,
        short_break_1h=-0.08,
        max_long_ret_1h=0.30,
        cooldown_hours=24.0,
        fee_bps_per_side=4.0,
        slippage_bps_per_side=4.0,
        progress_every=0,
    )


def _objective(metrics: dict[str, Any]) -> float:
    total_return = float(metrics.get("total_return") or 0.0)
    max_dd = abs(float(metrics.get("max_drawdown") or 0.0))
    best_r = float(metrics.get("best_return_on_risk_budget") or 0.0)
    worst_r = abs(float(metrics.get("worst_return_on_risk_budget") or 0.0))
    profit_factor = float(metrics.get("profit_factor") or 0.0)
    return total_return * 2.0 + best_r * 0.15 + profit_factor * 0.25 - max_dd * 1.5 - worst_r * 0.15


if __name__ == "__main__":
    raise SystemExit(main())
