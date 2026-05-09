#!/usr/bin/env python3
"""Generate a daily C-Auto review report from the local paper/live state."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PAPER_DIR = ROOT / "engine" / "logs" / "c_auto_v2_paper"
REVIEW_DIR = PAPER_DIR / "reviews"
RESEARCH_QUEUE_DIR = ROOT / "engine" / "research" / "queue"
RESEARCH_QUEUE_PATH = RESEARCH_QUEUE_DIR / "c_auto_research_tasks.jsonl"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate C-Auto daily review markdown")
    p.add_argument("--state-id", default="fixed1000_conservative")
    p.add_argument("--environment", default="competition")
    p.add_argument("--date", default=None, help="Local review date, YYYY-MM-DD. Defaults to today in Asia/Shanghai.")
    p.add_argument("--output-dir", default=str(REVIEW_DIR))
    return p.parse_args()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def iter_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
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


def parse_ts(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def in_local_day(item: dict, review_date: date) -> bool:
    dt = parse_ts(item.get("ts") or item.get("timestamp") or item.get("entry_ts") or item.get("exit_ts"))
    if dt is None:
        return False
    return dt.astimezone(LOCAL_TZ).date() == review_date


def num(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def money(value: object) -> str:
    return f"{num(value):.2f}U"


def pct(value: object) -> str:
    value_num = num(value, float("nan"))
    if not math.isfinite(value_num):
        return "--"
    return f"{value_num * 100:.2f}%"


def price(value: object) -> str:
    value_num = num(value, float("nan"))
    if not math.isfinite(value_num):
        return "--"
    if abs(value_num) >= 100:
        return f"{value_num:.2f}"
    if abs(value_num) >= 1:
        return f"{value_num:.4f}"
    return f"{value_num:.6g}"


def summarize_events(events: list[dict]) -> dict:
    counts = Counter(str(item.get("event") or "unknown") for item in events)
    realized_pnl = sum(num(item.get("pnl")) for item in events if item.get("pnl") is not None)
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: {"events": 0, "pnl": 0.0})
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"events": 0, "pnl": 0.0})
    for item in events:
        source = str(item.get("source_strategy_id") or item.get("signal_family") or item.get("entry_reason") or item.get("reason") or "unknown")
        symbol = str(item.get("symbol") or "--")
        by_source[source]["events"] += 1
        by_source[source]["pnl"] += num(item.get("pnl"))
        by_symbol[symbol]["events"] += 1
        by_symbol[symbol]["pnl"] += num(item.get("pnl"))
    return {"counts": counts, "realized_pnl": realized_pnl, "by_source": by_source, "by_symbol": by_symbol}


def event_context(ledger: list[dict]) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for item in ledger:
        if item.get("event") != "entry":
            continue
        decision_id = str(item.get("decision_id") or "")
        if decision_id:
            entries[decision_id] = item
        symbol = str(item.get("symbol") or "")
        if symbol:
            entries[f"symbol:{symbol}"] = item
    return entries


def enrich_event(item: dict, entries: dict[str, dict]) -> dict:
    decision_id = str(item.get("decision_id") or "")
    source = entries.get(decision_id) if decision_id else None
    if source is None and item.get("symbol"):
        source = entries.get(f"symbol:{item.get('symbol')}")
    out = dict(item)
    if source:
        for key in ("expected_ev", "p_target", "leverage_policy", "notional_usdt", "leverage"):
            out.setdefault(key, source.get(key))
        out.setdefault("entry_reason", source.get("reason"))
        out.setdefault("source_strategy_id", source.get("source_strategy_id"))
    return out


def build_quality_findings(day_events: list[dict], positions: dict) -> tuple[list[str], list[dict]]:
    findings: list[str] = []
    research_tasks: list[dict] = []
    exits = [item for item in day_events if item.get("event") == "exit"]
    losses = [item for item in exits if num(item.get("pnl")) < 0]
    stop_losses = [item for item in losses if item.get("reason") == "stop"]
    horizon_losses = [item for item in losses if item.get("reason") == "horizon"]
    if losses:
        worst = min(losses, key=lambda item: num(item.get("pnl")))
        findings.append(
            f"最大亏损来自 `{worst.get('symbol')}`，原因 `{worst.get('reason')}`，PnL {money(worst.get('pnl'))}。"
        )
        research_tasks.append(
            research_task(
                key=f"loss_attribution_{worst.get('symbol')}_{worst.get('reason')}",
                title=f"复盘 {worst.get('symbol')} {worst.get('reason')} 亏损",
                priority="p1",
                reason="daily_review_loss",
                payload={"symbol": worst.get("symbol"), "reason": worst.get("reason"), "pnl": num(worst.get("pnl"))},
            )
        )
    if stop_losses:
        findings.append(f"今日有 {len(stop_losses)} 笔 stop 亏损，需要确认 stop 是否按预期限制尾部亏损。")
    if horizon_losses:
        findings.append(f"今日有 {len(horizon_losses)} 笔 horizon 亏损，优先检查信号方向和持有期。")
        research_tasks.append(
            research_task(
                key="horizon_loss_exit_policy",
                title="测试 C-Auto horizon 亏损的提前退出规则",
                priority="p1",
                reason="daily_review_horizon_loss",
                payload={"horizon_losses": len(horizon_losses)},
            )
        )
    open_by_side = Counter(str(pos.get("side") or "--") for pos in positions.values())
    if open_by_side:
        side, count = open_by_side.most_common(1)[0]
        if count >= 3:
            findings.append(f"当前持仓方向集中：{count} 笔 `{side}`，需要观察同向拥挤风险。")
            research_tasks.append(
                research_task(
                    key=f"same_side_concentration_{side}",
                    title=f"评估 C-Auto {side} 同向持仓拥挤过滤",
                    priority="p2",
                    reason="daily_review_concentration",
                    payload={"side": side, "count": count},
                )
            )
    missing_stop = [symbol for symbol, pos in positions.items() if pos.get("stop_price") is None]
    if missing_stop:
        findings.append("存在未带 stop 的持仓：" + ", ".join(sorted(missing_stop)))
        research_tasks.append(
            research_task(
                key="missing_stop_position_manager",
                title="修复持仓管理中未带 stop 的持仓入口",
                priority="p0",
                reason="daily_review_missing_stop",
                payload={"symbols": sorted(missing_stop)},
            )
        )
    if not findings:
        findings.append("今日未发现新的亏损归因或持仓治理异常。")
    return findings, research_tasks


def research_task(key: str, title: str, priority: str, reason: str, payload: dict) -> dict:
    safe_key = safe_task_key(key)
    return {
        "task_id": f"c_auto_{safe_key}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_id": "c_auto_v2_fixed1000_conservative",
        "priority": priority,
        "status": "open",
        "title": title,
        "reason": reason,
        "payload": payload,
    }


def append_research_tasks(tasks: list[dict]) -> list[dict]:
    if not tasks:
        return []
    RESEARCH_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    existing_ids = set()
    if RESEARCH_QUEUE_PATH.exists():
        for item in iter_jsonl(RESEARCH_QUEUE_PATH):
            task_id = str(item.get("task_id") or "")
            if task_id:
                existing_ids.add(task_id)
                existing_ids.add(safe_task_id(task_id))
    written = []
    with RESEARCH_QUEUE_PATH.open("a") as fh:
        for task in tasks:
            if task["task_id"] in existing_ids:
                continue
            fh.write(json.dumps(task, sort_keys=True) + "\n")
            existing_ids.add(task["task_id"])
            written.append(task)
    return written


def safe_task_key(key: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in key)


def safe_task_id(task_id: str) -> str:
    if task_id.startswith("c_auto_"):
        return f"c_auto_{safe_task_key(task_id.removeprefix('c_auto_'))}"
    return safe_task_key(task_id)


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_无记录_"]
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    out.extend("| " + " | ".join(row) + " |" for row in rows)
    return out


def generate(args: argparse.Namespace) -> Path:
    review_date = date.fromisoformat(args.date) if args.date else datetime.now(LOCAL_TZ).date()
    prefix = f"{args.state_id}_{args.environment}"
    state_path = PAPER_DIR / f"{prefix}.json"
    scheduler_path = PAPER_DIR / f"{prefix}_scheduler.json"
    ledger_path = PAPER_DIR / f"{prefix}_ledger.jsonl"
    equity_path = PAPER_DIR / f"{prefix}_equity.jsonl"
    state = read_json(state_path)
    scheduler = read_json(scheduler_path)
    ledger = iter_jsonl(ledger_path)
    equity = iter_jsonl(equity_path)
    entries_by_key = event_context(ledger)
    day_events = [enrich_event(item, entries_by_key) for item in ledger if in_local_day(item, review_date)]
    summary = summarize_events(day_events)
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    candidates = state.get("latest_candidates") if isinstance(state.get("latest_candidates"), list) else []
    findings, research_tasks = build_quality_findings(day_events, positions)
    written_tasks = append_research_tasks(research_tasks)
    generated_at = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")

    lines: list[str] = [
        f"# C-Auto Daily Review - {review_date.isoformat()}",
        "",
        f"- 生成时间: {generated_at}",
        f"- 环境: `{args.environment}`",
        f"- 状态文件: `{state_path.relative_to(ROOT)}`",
        f"- 调度状态: `{scheduler.get('scheduler_status', scheduler.get('status', '--'))}` / cycles `{scheduler.get('cycles', '--')}`",
        "",
        "## 账户概览",
        "",
    ]
    initial_nav = num((state.get("metrics") or {}).get("initial_nav"), num(state.get("cash"), 3000.0))
    nav = num(state.get("nav") or state.get("realized_nav") or state.get("cash") or initial_nav)
    realized_pnl = num(state.get("realized_pnl"), nav - initial_nav)
    unrealized_pnl = num(state.get("unrealized_pnl"))
    lines += table(
        ["NAV", "已实现", "未实现", "开仓数", "开放风险", "今日事件PnL", "Equity点数"],
        [[money(nav), money(realized_pnl), money(unrealized_pnl), str(len(positions)), money(state.get("open_risk")), money(summary["realized_pnl"]), str(len(equity))]],
    )
    lines += ["", "## 当前持仓", ""]
    pos_rows = []
    for symbol, pos in sorted(positions.items()):
        source = str(pos.get("source_strategy_id") or pos.get("signal_family") or "--")
        pos_rows.append(
            [
                str(symbol),
                str(pos.get("side") or "--"),
                source,
                str(pos.get("regime") or "--"),
                price(pos.get("entry_price")),
                price(pos.get("mark_price")),
                money(pos.get("unrealized_pnl")),
                pct(pos.get("net_return") or pos.get("unrealized_pct")),
                money(pos.get("risk_budget")),
                price(pos.get("stop_price")),
                price(pos.get("tp1_price")),
                pct(pos.get("p_target")),
                pct(pos.get("expected_ev")),
            ]
        )
    lines += table(["Symbol", "Side", "Source", "Regime", "Entry", "Mark", "UPnL", "Ret", "Risk", "Stop", "TP1", "P(target)", "EV"], pos_rows)
    lines += ["", "## 今日事件", ""]
    event_rows = []
    for item in day_events[-40:]:
        ts = parse_ts(item.get("ts"))
        ts_text = ts.astimezone(LOCAL_TZ).strftime("%H:%M:%S") if ts else "--"
        event_rows.append(
            [
                ts_text,
                str(item.get("event") or "--"),
                str(item.get("symbol") or "--"),
                str(item.get("side") or "--"),
                str(item.get("source_strategy_id") or item.get("signal_family") or item.get("entry_reason") or item.get("reason") or "--"),
                money(item.get("pnl")) if item.get("pnl") is not None else "--",
                str(item.get("reason") or item.get("committee_reason") or "--")[:80],
            ]
        )
    lines += table(["Time", "Event", "Symbol", "Side", "Source", "PnL", "Reason"], event_rows)
    lines += ["", "## 分组复盘", ""]
    source_rows = [[source, str(int(data["events"])), money(data["pnl"])] for source, data in sorted(summary["by_source"].items())]
    lines += table(["Source", "Events", "PnL"], source_rows)
    lines += ["", "## 质量判断", ""]
    lines += [f"- {item}" for item in findings]
    lines += ["", "## Research Queue", ""]
    if written_tasks:
        lines += table(
            ["Task", "Priority", "Reason"],
            [[task["title"], task["priority"], task["reason"]] for task in written_tasks],
        )
    elif research_tasks:
        lines += ["_相关 research task 已存在，未重复写入。_"]
    else:
        lines += ["_无新增 research task。_"]
    lines += ["", "## 最新候选", ""]
    candidate_rows = []
    for item in candidates[:12]:
        candidate_rows.append(
            [
                str(item.get("symbol") or "--"),
                str(item.get("side") or "--"),
                str(item.get("regime") or "--"),
                f"{num(item.get('score'), float('nan')):.4f}" if math.isfinite(num(item.get("score"), float("nan"))) else "--",
                "yes" if item.get("eligible") else "no",
                "crowding" if item.get("blocked_by_crowding") else "--",
            ]
        )
    lines += table(["Symbol", "Side", "Regime", "Score", "Eligible", "Block"], candidate_rows)
    lines += ["", "## 待检查项", ""]
    lines += [
        f"- 事件计数: {dict(summary['counts'])}",
        "- 若今日没有开仓但候选充足，优先检查 freshness gate、数据刷新和 rebalance 时间点。",
        "- 若 EV 为负或目标概率缺失，检查信号委员会输入字段是否完整。",
    ]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"c_auto_daily_review_{args.environment}_{review_date.strftime('%Y%m%d')}.md"
    out_path.write_text("\n".join(lines) + "\n")
    return out_path


def main() -> int:
    out = generate(parse_args())
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
