"""
Parameter space definitions for each strategy sleeve and portfolio weights.

Each space is a dict mapping parameter names to lists of candidate values.
The optimizer does a grid search (or sampled grid) over the Cartesian product.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Dict, List, Sequence

import numpy as np


@dataclass
class ParamSpace:
    """Defines a searchable parameter space for one strategy or the portfolio."""

    name: str
    params: Dict[str, List[Any]]
    defaults: Dict[str, Any] = field(default_factory=dict)

    @property
    def num_combinations(self) -> int:
        if not self.params:
            return 1
        counts = [len(v) for v in self.params.values()]
        result = 1
        for c in counts:
            result *= c
        return result

    def grid(self) -> List[Dict[str, Any]]:
        """Return all combinations as a list of dicts."""
        if not self.params:
            return [{}]
        keys = list(self.params.keys())
        return [dict(zip(keys, vals)) for vals in product(*self.params.values())]

    def sample(self, n: int, seed: int = 42) -> List[Dict[str, Any]]:
        """Random sample n combinations (without replacement if possible)."""
        full = self.grid()
        rng = np.random.default_rng(seed)
        n = min(n, len(full))
        indices = rng.choice(len(full), size=n, replace=False)
        return [full[i] for i in indices]

    def make_config(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """Merge overrides into defaults to produce a full config dict."""
        return {**self.defaults, **overrides}


# ---------------------------------------------------------------------------
# Strategy-specific parameter spaces
# ---------------------------------------------------------------------------

TREND_MOMENTUM_SPACE = ParamSpace(
    name="trend_momentum",
    defaults={
        "preferred_mode": "futures",
        "fast_windows": [20, 30],
        "slow_windows": [100, 150, 200],
        "breakout_windows": [20, 50, 100],
        "momentum_windows": [20, 60, 120],
        "band": 0.005,
        "vol_lookback": 30,
        "target_sleeve_vol": 0.10,
        "rebalance_freq": "1D",
    },
    params={
        "fast_windows": [
            [10, 20],
            [20, 30],
            [15, 25],
            [20, 40],
        ],
        "slow_windows": [
            [80, 120, 200],
            [100, 150, 200],
            [120, 180, 250],
        ],
        "breakout_windows": [
            [20, 50, 100],
            [15, 40, 80],
            [30, 60, 120],
        ],
        "momentum_windows": [
            [20, 60, 120],
            [10, 40, 90],
            [30, 60, 90],
        ],
        "band": [0.0, 0.003, 0.005, 0.01],
        "vol_lookback": [20, 30, 45, 60],
    },
)

CROSS_SECTIONAL_SPACE = ParamSpace(
    name="cross_sectional",
    defaults={
        "preferred_mode": "futures",
        "lookbacks": [7, 30, 90],
        "vol_lookback": 20,
        "funding_lookback": 7,
        "top_n": 2,
        "bottom_n": 2,
        "target_sleeve_vol": 0.10,
        "rebalance_freq": "1D",
        "require_min_universe": 4,
    },
    params={
        "lookbacks": [
            [7, 30, 90],
            [5, 20, 60],
            [14, 45, 120],
            [7, 21, 63],
        ],
        "vol_lookback": [15, 20, 30, 45],
        "top_n": [1, 2, 3],
        "bottom_n": [1, 2, 3],
    },
)

FUNDING_CARRY_SPACE = ParamSpace(
    name="funding_carry",
    defaults={
        "preferred_mode": "futures",
        "funding_lookbacks": [3, 7, 14],
        "trend_veto_lookback": 20,
        "action_threshold": 0.0001,
        "max_abs_funding": 0.005,
        "target_sleeve_vol": 0.06,
        "rebalance_freq": "1D",
    },
    params={
        "funding_lookbacks": [
            [3, 7, 14],
            [5, 10, 21],
            [3, 7, 21],
            [7, 14, 28],
        ],
        "trend_veto_lookback": [10, 20, 30, 45],
        "action_threshold": [0.00005, 0.0001, 0.0002, 0.0005],
        "max_abs_funding": [0.003, 0.005, 0.008],
    },
)

PORTFOLIO_WEIGHTS_SPACE = ParamSpace(
    name="portfolio_weights",
    defaults={
        "trend": 0.50,
        "cross_sectional": 0.35,
        "carry": 0.15,
    },
    params={
        "trend": [0.30, 0.40, 0.50, 0.60],
        "cross_sectional": [0.20, 0.30, 0.35, 0.40],
        "carry": [0.10, 0.15, 0.20, 0.25],
    },
)

HOURLY_TREND_MOMENTUM_SPACE = ParamSpace(
    name="trend_momentum",
    defaults={
        "preferred_mode": "futures",
        "fast_windows": [8, 12, 24],
        "slow_windows": [48, 72, 96],
        "breakout_windows": [24, 48, 72],
        "momentum_windows": [12, 24, 72],
        "band": 0.0015,
        "vol_lookback": 48,
        "target_sleeve_vol": 0.10,
        "rebalance_freq": "1H",
        "periods_per_year": 24 * 365,
        "max_ffill_bars": 2,
    },
    params={
        "fast_windows": [
            [6, 12, 24],
            [8, 12, 24],
            [12, 18, 24],
        ],
        "slow_windows": [
            [36, 48, 72],
            [48, 72, 96],
            [48, 96, 144],
        ],
        "breakout_windows": [
            [12, 24, 48],
            [24, 48, 72],
            [24, 72, 96],
        ],
        "momentum_windows": [
            [6, 24, 48],
            [12, 24, 72],
            [12, 48, 96],
        ],
        "band": [0.0, 0.001, 0.0015, 0.0025],
        "vol_lookback": [24, 36, 48, 72],
    },
)

HOURLY_CROSS_SECTIONAL_SPACE = ParamSpace(
    name="cross_sectional",
    defaults={
        "preferred_mode": "futures",
        "lookbacks": [6, 24, 72],
        "vol_lookback": 48,
        "funding_lookback": 24,
        "top_n": 3,
        "bottom_n": 3,
        "target_sleeve_vol": 0.10,
        "rebalance_freq": "1H",
        "require_min_universe": 6,
        "periods_per_year": 24 * 365,
        "max_ffill_bars": 2,
    },
    params={
        "lookbacks": [
            [4, 12, 48],
            [6, 24, 72],
            [12, 24, 96],
        ],
        "vol_lookback": [24, 36, 48, 72],
        "top_n": [2, 3, 4],
        "bottom_n": [2, 3, 4],
    },
)

HOURLY_FUNDING_CARRY_SPACE = ParamSpace(
    name="funding_carry",
    defaults={
        "preferred_mode": "futures",
        "funding_lookbacks": [8, 24, 72],
        "trend_veto_lookback": 48,
        "action_threshold": 0.00005,
        "max_abs_funding": 0.003,
        "target_sleeve_vol": 0.06,
        "rebalance_freq": "1H",
        "periods_per_year": 24 * 365,
        "max_ffill_bars": 2,
    },
    params={
        "funding_lookbacks": [
            [8, 24, 72],
            [8, 16, 48],
            [8, 24, 96],
        ],
        "trend_veto_lookback": [24, 48, 72],
        "action_threshold": [0.00003, 0.00005, 0.00008, 0.0001],
        "max_abs_funding": [0.002, 0.003, 0.004],
    },
)

HOURLY_PORTFOLIO_WEIGHTS_SPACE = ParamSpace(
    name="portfolio_weights",
    defaults={
        "trend": 0.50,
        "cross_sectional": 0.35,
        "carry": 0.15,
    },
    params={
        "trend": [0.40, 0.50, 0.60],
        "cross_sectional": [0.25, 0.35, 0.45],
        "carry": [0.10, 0.15, 0.20],
    },
)


DAILY_SPACES = {
    "trend_momentum": TREND_MOMENTUM_SPACE,
    "cross_sectional": CROSS_SECTIONAL_SPACE,
    "funding_carry": FUNDING_CARRY_SPACE,
    "portfolio_weights": PORTFOLIO_WEIGHTS_SPACE,
}

HOURLY_SPACES = {
    "trend_momentum": HOURLY_TREND_MOMENTUM_SPACE,
    "cross_sectional": HOURLY_CROSS_SECTIONAL_SPACE,
    "funding_carry": HOURLY_FUNDING_CARRY_SPACE,
    "portfolio_weights": HOURLY_PORTFOLIO_WEIGHTS_SPACE,
}

PROFILE_SPACES = {
    "daily": DAILY_SPACES,
    "hourly": HOURLY_SPACES,
}


def get_param_spaces(profile_name: str = "daily") -> Dict[str, ParamSpace]:
    profile = profile_name.lower()
    aliases = {"1d": "daily", "1h": "hourly"}
    profile = aliases.get(profile, profile)
    if profile not in PROFILE_SPACES:
        raise ValueError(f"Unknown profile '{profile_name}'. Choose from: {sorted(PROFILE_SPACES)}")
    return PROFILE_SPACES[profile]


def get_param_space(target: str, profile_name: str = "daily") -> ParamSpace:
    spaces = get_param_spaces(profile_name)
    if target not in spaces:
        raise ValueError(f"Unknown target '{target}'. Choose from: {list(spaces.keys())}")
    return spaces[target]


ALL_SPACES = DAILY_SPACES
