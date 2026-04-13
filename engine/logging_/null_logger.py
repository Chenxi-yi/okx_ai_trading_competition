"""
logging_/null_logger.py
=======================
NullLogger: drop-in replacement for StructuredLogger that discards
all output. Used by BacktestRunner so the TradingAlgorithm pipeline
can run without writing live-trading log files.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class NullLogger:
    """
    Silent logger — all methods accept the same arguments as StructuredLogger
    but do nothing. Lets TradingAlgorithm.rebalance() run in backtest mode
    without side effects.
    """

    def log_rebalance(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_signals(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_performance_csv(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_trade_csv(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_trade(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_risk_check(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_engine_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def write_summary(self, *args: Any, **kwargs: Any) -> None:
        pass
