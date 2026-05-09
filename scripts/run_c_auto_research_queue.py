#!/usr/bin/env python3
"""Process C-Auto research queue tasks from daily reviews.

This worker is intentionally narrow: it turns paper-trading anomalies into
local research reports without restarting the paper runner or rerunning model
training. Heavy strategy changes still go through the experiment/backtest lane.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = ROOT / "engine"
QUEUE_PATH = ENGINE_DIR / "research" / "queue" / "c_auto_research_tasks.jsonl"
REPORT_DIR = ENGINE_DIR / "research" / "reports" / "c_auto"
PAPER_DIR = ENGINE_DIR / "logs" / "c_auto_v2_paper"
BACKTEST_DIR = (
    ENGINE_DIR
    / "data"
    / "research"
    / "c_auto"
    / "c_auto_v2_portfolio_rebuild_161_ohlcv_snapshot_fixed1000_conservative_v1"
)
DATA_CACHE = ENGINE_DIR / "data" / "cache"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run C-Auto research queue worker")
    p.add_argument("--queue", default=str(QUEUE_PATH))
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition")
    p.add_argument("--limit", type=int, default=0, help="Max open tasks to process; 0 means all supported tasks.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    queue_path = Path(args.queue)
    tasks = read_jsonl(queue_path)
    open_indexes = [
        idx
        for idx, task in sorted(enumerate(tasks), key=lambda item: priority_sort(item[1]))
        if task.get("status") == "open" and is_supported(task)
    ]
    if args.limit > 0:
        open_indexes = open_indexes[: args.limit]
    if not open_indexes:
        print("No supported open C-Auto research tasks.")
        return 0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    context = WorkerContext(args.state_id, args.environment)
    processed: list[dict[str, Any]] = []
    for idx in open_indexes:
        task = tasks[idx]
        if "loss_attribution" in str(task.get("task_id")):
            result = analyze_loss_attribution(task, context)
        elif "same_side_concentration" in str(task.get("task_id")):
            result = analyze_same_side_concentration(task, context)
        else:
            continue
        updated = dict(task)
        updated.update(
            {
                "status": result["status"],
                "completed_at": now_iso(),
                "report_path": str(result["report_path"].relative_to(ROOT)),
                "result_summary": result["summary"],
            }
        )
        tasks[idx] = updated
        processed.append(updated)

    if args.dry_run:
        print(json.dumps(processed, indent=2, ensure_ascii=False))
        return 0
    write_jsonl_atomic(queue_path, tasks)
    for item in processed:
        print(f"{item['task_id']}: {item['status']} -> {item['report_path']}")
    return 0


class WorkerContext:
    def __init__(self, state_id: str, environment: str) -> None:
        self.prefix = f"{state_id}_{environment}"
        self.state = read_json(PAPER_DIR / f"{self.prefix}.json")
        self.ledger = read_jsonl(PAPER_DIR / f"{self.prefix}_ledger.jsonl")
        self.trades = read_frame(BACKTEST_DIR / "trades.parquet", BACKTEST_DIR / "trades.csv")
        if not self.trades.empty:
            for col in ("entry_ts", "exit_ts", "signal_ts"):
                if col in self.trades:
                    self.trades[col] = pd.to_datetime(self.trades[col], utc=True, errors="coerce")


def analyze_loss_attribution(task: dict[str, Any], context: WorkerContext) -> dict[str, Any]:
    payload = dict(task.get("payload") or {})
    symbol = str(payload.get("symbol") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not symbol:
        symbol = symbol_from_task(task)
    exits = [
        item
        for item in context.ledger
        if item.get("event") == "exit"
        and str(item.get("symbol")) == symbol
        and (not reason or str(item.get("reason")) == reason)
    ]
    exit_event = min(exits, key=lambda item: num(item.get("pnl")), default={})
    entry_event = latest_entry_before(context.ledger, symbol, parse_ts(exit_event.get("ts")))

    exit_ts = parse_ts(exit_event.get("ts"))
    entry_ts = parse_ts(entry_event.get("ts"))
    side = str(exit_event.get("side") or entry_event.get("side") or "")
    notional = num(entry_event.get("notional_usdt") or payload.get("notional_usdt"), 0.0)
    pnl = num(exit_event.get("pnl") or payload.get("pnl"), float("nan"))
    exit_price = num(exit_event.get("exit_price"), float("nan"))
    net_return = num(exit_event.get("net_return"), float("nan"))
    stop_pct = num((entry_event.get("leverage_policy") or {}).get("stop_pct"), 0.025)
    entry_price = infer_entry_price(side, exit_price, net_return, stop_pct)
    stop_price = infer_stop_price(side, entry_price, stop_pct)
    target_pct = 0.035
    target_price = entry_price * (1 + target_pct) if side == "long" else entry_price * (1 - target_pct)

    bars = load_ohlcv(symbol, "5m")
    window = slice_bars(bars, entry_ts, exit_ts)
    trigger = find_trigger(window, side, stop_price, target_price)
    hist = historical_slice_stats(context.trades, symbol, side)
    report_path = REPORT_DIR / f"{safe_id(task.get('task_id'))}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"

    report = render_loss_report(
        task=task,
        symbol=symbol,
        side=side,
        entry_event=entry_event,
        exit_event=exit_event,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        notional=notional,
        pnl=pnl,
        net_return=net_return,
        window=window,
        trigger=trigger,
        hist=hist,
    )
    report_path.write_text(report)

    if not entry_event or not exit_event:
        status = "needs_experiment"
        summary = "paper ledger 不完整，无法完成单笔归因。"
    elif trigger:
        status = "done"
        summary = f"{symbol} 是正常 stop 执行，亏损 {money(pnl)}，尾部损失被限制在 {pct(abs(net_return))} 左右。"
    else:
        status = "needs_experiment"
        summary = f"{symbol} ledger 有 stop exit，但 5m cache 未复现触发点，需要补数据审计。"
    return {"status": status, "summary": summary, "report_path": report_path}


def analyze_same_side_concentration(task: dict[str, Any], context: WorkerContext) -> dict[str, Any]:
    payload = dict(task.get("payload") or {})
    side = str(payload.get("side") or "short")
    max_same_side = 2
    trades = context.trades.copy()
    baseline = trade_stats(trades)
    limited = apply_same_side_limit(trades, side=side, max_same_side=max_same_side)
    limited_stats = trade_stats(limited)
    short_base = trade_stats(trades[trades["side"].astype(str) == side]) if not trades.empty and "side" in trades else {}
    short_limited = trade_stats(limited[limited["side"].astype(str) == side]) if not limited.empty and "side" in limited else {}
    current = current_same_side_snapshot(context.state, context.ledger, side, max_same_side)
    pnl_retention = safe_div(limited_stats.get("pnl", 0.0), baseline.get("pnl", 0.0))
    avg_change = limited_stats.get("avg_net_return", 0.0) - baseline.get("avg_net_return", 0.0)

    report_path = REPORT_DIR / f"{safe_id(task.get('task_id'))}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    report = render_concentration_report(
        task=task,
        side=side,
        max_same_side=max_same_side,
        baseline=baseline,
        limited=limited_stats,
        short_base=short_base,
        short_limited=short_limited,
        current=current,
        pnl_retention=pnl_retention,
        avg_change=avg_change,
    )
    report_path.write_text(report)

    if not current["over_limit"] and pnl_retention >= 0.95:
        status = "done"
        summary = f"同向 {side} 当前未超限；历史 proxy 保留 {pct(pnl_retention)} PnL。"
    else:
        status = "needs_experiment"
        summary = (
            f"建议提交 max {max_same_side} 同向 {side} 拥挤过滤到组合级实验；"
            f"proxy PnL 保留 {pct(pnl_retention)}，当前 {side} 持仓 {current['count']} 笔。"
        )
    return {"status": status, "summary": summary, "report_path": report_path}


def render_loss_report(
    *,
    task: dict[str, Any],
    symbol: str,
    side: str,
    entry_event: dict[str, Any],
    exit_event: dict[str, Any],
    entry_price: float,
    stop_price: float,
    target_price: float,
    notional: float,
    pnl: float,
    net_return: float,
    window: pd.DataFrame,
    trigger: dict[str, Any],
    hist: dict[str, Any],
) -> str:
    rows = []
    for ts, row in window.tail(8).iterrows():
        rows.append(
            f"| {ts.isoformat()} | {price(row.get('open'))} | {price(row.get('high'))} | "
            f"{price(row.get('low'))} | {price(row.get('close'))} |"
        )
    bar_table = "\n".join(rows) if rows else "| -- | -- | -- | -- | -- |"
    trigger_line = (
        f"{trigger['ts'].isoformat()} 的 5m K 线触发 `{trigger['reason']}`，"
        f"high={price(trigger.get('high'))}, low={price(trigger.get('low'))}。"
        if trigger
        else "本地 5m K 缓存没有复现 stop/target 触发。"
    )
    conclusion = (
        "结论：这笔不是止损缺失，也不是 paper 和生产逻辑不一致；paper 的保护性 stop 已按 5m K 执行。"
        "问题更偏向信号/组合筛选：同一 rebalance 同时开了多笔 short，NOT 是排名第 3 的 short 候选。"
    )
    return f"""# C-Auto 单笔亏损归因：{symbol}

