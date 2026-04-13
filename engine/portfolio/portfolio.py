"""
portfolio/portfolio.py
======================
Portfolio and Position classes for isolated per-portfolio tracking.

Each Portfolio instance maintains its own positions, cash, and PnL —
independent of the exchange account balance. The exchange is used for
execution only; bookkeeping is internal.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR, TRANSACTION_COSTS, TRADING_MODE

logger = logging.getLogger(__name__)

STATE_FILE = BASE_DIR / "data" / "engine_state.json"


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """A single position within a portfolio."""

    symbol: str
    qty: float = 0.0                # positive = long, negative = short
    avg_entry_price: float = 0.0    # volume-weighted average entry

    def unrealized_pnl(self, mark_price: float) -> float:
        """Compute unrealized PnL at the given mark price."""
        if self.qty == 0 or mark_price <= 0:
            return 0.0
        return (mark_price - self.avg_entry_price) * self.qty

    def notional(self, mark_price: float) -> float:
        """Absolute notional value at mark price."""
        return abs(self.qty * mark_price)

    def side(self) -> str:
        if self.qty > 0:
            return "long"
        elif self.qty < 0:
            return "short"
        return "flat"

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "qty": self.qty, "avg_entry_price": self.avg_entry_price}

    @classmethod
    def from_dict(cls, d: dict) -> Position:
        return cls(symbol=d["symbol"], qty=d["qty"], avg_entry_price=d["avg_entry_price"])


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

class Portfolio:
    """
    Isolated portfolio with its own capital, positions, and performance tracking.

    Designed so that multiple Portfolio instances can share one exchange account
    while each maintaining independent bookkeeping.
    """

    def __init__(
        self,
        portfolio_id: str,
        strategy_id: str,
        profile_name: str,
        initial_capital: float,
        rebalance_interval_sec: int = 86400,
        risk_check_interval_sec: int = 60,
    ):
        self.portfolio_id = portfolio_id
        self.strategy_id = strategy_id
        self.profile_name = profile_name
        self.initial_capital = initial_capital
        self.cash = initial_capital  # tracks: initial + realized_pnl - fees
        self.positions: Dict[str, Position] = {}
        self.realized_pnl_total: float = 0.0
        self.total_fees: float = 0.0
        self.rebalance_interval_sec = rebalance_interval_sec
        self.risk_check_interval_sec = risk_check_interval_sec
        self.last_rebalance: Optional[datetime] = None
        self.last_risk_check: Optional[datetime] = None
        self.last_target_weights: Dict[str, float] = {}
        self.trade_log: List[Dict[str, Any]] = []
        self.nav_history: List[Dict[str, Any]] = []
        self.risk_state: Dict[str, Any] = {
            "peak_nav": initial_capital,
            "circuit_breaker_state": "NORMAL",
            "circuit_breaker_cash_since": None,
            "vol_regime": "MEDIUM",
            "total_fees": 0.0,
            "total_slippage": 0.0,
            "total_funding": 0.0,
        }
        self.engine_status: str = "running"
        self.created_at: str = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # NAV and PnL
    # ------------------------------------------------------------------

    def nav(self, prices: Dict[str, float]) -> float:
        """Portfolio NAV = cash + total unrealized PnL."""
        return max(self.cash + self.total_unrealized_pnl(prices), 0.0)

    def total_unrealized_pnl(self, prices: Dict[str, float]) -> float:
        """Sum of unrealized PnL across all open positions."""
        total = 0.0
        for pos in self.positions.values():
            mark = prices.get(pos.symbol, 0.0)
            total += pos.unrealized_pnl(mark)
        return total

    def total_pnl(self, prices: Dict[str, float]) -> float:
        """Total PnL = realized + unrealized - fees."""
        return self.realized_pnl_total + self.total_unrealized_pnl(prices) - self.total_fees

    def pnl_pct(self, prices: Dict[str, float]) -> float:
        """Total PnL as percentage of initial capital."""
        if self.initial_capital <= 0:
            return 0.0
        return self.total_pnl(prices) / self.initial_capital * 100

    # ------------------------------------------------------------------
    # Position weights and exposure
    # ------------------------------------------------------------------

    def position_weights(self, prices: Dict[str, float]) -> Dict[str, float]:
        """Current position weights as fraction of NAV."""
        current_nav = self.nav(prices)
        if current_nav <= 0:
            return {}
        weights = {}
        for sym, pos in self.positions.items():
            mark = prices.get(sym, 0.0)
            if mark > 0:
                weights[sym] = (pos.qty * mark) / current_nav
        return weights

    def gross_exposure(self, prices: Dict[str, float]) -> float:
        """Gross exposure as fraction of NAV."""
        current_nav = self.nav(prices)
        if current_nav <= 0:
            return 0.0
        gross = sum(pos.notional(prices.get(pos.symbol, 0.0)) for pos in self.positions.values())
        return gross / current_nav

    def net_exposure(self, prices: Dict[str, float]) -> float:
        """Net exposure as fraction of NAV."""
        current_nav = self.nav(prices)
        if current_nav <= 0:
            return 0.0
        net = sum(pos.qty * prices.get(pos.symbol, 0.0) for pos in self.positions.values())
        return net / current_nav

    # ------------------------------------------------------------------
    # Trade execution bookkeeping
    # ------------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        fill_price: float,
        fee: float,
        order_id: str = "",
    ) -> Dict[str, Any]:
        """
        Update internal positions and cash after a trade execution.

        For futures: cash only changes by fees (margin handled by exchange).
        Realized PnL is booked when reducing or closing a position.
        """
        pos = self.positions.get(symbol)
        realized = 0.0

        signed_qty = qty if side == "buy" else -qty

        if pos is None:
            # New position
            pos = Position(symbol=symbol, qty=signed_qty, avg_entry_price=fill_price)
            self.positions[symbol] = pos
        else:
            # Existing position — check if adding or reducing
            old_qty = pos.qty

            if (old_qty > 0 and signed_qty > 0) or (old_qty < 0 and signed_qty < 0):
                # Adding to position — update weighted average entry
                total_cost = pos.avg_entry_price * abs(old_qty) + fill_price * abs(signed_qty)
                new_qty = old_qty + signed_qty
                pos.avg_entry_price = total_cost / abs(new_qty) if new_qty != 0 else 0
                pos.qty = new_qty
            else:
                # Reducing or flipping position
                reduce_qty = min(abs(signed_qty), abs(old_qty))
                realized = (fill_price - pos.avg_entry_price) * reduce_qty
                if old_qty < 0:
                    realized = -realized  # short position: profit when price drops

                remaining_new = abs(signed_qty) - reduce_qty
                new_qty = old_qty + signed_qty

                if abs(new_qty) < 1e-10:
                    # Position fully closed
                    new_qty = 0
                    pos.avg_entry_price = 0
                elif remaining_new > 0:
                    # Position flipped — new entry at fill price
                    pos.avg_entry_price = fill_price
                # else: partially reduced, keep old avg entry

                pos.qty = new_qty

        # Remove flat positions
        if abs(pos.qty) < 1e-10:
            self.positions.pop(symbol, None)

        # Update cash and totals
        self.realized_pnl_total += realized
        self.total_fees += fee
        self.cash += realized - fee

        notional = qty * fill_price
        trade_record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "fill_price": fill_price,
            "notional": notional,
            "fee": fee,
            "realized_pnl": realized,
            "order_id": order_id,
        }
        self.trade_log.append(trade_record)

        logger.info(
            "[%s] Trade: %s %s %.6f @ %.2f | notional=$%.2f | fee=$%.4f | realized=$%.2f",
            self.portfolio_id, side.upper(), symbol, qty, fill_price,
            notional, fee, realized,
        )
        return trade_record

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def should_rebalance(self, now: datetime) -> bool:
        if self.engine_status != "running":
            return False
        if self.last_rebalance is None:
            return True
        elapsed = (now - self.last_rebalance).total_seconds()
        return elapsed >= self.rebalance_interval_sec

    def should_check_risk(self, now: datetime) -> bool:
        if self.engine_status != "running":
            return False
        if self.last_risk_check is None:
            return True
        elapsed = (now - self.last_risk_check).total_seconds()
        return elapsed >= self.risk_check_interval_sec

    # ------------------------------------------------------------------
    # Snapshot for logging
    # ------------------------------------------------------------------

    def snapshot(self, prices: Dict[str, float]) -> Dict[str, Any]:
        """Full portfolio state snapshot for structured logging."""
        current_nav = self.nav(prices)
        peak = self.risk_state.get("peak_nav", self.initial_capital)
        dd_pct = ((peak - current_nav) / peak * 100) if peak > 0 else 0.0

        positions_snap = {}
        for sym, pos in self.positions.items():
            mark = prices.get(sym, 0.0)
            upnl = pos.unrealized_pnl(mark)
            notional = pos.notional(mark)
            weight = (pos.qty * mark / current_nav) if current_nav > 0 else 0.0
            positions_snap[sym] = {
                "qty": round(pos.qty, 8),
                "entry": round(pos.avg_entry_price, 4),
                "mark": round(mark, 4),
                "notional": round(notional, 2),
                "weight": round(weight, 4),
                "upnl": round(upnl, 2),
                "side": pos.side(),
            }

        return {
            "portfolio_id": self.portfolio_id,
            "strategy_id": self.strategy_id,
            "profile": self.profile_name,
            "status": self.engine_status,
            "nav": round(current_nav, 2),
            "capital": self.initial_capital,
            "cash": round(self.cash, 2),
            "pnl": round(self.total_pnl(prices), 2),
            "pnl_pct": round(self.pnl_pct(prices), 2),
            "realized_pnl": round(self.realized_pnl_total, 2),
            "upnl": round(self.total_unrealized_pnl(prices), 2),
            "total_fees": round(self.total_fees, 2),
            "peak_nav": round(peak, 2),
            "drawdown_pct": round(dd_pct, 2),
            "positions": positions_snap,
            "n_positions": len(self.positions),
            "gross_exp": round(self.gross_exposure(prices), 4),
            "net_exp": round(self.net_exposure(prices), 4),
            "risk": {
                "cb": self.risk_state.get("circuit_breaker_state", "NORMAL"),
                "vol": self.risk_state.get("vol_regime", "MEDIUM"),
                "scalar": self.risk_state.get("risk_scalar", 1.0),
            },
            "last_rebalance": self.last_rebalance.isoformat() if self.last_rebalance else None,
            "next_rebalance": (
                (self.last_rebalance.timestamp() + self.rebalance_interval_sec)
                if self.last_rebalance
                else None
            ),
            "created_at": self.created_at,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Serialize portfolio state for persistence."""
        return {
            "portfolio_id": self.portfolio_id,
            "strategy_id": self.strategy_id,
            "profile_name": self.profile_name,
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "realized_pnl_total": self.realized_pnl_total,
            "total_fees": self.total_fees,
            "positions": {sym: pos.to_dict() for sym, pos in self.positions.items()},
            "rebalance_interval_sec": self.rebalance_interval_sec,
            "risk_check_interval_sec": self.risk_check_interval_sec,
            "last_rebalance": self.last_rebalance.isoformat() if self.last_rebalance else None,
            "last_risk_check": self.last_risk_check.isoformat() if self.last_risk_check else None,
            "last_target_weights": self.last_target_weights,
            "trade_log": self.trade_log[-500:],  # keep last 500 trades
            "nav_history": self.nav_history[-1000:],  # keep last 1000 entries
            "risk_state": self.risk_state,
            "engine_status": self.engine_status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Portfolio:
        """Restore portfolio from persisted state."""
        p = cls(
            portfolio_id=d["portfolio_id"],
            strategy_id=d["strategy_id"],
            profile_name=d["profile_name"],
            initial_capital=d["initial_capital"],
            rebalance_interval_sec=d.get("rebalance_interval_sec", 86400),
            risk_check_interval_sec=d.get("risk_check_interval_sec", 60),
        )
        p.cash = d["cash"]
        p.realized_pnl_total = d.get("realized_pnl_total", 0.0)
        p.total_fees = d.get("total_fees", 0.0)
        p.positions = {
            sym: Position.from_dict(pos_d)
            for sym, pos_d in d.get("positions", {}).items()
        }
        if d.get("last_rebalance"):
            p.last_rebalance = datetime.fromisoformat(d["last_rebalance"])
        if d.get("last_risk_check"):
            p.last_risk_check = datetime.fromisoformat(d["last_risk_check"])
        p.last_target_weights = d.get("last_target_weights", {})
        p.trade_log = d.get("trade_log", [])
        p.nav_history = d.get("nav_history", [])
        p.risk_state = d.get("risk_state", p.risk_state)
        p.engine_status = d.get("engine_status", "running")
        p.created_at = d.get("created_at", p.created_at)
        return p


# ---------------------------------------------------------------------------
# Engine state persistence (all portfolios)
# ---------------------------------------------------------------------------

def save_engine_state(portfolios: Dict[str, Portfolio], state_file: Path = STATE_FILE) -> None:
    """Atomically persist all portfolio states."""
    state = {
        "version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "portfolios": {pid: p.to_dict() for pid, p in portfolios.items()},
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)

    backup = state_file.with_suffix(".backup.json")
    if state_file.exists():
        shutil.copy2(state_file, backup)

    tmp = state_file.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.rename(tmp, state_file)
    logger.debug("Engine state saved to %s", state_file)


def load_engine_state(state_file: Path = STATE_FILE) -> Dict[str, Portfolio]:
    """Load all portfolios from persisted state."""
    if not state_file.exists():
        return {}

    with open(state_file, "r") as f:
        data = json.load(f)

    portfolios = {}
    for pid, pdata in data.get("portfolios", {}).items():
        portfolios[pid] = Portfolio.from_dict(pdata)
        logger.info("Restored portfolio '%s' (NAV state: cash=$%.2f)", pid, portfolios[pid].cash)

    return portfolios
