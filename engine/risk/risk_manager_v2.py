"""
risk/risk_manager_v2.py
=======================
Enhanced dynamic risk management system for the quantitative trading framework.

Supersedes the fixed stop-loss in risk_manager.py with:
  A. ATR-based dynamic stop-loss
  B. Portfolio drawdown circuit breaker
  C. Volatility regime detection (LOW / MEDIUM / HIGH)
  D. Correlation watchdog
  E. VaR / CVaR (historical, 95%)
  F. Fees & slippage model (OKX USDT-M swaps)

All thresholds and parameters are configurable via config/settings.py.
"""

import logging
import math
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from config.settings import (
    ATR_MULTIPLIER,
    CORRELATION_THRESHOLD,
    DRAWDOWN_CIRCUIT_BREAKER_1,
    DRAWDOWN_CIRCUIT_BREAKER_2,
    HIGH_VOL_PERCENTILE,
    INITIAL_CAPITAL,
    LEVERAGE_LIMITS,
    LOW_VOL_PERCENTILE,
    MAKER_FEE,
    SLIPPAGE_FACTOR,
    TAKER_FEE,
    TRADING_MODE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class VolRegime(str, Enum):
    """Volatility regime classification."""
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


class CircuitBreakerState(str, Enum):
    """Portfolio drawdown circuit breaker state."""
    NORMAL   = "NORMAL"     # no action required
    REDUCED  = "REDUCED"    # drawdown_1 breached → 50% position reduction
    CASH     = "CASH"       # drawdown_2 breached → all positions closed


# ---------------------------------------------------------------------------
# RiskManagerV2
# ---------------------------------------------------------------------------

class RiskManagerV2:
    """
    Dynamic risk manager with ATR stops, circuit breaker, vol-regime scaling,
    correlation watchdog, VaR/CVaR reporting, and a fee+slippage model.

    Parameters
    ----------
    mode : str
        Trading mode: "spot" | "futures" | "margin".
    initial_capital : float
        Starting NAV in USD.
    max_position_pct : float
        Maximum notional per position as fraction of NAV (before any scaling).
    atr_multiplier : float
        Multiplier applied to ATR to compute stop distance.
    drawdown_threshold_1 : float
        First circuit-breaker level (e.g. 0.15 → 15% drawdown from peak).
    drawdown_threshold_2 : float
        Second circuit-breaker level (e.g. 0.25 → 25% drawdown from peak).
    high_vol_pct : int
        Percentile above which volatility is classified as HIGH.
    low_vol_pct : int
        Percentile below which volatility is classified as LOW.
    correlation_threshold : float
        Average pairwise correlation above which positions are cut 40%.
    taker_fee : float
        Taker fee rate (e.g. 0.0004 for Binance USDT-M futures).
    maker_fee : float
        Maker fee rate.
    slippage_factor : float
        Square-root market impact coefficient.
    """

    def __init__(
        self,
        mode: str = TRADING_MODE,
        initial_capital: float = INITIAL_CAPITAL,
        max_position_pct: float = 0.20,
        atr_multiplier: float = ATR_MULTIPLIER,
        drawdown_threshold_1: float = DRAWDOWN_CIRCUIT_BREAKER_1,
        drawdown_threshold_2: float = DRAWDOWN_CIRCUIT_BREAKER_2,
        high_vol_pct: int = HIGH_VOL_PERCENTILE,
        low_vol_pct: int = LOW_VOL_PERCENTILE,
        correlation_threshold: float = CORRELATION_THRESHOLD,
        taker_fee: float = TAKER_FEE,
        maker_fee: float = MAKER_FEE,
        slippage_factor: float = SLIPPAGE_FACTOR,
    ):
        if mode not in LEVERAGE_LIMITS:
            raise ValueError(f"Unknown mode '{mode}'. Choose: spot | futures | margin")

        self.mode = mode
        self.nav = initial_capital
        self.max_position_pct = max_position_pct
        self.atr_multiplier = atr_multiplier
        self.drawdown_threshold_1 = drawdown_threshold_1
        self.drawdown_threshold_2 = drawdown_threshold_2
        self.high_vol_pct = high_vol_pct
        self.low_vol_pct = low_vol_pct
        self.correlation_threshold = correlation_threshold
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.slippage_factor = slippage_factor

        # High-water mark tracking
        self._peak_nav: float = initial_capital
        self._circuit_breaker_state: CircuitBreakerState = CircuitBreakerState.NORMAL
        self._circuit_breaker_cash_since: Optional[pd.Timestamp] = None
        self._circuit_breaker_cooldown_days: int = 90  # reset after 90 days in CASH

        # Accumulated metrics
        self.total_slippage_paid: float = 0.0
        self.total_fees_paid: float = 0.0
        self.num_fills: int = 0

        # Current vol-regime (updated via detect_vol_regime())
        self._vol_regime: VolRegime = VolRegime.MEDIUM

        logger.info(
            "RiskManagerV2 initialised | mode=%s | capital=$%.2f | ATR_mult=%.1f",
            mode, initial_capital, atr_multiplier,
        )

    # ------------------------------------------------------------------
    # A. ATR-based Dynamic Stop-Loss
    # ------------------------------------------------------------------

    @staticmethod
    def compute_atr(ohlcv: pd.DataFrame, window: int = 14) -> pd.Series:
        """
        Compute the Average True Range (ATR) over *window* periods.

        Parameters
        ----------
        ohlcv : pd.DataFrame
            Must contain columns: 'high', 'low', 'close'.
        window : int
            Rolling window in periods (default 14).

        Returns
        -------
        pd.Series
            ATR series, same index as *ohlcv*.
        """
        high  = ohlcv["high"]
        low   = ohlcv["low"]
        close = ohlcv["close"]

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low  - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        return tr.rolling(window, min_periods=1).mean()

    def compute_stop_prices(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        ohlcv: pd.DataFrame,
        atr_window: int = 14,
        atr_multiplier: Optional[float] = None,
    ) -> Tuple[float, Optional[float]]:
        """
        Compute ATR-based dynamic stop price for a position.

        Parameters
        ----------
        symbol : str
            Asset symbol (for logging).
        side : str
            "long" or "short".
        entry_price : float
            Price at which the position was opened.
        ohlcv : pd.DataFrame
            OHLCV data for the asset (must include 'high', 'low', 'close').
        atr_window : int
            ATR lookback period.
        atr_multiplier : float, optional
            Override the instance-level ATR multiplier.

        Returns
        -------
        (stop_price, atr_value)
            stop_price : float  — price level that triggers the stop.
            atr_value  : float  — current ATR value (last bar).
        """
        mult = atr_multiplier if atr_multiplier is not None else self.atr_multiplier

        # Widen ATR in HIGH vol regime
        if self._vol_regime == VolRegime.HIGH:
            mult = max(mult, 3.0)

        atr_series = self.compute_atr(ohlcv, window=atr_window)
        current_atr = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0

        stop_distance = mult * current_atr

        if side == "long":
            stop_price = entry_price - stop_distance
        else:
            stop_price = entry_price + stop_distance

        logger.debug(
            "ATR stop | %s %s | entry=%.4f | ATR=%.4f | mult=%.1f | stop=%.4f",
            side.upper(), symbol, entry_price, current_atr, mult, stop_price,
        )
        return stop_price, current_atr

    def check_stops(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        current_price: float,
        stop_price: float,
    ) -> bool:
        """
        Check whether current_price has breached the ATR-based stop.

        Parameters
        ----------
        symbol : str
            Asset symbol.
        side : str
            "long" or "short".
        entry_price : float
            Original entry price.
        current_price : float
            Latest market price.
        stop_price : float
            Pre-computed stop level (from compute_stop_prices).

        Returns
        -------
        bool
            True if the stop has been triggered (caller should close position).
        """
        triggered = (
            (side == "long"  and current_price <= stop_price) or
            (side == "short" and current_price >= stop_price)
        )
        if triggered:
            pnl_pct = (
                (current_price - entry_price) / entry_price if side == "long"
                else (entry_price - current_price) / entry_price
            )
            logger.info(
                "🛑 ATR stop triggered | %s %s | entry=%.4f | stop=%.4f | current=%.4f | pnl=%.2f%%",
                side.upper(), symbol, entry_price, stop_price, current_price, pnl_pct * 100,
            )
        return triggered

    # ------------------------------------------------------------------
    # B. Portfolio Drawdown Circuit Breaker
    # ------------------------------------------------------------------

    def update_nav(self, nav: float) -> None:
        """
        Sync the current NAV and update the high-water mark.

        Call once per bar after marking positions to market.

        Parameters
        ----------
        nav : float
            Current portfolio NAV in USD.
        """
        self.nav = nav
        if nav > self._peak_nav:
            self._peak_nav = nav

    def apply_circuit_breaker(self, current_date: Optional[pd.Timestamp] = None) -> CircuitBreakerState:
        """
        Evaluate the drawdown circuit breaker and return the required action.

        The circuit breaker resets after a 90-day cooldown period in CASH state,
        or when NAV recovers to within 5% of the peak (whichever comes first).
        This prevents the circuit breaker from permanently locking the portfolio
        in cash after large drawdowns (common in crypto).

        Returns
        -------
        CircuitBreakerState
            NORMAL   – no action required.
            REDUCED  – cut all position sizes by 50% on next rebalance.
            CASH     – close all positions immediately, go to cash.

        Side effects
        ------------
        Updates self._circuit_breaker_state.
        """
        if self._peak_nav <= 0:
            return CircuitBreakerState.NORMAL

        drawdown = (self._peak_nav - self.nav) / self._peak_nav

        # --- Time-based reset: if in CASH for > cooldown_days, resume trading ---
        if (
            self._circuit_breaker_state == CircuitBreakerState.CASH
            and current_date is not None
            and self._circuit_breaker_cash_since is not None
        ):
            days_in_cash = (current_date - self._circuit_breaker_cash_since).days
            if days_in_cash >= self._circuit_breaker_cooldown_days:
                logger.info(
                    "✅ Circuit breaker RESET (cooldown) | %d days in cash | "
                    "Resetting peak from $%.2f → $%.2f",
                    days_in_cash, self._peak_nav, self.nav,
                )
                self._circuit_breaker_state = CircuitBreakerState.NORMAL
                self._circuit_breaker_cash_since = None
                self._peak_nav = self.nav  # reset peak to current level
                return self._circuit_breaker_state

        # --- Price-based reset: NAV within 5% of peak ---
        if drawdown <= 0.05 and self._circuit_breaker_state != CircuitBreakerState.NORMAL:
            logger.info(
                "✅ Circuit breaker RESET (recovery) | NAV=$%.2f | peak=$%.2f",
                self.nav, self._peak_nav,
            )
            self._circuit_breaker_state = CircuitBreakerState.NORMAL
            self._circuit_breaker_cash_since = None

        # --- Evaluate thresholds ---
        if drawdown >= self.drawdown_threshold_2:
            if self._circuit_breaker_state != CircuitBreakerState.CASH:
                logger.warning(
                    "🚨 CIRCUIT BREAKER CASH | drawdown=%.1f%% ≥ threshold=%.0f%% | "
                    "Closing all positions!",
                    drawdown * 100, self.drawdown_threshold_2 * 100,
                )
                self._circuit_breaker_cash_since = current_date
            self._circuit_breaker_state = CircuitBreakerState.CASH

        elif drawdown >= self.drawdown_threshold_1:
            if self._circuit_breaker_state == CircuitBreakerState.NORMAL:
                logger.warning(
                    "⚠️  CIRCUIT BREAKER REDUCED | drawdown=%.1f%% ≥ threshold=%.0f%% | "
                    "Halving position sizes on next rebalance.",
                    drawdown * 100, self.drawdown_threshold_1 * 100,
                )
            self._circuit_breaker_state = CircuitBreakerState.REDUCED

        return self._circuit_breaker_state

    @property
    def circuit_breaker_state(self) -> CircuitBreakerState:
        """Current circuit breaker state."""
        return self._circuit_breaker_state

    def circuit_breaker_size_scalar(self) -> float:
        """
        Return the position-size scalar implied by the circuit breaker:
          NORMAL  → 1.0
          REDUCED → 0.5
          CASH    → 0.0
        """
        mapping = {
            CircuitBreakerState.NORMAL:  1.0,
            CircuitBreakerState.REDUCED: 0.5,
            CircuitBreakerState.CASH:    0.0,
        }
        return mapping[self._circuit_breaker_state]

    # ------------------------------------------------------------------
    # C. Volatility Regime Detection
    # ------------------------------------------------------------------

    def detect_vol_regime(
        self,
        nav_series: pd.Series,
        window: int = 30,
        periods_per_year: int = 365,
    ) -> VolRegime:
        """
        Classify the current volatility regime from the NAV series.

        Uses a rolling realised volatility (annualised) and classifies
        into LOW / MEDIUM / HIGH using historical percentile thresholds.

        Parameters
        ----------
        nav_series : pd.Series
            NAV values (DatetimeIndex).
        window : int
            Rolling window for realised vol (default 30 bars).
        periods_per_year : int
            Annualization factor (365 for daily, 8760 for hourly).

        Returns
        -------
        VolRegime
            The current regime (also stored in self._vol_regime).
        """
        if len(nav_series) < window + 1:
            self._vol_regime = VolRegime.MEDIUM
            return self._vol_regime

        daily_ret = nav_series.pct_change().dropna()
        rolling_vol = daily_ret.rolling(window, min_periods=window // 2).std() * math.sqrt(periods_per_year)

        current_vol = float(rolling_vol.iloc[-1])
        low_thresh  = float(np.nanpercentile(rolling_vol.dropna(), self.low_vol_pct))
        high_thresh = float(np.nanpercentile(rolling_vol.dropna(), self.high_vol_pct))

        # Hysteresis buffer (5%) prevents flapping on bar-to-bar noise
        hyst = 0.05
        if self._vol_regime == VolRegime.LOW:
            # Must exceed low_thresh by buffer to leave LOW
            if current_vol > high_thresh:
                regime = VolRegime.HIGH
            elif current_vol > low_thresh * (1 + hyst):
                regime = VolRegime.MEDIUM
            else:
                regime = VolRegime.LOW
        elif self._vol_regime == VolRegime.HIGH:
            # Must drop below high_thresh by buffer to leave HIGH
            if current_vol < low_thresh:
                regime = VolRegime.LOW
            elif current_vol < high_thresh * (1 - hyst):
                regime = VolRegime.MEDIUM
            else:
                regime = VolRegime.HIGH
        else:  # MEDIUM
            if current_vol < low_thresh:
                regime = VolRegime.LOW
            elif current_vol > high_thresh:
                regime = VolRegime.HIGH
            else:
                regime = VolRegime.MEDIUM

        if regime != self._vol_regime:
            logger.info(
                "📊 Vol regime change: %s → %s | current_vol=%.2f%% | "
                "low_thresh=%.2f%% | high_thresh=%.2f%%",
                self._vol_regime.value, regime.value,
                current_vol * 100, low_thresh * 100, high_thresh * 100,
            )
        self._vol_regime = regime
        return regime

    def vol_regime_size_scalar(self) -> float:
        """
        Return the position-size scalar implied by the current vol regime:
          LOW    → 1.25 (allow modest upsize)
          MEDIUM → 1.0  (no change)
          HIGH   → 0.5  (halve positions)
        """
        mapping = {
            VolRegime.LOW:    1.25,
            VolRegime.MEDIUM: 1.0,
            VolRegime.HIGH:   0.5,
        }
        return mapping[self._vol_regime]

    @property
    def vol_regime(self) -> VolRegime:
        """Current volatility regime."""
        return self._vol_regime

    # ------------------------------------------------------------------
    # D. Correlation Watchdog
    # ------------------------------------------------------------------

    def check_correlation_watchdog(
        self,
        price_data: Dict[str, pd.DataFrame],
        window: int = 20,
    ) -> Tuple[bool, float]:
        """
        Compute the rolling 20-day average pairwise correlation across all assets.
        If the average exceeds the threshold, log a WARNING and return True.

        Parameters
        ----------
        price_data : dict[str, pd.DataFrame]
            {symbol: OHLCV DataFrame} with a 'close' column.
        window : int
            Rolling window for correlation (default 20 days).

        Returns
        -------
        (triggered, avg_corr)
            triggered : bool   — True if systemic risk threshold was exceeded.
            avg_corr  : float  — current average pairwise correlation.
        """
        if len(price_data) < 2:
            return False, 0.0

        closes = pd.DataFrame(
            {sym: df["close"] for sym, df in price_data.items()}
        ).ffill()

        if len(closes) < window:
            return False, 0.0

        recent = closes.iloc[-window:]
        returns = recent.pct_change().dropna()

        if returns.shape[0] < 2:
            return False, 0.0

        corr_matrix = returns.corr()
        # Extract upper triangle (excluding diagonal)
        n = len(corr_matrix)
        upper_vals = [
            corr_matrix.iloc[i, j]
            for i in range(n)
            for j in range(i + 1, n)
            if not math.isnan(corr_matrix.iloc[i, j])
        ]

        if not upper_vals:
            return False, 0.0

        avg_corr = float(np.mean(upper_vals))
        triggered = avg_corr > self.correlation_threshold

        if triggered:
            logger.warning(
                "🔗 CORRELATION WATCHDOG | avg_pairwise_corr=%.3f > threshold=%.2f | "
                "Reducing all positions by 40%%.",
                avg_corr, self.correlation_threshold,
            )

        return triggered, avg_corr

    # ------------------------------------------------------------------
    # E. VaR / CVaR (Historical)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_var_cvar(
        nav_series: pd.Series,
        confidence: float = 0.95,
    ) -> Tuple[float, float]:
        """
        Compute 1-day historical Value-at-Risk and Conditional VaR at *confidence* level.

        Parameters
        ----------
        nav_series : pd.Series
            Daily NAV series (at least 30 bars recommended).
        confidence : float
            Confidence level (default 0.95 → 95% VaR).

        Returns
        -------
        (VaR_usd, CVaR_usd)
            Both expressed as positive USD dollar amounts.
            Returns (0.0, 0.0) if insufficient data.
        """
        if len(nav_series) < 2:
            return 0.0, 0.0

        current_nav = float(nav_series.iloc[-1])
        portfolio_returns = nav_series.pct_change().dropna()

        if len(portfolio_returns) == 0:
            return 0.0, 0.0

        alpha = 1.0 - confidence
        var_pct = float(-np.percentile(portfolio_returns, alpha * 100))
        var_usd = var_pct * current_nav

        tail_returns = portfolio_returns[portfolio_returns <= -var_pct]
        if len(tail_returns) == 0:
            cvar_usd = var_usd  # fallback: CVaR = VaR
        else:
            cvar_usd = float(-tail_returns.mean()) * current_nav

        return var_usd, cvar_usd

    # ------------------------------------------------------------------
    # F. Fees & Slippage Model
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_avg_daily_volume_usd(
        ohlcv: pd.DataFrame,
        window: int = 30,
    ) -> float:
        """
        Estimate the average daily trading volume in USD using a rolling window.

        avg_daily_volume_usd = rolling_mean(volume × close, window)

        Parameters
        ----------
        ohlcv : pd.DataFrame
            OHLCV data with 'volume' and 'close' columns.
        window : int
            Rolling window in days (default 30).

        Returns
        -------
        float
            Estimated average daily volume in USD.
        """
        vol_usd = ohlcv["volume"] * ohlcv["close"]
        return float(vol_usd.rolling(window, min_periods=1).mean().iloc[-1])

    def compute_slippage_pct(
        self,
        order_size_usd: float,
        avg_daily_volume_usd: float,
    ) -> float:
        """
        Compute estimated slippage percentage using a square-root market impact model.

        slippage_pct = slippage_factor × sqrt(order_size_usd / avg_daily_volume_usd)

        Parameters
        ----------
        order_size_usd : float
            Notional value of the order in USD.
        avg_daily_volume_usd : float
            Estimated average daily trading volume in USD.

        Returns
        -------
        float
            Slippage as a fraction (e.g. 0.0003 = 0.03%).
        """
        if avg_daily_volume_usd <= 0:
            return 0.0
        ratio = order_size_usd / avg_daily_volume_usd
        return self.slippage_factor * math.sqrt(ratio)

    def compute_execution_cost(
        self,
        order_size_usd: float,
        avg_daily_volume_usd: float,
        use_taker: bool = True,
    ) -> Tuple[float, float, float]:
        """
        Compute total execution cost for a single trade.

        execution_cost = (fee + slippage_pct) × notional_value

        Parameters
        ----------
        order_size_usd : float
            Notional value of the trade in USD.
        avg_daily_volume_usd : float
            Estimated average daily trading volume in USD.
        use_taker : bool
            Use taker fee (True) or maker fee (False).

        Returns
        -------
        (execution_cost_usd, fee_usd, slippage_usd)
        """
        fee_rate    = self.taker_fee if use_taker else self.maker_fee
        slip_pct    = self.compute_slippage_pct(order_size_usd, avg_daily_volume_usd)
        fee_usd     = fee_rate * order_size_usd
        slippage_usd = slip_pct * order_size_usd
        total_cost  = fee_usd + slippage_usd

        # Accumulate totals
        self.total_fees_paid    += fee_usd
        self.total_slippage_paid += slippage_usd
        self.num_fills          += 1

        logger.debug(
            "Execution cost | notional=$%.2f | fee=$%.4f (%.4f%%) | "
            "slip=$%.4f (%.4f%%) | total=$%.4f",
            order_size_usd,
            fee_usd, fee_rate * 100,
            slippage_usd, slip_pct * 100,
            total_cost,
        )
        return total_cost, fee_usd, slippage_usd

    # ------------------------------------------------------------------
    # Combined effective position scalar
    # ------------------------------------------------------------------

    def effective_size_scalar(self, correlation_triggered: bool = False) -> float:
        """
        Combine all active risk scalars into a single position-size multiplier.

        Scalars applied:
          - Circuit breaker (NORMAL=1.0, REDUCED=0.5, CASH=0.0)
          - Volatility regime (LOW=1.25, MEDIUM=1.0, HIGH=0.5)
          - Correlation watchdog (triggered → ×0.6, i.e. reduce 40%)

        Parameters
        ----------
        correlation_triggered : bool
            Whether the correlation watchdog has fired this bar.

        Returns
        -------
        float
            Net position-size scalar in [0, ∞).
        """
        scalar = self.circuit_breaker_size_scalar() * self.vol_regime_size_scalar()
        if correlation_triggered:
            scalar *= 0.60
        return scalar

    # ------------------------------------------------------------------
    # Metrics summary
    # ------------------------------------------------------------------

    def risk_metrics(self, nav_series: Optional[pd.Series] = None) -> Dict:
        """
        Return a snapshot of current risk metrics.

        Parameters
        ----------
        nav_series : pd.Series, optional
            Full NAV history for VaR/CVaR computation.

        Returns
        -------
        dict
            Risk metrics suitable for logging or JSON serialisation.
        """
        var_usd, cvar_usd = (0.0, 0.0)
        if nav_series is not None and len(nav_series) >= 2:
            var_usd, cvar_usd = self.compute_var_cvar(nav_series)

        avg_slip_bps = 0.0
        if self.num_fills > 0 and self.total_slippage_paid > 0 and self.nav > 0:
            avg_slip_bps = (self.total_slippage_paid / self.num_fills / self.nav) * 10_000

        return {
            "nav":                    round(self.nav, 2),
            "peak_nav":               round(self._peak_nav, 2),
            "drawdown_pct":           round((self._peak_nav - self.nav) / max(self._peak_nav, 1) * 100, 2),
            "circuit_breaker_state":  self._circuit_breaker_state.value,
            "vol_regime":             self._vol_regime.value,
            "var_95_usd":             round(var_usd, 2),
            "cvar_95_usd":            round(cvar_usd, 2),
            "total_fees_paid_usd":    round(self.total_fees_paid, 2),
            "total_slippage_paid_usd": round(self.total_slippage_paid, 2),
            "avg_slippage_per_trade_bps": round(avg_slip_bps, 4),
            "num_fills":              self.num_fills,
        }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pandas as pd
    import numpy as np

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    print("=== RiskManagerV2 smoke test ===\n")

    # Build synthetic OHLCV data
    np.random.seed(42)
    n = 200
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    close = 30_000 * np.cumprod(1 + np.random.normal(0, 0.02, n))
    high  = close * (1 + np.abs(np.random.normal(0, 0.01, n)))
    low   = close * (1 - np.abs(np.random.normal(0, 0.01, n)))
    vol   = np.random.uniform(1e9, 5e9, n)
    ohlcv = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": vol}, index=dates)

    rm = RiskManagerV2(mode="futures", initial_capital=5_000.0)

    # A. ATR stop
    stop_price, atr_val = rm.compute_stop_prices("BTC/USDT", "long", entry_price=30_000.0, ohlcv=ohlcv)
    print(f"ATR={atr_val:.2f} | stop_price={stop_price:.2f}")
    triggered = rm.check_stops("BTC/USDT", "long", 30_000.0, 29_000.0, stop_price)
    print(f"Stop triggered at 29000: {triggered}")

    # B. Circuit breaker
    rm.update_nav(5_000.0)
    rm.update_nav(4_200.0)  # 16% drawdown → should trigger REDUCED
    state = rm.apply_circuit_breaker()
    print(f"Circuit breaker state: {state.value}")

    # C. Vol regime
    nav_series = pd.Series(close / close[0] * 5000, index=dates)
    regime = rm.detect_vol_regime(nav_series)
    print(f"Vol regime: {regime.value}")

    # D. Correlation watchdog
    price_data = {
        "BTC/USDT": ohlcv,
        "ETH/USDT": ohlcv.copy(),  # perfectly correlated → triggers watchdog
    }
    triggered, avg_corr = rm.check_correlation_watchdog(price_data)
    print(f"Correlation watchdog triggered: {triggered} | avg_corr={avg_corr:.4f}")

    # E. VaR/CVaR
    var_usd, cvar_usd = RiskManagerV2.compute_var_cvar(nav_series)
    print(f"VaR(95%)=${var_usd:.2f} | CVaR(95%)=${cvar_usd:.2f}")

    # F. Slippage / fees
    avg_vol = RiskManagerV2.estimate_avg_daily_volume_usd(ohlcv)
    cost, fee, slip = rm.compute_execution_cost(order_size_usd=5_000.0, avg_daily_volume_usd=avg_vol)
    print(f"Execution cost=${cost:.4f} | fee=${fee:.4f} | slippage=${slip:.4f}")

    # Summary
    print("\nRisk metrics snapshot:")
    metrics = rm.risk_metrics(nav_series)
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    print("\n✅ RiskManagerV2 smoke test PASSED")
