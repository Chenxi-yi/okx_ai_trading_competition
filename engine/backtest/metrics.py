"""
backtest/metrics.py
===================
Performance metric calculations for backtest results.

All functions accept a pandas Series of daily portfolio values (NAV)
or a Series of daily returns.
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional


TRADING_DAYS_PER_YEAR = 365  # crypto trades 24/7


# ---------------------------------------------------------------------------
# Core return calculations
# ---------------------------------------------------------------------------

def daily_returns(nav: pd.Series) -> pd.Series:
    """Compute simple daily returns from a NAV series."""
    return nav.pct_change().dropna()


def infer_periods_per_year(index: pd.Index, default: int = TRADING_DAYS_PER_YEAR) -> int:
    """Infer bar frequency from a DatetimeIndex for annualisation."""
    if not isinstance(index, pd.DatetimeIndex) or len(index) < 2:
        return default

    deltas = index.to_series().diff().dropna()
    if deltas.empty:
        return default

    median_delta = deltas.median()
    if median_delta <= pd.Timedelta(0):
        return default

    periods = pd.Timedelta(days=365) / median_delta
    return max(int(round(periods)), 1)


def log_returns(nav: pd.Series) -> pd.Series:
    """Compute log daily returns from a NAV series."""
    return np.log(nav / nav.shift(1)).dropna()


# ---------------------------------------------------------------------------
# Return metrics
# ---------------------------------------------------------------------------

def annualised_return(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Geometric annualised return."""
    n = len(returns)
    if n == 0:
        return 0.0
    total = (1 + returns).prod()
    return float(total ** (periods_per_year / n) - 1)


