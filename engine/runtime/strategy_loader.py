"""Load runtime strategies through Strategy Office."""

from __future__ import annotations

import importlib
from typing import Iterable

from contracts import Strategy
from registry import StrategyRegistry


class StrategyLoader:
    def __init__(self, registry: StrategyRegistry | None = None):
        self.registry = registry or StrategyRegistry()

    def load(self, strategy_id: str, allowed_statuses: Iterable[str] = ("paper", "live", "backtest", "research")) -> Strategy:
        record = self.registry.get_strategy(strategy_id)
        if record.status not in set(allowed_statuses):
            raise ValueError(f"Strategy {strategy_id} status {record.status} not allowed")
        params = {}
        if record.default_parameter_set_id:
            params = dict(self.registry.get_parameter_set(record.default_parameter_set_id).params)
        module = importlib.import_module(record.module)
        cls = getattr(module, record.class_name)
        try:
            return cls(strategy_id=record.strategy_id, params=params)
        except TypeError:
            try:
                return cls(params=params)
            except TypeError:
                return cls()
