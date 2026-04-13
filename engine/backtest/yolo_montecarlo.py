"""
backtest/yolo_montecarlo.py
============================
Monte Carlo backtester for the YOLO Momentum strategy.

Design:
  - Random start dates across a historical window
  - Each trial simulates a full 14-day YOLO run with martingale doubling
  - Survivorship-bias-free: only trades symbols with data at that date
  - Realistic fees (taker 0.05%) and slippage (sqrt market impact)
  - No forward-looking: signals computed from data up to current bar only
  - Records detailed per-trial results to CSV for statistical analysis

Usage:
  python3 -m backtest.yolo_montecarlo --trials 500 --output results/yolo_mc.csv
  python3 -m backtest.yolo_montecarlo --trials 500 --summary  # print stats
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add engine root to path
ENGINE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_DIR))

from data.fetcher import fetch_ohlcv
from config.settings import TRANSACTION_COSTS, TAKER_FEE, SLIPPAGE_FACTOR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Historical data window — OKX perps have data from ~mid 2021
DATA_START = "2022-01-01"
DATA_END = "2026-03-31"

# Timeframe for simulation: 1h bars give enough granularity for
# momentum signals without being overwhelming
SIM_TIMEFRAME = "1h"

# Broad universe — all major USDT perps historically available on OKX.
# We fetch all of these, and at each trial start date, we filter to
# only those with actual data (= were listed and liquid at that time).
UNIVERSE = [
    # Large caps
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "DOT/USDT", "LINK/USDT",
    "MATIC/USDT", "UNI/USDT", "ATOM/USDT", "LTC/USDT", "FIL/USDT",
    "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "SUI/USDT",
    # Mid caps
    "TRX/USDT", "ETC/USDT", "AAVE/USDT", "MKR/USDT", "SNX/USDT",
    "CRV/USDT", "LDO/USDT", "IMX/USDT", "INJ/USDT", "SEI/USDT",
    "TIA/USDT", "STX/USDT", "RUNE/USDT", "FTM/USDT", "ALGO/USDT",
    # Small / meme / alt
    "PEPE/USDT", "WIF/USDT", "BONK/USDT", "FLOKI/USDT", "SHIB/USDT",
    "1000SATS/USDT", "ORDI/USDT", "WLD/USDT", "JTO/USDT", "PYTH/USDT",
    "BLUR/USDT", "MEME/USDT", "CFX/USDT", "ACE/USDT", "PEOPLE/USDT",
    "GALA/USDT", "SAND/USDT", "MANA/USDT", "AXS/USDT", "ENS/USDT",
]

# Stablecoins / excluded — should not be in UNIVERSE but guard anyway
_EXCLUDE = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDD"}

# Strategy parameters (mirrors yolo_momentum.py defaults)
ROUND_MARGINS = [50, 100, 200, 400]
TARGET_ROI_PCT = 0.20
DEFAULT_LEVER = 50
HIGH_VOL_LEVER = 30
LOW_VOL_LEVER = 75
TAKER_FEE_PCT = 0.0005      # 0.05% per side (taker)
SLIPPAGE_BASE_BPS = 3.0     # 3 bps base slippage
TRIAL_DURATION_DAYS = 14    # competition window

# Signal thresholds
EMA_ALIGNMENT_MIN = 0.60
RSI_LONG_RANGE = (55, 75)
RSI_SHORT_RANGE = (25, 45)
VOLUME_MULT_THRESHOLD = 1.2
HARD_STOP_PCT = 0.60        # stop at -60% of round margin
TRAIL_ACTIVATE_PCT = 0.50   # trail after 50% of target
TRAIL_DISTANCE_PCT = 0.40   # keep 60% of peak
TIME_DECAY_HOURS = 96       # 4 days (in hours)


# ---------------------------------------------------------------------------
# Technical analysis (pure numpy, no CLI calls)
# ---------------------------------------------------------------------------

def ema(arr: np.ndarray, period: int) -> np.ndarray:
    """EMA over a 1-D array. Returns array of same length (NaN-padded)."""
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = np.mean(arr[:period])
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes: np.ndarray, period: int = 14) -> float:
    """RSI from close array. Returns latest value or NaN."""
    if len(closes) < period + 1:
        return np.nan
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd_hist(closes: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9) -> float:
    """MACD histogram (latest value) or NaN."""
    if len(closes) < slow + sig:
        return np.nan
    e_fast = ema(closes, fast)
    e_slow = ema(closes, slow)
    # Both valid from index slow-1 onward
    macd_line = e_fast - e_slow
    valid = macd_line[~np.isnan(macd_line)]
    if len(valid) < sig:
        return np.nan
    sig_line = ema(valid, sig)
    return float(valid[-1] - sig_line[-1])


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """ATR (latest). NaN if insufficient data."""
    if len(closes) < period + 1:
        return np.nan
    trs = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )
    if len(trs) < period:
        return np.nan
    return float(np.mean(trs[-period:]))


def adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """ADX (latest). NaN if insufficient data."""
    if len(closes) < 2 * period + 1:
        return np.nan
    up = np.diff(highs)
    down = -np.diff(lows)
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    trs = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )

    def wilder(data, n):
        s = [np.sum(data[:n])]
        for i in range(n, len(data)):
            s.append(s[-1] - s[-1] / n + data[i])
        return np.array(s)

    s_tr = wilder(trs, period)
    s_plus = wilder(plus_dm, period)
    s_minus = wilder(minus_dm, period)

    dx_vals = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            continue
        pdi = 100 * s_plus[i] / s_tr[i]
        mdi = 100 * s_minus[i] / s_tr[i]
        denom = pdi + mdi
        if denom == 0:
            continue
        dx_vals.append(100 * abs(pdi - mdi) / denom)

    if len(dx_vals) < period:
        return np.nan
    return float(np.mean(dx_vals[-period:]))


# ---------------------------------------------------------------------------
# Data loading with survivorship-bias handling
# ---------------------------------------------------------------------------

def load_universe_data(
    symbols: List[str],
    timeframe: str = SIM_TIMEFRAME,
    start: str = DATA_START,
    end: str = DATA_END,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch hourly OHLCV for all symbols. Symbols that fail to fetch
    (= didn't exist or no data) are silently skipped.
    Returns {symbol: DataFrame} with DatetimeIndex.
    """
    data = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        base = sym.split("/")[0]
        if base in _EXCLUDE:
            continue
        try:
            df = fetch_ohlcv(
                symbol=sym,
                start=start,
                end=end,
                mode="futures",
                timeframe=timeframe,
                use_cache=True,
                sandbox=False,
            )
            if df is not None and not df.empty and len(df) >= 100:
                data[sym] = df
                logger.info("[%d/%d] Loaded %s: %d bars (%s → %s)",
                            i + 1, total, sym, len(df),
                            df.index.min().date(), df.index.max().date())
            else:
                logger.warning("[%d/%d] Skipped %s: insufficient data (%d bars)",
                               i + 1, total, sym, len(df) if df is not None else 0)
        except Exception as e:
            logger.warning("[%d/%d] Failed to fetch %s: %s", i + 1, total, sym, e)
        # Rate limit kindness
        time.sleep(0.3)

    logger.info("Loaded %d / %d symbols", len(data), total)
    return data


