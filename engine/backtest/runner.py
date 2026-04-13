"""
backtest/runner.py
==================
BacktestRunner: unified backtester that runs the exact same TradingAlgorithm
pipeline used in live trading — only the ExecutionModel swaps out.

Key property: any risk rule, signal filter, or portfolio construction change
you make to the live engine automatically applies in the backtest. No separate
backtest logic to keep in sync.

Architecture:
    BacktestRunner
      └─ TradingAlgorithm (same as live)
           ├─ CombinedAlphaModel        (unchanged)
           ├─ SignalFilteredPortfolioModel (unchanged)
           ├─ CompositeRiskModel         (unchanged — CB + Vol + Corr)
           └─ SimulatedExecution         ← only difference vs live
                 fills at mark price ± square-root slippage, no exchange needed

Usage:
    runner = BacktestRunner(profile_name="daily", mode="futures", capital=5000)
    results = runner.run(price_data, start="2024-01-01", end="2024-12-31")
    BacktestRunner.print_results(results)

Results dict is compatible with the legacy BacktestEngine output format so
existing tooling (metrics, monthly P&L table, optimiser) keeps working.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.metrics import compute_all_metrics, monthly_pnl_table, print_metrics
from config.profiles import get_profile
from config.settings import INITIAL_CAPITAL, TRADING_MODE
from core.algorithm import TradingAlgorithm
from core.alpha import CombinedAlphaModel
from core.execution import SimulatedExecution
from core.portfolio_construction import SignalFilteredPortfolioModel
from core.risk import (
    CompositeRiskModel,
    CorrelationWatchdogModel,
    DrawdownCircuitBreakerModel,
    VolRegimeModel,
)
from logging_.null_logger import NullLogger
from portfolio.portfolio import Portfolio

logger = logging.getLogger(__name__)


def _deep_update(base: dict, overrides: dict) -> dict:
    """Recursively merge *overrides* into *base* (modifies base in-place)."""
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


class BacktestRunner:
    """
    Bar-by-bar backtest using the live TradingAlgorithm pipeline.

    Parameters
    ----------
    profile_name     : "daily" or "hourly"
    mode             : "futures" | "spot" | "margin"
    initial_capital  : starting NAV in USD
    slippage_factor  : square-root market impact coefficient (default 0.10)
    profile_overrides: dict deep-merged into the profile after loading.
                       Use this to override sleeve weights, portfolio limits, etc.
                       e.g. {"portfolio_weights": {"trend": 0.70, ...}}
    risk_overrides   : dict of risk model thresholds.
                       Keys: drawdown_cb_1, drawdown_cb_2
                       e.g. {"drawdown_cb_1": 0.08, "drawdown_cb_2": 0.15}
    """

    def __init__(
        self,
        profile_name: str = "daily",
        mode: str = TRADING_MODE,
        initial_capital: float = INITIAL_CAPITAL,
        slippage_factor: float = 0.10,
        profile_overrides: Optional[dict] = None,
        risk_overrides: Optional[dict] = None,
    ):
        self.profile_name      = profile_name
        self.mode              = mode
        self.initial_capital   = initial_capital
        self.profile_overrides = profile_overrides or {}
        self.risk_overrides    = risk_overrides or {}

        self._sim_exec = SimulatedExecution(slippage_factor=slippage_factor)
        self._algorithm = self._build_algorithm()
        self._null_logger = NullLogger()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(
        self,
        price_data: Dict[str, pd.DataFrame],
        start: str,
        end: str,
    ) -> Dict[str, Any]:
        """
        Run the unified backtest over historical price data.

        Parameters
        ----------
        price_data : {symbol: OHLCV DataFrame} from fetch_universe / synthetic
        start      : ISO date string "YYYY-MM-DD"
        end        : ISO date string "YYYY-MM-DD"

        Returns
        -------
        Dict compatible with legacy BacktestEngine results format.
        """
        profile = get_profile(self.profile_name)
        if self.profile_overrides:
            _deep_update(profile, self.profile_overrides)
        periods_per_year = int(profile["periods_per_year"])
        bars_per_day = int(profile["bars_per_day"])
        lookback_days = int(profile["market_data_lookback_days"])
        rebalance_every = max(1, bars_per_day // 6)   # 4h for hourly, 1 for daily
        min_bars_for_signal = int(profile["backtest_guards"]["min_history_bars"])

        # Build close / open matrices over the full date range
        from data.fetcher import build_close_matrix, build_open_matrix
        closes = build_close_matrix(price_data, max_ffill_days=int(profile["max_ffill_bars"]))
        opens  = build_open_matrix(price_data)
        closes = closes.loc[start:end]
        opens  = opens.loc[start:end]

        # Create a fresh live Portfolio (same class used in production)
        portfolio = Portfolio(
            portfolio_id="backtest",
            strategy_id="combined_portfolio",
            profile_name=self.profile_name,
            initial_capital=self.initial_capital,
            rebalance_interval_sec=86400,   # scheduling not used in backtest
        )

        nav_history: List[tuple] = []   # (date, nav)
        all_trades: List[dict]   = []
        total_fees    = 0.0
        total_slippage = 0.0
        total_funding  = 0.0
        total_turnover = 0.0

        dates = closes.index.tolist()
        logger.info(
            "BacktestRunner: %d bars | %s → %s | profile=%s",
            len(dates), dates[0].date() if dates else "?", dates[-1].date() if dates else "?",
            self.profile_name,
        )

        for bar_idx, date in enumerate(dates):
            mark_prices = self._prices_on(closes, date)
            exec_prices = self._prices_on(opens,  date)
            if not mark_prices:
                continue

            # Apply funding payments (futures only)
            if self.mode == "futures":
                funding = self._funding_on(price_data, closes.index, date)
                fund_pnl = self._apply_funding(portfolio, mark_prices, funding)
                total_funding += abs(fund_pnl)

            # Record NAV every bar
            current_nav = portfolio.nav(mark_prices)
            nav_history.append((date, current_nav))
            portfolio.nav_history.append({"date": str(date), "nav": current_nav})

            # Rebalance on schedule once we have enough history
            if bar_idx % rebalance_every != 0 or bar_idx < min_bars_for_signal:
                continue
            if not exec_prices:
                continue

            # Compute ADV for slippage model
            adv = self._avg_daily_volumes(price_data, closes.index, date, bars_per_day,
                                          int(profile["backtest_guards"]["liquidity_lookback"]))
            self._sim_exec.set_volumes(adv)

            # Slice data to this date — no lookahead bias
            sliced = self._slice_to_date(price_data, date, lookback_days)
            if not sliced:
                continue

            # Execute the unified pipeline
            result = self._algorithm.rebalance(
                portfolio=portfolio,
                price_data=sliced,
                profile=profile,
                mode=self.mode,
                slogger=self._null_logger,
            )

            if result.success and result.trades:
                for t in result.trades:
                    if "error" not in t:
                        notional = abs(t.get("notional", 0))
                        total_fees     += t.get("fee", 0)
                        total_slippage += t.get("slippage", 0)
                        total_turnover += notional
                        all_trades.append(t)

            # After rebalance, re-mark positions at execution prices
            # (price moved from mark to exec between bar open and signal)
            # We already have fill prices in trade records — portfolio is updated.

        # Build NAV series for metrics
        if not nav_history:
            return {"metrics": {}, "nav_series": pd.Series(dtype=float), "trades": pd.DataFrame()}

        nav_series = pd.Series(
            [n for _, n in nav_history],
            index=pd.DatetimeIndex([d for d, _ in nav_history]),
            name="nav",
        )

        # Trade P&L series for Sharpe/Sortino
        trade_pnls = None
        if all_trades:
            rpnls = [t.get("realized_pnl", 0) for t in all_trades if "realized_pnl" in t]
            if rpnls:
                trade_pnls = pd.Series(rpnls, dtype=float)

        metrics = compute_all_metrics(
            nav=nav_series,
            trade_pnls=trade_pnls,
            total_fees=total_fees,
            mode=self.mode,
            total_slippage=total_slippage,
            total_funding=total_funding,
            turnover=total_turnover,
            periods_per_year=periods_per_year,
        )

        trades_df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

        return {
            "metrics": metrics,
            "nav_series": nav_series,
            "trades": trades_df,
            "monthly_pnl": monthly_pnl_table(nav_series),
            "total_fees": total_fees,
            "total_funding": total_funding,
            "strategy": "combined_portfolio",
            "mode": self.mode,
            "profile": self.profile_name,
            "engine": "unified",
        }

    @staticmethod
    def print_results(results: Dict[str, Any]) -> None:
        engine_label = results.get("engine", "legacy")
        print(f"\n[BacktestRunner — {engine_label} pipeline]\n")
        print_metrics(results["metrics"])
        monthly = results.get("monthly_pnl")
        if monthly is not None and not monthly.empty:
            print("\nMonthly Returns (%):\n")
            print(monthly.to_string())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_algorithm(self) -> TradingAlgorithm:
        """Same risk stack as TradingEngine._build_algorithm(), SwapExecution only."""
        ro = self.risk_overrides
        if self.profile_name == "hourly":
            cb1 = ro.get("drawdown_cb_1", 0.08)
            cb2 = ro.get("drawdown_cb_2", 0.15)
            risk_stack = CompositeRiskModel([
                DrawdownCircuitBreakerModel(threshold_reduced=cb1, threshold_cash=cb2),
                VolRegimeModel(),
                CorrelationWatchdogModel(threshold=0.80),
            ])
        else:
            cb1 = ro.get("drawdown_cb_1", 0.10)
            cb2 = ro.get("drawdown_cb_2", 0.20)
            risk_stack = CompositeRiskModel([
                DrawdownCircuitBreakerModel(threshold_reduced=cb1, threshold_cash=cb2),
                VolRegimeModel(),
                CorrelationWatchdogModel(),
            ])

        return TradingAlgorithm(
            alpha=CombinedAlphaModel(),
            portfolio_construction=SignalFilteredPortfolioModel(),
            risk=risk_stack,
            execution=self._sim_exec,
        )

    @staticmethod
    def _prices_on(matrix: pd.DataFrame, date: pd.Timestamp) -> Dict[str, float]:
        if date not in matrix.index:
            return {}
        row = matrix.loc[date]
        return {sym: float(row[sym]) for sym in matrix.columns if not pd.isna(row[sym])}

    @staticmethod
    def _funding_on(
        price_data: Dict[str, pd.DataFrame],
        index: pd.Index,
        date: pd.Timestamp,
    ) -> Dict[str, float]:
        funding: Dict[str, float] = {}
        for sym, df in price_data.items():
            if "funding_rate" not in df.columns:
                continue
            try:
                value = df["funding_rate"].reindex(index).shift(1).fillna(0.0).loc[date]
                funding[sym] = float(value)
            except Exception:
                pass
        return funding

    @staticmethod
    def _apply_funding(
        portfolio: Portfolio,
        mark_prices: Dict[str, float],
        funding: Dict[str, float],
    ) -> float:
        """Apply funding payments directly to portfolio cash. Returns total paid."""
        total = 0.0
        for sym, pos in portfolio.positions.items():
            price = mark_prices.get(sym, 0.0)
            fr = funding.get(sym, 0.0)
            if abs(pos.qty) > 0 and price > 0 and fr != 0:
                pnl = pos.qty * price * fr
                portfolio.cash += pnl
                total += pnl
        return total

    @staticmethod
    def _avg_daily_volumes(
        price_data: Dict[str, pd.DataFrame],
        index: pd.Index,
        date: pd.Timestamp,
        bars_per_day: int,
        lookback: int,
    ) -> Dict[str, float]:
        adv: Dict[str, float] = {}
        for sym, df in price_data.items():
            if "volume" not in df.columns or "close" not in df.columns:
                continue
            vol_usd = df["volume"] * df["close"]
            prior = vol_usd.shift(1).reindex(index).loc[:date].iloc[-lookback:]
            adv[sym] = float(prior.mean()) * bars_per_day if len(prior) > 0 else 0.0
        return adv

    @staticmethod
    def _slice_to_date(
        price_data: Dict[str, pd.DataFrame],
        date: pd.Timestamp,
        lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Return price data up to and including *date*, capped at lookback_days bars."""
        result: Dict[str, pd.DataFrame] = {}
        for sym, df in price_data.items():
            sliced = df.loc[:date]
            if sliced.empty:
                continue
            if len(sliced) > lookback_days:
                sliced = sliced.iloc[-lookback_days:]
            result[sym] = sliced
        return result
