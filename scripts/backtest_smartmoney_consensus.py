#!/usr/bin/env python3
"""Prototype backtest for smart-money weighted consensus.

This is a standalone candidate strategy study. It does not feed the investment
committee or paper trading until it has more history and walk-forward evidence.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PANEL = (
    ROOT
    / "engine"
    / "research"
    / "reports"
    / "smartmoney_diffusion"
    / "run_2026050905_20260509_091456"
    / "smartmoney_diffusion_panel.csv"
)
OUT_ROOT = ROOT / "engine" / "data" / "research" / "smartmoney_consensus"


@dataclass
class OpenPosition:
    ccy: str
    side: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_price: float
    notional: float
    signal_score: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest smartmoney weighted consensus")
    p.add_argument("--panel", default=str(DEFAULT_PANEL))
    p.add_argument("--out-id", default=f"smartmoney_weighted_consensus_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}")
    p.add_argument("--initial-capital", type=float, default=3000.0)
    p.add_argument("--fixed-notional", type=float, default=117.0)
    p.add_argument("--hold-hours", type=int, default=12, choices=[1, 3, 6, 12, 24])
    p.add_argument("--max-positions", type=int, default=4)
    p.add_argument("--min-traders", type=int, default=3)
    p.add_argument("--min-notional", type=float, default=50_000.0)
    p.add_argument("--weighted-threshold", type=float, default=0.80)
    p.add_argument("--fee-bps-per-side", type=float, default=5.0)
    p.add_argument("--slippage-bps-per-side", type=float, default=2.0)
    p.add_argument("--allow-long", action="store_true", default=True)
    p.add_argument("--allow-short", action="store_true", default=True)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    panel = pd.read_csv(args.panel)
    panel["ts"] = pd.to_datetime(panel["ts"], utc=True, errors="coerce")
    panel = panel.dropna(subset=["ts", "close"]).sort_values(["ts", "ccy"]).reset_index(drop=True)
    trades, equity = simulate(panel, args)
    out_dir = OUT_ROOT / args.out_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trades).to_csv(out_dir / "trades.csv", index=False)
    pd.DataFrame(equity).to_csv(out_dir / "equity_curve.csv", index=False)
    metrics = compute_metrics(pd.DataFrame(trades), pd.DataFrame(equity), args)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": "smartmoney_weighted_consensus_v1",
        "inputs": {
            "panel": str(Path(args.panel).relative_to(ROOT)),
            "initial_capital": args.initial_capital,
            "fixed_notional": args.fixed_notional,
            "hold_hours": args.hold_hours,
            "max_positions": args.max_positions,
            "min_traders": args.min_traders,
            "min_notional": args.min_notional,
            "weighted_threshold": args.weighted_threshold,
            "fee_bps_per_side": args.fee_bps_per_side,
            "slippage_bps_per_side": args.slippage_bps_per_side,
        },
        "metrics": metrics,
        "artifacts": {"trades": "trades.csv", "equity_curve": "equity_curve.csv", "summary": "summary.md"},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out_dir / "summary.md").write_text(render_summary(manifest, pd.DataFrame(trades)))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def simulate(panel: pd.DataFrame, args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    round_trip_cost = 2.0 * (float(args.fee_bps_per_side) + float(args.slippage_bps_per_side)) / 10000.0
    nav = float(args.initial_capital)
    open_positions: list[OpenPosition] = []
    trades: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    rows_by_ts = {ts: g.copy() for ts, g in panel.groupby("ts")}
    close_lookup = {(row.ccy, row.ts): float(row.close) for row in panel.itertuples() if math.isfinite(float(row.close))}

    for ts in sorted(rows_by_ts):
        still_open: list[OpenPosition] = []
        for pos in open_positions:
            if pos.exit_ts <= ts:
                exit_price = close_lookup.get((pos.ccy, pos.exit_ts), float("nan"))
                if not math.isfinite(exit_price) or exit_price <= 0:
                    still_open.append(pos)
                    continue
                raw_ret = exit_price / pos.entry_price - 1.0
                gross_ret = raw_ret if pos.side == "long" else -raw_ret
                net_ret = gross_ret - round_trip_cost
                pnl = pos.notional * net_ret
                nav += pnl
                trades.append(
                    {
                        "ccy": pos.ccy,
                        "side": pos.side,
                        "entry_ts": pos.entry_ts.isoformat(),
                        "exit_ts": pos.exit_ts.isoformat(),
                        "entry_price": pos.entry_price,
                        "exit_price": exit_price,
                        "notional": pos.notional,
                        "gross_return": gross_ret,
                        "net_return": net_ret,
                        "pnl": pnl,
                        "signal_score": pos.signal_score,
                    }
                )
            else:
                still_open.append(pos)
        open_positions = still_open

        slots = max(0, int(args.max_positions) - len(open_positions))
        if slots > 0:
            open_symbols = {pos.ccy for pos in open_positions}
            signals = build_signals(rows_by_ts[ts], args)
            signals = [sig for sig in signals if sig["ccy"] not in open_symbols]
            for sig in signals[:slots]:
                entry_price = float(sig["close"])
                exit_ts = ts + pd.Timedelta(hours=int(args.hold_hours))
                if (sig["ccy"], exit_ts) not in close_lookup or entry_price <= 0:
                    continue
                open_positions.append(
                    OpenPosition(
                        ccy=str(sig["ccy"]),
                        side=str(sig["side"]),
                        entry_ts=ts,
                        exit_ts=exit_ts,
                        entry_price=entry_price,
                        notional=float(args.fixed_notional),
                        signal_score=float(sig["score"]),
                    )
                )

        equity.append({"ts": ts.isoformat(), "nav": nav, "open_positions": len(open_positions)})

    return trades, equity


def build_signals(group: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    min_traders = int(args.min_traders)
    min_notional = float(args.min_notional)
    threshold = float(args.weighted_threshold)
    for row in group.itertuples(index=False):
        traders = num(getattr(row, "tradersWithPosition", 0))
        total_notional = num(getattr(row, "totalNotionalUsdt", 0))
        if traders < min_traders or total_notional < min_notional:
            continue
        weighted_long = num(getattr(row, "weightedLongRatio", float("nan")))
        weighted_short = num(getattr(row, "weightedShortRatio", float("nan")))
        net_notional = num(getattr(row, "netNotionalUsdt", 0))
        if args.allow_long and weighted_long >= threshold and net_notional > 0:
            signals.append(
                {
                    "ccy": row.ccy,
                    "side": "long",
                    "close": row.close,
                    "score": weighted_long * math.log1p(total_notional) * math.log1p(traders),
                }
            )
        if args.allow_short and weighted_short >= threshold and net_notional < 0:
            signals.append(
                {
                    "ccy": row.ccy,
                    "side": "short",
                    "close": row.close,
                    "score": weighted_short * math.log1p(total_notional) * math.log1p(traders),
                }
            )
    return sorted(signals, key=lambda item: item["score"], reverse=True)


def compute_metrics(trades: pd.DataFrame, equity: pd.DataFrame, args: argparse.Namespace) -> dict[str, Any]:
    if equity.empty:
        return {}
    nav = pd.to_numeric(equity["nav"], errors="coerce").ffill()
    peak = nav.cummax()
    drawdown = nav / peak - 1.0
    pnl = pd.to_numeric(trades.get("pnl", pd.Series(dtype=float)), errors="coerce") if not trades.empty else pd.Series(dtype=float)
    net = pd.to_numeric(trades.get("net_return", pd.Series(dtype=float)), errors="coerce") if not trades.empty else pd.Series(dtype=float)
    return {
        "final_nav": float(nav.iloc[-1]),
        "total_return_pct": float(nav.iloc[-1] / float(args.initial_capital) - 1.0),
        "max_drawdown_pct": float(drawdown.min()) if len(drawdown) else 0.0,
        "trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "avg_net_return": float(net.mean()) if len(net) else 0.0,
        "total_pnl": float(pnl.sum()) if len(pnl) else 0.0,
        "by_side": group_metrics(trades, "side"),
    }


def group_metrics(trades: pd.DataFrame, key: str) -> dict[str, Any]:
    if trades.empty or key not in trades:
        return {}
    out: dict[str, Any] = {}
    for value, g in trades.groupby(key):
        pnl = pd.to_numeric(g["pnl"], errors="coerce")
        net = pd.to_numeric(g["net_return"], errors="coerce")
        out[str(value)] = {
            "trades": int(len(g)),
            "win_rate": float((pnl > 0).mean()),
            "avg_net_return": float(net.mean()),
            "pnl": float(pnl.sum()),
        }
    return out


def render_summary(manifest: dict[str, Any], trades: pd.DataFrame) -> str:
    m = manifest["metrics"]
    lines = [
        "# Smartmoney Weighted Consensus Backtest",
        "",
        f"- strategy_id: `{manifest['strategy_id']}`",
        f"- created_at: `{manifest['created_at']}`",
        f"- panel: `{manifest['inputs']['panel']}`",
        "",
        "## Metrics",
        "",
        f"- final_nav: {m.get('final_nav', 0):.2f}",
        f"- total_return: {m.get('total_return_pct', 0) * 100:.2f}%",
        f"- max_drawdown: {m.get('max_drawdown_pct', 0) * 100:.2f}%",
        f"- trades: {m.get('trades', 0)}",
        f"- win_rate: {m.get('win_rate', 0) * 100:.2f}%",
        f"- avg_net_return: {m.get('avg_net_return', 0) * 100:.2f}%",
        "",
        "## By Side",
        "",
        "| side | trades | win_rate | avg_net_return | pnl |",
        "|---|---:|---:|---:|---:|",
    ]
    for side, row in dict(m.get("by_side") or {}).items():
        lines.append(
            f"| {side} | {row['trades']} | {row['win_rate'] * 100:.2f}% | "
            f"{row['avg_net_return'] * 100:.2f}% | {row['pnl']:.2f} |"
        )
    if not trades.empty:
        lines.extend(["", "## Top Symbols", "", "| ccy | trades | pnl |", "|---|---:|---:|"])
        by_symbol = trades.groupby("ccy")["pnl"].agg(["count", "sum"]).sort_values("sum", ascending=False).head(12)
        for ccy, row in by_symbol.iterrows():
            lines.append(f"| {ccy} | {int(row['count'])} | {float(row['sum']):.2f} |")
    return "\n".join(lines) + "\n"


def num(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return float("nan")
    return out if math.isfinite(out) else float("nan")


if __name__ == "__main__":
    raise SystemExit(main())
