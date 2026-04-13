"""
logging_/structured_logger.py
=============================
Structured JSON logger for the trading engine.

Writes two types of output:
  1. Per-portfolio JSONL event logs (append-only): logs/{portfolio_id}.jsonl
  2. Summary snapshot (overwritten each cycle): logs/summary.json

Claude Code reads ONLY from these files for status — no Python execution needed.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR

logger = logging.getLogger(__name__)

LOGS_DIR = BASE_DIR / "logs"


class StructuredLogger:
    """JSON-based structured logger for trading events and status snapshots."""

    def __init__(self, logs_dir: Path = LOGS_DIR):
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _append_event(self, portfolio_id: str, event: Dict[str, Any]) -> None:
        """Append a JSON event line to the per-portfolio log."""
        log_file = self.logs_dir / f"{portfolio_id}.jsonl"
        event["ts"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write event log for %s: %s", portfolio_id, e)

    # ------------------------------------------------------------------
    # Event loggers
    # ------------------------------------------------------------------

    def log_rebalance(
        self,
        portfolio_id: str,
        snapshot: Dict[str, Any],
        trades: List[Dict[str, Any]],
    ) -> None:
        """Log a rebalance event with full portfolio snapshot."""
        event = {
            "event": "rebalance",
            "portfolio": portfolio_id,
            **snapshot,
            "trades": trades,
        }
        self._append_event(portfolio_id, event)
        logger.info(
            "[%s] Rebalance logged | NAV=$%.2f | PnL=$%.2f (%.2f%%) | %d trades",
            portfolio_id, snapshot.get("nav", 0), snapshot.get("pnl", 0),
            snapshot.get("pnl_pct", 0), len(trades),
        )

    def log_risk_check(
        self,
        portfolio_id: str,
        snapshot: Dict[str, Any],
        action: str = "none",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log a risk monitoring event."""
        event = {
            "event": "risk_check",
            "portfolio": portfolio_id,
            "action": action,
            "nav": snapshot.get("nav", 0),
            "drawdown_pct": snapshot.get("drawdown_pct", 0),
            "risk": snapshot.get("risk", {}),
        }
        if details:
            event["details"] = details
        self._append_event(portfolio_id, event)

    def log_trade(self, portfolio_id: str, trade: Dict[str, Any]) -> None:
        """Log an individual trade execution."""
        event = {
            "event": "trade",
            "portfolio": portfolio_id,
            **trade,
        }
        self._append_event(portfolio_id, event)

    def log_engine_event(
        self,
        event_type: str,
        message: str,
        portfolios: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log an engine-level event (start, stop, error)."""
        event = {
            "event": event_type,
            "message": message,
        }
        if portfolios:
            event["portfolios"] = list(portfolios.keys())

        log_file = self.logs_dir / "engine.jsonl"
        event["ts"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write engine event: %s", e)

    # ------------------------------------------------------------------
    # Signal decomposition log (why each position was taken)
    # ------------------------------------------------------------------

    def log_signals(
        self,
        portfolio_id: str,
        signal_meta: Dict[str, Any],
        risk_summary: Dict[str, Any],
    ) -> None:
        """
        Log per-sleeve signal decomposition for a rebalance.

        Captures: per-sleeve weights, threshold filter decisions,
        concentration filter decisions, final position reasoning.
        Written to logs/signals/{portfolio_id}.jsonl — one entry per rebalance.
        """
        signals_dir = self.logs_dir / "signals"
        signals_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "portfolio": portfolio_id,
            "risk": risk_summary,
            **signal_meta,
        }

        log_file = signals_dir / f"{portfolio_id}.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write signal log for %s: %s", portfolio_id, e)

    # ------------------------------------------------------------------
    # Performance CSV (one row per rebalance, easy to load in pandas)
    # ------------------------------------------------------------------

    def log_performance_csv(
        self,
        portfolio_id: str,
        snapshot: Dict[str, Any],
        n_trades: int,
    ) -> None:
        """
        Append one row to logs/performance.csv per rebalance.
        Columns designed for weekly analysis: load with pd.read_csv().
        """
        csv_file = self.logs_dir / "performance.csv"
        write_header = not csv_file.exists()

        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "portfolio_id": portfolio_id,
            "strategy_id": snapshot.get("strategy_id", ""),
            "profile": snapshot.get("profile", ""),
            "nav": snapshot.get("nav", 0),
            "capital": snapshot.get("capital", 0),
            "pnl": snapshot.get("pnl", 0),
            "pnl_pct": snapshot.get("pnl_pct", 0),
            "realized_pnl": snapshot.get("realized_pnl", 0),
            "unrealized_pnl": snapshot.get("upnl", 0),
            "total_fees": snapshot.get("total_fees", 0),
            "peak_nav": snapshot.get("peak_nav", 0),
            "drawdown_pct": snapshot.get("drawdown_pct", 0),
            "gross_exp": snapshot.get("gross_exp", 0),
            "net_exp": snapshot.get("net_exp", 0),
            "n_positions": snapshot.get("n_positions", 0),
            "n_trades": n_trades,
            "risk_cb": snapshot.get("risk", {}).get("cb", ""),
            "risk_vol": snapshot.get("risk", {}).get("vol", ""),
        }

        try:
            with open(csv_file, "a") as f:
                if write_header:
                    f.write(",".join(row.keys()) + "\n")
                f.write(",".join(str(v) for v in row.values()) + "\n")
        except Exception as e:
            logger.error("Failed to write performance CSV: %s", e)

    # ------------------------------------------------------------------
    # Trade CSV (every trade with reason)
    # ------------------------------------------------------------------

    def log_trade_csv(
        self,
        portfolio_id: str,
        trade: Dict[str, Any],
        signal_reason: str = "",
    ) -> None:
        """
        Append one row to logs/trades.csv per trade execution.
        Includes the signal reason (which sleeve drove the trade).
        """
        csv_file = self.logs_dir / "trades.csv"
        write_header = not csv_file.exists()

        row = {
            "ts": trade.get("ts", datetime.now(timezone.utc).isoformat()),
            "portfolio_id": portfolio_id,
            "symbol": trade.get("symbol", ""),
            "side": trade.get("side", ""),
            "qty": trade.get("qty", 0),
            "fill_price": trade.get("fill_price", 0),
            "notional": trade.get("notional", 0),
            "fee": trade.get("fee", 0),
            "realized_pnl": trade.get("realized_pnl", 0),
            "order_id": trade.get("order_id", ""),
            "reason": signal_reason.replace(",", ";"),  # escape commas for CSV
        }

        try:
            with open(csv_file, "a") as f:
                if write_header:
                    f.write(",".join(row.keys()) + "\n")
                f.write(",".join(str(v) for v in row.values()) + "\n")
        except Exception as e:
            logger.error("Failed to write trade CSV: %s", e)

    # ------------------------------------------------------------------
    # Summary snapshot
    # ------------------------------------------------------------------

    def write_summary(
        self,
        portfolios_snapshot: Dict[str, Dict[str, Any]],
        engine_status: str = "running",
        pid: Optional[int] = None,
    ) -> None:
        """
        Write the latest status snapshot for all portfolios.

        This file is what Claude Code reads for the `trading_status` tool.
        It is overwritten atomically on each cycle.
        """
        total_nav = sum(s.get("nav", 0) for s in portfolios_snapshot.values())
        total_capital = sum(s.get("capital", 0) for s in portfolios_snapshot.values())
        total_pnl = sum(s.get("pnl", 0) for s in portfolios_snapshot.values())
        total_pnl_pct = (total_pnl / total_capital * 100) if total_capital > 0 else 0.0

        summary = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "engine_status": engine_status,
            "pid": pid or os.getpid(),
            "portfolios": portfolios_snapshot,
            "total_nav": round(total_nav, 2),
            "total_capital": round(total_capital, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
        }

        summary_file = self.logs_dir / "summary.json"
        tmp = summary_file.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2, default=str)
            os.rename(tmp, summary_file)
        except Exception as e:
            logger.error("Failed to write summary: %s", e)

    # ------------------------------------------------------------------
    # Competition intensive logging
    # ------------------------------------------------------------------

    def log_signal_detail(
        self,
        portfolio_id: str,
        insights: List[Dict[str, Any]],
        final_targets: Dict[str, float],
        filtered_out: Optional[List[str]] = None,
    ) -> None:
        """
        Log the full per-symbol, per-sleeve signal breakdown for a rebalance.

        Captures raw insight weights from every sleeve plus what the portfolio
        construction model actually accepted. Crucial for understanding which
        signals drove each trade.

        Written to logs/competition/<portfolio_id>/signals.jsonl
        """
        comp_dir = self.logs_dir / "competition" / portfolio_id
        comp_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "signal_detail",
            "portfolio": portfolio_id,
            "n_raw_insights": len(insights),
            "n_final_positions": len(final_targets),
            "n_filtered_out": len(filtered_out or []),
            "filtered_out": filtered_out or [],
            "final_targets": final_targets,
            "insights": insights,
        }

        log_file = comp_dir / "signals.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write signal detail for %s: %s", portfolio_id, e)

    def log_risk_detail(
        self,
        portfolio_id: str,
        risk_checks: List[Dict[str, Any]],
        combined_scale: float,
        nav: float,
        drawdown_pct: float,
    ) -> None:
        """
        Log each risk model's outcome (scale factor, reason, regime).

        Written to logs/competition/<portfolio_id>/risk.jsonl
        """
        comp_dir = self.logs_dir / "competition" / portfolio_id
        comp_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "risk_detail",
            "portfolio": portfolio_id,
            "combined_scale": combined_scale,
            "nav": nav,
            "drawdown_pct": drawdown_pct,
            "checks": risk_checks,
        }

        log_file = comp_dir / "risk.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write risk detail for %s: %s", portfolio_id, e)

    def log_pnl_snapshot(
        self,
        portfolio_id: str,
        snapshot: Dict[str, Any],
        source: str = "periodic",
    ) -> None:
        """
        Log a NAV/PnL snapshot to the competition per-strategy PnL CSV.
        Call this every few minutes during demo runs for granular tracking.

        Written to logs/competition/<portfolio_id>/pnl_snapshots.csv
        """
        comp_dir = self.logs_dir / "competition" / portfolio_id
        comp_dir.mkdir(parents=True, exist_ok=True)

        csv_file = comp_dir / "pnl_snapshots.csv"
        write_header = not csv_file.exists()

        row = {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "source":        source,
            "portfolio_id":  portfolio_id,
            "nav":           snapshot.get("nav", 0),
            "capital":       snapshot.get("capital", 0),
            "pnl":           snapshot.get("pnl", 0),
            "pnl_pct":       snapshot.get("pnl_pct", 0),
            "realized_pnl":  snapshot.get("realized_pnl", 0),
            "unrealized_pnl":snapshot.get("upnl", 0),
            "total_fees":    snapshot.get("total_fees", 0),
            "drawdown_pct":  snapshot.get("drawdown_pct", 0),
            "gross_exp":     snapshot.get("gross_exp", 0),
            "net_exp":       snapshot.get("net_exp", 0),
            "n_positions":   snapshot.get("n_positions", 0),
            "risk_cb":       snapshot.get("risk", {}).get("cb", ""),
            "risk_vol":      snapshot.get("risk", {}).get("vol", ""),
        }

        try:
            with open(csv_file, "a") as f:
                if write_header:
                    f.write(",".join(row.keys()) + "\n")
                f.write(",".join(str(v) for v in row.values()) + "\n")
        except Exception as e:
            logger.error("Failed to write PnL snapshot for %s: %s", portfolio_id, e)

    def log_rebalance_competition(
        self,
        portfolio_id: str,
        snapshot: Dict[str, Any],
        trades: List[Dict[str, Any]],
        signal_meta: Optional[Dict[str, Any]] = None,
        risk_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Full-detail rebalance log for competition strategies.
        Combines rebalance + signals + risk into one dense JSONL entry.

        Written to logs/competition/<portfolio_id>/rebalances.jsonl
        """
        comp_dir = self.logs_dir / "competition" / portfolio_id
        comp_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "event":    "rebalance",
            "portfolio": portfolio_id,
            "snapshot": snapshot,
            "trades":   trades,
            "n_trades": len(trades),
        }
        if signal_meta:
            entry["signals"] = signal_meta
        if risk_summary:
            entry["risk"] = risk_summary

        log_file = comp_dir / "rebalances.jsonl"
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write competition rebalance for %s: %s", portfolio_id, e)

        # Also write PnL snapshot on every rebalance
        self.log_pnl_snapshot(portfolio_id, snapshot, source="rebalance")

        logger.info(
            "[COMP:%s] Rebalance | NAV=$%.2f | PnL=$%.2f (%.2f%%) | %d trades | CB=%s Vol=%s",
            portfolio_id,
            snapshot.get("nav", 0), snapshot.get("pnl", 0), snapshot.get("pnl_pct", 0),
            len(trades),
            risk_summary.get("cb", "?") if risk_summary else "?",
            risk_summary.get("vol", "?") if risk_summary else "?",
        )

    # ------------------------------------------------------------------
    # Read helpers (for status command)
    # ------------------------------------------------------------------

    @staticmethod
    def read_summary(logs_dir: Path = LOGS_DIR) -> Optional[Dict[str, Any]]:
        """Read the latest summary snapshot. Returns None if not available."""
        summary_file = logs_dir / "summary.json"
        if not summary_file.exists():
            return None
        try:
            with open(summary_file, "r") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def read_recent_events(
        portfolio_id: str,
        n: int = 20,
        logs_dir: Path = LOGS_DIR,
    ) -> List[Dict[str, Any]]:
        """Read the last N events from a portfolio's event log."""
        log_file = logs_dir / f"{portfolio_id}.jsonl"
        if not log_file.exists():
            return []
        try:
            with open(log_file, "r") as f:
                lines = f.readlines()
            recent = lines[-n:]
            return [json.loads(line) for line in recent if line.strip()]
        except Exception:
            return []
