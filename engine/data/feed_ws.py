"""
data/feed_ws.py
===============
WebSocket live price cache using ccxt.pro.

Runs a background asyncio event loop in a daemon thread.
Subscribes to OKX USDT-M swap ticker streams and keeps
a thread-safe in-memory price cache — no REST calls needed for
risk checks once the cache is warm.

Usage:
    cache = WebSocketPriceCache(symbols, sandbox=True)
    cache.start()                       # blocks up to 15s for first prices

    price = cache.get("BTC/USDT")       # instant, no API call
    prices = cache.get_all()            # snapshot of all cached prices
    cache.update_symbols(new_list)      # takes effect on next reconnect

    cache.stop()
"""

import asyncio
import logging
import threading
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_RECONNECT_DELAY_BASE = 5     # seconds before first reconnect
_MAX_RECONNECT_DELAY = 60     # cap backoff at 60s
_READY_TIMEOUT = 20           # seconds to wait for first prices on start()


class WebSocketPriceCache:
    """
    Thread-safe live price cache backed by ccxt.pro WebSocket streams.

    The background thread runs its own asyncio event loop and continuously
    pumps ticker updates into self._prices. All public methods are safe
    to call from any thread.
    """

    def __init__(
        self,
        symbols: List[str],
        sandbox: bool = True,
    ):
        self._symbols = list(symbols)
        self._sandbox = sandbox

        self._prices: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background WebSocket thread. Blocks until first prices arrive."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="ws-price-cache",
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=_READY_TIMEOUT):
            logger.warning(
                "WebSocket price cache not ready after %ds — "
                "risk checks will fall back to REST until it warms up",
                _READY_TIMEOUT,
            )

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=8)
        logger.info("WebSocket price cache stopped")

    def get(self, symbol: str) -> Optional[float]:
        """Return the latest cached price for *symbol*, or None if not yet seen."""
        with self._lock:
            return self._prices.get(symbol)

    def get_all(self) -> Dict[str, float]:
        """Return a snapshot of all cached prices."""
        with self._lock:
            return dict(self._prices)

    def update_symbols(self, symbols: List[str]) -> None:
        """Replace the symbol list. Takes effect on the next reconnect."""
        self._symbols = list(symbols)

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    @property
    def cached_count(self) -> int:
        with self._lock:
            return len(self._prices)

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._reconnect_loop())
        except Exception as e:
            logger.error("WebSocket background thread crashed: %s", e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _reconnect_loop(self) -> None:
        """Outer reconnect loop — catches errors and backs off."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._stream_tickers()
                attempt = 0  # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._stop_event.is_set():
                    break
                attempt += 1
                delay = min(_RECONNECT_DELAY_BASE * attempt, _MAX_RECONNECT_DELAY)
                logger.warning(
                    "WebSocket error (attempt %d): %s — reconnecting in %ds",
                    attempt, e, delay,
                )
                await asyncio.sleep(delay)

    async def _stream_tickers(self) -> None:
        """Connect and stream ticker updates until an error or stop signal."""
        import ccxt.pro as ccxtpro

        ex = self._make_exchange(ccxtpro)
        try:
            await ex.load_markets()
            watch_syms = self._resolve_symbols(ex)
            if not watch_syms:
                logger.warning("WebSocket: no tradable symbols found, sleeping 30s")
                await asyncio.sleep(30)
                return

            logger.info("WebSocket: streaming %d symbols (sandbox=%s)", len(watch_syms), self._sandbox)

            while not self._stop_event.is_set():
                # watch_tickers returns a dict of all symbols that fired in this batch
                tickers = await ex.watch_tickers(watch_syms)
                updates: Dict[str, float] = {}
                for sym, ticker in tickers.items():
                    last = ticker.get("last")
                    if last:
                        clean = sym.split(":")[0]   # "BTC/USDT:USDT" → "BTC/USDT"
                        updates[clean] = float(last)

                if updates:
                    with self._lock:
                        self._prices.update(updates)
                    if not self._ready_event.is_set():
                        self._ready_event.set()
                        logger.info(
                            "WebSocket price cache warm: %d symbols live",
                            len(self._prices),
                        )
        finally:
            try:
                await ex.close()
            except Exception:
                pass

    def _make_exchange(self, ccxtpro):
        from config.settings import OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE

        config: dict = {
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if OKX_API_KEY:
            config["apiKey"] = OKX_API_KEY
        if OKX_API_SECRET:
            config["secret"] = OKX_API_SECRET
        if OKX_PASSPHRASE:
            config["password"] = OKX_PASSPHRASE

        return ccxtpro.okx(config)

    def _resolve_symbols(self, ex) -> List[str]:
        """
        Translate BTC/USDT → BTC/USDT:USDT for ccxt futures markets.
        Silently skips symbols not found on this exchange/testnet.
        """
        result = []
        for sym in self._symbols:
            candidate = f"{sym}:USDT" if ":" not in sym else sym
            if candidate in ex.markets:
                result.append(candidate)
            elif sym in ex.markets:
                result.append(sym)
        return result
