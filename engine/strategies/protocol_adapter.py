"""Adapters from legacy target-weight strategies to the new Signal protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

import pandas as pd

from contracts import MarketState, PortfolioState, Signal, StrategyContext, StrategySpec
from strategies.base import BaseStrategy


@dataclass
class TargetWeightStrategyAdapter:
    """Wraps an existing `BaseStrategy` so it emits protocol `Signal` objects."""

    legacy: BaseStrategy
    spec: StrategySpec
    target_pct: float = 0.03
    adverse_pct: float = 0.015
    horizon_sec: int = 24 * 3600

    @property
    def strategy_id(self) -> str:
        return self.spec.strategy_id

    def generate(self, context: StrategyContext) -> list[Signal]:
        price_data = self._price_data_from_market(context.market)
        if not price_data:
            return []
        output = self.legacy.generate(price_data, mode="futures")
        if output.target_weights.empty:
            return []
        weights = output.target_weights.iloc[-1].dropna()
        confidence = output.confidence.iloc[-1] if not output.confidence.empty else weights.abs()
        now = context.market.timestamp or datetime.now(timezone.utc)
        signals: list[Signal] = []
        for symbol, weight in weights.items():
            weight = float(weight)
            if abs(weight) < 1e-8:
                continue
            entry = self._last_close(price_data.get(symbol))
            if entry is None or entry <= 0:
                continue
            side = "long" if weight > 0 else "short"
            target = entry * (1.0 + self.target_pct if side == "long" else 1.0 - self.target_pct)
            stop = entry * (1.0 - self.adverse_pct if side == "long" else 1.0 + self.adverse_pct)
            conf = float(confidence.get(symbol, min(abs(weight), 1.0))) if hasattr(confidence, "get") else 0.5
            signals.append(
                Signal(
                    strategy_id=self.strategy_id,
                    symbol=str(symbol),
                    side=side,
                    timestamp=now,
                    entry=entry,
                    target=target,
                    stop=stop,
                    horizon_sec=self.horizon_sec,
                    p_target=max(0.01, min(0.99, conf)),
                    adverse_pct_estimate=self.adverse_pct,
                    confidence=max(0.0, min(1.0, conf)),
                    metadata={"legacy_weight": weight, "adapter": "target_weight_v1"},
                )
            )
        return signals

    @staticmethod
    def _price_data_from_market(market: MarketState) -> Mapping[str, pd.DataFrame]:
        return market.ohlcv  # type: ignore[return-value]

    @staticmethod
    def _last_close(df: pd.DataFrame | None) -> float | None:
        if df is None or df.empty or "close" not in df.columns:
            return None
        return float(pd.to_numeric(df["close"], errors="coerce").dropna().iloc[-1])
