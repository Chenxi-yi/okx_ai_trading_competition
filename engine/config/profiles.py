"""
Strategy and backtest profiles.

Profiles let the repository support multiple trading cadences without
rewiring the existing daily defaults. The daily profile mirrors the current
settings, while the hourly profile is an additive extension for 1h research.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from config.settings import (
    BACKTEST_END,
    BACKTEST_GUARDS,
    BACKTEST_START,
    CROSS_SECTIONAL_MOMENTUM,
    FUNDING_CARRY,
    MAX_GROSS_LEVERAGE,
    MAX_NET_EXPOSURE,
    MAX_POSITION_PCT,
    PORTFOLIO,
    PORTFOLIO_WEIGHTS,
    TIMEFRAME,
    TREND_MOMENTUM,
    VOL_TARGET,
)


DAILY_PROFILE: Dict[str, Any] = {
    "name": "daily",
    "timeframe": TIMEFRAME,
    "periods_per_year": 365,
    "bars_per_day": 1,
    "backtest_start": BACKTEST_START,
    "backtest_end": BACKTEST_END,
    "max_ffill_bars": int(BACKTEST_GUARDS["max_ffill_days"]),
    "market_data_lookback_days": 300,
    "backtest_guards": deepcopy(BACKTEST_GUARDS),
    "risk": {
        "vol_regime_window_bars": 30,
        "correlation_window_bars": 20,
    },
    "portfolio_weights": deepcopy(PORTFOLIO_WEIGHTS),
    "portfolio": {
        **deepcopy(PORTFOLIO),
        "target_vol": VOL_TARGET,
        "max_gross_leverage": MAX_GROSS_LEVERAGE,
        "max_net_exposure": MAX_NET_EXPOSURE,
        "max_position_pct": MAX_POSITION_PCT,
        "portfolio_leverage": 1.0,
        "max_positions": 10,
        "min_weight_threshold": 0.03,   # ignore signals < 3% weight (no conviction)
    },
    "strategies": {
        "trend": {
            **deepcopy(TREND_MOMENTUM),
            "periods_per_year": 365,
            "max_ffill_bars": int(BACKTEST_GUARDS["max_ffill_days"]),
        },
        "cross_sectional": {
            **deepcopy(CROSS_SECTIONAL_MOMENTUM),
            "periods_per_year": 365,
            "max_ffill_bars": int(BACKTEST_GUARDS["max_ffill_days"]),
        },
        "carry": {
            **deepcopy(FUNDING_CARRY),
            "periods_per_year": 365,
            "max_ffill_bars": int(BACKTEST_GUARDS["max_ffill_days"]),
        },
    },
}


HOURLY_PROFILE: Dict[str, Any] = {
    "name": "hourly",
    "timeframe": "1h",
    "periods_per_year": 24 * 365,
    "bars_per_day": 24,
    "backtest_start": "2024-01-01",
    "backtest_end": BACKTEST_END,
    "max_ffill_bars": 2,
    "market_data_lookback_days": 90,
    "backtest_guards": {
        **deepcopy(BACKTEST_GUARDS),
        "min_history_bars": 24 * 14,
        "max_ffill_days": 2,
        "liquidity_lookback": 24 * 7,
    },
    "risk": {
        "vol_regime_window_bars": 24 * 30,
        "correlation_window_bars": 24 * 7,
    },
    "portfolio_weights": {
        "trend": 0.60,
        "cross_sectional": 0.25,
        "carry": 0.15,
    },
    "portfolio": {
        **deepcopy(PORTFOLIO),
        "target_vol": 0.20,
        "max_gross_leverage": 2.0,
        "max_net_exposure": 1.0,
        "max_position_pct": 0.30,
        "portfolio_leverage": 1.0,
        "max_positions": 8,
        "min_weight_threshold": 0.02,   # ignore signals < 2% weight (no conviction)
        "skip_exchange_reconciliation": True,
        "min_rebalance_notional_usd": 15.0,
    },
    "strategies": {
        "trend": {
            "preferred_mode": "futures",
            "fast_windows": [8, 12, 24],
            "slow_windows": [48, 72, 96],
            "breakout_windows": [24, 48, 72],
            "momentum_windows": [12, 24, 72],
            "band": 0.0015,
            "vol_lookback": 48,
            "target_sleeve_vol": 0.20,
            "rebalance_freq": "1H",
            "periods_per_year": 24 * 365,
            "max_ffill_bars": 2,
        },
        "cross_sectional": {
            "preferred_mode": "futures",
            "lookbacks": [6, 24, 72],
            "vol_lookback": 48,
            "funding_lookback": 24,
            "top_n": 3,
            "bottom_n": 3,
            "target_sleeve_vol": 0.20,
            "rebalance_freq": "1H",
            "require_min_universe": 6,
            "periods_per_year": 24 * 365,
            "max_ffill_bars": 2,
        },
        "carry": {
            "preferred_mode": "futures",
            "funding_lookbacks": [8, 24, 72],
            "trend_veto_lookback": 48,
            "action_threshold": 0.00005,
            "max_abs_funding": 0.003,
            "target_sleeve_vol": 0.12,
            "rebalance_freq": "1H",
            "periods_per_year": 24 * 365,
            "max_ffill_bars": 2,
        },
    },
}


PROFILES: Dict[str, Dict[str, Any]] = {
    "daily": DAILY_PROFILE,
    "hourly": HOURLY_PROFILE,
}


def get_profile(name: str = "daily") -> Dict[str, Any]:
    key = name.lower()
    aliases = {"1d": "daily", "1h": "hourly"}
    key = aliases.get(key, key)
    if key not in PROFILES:
        raise ValueError(f"Unknown profile '{name}'. Choose from: {sorted(PROFILES)}")
    return deepcopy(PROFILES[key])