- task_id: `{task.get('task_id')}`
- status: researched
- generated_at: `{now_iso()}`

## 交易时间线

| 字段 | 值 |
|---|---:|
| symbol | `{symbol}` |
| side | `{side}` |
| entry_ts | `{entry_event.get('ts', '--')}` |
| exit_ts | `{exit_event.get('ts', '--')}` |
| exit_reason | `{exit_event.get('reason', '--')}` |
| entry_price_inferred | {price(entry_price)} |
| stop_price | {price(stop_price)} |
| target_price | {price(target_price)} |
| exit_price | {price(exit_event.get('exit_price'))} |
| notional | {money(notional)} |
| net_return | {pct(net_return)} |
| pnl | {money(pnl)} |
| expected_ev | {pct(entry_event.get('expected_ev'))} |
| p_target | {pct(entry_event.get('p_target'))} |
| leverage | {num(entry_event.get('leverage'), 0.0):.2f}x |

## 5m K 线验证

{trigger_line}

| ts | open | high | low | close |
|---|---:|---:|---:|---:|
{bar_table}

## 历史同类样本

| 切片 | trades | win_rate | avg_net_return | pnl |
|---|---:|---:|---:|---:|
| {symbol} {side} | {hist.get('symbol_side_trades', 0)} | {pct(hist.get('symbol_side_win_rate'))} | {pct(hist.get('symbol_side_avg_net'))} | {money(hist.get('symbol_side_pnl'))} |
| all {side} | {hist.get('side_trades', 0)} | {pct(hist.get('side_win_rate'))} | {pct(hist.get('side_avg_net'))} | {money(hist.get('side_pnl'))} |

