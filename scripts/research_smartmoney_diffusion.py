#!/usr/bin/env python3
"""Event study for OKX smart-money diffusion signals.

Hypothesis:
small early smart-money participation may lead price; later broad adoption or
exit may change the forward return profile. This script treats smart money as a
standalone strategy research source, not as an investment-committee veto.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "engine" / "data" / "cache"
OUT_ROOT = ROOT / "engine" / "research" / "reports" / "smartmoney_diffusion"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Research smart-money diffusion vs forward returns")
    p.add_argument("--symbols", default="auto", help="Comma-separated ccys, or 'auto' for smartmoney overview ∩ local cache.")
    p.add_argument("--max-symbols", type=int, default=50)
    p.add_argument("--as-of", default="", help="UTC hour yyyymmddHH. Defaults to latest common local 1h cache hour.")
    p.add_argument("--limit", type=int, default=72)
    p.add_argument("--period", type=int, default=7, choices=[3, 7, 30, 90])
    p.add_argument("--lmt-num", type=int, default=100)
    p.add_argument("--output-dir", default=str(OUT_ROOT))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbols = resolve_symbols(args.symbols, args.max_symbols, args.period, args.lmt_num)
    as_of = args.as_of.strip() or infer_as_of(symbols)
    if not as_of:
        raise SystemExit("could not infer --as-of from local 1h cache")
    out_dir = Path(args.output_dir) / f"run_{as_of}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "symbols.json").write_text(json.dumps(symbols, indent=2, sort_keys=True))

    frames: list[pd.DataFrame] = []
    for ccy in symbols:
        trend = fetch_smartmoney_trend(ccy, as_of, args.limit, args.period, args.lmt_num)
        if trend.empty:
            continue
        joined = attach_price_returns(trend, ccy)
        joined.to_csv(out_dir / f"{ccy}_smartmoney_diffusion.csv", index=False)
        frames.append(joined)
    if not frames:
        raise SystemExit("no smartmoney trend data returned")
    panel = pd.concat(frames, ignore_index=True)
    panel.to_csv(out_dir / "smartmoney_diffusion_panel.csv", index=False)
    report = render_report(panel, as_of, symbols)
    report_path = out_dir / "smartmoney_diffusion_report.md"
    report_path.write_text(report)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "symbols_requested": symbols,
        "symbols_with_data": sorted(panel["ccy"].dropna().astype(str).unique().tolist()),
        "rows": int(len(panel)),
        "panel_path": str((out_dir / "smartmoney_diffusion_panel.csv").relative_to(ROOT)),
        "report_path": str(report_path.relative_to(ROOT)),
        "run_dir": str(out_dir.relative_to(ROOT)),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(report_path.relative_to(ROOT))
    return 0


def resolve_symbols(raw: str, max_symbols: int, period: int, lmt_num: int) -> list[str]:
    if raw.strip().lower() != "auto":
        return [normalize_ccy(item) for item in raw.split(",") if item.strip()]
    cache_symbols = set(local_cache_symbols())
    overview = fetch_smartmoney_overview_symbols(max_symbols=max_symbols * 2, period=period, lmt_num=lmt_num)
    symbols = [ccy for ccy in overview if ccy in cache_symbols]
    if len(symbols) < max(5, max_symbols // 4):
        symbols = local_cache_symbols()
    return symbols[:max_symbols]


def normalize_ccy(value: str) -> str:
    return value.strip().upper().replace("/USDT", "").replace("-USDT-SWAP", "")


def local_cache_symbols() -> list[str]:
    symbols = []
    for path in sorted(CACHE_DIR.glob("*_USDT_futures_1h.parquet")):
        symbols.append(path.name.replace("_USDT_futures_1h.parquet", ""))
    priority = {"BTC": 0, "ETH": 1, "SOL": 2, "NOT": 3, "FIL": 4, "AR": 5, "OP": 6}
    return sorted(symbols, key=lambda item: (priority.get(item, 100), item))


def fetch_smartmoney_overview_symbols(max_symbols: int, period: int, lmt_num: int) -> list[str]:
    cmd = [
        "okx",
        "smartmoney",
        "signal-overview-by-filter",
        "--topInstruments",
        str(max_symbols),
        "--period",
        str(period),
        "--lmtNum",
        str(lmt_num),
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        print(f"[warn] overview: {proc.stderr.strip() or proc.stdout.strip()}")
        return []
    payload = json.loads(proc.stdout)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [normalize_ccy(str(item.get("ccy") or "")) for item in rows if item.get("ccy")]


def fetch_smartmoney_trend(ccy: str, as_of: str, limit: int, period: int, lmt_num: int) -> pd.DataFrame:
    cmd = [
        "okx",
        "smartmoney",
        "signal-trend-by-filter",
        "--instCcy",
        ccy,
        "--asOfTime",
        as_of,
        "--granularity",
        "1h",
        "--limit",
        str(limit),
        "--period",
        str(period),
        "--lmtNum",
        str(lmt_num),
        "--json",
    ]
    proc = None
    for attempt in range(3):
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=60)
        if proc.returncode == 0:
            break
        time.sleep(1.5 * (attempt + 1))
    assert proc is not None
    if proc.returncode != 0:
        print(f"[warn] {ccy}: {proc.stderr.strip() or proc.stdout.strip()}")
        return pd.DataFrame()
    payload = json.loads(proc.stdout)
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ccy"] = ccy
    df["ts"] = pd.to_datetime(df["dataVersion"].astype(str), format="%Y%m%d%H", utc=True, errors="coerce")
    for col in (
        "longRatio",
        "shortRatio",
        "weightedLongRatio",
        "weightedShortRatio",
        "netNotionalUsdt",
        "totalNotionalUsdt",
        "tradersQualified",
        "tradersWithPosition",
        "longTraders",
        "shortTraders",
    ):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("ts").reset_index(drop=True)
    df["d_traders_with_position"] = df.groupby("ccy")["tradersWithPosition"].diff()
    df["d_total_notional_usdt"] = df.groupby("ccy")["totalNotionalUsdt"].diff()
    df["d_net_notional_usdt"] = df.groupby("ccy")["netNotionalUsdt"].diff()
    df["d_weighted_long_ratio"] = df.groupby("ccy")["weightedLongRatio"].diff()
    df["smartmoney_direction"] = df["netNotionalUsdt"].apply(lambda x: "long" if num(x) > 0 else ("short" if num(x) < 0 else "flat"))
    df["long_diffusion_event"] = (
        (df["weightedLongRatio"] >= 0.60)
        & (df["netNotionalUsdt"] > 0)
        & ((df["d_traders_with_position"] > 0) | (df["d_total_notional_usdt"] > 0))
    )
    df["long_exit_event"] = (
        (df["weightedLongRatio"] >= 0.60)
        & ((df["d_traders_with_position"] < 0) | (df["d_total_notional_usdt"] < 0))
    )
    df["short_diffusion_event"] = (
        (df["weightedShortRatio"] >= 0.60)
        & (df["netNotionalUsdt"] < 0)
        & ((df["d_traders_with_position"] > 0) | (df["d_total_notional_usdt"] > 0) | (df["d_net_notional_usdt"] < 0))
    )
    df["short_exit_event"] = (
        (df["weightedShortRatio"] >= 0.60)
        & ((df["d_traders_with_position"] < 0) | (df["d_total_notional_usdt"] < 0) | (df["d_net_notional_usdt"] > 0))
    )
    return df


def attach_price_returns(df: pd.DataFrame, ccy: str) -> pd.DataFrame:
    price = load_ohlcv_1h(ccy)
    out = df.copy()
    if price.empty:
        return out
    closes = price["close"].sort_index()
    out["close"] = [close_at(closes, ts) for ts in out["ts"]]
    for h in (1, 3, 6, 12, 24):
        out[f"fwd_ret_{h}h"] = [forward_return(closes, ts, h) for ts in out["ts"]]
    return out


def load_ohlcv_1h(ccy: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{ccy}_USDT_futures_1h.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.set_index("timestamp")
    else:
        df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
    return df.sort_index()


def close_at(closes: pd.Series, ts: pd.Timestamp) -> float:
    try:
        return float(closes.loc[ts])
    except Exception:
        return float("nan")


def forward_return(closes: pd.Series, ts: pd.Timestamp, hours: int) -> float:
    start = close_at(closes, ts)
    end = close_at(closes, ts + pd.Timedelta(hours=hours))
    if not math.isfinite(start) or not math.isfinite(end) or start <= 0:
        return float("nan")
    return end / start - 1.0


def infer_as_of(symbols: list[str]) -> str:
    latest: list[pd.Timestamp] = []
    for ccy in symbols:
        df = load_ohlcv_1h(ccy)
        if not df.empty:
            latest.append(pd.Timestamp(df.index.max()).floor("h"))
    if not latest:
        return ""
    return min(latest).strftime("%Y%m%d%H")


def render_report(panel: pd.DataFrame, as_of: str, symbols: list[str]) -> str:
    lines = [
        "# Smart-Money Diffusion Research v1",
        "",
        f"- generated_at: `{datetime.now(timezone.utc).isoformat()}`",
        f"- as_of: `{as_of}` UTC hour",
        f"- symbols: `{','.join(symbols)}`",
        "- source: `okx smartmoney signal-trend-by-filter` + local 1h OHLCV cache",
        "",
        "## Snapshot",
        "",
        "| ccy | rows | latest_dir | latest_traders | latest_weighted_long | latest_net_notional | latest_total_notional |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for ccy, g in panel.groupby("ccy"):
        latest = g.sort_values("ts").tail(1).iloc[0]
        lines.append(
            f"| {ccy} | {len(g)} | {latest.get('smartmoney_direction', '--')} | "
            f"{fmt_num(latest.get('tradersWithPosition'), 0)} | {fmt_pct(latest.get('weightedLongRatio'))} | "
            f"{fmt_money(latest.get('netNotionalUsdt'))} | {fmt_money(latest.get('totalNotionalUsdt'))} |"
        )
    lines.extend(["", "## Event Study", ""])
    lines.append("| event | samples | avg_fwd_1h | avg_fwd_3h | avg_fwd_6h | avg_fwd_12h | avg_fwd_24h |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for name, mask in (
        ("long_diffusion_event", panel["long_diffusion_event"].astype(bool)),
        ("long_exit_event", panel["long_exit_event"].astype(bool)),
        ("short_diffusion_event", panel["short_diffusion_event"].astype(bool)),
        ("short_exit_event", panel["short_exit_event"].astype(bool)),
        ("all_smartmoney_long", panel["netNotionalUsdt"] > 0),
        ("all_smartmoney_short", panel["netNotionalUsdt"] < 0),
    ):
        subset = panel[mask]
        lines.append(
            f"| {name} | {len(subset)} | {avg_pct(subset, 'fwd_ret_1h')} | {avg_pct(subset, 'fwd_ret_3h')} | "
            f"{avg_pct(subset, 'fwd_ret_6h')} | {avg_pct(subset, 'fwd_ret_12h')} | {avg_pct(subset, 'fwd_ret_24h')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- This is an event-study probe, not a tradable strategy yet.",
            "- Small-count coins must be treated carefully: 1-4 traders can move ratios sharply.",
            "- Next research step: collect a larger universe hourly and test entry/exit rules out-of-sample.",
        ]
    )
    return "\n".join(lines) + "\n"


def avg_pct(df: pd.DataFrame, col: str) -> str:
    if df.empty or col not in df:
        return "--"
    value = pd.to_numeric(df[col], errors="coerce").mean()
    return fmt_pct(value)


def num(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def fmt_pct(value: Any) -> str:
    value_num = num(value)
    return f"{value_num * 100:.2f}%" if math.isfinite(value_num) else "--"


def fmt_money(value: Any) -> str:
    value_num = num(value)
    return f"{value_num:,.0f}" if math.isfinite(value_num) else "--"


def fmt_num(value: Any, digits: int = 2) -> str:
    value_num = num(value)
    return f"{value_num:.{digits}f}" if math.isfinite(value_num) else "--"


if __name__ == "__main__":
    raise SystemExit(main())