def available_symbols_at(data: Dict[str, pd.DataFrame], date: pd.Timestamp) -> List[str]:
    """
    Return symbols that had data (= were listed and trading) at `date`.
    A symbol qualifies if it has data both before and at/after `date`,
    with at least 200 bars of history before (for indicator warmup).
    """
    available = []
    for sym, df in data.items():
        if df.index.min() > date:
            continue  # not listed yet
        bars_before = len(df.loc[:date])
        if bars_before < 200:
            continue  # not enough history for indicators
        # Must have data after start date (still active)
        bars_after = len(df.loc[date:])
        if bars_after < 24:  # need at least 1 day of forward data
            continue
        available.append(sym)
    return available


def get_hourly_volume_usd(df: pd.DataFrame, at: pd.Timestamp, lookback: int = 168) -> float:
    """Average hourly volume in USD over lookback hours (default 7 days)."""
    prior = df.loc[:at]
    if len(prior) < 10:
        return 0.0
    recent = prior.tail(lookback)
    vol_usd = (recent["volume"] * recent["close"]).mean()
    return float(vol_usd)


# ---------------------------------------------------------------------------
# Contract scoring (backtest version — uses DataFrames, not CLI)
# ---------------------------------------------------------------------------

@dataclass
class ScoredContract:
    symbol: str
    direction: str      # "long" or "short"
    score: float        # 0-100
    adx_val: float
    alignment: float    # 0-1
    vol_ratio: float
    atr_pct: float
    rsi_val: float
    macd_h: float