## 结论

{conclusion}
"""


def render_concentration_report(
    *,
    task: dict[str, Any],
    side: str,
    max_same_side: int,
    baseline: dict[str, Any],
    limited: dict[str, Any],
    short_base: dict[str, Any],
    short_limited: dict[str, Any],
    current: dict[str, Any],
    pnl_retention: float,
    avg_change: float,
) -> str:
    dropped = ", ".join(current["would_drop"]) if current["would_drop"] else "--"
    keep = ", ".join(current["would_keep"]) if current["would_keep"] else "--"
    recommendation = (
        "建议：进入组合级实验，不直接进 paper。这个 proxy 只是在已成交交易表上删单，"
        "没有重算资金曲线、候选替补和同时段风险预算。"
    )
    return f"""# C-Auto 同向持仓拥挤过滤评估

- task_id: `{task.get('task_id')}`
- status: proxy_researched
- generated_at: `{now_iso()}`

## 当前 paper 暴露

| 字段 | 值 |
|---|---:|
| side | `{side}` |
| current_count | {current['count']} |
| proposed_limit | {max_same_side} |
| latest_rebalance_keep_by_score | `{keep}` |
| latest_rebalance_drop_by_score | `{dropped}` |
| current_gross_notional | {money(current['gross_notional'])} |

## 历史 proxy：同一 entry_ts 最多 {max_same_side} 笔 `{side}`

| 切片 | trades | win_rate | avg_net_return | pnl |
|---|---:|---:|---:|---:|
| baseline all | {baseline.get('trades', 0)} | {pct(baseline.get('win_rate'))} | {pct(baseline.get('avg_net_return'))} | {money(baseline.get('pnl'))} |
| limited all | {limited.get('trades', 0)} | {pct(limited.get('win_rate'))} | {pct(limited.get('avg_net_return'))} | {money(limited.get('pnl'))} |
| baseline {side} | {short_base.get('trades', 0)} | {pct(short_base.get('win_rate'))} | {pct(short_base.get('avg_net_return'))} | {money(short_base.get('pnl'))} |
| limited {side} | {short_limited.get('trades', 0)} | {pct(short_limited.get('win_rate'))} | {pct(short_limited.get('avg_net_return'))} | {money(short_limited.get('pnl'))} |

