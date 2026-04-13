"""
competition/capital_optimizer.py
=================================
Capital top-up decision logic for the OKX AI Skills Competition.

The competition ROI formula is:
    ROI = AI_net_pnl / (initial_nav + cumulative_deposits) * 100%

Every top-up adds to the denominator, which dilutes ROI.
Only top up if the expected return on new capital exceeds the dilution cost.

Usage:
    from competition.capital_optimizer import TopUpAdvisor
    from competition.registry import CompetitionRegistry

    advisor = TopUpAdvisor(registry=CompetitionRegistry())

    # Get recommendation
    rec = advisor.evaluate(
        strategy_id="elite_flow",
        current_pnl=25.0,          # USD PnL so far
        days_elapsed=5,
        days_remaining=9,
        candidate_topup=200.0,     # how much you're considering adding
    )
    print(rec)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from competition.registry import CompetitionRegistry


@dataclass
class TopUpRecommendation:
    strategy_id:         str
    recommended:         bool
    recommended_amount:  float          # USDT — 0 if not recommended
    current_capital:     float
    seed_capital:        float
    current_roi:         float          # % — current ROI so far
    roi_after_topup:     float          # % — projected ROI if topped up
    roi_delta:           float          # roi_after_topup - current_roi (negative = dilution)
    reason:              str

    def __str__(self) -> str:
        sign = "+" if self.roi_delta >= 0 else ""
        verdict = "✅ RECOMMEND TOP-UP" if self.recommended else "❌ DO NOT TOP-UP"
        return (
            f"\n{verdict}: {self.strategy_id}\n"
            f"  Current capital : ${self.current_capital:.0f} USDT (seed ${self.seed_capital:.0f})\n"
            f"  Current ROI     : {self.current_roi:+.2f}%\n"
            f"  ROI after top-up: {self.roi_after_topup:+.2f}% ({sign}{self.roi_delta:.2f}%)\n"
            f"  Recommended amt : ${self.recommended_amount:.0f} USDT\n"
            f"  Reason          : {self.reason}\n"
        )


class TopUpAdvisor:
    """
    Advises on capital top-up decisions during the competition.

    Parameters
    ----------
    registry          : CompetitionRegistry instance
    min_sharpe        : Minimum backtest Sharpe ratio for strategy to be eligible
    min_roi_delta     : Minimum ROI improvement required to justify top-up (default 0 = must not worsen)
    """

    def __init__(
        self,
        registry: Optional[CompetitionRegistry] = None,
        min_sharpe: float = 0.5,
        min_roi_delta: float = 0.0,
    ):
        self.registry      = registry or CompetitionRegistry()
        self.min_sharpe    = min_sharpe
        self.min_roi_delta = min_roi_delta

    def evaluate(
        self,
        strategy_id: str,
        current_pnl: float,
        days_elapsed: int,
        days_remaining: int,
        candidate_topup: float,
        backtest_sharpe: float = 0.0,
        backtest_daily_return: Optional[float] = None,
    ) -> TopUpRecommendation:
        """
        Evaluate whether to top up capital for a strategy.

        Parameters
        ----------
        current_pnl             : Realised + unrealised PnL so far (USD)
        days_elapsed            : Competition days already run
        days_remaining          : Days left in competition
        candidate_topup         : Amount considering adding (USD)
        backtest_sharpe         : Strategy's backtested Sharpe ratio
        backtest_daily_return   : Strategy's backtested avg daily return (fraction).
                                  If None, estimated from current_pnl / days_elapsed.
        """
        current_capital = self.registry.current_capital(strategy_id)
        seed_capital    = self.registry.seed_capital(strategy_id)

        # Current ROI
        current_roi = (current_pnl / current_capital * 100) if current_capital > 0 else 0.0

        # Estimate expected daily return
        if backtest_daily_return is not None:
            daily_return = backtest_daily_return
        elif days_elapsed > 0 and current_capital > 0:
            daily_return = (current_pnl / current_capital) / days_elapsed
        else:
            daily_return = 0.0

        # Project PnL if we add capital now
        expected_topup_pnl   = candidate_topup * daily_return * days_remaining
        projected_total_pnl  = current_pnl + expected_topup_pnl
        new_denominator      = current_capital + candidate_topup
        roi_after_topup      = (projected_total_pnl / new_denominator * 100) if new_denominator > 0 else 0.0
        roi_delta            = roi_after_topup - current_roi

        # Decision logic
        if days_remaining <= 0:
            return TopUpRecommendation(
                strategy_id=strategy_id, recommended=False, recommended_amount=0,
                current_capital=current_capital, seed_capital=seed_capital,
                current_roi=current_roi, roi_after_topup=roi_after_topup,
                roi_delta=roi_delta,
                reason="Competition ending — no time to generate returns on new capital.",
            )

        if backtest_sharpe > 0 and backtest_sharpe < self.min_sharpe:
            return TopUpRecommendation(
                strategy_id=strategy_id, recommended=False, recommended_amount=0,
                current_capital=current_capital, seed_capital=seed_capital,
                current_roi=current_roi, roi_after_topup=roi_after_topup,
                roi_delta=roi_delta,
                reason=f"Strategy Sharpe {backtest_sharpe:.2f} < minimum {self.min_sharpe}. Risk too high.",
            )

        if roi_delta < self.min_roi_delta:
            return TopUpRecommendation(
                strategy_id=strategy_id, recommended=False, recommended_amount=0,
                current_capital=current_capital, seed_capital=seed_capital,
                current_roi=current_roi, roi_after_topup=roi_after_topup,
                roi_delta=roi_delta,
                reason=(
                    f"Top-up would reduce ROI by {abs(roi_delta):.2f}pp "
                    f"({current_roi:+.2f}% → {roi_after_topup:+.2f}%). "
                    f"Expected daily return ({daily_return*100:.3f}%/day × {days_remaining}d) "
                    f"does not offset denominator dilution."
                ),
            )

        return TopUpRecommendation(
            strategy_id=strategy_id, recommended=True,
            recommended_amount=candidate_topup,
            current_capital=current_capital, seed_capital=seed_capital,
            current_roi=current_roi, roi_after_topup=roi_after_topup,
            roi_delta=roi_delta,
            reason=(
                f"Top-up improves ROI by {roi_delta:+.2f}pp "
                f"({current_roi:+.2f}% → {roi_after_topup:+.2f}%). "
                f"Expected {expected_topup_pnl:+.2f} USDT from ${candidate_topup:.0f} over {days_remaining} days."
            ),
        )

    def optimal_amount(
        self,
        strategy_id: str,
        current_pnl: float,
        days_elapsed: int,
        days_remaining: int,
        available_capital: float,
        backtest_daily_return: float,
        step: float = 50.0,
    ) -> float:
        """
        Find the optimal top-up amount by evaluating increments up to available_capital.
        Returns the largest amount that still improves ROI.
        """
        best_amount = 0.0
        best_roi_delta = self.min_roi_delta

        candidate = step
        while candidate <= available_capital:
            rec = self.evaluate(
                strategy_id=strategy_id,
                current_pnl=current_pnl,
                days_elapsed=days_elapsed,
                days_remaining=days_remaining,
                candidate_topup=candidate,
                backtest_daily_return=backtest_daily_return,
            )
            if rec.recommended and rec.roi_delta > best_roi_delta:
                best_roi_delta = rec.roi_delta
                best_amount = candidate
            candidate += step

        return best_amount