def score_contract(
    df_1h: pd.DataFrame,
    symbol: str,
    at: pd.Timestamp,
) -> Optional[ScoredContract]:
    """
    Score a contract for momentum using data up to `at` (no lookahead).
    Uses 1H bars to derive 15m-equivalent, 1H, and 4H signals.
    """
    hist = df_1h.loc[:at]
    if len(hist) < 200:
        return None

    c = hist["close"].values
    h = hist["high"].values
    l = hist["low"].values
    v = hist["volume"].values

    # 4H equivalent: resample last 200 bars by taking every 4th
    c_4h = c[-(200 // 4 * 4):].reshape(-1, 4).mean(axis=1) if len(c) >= 200 else c[-50:]

    # 1. ADX on 1H (35%)
    adx_val = adx(h[-200:], l[-200:], c[-200:], 14)
    if np.isnan(adx_val):
        adx_val = 20.0
    adx_score = min(adx_val, 60) / 60 * 100

    # 2. EMA alignment (25%)
    direction_votes = {"long": 0, "short": 0}
    # 1H EMA(9) vs EMA(21)
    e9 = ema(c[-100:], 9)
    e21 = ema(c[-100:], 21)
    if not np.isnan(e9[-1]) and not np.isnan(e21[-1]):
        if e9[-1] > e21[-1]:
            direction_votes["long"] += 1
        else:
            direction_votes["short"] += 1

    # 4H equivalent
    e9_4h = ema(c_4h, 9)
    e21_4h = ema(c_4h, 21)
    if not np.isnan(e9_4h[-1]) and not np.isnan(e21_4h[-1]):
        if e9_4h[-1] > e21_4h[-1]:
            direction_votes["long"] += 1
        else:
            direction_votes["short"] += 1

    # 1H EMA(50) for longer trend
    e50 = ema(c[-100:], 50)
    if not np.isnan(e50[-1]):
        if c[-1] > e50[-1]:
            direction_votes["long"] += 1
        else:
            direction_votes["short"] += 1

    total_v = direction_votes["long"] + direction_votes["short"]
    if total_v == 0:
        return None
    majority = max(direction_votes["long"], direction_votes["short"])
    direction = "long" if direction_votes["long"] >= direction_votes["short"] else "short"
    alignment = majority / total_v

    # 3. Volume (20%)
    avg_vol = np.mean(v[-168:]) if len(v) >= 168 else np.mean(v[-20:])  # 7-day avg
    cur_vol = v[-1]
    vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0
    vol_score = min(vol_ratio / 2.0, 1.0) * 100

    # 4. ATR sweet spot (10%)
    atr_val = atr(h[-100:], l[-100:], c[-100:], 14)
    if np.isnan(atr_val):
        atr_pct = 1.5
    else:
        atr_pct = atr_val / c[-1] * 100 if c[-1] > 0 else 1.5

    if 1.0 <= atr_pct <= 3.0:
        vol_sweet = 100
    elif 0.5 <= atr_pct < 1.0 or 3.0 < atr_pct <= 5.0:
        vol_sweet = 60
    else:
        vol_sweet = 30

    # 5. Momentum burst (10% bonus for strong RSI + MACD confirmation)
    rsi_val = rsi(c[-50:], 14)
    macd_h = macd_hist(c[-50:], 12, 26, 9)
    momentum_bonus = 0
    if not np.isnan(rsi_val) and not np.isnan(macd_h):
        if direction == "long" and RSI_LONG_RANGE[0] <= rsi_val <= RSI_LONG_RANGE[1] and macd_h > 0:
            momentum_bonus = 100
        elif direction == "short" and RSI_SHORT_RANGE[0] <= rsi_val <= RSI_SHORT_RANGE[1] and macd_h < 0:
            momentum_bonus = 100

    composite = (
        0.30 * adx_score +
        0.25 * alignment * 100 +
        0.20 * vol_score +
        0.10 * vol_sweet +
        0.15 * momentum_bonus
    )

    return ScoredContract(
        symbol=symbol,
        direction=direction,
        score=composite,
        adx_val=adx_val,
        alignment=alignment,
        vol_ratio=vol_ratio,
        atr_pct=atr_pct,
        rsi_val=rsi_val if not np.isnan(rsi_val) else 50.0,
        macd_h=macd_h if not np.isnan(macd_h) else 0.0,
    )


def validate_entry(sc: ScoredContract) -> bool:
    """Triple confirmation gate (backtest version)."""
    if sc.alignment < EMA_ALIGNMENT_MIN:
        return False
    if sc.direction == "long":
        if not (RSI_LONG_RANGE[0] <= sc.rsi_val <= RSI_LONG_RANGE[1]):
            return False
        if sc.macd_h <= 0:
            return False
    else:
        if not (RSI_SHORT_RANGE[0] <= sc.rsi_val <= RSI_SHORT_RANGE[1]):
            return False
        if sc.macd_h >= 0:
            return False
    if sc.vol_ratio < VOLUME_MULT_THRESHOLD:
        return False
    return True


# ---------------------------------------------------------------------------
# Reversal detection (backtest version)
# ---------------------------------------------------------------------------

def detect_reversal_bt(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    volumes: np.ndarray,
    pos_side: str,
) -> float:
    """Composite reversal score [0,1]. Uses data up to current bar."""
    if len(closes) < 30:
        return 0.0

    signals = {}

    # Volume climax + reversal candle
    avg_v = np.mean(volumes[-20:])
    vol_climax = 0.0
    if avg_v > 0 and volumes[-1] > 5 * avg_v:
        body = abs(closes[-1] - closes[-2])
        rng = highs[-1] - lows[-1]
        if rng > 0 and (1 - body / rng) > 0.6:
            vol_climax = 1.0
    signals["vol_climax"] = vol_climax

    # RSI divergence
    rsi_div = 0.0
    if len(closes) >= 28:
        rsi_now = rsi(closes[-20:], 14)
        rsi_prev = rsi(closes[-23:-3], 14)
        if not np.isnan(rsi_now) and not np.isnan(rsi_prev):
            if pos_side == "long" and closes[-1] > closes[-3] and rsi_now < rsi_prev:
                rsi_div = 1.0
            elif pos_side == "short" and closes[-1] < closes[-3] and rsi_now > rsi_prev:
                rsi_div = 1.0
    signals["rsi_div"] = rsi_div

    # EMA cross
    ema_cross = 0.0
    e9 = ema(closes[-50:], 9)
    e21 = ema(closes[-50:], 21)
    if not np.isnan(e9[-1]) and not np.isnan(e21[-1]) and not np.isnan(e9[-2]) and not np.isnan(e21[-2]):
        if pos_side == "long" and e9[-1] < e21[-1] and e9[-2] >= e21[-2]:
            ema_cross = 1.0
        elif pos_side == "short" and e9[-1] > e21[-1] and e9[-2] <= e21[-2]:
            ema_cross = 1.0
    signals["ema_cross"] = ema_cross

    # MACD shrinking
    macd_div = 0.0
    if len(closes) >= 40:
        mh_now = macd_hist(closes[-40:], 12, 26, 9)
        mh_prev = macd_hist(closes[-43:-3], 12, 26, 9)
        if not np.isnan(mh_now) and not np.isnan(mh_prev):
            if pos_side == "long" and mh_now < mh_prev and mh_now > 0:
                macd_div = 1.0
            elif pos_side == "short" and mh_now > mh_prev and mh_now < 0:
                macd_div = 1.0
    signals["macd_div"] = macd_div

    return (
        0.30 * signals["vol_climax"] +
        0.30 * signals["rsi_div"] +
        0.25 * signals["ema_cross"] +
        0.15 * signals["macd_div"]
    )


# ---------------------------------------------------------------------------
# Single trial simulation
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    """One trade within a trial."""
    round_num: int
    symbol: str
    direction: str
    entry_bar: int          # bar index from trial start
    exit_bar: int
    entry_price: float
    exit_price: float
    leverage: int
    contracts: int          # simulated count
    margin_used: float
    notional: float
    pnl_usd: float
    pnl_pct: float
    fees_usd: float
    slippage_usd: float
    exit_reason: str
    duration_hours: float


@dataclass
class TrialResult:
    """Result of one 14-day trial."""
    trial_id: int
    start_date: str
    end_date: str
    success: bool           # hit 20% target?
    final_roi_pct: float    # net PnL / cumulative invested
    total_invested: float
    net_pnl: float
    total_fees: float
    total_slippage: float
    num_rounds: int
    num_trades: int
    max_drawdown_pct: float
    symbols_traded: str     # comma-separated
    rounds_detail: str      # JSON array of per-round summaries
    trades: List[TradeRecord] = field(default_factory=list)

    def to_row(self) -> Dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "success": int(self.success),
            "final_roi_pct": round(self.final_roi_pct * 100, 4),
            "total_invested": round(self.total_invested, 2),
            "net_pnl": round(self.net_pnl, 4),
            "total_fees": round(self.total_fees, 4),
            "total_slippage": round(self.total_slippage, 4),
            "num_rounds": self.num_rounds,
            "num_trades": self.num_trades,
            "max_drawdown_pct": round(self.max_drawdown_pct * 100, 4),
            "symbols_traded": self.symbols_traded,
            "rounds_detail": self.rounds_detail,
        }


def compute_slippage_pct(notional: float, hourly_vol_usd: float) -> float:
    """Square-root market impact slippage (same model as SimulatedExecution)."""
    if hourly_vol_usd <= 0 or notional <= 0:
        return SLIPPAGE_BASE_BPS / 10000  # fallback 3 bps
    # Annualized ADV proxy: hourly * 24
    adv = hourly_vol_usd * 24
    slip = SLIPPAGE_FACTOR * math.sqrt(notional / adv)
    return max(slip, SLIPPAGE_BASE_BPS / 10000)


def run_trial(
    trial_id: int,
    data: Dict[str, pd.DataFrame],
    start: pd.Timestamp,
    rng: random.Random,
) -> TrialResult:
    """
    Simulate one full 14-day YOLO run starting at `start`.

    The simulation steps through 1H bars. At each bar when HUNTING,
    it scores all available contracts and enters on the best validated signal.
    While IN_POSITION, it checks exit conditions every bar.
    """
    end = start + pd.Timedelta(days=TRIAL_DURATION_DAYS)

    # Determine available symbols at start
    avail = available_symbols_at(data, start)
    if not avail:
        return TrialResult(
            trial_id=trial_id, start_date=str(start.date()), end_date=str(end.date()),
            success=False, final_roi_pct=0, total_invested=0, net_pnl=0,
            total_fees=0, total_slippage=0, num_rounds=0, num_trades=0,
            max_drawdown_pct=0, symbols_traded="", rounds_detail="[]",
        )

    # State
    current_round = 0
    cumulative_invested = 0.0
    realized_pnl = 0.0
    total_fees = 0.0
    total_slippage = 0.0
    nav_history = []
    trades: List[TradeRecord] = []
    rounds_detail: List[Dict] = []
    symbols_traded = set()
    target_hit = False

    # Position state
    pos_symbol: Optional[str] = None
    pos_side: Optional[str] = None
    pos_entry: float = 0.0
    pos_notional: float = 0.0
    pos_margin: float = 0.0
    pos_lever: int = DEFAULT_LEVER
    pos_entry_bar: int = 0
    pos_peak_pnl: float = 0.0
    pos_trailing: bool = False

    # Hunting state
    status = "START_ROUND"
    hunt_cooldown_bar = 0  # don't enter immediately, wait a few bars
    same_dir_losses = 0
    last_loss_dir: Optional[str] = None

    def start_round():
        nonlocal current_round, cumulative_invested, status, hunt_cooldown_bar
        if current_round >= len(ROUND_MARGINS):
            status = "DONE"
            return
        margin = ROUND_MARGINS[current_round]
        cumulative_invested += margin
        current_round += 1
        status = "HUNTING"
        hunt_cooldown_bar = bar_idx + 4  # wait 4 bars (4h) before first entry

    def calc_target() -> float:
        """Target profit to achieve 20% on cumulative invested, covering all prior losses."""
        prior_losses = abs(min(realized_pnl, 0))
        return cumulative_invested * TARGET_ROI_PCT + prior_losses

    # Build combined time index (union of all available symbol bars within window)
    all_indices = []
    for sym in avail:
        idx = data[sym].loc[start:end].index
        all_indices.append(idx)
    if not all_indices:
        return TrialResult(
            trial_id=trial_id, start_date=str(start.date()), end_date=str(end.date()),
            success=False, final_roi_pct=0, total_invested=0, net_pnl=0,
            total_fees=0, total_slippage=0, num_rounds=0, num_trades=0,
            max_drawdown_pct=0, symbols_traded="", rounds_detail="[]",
        )
    time_index = sorted(set().union(*all_indices))
    if len(time_index) < 24:
        return TrialResult(
            trial_id=trial_id, start_date=str(start.date()), end_date=str(end.date()),
            success=False, final_roi_pct=0, total_invested=0, net_pnl=0,
            total_fees=0, total_slippage=0, num_rounds=0, num_trades=0,
            max_drawdown_pct=0, symbols_traded="", rounds_detail="[]",
        )

    # Main simulation loop
    for bar_idx, ts in enumerate(time_index):
        # NAV tracking
        unrealized = 0.0
        if pos_symbol and pos_symbol in data:
            df = data[pos_symbol]
            if ts in df.index:
                price = float(df.loc[ts, "close"])
                raw_pct = (price - pos_entry) / pos_entry
                pnl_pct = raw_pct if pos_side == "long" else -raw_pct
                unrealized = pos_notional * pnl_pct

        nav = cumulative_invested + realized_pnl + unrealized - total_fees - total_slippage
        nav_history.append(nav)

        # State machine
        if status == "START_ROUND":
            start_round()
            if status == "DONE":
                break
            continue

        elif status == "HUNTING":
            if bar_idx < hunt_cooldown_bar:
                continue

            # Score available contracts (every 4 bars = 4h to save compute)
            if bar_idx % 4 != 0:
                continue

            scored = []
            for sym in avail:
                df = data[sym]
                if ts not in df.index:
                    continue
                sc = score_contract(df, sym, ts)
                if sc and validate_entry(sc):
                    scored.append(sc)

            if not scored:
                continue

            # Force direction flip after consecutive same-dir losses
            forced_dir = None
            if same_dir_losses >= 3 and last_loss_dir:
                forced_dir = "short" if last_loss_dir == "long" else "long"

            if forced_dir:
                scored = [s for s in scored if s.direction == forced_dir]
                if not scored:
                    continue

            scored.sort(key=lambda x: x.score, reverse=True)
            best = scored[0]

            # Determine leverage
            if best.atr_pct > 3.0:
                lever = HIGH_VOL_LEVER
            elif best.atr_pct < 1.0:
                lever = LOW_VOL_LEVER
            else:
                lever = DEFAULT_LEVER

            # Position sizing
            round_margin = ROUND_MARGINS[current_round - 1]
            margin_use = round_margin * 0.90
            notional = margin_use * lever

            # Get entry price (use close of current bar as market order fill proxy)
            df = data[best.symbol]
            if ts not in df.index:
                continue
            entry_price = float(df.loc[ts, "close"])
            if entry_price <= 0:
                continue

            # Slippage on entry
            hvol = get_hourly_volume_usd(df, ts)
            slip_pct = compute_slippage_pct(notional, hvol)
            if best.direction == "long":
                fill_price = entry_price * (1 + slip_pct)
            else:
                fill_price = entry_price * (1 - slip_pct)
            slip_usd = abs(fill_price - entry_price) / entry_price * notional

            # Entry fee
            fee = notional * TAKER_FEE_PCT

            total_fees += fee
            total_slippage += slip_usd

            # Set position
            pos_symbol = best.symbol
            pos_side = best.direction
            pos_entry = fill_price
            pos_notional = notional
            pos_margin = margin_use
            pos_lever = lever
            pos_entry_bar = bar_idx
            pos_peak_pnl = 0.0
            pos_trailing = False
            status = "IN_POSITION"
            symbols_traded.add(best.symbol)

        elif status == "IN_POSITION":
            if pos_symbol not in data:
                status = "HUNTING"
                continue

            df = data[pos_symbol]
            if ts not in df.index:
                continue

            price = float(df.loc[ts, "close"])
            raw_pct = (price - pos_entry) / pos_entry
            pnl_pct = raw_pct if pos_side == "long" else -raw_pct
            unrealized = pos_notional * pnl_pct

            hours_held = bar_idx - pos_entry_bar  # 1 bar = 1 hour
            target = calc_target()
            round_margin = ROUND_MARGINS[current_round - 1]

            # Update peak
            if unrealized > pos_peak_pnl:
                pos_peak_pnl = unrealized

            exit_reason = None

            # 1. Target hit
            if unrealized >= target:
                exit_reason = "TARGET_HIT"

            # 2. Hard stop
            elif unrealized <= -(round_margin * HARD_STOP_PCT):
                exit_reason = "HARD_STOP"

            # 3. Liquidation proxy (unrealized loss >= margin)
            elif unrealized <= -round_margin:
                exit_reason = "LIQUIDATED"

            # 4. Trailing stop
            if exit_reason is None:
                if not pos_trailing and unrealized >= target * TRAIL_ACTIVATE_PCT:
                    pos_trailing = True
                if pos_trailing:
                    floor = pos_peak_pnl * (1 - TRAIL_DISTANCE_PCT)
                    if unrealized < floor and unrealized < pos_peak_pnl * 0.8:
                        exit_reason = "TRAILING_STOP"

            # 5. Time decay (4 days)
            if exit_reason is None and hours_held >= TIME_DECAY_HOURS:
                if unrealized < target * 0.05:
                    exit_reason = "TIME_DECAY"

            # 6. Reversal detection (every 6 bars = 6h)
            if exit_reason is None and bar_idx % 6 == 0:
                hist = df.loc[:ts]
                rev_score = detect_reversal_bt(
                    hist["close"].values[-50:],
                    hist["high"].values[-50:],
                    hist["low"].values[-50:],
                    hist["volume"].values[-50:],
                    pos_side,
                )
                if rev_score >= 0.45:
                    exit_reason = f"REVERSAL"
                elif rev_score >= 0.30 and not pos_trailing and unrealized > 0:
                    pos_trailing = True
                    pos_peak_pnl = max(pos_peak_pnl, unrealized)

            # Execute exit
            if exit_reason:
                # Exit slippage + fee
                hvol = get_hourly_volume_usd(df, ts)
                exit_slip_pct = compute_slippage_pct(pos_notional, hvol)
                exit_fee = pos_notional * TAKER_FEE_PCT
                exit_slip_usd = exit_slip_pct * pos_notional

                total_fees += exit_fee
                total_slippage += exit_slip_usd

                # Net PnL after all costs
                trade_pnl = unrealized - exit_fee - exit_slip_usd

                trade = TradeRecord(
                    round_num=current_round,
                    symbol=pos_symbol,
                    direction=pos_side,
                    entry_bar=pos_entry_bar,
                    exit_bar=bar_idx,
                    entry_price=pos_entry,
                    exit_price=price,
                    leverage=pos_lever,
                    contracts=0,
                    margin_used=pos_margin,
                    notional=pos_notional,
                    pnl_usd=trade_pnl,
                    pnl_pct=pnl_pct,
                    fees_usd=exit_fee + (pos_notional * TAKER_FEE_PCT),  # entry + exit
                    slippage_usd=exit_slip_usd,
                    exit_reason=exit_reason,
                    duration_hours=hours_held,
                )
                trades.append(trade)

                # Update realized
                if exit_reason == "LIQUIDATED":
                    realized_pnl -= round_margin
                else:
                    realized_pnl += trade_pnl

                # Track direction losses
                if trade_pnl < 0:
                    if last_loss_dir == pos_side:
                        same_dir_losses += 1
                    else:
                        same_dir_losses = 1
                        last_loss_dir = pos_side
                else:
                    same_dir_losses = 0
                    last_loss_dir = None

                rounds_detail.append({
                    "round": current_round,
                    "symbol": pos_symbol,
                    "direction": pos_side,
                    "pnl": round(trade_pnl, 4),
                    "reason": exit_reason,
                    "hours": hours_held,
                })

                # Clear position
                pos_symbol = None
                pos_side = None

                # Next state
                if exit_reason == "TARGET_HIT":
                    target_hit = True
                    status = "DONE"
                elif exit_reason in ("HARD_STOP", "LIQUIDATED"):
                    status = "START_ROUND"
                    hunt_cooldown_bar = bar_idx + 12  # 12h cooldown
                elif exit_reason == "TRAILING_STOP" and trade_pnl > 0:
                    # Partial win, re-hunt
                    status = "HUNTING"
                    hunt_cooldown_bar = bar_idx + 2
                else:
                    status = "HUNTING"
                    hunt_cooldown_bar = bar_idx + 4

        elif status == "DONE":
            # Keep recording NAV but don't trade
            pass

    # Final stats
    if not nav_history:
        nav_history = [0]

    peak = nav_history[0]
    max_dd = 0.0
    for n in nav_history:
        if n > peak:
            peak = n
        if peak > 0:
            dd = (n - peak) / peak
            if dd < max_dd:
                max_dd = dd

    net_pnl = realized_pnl - total_fees - total_slippage
    final_roi = net_pnl / cumulative_invested if cumulative_invested > 0 else 0

    return TrialResult(
        trial_id=trial_id,
        start_date=str(start.date()),
        end_date=str(end.date()),
        success=target_hit,
        final_roi_pct=final_roi,
        total_invested=cumulative_invested,
        net_pnl=net_pnl,
        total_fees=total_fees,
        total_slippage=total_slippage,
        num_rounds=current_round,
        num_trades=len(trades),
        max_drawdown_pct=max_dd,
        symbols_traded=",".join(sorted(symbols_traded)),
        rounds_detail=json.dumps(rounds_detail),
        trades=trades,
    )


# ---------------------------------------------------------------------------
# Monte Carlo runner
# ---------------------------------------------------------------------------

def generate_random_starts(
    data: Dict[str, pd.DataFrame],
    n_trials: int,
    seed: int = 42,
) -> List[pd.Timestamp]:
    """
    Generate n_trials random start dates where at least 5 symbols
    have data and there are 14 days of forward data.
    """
    # Find global data range
    all_starts = [df.index.min() for df in data.values()]
    all_ends = [df.index.max() for df in data.values()]
    global_start = max(all_starts) + pd.Timedelta(days=30)  # 30-day warmup
    global_end = min(all_ends) - pd.Timedelta(days=TRIAL_DURATION_DAYS + 1)

    if global_start >= global_end:
        logger.error("Insufficient data range: %s to %s", global_start, global_end)
        return []

    rng = random.Random(seed)
    starts = []
    attempts = 0
    max_attempts = n_trials * 10

    while len(starts) < n_trials and attempts < max_attempts:
        # Random timestamp between global_start and global_end
        delta = (global_end - global_start).total_seconds()
        random_sec = rng.uniform(0, delta)
        candidate = global_start + pd.Timedelta(seconds=random_sec)
        # Round to nearest hour
        candidate = candidate.floor("h")

        # Check enough symbols available
        avail = available_symbols_at(data, candidate)
        if len(avail) >= 3:
            starts.append(candidate)
        attempts += 1

    logger.info("Generated %d valid start dates (from %d attempts)", len(starts), attempts)
    return starts


def run_montecarlo(
    n_trials: int = 500,
    seed: int = 42,
    output_csv: Optional[str] = None,
    output_json: Optional[str] = None,
    data: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[TrialResult]:
    """
    Run the full Monte Carlo backtest.

    1. Load historical data for all universe symbols
    2. Generate random start dates
    3. Run trials
    4. Save results
    """
    # 1. Load data
    if data is None:
        logger.info("Loading universe data (%d symbols, %s to %s)...",
                     len(UNIVERSE), DATA_START, DATA_END)
        data = load_universe_data(UNIVERSE)

    if len(data) < 3:
        logger.error("Insufficient data: only %d symbols loaded", len(data))
        return []

    # 2. Generate starts
    starts = generate_random_starts(data, n_trials, seed)
    if not starts:
        logger.error("No valid start dates generated")
        return []

    # 3. Run trials
    results = []
    rng = random.Random(seed)
    t0 = time.time()

    for i, start in enumerate(starts):
        result = run_trial(i + 1, data, start, rng)
        results.append(result)

        if (i + 1) % 50 == 0 or i == 0:
            elapsed = time.time() - t0
            wins = sum(1 for r in results if r.success)
            logger.info(
                "Trial %d/%d (%.1fs): %d wins / %d done (%.1f%% success rate so far)",
                i + 1, len(starts), elapsed, wins, len(results),
                wins / len(results) * 100 if results else 0,
            )

    elapsed = time.time() - t0
    logger.info("All %d trials complete in %.1fs", len(results), elapsed)

    # 4. Save results
    if output_csv:
        _save_csv(results, output_csv)
    if output_json:
        _save_json(results, output_json)
    if not output_csv and not output_json:
        # Default: save to engine/results/
        results_dir = ENGINE_DIR / "results"
        results_dir.mkdir(exist_ok=True)
        default_csv = results_dir / "yolo_mc_results.csv"
        _save_csv(results, str(default_csv))
        logger.info("Results saved to %s", default_csv)

    return results


def _save_csv(results: List[TrialResult], path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(results[0].to_row().keys())
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r.to_row())
    logger.info("CSV saved: %s (%d rows)", path, len(results))


def _save_json(results: List[TrialResult], path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    rows = [r.to_row() for r in results]
    with open(path, "w") as f:
        json.dump(rows, f, indent=2)
    logger.info("JSON saved: %s (%d rows)", path, len(results))


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary(results: List[TrialResult]):
    """Print statistical summary of Monte Carlo results."""
    if not results:
        print("No results to summarize.")
        return

    n = len(results)
    wins = [r for r in results if r.success]
    losses = [r for r in results if not r.success]

    rois = [r.final_roi_pct for r in results]
    invested = [r.total_invested for r in results]
    trades = [r.num_trades for r in results]
    rounds = [r.num_rounds for r in results]
    drawdowns = [r.max_drawdown_pct for r in results]
    fees = [r.total_fees for r in results]

    win_rois = [r.final_roi_pct for r in wins] if wins else [0]
    loss_rois = [r.final_roi_pct for r in losses] if losses else [0]

    print(f"\n{'='*65}")
    print(f"  YOLO MOMENTUM — MONTE CARLO BACKTEST RESULTS")
    print(f"  {n} trials | {len(wins)} wins | {len(losses)} losses")
    print(f"{'='*65}")

    print(f"\n  SUCCESS RATE:        {len(wins)/n*100:.1f}%")
    print(f"  EXPECTED ROI:        {np.mean(rois)*100:.2f}%")
    print(f"  MEDIAN ROI:          {np.median(rois)*100:.2f}%")
    print(f"  ROI STD DEV:         {np.std(rois)*100:.2f}%")

    print(f"\n  {'─'*55}")
    print(f"  Win Analysis ({len(wins)} trials):")
    print(f"    Mean ROI:          {np.mean(win_rois)*100:.2f}%")
    print(f"    Mean invested:     ${np.mean([w.total_invested for w in wins]):.0f}" if wins else "    N/A")
    print(f"    Mean trades:       {np.mean([w.num_trades for w in wins]):.1f}" if wins else "    N/A")
    print(f"    Mean rounds:       {np.mean([w.num_rounds for w in wins]):.1f}" if wins else "    N/A")

    print(f"\n  Loss Analysis ({len(losses)} trials):")
    print(f"    Mean ROI:          {np.mean(loss_rois)*100:.2f}%")
    print(f"    Mean invested:     ${np.mean([l.total_invested for l in losses]):.0f}" if losses else "    N/A")
    print(f"    Mean max DD:       {np.mean([l.max_drawdown_pct for l in losses])*100:.2f}%" if losses else "    N/A")

    print(f"\n  {'─'*55}")
    print(f"  Overall Statistics:")
    print(f"    Mean invested:     ${np.mean(invested):.0f}")
    print(f"    Mean trades:       {np.mean(trades):.1f}")
    print(f"    Mean rounds:       {np.mean(rounds):.1f}")
    print(f"    Mean max DD:       {np.mean(drawdowns)*100:.2f}%")
    print(f"    Mean fees:         ${np.mean(fees):.2f}")
    print(f"    ROI 5th pctile:    {np.percentile(rois, 5)*100:.2f}%")
    print(f"    ROI 25th pctile:   {np.percentile(rois, 25)*100:.2f}%")
    print(f"    ROI 75th pctile:   {np.percentile(rois, 75)*100:.2f}%")
    print(f"    ROI 95th pctile:   {np.percentile(rois, 95)*100:.2f}%")

    # Kelly criterion estimate
    if wins and losses:
        p_win = len(wins) / n
        avg_win = np.mean(win_rois) if win_rois else 0
        avg_loss = abs(np.mean(loss_rois)) if loss_rois else 1
        if avg_loss > 0:
            kelly = p_win - (1 - p_win) / (avg_win / avg_loss) if avg_win > 0 else 0
            print(f"    Kelly fraction:    {kelly:.3f}")

    # Distribution of rounds used
    round_dist = {}
    for r in results:
        round_dist[r.num_rounds] = round_dist.get(r.num_rounds, 0) + 1
    print(f"\n  Rounds Distribution:")
    for k in sorted(round_dist.keys()):
        pct = round_dist[k] / n * 100
        print(f"    {k} round(s):  {round_dist[k]:4d} ({pct:.1f}%)")

    # Most traded symbols
    sym_counts: Dict[str, int] = {}
    for r in results:
        for s in r.symbols_traded.split(","):
            s = s.strip()
            if s:
                sym_counts[s] = sym_counts.get(s, 0) + 1
    top_syms = sorted(sym_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    print(f"\n  Top 10 Most Traded Symbols:")
    for sym, count in top_syms:
        print(f"    {sym:20s} {count:4d} trials ({count/n*100:.1f}%)")

    print(f"\n{'='*65}\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Monte Carlo backtester for YOLO Momentum strategy",
    )
    parser.add_argument("--trials", type=int, default=500,
                        help="Number of Monte Carlo trials (default: 500)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV path (default: engine/results/yolo_mc_results.csv)")
    parser.add_argument("--output-json", type=str, default=None,
                        help="Also save results as JSON")
    parser.add_argument("--summary", action="store_true",
                        help="Print summary statistics after run")
    parser.add_argument("--data-start", type=str, default=DATA_START,
                        help="Historical data start date")
    parser.add_argument("--data-end", type=str, default=DATA_END,
                        help="Historical data end date")
    parser.add_argument("--universe", type=str, default=None,
                        help="Comma-separated symbol list (override default)")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    universe = UNIVERSE
    if args.universe:
        universe = [s.strip() for s in args.universe.split(",")]

    # Load data once
    logger.info("Loading universe data...")
    data = load_universe_data(universe, start=args.data_start, end=args.data_end)

    # Run
    results = run_montecarlo(
        n_trials=args.trials,
        seed=args.seed,
        output_csv=args.output,
        output_json=args.output_json,
        data=data,
    )

    # Summary
    if args.summary or True:  # always print summary
        print_summary(results)


if __name__ == "__main__":
    main()