def annualised_volatility(returns: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised standard deviation of returns."""
    return float(returns.std() * np.sqrt(periods_per_year))


# ---------------------------------------------------------------------------
# Risk-adjusted metrics
# ---------------------------------------------------------------------------

def sharpe_ratio(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Annualised Sharpe Ratio."""
    ann_ret = annualised_return(returns, periods_per_year)
    ann_vol = annualised_volatility(returns, periods_per_year)
    if ann_vol == 0:
        return 0.0
    return (ann_ret - risk_free_rate) / ann_vol


def sortino_ratio(
    returns: pd.Series,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Sortino Ratio using downside deviation."""
    ann_ret = annualised_return(returns, periods_per_year)
    downside = returns[returns < 0]
    if len(downside) == 0:
        return np.inf
    downside_std = float(downside.std() * np.sqrt(periods_per_year))
    if downside_std == 0:
        return 0.0
    return (ann_ret - risk_free_rate) / downside_std


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def drawdown_series(nav: pd.Series) -> pd.Series:
    """Return the drawdown series (fraction from running peak)."""
    rolling_peak = nav.cummax()
    return (nav - rolling_peak) / rolling_peak


def max_drawdown(nav: pd.Series) -> float:
    """Maximum drawdown (negative value, e.g. -0.25 = -25%)."""
    return float(drawdown_series(nav).min())


def calmar_ratio(nav: pd.Series, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Calmar Ratio = Annualised Return / abs(Max Drawdown)."""
    ret = annualised_return(daily_returns(nav), periods_per_year)
    mdd = abs(max_drawdown(nav))
    if mdd == 0:
        return np.inf
    return ret / mdd


# ---------------------------------------------------------------------------
# Trade-level metrics
# ---------------------------------------------------------------------------

def win_rate(trade_pnls: pd.Series) -> float:
    """Fraction of trades with positive PnL."""
    if len(trade_pnls) == 0:
        return 0.0
    return float((trade_pnls > 0).mean())


def profit_factor(trade_pnls: pd.Series) -> float:
    """Gross profit / gross loss."""
    gross_profit = trade_pnls[trade_pnls > 0].sum()
    gross_loss   = abs(trade_pnls[trade_pnls < 0].sum())
    if gross_loss == 0:
        return np.inf
    return float(gross_profit / gross_loss)


# ---------------------------------------------------------------------------
# Monthly P&L table
# ---------------------------------------------------------------------------

def monthly_pnl_table(nav: pd.Series) -> pd.DataFrame:
    """
    Build a Year × Month table of monthly returns (%).

    Parameters
    ----------
    nav : pd.Series
        Daily NAV series with DatetimeIndex.

    Returns
    -------
    pd.DataFrame
        Rows = years, columns = month abbreviations, cell values = return (%).
    """
    monthly = nav.resample("ME").last().pct_change().dropna()
    monthly.index = pd.to_datetime(monthly.index)

    table = monthly.groupby([monthly.index.year, monthly.index.month]).first().unstack()
    month_names = {
        1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
        7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
    }
    table.columns = [month_names.get(c, c) for c in table.columns.get_level_values(-1)]
    table = (table * 100).round(2)
    table.index.name = "Year"
    return table


# ---------------------------------------------------------------------------
# Comprehensive summary
# ---------------------------------------------------------------------------

def var_cvar_historical(
    nav: pd.Series,
    confidence: float = 0.95,
) -> tuple:
    """
    Compute 1-day historical Value-at-Risk and Conditional VaR (Expected Shortfall).

    Parameters
    ----------
    nav : pd.Series
        Daily NAV series.
    confidence : float
        Confidence level (default 0.95 → 95%).

    Returns
    -------
    (var_usd, cvar_usd)
        Both expressed as positive USD amounts representing potential losses.
        Returns (0.0, 0.0) if insufficient data.
    """
    if len(nav) < 2:
        return 0.0, 0.0

    current_nav = float(nav.iloc[-1])
    returns = nav.pct_change().dropna()

    if len(returns) == 0:
        return 0.0, 0.0

    alpha = 1.0 - confidence
    var_pct = float(-np.percentile(returns, alpha * 100))
    var_usd = var_pct * current_nav

    tail = returns[returns <= -var_pct]
    cvar_usd = (float(-tail.mean()) * current_nav) if len(tail) > 0 else var_usd

    return var_usd, cvar_usd


def strategy_attribution_table(attribution_df) -> Optional[str]:
    """
    Format the attribution DataFrame as a printable string table.

    Parameters
    ----------
    attribution_df : pd.DataFrame or None
        Output from PortfolioManager.attribution_table().

    Returns
    -------
    str or None
        Formatted table string, or None if no data.
    """
    if attribution_df is None or (hasattr(attribution_df, "empty") and attribution_df.empty):
        return None
    return attribution_df.to_string(index=False)


def compute_all_metrics(
    nav: pd.Series,
    trade_pnls: Optional[pd.Series] = None,
    total_fees: float = 0.0,
    risk_free_rate: float = 0.0,
    mode: str = "spot",
    total_slippage: float = 0.0,
    total_funding: float = 0.0,
    turnover: float = 0.0,
    attribution_df=None,
    periods_per_year: Optional[int] = None,
) -> Dict:
    """
    Compute and return all performance metrics as a dictionary.

    Parameters
    ----------
    nav             : daily NAV series
    trade_pnls      : per-trade PnL series (optional)
    total_fees      : cumulative fees paid (USD)
    risk_free_rate  : annualised risk-free rate
    mode            : trading mode label
    total_slippage  : cumulative slippage paid (USD, separate from fees)
    attribution_df  : pd.DataFrame from PortfolioManager.attribution_table() (optional)
    """
    if nav.empty:
        return {"mode": mode}

    ret = daily_returns(nav)
    periods_per_year = periods_per_year or infer_periods_per_year(nav.index)
    var_usd, cvar_usd = var_cvar_historical(nav)

    metrics = {
        "mode":                      mode,
        "start_date":                str(nav.index[0].date()),
        "end_date":                  str(nav.index[-1].date()),
        "initial_capital":           round(float(nav.iloc[0]), 2),
        "final_nav":                 round(float(nav.iloc[-1]), 2),
        "total_return_pct":          round((nav.iloc[-1] / nav.iloc[0] - 1) * 100, 2),
        "annualised_return_pct":     round(annualised_return(ret, periods_per_year) * 100, 2),
        "annualised_volatility_pct": round(annualised_volatility(ret, periods_per_year) * 100, 2),
        "sharpe_ratio":              round(sharpe_ratio(ret, risk_free_rate, periods_per_year), 4),
        "sortino_ratio":             round(sortino_ratio(ret, risk_free_rate, periods_per_year), 4),
        "max_drawdown_pct":          round(max_drawdown(nav) * 100, 2),
        "calmar_ratio":              round(calmar_ratio(nav, periods_per_year), 4),
        "total_fees_usd":            round(total_fees, 2),
        "total_slippage_usd":        round(total_slippage, 2),
        "total_funding_usd":         round(total_funding, 2),
        "turnover_usd":              round(turnover, 2),
        "var_95_usd":                round(var_usd, 2),
        "cvar_95_usd":               round(cvar_usd, 2),
        "periods_per_year":          periods_per_year,
    }

    if trade_pnls is not None and len(trade_pnls) > 0:
        metrics["num_trades"]    = len(trade_pnls)
        metrics["win_rate_pct"]  = round(win_rate(trade_pnls) * 100, 2)
        metrics["profit_factor"] = round(profit_factor(trade_pnls), 4)

        # Avg slippage per trade in basis points (relative to final NAV)
        if metrics["num_trades"] > 0 and total_slippage > 0 and float(nav.iloc[-1]) > 0:
            avg_slip_bps = (total_slippage / metrics["num_trades"] / float(nav.iloc[-1])) * 10_000
            metrics["avg_slippage_per_trade_bps"] = round(avg_slip_bps, 4)
        else:
            metrics["avg_slippage_per_trade_bps"] = 0.0

    if attribution_df is not None:
        metrics["strategy_attribution"] = attribution_df

    return metrics


def print_metrics(metrics: Dict) -> None:
    """Pretty-print a metrics dictionary."""
    divider = "─" * 55
    print(f"\n{'═' * 55}")
    print(f"  BACKTEST RESULTS  |  Mode: {metrics.get('mode', 'N/A').upper()}")
    print(f"  {metrics.get('start_date')} → {metrics.get('end_date')}")
    print(f"{'═' * 55}")

    fields = [
        ("Initial Capital",           f"${metrics.get('initial_capital', 0):,.2f}"),
        ("Final NAV",                 f"${metrics.get('final_nav', 0):,.2f}"),
        ("Total Return",              f"{metrics.get('total_return_pct', 0):.2f}%"),
        ("Annualised Return",         f"{metrics.get('annualised_return_pct', 0):.2f}%"),
        ("Annualised Volatility",     f"{metrics.get('annualised_volatility_pct', 0):.2f}%"),
        (divider, ""),
        ("Sharpe Ratio",              f"{metrics.get('sharpe_ratio', 0):.4f}"),
        ("Sortino Ratio",             f"{metrics.get('sortino_ratio', 0):.4f}"),
        ("Max Drawdown",              f"{metrics.get('max_drawdown_pct', 0):.2f}%"),
        ("Calmar Ratio",              f"{metrics.get('calmar_ratio', 0):.4f}"),
        (divider, ""),
        ("Total Fees Paid",           f"${metrics.get('total_fees_usd', 0):,.2f}"),
        ("Total Funding",             f"${metrics.get('total_funding_usd', 0):,.2f}"),
        ("Turnover",                  f"${metrics.get('turnover_usd', 0):,.2f}"),
    ]

    if "num_trades" in metrics:
        fields += [
            ("Num Trades",            str(metrics["num_trades"])),
            ("Win Rate",              f"{metrics.get('win_rate_pct', 0):.2f}%"),
            ("Profit Factor",         f"{metrics.get('profit_factor', 0):.4f}"),
        ]

    fields += [
        (divider, ""),
        ("Total Slippage Paid",        f"${metrics.get('total_slippage_usd', 0):,.2f}"),
        ("Avg Slippage/Trade (bps)",   f"{metrics.get('avg_slippage_per_trade_bps', 0):.4f}"),
        ("VaR 95% (1-day)",            f"${metrics.get('var_95_usd', 0):,.2f}"),
        ("CVaR 95% (1-day)",           f"${metrics.get('cvar_95_usd', 0):,.2f}"),
    ]

    for label, value in fields:
        if label == divider:
            print(divider)
        else:
            print(f"  {label:<30} {value:>20}")

    print(f"{'═' * 55}")

    # Strategy attribution table
    if "strategy_attribution" in metrics:
        attr = metrics["strategy_attribution"]
        if attr is not None and (not hasattr(attr, "empty") or not attr.empty):
            print("\n📊 Strategy Attribution:")
            print(attr.to_string(index=False))

    print()
