"""
engine/trading_engine.py
========================
Core trading engine orchestrator.

Manages multiple Portfolio instances, each with independent:
  - Rebalance scheduling
  - Risk monitoring
  - Position tracking and PnL

Runs as a long-lived event loop. Replaces the old LiveEngine
single-shot approach with an autonomous daemon.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from config.profiles import get_profile
from config.settings import (
    DRAWDOWN_CIRCUIT_BREAKER_2,
    INTRADAY_DD_FROM_PEAK,
    TRADING_MODE,
    TRANSACTION_COSTS,
    get_symbols,
)
from data.feed_async import fetch_universe_parallel
from data.feed_ws import WebSocketPriceCache
from data.fetcher import fetch_universe
from execution.broker import Broker
from logging_.structured_logger import StructuredLogger
from portfolio.portfolio import (
    Portfolio,
    load_engine_state,
    save_engine_state,
)

logger = logging.getLogger(__name__)

# Rebalance intervals by profile
REBALANCE_INTERVALS = {
    "daily": 86400,      # 24 hours
    "hourly": 4 * 3600,  # 4 hours
}

RISK_CHECK_INTERVAL = 60  # seconds


class TradingEngine:
    """
    Autonomous trading engine managing multiple isolated portfolios.

    Usage:
        engine = TradingEngine(sandbox=True)
        engine.start(portfolio_configs)
        engine.run_loop()  # blocks until stop signal
    """

    def __init__(self, sandbox: bool = True, mode: str = TRADING_MODE, paper: bool = False):
        self.sandbox = sandbox
        self.mode = mode
        self.paper = paper
        self.broker = Broker(mode=mode, sandbox=sandbox)
        self.slogger = StructuredLogger()
        self.portfolios: Dict[str, Portfolio] = {}
        self.running = False
        self._pid: int = os.getpid()
        self._ws_cache: Optional[WebSocketPriceCache] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, portfolio_configs: List[Dict[str, Any]]) -> str:
        """
        Initialize portfolios from config and prepare the engine.

        portfolio_configs: list of dicts with keys:
          - id: str (portfolio identifier)
          - strategy: str (strategy id from strategies.json)
          - profile: str ("daily" or "hourly")
          - capital: float (USD capital)
        """
        # Try to restore existing state
        existing = load_engine_state()
        requested_ids = {cfg["id"] for cfg in portfolio_configs}

        for cfg in portfolio_configs:
            pid = cfg["id"]
            if pid in existing and existing[pid].positions:
                # Restore existing portfolio (regardless of engine_status)
                existing[pid].engine_status = "running"
                self.portfolios[pid] = existing[pid]
                logger.info("Restored portfolio '%s' from saved state (%d positions, cash=$%.2f)",
                            pid, len(existing[pid].positions), existing[pid].cash)
            else:
                # Create new portfolio
                profile_name = cfg.get("profile", "daily")
                rebal_sec = REBALANCE_INTERVALS.get(profile_name, 86400)
                p = Portfolio(
                    portfolio_id=pid,
                    strategy_id=cfg["strategy"],
                    profile_name=profile_name,
                    initial_capital=cfg["capital"],
                    rebalance_interval_sec=rebal_sec,
                    risk_check_interval_sec=RISK_CHECK_INTERVAL,
                )
                self.portfolios[pid] = p
                logger.info(
                    "Created portfolio '%s': strategy=%s, profile=%s, capital=$%.2f, rebal=%ds",
                    pid, cfg["strategy"], profile_name, cfg["capital"], rebal_sec,
                )

        self.running = True
        self._write_pid()

        # Start WebSocket price cache for the full universe + held positions
        ws_symbols = list(get_symbols(self.mode, dynamic=True))
        for p in self.portfolios.values():
            for sym in p.positions:
                if sym not in ws_symbols:
                    ws_symbols.append(sym)
        try:
            self._ws_cache = WebSocketPriceCache(ws_symbols, sandbox=self.sandbox)
            self._ws_cache.start()
            logger.info("WebSocket price cache started (%d symbols)", len(ws_symbols))
        except Exception as e:
            logger.warning("WebSocket price cache failed to start: %s — falling back to REST", e)
            self._ws_cache = None

        # Persist initial state and write summary
        save_engine_state(self.portfolios)
        self._update_summary("running")

        # Log engine start
        self.slogger.log_engine_event(
            "start",
            f"Engine started with {len(self.portfolios)} portfolio(s)",
            self.portfolios,
        )

        # Build startup message
        lines = [
            "=" * 50,
            "TRADING ENGINE STARTED",
            "=" * 50,
            f"PID: {self._pid}",
            f"Mode: {self.mode} ({'sandbox' if self.sandbox else 'LIVE'})",
            f"Portfolios: {len(self.portfolios)}",
            "",
        ]
        for pid, p in self.portfolios.items():
            lines.append(
                f"  [{pid}] strategy={p.strategy_id} profile={p.profile_name} "
                f"capital=${p.initial_capital:,.2f} rebalance_every={p.rebalance_interval_sec}s"
            )
        lines.append("")
        lines.append("Daemon running. Use 'main.py status' to check progress.")
        lines.append("=" * 50)
        return "\n".join(lines)

    def run_loop(self) -> None:
        """
        Main event loop. Blocks until SIGTERM/SIGINT.

        Checks each portfolio's rebalance and risk schedule every 30 seconds.
        """
        # Install signal handlers
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

        logger.info("Event loop started (PID=%d)", self._pid)

        # Trigger immediate first rebalance for all portfolios
        for pid, portfolio in self.portfolios.items():
            if portfolio.last_rebalance is None:
                try:
                    logger.info("Running initial rebalance for '%s'", pid)
                    self.rebalance_portfolio(portfolio)
                except Exception as e:
                    logger.error("Initial rebalance failed for '%s': %s", pid, e)

        while self.running:
            try:
                now = datetime.now(timezone.utc)

                for portfolio in list(self.portfolios.values()):
                    if not self.running:
                        break

                    # Risk check (every 60s)
                    if portfolio.should_check_risk(now):
                        try:
                            self.check_risk(portfolio)
                        except Exception as e:
                            logger.error("Risk check failed for '%s': %s", portfolio.portfolio_id, e)

                    # Rebalance on schedule
                    if portfolio.should_rebalance(now):
                        try:
                            self.rebalance_portfolio(portfolio)
                        except Exception as e:
                            logger.error("Rebalance failed for '%s': %s", portfolio.portfolio_id, e)

                # Write summary and persist state
                self._update_summary("running")
                save_engine_state(self.portfolios)

            except Exception as e:
                logger.error("Event loop error: %s", e)

            # Sleep in small increments so we can respond to signals quickly
            for _ in range(6):  # 6 x 5s = 30s total
                if not self.running:
                    break
                time.sleep(5)

        # Graceful shutdown
        self._shutdown()

    def stop(self) -> str:
        """Signal the engine to stop (called from main.py stop command)."""
        self.running = False
        return "Stop signal sent."

    # ------------------------------------------------------------------
    # Rebalance
    # ------------------------------------------------------------------

    def rebalance_portfolio(self, portfolio: Portfolio) -> Dict[str, Any]:
        """Run a single rebalance cycle via the TradingAlgorithm pipeline."""
        pid = portfolio.portfolio_id
        logger.info("[%s] Starting rebalance", pid)

        profile = get_profile(portfolio.profile_name)
        symbols = get_symbols(self.mode, dynamic=True)

        price_data = self._fetch_market_data(profile, symbols)
        if not price_data:
            logger.error("[%s] No market data available, skipping rebalance", pid)
            return {"success": False, "error": "no_market_data"}

        algorithm = self._build_algorithm(portfolio.profile_name)
        result = algorithm.rebalance(portfolio, price_data, profile, self.mode, self.slogger)

        # Cache ATR stops for each position — used by check_risk() every 60s
        self._update_atr_stops(portfolio, price_data)
        save_engine_state(self.portfolios)

        logger.info(
            "[%s] Rebalance complete | NAV: $%.2f → $%.2f | Trades: %d | Positions: %d",
            pid, result.nav_before, result.nav_after, len(result.trades), result.n_positions,
        )

        return {
            "success": result.success,
            "nav_before": result.nav_before,
            "nav_after": result.nav_after,
            "trades": result.trades,
            "risk_summary": result.risk_summary,
        }

    def _build_algorithm(self, profile_name: str):
        """
        Compose the TradingAlgorithm pipeline for the given profile.

        Each profile can use different risk model parameters. Hourly runs
        tighter circuit breaker thresholds because its higher trading
        frequency means losses can compound faster between checks.
        """
        from core.algorithm import TradingAlgorithm
        from core.alpha import CombinedAlphaModel
        from core.execution import MarketOrderExecution, SimulatedExecution, SmartExecution
        from core.portfolio_construction import SignalFilteredPortfolioModel
        from core.risk import (
            CompositeRiskModel,
            CorrelationWatchdogModel,
            DrawdownCircuitBreakerModel,
            VolRegimeModel,
        )

        if profile_name == "hourly":
            # Tighter thresholds: 4h rebalance means intraday losses can build up
            # fast, so act sooner (8% → REDUCED, 15% → CASH vs 10%/20% for daily).
            risk_stack = CompositeRiskModel([
                DrawdownCircuitBreakerModel(threshold_reduced=0.08, threshold_cash=0.15),
                VolRegimeModel(),
                CorrelationWatchdogModel(threshold=0.80),
            ])
        else:  # daily and any future profiles default to standard settings
            risk_stack = CompositeRiskModel([
                DrawdownCircuitBreakerModel(),   # 10% → REDUCED, 20% → CASH
                VolRegimeModel(),
                CorrelationWatchdogModel(),      # 0.85 threshold
            ])

        # Paper mode → SimulatedExecution (real prices, no real orders)
        if self.paper:
            execution = SimulatedExecution()
        else:
            execution = SmartExecution(self.broker)

        return TradingAlgorithm(
            alpha=CombinedAlphaModel(),
            portfolio_construction=SignalFilteredPortfolioModel(),
            risk=risk_stack,
            execution=execution,
        )

    # ------------------------------------------------------------------
    # Risk monitoring
    # ------------------------------------------------------------------

    def check_risk(self, portfolio: Portfolio) -> Dict[str, Any]:
        """
        Lightweight risk check: fetch equity, check drawdown thresholds.
        Runs every 60s.
        """
        pid = portfolio.portfolio_id

        # Get latest prices via broker ticker (lightweight, no OHLCV)
        prices = self._fetch_prices_lightweight(portfolio)
        if not prices:
            portfolio.last_risk_check = datetime.now(timezone.utc)
            return {"action": "error", "reason": "no_prices"}

        current_nav = portfolio.nav(prices)
        peak_nav = portfolio.risk_state.get("peak_nav", portfolio.initial_capital)

        # Update peak
        if current_nav > peak_nav:
            portfolio.risk_state["peak_nav"] = current_nav
            peak_nav = current_nav

        dd_pct = ((peak_nav - current_nav) / peak_nav * 100) if peak_nav > 0 else 0

        # Check circuit breaker thresholds
        from config.settings import DRAWDOWN_CIRCUIT_BREAKER_2, INTRADAY_DD_FROM_PEAK
        action = "none"

        if dd_pct >= DRAWDOWN_CIRCUIT_BREAKER_2 * 100:
            action = "emergency_close"
            logger.warning(
                "[%s] RISK ALERT: Drawdown %.1f%% exceeds threshold. Emergency close!",
                pid, dd_pct,
            )
            self._close_all_positions(portfolio, prices)
            portfolio.engine_status = "stopped"
            portfolio.risk_state["circuit_breaker_state"] = "CASH"

        elif dd_pct >= INTRADAY_DD_FROM_PEAK * 100:
            action = "warning"
            logger.warning("[%s] RISK WARNING: Drawdown %.1f%% approaching threshold", pid, dd_pct)

        # ATR stop check — close individual positions that breached their stop level
        if action == "none":
            stopped = self._check_atr_stops(portfolio, prices)
            if stopped:
                action = "atr_stop"

        portfolio.last_risk_check = datetime.now(timezone.utc)

        # Log risk check (only if notable)
        if action != "none":
            snapshot = portfolio.snapshot(prices)
            self.slogger.log_risk_check(pid, snapshot, action, {"dd_pct": dd_pct})

        return {"action": action, "dd_pct": dd_pct, "nav": current_nav}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_market_data(
        self,
        profile: Dict[str, Any],
        symbols: list,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV data for the universe — parallel async, no IP-ban risk."""
        lookback_days = profile.get("market_data_lookback_days", 300)
        timeframe = profile.get("timeframe", "1d")
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        logger.info("Fetching market data (parallel): %s to %s (tf=%s, %d symbols)", start, end, timeframe, len(symbols))
        try:
            return fetch_universe_parallel(
                symbols=symbols,
                start=start,
                end=end,
                mode=self.mode,
                timeframe=timeframe,
                sandbox=self.sandbox,
            )
        except Exception as e:
            logger.warning("Parallel fetch failed (%s), falling back to sequential: %s", type(e).__name__, e)
            return fetch_universe(
                symbols=symbols,
                start=start,
                end=end,
                mode=self.mode,
                use_cache=False,
                sandbox=self.sandbox,
                timeframe=timeframe,
            )

    def _get_current_prices(self, price_data: Dict[str, pd.DataFrame]) -> Dict[str, float]:
        prices = {}
        for sym, df in price_data.items():
            if not df.empty and "close" in df.columns:
                prices[sym] = float(df["close"].iloc[-1])
        return prices

    def _update_atr_stops(
        self,
        portfolio: Portfolio,
        price_data: Dict[str, pd.DataFrame],
    ) -> None:
        """
        Compute ATR-based stop levels for every open position and cache them
        in portfolio.risk_state["atr_stops"]. Called after each rebalance.
        check_risk() then compares current prices against these levels every 60s
        without needing a fresh OHLCV fetch.
        """
        from risk.risk_manager_v2 import RiskManagerV2
        from config.settings import ATR_MULTIPLIER

        rm = RiskManagerV2(mode=self.mode)
        atr_stops: Dict[str, Any] = {}

        for sym, pos in portfolio.positions.items():
            if abs(pos.qty) < 1e-10 or sym not in price_data:
                continue
            ohlcv = price_data[sym]
            if ohlcv.empty or not all(c in ohlcv.columns for c in ("high", "low", "close")):
                continue
            side = "long" if pos.qty > 0 else "short"
            stop_price, atr = rm.compute_stop_prices(sym, side, pos.avg_entry_price, ohlcv)
            atr_stops[sym] = {
                "stop_price": round(stop_price, 8),
                "side": side,
                "entry": pos.avg_entry_price,
                "atr": round(atr, 8) if atr else None,
            }
            logger.debug(
                "[%s] ATR stop set: %s %s | entry=%.4f | stop=%.4f | atr=%.4f",
                portfolio.portfolio_id, sym, side.upper(), pos.avg_entry_price, stop_price, atr or 0,
            )

        portfolio.risk_state["atr_stops"] = atr_stops

    def _check_atr_stops(
        self,
        portfolio: Portfolio,
        prices: Dict[str, float],
    ) -> bool:
        """
        Check cached ATR stop levels against current prices.
        Closes any position that has breached its stop. Returns True if any stops triggered.
        """
        from risk.risk_manager_v2 import RiskManagerV2

        rm = RiskManagerV2(mode=self.mode)
        atr_stops = portfolio.risk_state.get("atr_stops", {})
        any_triggered = False

        for sym, stop_info in list(atr_stops.items()):
            if sym not in portfolio.positions:
                continue
            current_price = prices.get(sym)
            if not current_price:
                continue
            triggered = rm.check_stops(
                sym,
                stop_info["side"],
                stop_info["entry"],
                current_price,
                stop_info["stop_price"],
            )
            if triggered:
                logger.warning(
                    "[%s] ATR stop triggered: %s %s | price=%.4f | stop=%.4f",
                    portfolio.portfolio_id, sym, stop_info["side"].upper(),
                    current_price, stop_info["stop_price"],
                )
                self._close_position(portfolio, sym, prices)
                del atr_stops[sym]
                any_triggered = True

        return any_triggered

    def _close_position(
        self,
        portfolio: Portfolio,
        symbol: str,
        prices: Dict[str, float],
    ) -> Optional[Dict[str, Any]]:
        """Close a single position via market order."""
        pos = portfolio.positions.get(symbol)
        if not pos or abs(pos.qty) < 1e-10:
            return None

        side = "sell" if pos.qty > 0 else "buy"
        qty = abs(pos.qty)
        price = prices.get(symbol, 0)
        notional = qty * price

        try:
            result = self.broker.market_sell(symbol, qty) if side == "sell" else self.broker.market_buy(symbol, qty)
            fill_price = result.get("price") or price
            fee = notional * TRANSACTION_COSTS[self.mode]
            trade = portfolio.record_trade(
                symbol=symbol, side=side, qty=qty,
                fill_price=fill_price, fee=fee,
                order_id=result.get("id", "atr_stop"),
            )
            logger.info("[%s] Position closed (ATR stop): %s | qty=%.4f @ %.4f", portfolio.portfolio_id, symbol, qty, fill_price)
            return trade
        except Exception as e:
            logger.error("[%s] Failed to close %s on ATR stop: %s", portfolio.portfolio_id, symbol, e)
            return None

    def _close_all_positions(
        self,
        portfolio: Portfolio,
        prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Emergency close all positions for a portfolio."""
        logger.warning("[%s] CLOSING ALL POSITIONS", portfolio.portfolio_id)
        trades = []

        for sym, pos in list(portfolio.positions.items()):
            if abs(pos.qty) < 1e-10:
                continue
            side = "sell" if pos.qty > 0 else "buy"
            qty = abs(pos.qty)
            price = prices.get(sym, 0)
            notional = qty * price

            try:
                if side == "buy":
                    result = self.broker.market_buy(sym, qty)
                else:
                    result = self.broker.market_sell(sym, qty)

                fill_price = result.get("price") or price
                fee = notional * TRANSACTION_COSTS[self.mode]

                trade_record = portfolio.record_trade(
                    symbol=sym, side=side, qty=qty,
                    fill_price=fill_price, fee=fee,
                    order_id=result.get("id", "emergency"),
                )
                trades.append(trade_record)

            except Exception as e:
                logger.error("[%s] Emergency close failed for %s: %s", portfolio.portfolio_id, sym, e)

        return trades

    def _fetch_prices_lightweight(self, portfolio: Portfolio) -> Dict[str, float]:
        """
        Get current prices — from WebSocket cache if warm, REST otherwise.
        WebSocket path: instant, zero API calls.
        REST fallback: individual ticker calls (original behaviour).
        """
        symbols = set(portfolio.positions.keys())
        if not symbols:
            return {}

        # Try WebSocket cache first
        if self._ws_cache and self._ws_cache.ready:
            prices = {}
            missing = []
            for sym in symbols:
                p = self._ws_cache.get(sym)
                if p:
                    prices[sym] = p
                else:
                    missing.append(sym)
            if missing:
                # Cache warm but this symbol not yet seen — REST fallback for missing only
                for sym in missing:
                    try:
                        ticker = self.broker.get_ticker(sym)
                        prices[sym] = float(ticker.get("last", 0))
                    except Exception as e:
                        logger.debug("REST ticker fallback failed for %s: %s", sym, e)
            return prices

        # WS not ready — full REST fallback
        prices = {}
        for sym in symbols:
            try:
                ticker = self.broker.get_ticker(sym)
                prices[sym] = float(ticker.get("last", 0))
            except Exception as e:
                logger.debug("Could not fetch ticker for %s: %s", sym, e)
        return prices

    # ------------------------------------------------------------------
    # Summary and state management
    # ------------------------------------------------------------------

    def _update_summary(self, status: str) -> None:
        """Write summary.json with all portfolio snapshots."""
        # Get prices for snapshot (lightweight)
        all_prices: Dict[str, float] = {}
        for portfolio in self.portfolios.values():
            prices = self._fetch_prices_lightweight(portfolio)
            all_prices.update(prices)

        snapshots = {}
        for pid, portfolio in self.portfolios.items():
            snapshots[pid] = portfolio.snapshot(all_prices)

        self.slogger.write_summary(snapshots, engine_status=status, pid=self._pid)

    # ------------------------------------------------------------------
    # Signal handling and shutdown
    # ------------------------------------------------------------------

    def _handle_stop(self, signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        logger.info("Received signal %d, initiating graceful shutdown...", signum)
        self.running = False

    def _shutdown(self) -> None:
        """Graceful shutdown: close positions, persist state, write final summary."""
        logger.info("Shutting down trading engine...")

        # Update status
        for portfolio in self.portfolios.values():
            portfolio.engine_status = "stopped"

        # Persist final state
        save_engine_state(self.portfolios)
        self._update_summary("stopped")

        # Log engine stop
        self.slogger.log_engine_event(
            "stop",
            f"Engine stopped gracefully with {len(self.portfolios)} portfolio(s)",
            self.portfolios,
        )

        # Stop WebSocket price cache
        if self._ws_cache:
            try:
                self._ws_cache.stop()
            except Exception as e:
                logger.debug("WS cache stop error: %s", e)

        # Clean up PID file
        self._remove_pid()
        logger.info("Trading engine stopped.")

    def _write_pid(self) -> None:
        """Write PID file for daemon management."""
        from config.settings import BASE_DIR
        pid_file = BASE_DIR / "control" / "trading.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(self._pid))

    def _remove_pid(self) -> None:
        """Remove PID file on shutdown."""
        from config.settings import BASE_DIR
        pid_file = BASE_DIR / "control" / "trading.pid"
        if pid_file.exists():
            pid_file.unlink()

    # ------------------------------------------------------------------
    # Exchange sanity check
    # ------------------------------------------------------------------

    def verify_exchange_alignment(self) -> Dict[str, Any]:
        """
        Cross-check sum of all portfolio NAVs against exchange equity.
        Returns alignment info for logging.
        """
        try:
            exchange_equity = self.broker.get_total_equity()
        except Exception as e:
            return {"status": "error", "reason": str(e)}

        all_prices = {}
        for portfolio in self.portfolios.values():
            prices = self._fetch_prices_lightweight(portfolio)
            all_prices.update(prices)

        internal_total = sum(p.nav(all_prices) for p in self.portfolios.values())

        divergence = abs(internal_total - exchange_equity) / max(exchange_equity, 1.0)
        result = {
            "status": "ok" if divergence < 0.05 else "warning",
            "internal_total": round(internal_total, 2),
            "exchange_equity": round(exchange_equity, 2),
            "divergence_pct": round(divergence * 100, 2),
        }

        if divergence >= 0.05:
            logger.warning(
                "NAV divergence: internal=$%.2f vs exchange=$%.2f (%.1f%%)",
                internal_total, exchange_equity, divergence * 100,
            )

        return result
