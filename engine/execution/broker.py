"""
execution/broker.py
====================
Execution layer — OKX USDT-M perpetual swaps.

ALL order operations (market, limit, cancel, leverage) route through the
OKX Agent Trade Kit CLI (`okx` command). This ensures every order is tagged
"agentTradeKit" in OKX billing records — required for competition scoring.

ccxt is used ONLY for read-only market data (tickers, orderbooks, OHLCV)
and contract size lookups. No ccxt calls ever place, amend, or cancel orders.

  Read-only data  → ccxt.okx (tickers, orderbooks, contract specs)
  Order placement → `okx swap place ...` CLI
  Order management → `okx swap cancel/get ...` CLI
  Account queries → `okx account balance ...` CLI
  Leverage/config → `okx swap leverage ...` CLI

In backtest/paper mode (LIVE_TRADING=False) all order methods return synthetic
fills. Set LIVE_TRADING=True to route real orders via the CLI.
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from config.settings import (
    OKX_API_KEY,
    OKX_API_SECRET,
    OKX_PASSPHRASE,
    LEVERAGE_LIMITS,
    LIVE_TRADING as _SETTINGS_LIVE_TRADING,
    TRADING_MODE,
    TRANSACTION_COSTS,
)

logger = logging.getLogger(__name__)


def _is_live_trading() -> bool:
    return os.environ.get("LIVE_TRADING", "false").lower() == "true"

LIVE_TRADING = _SETTINGS_LIVE_TRADING


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

def _to_inst_id(symbol: str) -> str:
    """Convert ccxt symbol 'BTC/USDT' or 'BTC/USDT:USDT' → OKX instId 'BTC-USDT-SWAP'."""
    base = symbol.split("/")[0]
    return f"{base}-USDT-SWAP"


def _qty_to_contracts(ex, symbol: str, quantity: float) -> str:
    """Convert base-asset quantity to OKX contract count string."""
    try:
        mkt_key = f"{symbol}:USDT" if ":" not in symbol else symbol
        market = ex.market(mkt_key)
        ct_val = float(market.get("contractSize") or 1)
        contracts = max(1, round(quantity / ct_val))
        return str(contracts)
    except Exception:
        return str(max(1, round(quantity)))


# ---------------------------------------------------------------------------
# Agent Trade Kit CLI
# ---------------------------------------------------------------------------

def _place_via_agent_trade_kit(
    inst_id: str,
    side: str,
    sz: str,
    pos_side: str = "net",
    td_mode: str = "cross",
    profile: str = "demo",
) -> Dict:
    """
    Submit a market order via the OKX Agent Trade Kit CLI.
    Delegates to _place_via_cli. Kept for backward compatibility.
    """
    return _place_via_cli(inst_id, side, sz, ord_type="market", profile=profile)


def _place_via_cli(
    inst_id: str,
    side: str,
    sz: str,
    ord_type: str = "market",
    price: str = None,
    profile: str = "demo",
) -> Dict:
    """
    Place any order type via OKX CLI. Supports market and limit orders.
    All orders get the agentTradeKit tag automatically.
    """
    cmd = [
        "okx", "--profile", profile, "--json",
        "swap", "place",
        "--instId", inst_id,
        "--side", side,
        "--ordType", ord_type,
        "--sz", sz,
    ]
    if price and ord_type == "limit":
        cmd.extend(["--px", price])

    logger.info("[ATK] %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError("Agent Trade Kit CLI not found. Run: npm install -g @okx_ai/okx-trade-cli")

    output = result.stdout.strip() if result.stdout else ""
    if result.returncode != 0:
        error = result.stderr.strip() or output
        raise RuntimeError(f"Agent Trade Kit CLI error (rc={result.returncode}): {error}")

    try:
        parsed = json.loads(output)
        if isinstance(parsed, list) and parsed:
            entry = parsed[0]
            ord_id = entry.get("ordId", "")
            if ord_id and ord_type == "market":
                # Query fill price for market orders
                try:
                    fill_r = subprocess.run(
                        ["okx", "--profile", profile, "--json", "swap", "get",
                         "--instId", inst_id, "--ordId", ord_id],
                        capture_output=True, text=True, timeout=10,
                    )
                    if fill_r.returncode == 0 and fill_r.stdout.strip():
                        fill_data = json.loads(fill_r.stdout)
                        if isinstance(fill_data, list) and fill_data:
                            fill_data = fill_data[0]
                        fill_px = fill_data.get("avgPx") or fill_data.get("fillPx")
                        if fill_px:
                            return {"id": ord_id, "price": float(fill_px), "status": "filled", "raw": entry}
                except Exception:
                    pass
            return {"id": ord_id, "status": "filled" if ord_type == "market" else "open", "raw": entry}
        return parsed
    except Exception:
        return {"raw": output, "status": "ok"}


# ---------------------------------------------------------------------------
# Exchange client factory (ccxt — market data queries ONLY)
# ---------------------------------------------------------------------------

def create_exchange(mode: str = TRADING_MODE, sandbox: bool = False) -> Any:
    """
    Build a ccxt.okx exchange object for READ-ONLY market data queries.

    Used exclusively for: tickers, orderbooks, OHLCV, contract size lookups.
    NEVER used for order placement — all orders go through the OKX CLI.
    """
    try:
        import ccxt
    except ImportError:
        raise ImportError("ccxt not installed. Run: pip install ccxt")

    creds = {
        "apiKey": OKX_API_KEY,
        "secret": OKX_API_SECRET,
        "password": OKX_PASSPHRASE,
        "enableRateLimit": True,
    }

    default_type = "swap" if mode == "futures" else ("margin" if mode == "margin" else "spot")
    exchange = ccxt.okx({**creds, "options": {"defaultType": default_type}})
    # Prevent ccxt from calling the private /asset/currencies endpoint during
    # load_markets — not needed for trading and fails on restricted sub-accounts.
    exchange.has["fetchCurrencies"] = False
    if sandbox:
        # OKX demo/simulated trading requires this header on all private requests.
        exchange.headers["x-simulated-trading"] = "1"
        logger.info("Exchange client: OKX (%s) [DEMO/SIMULATED]", default_type)
    else:
        logger.info("Exchange client: OKX (%s)", default_type)

    return exchange


# ---------------------------------------------------------------------------
# Broker
# ---------------------------------------------------------------------------

class Broker:
    """
    Abstraction over OKX USDT-M perpetual swap execution.

    In backtest mode (LIVE_TRADING=False): logs orders, returns synthetic fills.
    In live mode (LIVE_TRADING=True): submits orders via Agent Trade Kit CLI
    (required for competition tag compliance) and uses ccxt for data/balance.

    Parameters
    ----------
    mode : str
        "spot" | "futures" | "margin"
    """

    def __init__(self, mode: str = TRADING_MODE, sandbox: bool = False):
        self.mode = mode
        self.sandbox = sandbox
        self.max_leverage = LEVERAGE_LIMITS[mode]
        self.tx_cost_pct  = TRANSACTION_COSTS[mode]
        self._exchange = None   # lazy-initialised

    def _get_exchange(self):
        if self._exchange is None:
            self._exchange = create_exchange(self.mode, sandbox=self.sandbox)
            try:
                self._exchange.load_markets()
            except Exception as e:
                logger.warning("Failed to load markets: %s", e)
        return self._exchange

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    def market_buy(
        self,
        symbol: str,
        quantity: float,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> Dict:
        """
        Submit a market buy order.

        Parameters
        ----------
        symbol    : ccxt format, e.g. "BTC/USDT"
        quantity  : base asset amount
        leverage  : requested leverage (capped at mode limit)
        reduce_only : True to close a short position (futures)
        """
        leverage = min(leverage, self.max_leverage)

        if not _is_live_trading():
            logger.info(
                "[PAPER] MARKET BUY | %s | qty=%.6f | leverage=%.1fx | mode=%s",
                symbol, quantity, leverage, self.mode,
            )
            return self._synthetic_fill("buy", symbol, quantity, leverage)

        # --- Live: route through Agent Trade Kit CLI ---
        inst_id = _to_inst_id(symbol)
        if self.mode == "futures":
            self.set_leverage(symbol, leverage)
        ex = self._get_exchange()
        sz = _qty_to_contracts(ex, symbol, quantity)
        profile = "demo" if self.sandbox else "live"
        order = _place_via_agent_trade_kit(inst_id, "buy", sz, profile=profile)
        logger.info("[LIVE] BUY executed via ATK: %s", order)
        return order

    def market_sell(
        self,
        symbol: str,
        quantity: float,
        leverage: float = 1.0,
        reduce_only: bool = False,
    ) -> Dict:
        """Submit a market sell / short order."""
        leverage = min(leverage, self.max_leverage)

        if not _is_live_trading():
            logger.info(
                "[PAPER] MARKET SELL | %s | qty=%.6f | leverage=%.1fx | mode=%s",
                symbol, quantity, leverage, self.mode,
            )
            return self._synthetic_fill("sell", symbol, quantity, leverage)

        # --- Live: route through Agent Trade Kit CLI ---
        inst_id = _to_inst_id(symbol)
        if self.mode == "futures":
            self.set_leverage(symbol, leverage)
        ex = self._get_exchange()
        sz = _qty_to_contracts(ex, symbol, quantity)
        profile = "demo" if self.sandbox else "live"
        order = _place_via_agent_trade_kit(inst_id, "sell", sz, profile=profile)
        logger.info("[LIVE] SELL executed via ATK: %s", order)
        return order

    def set_leverage(self, symbol: str, leverage: float) -> None:
        """Set leverage via CLI."""
        if self.mode == "spot":
            logger.debug("set_leverage is a no-op in spot mode")
            return
        capped = min(leverage, self.max_leverage)
        if not _is_live_trading():
            logger.info("[PAPER] SET LEVERAGE %s | %.1fx | mode=%s", symbol, capped, self.mode)
            return
        inst_id = _to_inst_id(symbol)
        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "swap", "leverage",
               "--instId", inst_id, "--lever", str(int(capped)), "--mgnMode", "cross"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                logger.warning("set_leverage failed for %s: %s", symbol, (r.stderr or r.stdout)[:200])
        except Exception as e:
            logger.warning("set_leverage exception for %s: %s", symbol, e)

    def set_margin_type(self, symbol: str, margin_type: str = "cross") -> None:
        """Set margin type — always cross for competition, no-op."""
        if self.mode != "futures":
            return
        if not _is_live_trading():
            logger.info("[PAPER] SET MARGIN TYPE %s | %s", symbol, margin_type)
            return
        # Competition uses cross margin exclusively; leverage CLI sets mgnMode=cross
        logger.debug("set_margin_type: cross mode enforced via leverage CLI")

    # ------------------------------------------------------------------
    # Limit orders & order management
    # ------------------------------------------------------------------

    def limit_buy(self, symbol: str, quantity: float, price: float) -> Dict:
        """Place a limit buy order via Agent Trade Kit CLI."""
        if not _is_live_trading():
            logger.info("[PAPER] LIMIT BUY | %s | qty=%.6f | price=%.4f", symbol, quantity, price)
            fill = self._synthetic_fill("buy", symbol, quantity, 1.0)
            fill["price"] = price
            return fill
        inst_id = _to_inst_id(symbol)
        ex = self._get_exchange()
        sz = _qty_to_contracts(ex, symbol, quantity)
        profile = "demo" if self.sandbox else "live"
        order = _place_via_cli(inst_id, "buy", sz, ord_type="limit", price=str(price), profile=profile)
        logger.info("[LIVE] LIMIT BUY placed via ATK: %s", order)
        return order

    def limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        """Place a limit sell order via Agent Trade Kit CLI."""
        if not _is_live_trading():
            logger.info("[PAPER] LIMIT SELL | %s | qty=%.6f | price=%.4f", symbol, quantity, price)
            fill = self._synthetic_fill("sell", symbol, quantity, 1.0)
            fill["price"] = price
            return fill
        inst_id = _to_inst_id(symbol)
        ex = self._get_exchange()
        sz = _qty_to_contracts(ex, symbol, quantity)
        profile = "demo" if self.sandbox else "live"
        order = _place_via_cli(inst_id, "sell", sz, ord_type="limit", price=str(price), profile=profile)
        logger.info("[LIVE] LIMIT SELL placed via ATK: %s", order)
        return order

    def fetch_order_status(self, order_id: str, symbol: str) -> Dict:
        """Check if an order has been filled via CLI."""
        if not _is_live_trading():
            return {"id": order_id, "status": "closed", "filled": 1.0}
        inst_id = _to_inst_id(symbol)
        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "--json", "swap", "get",
               "--instId", inst_id, "--ordId", order_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                if isinstance(data, list) and data:
                    data = data[0]
                state = data.get("state", "")
                return {
                    "id": order_id,
                    "status": "closed" if state == "filled" else state,
                    "filled": float(data.get("accFillSz", 0)),
                    "average": data.get("avgPx"),
                    "price": data.get("avgPx"),
                }
        except Exception as e:
            logger.warning("fetch_order_status failed: %s", e)
        return {"id": order_id, "status": "unknown"}

    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Cancel a specific order via CLI."""
        if not _is_live_trading():
            logger.info("[PAPER] CANCEL ORDER %s | %s", order_id, symbol)
            return
        inst_id = _to_inst_id(symbol)
        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "swap", "cancel", inst_id, "--ordId", order_id]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                logger.warning("cancel_order failed: %s", (r.stderr or r.stdout)[:200])
        except Exception as e:
            logger.warning("cancel_order exception: %s", e)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def get_positions(self) -> List[Dict]:
        """Fetch current open positions via CLI."""
        if not _is_live_trading():
            logger.debug("[PAPER] get_positions → returning empty list")
            return []
        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "--json", "swap", "positions"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception as e:
            logger.warning("get_positions failed: %s", e)
        return []

    def get_balance(self) -> Dict:
        """Fetch account balance via CLI."""
        if not _is_live_trading():
            logger.debug("[PAPER] get_balance → returning placeholder")
            return {"USDT": {"free": 5000.0, "used": 0.0, "total": 5000.0}}
        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "--json", "account", "balance"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception as e:
            logger.warning("get_balance failed: %s", e)
        return {}

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        """Cancel all open orders for a symbol via CLI."""
        if not _is_live_trading():
            logger.info("[PAPER] CANCEL ALL ORDERS | symbol=%s", symbol or "ALL")
            return
        profile = "demo" if self.sandbox else "live"
        if symbol:
            inst_id = _to_inst_id(symbol)
            # List open orders, then cancel each
            cmd = ["okx", "--profile", profile, "--json", "swap", "orders", "--instId", inst_id]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and r.stdout.strip():
                    orders = json.loads(r.stdout)
                    for o in (orders if isinstance(orders, list) else []):
                        oid = o.get("ordId")
                        if oid:
                            self.cancel_order(oid, symbol)
            except Exception as e:
                logger.warning("cancel_all_orders failed: %s", e)

    # ------------------------------------------------------------------
    # Account equity
    # ------------------------------------------------------------------

    _STABLECOINS = {"USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI"}

    def get_total_equity(self) -> float:
        """
        Compute total account equity in USD via CLI.
        """
        if not _is_live_trading():
            return 5000.0

        profile = "demo" if self.sandbox else "live"
        cmd = ["okx", "--profile", profile, "--json", "account", "balance"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                # OKX account balance response format
                if isinstance(data, list) and data:
                    data = data[0]
                total_eq = data.get("totalEq")
                if total_eq:
                    return float(total_eq)
                # Fallback: sum details
                details = data.get("details", [])
                return sum(float(d.get("eqUsd", 0)) for d in details)
        except Exception as e:
            logger.warning("get_total_equity failed: %s", e)
        return 0.0

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict:
        """Get current ticker for a symbol. Uses ccxt for market data (read-only)."""
        ex = self._get_exchange()
        mkt_sym = f"{symbol}:USDT" if self.mode == "futures" and ":" not in symbol else symbol
        return ex.fetch_ticker(mkt_sym)

    def get_orderbook(self, symbol: str, limit: int = 5) -> Dict:
        """Get order book snapshot. Uses ccxt for market data (read-only)."""
        ex = self._get_exchange()
        mkt_sym = f"{symbol}:USDT" if self.mode == "futures" and ":" not in symbol else symbol
        return ex.fetch_order_book(mkt_sym, limit=limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _synthetic_fill(side: str, symbol: str, quantity: float, leverage: float) -> Dict:
        """Return a fake order fill for paper trading."""
        return {
            "id":       f"PAPER-{int(time.time() * 1000)}",
            "symbol":   symbol,
            "side":     side,
            "type":     "market",
            "quantity": quantity,
            "leverage": leverage,
            "status":   "closed",
            "filled":   quantity,
            "price":    None,
        }

    def __repr__(self) -> str:
        live = "LIVE" if LIVE_TRADING else "PAPER"
        return f"<Broker mode={self.mode} status={live} max_leverage={self.max_leverage}x>"
