from strategies.base import BaseStrategy, StrategyOutput
from strategies.cross_sectional_momentum import CrossSectionalMomentumStrategy
from strategies.funding_carry import FundingCarryStrategy
from strategies.trend_momentum import TrendMomentumStrategy


def __getattr__(name):
    """Lazy import factory to avoid circular import with signals.combiner."""
    if name in ("CombinedPortfolioStrategy", "build_portfolio_strategy", "build_strategy"):
        from strategies.factory import CombinedPortfolioStrategy, build_portfolio_strategy, build_strategy
        return {"CombinedPortfolioStrategy": CombinedPortfolioStrategy,
                "build_portfolio_strategy": build_portfolio_strategy,
                "build_strategy": build_strategy}[name]
    raise AttributeError(f"module 'strategies' has no attribute {name!r}")


__all__ = [
    "BaseStrategy",
    "StrategyOutput",
    "CombinedPortfolioStrategy",
    "build_strategy",
    "build_portfolio_strategy",
    "TrendMomentumStrategy",
    "CrossSectionalMomentumStrategy",
    "FundingCarryStrategy",
]
