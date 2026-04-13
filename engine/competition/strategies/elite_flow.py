"""
competition/strategies/elite_flow.py
=====================================
Elite Flow — Multi-level OFI + Crowding + Regime Gate

Successor to Elite Alpha. Uses top-5 book levels for richer OFI,
crowding signals (OI, funding, premium, L/S ratio) for squeeze detection,
and a momentum-regime gate to filter chop.

Signal pipeline:
  Multi-level OFI  (45%)  ─┐
  Crowding model   (35%)  ─┼─→ composite conviction → state machine → ATK CLI
  Regime gate      (20%)  ─┘

Architecture:
  - Standalone async execution loop (NOT BacktestRunner)
  - WebSocket streams: orderbook (depth), trades, 1m OHLCV
  - REST polling: funding rate, open interest, long/short ratio
  - Orders via `okx swap place` CLI (Agent Trade Kit)
  - 15-second reconciliation loop

Entry point:
  run(config, foreground=True)  ← called by main.py _run_custom_strategy
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import signal as _signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Contract values: coins per contract (OKX perpetuals)
CT_VAL: Dict[str, float] = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.10,
    "SOL-USDT-SWAP": 1.00,
    "BNB-USDT-SWAP": 0.10,
    "ADA-USDT-SWAP": 10.0,
    "AVAX-USDT-SWAP": 1.00,
}

DEFAULT_CONFIG: Dict = {
    "symbols":              ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    "base_sz_usdt":         40.0,
    "base_lever":           2,
    "max_lever":            5,
    "max_positions":        1,
    "reconcile_sec":        15,
    "profile":              "live",

    # Multi-level OFI
    "ofi_levels":           5,
    "ofi_decay_lambda":     0.35,
    "ofi_window_sec":       300,
    "taker_window_sec":     1800,

    # Crowding
    "squeeze_threshold":    1.25,
    "crowd_poll_sec":       60,

    # Regime gate
    "rv_low":               0.10,
    "rv_high":              0.95,

    # Composite
    "flow_weight":          0.45,
    "crowd_weight":         0.35,
    "regime_weight":        0.20,
    "flow_min_threshold":   0.40,       # was 0.20 — require strong flow
    "entry_threshold":      0.40,       # was 0.25 — higher conviction to enter
    "full_threshold":       0.60,       # was 0.45 — higher bar for full size

    # Risk
    "stop_loss_pct":        0.020,
    "take_profit_pct":      0.035,
    "max_hold_min":         240,
    "min_hold_min":         3,          # NEW: minimum 3 min hold before exit
    "daily_loss_stop_pct":  0.05,
    "lag_ms":               1500,
    "heartbeat_sec":        60,
    "market_load_timeout_ms": 30000,
    "market_load_retries":  3,
    "market_load_retry_sec": 5,
}


# ---------------------------------------------------------------------------
# Per-symbol live state
# ---------------------------------------------------------------------------

@dataclass
class SymbolState:
    ofi:          "MultiLevelOFICalculator"
    momentum:     "RegimeGate"
    crowding:     "CrowdingModel"

    # Computed scores
    flow_score:   float = 0.0
    crowd_score:  float = 0.0
    regime_score: float = 0.0
    last_price:   Optional[float] = None
    last_book_ts: Optional[float] = None
    last_trade_ts: Optional[float] = None
    last_ohlcv_ts: Optional[float] = None

    # Taker flow
    taker_buys:   "collections.deque" = field(default_factory=lambda: collections.deque())
    taker_sells:  "collections.deque" = field(default_factory=lambda: collections.deque())


# ---------------------------------------------------------------------------
# Multi-Level OFI Calculator
# ---------------------------------------------------------------------------

class MultiLevelOFICalculator:
    """
    Depth-weighted OFI across top L bid/ask levels.
    Downweights deeper levels with exp(-decay * (level-1)).
    Returns Z-score over rolling window.
    """

    def __init__(self, levels: int = 5, decay_lambda: float = 0.35, window_sec: int = 300):
        self._levels = levels
        self._decay = decay_lambda
        self._window_sec = window_sec
        self._weights = [math.exp(-decay_lambda * i) for i in range(levels)]
        self._samples: collections.deque = collections.deque()
        self._prev_bids: Optional[List[Tuple[float, float]]] = None
        self._prev_asks: Optional[List[Tuple[float, float]]] = None

    def update(self, bids: List[list], asks: List[list]) -> Optional[float]:
        """
        Args:
            bids: [[px, sz], ...] top-of-book first, at least self._levels entries
            asks: [[px, sz], ...] top-of-book first
        Returns:
            Z-score of depth-weighted OFI, or None if insufficient data.
        """
        now = time.monotonic()
        L = min(self._levels, len(bids), len(asks))

        if self._prev_bids is None or self._prev_asks is None:
            self._prev_bids = [(float(bids[i][0]), float(bids[i][1])) for i in range(L)]
            self._prev_asks = [(float(asks[i][0]), float(asks[i][1])) for i in range(L)]
            return None

        raw_ofi = 0.0
        new_bids = []
        new_asks = []

        for i in range(L):
            bid_px = float(bids[i][0])
            bid_sz = float(bids[i][1])
            ask_px = float(asks[i][0])
            ask_sz = float(asks[i][1])

            if i < len(self._prev_bids):
                pb_px, pb_sz = self._prev_bids[i]
                e_bid = (bid_sz if bid_px >= pb_px else 0.0) - (pb_sz if bid_px <= pb_px else 0.0)
            else:
                e_bid = 0.0

            if i < len(self._prev_asks):
                pa_px, pa_sz = self._prev_asks[i]
                e_ask = (ask_sz if ask_px <= pa_px else 0.0) - (pa_sz if ask_px >= pa_px else 0.0)
            else:
                e_ask = 0.0

            raw_ofi += self._weights[i] * (e_bid - e_ask)
            new_bids.append((bid_px, bid_sz))
            new_asks.append((ask_px, ask_sz))

        self._prev_bids = new_bids
        self._prev_asks = new_asks

        self._samples.append((now, raw_ofi))
        cutoff = now - self._window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < 10:
            return None

        vals = [s[1] for s in self._samples]
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(variance) if variance > 0 else 0.0
        return 0.0 if std < 1e-9 else (raw_ofi - mean) / std

    @property
    def sample_count(self) -> int:
        return len(self._samples)


# ---------------------------------------------------------------------------
# Crowding Model
# ---------------------------------------------------------------------------

class CrowdingModel:
    """
    Detects one-sided crowding via OI, funding, premium, and L/S ratio.
    Produces a directional crowd_score when a squeeze/flush setup is detected.
    """

    def __init__(self, squeeze_threshold: float = 1.25):
        self._squeeze_threshold = squeeze_threshold
        # Rolling buffers for z-score computation
        self._oi_buf:      collections.deque = collections.deque(maxlen=7 * 24 * 4)  # 7d at 15min
        self._funding_buf: collections.deque = collections.deque(maxlen=30 * 24 * 4)
        self._premium_buf: collections.deque = collections.deque(maxlen=30 * 24 * 4)
        self._lsr_buf:     collections.deque = collections.deque(maxlen=30 * 24 * 4)

        self._last_oi: Optional[float] = None

    def update(
        self,
        open_interest: float,
        funding_rate: float,
        mark_price: float,
        index_price: float,
        long_short_ratio: float,
        breakout_direction: int,  # +1 up, -1 down, 0 no breakout
    ) -> float:
        """Returns crowd_score in [-1, +1]."""
        # OI change
        oi_chg = 0.0
        if self._last_oi is not None and self._last_oi > 0:
            oi_chg = (open_interest - self._last_oi) / self._last_oi
        self._last_oi = open_interest
        self._oi_buf.append(oi_chg)

        self._funding_buf.append(funding_rate)
        premium = (mark_price / index_price - 1.0) if index_price > 0 else 0.0
        self._premium_buf.append(premium)
        lsr_log = math.log(max(long_short_ratio, 0.01))
        self._lsr_buf.append(lsr_log)

        # Z-scores (need sufficient data)
        if len(self._oi_buf) < 20 or len(self._funding_buf) < 20:
            return 0.0

        oi_z = self._zscore(self._oi_buf)
        fund_z = self._zscore(self._funding_buf)
        prem_z = self._zscore(self._premium_buf)
        lsr_z = self._zscore(self._lsr_buf)

        # Directional crowding composites
        long_crowded = (oi_z + fund_z + prem_z + lsr_z) / 4.0
        short_crowded = (oi_z - fund_z - prem_z - lsr_z) / 4.0

        # Squeeze/flush logic
        if breakout_direction > 0 and short_crowded > self._squeeze_threshold:
            return min(short_crowded / 3.0, 1.0)
        elif breakout_direction < 0 and long_crowded > self._squeeze_threshold:
            return -min(long_crowded / 3.0, 1.0)
        return 0.0

    @staticmethod
    def _zscore(buf: collections.deque) -> float:
        if len(buf) < 5:
            return 0.0
        vals = list(buf)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var) if var > 0 else 0.0
        return (vals[-1] - mean) / std if std > 1e-9 else 0.0


# ---------------------------------------------------------------------------
# Regime Gate
# ---------------------------------------------------------------------------

class RegimeGate:
    """
    Momentum + volatility regime filter.
    Skips chop and panic-vol tails; only passes usable trending regimes.
    """

    def __init__(self, rv_low: float = 0.20, rv_high: float = 0.90):
        self._rv_low = rv_low
        self._rv_high = rv_high
        self._closes: collections.deque = collections.deque(maxlen=60)
        self._rv_history: collections.deque = collections.deque(maxlen=30 * 24 * 60)  # 30d of 1-min

    def update(self, close: float) -> Optional[float]:
        """Returns regime_score in [-1, +1] or None if in warmup."""
        self._closes.append(close)
        if len(self._closes) < 60:
            return None

        closes = list(self._closes)

        # Returns for vol
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        rv_60m = np.std(rets) * math.sqrt(60 * 24 * 365) if rets else 0.0
        self._rv_history.append(rv_60m)

        # Percentile rank of current vol
        if len(self._rv_history) < 100:
            rv_rank = 0.5  # default mid
        else:
            hist = sorted(self._rv_history)
            rv_rank = sum(1 for v in hist if v <= rv_60m) / len(hist)

        # Momentum components
        ret_5m = (closes[-1] / closes[-6] - 1.0) if len(closes) >= 6 else 0.0
        ret_30m = (closes[-1] / closes[-31] - 1.0) if len(closes) >= 31 else 0.0

        # Linear regression slope over 60 bars
        x = np.arange(60, dtype=float)
        y = np.array(closes[-60:], dtype=float)
        slope, _ = np.polyfit(x, y, 1)
        slope_norm = slope / closes[-1] if closes[-1] > 0 else 0.0

        trend_score = (
            self._clip(ret_5m / 0.003) +
            self._clip(ret_30m / 0.008) +
            self._clip(slope_norm / 0.005)
        ) / 3.0

        # Regime filter
        if self._rv_low <= rv_rank <= self._rv_high:
            return trend_score
        return 0.0

    @property
    def sample_count(self) -> int:
        return len(self._closes)

    @staticmethod
    def _clip(val: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, val))


# ---------------------------------------------------------------------------
# Strategy Core
# ---------------------------------------------------------------------------

class EliteFlowStrategy:
    """
    Multi-symbol Elite Flow strategy with composite conviction
    and position state machine.
    """

    # Position states
    FLAT  = "FLAT"
    PROBE = "PROBE"
    FULL  = "FULL"

    # State file for restart recovery
    _STATE_FILE = "elite_flow_state.json"

    def __init__(self, config: Optional[Dict] = None):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        profile_override = os.getenv("STRATEGY_PROFILE")
        if profile_override in ("demo", "live"):
            self.cfg["profile"] = profile_override
        self._symbols: List[str] = list(self.cfg["symbols"])

        # Per-symbol state
        self._state: Dict[str, SymbolState] = {
            sym: SymbolState(
                ofi=MultiLevelOFICalculator(
                    levels=self.cfg["ofi_levels"],
                    decay_lambda=self.cfg["ofi_decay_lambda"],
                    window_sec=self.cfg["ofi_window_sec"],
                ),
                momentum=RegimeGate(
                    rv_low=self.cfg["rv_low"],
                    rv_high=self.cfg["rv_high"],
                ),
                crowding=CrowdingModel(
                    squeeze_threshold=self.cfg["squeeze_threshold"],
                ),
            )
            for sym in self._symbols
        }

        # Position tracking
        self._pos_symbol: Optional[str] = None
        self._pos_side: Optional[str] = None    # "long" | "short"
        self._pos_entry: Optional[float] = None
        self._pos_sz: int = 0
        self._pos_state: str = self.FLAT
        self._pos_open_time: float = 0.0

        # Cumulative realized P&L and fees (persists across restarts)
        self._realized_pnl: float = 0.0
        self._last_unrealized_pnl: float = 0.0
        self._total_fees: float = 0.0

        # Daily loss tracking
        self._daily_pnl: float = 0.0
        self._daily_reset_day: int = 0

        # Execution guard
        self._last_order_ts: float = 0.0
        self._order_cooldown_sec: float = 120.0  # 2 min cooldown between trades

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Recover state from previous run
        self._recover_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_sync, daemon=True, name="elite-flow",
        )
        self._thread.start()
        logger.info(
            "EliteFlow started — symbols=%s  lever=%dx  profile=%s",
            self._symbols, self._effective_lever(), self.cfg["profile"],
        )

    def stop(self) -> None:
        self._save_state()
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        logger.info("EliteFlow stopped")

    @property
    def profile(self) -> str:
        return self.cfg["profile"]

    # ------------------------------------------------------------------
    # State persistence & recovery
    # ------------------------------------------------------------------

    def _state_path(self) -> Path:
        if "session_state_file" in self.cfg:
            return Path(self.cfg["session_state_file"])
        return Path(__file__).resolve().parents[2] / "logs" / self._STATE_FILE

    def _save_state(self) -> None:
        """Persist position state + realized PnL to disk."""
        state = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "pos_symbol": self._pos_symbol,
            "pos_side": self._pos_side,
            "pos_entry": self._pos_entry,
            "pos_sz": self._pos_sz,
            "pos_state": self._pos_state,
            "realized_pnl": self._realized_pnl,
            "total_fees": self._total_fees,
            "daily_pnl": self._daily_pnl,
            "daily_reset_day": self._daily_reset_day,
        }
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("EliteFlow: failed to save state: %s", e)

    def _recover_state(self) -> None:
        """
        On startup, recover position state from two sources:
        1. OKX exchange (ground truth for what's actually open)
        2. State file (for realized PnL and metadata)
        """
        # Step 1: Load saved state file for realized PnL history
        saved = self._load_state_file()
        if saved:
            self._realized_pnl = saved.get("realized_pnl", 0.0)
            self._total_fees = saved.get("total_fees", 0.0)
            self._daily_pnl = saved.get("daily_pnl", 0.0)
            self._daily_reset_day = saved.get("daily_reset_day", 0)
            logger.info(
                "EliteFlow: recovered state file — realized_pnl=%.2f  daily_pnl=%.4f",
                self._realized_pnl, self._daily_pnl,
            )

        # Step 2: Query OKX for actual open positions (ground truth)
        okx_pos = self._fetch_okx_positions()
        if okx_pos:
            sym = okx_pos["symbol"]
            self._pos_symbol = sym
            self._pos_side = okx_pos["side"]
            self._pos_entry = okx_pos["entry"]
            self._pos_sz = okx_pos["sz"]
            self._pos_state = self.FULL  # assume FULL if position exists
            self._pos_open_time = time.monotonic()
            logger.info(
                "EliteFlow: recovered OKX position — sym=%s  side=%s  "
                "entry=%.2f  sz=%d",
                sym, okx_pos["side"], okx_pos["entry"], okx_pos["sz"],
            )
        elif saved and saved.get("pos_symbol"):
            logger.info(
                "EliteFlow: state file had position %s but OKX shows none — "
                "starting FLAT (position was likely closed externally)",
                saved["pos_symbol"],
            )
        else:
            logger.info("EliteFlow: starting fresh — no open positions")

    def _load_state_file(self) -> Optional[Dict]:
        path = self._state_path()
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning("EliteFlow: failed to read state file: %s", e)
            return None

    def _fetch_okx_positions(self) -> Optional[Dict]:
        """Query OKX for any open swap positions in our symbol set."""
        try:
            r = subprocess.run(
                ["okx", "--profile", self.profile, "--json", "swap", "positions"],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                logger.warning("EliteFlow: positions query failed: %s", r.stderr[:200])
                return None

            positions = json.loads(r.stdout) if r.stdout.strip() else []
            if not isinstance(positions, list):
                positions = [positions] if positions else []

            for p in positions:
                inst = p.get("instId", "")
                pos_amt = float(p.get("pos", 0))
                if inst in self._symbols and pos_amt != 0:
                    side = "long" if pos_amt > 0 else "short"
                    entry = float(p.get("avgPx", 0))
                    return {
                        "symbol": inst,
                        "side": side,
                        "entry": entry,
                        "sz": abs(int(pos_amt)),
                    }
            return None
        except Exception as e:
            logger.warning("EliteFlow: positions fetch error: %s", e)
            return None

    # ------------------------------------------------------------------
    # Thread → asyncio bridge
    # ------------------------------------------------------------------

    def _run_sync(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except Exception as e:
            logger.error("EliteFlow crashed: %s", e, exc_info=True)
        finally:
            loop.close()

    async def _load_markets_with_retry(self, ex) -> None:
        attempts = max(1, int(self.cfg.get("market_load_retries", 1)))
        delay = float(self.cfg.get("market_load_retry_sec", 5))

        for attempt in range(1, attempts + 1):
            try:
                await ex.load_markets(reload=(attempt > 1))
                return
            except Exception as e:
                if attempt >= attempts or self._stop_event.is_set():
                    raise
                logger.warning(
                    "EliteFlow: load_markets failed (%s/%s): %s. Retrying in %.1fs",
                    attempt,
                    attempts,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Main async loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        import ccxt.pro as ccxtpro
        from config.settings import OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE

        ex_cfg: Dict = {
            "enableRateLimit": True,
            "timeout": int(self.cfg.get("market_load_timeout_ms", 30000)),
            "options": {"defaultType": "swap"},
        }
        if OKX_API_KEY:
            ex_cfg["apiKey"] = OKX_API_KEY
        if OKX_API_SECRET:
            ex_cfg["secret"] = OKX_API_SECRET
        if OKX_PASSPHRASE:
            ex_cfg["password"] = OKX_PASSPHRASE
        if self.profile == "demo":
            ex_cfg["headers"] = {"x-simulated-trading": "1"}

        ex = ccxtpro.okx(ex_cfg)
        ex.has["fetchCurrencies"] = False

        try:
            await self._load_markets_with_retry(ex)
            logger.info("EliteFlow: markets loaded — tracking %s", self._symbols)

            lever = self._effective_lever()
            for sym in self._symbols:
                self._set_leverage(sym, lever)

            # Launch tasks: 3 WS streams per symbol + crowd poller + reconciler
            tasks: List[asyncio.Task] = []
            for sym in self._symbols:
                ccxt_sym = self._to_ccxt(sym)
                tasks += [
                    asyncio.create_task(self._stream_orderbook(ex, sym, ccxt_sym)),
                    asyncio.create_task(self._stream_trades(ex, sym, ccxt_sym)),
                    asyncio.create_task(self._stream_ohlcv(ex, sym, ccxt_sym)),
                ]
            tasks.append(asyncio.create_task(self._crowd_poll_loop(ex)))
            tasks.append(asyncio.create_task(self._reconcile_loop()))
            tasks.append(asyncio.create_task(self._diagnostics_loop()))

            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        finally:
            try:
                await ex.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # WebSocket stream handlers
    # ------------------------------------------------------------------

    async def _stream_orderbook(self, ex, sym: str, ccxt_sym: str) -> None:
        st = self._state[sym]
        while not self._stop_event.is_set():
            try:
                ob = await ex.watch_order_book(ccxt_sym, limit=self.cfg["ofi_levels"])
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                if len(bids) < 1 or len(asks) < 1:
                    continue

                st.last_price = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                st.last_book_ts = time.monotonic()

                # Check lag
                ts_ms = ob.get("timestamp")
                if ts_ms and (time.time() * 1000.0 - ts_ms) > self.cfg["lag_ms"]:
                    continue

                ofi_z = st.ofi.update(bids, asks)
                if ofi_z is not None:
                    # Compute taker z-score
                    taker_z = self._taker_zscore(st)
                    # flow_score = 0.7 * ofi_z_clipped + 0.3 * taker_z_clipped
                    st.flow_score = (
                        0.7 * max(-1.0, min(1.0, ofi_z / 3.0)) +
                        0.3 * max(-1.0, min(1.0, taker_z / 3.0))
                    )
                    await self._evaluate_signals(sym)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow [%s]: orderbook error: %s", sym, e)
                await asyncio.sleep(2)

    async def _stream_trades(self, ex, sym: str, ccxt_sym: str) -> None:
        st = self._state[sym]
        while not self._stop_event.is_set():
            try:
                trades = await ex.watch_trades(ccxt_sym)
                now = time.monotonic()
                for trade in trades:
                    amount = float(trade.get("amount", 0.0))
                    side = trade.get("side", "")
                    if side == "buy":
                        st.taker_buys.append((now, amount))
                    elif side == "sell":
                        st.taker_sells.append((now, amount))
                if trades:
                    st.last_trade_ts = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow [%s]: trades error: %s", sym, e)
                await asyncio.sleep(2)

    async def _stream_ohlcv(self, ex, sym: str, ccxt_sym: str) -> None:
        st = self._state[sym]
        while not self._stop_event.is_set():
            try:
                candles = await ex.watch_ohlcv(ccxt_sym, "1m")
                if candles and len(candles) >= 2:
                    close = float(candles[-2][4])
                    st.last_ohlcv_ts = time.monotonic()
                    result = st.momentum.update(close)
                    if result is not None:
                        st.regime_score = result
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow [%s]: ohlcv error: %s", sym, e)
                await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # REST polling: crowding data
    # ------------------------------------------------------------------

    async def _crowd_poll_loop(self, ex) -> None:
        """Poll funding, OI, and L/S ratio via REST every crowd_poll_sec."""
        interval = self.cfg["crowd_poll_sec"]
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self._stop_event.is_set():
                    break

                for sym in self._symbols:
                    st = self._state[sym]
                    ccxt_sym = self._to_ccxt(sym)

                    # Fetch data via REST
                    oi = await self._fetch_open_interest(ex, ccxt_sym)
                    funding = await self._fetch_funding_rate(ex, ccxt_sym)
                    mark_px = st.last_price or 0.0
                    index_px = await self._fetch_index_price(ex, sym)
                    lsr = await self._fetch_long_short_ratio(ex, sym)

                    # Detect breakout direction from regime gate
                    breakout_dir = 0
                    if st.regime_score > 0.3:
                        breakout_dir = 1
                    elif st.regime_score < -0.3:
                        breakout_dir = -1

                    st.crowd_score = st.crowding.update(
                        open_interest=oi,
                        funding_rate=funding,
                        mark_price=mark_px,
                        index_price=index_px,
                        long_short_ratio=lsr,
                        breakout_direction=breakout_dir,
                    )

                    if abs(st.crowd_score) > 0.1:
                        logger.info(
                            "EliteFlow [%s]: crowd_score=%.3f  oi=%.0f  funding=%.6f  lsr=%.2f",
                            sym, st.crowd_score, oi, funding, lsr,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow: crowd poll error: %s", e)

    async def _fetch_open_interest(self, ex, ccxt_sym: str) -> float:
        try:
            result = await ex.fetch_open_interest(ccxt_sym)
            return float(result.get("openInterestAmount") or result.get("openInterestValue") or result.get("openInterest", 0.0))
        except Exception as e:
            logger.debug("EliteFlow: OI fetch error for %s: %s", ccxt_sym, e)
        return 0.0

    async def _fetch_funding_rate(self, ex, ccxt_sym: str) -> float:
        try:
            result = await ex.fetch_funding_rate(ccxt_sym)
            return float(result.get("fundingRate", 0.0))
        except Exception as e:
            logger.debug("EliteFlow: funding fetch error: %s", e)
            return 0.0

    async def _fetch_index_price(self, ex, sym: str) -> float:
        try:
            cmd = ["okx", "--json", "market", "index-ticker", "--instId", self._to_index_instid(sym)]
            r = await asyncio.get_event_loop().run_in_executor(
                None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            )
            if r.returncode == 0 and r.stdout.strip():
                import json
                parsed = json.loads(r.stdout)
                data = self._unwrap_public_data(parsed)
                if data:
                    return float(data[0].get("idxPx", data[0].get("last", 0)))
        except Exception as e:
            logger.debug("EliteFlow: index price fetch error for %s: %s", sym, e)
        return 0.0

    async def _fetch_long_short_ratio(self, ex, sym: str) -> float:
        """Fetch contract long/short ratio via OKX Rubik endpoint."""
        try:
            rubik_call = getattr(ex, "publicGetRubikStatContractsLongShortAccountRatioContract", None)
            if rubik_call is None:
                rubik_call = getattr(ex, "public_get_rubik_stat_contracts_long_short_account_ratio_contract", None)
            if rubik_call is None:
                raise AttributeError("Rubik long/short endpoint not available on exchange object")

            response = await rubik_call({
                "instId": sym,
                "period": "5m",
                "limit": 1,
            })
            data = response.get("data", [])
            if data:
                return float(data[-1][1])
        except Exception as e:
            logger.debug("EliteFlow: long/short ratio fetch error for %s: %s", sym, e)
        return 1.0  # neutral default

    # ------------------------------------------------------------------
    # Signal evaluation + state machine
    # ------------------------------------------------------------------

    async def _evaluate_signals(self, trigger_sym: str) -> None:
        """Score all symbols, pick best, apply state machine."""
        # Daily loss check
        now = time.time()
        today = int(now / 86400)
        if today != self._daily_reset_day:
            self._daily_pnl = 0.0
            self._daily_reset_day = today

        if self._daily_pnl <= -self.cfg["daily_loss_stop_pct"]:
            return

        best_sym = None
        best_conviction = 0.0
        best_dir = 0

        for sym, st in self._state.items():
            if st.last_price is None:
                continue

            raw = (
                self.cfg["flow_weight"] * st.flow_score +
                self.cfg["crowd_weight"] * st.crowd_score +
                self.cfg["regime_weight"] * st.regime_score
            )

            # Direction agreement gate — BLOCK if flow and regime disagree
            if st.flow_score != 0 and st.regime_score != 0:
                if (st.flow_score > 0) != (st.regime_score > 0):
                    raw = 0.0  # full block, was 0.25x penalty

            # Flow minimum
            if abs(st.flow_score) < self.cfg["flow_min_threshold"]:
                raw = 0.0

            conviction = max(-1.0, min(1.0, raw))

            if abs(conviction) > abs(best_conviction):
                best_conviction = conviction
                best_sym = sym
                best_dir = 1 if conviction > 0 else -1

        if best_sym is None or abs(best_conviction) < self.cfg["entry_threshold"]:
            return

        # State machine
        if abs(best_conviction) >= self.cfg["full_threshold"]:
            target_state = self.FULL
        elif abs(best_conviction) >= self.cfg["entry_threshold"]:
            target_state = self.PROBE
        else:
            target_state = self.FLAT

        target_side = "long" if best_dir > 0 else "short"

        # Already in this exact position and state — skip
        if (self._pos_symbol == best_sym and
                self._pos_side == target_side and
                self._pos_state == target_state):
            return

        # Same symbol, same direction, just PROBE↔FULL — don't close+reopen
        # (avoids paying 0.10% round-trip fee for a size adjustment)
        if (self._pos_symbol == best_sym and
                self._pos_side == target_side and
                self._pos_state in (self.PROBE, self.FULL) and
                target_state in (self.PROBE, self.FULL)):
            self._pos_state = target_state  # just update state label
            return

        # Minimum hold time — don't exit a position too early
        min_hold_sec = self.cfg.get("min_hold_min", 3) * 60
        if self._pos_symbol is not None and self._pos_open_time > 0:
            held_sec = time.monotonic() - self._pos_open_time
            if held_sec < min_hold_sec:
                return

        # Cooldown between trades
        if time.monotonic() - self._last_order_ts < self._order_cooldown_sec:
            return

        st = self._state[best_sym]
        price = st.last_price

        logger.info(
            "EliteFlow: sym=%s  conviction=%.3f  state=%s→%s  "
            "flow=%.3f  crowd=%.3f  regime=%.3f",
            best_sym, best_conviction, self._pos_state, target_state,
            st.flow_score, st.crowd_score, st.regime_score,
        )

        # Close existing if switching symbol or direction
        if self._pos_symbol is not None:
            if (self._pos_symbol != best_sym or self._pos_side != target_side):
                closed = await asyncio.get_event_loop().run_in_executor(
                    None, self._close_position,
                )
                if not closed:
                    return

        # Calculate size based on state machine
        sz = self._calculate_sz(best_sym, price, target_state, abs(st.crowd_score))
        if sz < 1:
            logger.warning("EliteFlow [%s]: sz=%d < 1 — skipping", best_sym, sz)
            return

        atk_side = "buy" if target_side == "long" else "sell"
        success = await asyncio.get_event_loop().run_in_executor(
            None, self._place_order, best_sym, atk_side, sz,
        )
        if success:
            self._pos_symbol = best_sym
            self._pos_side = target_side
            self._pos_entry = price
            self._pos_sz = sz
            self._pos_state = target_state
            self._pos_open_time = time.monotonic()
            self._last_order_ts = time.monotonic()
            self._save_state()

    async def _diagnostics_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.cfg["heartbeat_sec"])
                if self._stop_event.is_set():
                    break
                for sym, st in self._state.items():
                    logger.info("EliteFlow diag [%s]: %s", sym, self._status_summary(st))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow: diagnostics error: %s", e)

    # ------------------------------------------------------------------
    # Reconciliation (stop-loss, take-profit, max hold, exit logic)
    # ------------------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.cfg["reconcile_sec"])
                if self._stop_event.is_set():
                    break
                await asyncio.get_event_loop().run_in_executor(None, self._reconcile)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("EliteFlow: reconcile error: %s", e)

    def _reconcile(self) -> None:
        if self._pos_symbol is None or self._pos_entry is None:
            # Still write summary when flat so dashboard stays current
            self._write_summary(pnl=0.0, pnl_pct=0.0)
            return

        st = self._state[self._pos_symbol]
        if st.last_price is None:
            return

        price = st.last_price
        entry = self._pos_entry
        raw = (price - entry) / entry
        pnl_pct = raw if self._pos_side == "long" else -raw
        minutes_held = (time.monotonic() - self._pos_open_time) / 60.0

        logger.info(
            "EliteFlow reconcile: sym=%s  side=%s  state=%s  entry=%.2f  price=%.2f  "
            "pnl=%.2f%%  held=%.0fm  flow=%.3f  crowd=%.3f  regime=%.3f",
            self._pos_symbol, self._pos_side, self._pos_state,
            entry, price, pnl_pct * 100, minutes_held,
            st.flow_score, st.crowd_score, st.regime_score,
        )
        capital = self.cfg.get("capital", 300.0)
        self._write_summary(pnl=capital * pnl_pct, pnl_pct=pnl_pct)

        # Exit conditions (Signal 6 from spec)
        pos_dir = 1 if self._pos_side == "long" else -1
        exit_now = any([
            # Flow reversal
            (pos_dir > 0 and st.flow_score < -0.50) or (pos_dir < 0 and st.flow_score > 0.50),
            # Stop loss
            pnl_pct <= -self.cfg["stop_loss_pct"],
            # Take profit with fading flow
            pnl_pct >= self.cfg["take_profit_pct"] and abs(st.flow_score) < 0.25,
            # Max hold time
            minutes_held >= self.cfg["max_hold_min"],
        ])

        # Save state every reconcile cycle
        self._save_state()

        if exit_now:
            reason = "unknown"
            if pnl_pct <= -self.cfg["stop_loss_pct"]:
                reason = "STOP_LOSS"
            elif pnl_pct >= self.cfg["take_profit_pct"]:
                reason = "TAKE_PROFIT"
            elif minutes_held >= self.cfg["max_hold_min"]:
                reason = "MAX_HOLD"
            else:
                reason = "FLOW_REVERSAL"

            logger.warning(
                "EliteFlow: EXIT %s  sym=%s  pnl=%.2f%%  reason=%s",
                self._pos_side, self._pos_symbol, pnl_pct * 100, reason,
            )
            capital = self.cfg.get("capital", 300.0)
            self._realized_pnl += capital * pnl_pct
            self._daily_pnl += pnl_pct
            self._close_position()
            self._save_state()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _taker_zscore(self, st: SymbolState) -> float:
        """Taker buy - sell volume z-score over rolling window."""
        now = time.monotonic()
        cutoff = now - self.cfg["taker_window_sec"]

        # Trim old entries
        while st.taker_buys and st.taker_buys[0][0] < cutoff:
            st.taker_buys.popleft()
        while st.taker_sells and st.taker_sells[0][0] < cutoff:
            st.taker_sells.popleft()

        # 30-second buckets for z-score
        bucket_sec = 30
        n_buckets = self.cfg["taker_window_sec"] // bucket_sec
        deltas = []
        for i in range(n_buckets):
            t_start = now - (i + 1) * bucket_sec
            t_end = now - i * bucket_sec
            buy_vol = sum(amt for ts, amt in st.taker_buys if t_start <= ts < t_end)
            sell_vol = sum(amt for ts, amt in st.taker_sells if t_start <= ts < t_end)
            deltas.append(buy_vol - sell_vol)

        if len(deltas) < 5:
            return 0.0
        mean = sum(deltas) / len(deltas)
        var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        std = math.sqrt(var) if var > 0 else 0.0
        return (deltas[0] - mean) / std if std > 1e-9 else 0.0

    def _status_summary(self, st: SymbolState) -> str:
        reasons: List[str] = []
        if st.last_book_ts is None:
            reasons.append("waiting_orderbook")
        if st.last_trade_ts is None:
            reasons.append("waiting_trades")
        if st.last_ohlcv_ts is None:
            reasons.append("waiting_ohlcv")
        if st.ofi.sample_count < 10:
            reasons.append(f"ofi_warmup={st.ofi.sample_count}/10")
        if st.momentum.sample_count < 60:
            reasons.append(f"regime_warmup={st.momentum.sample_count}/60")
        if st.last_price is not None and not reasons:
            reasons.append("ready")

        return (
            (f"price={st.last_price:.2f} " if st.last_price is not None else "price=NA ") +
            f"flow={st.flow_score:.3f} crowd={st.crowd_score:.3f} regime={st.regime_score:.3f} "
            f"reasons={','.join(reasons)}"
        )

    def _calculate_sz(self, sym: str, price: float, state: str, crowd_boost_raw: float) -> int:
        """
        Sizing from spec:
          base_notional * size_mult * (1 + crowd_boost)
          then convert to contracts
        """
        size_mult = {"FLAT": 0.0, "PROBE": 0.5, "FULL": 1.0}.get(state, 0.0)
        crowd_boost = min(max(abs(crowd_boost_raw) - 0.5, 0), 0.5)
        notional = self.cfg["base_sz_usdt"] * size_mult * (1.0 + crowd_boost) * self._effective_lever()

        ct_val = CT_VAL.get(sym, 0.01)
        return max(1, round(notional / (price * ct_val))) if notional > 0 else 0

    def _effective_lever(self) -> int:
        return min(self.cfg["base_lever"], self.cfg["max_lever"])

    @staticmethod
    def _to_ccxt(instid: str) -> str:
        """BTC-USDT-SWAP → BTC/USDT:USDT"""
        parts = instid.split("-")
        return f"{parts[0]}/{parts[1]}:{parts[1]}"

    @staticmethod
    def _to_index_instid(instid: str) -> str:
        """BTC-USDT-SWAP → BTC-USDT for index ticker queries."""
        parts = instid.split("-")
        return f"{parts[0]}-{parts[1]}"

    @staticmethod
    def _unwrap_public_data(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
        return []

    # ------------------------------------------------------------------
    # ATK CLI helpers
    # ------------------------------------------------------------------

    def _place_order(self, sym: str, side: str, sz: int) -> bool:
        cmd = [
            "okx", "--profile", self.profile, "--json",
            "swap", "place",
            "--instId", sym, "--side", side, "--ordType", "market",
            "--sz", str(sz), "--posSide", "net", "--tdMode", "cross",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.error("EliteFlow [%s]: place failed: %s", sym, (r.stderr or r.stdout)[:400])
                return False
            logger.info("EliteFlow [%s]: placed side=%s sz=%d → %s", sym, side, sz, r.stdout[:200])
            # Estimate and track fee: notional * 0.05% taker fee
            ct_val = CT_VAL.get(sym, 0.01)
            price = self._pos_entry or 0
            if price <= 0:
                # Try to get price from order response
                try:
                    parsed = json.loads(r.stdout)
                    ord_id = parsed[0].get("ordId", "") if isinstance(parsed, list) else ""
                    if ord_id:
                        fill_r = subprocess.run(
                            ["okx", "--profile", self.profile, "--json", "swap", "get",
                             "--instId", sym, "--ordId", ord_id],
                            capture_output=True, text=True, timeout=10,
                        )
                        if fill_r.returncode == 0:
                            fd = json.loads(fill_r.stdout)
                            if isinstance(fd, list) and fd:
                                fd = fd[0]
                            price = float(fd.get("avgPx", 0) or 0)
                except Exception:
                    pass
            if price > 0:
                notional = sz * ct_val * price
                fee = notional * 0.0005  # 0.05% taker fee
                self._total_fees += fee
            return True
        except Exception as e:
            logger.error("EliteFlow [%s]: place exception: %s", sym, e)
            return False

    def _close_position(self) -> bool:
        sym = self._pos_symbol
        if sym is None:
            return True
        # Try okx swap close first
        cmd = [
            "okx", "--profile", self.profile, "--json",
            "swap", "close",
            "--instId", sym, "--mgnMode", "cross", "--posSide", "net",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                logger.info("EliteFlow [%s]: closed → %s", sym, r.stdout[:200])
                self._pos_symbol = None
                self._pos_side = None
                self._pos_entry = None
                self._pos_sz = 0
                self._pos_state = self.FLAT
                return True
            # Fallback: place opposite-side market order to flatten
            logger.warning("EliteFlow [%s]: close cmd failed, using market flatten", sym)
            return self._flatten_with_market_order(sym)
        except Exception as e:
            logger.error("EliteFlow [%s]: close exception: %s", sym, e)
            return self._flatten_with_market_order(sym)

    def _flatten_with_market_order(self, sym: str) -> bool:
        """Fallback close: query actual position and place opposite-side order."""
        try:
            # Check if there's actually a position on-exchange
            r = subprocess.run(
                ["okx", "--profile", self.profile, "--json", "swap", "positions", sym],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0 or not r.stdout.strip():
                # No position on-exchange — just clear internal state
                logger.info("EliteFlow [%s]: no exchange position, clearing internal state", sym)
                self._pos_symbol = None
                self._pos_side = None
                self._pos_entry = None
                self._pos_sz = 0
                self._pos_state = self.FLAT
                return True

            positions = json.loads(r.stdout) if r.stdout.strip() else []
            if not positions:
                self._pos_symbol = None
                self._pos_side = None
                self._pos_entry = None
                self._pos_sz = 0
                self._pos_state = self.FLAT
                return True

            pos = positions[0] if isinstance(positions, list) else positions
            pos_sz = abs(int(float(pos.get("pos", pos.get("size", 0)))))
            if pos_sz == 0:
                self._pos_symbol = None
                self._pos_side = None
                self._pos_entry = None
                self._pos_sz = 0
                self._pos_state = self.FLAT
                return True

            # Determine opposite side
            pos_side_raw = pos.get("posSide", pos.get("side", "net"))
            cur_pos = float(pos.get("pos", pos.get("size", 0)))
            flatten_side = "sell" if cur_pos > 0 else "buy"

            cmd = [
                "okx", "--profile", self.profile, "--json",
                "swap", "place",
                "--instId", sym, "--side", flatten_side,
                "--ordType", "market", "--sz", str(pos_sz),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                logger.info("EliteFlow [%s]: flattened via market %s sz=%d → %s",
                            sym, flatten_side, pos_sz, r.stdout[:200])
                self._pos_symbol = None
                self._pos_side = None
                self._pos_entry = None
                self._pos_sz = 0
                self._pos_state = self.FLAT
                return True
            else:
                logger.error("EliteFlow [%s]: flatten failed: %s", sym, (r.stderr or r.stdout)[:400])
                return False
        except Exception as e:
            logger.error("EliteFlow [%s]: flatten exception: %s", sym, e)
            return False

    def get_snapshot(self) -> Dict:
        """Return portfolio snapshot dict for dashboard / session daemon."""
        capital = self.cfg.get("capital", 300.0)
        unrealized = self._last_unrealized_pnl
        total_pnl = self._realized_pnl + unrealized - self._total_fees
        nav = capital + total_pnl

        long_exp = short_exp = 0.0
        n_pos = 0
        if self._pos_symbol and self._pos_entry and self._pos_sz > 0:
            ct_val = CT_VAL.get(self._pos_symbol, 0.01)
            notional = self._pos_sz * ct_val * (self._pos_entry or 0)
            if self._pos_side == "long":
                long_exp = notional
            else:
                short_exp = notional
            n_pos = 1

        # Build positions dict matching bar strategy format for dashboard
        positions = {}
        if self._pos_symbol and self._pos_entry and self._pos_sz > 0:
            ct_val = CT_VAL.get(self._pos_symbol, 0.01)
            mark = self._pos_entry  # best we have without a fresh price
            notional = self._pos_sz * ct_val * mark
            weight = notional / nav if nav > 0 else 0.0
            upnl = unrealized
            positions[self._pos_symbol] = {
                "qty":    self._pos_sz * ct_val * (1 if self._pos_side == "long" else -1),
                "entry":  round(self._pos_entry, 4),
                "mark":   round(mark, 4),
                "notional": round(notional, 2),
                "weight": round(weight, 4),
                "upnl":   round(upnl, 2),
                "side":   self._pos_side or "flat",
            }

        session_id = self.cfg.get("session_id", "elite_flow")
        return {
            "portfolio_id":    session_id,
            "strategy_id":     "elite_flow",
            "nav":             round(nav, 2),
            "capital":         round(capital, 2),
            "pnl":             round(total_pnl, 2),
            "pnl_pct":         round(total_pnl / capital * 100, 4) if capital > 0 else 0.0,
            "drawdown_pct":    0.0,
            "peak_nav":        round(nav, 2),
            "gross_exp":       round(long_exp + short_exp, 2),
            "net_exp":         round(long_exp - short_exp, 2),
            "long_exposure":   round(long_exp, 2),
            "short_exposure":  round(short_exp, 2),
            "n_positions":     n_pos,
            "positions":       positions,
            "strategy":        "elite_flow",
            "session_id":      session_id,
            "pos_symbol":      self._pos_symbol,
            "pos_side":        self._pos_side,
            "pos_state":       self._pos_state,
            "risk":            {"cb": "NORMAL", "vol": "MEDIUM", "scalar": 1.0},
            "total_fees":      round(self._total_fees, 4),
        }

    def _write_summary(self, pnl: float = 0.0, pnl_pct: float = 0.0) -> None:
        """Write summary.json directly (only when not managed by session daemon)."""
        # Store latest unrealized PnL for get_snapshot()
        self._last_unrealized_pnl = pnl

        # When running under session daemon, it handles summary.json
        if "session_id" in self.cfg:
            return

        portfolio = self.get_snapshot()
        session_id = self.cfg.get("session_id", "elite_flow")

        summary = {
            "updated_at":    datetime.now(timezone.utc).isoformat(),
            "engine_status": "running",
            "pid":           os.getpid(),
            "strategy":      "elite_flow",
            "portfolios":    {session_id: portfolio},
            "total_nav":     portfolio["nav"],
            "total_capital": portfolio["capital"],
            "total_pnl":     portfolio["pnl"],
            "total_pnl_pct": portfolio["pnl_pct"],
        }

        logs_dir = Path(__file__).resolve().parents[2] / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        tmp = logs_dir / "summary.json.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2)
            os.replace(tmp, logs_dir / "summary.json")
        except Exception as e:
            logger.warning("EliteFlow: failed to write summary.json: %s", e)

    def _set_leverage(self, sym: str, lever: int) -> None:
        cmd = [
            "okx", "--profile", self.profile,
            "swap", "set-leverage",
            "--instId", sym, "--lever", str(lever), "--mgnMode", "cross",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                logger.warning("EliteFlow [%s]: set-leverage failed: %s", sym, (r.stderr or r.stdout)[:200])
            else:
                logger.info("EliteFlow [%s]: leverage set to %dx", sym, lever)
        except Exception as e:
            logger.warning("EliteFlow [%s]: set-leverage exception: %s", sym, e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: Optional[Dict] = None, foreground: bool = True) -> Optional[EliteFlowStrategy]:
    """Start Elite Flow. Called by main.py _run_custom_strategy."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    strategy = EliteFlowStrategy(config)
    strategy.start()

    if not foreground:
        return strategy

    stop_flag = threading.Event()

    def _on_signal(sig, frame):
        logger.info("EliteFlow: signal %d received — shutting down", sig)
        stop_flag.set()

    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    logger.info("EliteFlow running in foreground. Ctrl+C to stop.")
    try:
        while not stop_flag.is_set() and not strategy._stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        strategy.stop()

    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Elite Flow strategy runner")
    parser.add_argument("--symbols", nargs="+", default=["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
    parser.add_argument("--profile", default="live", choices=["demo", "live"])
    args = parser.parse_args()

    run(config={"symbols": args.symbols, "profile": args.profile})
