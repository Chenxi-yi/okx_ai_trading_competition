"""
core/execution.py
=================
ExecutionModel: turns target weights into live orders or simulated fills.

Maps to LEAN's ExecutionModel. Receives final risk-adjusted weights and
is responsible for computing position deltas and submitting / simulating orders.

Concrete implementations:
  MarketOrderExecution  — live trading: sends real market orders via Broker
  SimulatedExecution    — backtesting: fills at mark price ± square-root slippage,
                          no exchange dependency

Both share _compute_deltas() so delta logic is never duplicated.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ExecutionModel(ABC):
    """
    Execute trades to move the portfolio from current positions to target weights.

    execute() returns trade record dicts (same format as portfolio.record_trade()).
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def execute(
        self,
        portfolio,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
        nav: float,
        profile: Dict[str, Any],
        mode: str,
    ) -> List[Dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------

def compute_deltas(
    portfolio,
    target_weights: Dict[str, float],
    prices: Dict[str, float],
    nav: float,
    min_notional: float,
) -> List[Dict[str, Any]]:
    """
    Compute the list of trades needed to move from current positions to
    target_weights. Shared by both live and simulated execution.
    """
    deltas = []
    all_symbols = sorted(set(target_weights.keys()) | set(portfolio.positions.keys()))

    for sym in all_symbols:
        price = prices.get(sym)
        if not price or price <= 0:
            continue

        current_qty = portfolio.positions[sym].qty if sym in portfolio.positions else 0.0
        current_notional = current_qty * price
        target_notional = target_weights.get(sym, 0.0) * nav
        delta_notional = target_notional - current_notional

        if abs(delta_notional) < min_notional:
            continue

        deltas.append({
            "symbol": sym,
            "side": "buy" if delta_notional > 0 else "sell",
            "quantity": abs(delta_notional) / price,
            "delta_notional": delta_notional,
        })

    return deltas


# ---------------------------------------------------------------------------
# MarketOrderExecution  (live trading)
# ---------------------------------------------------------------------------

class MarketOrderExecution(ExecutionModel):
    """
    Computes position deltas and submits market orders via the exchange Broker.
    Used in live trading only.
    """

    def __init__(self, broker):
        self.broker = broker

    @property
    def name(self) -> str:
        return "market_order"

    def execute(
        self,
        portfolio,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
        nav: float,
        profile: Dict[str, Any],
        mode: str,
    ) -> List[Dict[str, Any]]:
        from config.settings import TRANSACTION_COSTS

        min_notional = float(profile.get("portfolio", {}).get("min_rebalance_notional_usd", 10.0))
        deltas = compute_deltas(portfolio, target_weights, prices, nav, min_notional)

        trades = []
        tx_cost_pct = TRANSACTION_COSTS[mode]

        for delta in deltas:
            sym = delta["symbol"]
            side = delta["side"]
            qty = delta["quantity"]
            notional = abs(delta["delta_notional"])

            try:
                result = self.broker.market_buy(sym, qty) if side == "buy" else self.broker.market_sell(sym, qty)
                fill_price = result.get("price") or prices.get(sym, 0)
                fee = notional * tx_cost_pct

                trade_record = portfolio.record_trade(
                    symbol=sym, side=side, qty=qty,
                    fill_price=fill_price, fee=fee,
                    order_id=result.get("id", "unknown"),
                )
                trades.append(trade_record)
                logger.info(
                    "[%s] Trade: %s %s %.4f @ %.4f | notional=$%.2f",
                    portfolio.portfolio_id, side.upper(), sym, qty, fill_price, notional,
                )

            except Exception as e:
                logger.error("[%s] Failed to execute %s %s: %s", portfolio.portfolio_id, side, sym, e)
                trades.append({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "symbol": sym, "side": side, "qty": qty, "error": str(e),
                })

        return trades


# ---------------------------------------------------------------------------
# SimulatedExecution  (backtesting)
# ---------------------------------------------------------------------------

class SimulatedExecution(ExecutionModel):
    """
    Simulates order fills for backtesting.

    Uses the same delta logic as MarketOrderExecution but fills at
    mark price ± square-root market impact slippage:

        slippage_pct = slippage_factor × √(order_notional / avg_daily_volume)

    No exchange dependency — works offline with historical data.

    Usage in BacktestRunner:
        exec_model = SimulatedExecution()
        exec_model.set_volumes(adv_dict)   # call before each rebalance
        algorithm = TradingAlgorithm(..., execution=exec_model)
    """

    def __init__(
        self,
        slippage_factor: float = 0.10,
        default_slippage_pct: float = 0.001,  # fallback if no volume data
    ):
        self.slippage_factor = slippage_factor
        self.default_slippage_pct = default_slippage_pct
        self._volumes: Dict[str, float] = {}   # ADV in USD, injected by BacktestRunner

    @property
    def name(self) -> str:
        return "simulated"

    def set_volumes(self, volumes: Dict[str, float]) -> None:
        """Inject current average daily volumes. Call before each rebalance."""
        self._volumes = volumes

    def execute(
        self,
        portfolio,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
        nav: float,
        profile: Dict[str, Any],
        mode: str,
    ) -> List[Dict[str, Any]]:
        from config.settings import TRANSACTION_COSTS

        min_notional = float(profile.get("portfolio", {}).get("min_rebalance_notional_usd", 10.0))
        deltas = compute_deltas(portfolio, target_weights, prices, nav, min_notional)

        trades = []
        tx_cost_pct = TRANSACTION_COSTS[mode]

        for delta in deltas:
            sym = delta["symbol"]
            side = delta["side"]
            qty = delta["quantity"]
            notional = abs(delta["delta_notional"])
            mark_price = prices.get(sym, 0.0)
            if not mark_price:
                continue

            # Square-root market impact slippage
            slippage_pct = self._compute_slippage(notional, self._volumes.get(sym, 0.0))

            # Buy at a higher price, sell at a lower price (adverse slippage)
            fill_price = mark_price * (1 + slippage_pct) if side == "buy" else mark_price * (1 - slippage_pct)
            fee = notional * tx_cost_pct

            slippage_usd = abs(fill_price - mark_price) * qty

            trade_record = portfolio.record_trade(
                symbol=sym, side=side, qty=qty,
                fill_price=fill_price, fee=fee,
                order_id=f"sim_{sym}_{datetime.now(timezone.utc).timestamp():.0f}",
            )
            # Attach slippage for BacktestRunner metrics tracking
            trade_record["slippage"] = slippage_usd
            trade_record["notional"] = notional
            trades.append(trade_record)

        return trades

    def _compute_slippage(self, order_notional: float, adv_usd: float) -> float:
        """Square-root market impact: slippage = factor × √(order / ADV)."""
        if adv_usd <= 0 or order_notional <= 0:
            return self.default_slippage_pct
        return self.slippage_factor * math.sqrt(order_notional / adv_usd)


# ---------------------------------------------------------------------------
# SmartExecution  (spread-aware: limit if tight spread, market otherwise)
# ---------------------------------------------------------------------------

class SmartExecution(ExecutionModel):
    """
    Spread-aware execution: checks the live orderbook before each order.

    - If bid-ask spread < spread_threshold_pct → post a limit order.
      Waits up to limit_timeout_sec for a fill, then falls back to market.
    - If spread >= threshold or orderbook unavailable → immediate market order.
    - For large orders (notional > twap_threshold × ADV) → delegates to TwapExecution.

    Uses settings from config/settings.py by default so live tuning is centralised.
    """

    def __init__(
        self,
        broker,
        spread_threshold_pct: float = None,
        limit_timeout_sec: int = None,
        twap_threshold_pct: float = None,
        twap_slices: int = None,
        twap_interval_sec: int = None,
    ):
        from config.settings import (
            LIMIT_ORDER_TIMEOUT_SEC,
            SPREAD_THRESHOLD_PCT,
            TWAP_DEPTH_THRESHOLD_PCT,
            TWAP_INTERVAL_SEC,
            TWAP_NUM_SLICES,
        )
        self.broker = broker
        self.spread_threshold = spread_threshold_pct if spread_threshold_pct is not None else SPREAD_THRESHOLD_PCT
        self.limit_timeout = limit_timeout_sec if limit_timeout_sec is not None else LIMIT_ORDER_TIMEOUT_SEC
        self.twap_threshold = twap_threshold_pct if twap_threshold_pct is not None else TWAP_DEPTH_THRESHOLD_PCT
        self._twap = TwapExecution(
            broker,
            num_slices=twap_slices or TWAP_NUM_SLICES,
            interval_sec=twap_interval_sec or TWAP_INTERVAL_SEC,
        )

    @property
    def name(self) -> str:
        return "smart"

    def execute(
        self,
        portfolio,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
        nav: float,
        profile: Dict[str, Any],
        mode: str,
    ) -> List[Dict[str, Any]]:
        from config.settings import TRANSACTION_COSTS

        min_notional = float(profile.get("portfolio", {}).get("min_rebalance_notional_usd", 10.0))
        deltas = compute_deltas(portfolio, target_weights, prices, nav, min_notional)

        trades = []
        tx_cost_pct = TRANSACTION_COSTS[mode]

        for delta in deltas:
            sym = delta["symbol"]
            side = delta["side"]
            qty = delta["quantity"]
            notional = abs(delta["delta_notional"])
            price = prices.get(sym, 0.0)

            # Large order → TWAP
            if self._is_large_order(notional, sym):
                twap_trades = self._twap._execute_single(
                    portfolio, sym, side, qty, notional, price, tx_cost_pct
                )
                trades.extend(twap_trades)
                continue

            # Check spread → limit or market
            spread_pct = self._get_spread(sym)
            if spread_pct is not None and spread_pct < self.spread_threshold:
                trade = self._try_limit_order(portfolio, sym, side, qty, price, notional, tx_cost_pct)
            else:
                trade = self._market_order(portfolio, sym, side, qty, price, notional, tx_cost_pct)

            if trade:
                trades.append(trade)

        return trades

    def _get_spread(self, symbol: str) -> Optional[float]:
        try:
            ob = self.broker.get_orderbook(symbol, limit=1)
            bid = ob["bids"][0][0] if ob.get("bids") else None
            ask = ob["asks"][0][0] if ob.get("asks") else None
            if bid and ask and bid > 0:
                return (ask - bid) / bid
        except Exception:
            pass
        return None

    def _is_large_order(self, notional: float, symbol: str) -> bool:
        # Without ADV data in live mode, use a simple $10k threshold
        return notional > 10_000

    def _try_limit_order(
        self, portfolio, sym, side, qty, price, notional, tx_cost_pct
    ) -> Optional[Dict]:
        """Post limit order, wait for fill, fall back to market on timeout."""
        import time as _time
        limit_price = price * 1.0005 if side == "buy" else price * 0.9995  # slight improvement on mid
        try:
            result = self.broker.limit_buy(sym, qty, limit_price) if side == "buy" else self.broker.limit_sell(sym, qty, limit_price)
            order_id = result.get("id")
            deadline = _time.time() + self.limit_timeout
            while _time.time() < deadline:
                _time.sleep(2)
                status = self.broker.fetch_order_status(order_id, sym)
                if status.get("status") in ("closed", "filled"):
                    fill_price = float(status.get("average") or status.get("price") or limit_price)
                    return portfolio.record_trade(
                        symbol=sym, side=side, qty=qty,
                        fill_price=fill_price, fee=notional * tx_cost_pct,
                        order_id=order_id,
                    )
            # Timeout — cancel and fall back to market
            try:
                self.broker.cancel_order(order_id, sym)
            except Exception:
                pass
            logger.info("[smart] Limit order timed out on %s, falling back to market", sym)
        except Exception as e:
            logger.warning("[smart] Limit order failed for %s: %s, using market", sym, e)

        return self._market_order(portfolio, sym, side, qty, price, notional, tx_cost_pct)

    def _market_order(self, portfolio, sym, side, qty, price, notional, tx_cost_pct) -> Optional[Dict]:
        try:
            result = self.broker.market_buy(sym, qty) if side == "buy" else self.broker.market_sell(sym, qty)
            fill_price = result.get("price") or price
            return portfolio.record_trade(
                symbol=sym, side=side, qty=qty,
                fill_price=fill_price, fee=notional * tx_cost_pct,
                order_id=result.get("id", "market"),
            )
        except Exception as e:
            logger.error("[smart] Market order failed for %s %s: %s", side, sym, e)
            return None


# ---------------------------------------------------------------------------
# TwapExecution  (time-weighted average price — splits large orders)
# ---------------------------------------------------------------------------

class TwapExecution(ExecutionModel):
    """
    Splits large orders into num_slices equal tranches executed interval_sec apart.
    Reduces market impact for illiquid symbols or large position changes.

    Defaults from config/settings.py: TWAP_NUM_SLICES=4, TWAP_INTERVAL_SEC=15
    """

    def __init__(self, broker, num_slices: int = None, interval_sec: int = None):
        from config.settings import TWAP_INTERVAL_SEC, TWAP_NUM_SLICES
        self.broker = broker
        self.num_slices = num_slices or TWAP_NUM_SLICES
        self.interval_sec = interval_sec or TWAP_INTERVAL_SEC

    @property
    def name(self) -> str:
        return "twap"

    def execute(
        self,
        portfolio,
        target_weights: Dict[str, float],
        prices: Dict[str, float],
        nav: float,
        profile: Dict[str, Any],
        mode: str,
    ) -> List[Dict[str, Any]]:
        from config.settings import TRANSACTION_COSTS

        min_notional = float(profile.get("portfolio", {}).get("min_rebalance_notional_usd", 10.0))
        deltas = compute_deltas(portfolio, target_weights, prices, nav, min_notional)
        tx_cost_pct = TRANSACTION_COSTS[mode]

        trades = []
        for delta in deltas:
            sym = delta["symbol"]
            side = delta["side"]
            qty = delta["quantity"]
            notional = abs(delta["delta_notional"])
            price = prices.get(sym, 0.0)
            trades.extend(self._execute_single(portfolio, sym, side, qty, notional, price, tx_cost_pct))

        return trades

    def _execute_single(
        self, portfolio, sym, side, total_qty, total_notional, price, tx_cost_pct
    ) -> List[Dict]:
        import time as _time
        slice_qty = total_qty / self.num_slices
        slice_notional = total_notional / self.num_slices
        trades = []

        logger.info(
            "[twap] %s %s: splitting %.4f into %d slices × %.4f (interval=%ds)",
            side.upper(), sym, total_qty, self.num_slices, slice_qty, self.interval_sec,
        )

        for i in range(self.num_slices):
            if i > 0:
                _time.sleep(self.interval_sec)
            try:
                result = self.broker.market_buy(sym, slice_qty) if side == "buy" else self.broker.market_sell(sym, slice_qty)
                fill_price = result.get("price") or price
                trade = portfolio.record_trade(
                    symbol=sym, side=side, qty=slice_qty,
                    fill_price=fill_price, fee=slice_notional * tx_cost_pct,
                    order_id=f"{result.get('id', 'twap')}_s{i+1}",
                )
                trades.append(trade)
                logger.debug("[twap] Slice %d/%d filled: %s @ %.4f", i+1, self.num_slices, sym, fill_price)
            except Exception as e:
                logger.error("[twap] Slice %d/%d failed for %s: %s", i+1, self.num_slices, sym, e)

        return trades
