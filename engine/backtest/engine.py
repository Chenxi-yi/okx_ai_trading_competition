"""
Backtest engine for sleeve-based target-weight strategies.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from backtest.metrics import compute_all_metrics, monthly_pnl_table, print_metrics
from backtest.portfolio import Portfolio
from config.profiles import get_profile
from config.settings import (
    BACKTEST_END,
    BACKTEST_START,
    EXECUTION_PRICE_FIELD,
    INITIAL_CAPITAL,
    MARK_PRICE_FIELD,
    TRADING_MODE,
)
from data.fetcher import build_close_matrix, build_open_matrix
from portfolio.portfolio_manager import PortfolioManager
from risk.risk_manager_v2 import RiskManagerV2, CircuitBreakerState
from strategies.base import BaseStrategy, StrategyOutput


class BacktestEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        price_data: Dict[str, pd.DataFrame],
        mode: str = TRADING_MODE,
        initial_capital: float = INITIAL_CAPITAL,
        profile_name: str = "daily",
    ):
        self.strategy = strategy
        self.price_data = price_data
        self.mode = mode
        self.initial_capital = initial_capital
        self.profile = get_profile(profile_name)
        self.profile_name = self.profile["name"]
        self.periods_per_year = int(self.profile["periods_per_year"])
        self.bars_per_day = int(self.profile["bars_per_day"])
        self.backtest_guards = self.profile["backtest_guards"]
        self.risk_cfg = self.profile["risk"]
        self.portfolio = Portfolio(mode=mode, initial_capital=initial_capital)
        self.portfolio_manager = PortfolioManager()
        self.risk_manager = RiskManagerV2(mode=mode, initial_capital=initial_capital)
        self.closes = build_close_matrix(
            price_data,
            max_ffill_days=int(self.profile["max_ffill_bars"]),
        )
        self.opens = build_open_matrix(price_data)
        self.symbols = list(self.closes.columns)
        self.tradeability_mask = self._build_tradeability_mask()

    def _prices_on(self, date: pd.Timestamp, field: str = MARK_PRICE_FIELD) -> Dict[str, float]:
        matrix = self.opens if field == EXECUTION_PRICE_FIELD else self.closes
        row = matrix.loc[date]
        return {sym: float(row[sym]) for sym in self.symbols if not pd.isna(row[sym])}

    def _funding_on(self, date: pd.Timestamp) -> Dict[str, float]:
        funding = {}
        for sym, df in self.price_data.items():
            if "funding_rate" not in df.columns:
                funding[sym] = 0.0
                continue
            value = df["funding_rate"].reindex(self.closes.index).shift(1).fillna(0.0).loc[date]
            funding[sym] = float(value)
        return funding

    def _avg_daily_volumes(self, date: pd.Timestamp, window: int | None = None) -> Dict[str, float]:
        """Compute trailing average daily volume in USD for each symbol."""
        window = window or int(self.backtest_guards["liquidity_lookback"])
        volumes: Dict[str, float] = {}
        for sym, df in self.price_data.items():
            if "volume" not in df.columns or "close" not in df.columns:
                continue
            vol_usd = df["volume"] * df["close"]
            prior = vol_usd.shift(1).loc[:date].iloc[-window:]
            volumes[sym] = (float(prior.mean()) * self.bars_per_day) if len(prior) > 0 else 0.0
        return volumes

    def _build_tradeability_mask(self) -> pd.DataFrame:
        raw_close = pd.DataFrame({sym: df["close"] for sym, df in self.price_data.items()}).sort_index()
        raw_volume = pd.DataFrame({sym: df["volume"] for sym, df in self.price_data.items()}).sort_index()
        history_count = raw_close.notna().cumsum().shift(1).fillna(0.0)
        adv_usd = (raw_close * raw_volume).shift(1).rolling(
            int(self.backtest_guards["liquidity_lookback"]),
            min_periods=max(5, int(self.backtest_guards["liquidity_lookback"]) // 2),
        ).mean() * self.bars_per_day
        eligible = history_count >= int(self.backtest_guards["min_history_bars"])
        liquid = adv_usd >= float(self.backtest_guards["min_adv_usd"])
        available = self.opens.notna() & self.closes.notna()
        mask = (eligible & liquid & available).reindex(index=self.closes.index, columns=self.closes.columns, fill_value=False)
        return mask

    def run(self, start: str = BACKTEST_START, end: str = BACKTEST_END) -> Dict:
        output: StrategyOutput = self.strategy.generate(self.price_data, mode=self.mode)
        target_weights = output.target_weights.shift(1).fillna(0.0)
        if self.backtest_guards["apply_tradeability_filter"]:
            target_weights = target_weights.where(self.tradeability_mask, 0.0)
        target_weights = self.portfolio_manager.align_output(target_weights, self.mode)

        # Rebalance frequency: for hourly profiles, rebalance every N bars
        # to avoid excessive turnover. Daily profiles rebalance every bar.
        rebalance_every = max(1, self.bars_per_day // 6)  # 4h for hourly, 1 for daily

        dates = self.closes.loc[start:end].index
        for bar_idx, date in enumerate(dates):
            mark_prices = self._prices_on(date, field=MARK_PRICE_FIELD)
            exec_prices = self._prices_on(date, field=EXECUTION_PRICE_FIELD)
            if not mark_prices or not exec_prices:
                continue

            funding = self._funding_on(date)
            self.portfolio.apply_funding(date, mark_prices, funding)

            current_nav = self.portfolio.nav(mark_prices)
            self.risk_manager.update_nav(current_nav)

            # Mark-to-market every bar but only rebalance on schedule
            if bar_idx % rebalance_every != 0:
                self.portfolio.record_nav(date, mark_prices)
                continue

            cb_state = self.risk_manager.apply_circuit_breaker(current_date=date)
            risk_scalar = self.risk_manager.effective_size_scalar(
                correlation_triggered=False,
            )

            nav_series = self.portfolio.nav_series()
            if len(nav_series) > int(self.risk_cfg["vol_regime_window_bars"]):
                self.risk_manager.detect_vol_regime(
                    nav_series,
                    window=int(self.risk_cfg["vol_regime_window_bars"]),
                    periods_per_year=self.periods_per_year,
                )
                corr_triggered, _ = self.risk_manager.check_correlation_watchdog(
                    self.price_data,
                    window=int(self.risk_cfg["correlation_window_bars"]),
                )
                risk_scalar = self.risk_manager.effective_size_scalar(
                    correlation_triggered=corr_triggered,
                )

            if date in target_weights.index:
                row = target_weights.loc[date].to_dict()
            else:
                row = {sym: 0.0 for sym in self.symbols}

            if cb_state == CircuitBreakerState.CASH:
                row = {sym: 0.0 for sym in self.symbols}
            elif risk_scalar != 1.0:
                row = {sym: w * risk_scalar for sym, w in row.items()}

            row = {sym: w for sym, w in row.items() if self.tradeability_mask.loc[date, sym]}

            # For symbols with open positions but no exec price, fall back to
            # mark price so the position can still be closed (emergency exit).
            fill_prices = dict(exec_prices)
            for sym in self.portfolio.positions:
                if sym not in fill_prices and sym in mark_prices:
                    fill_prices[sym] = mark_prices[sym]

            adv = self._avg_daily_volumes(date)
            self.portfolio.rebalance_to_weights(
                date=date,
                prices=fill_prices,
                target_weights=row,
                avg_daily_volumes=adv,
            )
            self.portfolio.record_nav(date, mark_prices)

        return self._compile_results()

    def _compile_results(self) -> Dict:
        nav_series = self.portfolio.nav_series()
        trades_df = self.portfolio.trades_dataframe()
        metrics = compute_all_metrics(
            nav=nav_series,
            trade_pnls=self.portfolio.trade_pnls(),
            total_fees=self.portfolio.total_fees,
            mode=self.mode,
            total_slippage=self.portfolio.total_slippage,
            total_funding=self.portfolio.total_funding,
            turnover=self.portfolio.total_turnover,
            periods_per_year=self.periods_per_year,
        )
        return {
            "metrics": metrics,
            "nav_series": nav_series,
            "trades": trades_df,
            "monthly_pnl": monthly_pnl_table(nav_series),
            "total_fees": self.portfolio.total_fees,
            "total_funding": self.portfolio.total_funding,
            "strategy": self.strategy.name,
            "mode": self.mode,
            "profile": self.profile_name,
        }

    @staticmethod
    def print_results(results: Dict) -> None:
        print_metrics(results["metrics"])
        print("\nMonthly Returns (%):\n")
        print(results["monthly_pnl"].to_string())
