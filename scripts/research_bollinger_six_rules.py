#!/usr/bin/env python3
"""Research Bollinger-band six-rule strategy variants.

The user idea is translated into a regime state machine:
- three lines up: buy middle-band pullback, take profit near upper band
- three lines flat: fade lower/upper band
- three lines down: optional lower-band long to middle-band only
- upward mouth opening: trend expansion long
- downward mouth opening: exit/no-trade by default, optional short test
- continued squeeze: no trade
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "engine" / "data"
CACHE_DIR = DATA_DIR / "cache"
OUT_ROOT = DATA_DIR / "research" / "bollinger_six_rules"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research Bollinger six-rule regimes")
    p.add_argument("--timeframe", choices=["1h", "4h", "1d"], default="4h")
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--recent-start", default="2026-04-01")
    p.add_argument("--max-symbols", type=int, default=100)
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--std-mult", type=float, default=2.0)
    p.add_argument("--slope-lookback", type=int, default=3)
    p.add_argument("--slope-min-pct", type=float, default=0.0015)
    p.add_argument("--flat-slope-pct", type=float, default=0.0008)
    p.add_argument("--band-touch-pct", type=float, default=0.003)
    p.add_argument("--open-bw-change-pct", type=float, default=0.08)
    p.add_argument("--squeeze-quantile", type=float, default=0.20)
    p.add_argument("--variant", choices=["all", "trend_long", "range_revert", "down_rebound", "expansion_long", "breakdown_short"], default="all")
    p.add_argument("--side-filter", choices=["both", "long", "short"], default="both")
    p.add_argument("--max-hold-bars", type=int, default=12)
    p.add_argument("--stop-mode", choices=["band", "atr"], default="band")
    p.add_argument("--atr-window", type=int, default=14)
    p.add_argument("--atr-stop", type=float, default=1.8)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--out-id", default=None)
    p.add_argument("--sweep", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = _select_symbols(args)
    if args.sweep:
        out_dir = _run_sweep(symbols, args)
        print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT))}, ensure_ascii=False, indent=2))
        return 0
    trades = _research_symbols(symbols, args)
    summary = _summarize(trades, args)
    out_dir = _write_outputs(trades, summary, args)
    print(json.dumps({"out_dir": str(out_dir.relative_to(ROOT)), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def _run_sweep(symbols: list[str], args: argparse.Namespace) -> Path:
    rows: list[dict[str, Any]] = []
    best: tuple[float, argparse.Namespace, pd.DataFrame, dict[str, Any]] | None = None
    for variant in ("trend_long", "range_revert", "down_rebound", "expansion_long", "breakdown_short", "all"):
        for slope_min in (0.001, 0.0015, 0.0025):
            for touch in (0.002, 0.004, 0.006):
                for hold in (6, 12, 18):
                    for stop_mode in ("band", "atr"):
                        cfg = argparse.Namespace(**vars(args))
                        cfg.variant = variant
                        cfg.slope_min_pct = slope_min
                        cfg.band_touch_pct = touch
                        cfg.max_hold_bars = hold
                        cfg.stop_mode = stop_mode
                        trades = _research_symbols(symbols, cfg)
                        summary = _summarize(trades, cfg)
                        recent = _period_summary(trades, str(cfg.recent_start))
                        score = _sweep_score(summary, recent)
                        rows.append(
                            {
                                "score": score,
                                "variant": variant,
                                "slope_min_pct": slope_min,
                                "band_touch_pct": touch,
                                "max_hold_bars": hold,
                                "stop_mode": stop_mode,
                                **{f"summary_{k}": v for k, v in summary.items() if isinstance(v, (int, float, str))},
                                **{f"recent_{k}": v for k, v in recent.items()},
                            }
                        )
                        if best is None or score > best[0]:
                            best = (score, cfg, trades, summary)
    out_id = args.out_id or f"bollinger_six_sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    results.to_csv(out_dir / "sweep_results.csv", index=False)
    results.head(30).to_csv(out_dir / "top30.csv", index=False)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "args": vars(args),
        "symbols": len(symbols),
        "configs_tested": len(rows),
        "top": results.head(12).to_dict(orient="records"),
    }
    if best is not None:
        _, cfg, trades, summary = best
        payload["best_config"] = {
            k: getattr(cfg, k)
            for k in ("timeframe", "variant", "window", "std_mult", "slope_min_pct", "band_touch_pct", "max_hold_bars", "stop_mode")
        }
        payload["best_summary"] = summary
        payload["best_recent"] = _period_summary(trades, str(cfg.recent_start))
        if not trades.empty:
            trades.to_csv(out_dir / "best_trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_sweep_markdown(payload))
    return out_dir


def _select_symbols(args: argparse.Namespace) -> list[str]:
    suffix = f"_futures_{args.timeframe}.parquet"
    available = sorted(p.name.replace(suffix, "") for p in CACHE_DIR.glob(f"*{suffix}"))
    return [s for s in available if s != "BTC_USDT"][: int(args.max_symbols)]


def _research_symbols(symbols: list[str], args: argparse.Namespace) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        df = _load_ohlcv(symbol, args)
        min_bars = max(int(args.window) + int(args.max_hold_bars) + 10, 80)
        if len(df) < min_bars:
            continue
        rows.extend(_research_symbol(symbol, df, args))
    return pd.DataFrame(rows)


def _load_ohlcv(symbol: str, args: argparse.Namespace) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_futures_{args.timeframe}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC") if args.end else pd.Timestamp.now(tz="UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def _research_symbol(symbol: str, df: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    bb = _bollinger_state(df, args)
    df = df.join(bb)
    atr = _atr_abs(df, int(args.atr_window))
    trades: list[dict[str, Any]] = []
    last_exit_idx = -10_000
    for i in range(int(args.window) + int(args.slope_lookback) + 3, len(df) - 2):
        if i <= last_exit_idx + 1:
            continue
        signal = _signal_at(df, i, args)
        if signal is None:
            continue
        if str(args.side_filter) != "both" and signal["side"] != str(args.side_filter):
            continue
        trade = _simulate_trade(symbol, df, atr, i, signal, args)
        if trade:
            trades.append(trade)
            last_exit_idx = int(trade["exit_idx"])
    return trades


def _bollinger_state(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    close = df["close"].astype(float)
    mid = close.rolling(int(args.window), min_periods=int(args.window)).mean()
    std = close.rolling(int(args.window), min_periods=int(args.window)).std()
    upper = mid + float(args.std_mult) * std
    lower = mid - float(args.std_mult) * std
    bw = (upper - lower) / mid
    lb = int(args.slope_lookback)
    out = pd.DataFrame(index=df.index)
    out["bb_mid"] = mid
    out["bb_upper"] = upper
    out["bb_lower"] = lower
    out["bb_bw"] = bw
    out["bb_mid_slope"] = (mid / mid.shift(lb) - 1.0) / lb
    out["bb_upper_slope"] = (upper / upper.shift(lb) - 1.0) / lb
    out["bb_lower_slope"] = (lower / lower.shift(lb) - 1.0) / lb
    out["bb_bw_change"] = bw / bw.shift(lb) - 1.0
    out["bb_bw_q20"] = bw.rolling(80, min_periods=30).quantile(float(args.squeeze_quantile))
    return out


def _signal_at(df: pd.DataFrame, i: int, args: argparse.Namespace) -> dict[str, Any] | None:
    row = df.iloc[i]
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    mid = float(row["bb_mid"] or math.nan)
    upper = float(row["bb_upper"] or math.nan)
    lower = float(row["bb_lower"] or math.nan)
    bw = float(row["bb_bw"] or math.nan)
    if not all(math.isfinite(x) and x > 0 for x in (close, mid, upper, lower, bw)):
        return None
    mid_s = float(row["bb_mid_slope"] or 0.0)
    up_s = float(row["bb_upper_slope"] or 0.0)
    low_s = float(row["bb_lower_slope"] or 0.0)
    bw_chg = float(row["bb_bw_change"] or 0.0)
    q20 = float(row["bb_bw_q20"] or math.nan)
    slope_min = float(args.slope_min_pct)
    flat = max(float(args.flat_slope_pct), slope_min * 0.6)
    tol = float(args.band_touch_pct)
    variant = str(args.variant)
    all_up = mid_s > slope_min and up_s > slope_min and low_s > slope_min
    all_down = mid_s < -slope_min and up_s < -slope_min and low_s < -slope_min
    flat_lines = abs(mid_s) < flat and abs(up_s) < flat * 1.5 and abs(low_s) < flat * 1.5
    opening_up = mid_s > 0 and up_s > slope_min and bw_chg > float(args.open_bw_change_pct)
    opening_down = mid_s < 0 and low_s < -slope_min and bw_chg > float(args.open_bw_change_pct)
    squeeze = math.isfinite(q20) and bw <= q20 and bw_chg < 0.02
    if squeeze:
        return None
    if variant in ("all", "trend_long") and all_up and low <= mid * (1.0 + tol) and close >= mid:
        return {"side": "long", "rule": "three_up_mid_pullback", "target": upper, "band_stop": lower}
    if variant in ("all", "range_revert") and flat_lines:
        if low <= lower * (1.0 + tol) and close > lower:
            return {"side": "long", "rule": "flat_lower_revert", "target": mid, "band_stop": lower * (1.0 - 0.01)}
        if high >= upper * (1.0 - tol) and close < upper:
            return {"side": "short", "rule": "flat_upper_revert", "target": mid, "band_stop": upper * (1.0 + 0.01)}
    if variant in ("all", "down_rebound") and all_down and low <= lower * (1.0 + tol) and close > lower:
        return {"side": "long", "rule": "three_down_lower_rebound", "target": mid, "band_stop": lower * (1.0 - 0.012)}
    if variant in ("all", "expansion_long") and opening_up and close > upper:
        return {"side": "long", "rule": "mouth_open_up_breakout", "target": close * 1.035, "band_stop": mid}
    if variant == "breakdown_short" and opening_down and close < lower:
        return {"side": "short", "rule": "mouth_open_down_breakdown", "target": close * 0.965, "band_stop": mid}
    return None


def _simulate_trade(
    symbol: str,
    df: pd.DataFrame,
    atr: pd.Series,
    signal_idx: int,
    signal: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    entry_idx = signal_idx + 1
    if entry_idx >= len(df):
        return None
    side = str(signal["side"])
    entry = float(df["open"].iloc[entry_idx])
    if entry <= 0:
        return None
    target = float(signal["target"])
    if str(args.stop_mode) == "atr":
        atr_abs = float(atr.iloc[signal_idx] or math.nan)
        if not math.isfinite(atr_abs) or atr_abs <= 0:
            return None
        stop = entry - float(args.atr_stop) * atr_abs if side == "long" else entry + float(args.atr_stop) * atr_abs
    else:
        stop = float(signal["band_stop"])
    if side == "long" and (stop <= 0 or target <= entry or stop >= entry):
        return None
    if side == "short" and (target <= 0 or target >= entry or stop <= entry):
        return None
    risk_pct = abs(entry / stop - 1.0)
    if risk_pct < 0.003 or risk_pct > 0.12:
        return None
    max_exit_idx = min(entry_idx + int(args.max_hold_bars), len(df) - 1)
    exit_idx = max_exit_idx
    exit_price = float(df["close"].iloc[exit_idx])
    reason = "horizon"
    for j in range(entry_idx, max_exit_idx + 1):
        high = float(df["high"].iloc[j])
        low = float(df["low"].iloc[j])
        if side == "long":
            if low <= stop:
                exit_idx = j
                exit_price = stop
                reason = "stop"
                break
            if high >= target:
                exit_idx = j
                exit_price = target
                reason = "target"
                break
        else:
            if high >= stop:
                exit_idx = j
                exit_price = stop
                reason = "stop"
                break
            if low <= target:
                exit_idx = j
                exit_price = target
                reason = "target"
                break
    gross = exit_price / entry - 1.0 if side == "long" else entry / exit_price - 1.0
    cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    net = gross - cost
    return {
        "symbol": symbol.replace("_USDT", "/USDT"),
        "side": side,
        "rule": str(signal["rule"]),
        "signal_ts": df.index[signal_idx].isoformat(),
        "entry_ts": df.index[entry_idx].isoformat(),
        "exit_ts": df.index[exit_idx].isoformat(),
        "exit_idx": exit_idx,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "target": target,
        "exit_reason": reason,
        "gross_return": gross,
        "net_return": net,
        "risk_pct": risk_pct,
        "r_multiple": net / risk_pct if risk_pct else math.nan,
    }


def _atr_abs(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=max(5, window // 2)).mean()


def _summarize(trades: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "verdict": "NO_SIGNAL"}
    ret = pd.to_numeric(trades["net_return"], errors="coerce").dropna()
    wins = ret > 0
    by_rule = {}
    for rule, group in trades.groupby("rule"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_rule[str(rule)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
            "sum_net_return": float(r.sum()) if len(r) else 0.0,
        }
    by_side = {}
    for side, group in trades.groupby("side"):
        r = pd.to_numeric(group["net_return"], errors="coerce").dropna()
        by_side[str(side)] = {
            "trades": int(len(r)),
            "win_rate": float((r > 0).mean()) if len(r) else math.nan,
            "mean_net_return": float(r.mean()) if len(r) else math.nan,
            "sum_net_return": float(r.sum()) if len(r) else 0.0,
        }
    mean = float(ret.mean())
    std = float(ret.std()) if len(ret) > 1 else math.nan
    pos_month = _monthly_positive_rate(trades)
    recent = _period_summary(trades, str(args.recent_start))
    sharpe_like = float(mean / std * math.sqrt(365 * _bars_per_day(args.timeframe) / max(1, int(args.max_hold_bars)))) if std and std > 0 else math.nan
    verdict = "ROBUST" if mean > 0 and float(wins.mean()) >= 0.52 and pos_month >= 0.58 else "MARGINAL" if mean > 0 and float(recent.get("mean_net_return") or 0.0) > 0 else "RANDOM"
    return {
        "trades": int(len(ret)),
        "symbols": int(trades["symbol"].nunique()),
        "win_rate": float(wins.mean()),
        "mean_net_return": mean,
        "median_net_return": float(ret.median()),
        "sum_net_return": float(ret.sum()),
        "sharpe_like": sharpe_like,
        "positive_month_rate": pos_month,
        "target_rate": float((trades["exit_reason"] == "target").mean()),
        "stop_rate": float((trades["exit_reason"] == "stop").mean()),
        "horizon_rate": float((trades["exit_reason"] == "horizon").mean()),
        "avg_risk_pct": float(pd.to_numeric(trades["risk_pct"], errors="coerce").mean()),
        "by_rule": by_rule,
        "by_side": by_side,
        "recent": recent,
        "verdict": verdict,
    }


def _period_summary(trades: pd.DataFrame, start: str) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    sample = trades[pd.to_datetime(trades["entry_ts"], utc=True) >= pd.Timestamp(start, tz="UTC")]
    if sample.empty:
        return {"trades": 0, "win_rate": math.nan, "mean_net_return": math.nan, "sum_net_return": 0.0}
    ret = pd.to_numeric(sample["net_return"], errors="coerce").dropna()
    return {
        "trades": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else math.nan,
        "mean_net_return": float(ret.mean()) if len(ret) else math.nan,
        "sum_net_return": float(ret.sum()) if len(ret) else 0.0,
    }


def _monthly_positive_rate(trades: pd.DataFrame) -> float:
    if trades.empty:
        return math.nan
    df = trades.copy()
    df["month"] = pd.to_datetime(df["entry_ts"], utc=True).dt.strftime("%Y-%m")
    monthly = df.groupby("month")["net_return"].sum()
    return float((monthly > 0).mean()) if len(monthly) else math.nan


def _sweep_score(summary: dict[str, Any], recent: dict[str, Any]) -> float:
    trades = int(summary.get("trades") or 0)
    if trades < 40:
        return -1e9
    mean = float(summary.get("mean_net_return") or 0.0)
    win = float(summary.get("win_rate") or 0.0)
    pos_month = float(summary.get("positive_month_rate") or 0.0)
    recent_trades = int(recent.get("trades") or 0)
    recent_mean = float(recent.get("mean_net_return") or 0.0) if recent_trades >= 8 else -0.004
    recent_win = float(recent.get("win_rate") or 0.0) if recent_trades >= 8 else 0.0
    stop_rate = float(summary.get("stop_rate") or 0.0)
    return mean * 120.0 + recent_mean * 180.0 + (win - 0.5) * 2.0 + (recent_win - 0.5) * 2.5 + (pos_month - 0.5) * 1.5 - stop_rate * 0.35


def _write_outputs(trades: pd.DataFrame, summary: dict[str, Any], args: argparse.Namespace) -> Path:
    out_id = args.out_id or f"bollinger_six_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir = OUT_ROOT / out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    if not trades.empty:
        trades.to_csv(out_dir / "trades.csv", index=False)
        trades.tail(500).to_csv(out_dir / "trades_tail.csv", index=False)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "args": vars(args), "summary": summary}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (out_dir / "report.md").write_text(_single_markdown(payload))
    return out_dir


def _single_markdown(payload: dict[str, Any]) -> str:
    args = payload["args"]
    summary = payload["summary"]
    return "\n".join(
        [
            "# Bollinger Six Rules Research",
            "",
            f"- generated_at: `{payload['generated_at']}`",
            f"- timeframe: `{args['timeframe']}`",
            f"- variant: `{args['variant']}`",
            f"- window: `{args['window']}`",
            f"- std_mult: `{args['std_mult']}`",
            "",
            "## Summary",
            "",
            f"- trades: `{summary.get('trades')}`",
            f"- win_rate: `{_fmt_pct(summary.get('win_rate'))}`",
            f"- mean_net_return: `{_fmt_pct(summary.get('mean_net_return'))}`",
            f"- recent: `{summary.get('recent')}`",
            f"- verdict: `{summary.get('verdict')}`",
        ]
    )


def _sweep_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Bollinger Six Rules Sweep",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        f"- symbols: `{payload['symbols']}`",
        f"- configs_tested: `{payload['configs_tested']}`",
        "",
        "## Best",
        "",
        f"- config: `{payload.get('best_config')}`",
        f"- summary: `{payload.get('best_summary')}`",
        f"- recent: `{payload.get('best_recent')}`",
        "",
        "## Top 12",
        "",
        "| rank | score | variant | slope | touch | hold | stop | trades | win | mean net | recent mean |",
        "|---:|---:|---|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for idx, row in enumerate(payload.get("top", []), 1):
        lines.append(
            f"| {idx} | {float(row.get('score') or 0):.3f} | {row.get('variant')} | {row.get('slope_min_pct')} | {row.get('band_touch_pct')} | {row.get('max_hold_bars')} | {row.get('stop_mode')} | {int(row.get('summary_trades') or 0)} | {_fmt_pct(row.get('summary_win_rate'))} | {_fmt_pct(row.get('summary_mean_net_return'))} | {_fmt_pct(row.get('recent_mean_net_return'))} |"
        )
    return "\n".join(lines)


def _bars_per_day(timeframe: str) -> int:
    return {"1h": 24, "4h": 6, "1d": 1}.get(str(timeframe), 6)


def _fmt_pct(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "nan"
    if not math.isfinite(x):
        return "nan"
    return f"{x * 100:.2f}%"


if __name__ == "__main__":
    raise SystemExit(main())