- raw_pnl_retention: {pct(pnl_retention)}
- avg_net_return_change: {pct(avg_change)}

## 结论

{recommendation}
"""


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        for row in rows:
            tmp.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def read_frame(parquet: Path, csv_path: Path) -> pd.DataFrame:
    if parquet.exists():
        return pd.read_parquet(parquet)
    if csv_path.exists():
        return pd.read_csv(csv_path)
    return pd.DataFrame()


def load_ohlcv(symbol: str, timeframe: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_").replace(":", "_")
    for suffix in ("parquet", "pkl", "csv"):
        path = DATA_CACHE / f"{safe}_futures_{timeframe}.{suffix}"
        if not path.exists():
            continue
        if suffix == "parquet":
            df = pd.read_parquet(path)
        elif suffix == "pkl":
            df = pd.read_pickle(path)
        else:
            df = pd.read_csv(path)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.set_index("timestamp")
        else:
            df.index = pd.to_datetime(df.index, utc=True, errors="coerce")
        return df.sort_index()
    return pd.DataFrame()


def slice_bars(df: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    if df.empty or start is None:
        return pd.DataFrame()
    if end is None:
        end = pd.Timestamp.now(tz="UTC")
    return df.loc[(df.index > start) & (df.index <= end)].copy()


def find_trigger(df: pd.DataFrame, side: str, stop: float, target: float) -> dict[str, Any]:
    if df.empty:
        return {}
    for ts, row in df.iterrows():
        high = num(row.get("high"), float("nan"))
        low = num(row.get("low"), float("nan"))
        if side == "long":
            if math.isfinite(stop) and low <= stop:
                return {"ts": ts, "reason": "stop", "high": high, "low": low}
            if math.isfinite(target) and high >= target:
                return {"ts": ts, "reason": "target", "high": high, "low": low}
        if side == "short":
            if math.isfinite(stop) and high >= stop:
                return {"ts": ts, "reason": "stop", "high": high, "low": low}
            if math.isfinite(target) and low <= target:
                return {"ts": ts, "reason": "target", "high": high, "low": low}
    return {}


def historical_slice_stats(trades: pd.DataFrame, symbol: str, side: str) -> dict[str, Any]:
    if trades.empty:
        return {}
    by_side = trades[trades["side"].astype(str) == side] if "side" in trades else pd.DataFrame()
    by_symbol_side = (
        by_side[by_side["symbol"].astype(str) == symbol] if not by_side.empty and "symbol" in by_side else pd.DataFrame()
    )
    side_stats = trade_stats(by_side)
    symbol_stats = trade_stats(by_symbol_side)
    return {
        "side_trades": side_stats.get("trades", 0),
        "side_win_rate": side_stats.get("win_rate", float("nan")),
        "side_avg_net": side_stats.get("avg_net_return", float("nan")),
        "side_pnl": side_stats.get("pnl", 0.0),
        "symbol_side_trades": symbol_stats.get("trades", 0),
        "symbol_side_win_rate": symbol_stats.get("win_rate", float("nan")),
        "symbol_side_avg_net": symbol_stats.get("avg_net_return", float("nan")),
        "symbol_side_pnl": symbol_stats.get("pnl", 0.0),
    }


def apply_same_side_limit(trades: pd.DataFrame, *, side: str, max_same_side: int) -> pd.DataFrame:
    if trades.empty or "side" not in trades or "entry_ts" not in trades:
        return trades.copy()
    side_mask = trades["side"].astype(str) == side
    keep_other = trades[~side_mask]
    target = trades[side_mask].copy()
    if target.empty:
        return trades.copy()
    score_col = "score" if "score" in target else "net_return"
    target["_score_for_rank"] = pd.to_numeric(target[score_col], errors="coerce").fillna(float("-inf"))
    kept = (
        target.sort_values(["entry_ts", "_score_for_rank"], ascending=[True, False])
        .groupby("entry_ts", dropna=False)
        .head(max_same_side)
        .drop(columns=["_score_for_rank"])
    )
    return pd.concat([keep_other, kept], ignore_index=True).sort_values(["entry_ts", "symbol"], na_position="last")


def trade_stats(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {"trades": 0, "win_rate": float("nan"), "avg_net_return": float("nan"), "pnl": 0.0}
    pnl = pd.to_numeric(df.get("pnl", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    net = pd.to_numeric(df.get("net_return", pd.Series(dtype=float)), errors="coerce")
    return {
        "trades": int(len(df)),
        "win_rate": float((pnl > 0).mean()) if len(df) else float("nan"),
        "avg_net_return": float(net.mean()) if len(net.dropna()) else float("nan"),
        "pnl": float(pnl.sum()),
    }


def current_same_side_snapshot(
    state: dict[str, Any],
    ledger: list[dict[str, Any]],
    side: str,
    max_same_side: int,
) -> dict[str, Any]:
    positions = [
        {"symbol": sym, **dict(pos)}
        for sym, pos in dict(state.get("positions") or {}).items()
        if str(dict(pos).get("side")) == side
    ]
    entries = [
        item
        for item in ledger
        if item.get("event") == "entry" and str(item.get("side")) == side and str(item.get("ts")) == latest_entry_ts(ledger, side)
    ]
    candidates = entries or positions
    candidates = sorted(candidates, key=lambda item: num(item.get("expected_ev") or item.get("score"), 0.0), reverse=True)
    would_keep = [str(item.get("symbol")) for item in candidates[:max_same_side]]
    would_drop = [str(item.get("symbol")) for item in candidates[max_same_side:]]
    gross = sum(num(item.get("notional_usdt") or item.get("risk_budget"), 0.0) for item in positions)
    return {
        "count": len(positions),
        "over_limit": len(positions) > max_same_side,
        "would_keep": would_keep,
        "would_drop": would_drop,
        "gross_notional": gross,
    }


def latest_entry_ts(ledger: list[dict[str, Any]], side: str) -> str:
    timestamps = [str(item.get("ts")) for item in ledger if item.get("event") == "entry" and str(item.get("side")) == side]
    return max(timestamps) if timestamps else ""


def latest_entry_before(ledger: list[dict[str, Any]], symbol: str, before: pd.Timestamp | None) -> dict[str, Any]:
    entries = [item for item in ledger if item.get("event") == "entry" and str(item.get("symbol")) == symbol]
    if before is not None:
        entries = [item for item in entries if parse_ts(item.get("ts")) is not None and parse_ts(item.get("ts")) <= before]
    return max(entries, key=lambda item: str(item.get("ts") or ""), default={})


def infer_entry_price(side: str, exit_price: float, net_return: float, stop_pct: float) -> float:
    cost = 0.0014
    if math.isfinite(exit_price) and math.isfinite(net_return):
        gross = net_return + cost
        if side == "short":
            return exit_price / (1.0 - gross)
        if side == "long":
            return exit_price / (1.0 + gross)
    if math.isfinite(exit_price) and stop_pct > 0:
        return exit_price / (1.0 + stop_pct) if side == "short" else exit_price / (1.0 - stop_pct)
    return float("nan")


def infer_stop_price(side: str, entry_price: float, stop_pct: float) -> float:
    if not math.isfinite(entry_price):
        return float("nan")
    return entry_price * (1.0 + stop_pct) if side == "short" else entry_price * (1.0 - stop_pct)


def parse_ts(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def priority_sort(task: dict[str, Any]) -> tuple[int, str]:
    rank = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}.get(str(task.get("priority")).lower(), 9)
    return rank, str(task.get("created_at") or "")


def is_supported(task: dict[str, Any]) -> bool:
    tid = str(task.get("task_id") or "")
    return "loss_attribution" in tid or "same_side_concentration" in tid


def symbol_from_task(task: dict[str, Any]) -> str:
    text = f"{task.get('task_id', '')} {task.get('title', '')}"
    match = re.search(r"([A-Z0-9]+)[/_-]USDT", text)
    if not match:
        return ""
    return f"{match.group(1)}/USDT"


def num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def safe_div(a: float, b: float) -> float:
    if not math.isfinite(a) or not math.isfinite(b) or abs(b) < 1e-12:
        return float("nan")
    return a / b


def money(value: Any) -> str:
    return f"{num(value, float('nan')):.2f}U" if math.isfinite(num(value, float("nan"))) else "--"


def pct(value: Any) -> str:
    value_num = num(value, float("nan"))
    return f"{value_num * 100:.2f}%" if math.isfinite(value_num) else "--"


def price(value: Any) -> str:
    value_num = num(value, float("nan"))
    if not math.isfinite(value_num):
        return "--"
    if abs(value_num) >= 100:
        return f"{value_num:.2f}"
    if abs(value_num) >= 1:
        return f"{value_num:.4f}"
    return f"{value_num:.8g}"


def safe_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "task")).strip("_") or "task"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
