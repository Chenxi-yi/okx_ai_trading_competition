"""
core/algorithm.py
=================
TradingAlgorithm: composes the five LEAN-style modules into a
rebalance pipeline.

  Universe → Alpha → PortfolioConstruction → Risk → Execution

Usage (from TradingEngine.rebalance_portfolio):
    algorithm = TradingAlgorithm(
        alpha=CombinedAlphaModel(),
        portfolio_construction=SignalFilteredPortfolioModel(),
        risk=CompositeRiskModel([EngineRiskModel()]),
        execution=MarketOrderExecution(broker),
    )
    result = algorithm.rebalance(portfolio, price_data, profile, mode, slogger)

TradingAlgorithm is stateless — all mutable state lives in Portfolio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from core.alpha import AlphaModel
from core.execution import ExecutionModel
from core.portfolio_construction import PortfolioConstructionModel
from core.risk import CompositeRiskModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RebalanceResult:
    success: bool
    nav_before: float
    nav_after: float
    trades: List[Dict[str, Any]]
    signal_meta: Dict[str, Any]
    risk_summary: Dict[str, Any]
    n_positions: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# TradingAlgorithm
# ---------------------------------------------------------------------------

class TradingAlgorithm:
    """
    Stateless rebalance pipeline.

    Receives:
      - price_data (OHLCV for universe)
      - portfolio  (mutable state — positions, cash, risk_state, …)
      - profile    (configuration — timeframe, weights, leverage limits, …)

    Produces:
      - RebalanceResult

    Also handles all structured logging so TradingEngine.rebalance_portfolio()
    becomes a thin wrapper.
    """

    def __init__(
        self,
        alpha: AlphaModel,
        portfolio_construction: PortfolioConstructionModel,
        risk: CompositeRiskModel,
        execution: ExecutionModel,
    ):
        self.alpha = alpha
        self.portfolio_construction = portfolio_construction
        self.risk = risk
        self.execution = execution

    def rebalance(
        self,
        portfolio,
        price_data: Dict[str, pd.DataFrame],
        profile: Dict[str, Any],
        mode: str,
        slogger,
    ) -> RebalanceResult:
        pid = portfolio.portfolio_id

        # Current prices from the last close of each symbol
        prices = {
            sym: float(df["close"].iloc[-1])
            for sym, df in price_data.items()
            if not df.empty and "close" in df.columns
        }
        nav_before = portfolio.nav(prices)

        try:
            # ---- 1. Alpha ------------------------------------------------
            insights, signal_meta = self.alpha.generate(
                price_data=price_data,
                symbols=list(price_data.keys()),
                mode=mode,
                profile=profile,
            )
            signal_meta["n_raw_insights"] = len(insights)

            # ---- 2. Portfolio construction --------------------------------
            target_weights = self.portfolio_construction.create_targets(
                insights=insights,
                profile=profile,
                mode=mode,
            )
            portfolio.last_target_weights = dict(target_weights)
            signal_meta["final_weights"] = {k: round(v, 6) for k, v in target_weights.items()}
            signal_meta["n_final_positions"] = len(target_weights)
            signal_meta["position_decisions"] = self._build_position_decisions(target_weights, signal_meta)

            # ---- 3. Risk -------------------------------------------------
            adjusted_weights, risk_summary = self.risk.adjust(
                weights=target_weights,
                portfolio=portfolio,
                price_data=price_data,
                prices=prices,
                profile=profile,
            )

            # ---- 4. Execution --------------------------------------------
            current_nav = portfolio.nav(prices)
            trades = self.execution.execute(
                portfolio=portfolio,
                target_weights=adjusted_weights,
                prices=prices,
                nav=current_nav,
                profile=profile,
                mode=mode,
            )

            # ---- 5. Update portfolio state -------------------------------
            portfolio.last_rebalance = datetime.now(timezone.utc)
            nav_after = portfolio.nav(prices)
            portfolio.nav_history.append({
                "date": datetime.now(timezone.utc).isoformat(),
                "nav": nav_after,
            })
            if nav_after > portfolio.risk_state.get("peak_nav", portfolio.initial_capital):
                portfolio.risk_state["peak_nav"] = nav_after

            # ---- 6. Logging ----------------------------------------------
            snapshot = portfolio.snapshot(prices)
            slogger.log_rebalance(pid, snapshot, trades)
            slogger.log_signals(pid, signal_meta, risk_summary)
            slogger.log_performance_csv(pid, snapshot, len(trades))
            for trade in trades:
                reason = signal_meta.get("position_decisions", {}).get(trade.get("symbol", ""), "")
                slogger.log_trade_csv(pid, trade, reason)

            return RebalanceResult(
                success=True,
                nav_before=nav_before,
                nav_after=nav_after,
                trades=trades,
                signal_meta=signal_meta,
                risk_summary=risk_summary,
                n_positions=signal_meta.get("n_final_positions", 0),
            )

        except Exception as e:
            logger.error("[%s] Algorithm rebalance failed: %s", pid, e, exc_info=True)
            return RebalanceResult(
                success=False,
                nav_before=nav_before,
                nav_after=nav_before,
                trades=[],
                signal_meta={},
                risk_summary={},
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_position_decisions(
        final_weights: Dict[str, float],
        signal_meta: Dict[str, Any],
    ) -> Dict[str, str]:
        """Human-readable reason for each final position (for trade logs)."""
        decisions = {}
        sleeves = signal_meta.get("sleeves", {})

        for sym, weight in final_weights.items():
            direction = "LONG" if weight > 0 else "SHORT"
            reasons = []
            for sleeve_name, sleeve_data in sleeves.items():
                sw = sleeve_data.get("all_weights", {}).get(sym, 0.0)
                if abs(sw) > 0.01:
                    sleeve_pct = sleeve_data.get("portfolio_weight", 0) * 100
                    side = "long" if sw > 0 else "short"
                    reasons.append(f"{sleeve_name}({sleeve_pct:.0f}%): {side} {abs(sw):.3f}")
            reason = f"{direction} {abs(weight):.3f} — " + ", ".join(reasons) if reasons else f"{direction} {abs(weight):.3f}"
            decisions[sym] = reason

        return decisions
