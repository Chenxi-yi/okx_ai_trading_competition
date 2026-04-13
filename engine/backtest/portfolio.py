"""
Portfolio accounting for target-weight futures backtests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

import pandas as pd

from config.settings import INITIAL_CAPITAL, PORTFOLIO, SLIPPAGE_FACTOR, TRANSACTION_COSTS, TRADING_MODE


@dataclass
class Trade:
    date: pd.Timestamp
    symbol: str
    notional_delta: float
    fill_price: float
    fee: float
    side: str


class Portfolio:
    def __init__(self, mode: str = TRADING_MODE, initial_capital: float = INITIAL_CAPITAL):
        self.mode = mode
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: Dict[str, float] = {}
        self.trades: List[Trade] = []
        self.nav_history: List[Tuple[pd.Timestamp, float]] = []
        self.total_fees: float = 0.0
        self.total_slippage: float = 0.0
        self.total_funding: float = 0.0
        self.total_turnover: float = 0.0
        self.min_rebalance_notional_usd: float = float(PORTFOLIO["min_rebalance_notional_usd"])

    def nav(self, prices: Dict[str, float]) -> float:
        mtm = 0.0
        for sym, qty in self.positions.items():
            if sym in prices:
                mtm += qty * prices[sym]
        return max(self.cash + mtm, 0.0)

    def record_nav(self, date: pd.Timestamp, prices: Dict[str, float]) -> float:
        current_nav = self.nav(prices)
        self.nav_history.append((date, current_nav))
        return current_nav

    def position_weights(self, prices: Dict[str, float]) -> Dict[str, float]:
        current_nav = self.nav(prices)
        if current_nav == 0:
            return {sym: 0.0 for sym in self.positions}
        return {
            sym: (qty * prices.get(sym, 0.0)) / current_nav
            for sym, qty in self.positions.items()
        }

    def apply_funding(self, date: pd.Timestamp, prices: Dict[str, float], funding_rates: Dict[str, float]) -> float:
        funding_cashflow = 0.0
        for sym, qty in self.positions.items():
            price = prices.get(sym)
            if price is None:
                continue
            rate = float(funding_rates.get(sym, 0.0))
            notional = qty * price
            funding = -notional * rate
            self.cash += funding
            funding_cashflow += funding
        self.total_funding += -funding_cashflow
        return funding_cashflow

    def _compute_slippage_pct(self, order_size_usd: float, avg_daily_volume_usd: float) -> float:
        """Square-root market impact model: slippage = factor * sqrt(order / volume)."""
        if avg_daily_volume_usd <= 0:
            return 0.0
        return SLIPPAGE_FACTOR * math.sqrt(order_size_usd / avg_daily_volume_usd)

    def rebalance_to_weights(
        self,
        date: pd.Timestamp,
        prices: Dict[str, float],
        target_weights: Dict[str, float],
        slippage_factor: float = 0.0,
        avg_daily_volumes: Dict[str, float] | None = None,
    ) -> None:
        nav = self.nav(prices)
        if nav <= 0:
            return
        tx_cost_pct = TRANSACTION_COSTS[self.mode]
        symbols = sorted(set(target_weights) | set(self.positions))

        for sym in symbols:
            price = prices.get(sym)
            if price is None or price <= 0:
                continue

            current_qty = self.positions.get(sym, 0.0)
            current_notional = current_qty * price
            target_notional = float(target_weights.get(sym, 0.0)) * nav
            delta_notional = target_notional - current_notional

            if abs(delta_notional) < self.min_rebalance_notional_usd:
                continue

            adv = (avg_daily_volumes or {}).get(sym, 0.0)
            slip_pct = self._compute_slippage_pct(abs(delta_notional), adv) if adv > 0 else 0.0
            fee = abs(delta_notional) * tx_cost_pct
            slippage_cost = abs(delta_notional) * slip_pct
            total_cost = fee + slippage_cost
            delta_qty = delta_notional / price

            self.cash -= delta_notional
            self.cash -= total_cost
            self.total_fees += fee
            self.total_slippage += slippage_cost
            self.total_turnover += abs(delta_notional)

            new_qty = current_qty + delta_qty
            if abs(new_qty) < 1e-10:
                self.positions.pop(sym, None)
            else:
                self.positions[sym] = new_qty

            self.trades.append(
                Trade(
                    date=date,
                    symbol=sym,
                    notional_delta=delta_notional,
                    fill_price=price,
                    fee=total_cost,
                    side="buy" if delta_notional > 0 else "sell",
                )
            )

    def nav_series(self) -> pd.Series:
        if not self.nav_history:
            return pd.Series(dtype=float)
        dates, navs = zip(*self.nav_history)
        return pd.Series(navs, index=pd.DatetimeIndex(dates), name="nav")

    def trades_dataframe(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        rows = []
        for trade in self.trades:
            rows.append(
                {
                    "date": trade.date,
                    "symbol": trade.symbol,
                    "notional_delta": trade.notional_delta,
                    "fill_price": trade.fill_price,
                    "fee": trade.fee,
                    "side": trade.side,
                }
            )
        return pd.DataFrame(rows).set_index("date")

    def trade_pnls(self) -> pd.Series:
        """Compute per-rebalance trade PnL from the trade log.

        Groups round-trip trades by symbol and computes realized PnL
        whenever a position is reduced or reversed.
        """
        if not self.trades:
            return pd.Series(dtype=float)
        pnls: List[float] = []
        open_cost: Dict[str, float] = {}
        open_qty: Dict[str, float] = {}
        for t in self.trades:
            sym = t.symbol
            qty_delta = t.notional_delta / t.fill_price if t.fill_price else 0.0
            prev_qty = open_qty.get(sym, 0.0)
            prev_cost = open_cost.get(sym, 0.0)
            new_qty = prev_qty + qty_delta

            if prev_qty != 0 and ((prev_qty > 0 and qty_delta < 0) or (prev_qty < 0 and qty_delta > 0)):
                closed_qty = min(abs(qty_delta), abs(prev_qty))
                avg_entry = prev_cost / prev_qty if prev_qty != 0 else 0.0
                if prev_qty > 0:
                    pnl = closed_qty * (t.fill_price - avg_entry) - t.fee
                else:
                    pnl = closed_qty * (avg_entry - t.fill_price) - t.fee
                pnls.append(pnl)
                remaining_frac = 1.0 - (closed_qty / abs(prev_qty))
                open_cost[sym] = prev_cost * remaining_frac
                open_qty[sym] = new_qty
                if abs(new_qty) > abs(qty_delta) - closed_qty + 1e-12:
                    excess = abs(qty_delta) - closed_qty
                    if excess > 1e-12:
                        open_cost[sym] += excess * t.fill_price * (1 if new_qty > 0 else -1)
            else:
                open_qty[sym] = new_qty
                open_cost[sym] = prev_cost + qty_delta * t.fill_price

        return pd.Series(pnls, dtype=float)
