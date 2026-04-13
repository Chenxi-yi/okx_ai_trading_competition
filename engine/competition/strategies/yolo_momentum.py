"""
competition/strategies/yolo_momentum.py
========================================
YOLO Momentum — Aggressive Leveraged Momentum with Martingale Doubling

Strategy:
  - Multi-timeframe momentum analysis (EMA crosses, RSI, MACD, volume)
  - High leverage (30-75x) on single contract
  - Target: 20% ROI on cumulative invested capital
  - Martingale: double margin on liquidation (50 → 100 → 200 → 400)
  - Auto-stop when target hit

Architecture:
  - Standalone async execution loop (like elite_flow)
  - REST polling for candles, indicators, funding, OI
  - Orders via `okx swap place` CLI (Agent Trade Kit)
  - 10-second reconciliation loop

Entry point:
  run(config, foreground=True) ← called by main.py _run_custom_strategy
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
from typing import Any, Dict, List, Optional, Tuple

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

# Stablecoins, index tokens, TradFi/equity perps — never trade these
_EXCLUDE_BASES = {
    # Stablecoins & fiat
    "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD", "USDD", "PYUSD",
    "EUR", "GBP", "AUD", "BRL", "TRY",
    # Index / synthetic
    "BTCDOM", "DEFI",
    # Commodity-pegged
    "XAU", "XAG", "PAXG",
    # Equity / TradFi perps
    "MSTR", "TSLA", "CRCL", "AAPL", "AMZN", "GOOG", "MSFT", "META",
    "NVDA", "NFLX", "COIN", "SQ", "PYPL", "BABA", "PLTR",
}

# Minimum 24h quote volume (USDT) to be considered liquid
_MIN_VOLUME_USD = 3_000_000.0
# Maximum bid-ask spread (%) — above this, entry/exit cost eats the edge
_MAX_SPREAD_PCT = 0.15
_ATK_RETRIES = 3
_ATK_RETRY_DELAY_SEC = 1.0
_INSTRUMENT_CACHE_TTL_SEC = 300
_INSTRUMENT_CACHE: Dict[str, Tuple[float, set[str]]] = {}
_INSTRUMENT_SPECS_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, float]]]] = {}
_INVALID_INSTRUMENTS: Dict[str, set[str]] = {}


def _instrument_specs_cache_path(profile: str) -> Path:
    return LOGS_DIR / f"okx_swap_instrument_specs_{profile}.json"


def _read_cached_instrument_specs(profile: str) -> Dict[str, Dict[str, float]]:
    path = _instrument_specs_cache_path(profile)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to read cached instrument specs %s: %s", path, e)
        return {}


def _write_cached_instrument_specs(profile: str, specs: Dict[str, Dict[str, float]]) -> None:
    path = _instrument_specs_cache_path(profile)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(specs, indent=2, sort_keys=True))
    except Exception as e:
        logger.warning("Failed to write cached instrument specs %s: %s", path, e)


def _fetch_swap_instrument_specs(profile: str = "live") -> Dict[str, Dict[str, float]]:
    now = time.time()
    cached = _INSTRUMENT_SPECS_CACHE.get(profile)
    if cached and now - cached[0] < _INSTRUMENT_CACHE_TTL_SEC:
        return cached[1]

    specs: Dict[str, Dict[str, float]] = {}
    raw = _atk_call(["market", "instruments", "--instType", "SWAP"], profile)
    if raw:
        try:
            payload = json.loads(raw)
            instruments = payload if isinstance(payload, list) else payload.get("data", [])
            for inst in instruments:
                inst_id = inst.get("instId", "")
                state = str(inst.get("state", "")).lower()
                if not inst_id.endswith("-USDT-SWAP") or state not in {"live", "preopen", ""}:
                    continue
                try:
                    specs[inst_id] = {
                        "max_leverage": float(inst.get("lever", 0) or 0),
                        "ct_val": float(inst.get("ctVal", 0) or 0),
                        "lot_sz": float(inst.get("lotSz", 0) or 0),
                        "min_sz": float(inst.get("minSz", 0) or 0),
                        "max_mkt_sz": float(inst.get("maxMktSz", 0) or 0),
                    }
                except Exception:
                    continue
        except Exception as e:
            logger.warning("Failed to parse swap instrument specs JSON: %s", e)

    if specs:
        _write_cached_instrument_specs(profile, specs)
    else:
        specs = _read_cached_instrument_specs(profile)
        if specs:
            logger.warning("Using cached swap instrument specs for profile=%s", profile)
        else:
            logger.warning("Swap instrument spec lookup failed; proceeding without spec cache")

    _INSTRUMENT_SPECS_CACHE[profile] = (now, specs)
    return specs


def _fetch_valid_swap_instruments(profile: str = "live") -> set[str]:
    now = time.time()
    cached = _INSTRUMENT_CACHE.get(profile)
    if cached and now - cached[0] < _INSTRUMENT_CACHE_TTL_SEC:
        return cached[1]

    valid = set(_fetch_swap_instrument_specs(profile).keys())
    if not valid:
        logger.warning("Swap instrument lookup failed; proceeding without validity filter")
    _INSTRUMENT_CACHE[profile] = (now, valid)
    return valid


def _mark_invalid_instrument(inst_id: str, profile: str = "live") -> None:
    if not inst_id:
        return
    _INVALID_INSTRUMENTS.setdefault(profile, set()).add(inst_id)


def _is_invalid_instrument(inst_id: str, profile: str = "live") -> bool:
    return inst_id in _INVALID_INSTRUMENTS.get(profile, set())


def fetch_universe(profile: str = "live") -> List[str]:
    """
    Dynamically discover all tradable USDT-SWAP perps on OKX.
    Filters out: equity perps, stablecoins, index tokens, illiquid coins.

    Returns list of instId strings like ['BTC-USDT-SWAP', 'PEPE-USDT-SWAP', ...]
    sorted by 24h volume descending.
    """
    # 1) Get all swap tickers via CLI (no auth needed)
    raw = _atk_call(["market", "tickers", "SWAP"], profile)
    if not raw:
        logger.warning("Universe fetch failed via CLI, falling back to ccxt")
        return _fetch_universe_ccxt(profile)

    try:
        tickers = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse tickers JSON, falling back to ccxt")
        return _fetch_universe_ccxt(profile)

    if not isinstance(tickers, list):
        tickers = tickers.get("data", []) if isinstance(tickers, dict) else []

    valid_instruments = _fetch_valid_swap_instruments(profile)
    candidates = []
    skipped_invalid = 0
    for t in tickers:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        if _is_invalid_instrument(inst_id, profile):
            skipped_invalid += 1
            continue
        if valid_instruments and inst_id not in valid_instruments:
            skipped_invalid += 1
            continue

        base = inst_id.split("-")[0]
        if base in _EXCLUDE_BASES:
            continue

        vol_24h = float(t.get("volCcy24h", 0) or t.get("vol24h", 0) or 0)
        if vol_24h < _MIN_VOLUME_USD:
            continue

        # Spread check: (ask - bid) / mid
        bid = float(t.get("bidPx", 0) or 0)
        ask = float(t.get("askPx", 0) or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100
            if spread_pct > _MAX_SPREAD_PCT:
                logger.debug("Excluded %s: spread=%.3f%% > %.3f%%", inst_id, spread_pct, _MAX_SPREAD_PCT)
                continue

        candidates.append((inst_id, vol_24h))

    candidates.sort(key=lambda x: x[1], reverse=True)
    symbols = [sym for sym, _ in candidates]

    logger.info(
        "Dynamic universe: %d liquid USDT-SWAP perps (from %d tickers, skipped_invalid=%d)",
        len(symbols), len(tickers), skipped_invalid,
    )
    return symbols


def _fetch_universe_ccxt(profile: str = "live") -> List[str]:
    """Fallback: use ccxt to discover universe."""
    try:
        import ccxt
        from config.settings import OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE

        ex = ccxt.okx({
            "apiKey": OKX_API_KEY,
            "secret": OKX_API_SECRET,
            "password": OKX_PASSPHRASE,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        ex.has["fetchCurrencies"] = False
        if profile == "demo":
            ex.headers["x-simulated-trading"] = "1"

        tickers = ex.fetch_tickers()
        candidates = []
        for symbol, ticker in tickers.items():
            if not symbol.endswith("/USDT:USDT") and not symbol.endswith("/USDT"):
                continue
            base = symbol.split("/")[0]
            if base in _EXCLUDE_BASES:
                continue
            vol = float(ticker.get("quoteVolume", 0) or 0)
            if vol < _MIN_VOLUME_USD:
                continue

            # Check spread
            bid = float(ticker.get("bid", 0) or 0)
            ask = float(ticker.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ((bid + ask) / 2) * 100
                if spread_pct > _MAX_SPREAD_PCT:
                    continue

            inst_id = f"{base}-USDT-SWAP"
            candidates.append((inst_id, vol))

        candidates.sort(key=lambda x: x[1], reverse=True)
        symbols = [sym for sym, _ in candidates]
        logger.info("ccxt universe: %d symbols", len(symbols))
        return symbols
    except Exception as e:
        logger.error("ccxt universe fetch failed: %s", e)
        # Last-resort fallback
        return ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


def _get_contract_value(inst_id: str, profile: str = "live") -> float:
    """
    Get contract value for an instId.
    First checks static CT_VAL, then queries OKX for unknown contracts.
    """
    if inst_id in CT_VAL:
        return CT_VAL[inst_id]

    specs = _fetch_swap_instrument_specs(profile)
    spec = specs.get(inst_id, {})
    if float(spec.get("ct_val") or 0) > 0:
        CT_VAL[inst_id] = float(spec["ct_val"])
        return CT_VAL[inst_id]

    # Query OKX for contract spec
    raw = _atk_call(["market", "instruments", "--instType", "SWAP", "--instId", inst_id], profile)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                ct_val = float(data[0].get("ctVal", 0) or 0)
                if ct_val > 0:
                    CT_VAL[inst_id] = ct_val  # cache it
                    return ct_val
        except Exception:
            pass

    # Fallback heuristic: assume 1 coin per contract for unknown alts
    logger.warning("Unknown ctVal for %s, defaulting to 1.0", inst_id)
    CT_VAL[inst_id] = 1.0
    return 1.0


def _get_max_leverage(inst_id: str, profile: str = "live") -> float:
    specs = _fetch_swap_instrument_specs(profile)
    spec = specs.get(inst_id, {})
    return float(spec.get("max_leverage") or 0)


def _clean_okx_cli_text(text: str) -> str:
    if not text:
        return ""
    cleaned_lines: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Update available for @okx_ai/okx-trade-cli"):
            continue
        if line.startswith("Run: npm install -g @okx_ai/okx-trade-cli"):
            continue
        if line.startswith("Version: @okx_ai/okx-trade-cli@"):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _parse_okx_payload(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_okx_error_detail(stdout: str, stderr: str) -> str:
    for candidate in (stdout or "", stderr or ""):
        payload = _parse_okx_payload(candidate)
        if isinstance(payload, dict):
            for key in ("msg", "message", "error", "detail"):
                value = payload.get(key)
                if value:
                    return str(value).strip()
            data = payload.get("data")
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    for key in ("sMsg", "msg", "message", "errMsg"):
                        value = first.get(key)
                        if value:
                            return str(value).strip()
        elif isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                for key in ("sMsg", "msg", "message", "errMsg"):
                    value = first.get(key)
                    if value:
                        return str(value).strip()
    detail = _clean_okx_cli_text(stderr) or _clean_okx_cli_text(stdout)
    return detail or "<empty response>"


def _extract_okx_data_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    return []


def _okx_rest_post(path: str, payload: Dict[str, Any], profile: str = "live") -> Dict[str, Any]:
    from config.settings import OKX_API_KEY, OKX_API_SECRET, OKX_PASSPHRASE

    if not (OKX_API_KEY and OKX_API_SECRET and OKX_PASSPHRASE):
        raise RuntimeError("Missing OKX API credentials for authenticated REST call")

    body = json.dumps(payload, separators=(",", ":"))
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    prehash = f"{timestamp}POST{path}{body}"
    signature = base64.b64encode(
        hmac.new(
            OKX_API_SECRET.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    cmd = [
        "curl", "-sS", "-X", "POST", f"https://www.okx.com{path}",
        "-H", "Content-Type: application/json",
        "-H", f"OK-ACCESS-KEY: {OKX_API_KEY}",
        "-H", f"OK-ACCESS-SIGN: {signature}",
        "-H", f"OK-ACCESS-TIMESTAMP: {timestamp}",
        "-H", f"OK-ACCESS-PASSPHRASE: {OKX_PASSPHRASE}",
        "-H", "User-Agent: curl/8.7.1",
        "--data", body,
    ]
    if profile == "demo":
        cmd.extend(["-H", "x-simulated-trading: 1"])

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        raise RuntimeError(str(e)) from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(detail or f"curl exited with {r.returncode}")
    raw = (r.stdout or "").strip()

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid OKX REST response: {raw[:500]}") from e
    if str(data.get("code")) not in {"0", ""}:
        raise RuntimeError(data.get("msg") or raw[:500])
    return data


def _adjust_isolated_margin(inst_id: str, pos_side: str, amount: float, profile: str = "live") -> Dict[str, Any]:
    return _okx_rest_post(
        "/api/v5/account/position/margin-balance",
        {
            "instId": inst_id,
            "posSide": pos_side,
            "type": "add",
            "amt": f"{amount:.8f}",
        },
        profile,
    )


def _get_account_max_size(
    inst_id: str,
    side: str,
    price: float,
    profile: str = "live",
    td_mode: str = "cross",
) -> Optional[int]:
    if price <= 0:
        return None
    payload = _atk_json(
        ["account", "max-size", "--instId", inst_id, "--tdMode", td_mode, "--px", f"{price:.12f}"],
        profile,
    )
    rows = _extract_okx_data_rows(payload)
    if not rows:
        return None
    row = rows[0]
    key = "maxBuy" if side == "buy" else "maxSell"
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        max_size = float(value)
    except Exception:
        return None
    if max_size <= 0:
        return 0
    return int(math.floor(max_size))


DEFAULT_CONFIG: Dict = {
    "profile":              "live",
    "reconcile_sec":        10,
    "heartbeat_sec":        30,
    "margin_mode":          "isolated",

    # Martingale
    "round_margins":        [50, 100, 200, 400],   # USDT per round
    "target_roi_pct":       0.20,                   # 20% of cumulative invested
    "total_budget":         1000,
    "reserve_margin_ratio": 1.0,                    # reserve follows round margin 1:1

    # Leverage
    "default_lever":        50,
    "high_vol_lever":       30,     # when ATR% > 3%
    "low_vol_lever":        75,     # when ATR% < 1%

    # Entry thresholds
    "ema_fast":             9,
    "ema_slow":             21,
    "rsi_long_low":         53,
    "rsi_long_high":        78,
    "rsi_short_low":        22,
    "rsi_short_high":       47,
    "volume_mult_threshold": 1.1,   # balanced_v2: widen RSI + lower volume gate

    # Exit
    "hard_stop_pct":        0.60,   # stop at -60% of margin (before liq)
    "trail_activate_pct":   0.50,   # activate trail at 50% of target
    "trail_distance_pct":   0.40,   # trail keeps 60% of peak unrealized
    "time_decay_hours":     4,      # close if <5% of target after 4h
    "time_decay_min_pct":   0.05,

    # Reversal detection
    "reversal_threshold":   0.45,   # close on composite >= 0.45
    "tighten_threshold":    0.30,   # tighten stop on composite >= 0.30

    # Re-entry
    "cooldown_min":         30,     # wait 30 min after liquidation
    "max_same_dir_losses":  3,      # flip direction after 3 same-dir losses
}

STATE_FILE = Path(__file__).resolve().parents[2] / "logs" / "yolo_momentum_state.json"
LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"


# ---------------------------------------------------------------------------
# Helpers: OKX CLI calls
# ---------------------------------------------------------------------------

def _atk_call(args: List[str], profile: str = "live", timeout: int = 30) -> Optional[str]:
    """Call OKX CLI and return stdout or None on failure."""
    cmd = ["okx", "--profile", profile, "--json"] + args
    for attempt in range(1, _ATK_RETRIES + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0:
                return r.stdout.strip()
            err = (r.stderr or r.stdout)[:300]
            if (
                len(args) >= 3
                and args[0] == "market"
                and args[1] == "candles"
                and "doesn't exist" in (r.stderr or r.stdout or "")
            ):
                _mark_invalid_instrument(args[2], profile)
            if attempt < _ATK_RETRIES:
                logger.warning(
                    "ATK call failed (%d/%d): %s → %s",
                    attempt, _ATK_RETRIES, " ".join(args), err,
                )
                time.sleep(_ATK_RETRY_DELAY_SEC * attempt)
                continue
            logger.warning("ATK call failed: %s → %s", " ".join(args), err)
            return None
        except Exception as e:
            if attempt < _ATK_RETRIES:
                logger.warning(
                    "ATK exception (%d/%d): %s → %s",
                    attempt, _ATK_RETRIES, " ".join(args), e,
                )
                time.sleep(_ATK_RETRY_DELAY_SEC * attempt)
                continue
            logger.error("ATK exception: %s → %s", " ".join(args), e)
            return None
    return None


def _atk_json(args: List[str], profile: str = "live") -> Any:
    """Call OKX CLI and return parsed JSON."""
    raw = _atk_call(args, profile)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return None


def _fetch_candles(inst_id: str, bar: str = "15m", limit: int = 100, profile: str = "live") -> Optional[List[List]]:
    """Fetch OHLCV candles via CLI. Returns list of [ts, o, h, l, c, vol, ...]."""
    if _is_invalid_instrument(inst_id, profile):
        return None
    raw = _atk_call(["market", "candles", inst_id, "--bar", bar, "--limit", str(limit)], profile)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        # OKX returns newest first — reverse for chronological
        if isinstance(data, list) and data:
            if isinstance(data[0], list):
                return list(reversed(data))
            elif isinstance(data[0], dict):
                return list(reversed(data))
        return data
    except Exception:
        return None


def _fetch_ticker(inst_id: str, profile: str = "live") -> Optional[Dict]:
    """Fetch current ticker."""
    return _atk_json(["market", "ticker", inst_id], profile)


def _fetch_funding(inst_id: str, profile: str = "live") -> Optional[Dict]:
    """Fetch current funding rate."""
    return _atk_json(["market", "funding-rate", inst_id], profile)


# ---------------------------------------------------------------------------
# Technical Analysis Helpers
# ---------------------------------------------------------------------------

def calc_ema(closes: List[float], period: int) -> List[float]:
    """Exponential Moving Average."""
    if len(closes) < period:
        return closes[:]
    k = 2.0 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for i in range(period, len(closes)):
        ema.append(closes[i] * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """RSI from close prices. Returns latest RSI value."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Tuple[float, float, float]]:
    """MACD line, signal line, histogram. Returns latest values."""
    if len(closes) < slow + signal:
        return None
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    # Align lengths
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[offset + i] - ema_slow[i] for i in range(len(ema_slow))]
    if len(macd_line) < signal:
        return None
    sig_line = calc_ema(macd_line, signal)
    offset2 = len(macd_line) - len(sig_line)
    hist = macd_line[-1] - sig_line[-1]
    return (macd_line[-1], sig_line[-1], hist)


def calc_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """Average True Range. Returns latest ATR."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    return atr


def calc_adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """Average Directional Index."""
    if len(closes) < 2 * period + 1:
        return None

    plus_dm = []
    minus_dm = []
    trs = []

    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    # Smooth with Wilder's method
    def wilder_smooth(data, n):
        s = [sum(data[:n])]
        for i in range(n, len(data)):
            s.append(s[-1] - s[-1] / n + data[i])
        return s

    smooth_tr = wilder_smooth(trs, period)
    smooth_plus = wilder_smooth(plus_dm, period)
    smooth_minus = wilder_smooth(minus_dm, period)

    dx_vals = []
    for i in range(len(smooth_tr)):
        if smooth_tr[i] == 0:
            continue
        plus_di = 100 * smooth_plus[i] / smooth_tr[i]
        minus_di = 100 * smooth_minus[i] / smooth_tr[i]
        denom = plus_di + minus_di
        if denom == 0:
            continue
        dx_vals.append(100 * abs(plus_di - minus_di) / denom)

    if len(dx_vals) < period:
        return None

    adx = sum(dx_vals[-period:]) / period
    return adx


def parse_candles(raw_candles: List) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
    """Parse OKX candle data into (opens, highs, lows, closes, volumes)."""
    opens, highs, lows, closes, volumes = [], [], [], [], []
    for c in raw_candles:
        if isinstance(c, list):
            # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
            opens.append(float(c[1]))
            highs.append(float(c[2]))
            lows.append(float(c[3]))
            closes.append(float(c[4]))
            volumes.append(float(c[5]))
        elif isinstance(c, dict):
            opens.append(float(c.get("o", 0)))
            highs.append(float(c.get("h", 0)))
            lows.append(float(c.get("l", 0)))
            closes.append(float(c.get("c", 0)))
            volumes.append(float(c.get("vol", 0)))
    return opens, highs, lows, closes, volumes


# ---------------------------------------------------------------------------
# Contract Selection & Signal Analysis
# ---------------------------------------------------------------------------

@dataclass
class ContractAnalysis:
    """Analysis result for a single contract."""
    inst_id: str
    direction: str          # "long" or "short"
    score: float            # 0-100 composite score
    adx: float              # trend strength
    ema_alignment: float    # 0 or 1 per timeframe pair
    volume_ratio: float     # current vol / avg vol
    funding_edge: float     # contrarian funding signal
    atr_pct: float          # ATR as % of price
    rsi_15m: float
    macd_hist_15m: float
    details: str = ""


def _compact_candidate(analysis: ContractAnalysis) -> Dict[str, Any]:
    return {
        "inst_id": analysis.inst_id,
        "direction": analysis.direction,
        "score": round(analysis.score, 2),
        "adx": round(analysis.adx, 2),
        "ema_alignment": round(analysis.ema_alignment, 2),
        "volume_ratio": round(analysis.volume_ratio, 2),
        "funding_edge": round(analysis.funding_edge, 4),
        "atr_pct": round(analysis.atr_pct, 3),
        "rsi_15m": round(analysis.rsi_15m, 2),
        "macd_hist_15m": round(analysis.macd_hist_15m, 5),
        "details": analysis.details,
    }


def analyze_contract(inst_id: str, profile: str = "live") -> Optional[ContractAnalysis]:
    """
    Score a contract for momentum trading suitability.
    Uses 15m, 1H, 4H candles for multi-timeframe analysis.
    """
    # Fetch candles at multiple timeframes
    candles_15m = _fetch_candles(inst_id, "15m", 100, profile)
    candles_1h = _fetch_candles(inst_id, "1H", 100, profile)
    candles_4h = _fetch_candles(inst_id, "4H", 50, profile)

    if not candles_15m or not candles_1h or not candles_4h:
        logger.warning("Failed to fetch candles for %s", inst_id)
        return None

    _, h_1h, l_1h, c_1h, v_1h = parse_candles(candles_1h)
    _, h_15m, l_15m, c_15m, v_15m = parse_candles(candles_15m)
    _, h_4h, l_4h, c_4h, v_4h = parse_candles(candles_4h)

    if len(c_1h) < 30 or len(c_15m) < 30 or len(c_4h) < 20:
        return None

    # 1. ADX on 1H (trend strength) — 35% weight
    adx = calc_adx(h_1h, l_1h, c_1h, 14)
    if adx is None:
        adx = 20.0
    adx_score = min(adx, 60) / 60 * 100  # normalize to 0-100

    # 2. EMA alignment across timeframes — 25% weight
    ema_alignments = 0
    direction_votes = {"long": 0, "short": 0}

    for closes, label in [(c_15m, "15m"), (c_1h, "1H"), (c_4h, "4H")]:
        ema9 = calc_ema(closes, 9)
        ema21 = calc_ema(closes, 21)
        if ema9 and ema21:
            if ema9[-1] > ema21[-1]:
                direction_votes["long"] += 1
                ema_alignments += 1
            else:
                direction_votes["short"] += 1
                ema_alignments += 1

    # Also check 1H EMA(50) for longer trend
    ema50_1h = calc_ema(c_1h, 50)
    if ema50_1h and len(ema50_1h) > 0:
        if c_1h[-1] > ema50_1h[-1]:
            direction_votes["long"] += 1
        else:
            direction_votes["short"] += 1

    # Direction = majority vote
    direction = "long" if direction_votes["long"] >= direction_votes["short"] else "short"
    # Alignment score: how many agree with majority
    majority = max(direction_votes["long"], direction_votes["short"])
    total_votes = direction_votes["long"] + direction_votes["short"]
    alignment_score = (majority / total_votes * 100) if total_votes > 0 else 50

    # 3. Volume confirmation — 20% weight
    avg_vol = sum(v_15m[-20:]) / 20 if len(v_15m) >= 20 else sum(v_15m) / len(v_15m)
    current_vol = v_15m[-1] if v_15m else 0
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
    vol_score = min(vol_ratio / 2.0, 1.0) * 100  # 2x avg = 100

    # 4. Funding rate edge — 10% weight
    funding_data = _fetch_funding(inst_id, profile)
    funding_rate = 0.0
    if funding_data:
        if isinstance(funding_data, list) and funding_data:
            funding_rate = float(funding_data[0].get("fundingRate", 0) or 0)
        elif isinstance(funding_data, dict):
            funding_rate = float(funding_data.get("fundingRate", 0) or 0)

    # Contrarian: negative funding when going long is good, positive when shorting
    if direction == "long":
        funding_edge = max(0, -funding_rate * 10000)  # negative funding = edge for longs
    else:
        funding_edge = max(0, funding_rate * 10000)  # positive funding = edge for shorts
    funding_score = min(funding_edge * 10, 100)

    # 5. Volatility sweet spot — 10% weight
    atr = calc_atr(h_1h, l_1h, c_1h, 14)
    price = c_1h[-1] if c_1h else 1.0
    atr_pct = (atr / price * 100) if atr and price > 0 else 1.5
    # Sweet spot: 1-3% ATR gets high score
    if 1.0 <= atr_pct <= 3.0:
        vol_sweet_score = 100
    elif 0.5 <= atr_pct < 1.0 or 3.0 < atr_pct <= 5.0:
        vol_sweet_score = 60
    else:
        vol_sweet_score = 30

    # Composite score
    composite = (
        0.35 * adx_score +
        0.25 * alignment_score +
        0.20 * vol_score +
        0.10 * funding_score +
        0.10 * vol_sweet_score
    )

    # RSI & MACD for entry gating
    rsi = calc_rsi(c_15m, 14) or 50.0
    macd = calc_macd(c_15m, 12, 26, 9)
    macd_hist = macd[2] if macd else 0.0

    details = (
        f"ADX={adx:.1f} align={alignment_score:.0f}% vol_ratio={vol_ratio:.2f} "
        f"funding={funding_rate:.6f} atr%={atr_pct:.2f} RSI={rsi:.1f} "
        f"MACD_hist={macd_hist:.4f} votes=L{direction_votes['long']}/S{direction_votes['short']}"
    )

    return ContractAnalysis(
        inst_id=inst_id,
        direction=direction,
        score=composite,
        adx=adx,
        ema_alignment=alignment_score,
        volume_ratio=vol_ratio,
        funding_edge=funding_edge,
        atr_pct=atr_pct,
        rsi_15m=rsi,
        macd_hist_15m=macd_hist,
        details=details,
    )


def select_best_contract(profile: str = "live", progress_cb=None) -> Tuple[Optional[ContractAnalysis], Dict[str, Any]]:
    """
    Dynamically discover all liquid USDT perps, score them,
    and return the single best momentum candidate.
    """
    universe = fetch_universe(profile)
    scan_status: Dict[str, Any] = {
        "stage": "scanning",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "universe_size": len(universe),
        "scanned_count": 0,
        "failed_symbols": [],
        "current_symbol": None,
        "top_candidates": [],
        "selected_candidate": None,
        "selection_reason": "",
    }
    if not universe:
        logger.warning("Empty universe — cannot select contract")
        scan_status["stage"] = "empty_universe"
        scan_status["completed_at"] = datetime.now(timezone.utc).isoformat()
        return None, scan_status

    logger.info("Scoring %d contracts for momentum...", len(universe))
    if progress_cb:
        progress_cb(scan_status)

    # Rate-limit friendly: analyze in batches with small delays
    results = []
    for i, sym in enumerate(universe):
        if _is_invalid_instrument(sym, profile):
            if len(scan_status["failed_symbols"]) < 12:
                scan_status["failed_symbols"].append(sym)
            continue
        scan_status["current_symbol"] = sym
        analysis = analyze_contract(sym, profile)
        scan_status["scanned_count"] = i + 1
        if analysis:
            results.append(analysis)
            results.sort(key=lambda x: x.score, reverse=True)
            scan_status["top_candidates"] = [_compact_candidate(r) for r in results[:5]]
            if analysis.score >= 50:
                logger.info("Contract analysis [%s]: score=%.1f dir=%s %s",
                            sym, analysis.score, analysis.direction, analysis.details)
        else:
            if len(scan_status["failed_symbols"]) < 12:
                scan_status["failed_symbols"].append(sym)
        if progress_cb and ((i + 1) <= 3 or (i + 1) % 5 == 0 or i == len(universe) - 1):
            progress_cb(scan_status)
        # OKX public rate limit: 20 req/2s. Each analyze_contract does ~4 calls.
        # Throttle after every 3 symbols to stay safe.
        if (i + 1) % 3 == 0:
            time.sleep(1.0)

    if not results:
        scan_status["stage"] = "no_candidate"
        scan_status["completed_at"] = datetime.now(timezone.utc).isoformat()
        return None, scan_status

    # Sort by score descending
    results.sort(key=lambda x: x.score, reverse=True)
    top5 = results[:5]
    logger.info("Top 5 candidates:")
    for r in top5:
        logger.info("  %s score=%.1f dir=%s %s", r.inst_id, r.score, r.direction, r.details)

    best = results[0]
    logger.info("SELECTED: %s direction=%s score=%.1f", best.inst_id, best.direction, best.score)
    scan_status["stage"] = "selected"
    scan_status["completed_at"] = datetime.now(timezone.utc).isoformat()
    scan_status["top_candidates"] = [_compact_candidate(r) for r in top5]
    scan_status["selected_candidate"] = _compact_candidate(best)
    scan_status["selection_reason"] = best.details
    return best, scan_status


# ---------------------------------------------------------------------------
# Entry Signal Validation (Triple Confirmation Gate)
# ---------------------------------------------------------------------------

def validate_entry(analysis: ContractAnalysis, cfg: Dict) -> bool:
    """
    Triple confirmation gate:
    A) Trend gate: 1H and 4H EMAs agree with direction
    B) Momentum burst: RSI in valid range + MACD expanding
    C) Volume gate: volume > threshold x average
    """
    direction = analysis.direction

    # A) Trend gate — already validated via ema_alignment
    if analysis.ema_alignment < 60:  # need at least 60% agreement
        logger.info("Entry BLOCKED: EMA alignment %.0f%% < 60%%", analysis.ema_alignment)
        return False

    # B) Momentum burst gate
    # MACD histogram is in absolute price units — normalize by price so
    # the threshold works across $0.0001 memecoins and $80k BTC alike.
    # A histogram of 0.05% of price is essentially flat (noise zone).
    rsi = analysis.rsi_15m
    macd_h = analysis.macd_hist_15m
    if direction == "long":
        if not (cfg["rsi_long_low"] <= rsi <= cfg["rsi_long_high"]):
            logger.info("Entry BLOCKED: RSI %.1f not in long range [%d, %d]",
                        rsi, cfg["rsi_long_low"], cfg["rsi_long_high"])
            return False
        if macd_h < 0 and abs(macd_h) > analysis.atr_pct * 0.001:
            logger.info("Entry BLOCKED: MACD histogram %.6f meaningfully negative for long", macd_h)
            return False
    else:  # short
        if not (cfg["rsi_short_low"] <= rsi <= cfg["rsi_short_high"]):
            logger.info("Entry BLOCKED: RSI %.1f not in short range [%d, %d]",
                        rsi, cfg["rsi_short_low"], cfg["rsi_short_high"])
            return False
        if macd_h > 0 and abs(macd_h) > analysis.atr_pct * 0.001:
            logger.info("Entry BLOCKED: MACD histogram %.6f meaningfully positive for short", macd_h)
            return False

    # C) Volume gate
    if analysis.volume_ratio < cfg["volume_mult_threshold"]:
        logger.info("Entry BLOCKED: volume ratio %.2f < %.2f",
                    analysis.volume_ratio, cfg["volume_mult_threshold"])
        return False

    logger.info("Entry VALIDATED: %s %s — RSI=%.1f MACD_hist=%.4f vol_ratio=%.2f",
                direction, analysis.inst_id, rsi, analysis.macd_hist_15m, analysis.volume_ratio)
    return True


# ---------------------------------------------------------------------------
# Reversal Detection
# ---------------------------------------------------------------------------

def detect_reversal(inst_id: str, pos_side: str, entry_price: float, cfg: Dict) -> Tuple[float, str]:
    """
    Composite reversal score. Returns (score, details).
    Score >= 0.45 → close immediately
    Score >= 0.30 → tighten stop to breakeven
    """
    candles_15m = _fetch_candles(inst_id, "15m", 50, cfg.get("profile", "live"))
    candles_1h = _fetch_candles(inst_id, "1H", 50, cfg.get("profile", "live"))

    if not candles_15m or not candles_1h:
        return 0.0, "no_data"

    _, h_15m, l_15m, c_15m, v_15m = parse_candles(candles_15m)
    _, h_1h, l_1h, c_1h, v_1h = parse_candles(candles_1h)

    signals = {}

    # 1. Volume climax + reversal candle (30% weight)
    volume_climax = 0.0
    if len(v_15m) >= 20:
        avg_vol = sum(v_15m[-20:]) / 20
        if v_15m[-1] > 5 * avg_vol:
            # Check for reversal candle (long wick)
            last_body = abs(c_15m[-1] - c_15m[-2]) if len(c_15m) >= 2 else 0
            last_range = h_15m[-1] - l_15m[-1] if h_15m[-1] > l_15m[-1] else 0.001
            wick_ratio = 1 - (last_body / last_range)
            if wick_ratio > 0.6:
                volume_climax = 1.0
    signals["volume_climax"] = volume_climax

    # 2. RSI divergence (25% weight)
    rsi_div = 0.0
    rsi_vals = []
    if len(c_15m) >= 28:
        for i in range(-3, 0):
            r = calc_rsi(c_15m[:len(c_15m) + i + 1], 14)
            if r:
                rsi_vals.append(r)
    if len(rsi_vals) >= 2:
        if pos_side == "long":
            # Bearish divergence: price higher but RSI lower
            if c_15m[-1] > c_15m[-3] and rsi_vals[-1] < rsi_vals[0]:
                rsi_div = 1.0
        else:
            # Bullish divergence: price lower but RSI higher
            if c_15m[-1] < c_15m[-3] and rsi_vals[-1] > rsi_vals[0]:
                rsi_div = 1.0
    signals["rsi_divergence"] = rsi_div

    # 3. EMA cross against position (20% weight)
    ema_cross = 0.0
    ema9 = calc_ema(c_1h, 9)
    ema21 = calc_ema(c_1h, 21)
    if ema9 and ema21 and len(ema9) >= 2 and len(ema21) >= 2:
        if pos_side == "long" and ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]:
            ema_cross = 1.0
        elif pos_side == "short" and ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]:
            ema_cross = 1.0
    signals["ema_cross"] = ema_cross

    # 4. Funding extreme (15% weight)
    funding_extreme = 0.0
    funding_data = _fetch_funding(inst_id, cfg.get("profile", "live"))
    if funding_data:
        rate = 0.0
        if isinstance(funding_data, list) and funding_data:
            rate = float(funding_data[0].get("fundingRate", 0) or 0)
        elif isinstance(funding_data, dict):
            rate = float(funding_data.get("fundingRate", 0) or 0)
        if pos_side == "long" and rate > 0.001:  # 0.1% = very crowded long
            funding_extreme = 1.0
        elif pos_side == "short" and rate < -0.001:
            funding_extreme = 1.0
    signals["funding_extreme"] = funding_extreme

    # 5. MACD histogram shrinking (10% weight)
    macd_div = 0.0
    if len(c_15m) >= 40:
        macd_now = calc_macd(c_15m, 12, 26, 9)
        macd_prev = calc_macd(c_15m[:-3], 12, 26, 9)
        if macd_now and macd_prev:
            if pos_side == "long":
                if macd_now[2] < macd_prev[2] and macd_now[2] > 0:  # shrinking positive
                    macd_div = 1.0
            else:
                if macd_now[2] > macd_prev[2] and macd_now[2] < 0:  # shrinking negative
                    macd_div = 1.0
    signals["macd_divergence"] = macd_div

    composite = (
        0.30 * signals["volume_climax"] +
        0.25 * signals["rsi_divergence"] +
        0.20 * signals["ema_cross"] +
        0.15 * signals["funding_extreme"] +
        0.10 * signals["macd_divergence"]
    )

    details = " ".join(f"{k}={v:.1f}" for k, v in signals.items())
    return composite, details


# ---------------------------------------------------------------------------
# Strategy State
# ---------------------------------------------------------------------------

@dataclass
class YoloState:
    """Persistent strategy state."""
    round: int = 1
    profile: str = "live"
    total_budget: float = 0.0
    cumulative_invested: float = 0.0
    current_margin: float = 0.0
    target_profit: float = 0.0
    status: str = "HUNTING"  # HUNTING | IN_POSITION | TARGET_HIT | ROUND_LOST | DONE | RECHARGE_REQUIRED
    inst_id: Optional[str] = None
    side: Optional[str] = None
    entry_price: Optional[float] = None
    sz: int = 0
    leverage: int = 50
    entry_time: Optional[float] = None
    peak_unrealized: float = 0.0
    trailing_active: bool = False
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    same_dir_losses: int = 0
    last_loss_dir: Optional[str] = None
    cooldown_until: Optional[float] = None
    history: List[Dict] = field(default_factory=list)
    scan_status: Dict[str, Any] = field(default_factory=dict)
    last_block_reason: str = ""
    state_file: Optional[Path] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict:
        return {
            "round": self.round,
            "profile": self.profile,
            "total_budget": self.total_budget,
            "cumulative_invested": self.cumulative_invested,
            "current_margin": self.current_margin,
            "target_profit": self.target_profit,
            "status": self.status,
            "inst_id": self.inst_id,
            "side": self.side,
            "entry_price": self.entry_price,
            "sz": self.sz,
            "leverage": self.leverage,
            "entry_time": self.entry_time,
            "peak_unrealized": self.peak_unrealized,
            "trailing_active": self.trailing_active,
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "same_dir_losses": self.same_dir_losses,
            "last_loss_dir": self.last_loss_dir,
            "cooldown_until": self.cooldown_until,
            "history": self.history,
            "scan_status": self.scan_status,
            "last_block_reason": self.last_block_reason,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "YoloState":
        s = cls()
        for k, v in d.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s

    def save(self, state_file: Optional[Path] = None):
        path = Path(state_file or self.state_file or STATE_FILE)
        self.state_file = path
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, state_file: Optional[Path] = None) -> "YoloState":
        path = Path(state_file or STATE_FILE)
        if path.exists():
            try:
                with open(path) as f:
                    state = cls.from_dict(json.load(f))
                    state.state_file = path
                    return state
            except Exception as e:
                logger.warning("Failed to load state: %s", e)
        state = cls()
        state.state_file = path
        return state


# ---------------------------------------------------------------------------
# Main Strategy Class
# ---------------------------------------------------------------------------

class YoloMomentumStrategy:
    """
    Aggressive leveraged momentum with martingale doubling.
    Runs as standalone async loop with REST polling.
    """

    def __init__(self, config: Optional[Dict] = None, state_file: Optional[Path] = None):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        profile_override = os.getenv("STRATEGY_PROFILE")
        if profile_override in ("demo", "live"):
            self.cfg["profile"] = profile_override
        budget_override = os.getenv("YOLO_TOTAL_BUDGET")
        if budget_override:
            try:
                self.cfg["total_budget"] = float(budget_override)
            except ValueError:
                logger.warning("Ignoring invalid YOLO_TOTAL_BUDGET=%r", budget_override)
        self.profile = self.cfg["profile"]
        default_state_file = STATE_FILE
        if not state_file and self.profile == "live":
            default_state_file = LOGS_DIR / "yolo_momentum_live_state.json"
        self.state_file = Path(state_file) if state_file else default_state_file
        if os.getenv("YOLO_RESET_STATE") == "1" and self.state_file.exists():
            try:
                self.state_file.unlink()
            except Exception as e:
                logger.warning("Failed to clear state file %s: %s", self.state_file, e)
        self.state = YoloState.load(self.state_file)
        self.state.profile = self.profile
        self.state.total_budget = float(self.cfg.get("total_budget") or 0)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_unrealized_pnl = 0.0
        self._last_order_error = ""
        self._last_set_leverage_error = ""
        self._last_margin_adjust_error = ""

        # Initialize round if fresh start
        if self.state.cumulative_invested == 0:
            self._init_round(1)

    def _required_capital_for_margin(self, margin: float) -> float:
        reserve_ratio = float(self.cfg.get("reserve_margin_ratio") or 0.0)
        return float(margin) * (1.0 + reserve_ratio)

    def _init_round(self, round_num: int):
        """Initialize a new round with appropriate margin."""
        margins = self.cfg["round_margins"]
        if round_num > len(margins):
            logger.warning("All rounds exhausted. Strategy DONE.")
            self.state.status = "DONE"
            self.state.save()
            return

        margin = margins[round_num - 1]
        budget = float(self.cfg.get("total_budget") or 0)
        needed = self._required_capital_for_margin(margin)
        if budget > 0 and needed > budget:
            self._pause_for_recharge(round_num, needed, budget, margin)
            return

        self.state.round = round_num
        self.state.current_margin = margin
        self.state.cumulative_invested += margin
        # Target = 20% of cumulative invested + recover all prior losses
        prior_losses = abs(min(self.state.realized_pnl, 0))
        self.state.target_profit = self.state.cumulative_invested * self.cfg["target_roi_pct"] + prior_losses
        self.state.status = "HUNTING"
        self.state.inst_id = None
        self.state.side = None
        self.state.entry_price = None
        self.state.sz = 0
        self.state.peak_unrealized = 0.0
        self.state.trailing_active = False
        self.state.entry_time = None
        self.state.scan_status = {}
        self.state.last_block_reason = ""
        self.state.save()

        logger.info(
            "Round %d initialized: margin=%.0f reserve_ratio=%.2f required_capital=%.0f "
            "cumulative=%.0f target_profit=%.2f prior_losses=%.2f",
            round_num, margin, float(self.cfg.get("reserve_margin_ratio") or 0.0),
            needed, self.state.cumulative_invested,
            self.state.target_profit, prior_losses,
        )

    def _pause_for_recharge(self, next_round: int, needed_budget: float, budget: float, margin: float):
        """Stop advancing rounds when configured capital cannot support the next round."""
        msg = (
            f"Recharge required before round {next_round}: "
            f"need ${needed_budget:.0f} isolated capital bucket "
            f"(margin ${margin:.0f} + reserve ${needed_budget - margin:.0f}), "
            f"configured total_budget=${budget:.0f}. Paused after round {self.state.round}."
        )
        self.state.status = "RECHARGE_REQUIRED"
        self.state.current_margin = 0.0
        self.state.scan_status = {}
        self.state.last_block_reason = msg
        self.state.save()
        logger.warning(msg)

    def start(self):
        """Start the strategy in a background thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("YoloMomentum started (round=%d status=%s)", self.state.round, self.state.status)

    def stop(self):
        """Stop the strategy gracefully."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
        logger.info("YoloMomentum stopped")

    def _run_loop(self):
        """Main loop: hunt for entries, manage positions, check exits."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.error("YoloMomentum: main loop error: %s", e)
        finally:
            loop.close()

    async def _async_main(self):
        """Async main: runs hunting and reconciliation concurrently."""
        tasks = [
            asyncio.create_task(self._hunting_loop()),
            asyncio.create_task(self._reconcile_loop()),
            asyncio.create_task(self._diagnostics_loop()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    logger.error("Task failed: %s", t.exception())
            for t in pending:
                t.cancel()
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()

    async def _hunting_loop(self):
        """Look for entry opportunities when in HUNTING state."""
        while not self._stop_event.is_set():
            try:
                if self.state.status == "HUNTING":
                    await self._hunt_for_entry()
                elif self.state.status in ("TARGET_HIT", "DONE", "RECHARGE_REQUIRED"):
                    logger.info("Strategy complete: status=%s. Sleeping.", self.state.status)
                    await asyncio.sleep(60)
                elif self.state.status == "ROUND_LOST":
                    # Check cooldown
                    if self.state.cooldown_until and time.time() < self.state.cooldown_until:
                        remaining = self.state.cooldown_until - time.time()
                        logger.info("Cooldown: %.0f seconds remaining", remaining)
                        await asyncio.sleep(min(remaining, 30))
                        continue
                    # Start next round
                    next_round = self.state.round + 1
                    if next_round > len(self.cfg["round_margins"]):
                        self.state.status = "DONE"
                        self.state.save()
                        logger.warning("All rounds exhausted. Budget depleted.")
                        continue
                    self._init_round(next_round)

                # Sleep between scans
                await asyncio.sleep(30)  # scan every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Hunting loop error: %s", e)
                await asyncio.sleep(10)

    async def _hunt_for_entry(self):
        """Analyze contracts and enter if triple confirmation passes."""
        logger.info("Hunting for entry (round=%d margin=%.0f target=%.2f)...",
                    self.state.round, self.state.current_margin, self.state.target_profit)

        # Check for forced direction flip
        forced_dir = None
        if self.state.same_dir_losses >= self.cfg["max_same_dir_losses"] and self.state.last_loss_dir:
            forced_dir = "short" if self.state.last_loss_dir == "long" else "long"
            logger.info("Forcing direction=%s after %d same-dir losses",
                        forced_dir, self.state.same_dir_losses)

        def _persist_scan_progress(scan_status: Dict[str, Any]) -> None:
            # Called from the executor thread while universe scanning is in progress.
            self.state.scan_status = json.loads(json.dumps(scan_status))
            self.state.save()

        def _select_with_progress():
            return select_best_contract(self.profile, progress_cb=_persist_scan_progress)

        # Run contract selection (blocking, in executor)
        best, scan_status = await asyncio.get_event_loop().run_in_executor(
            None, _select_with_progress
        )
        self.state.scan_status = scan_status
        self.state.save()
        if best is None:
            logger.warning("No suitable contract found")
            return

        # Apply forced direction if needed
        if forced_dir and best.direction != forced_dir:
            logger.info("Overriding direction: %s → %s (forced flip)", best.direction, forced_dir)
            best.direction = forced_dir

        # Validate entry with triple confirmation
        valid = await asyncio.get_event_loop().run_in_executor(
            None, validate_entry, best, self.cfg
        )
        if not valid:
            self.state.last_block_reason = f"Entry gate blocked for {best.inst_id}: {best.details}"
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "blocked",
                "blocked_candidate": _compact_candidate(best),
                "blocked_reason": self.state.last_block_reason,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.info("Entry not validated — waiting for better setup")
            return

        # Calculate leverage based on volatility, then cap it to the
        # exchange-supported max leverage for this exact contract.
        desired_lever = self.cfg["default_lever"]
        if best.atr_pct > 3.0:
            desired_lever = self.cfg["high_vol_lever"]
        elif best.atr_pct < 1.0:
            desired_lever = self.cfg["low_vol_lever"]

        max_lever = _get_max_leverage(best.inst_id, self.profile)
        lever = desired_lever
        if max_lever > 0 and desired_lever > max_lever:
            lever = int(max_lever)
            logger.info(
                "Leverage capped by exchange: %s desired=%dx max=%sx effective=%dx",
                best.inst_id, desired_lever, int(max_lever), lever,
            )
        if lever <= 0:
            logger.warning("Invalid effective leverage for %s: desired=%s max=%s", best.inst_id, desired_lever, max_lever)
            return

        # In isolated mode we spend the configured round margin on entry.
        # Reserve is modeled as separate account capital earmarked for this round.
        margin_to_use = self.state.current_margin
        notional = margin_to_use * lever
        ct_val = _get_contract_value(best.inst_id, self.profile)
        ticker = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_ticker, best.inst_id, self.profile
        )
        if not ticker:
            logger.warning("Failed to get ticker for %s", best.inst_id)
            return

        price = 0.0
        if isinstance(ticker, list) and ticker:
            price = float(ticker[0].get("last", 0) or 0)
        elif isinstance(ticker, dict):
            price = float(ticker.get("last", 0) or 0)
        if price <= 0:
            logger.warning("Invalid price for %s: %s", best.inst_id, price)
            return

        sz = max(1, round(notional / (price * ct_val)))
        specs = _fetch_swap_instrument_specs(self.profile).get(best.inst_id, {})
        lot_sz = max(1.0, float(specs.get("lot_sz") or 1.0))
        min_sz = max(1.0, float(specs.get("min_sz") or 1.0))
        max_mkt_sz = float(specs.get("max_mkt_sz") or 0)
        if max_mkt_sz > 0 and sz > max_mkt_sz:
            clamped = int(max(min_sz, math.floor(max_mkt_sz / lot_sz) * lot_sz))
            logger.info(
                "Order size capped by instrument maxMktSz: %s requested=%d max_mkt_sz=%.0f adjusted=%d",
                best.inst_id, sz, max_mkt_sz, clamped,
            )
            sz = clamped

        account_max_sz = await asyncio.get_event_loop().run_in_executor(
            None,
            _get_account_max_size,
            best.inst_id,
            "buy" if best.direction == "long" else "sell",
            price,
            self.profile,
            str(self.cfg.get("margin_mode") or "isolated"),
        )
        if account_max_sz == 0:
            reason = f"Order blocked by account max-size for {best.inst_id}: current max-size is 0"
            self.state.last_block_reason = reason
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "account_max_size_zero",
                "blocked_candidate": _compact_candidate(best),
                "blocked_reason": reason,
                "desired_leverage": desired_lever,
                "max_allowed_leverage": round(max_lever, 2) if max_lever > 0 else None,
                "planned_leverage": lever,
                "reference_price": round(price, 6),
                "account_max_size": account_max_sz,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.warning(reason)
            return
        if account_max_sz is not None and sz > account_max_sz:
            clamped = int(max(min_sz, math.floor(account_max_sz / lot_sz) * lot_sz))
            logger.info(
                "Order size capped by account max-size: %s requested=%d account_max=%d adjusted=%d",
                best.inst_id, sz, account_max_sz, clamped,
            )
            sz = clamped
        if sz < min_sz:
            reason = (
                f"Order below exchange minimum for {best.inst_id}: "
                f"effective_sz={sz} min_sz={int(min_sz)}"
            )
            self.state.last_block_reason = reason
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "size_below_min",
                "blocked_candidate": _compact_candidate(best),
                "blocked_reason": reason,
                "desired_leverage": desired_lever,
                "max_allowed_leverage": round(max_lever, 2) if max_lever > 0 else None,
                "planned_leverage": lever,
                "reference_price": round(price, 6),
                "account_max_size": account_max_sz,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.warning(reason)
            return

        logger.info(
            "ENTRY SIGNAL: %s %s lever=%dx margin=%.0f notional=%.0f sz=%d price=%.2f",
            best.direction, best.inst_id, lever, margin_to_use, notional, sz, price,
        )
        self.state.last_block_reason = ""
        self.state.scan_status = {
            **self.state.scan_status,
            "stage": "entry_signal",
            "selected_candidate": _compact_candidate(best),
            "desired_leverage": desired_lever,
            "max_allowed_leverage": round(max_lever, 2) if max_lever > 0 else None,
            "planned_leverage": lever,
            "planned_margin": round(margin_to_use, 2),
            "required_capital": round(self._required_capital_for_margin(self.state.current_margin), 2),
            "planned_notional": round(notional, 2),
            "planned_contracts": sz,
            "reference_price": round(price, 6),
            "account_max_size": account_max_sz,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.save()

        hard_stop_ratio = float(self.cfg.get("hard_stop_pct") or 0.0) / float(max(lever, 1))
        if best.direction == "long":
            sl_trigger_px = price * (1.0 - hard_stop_ratio)
        else:
            sl_trigger_px = price * (1.0 + hard_stop_ratio)

        # Set leverage
        success = await asyncio.get_event_loop().run_in_executor(
            None, self._set_leverage, best.inst_id, lever
        )
        if not success:
            detail = self._last_set_leverage_error or f"Set leverage failed for {best.inst_id} at {lever}x"
            self.state.last_block_reason = detail
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "set_leverage_failed",
                "blocked_candidate": _compact_candidate(best),
                "blocked_reason": detail,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.error("Failed to configure leverage for %s", best.inst_id)
            return

        # Place order
        atk_side = "buy" if best.direction == "long" else "sell"
        placed = await asyncio.get_event_loop().run_in_executor(
            None, self._place_order, best.inst_id, atk_side, sz, sl_trigger_px
        )

        if placed:
            self.state.status = "IN_POSITION"
            self.state.inst_id = best.inst_id
            self.state.side = best.direction
            self.state.entry_price = price
            self.state.sz = sz
            self.state.leverage = lever
            self.state.entry_time = time.time()
            self.state.peak_unrealized = 0.0
            self.state.trailing_active = False
            # Estimate entry fee
            entry_notional = sz * ct_val * price
            self.state.total_fees += entry_notional * 0.0005
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "position_opened",
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.info("POSITION OPENED: %s %s sz=%d @ %.2f lever=%dx",
                        best.direction, best.inst_id, sz, price, lever)

            reserve_margin = self.state.current_margin * float(self.cfg.get("reserve_margin_ratio") or 0.0)
            if reserve_margin > 0:
                adjusted = await asyncio.get_event_loop().run_in_executor(
                    None, self._add_isolated_margin, best.inst_id, reserve_margin
                )
                if adjusted:
                    self.state.scan_status = {
                        **self.state.scan_status,
                        "reserve_margin_added": round(reserve_margin, 2),
                        "stage": "margin_added",
                        "margin_added_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self.state.save()
                else:
                    detail = self._last_margin_adjust_error or (
                        f"Add isolated margin failed for {best.inst_id} amt={reserve_margin:.2f}"
                    )
                    self._handle_margin_add_failure(detail, reserve_margin)
        else:
            detail = self._last_order_error or f"Order rejected for {best.inst_id} at {lever}x"
            self.state.last_block_reason = detail
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "order_failed",
                "blocked_candidate": _compact_candidate(best),
                "blocked_reason": detail,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state.save()
            logger.error("Failed to place order for %s", best.inst_id)

    async def _reconcile_loop(self):
        """Monitor position P&L, exits, trailing stops, reversals."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.cfg["reconcile_sec"])
                if self._stop_event.is_set():
                    break
                if self.state.status == "IN_POSITION":
                    await asyncio.get_event_loop().run_in_executor(None, self._reconcile)
                self._write_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Reconcile error: %s", e)

    def _reconcile(self):
        """Check position status and apply exit rules."""
        if not self.state.inst_id or not self.state.entry_price:
            return

        # Get current price
        ticker = _fetch_ticker(self.state.inst_id, self.profile)
        if not ticker:
            return

        price = 0.0
        if isinstance(ticker, list) and ticker:
            price = float(ticker[0].get("last", 0) or 0)
        elif isinstance(ticker, dict):
            price = float(ticker.get("last", 0) or 0)
        if price <= 0:
            return

        entry = self.state.entry_price
        raw_pct = (price - entry) / entry
        pnl_pct = raw_pct if self.state.side == "long" else -raw_pct

        # Notional-based P&L
        ct_val = _get_contract_value(self.state.inst_id, self.profile)
        notional = self.state.sz * ct_val * entry
        unrealized_pnl = notional * pnl_pct
        self._last_unrealized_pnl = unrealized_pnl

        # Time held
        hours_held = (time.time() - (self.state.entry_time or time.time())) / 3600

        # Update peak
        if unrealized_pnl > self.state.peak_unrealized:
            self.state.peak_unrealized = unrealized_pnl

        target = self.state.target_profit
        margin = self.state.current_margin

        logger.info(
            "RECONCILE: %s %s entry=%.2f now=%.2f pnl=%.2f%% unrealized=%.2f/%.2f "
            "peak=%.2f hours=%.1f trail=%s",
            self.state.side, self.state.inst_id, entry, price,
            pnl_pct * 100, unrealized_pnl, target,
            self.state.peak_unrealized, hours_held, self.state.trailing_active,
        )

        exit_reason = None

        # 1. TARGET HIT — the win condition
        if unrealized_pnl >= target:
            exit_reason = "TARGET_HIT"

        # 2. Hard stop-loss
        elif unrealized_pnl <= -(margin * self.cfg["hard_stop_pct"]):
            exit_reason = "HARD_STOP"

        # 3. Trailing stop (after 50% of target reached)
        elif not self.state.trailing_active and unrealized_pnl >= target * self.cfg["trail_activate_pct"]:
            self.state.trailing_active = True
            logger.info("TRAILING STOP ACTIVATED at unrealized=%.2f (%.0f%% of target)",
                        unrealized_pnl, unrealized_pnl / target * 100)

        if self.state.trailing_active and exit_reason is None:
            trail_floor = self.state.peak_unrealized * (1 - self.cfg["trail_distance_pct"])
            if unrealized_pnl < trail_floor:
                exit_reason = "TRAILING_STOP"
                logger.info("TRAILING STOP hit: unrealized=%.2f < floor=%.2f (peak=%.2f)",
                            unrealized_pnl, trail_floor, self.state.peak_unrealized)

        # 4. Time decay
        if exit_reason is None and hours_held >= self.cfg["time_decay_hours"]:
            if unrealized_pnl < target * self.cfg["time_decay_min_pct"]:
                exit_reason = "TIME_DECAY"

        # 5. Reversal detection (every other reconcile to save API calls)
        if exit_reason is None and int(time.time()) % 20 < self.cfg["reconcile_sec"]:
            rev_score, rev_details = detect_reversal(
                self.state.inst_id, self.state.side, entry, self.cfg
            )
            if rev_score >= self.cfg["reversal_threshold"]:
                exit_reason = f"REVERSAL({rev_score:.2f})"
                logger.warning("REVERSAL detected: score=%.2f %s", rev_score, rev_details)
            elif rev_score >= self.cfg["tighten_threshold"] and not self.state.trailing_active:
                # Tighten stop to breakeven
                if unrealized_pnl > 0:
                    self.state.trailing_active = True
                    self.state.peak_unrealized = max(self.state.peak_unrealized, unrealized_pnl)
                    logger.info("TIGHTENED stop to breakeven: reversal_score=%.2f %s",
                                rev_score, rev_details)

        # 6. Check if position still exists on exchange (liquidation detection)
        if exit_reason is None and pnl_pct < -0.4:  # losing >40%, check for liquidation
            positions = _atk_json(["account", "positions"], self.profile)
            has_pos = False
            if positions and isinstance(positions, list):
                for p in positions:
                    if p.get("instId") == self.state.inst_id:
                        pos_val = abs(float(p.get("pos", 0)))
                        if pos_val > 0:
                            has_pos = True
                            break
            if not has_pos:
                exit_reason = "LIQUIDATED"

        # Execute exit
        if exit_reason:
            self._exit_position(exit_reason, unrealized_pnl, pnl_pct)

    def _exit_position(self, reason: str, unrealized_pnl: float, pnl_pct: float):
        """Close position and update state."""
        logger.warning(
            "EXIT: reason=%s %s %s pnl=%.2f (%.2f%%) round=%d",
            reason, self.state.side, self.state.inst_id,
            unrealized_pnl, pnl_pct * 100, self.state.round,
        )

        # Close on exchange (unless liquidated)
        if reason != "LIQUIDATED":
            self._close_position()
            # Estimate exit fee
            ct_val = _get_contract_value(self.state.inst_id, self.profile)
            ticker = _fetch_ticker(self.state.inst_id, self.profile)
            if ticker:
                p = 0.0
                if isinstance(ticker, list) and ticker:
                    p = float(ticker[0].get("last", 0) or 0)
                elif isinstance(ticker, dict):
                    p = float(ticker.get("last", 0) or 0)
                if p > 0:
                    self.state.total_fees += self.state.sz * ct_val * p * 0.0005

        # Record trade in history
        trade = {
            "round": self.state.round,
            "inst_id": self.state.inst_id,
            "side": self.state.side,
            "entry_price": self.state.entry_price,
            "sz": self.state.sz,
            "leverage": self.state.leverage,
            "pnl": unrealized_pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        self.state.history.append(trade)

        # Update realized PnL
        if reason == "LIQUIDATED":
            self.state.realized_pnl -= self.state.current_margin
        else:
            self.state.realized_pnl += unrealized_pnl

        # Track same-direction losses
        if unrealized_pnl < 0:
            if self.state.last_loss_dir == self.state.side:
                self.state.same_dir_losses += 1
            else:
                self.state.same_dir_losses = 1
                self.state.last_loss_dir = self.state.side
        else:
            self.state.same_dir_losses = 0
            self.state.last_loss_dir = None

        # Determine next state
        if reason == "TARGET_HIT":
            self.state.status = "TARGET_HIT"
            logger.info("TARGET HIT! Total realized PnL: %.2f  Stopping strategy.", self.state.realized_pnl)
        elif reason in ("HARD_STOP", "LIQUIDATED"):
            self.state.status = "ROUND_LOST"
            self.state.cooldown_until = time.time() + self.cfg["cooldown_min"] * 60
            logger.warning("Round %d LOST. Cooldown until %s. Next round will double margin.",
                           self.state.round,
                           datetime.fromtimestamp(self.state.cooldown_until, timezone.utc).isoformat())
        elif reason.startswith("REVERSAL") or reason == "TRAILING_STOP":
            # Partial loss or small win — stay in current round, re-hunt
            if unrealized_pnl > 0:
                self.state.status = "HUNTING"
                logger.info("Exited with profit %.2f but below target. Re-hunting.", unrealized_pnl)
            else:
                # Lost some margin but not all — reduce current margin and re-hunt
                margin_lost = abs(unrealized_pnl)
                self.state.current_margin = max(self.state.current_margin - margin_lost, 0)
                if self.state.current_margin < 10:  # too little to trade
                    self.state.status = "ROUND_LOST"
                    self.state.cooldown_until = time.time() + self.cfg["cooldown_min"] * 60
                else:
                    self.state.status = "HUNTING"
                    # Recalculate target
                    prior_losses = abs(min(self.state.realized_pnl, 0))
                    self.state.target_profit = (
                        self.state.cumulative_invested * self.cfg["target_roi_pct"] + prior_losses
                    )
        elif reason == "TIME_DECAY":
            self.state.status = "HUNTING"  # just re-enter
        else:
            self.state.status = "HUNTING"

        # Clear position state
        self.state.inst_id = None
        self.state.side = None
        self.state.entry_price = None
        self.state.sz = 0
        self.state.entry_time = None
        self.state.peak_unrealized = 0.0
        self.state.trailing_active = False
        self.state.save()

    async def _diagnostics_loop(self):
        """Periodic status logging."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.cfg["heartbeat_sec"])
                if self._stop_event.is_set():
                    break
                logger.info(
                    "YoloMomentum DIAG: status=%s round=%d margin=%.0f cumulative=%.0f "
                    "target=%.2f realized=%.2f fees=%.2f",
                    self.state.status, self.state.round, self.state.current_margin,
                    self.state.cumulative_invested, self.state.target_profit,
                    self.state.realized_pnl, self.state.total_fees,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Diagnostics error: %s", e)

    # ------------------------------------------------------------------
    # Order helpers
    # ------------------------------------------------------------------

    def _add_isolated_margin(self, inst_id: str, amount: float) -> bool:
        self._last_margin_adjust_error = ""
        if amount <= 0:
            return True
        try:
            _adjust_isolated_margin(inst_id, "net", amount, self.profile)
            logger.info("Added isolated margin: %s +%.2f USDT", inst_id, amount)
            return True
        except Exception as e:
            self._last_margin_adjust_error = f"Add isolated margin failed for {inst_id} amt={amount:.2f}: {e}"
            logger.error("add-isolated-margin failed [%s amt=%.2f]: %s", inst_id, amount, str(e)[:500])
            return False

    def _has_live_position(self, inst_id: Optional[str]) -> bool:
        if not inst_id:
            return False
        try:
            positions = _atk_json(["account", "positions"], self.profile)
            if not positions or not isinstance(positions, list):
                return False
            for p in positions:
                if p.get("instId") != inst_id:
                    continue
                try:
                    if abs(float(p.get("pos", 0) or 0)) > 0:
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.warning("position check after margin-add failure failed: %s", e)
        return False

    def _handle_margin_add_failure(self, detail: str, reserve_margin: float):
        inst_id = self.state.inst_id
        side = self.state.side
        sz = self.state.sz
        self.state.last_block_reason = detail
        self.state.scan_status = {
            **self.state.scan_status,
            "reserve_margin_added": 0.0,
            "reserve_margin_target": round(reserve_margin, 2),
            "stage": "margin_add_failed",
            "blocked_reason": detail,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.save()

        logger.error(
            "MARGIN GUARD: add margin failed after entry; attempting emergency close: %s %s sz=%s detail=%s",
            side, inst_id, sz, detail,
        )

        closed = self._close_position()
        still_open = self._has_live_position(inst_id)

        if closed and not still_open:
            # Conservative accounting: the trade is force-closed for safety.
            self.state.history.append({
                "round": self.state.round,
                "inst_id": inst_id,
                "side": side,
                "entry_price": self.state.entry_price,
                "sz": sz,
                "leverage": self.state.leverage,
                "pnl": 0.0,
                "pnl_pct": 0.0,
                "reason": "MARGIN_GUARD_FAIL",
                "time": datetime.now(timezone.utc).isoformat(),
            })

            self.state.status = "RECHARGE_REQUIRED"
            self.state.inst_id = None
            self.state.side = None
            self.state.entry_price = None
            self.state.sz = 0
            self.state.entry_time = None
            self.state.peak_unrealized = 0.0
            self.state.trailing_active = False
            self._last_unrealized_pnl = 0.0
            self.state.last_block_reason = (
                f"{detail} | Emergency close succeeded; strategy paused for safety."
            )
            self.state.scan_status = {
                **self.state.scan_status,
                "stage": "margin_guard_flattened",
                "flattened_at": datetime.now(timezone.utc).isoformat(),
                "blocked_reason": self.state.last_block_reason,
            }
            self.state.save()
            logger.error("MARGIN GUARD: emergency close succeeded; strategy paused.")
            return

        # If flatten failed, keep managing the live position rather than
        # pretending we're flat. The hard stop attached on entry remains active.
        self.state.last_block_reason = (
            f"{detail} | Emergency close failed; manual intervention required."
        )
        self.state.scan_status = {
            **self.state.scan_status,
            "stage": "margin_guard_close_failed",
            "blocked_reason": self.state.last_block_reason,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state.save()
        logger.critical(
            "MARGIN GUARD: emergency close failed for %s; keeping strategy IN_POSITION for continued protection.",
            inst_id,
        )

    def _set_leverage(self, inst_id: str, lever: int) -> bool:
        self._last_set_leverage_error = ""
        margin_mode = str(self.cfg.get("margin_mode") or "isolated")
        cmd = [
            "okx", "--profile", self.profile,
            "swap", "leverage",
            "--instId", inst_id, "--lever", str(lever), "--mgnMode", margin_mode,
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                detail = _extract_okx_error_detail(r.stdout, r.stderr)
                self._last_set_leverage_error = f"Set leverage failed for {inst_id} at {lever}x: {detail}"
                logger.warning("set-leverage failed [%s @ %dx]: %s", inst_id, lever, detail[:400])
                return False
            logger.info("Leverage set: %s → %dx", inst_id, lever)
            return True
        except Exception as e:
            self._last_set_leverage_error = f"Set leverage exception for {inst_id} at {lever}x: {e}"
            logger.error("set-leverage exception: %s", e)
            return False

    def _place_order(self, inst_id: str, side: str, sz: int, sl_trigger_px: Optional[float] = None) -> bool:
        self._last_order_error = ""
        margin_mode = str(self.cfg.get("margin_mode") or "isolated")
        cmd = [
            "okx", "--profile", self.profile, "--json",
            "swap", "place",
            "--instId", inst_id, "--side", side, "--ordType", "market",
            "--sz", str(sz), "--posSide", "net", "--tdMode", margin_mode,
        ]
        if sl_trigger_px and sl_trigger_px > 0:
            cmd.extend([
                "--slTriggerPx", f"{sl_trigger_px:.12f}",
                "--slOrdPx=-1",
            ])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                detail = _extract_okx_error_detail(r.stdout, r.stderr)
                self._last_order_error = f"Order rejected for {inst_id} side={side} sz={sz}: {detail}"
                logger.error("place_order failed [%s side=%s sz=%s]: %s", inst_id, side, sz, detail[:500])
                return False
            logger.info("Order placed: %s %s sz=%d → %s", side, inst_id, sz, _clean_okx_cli_text(r.stdout)[:200])
            return True
        except Exception as e:
            self._last_order_error = f"Order exception for {inst_id} side={side} sz={sz}: {e}"
            logger.error("place_order exception: %s", e)
            return False

    def _close_position(self) -> bool:
        inst_id = self.state.inst_id
        if not inst_id:
            return True
        margin_mode = str(self.cfg.get("margin_mode") or "isolated")
        cmd = [
            "okx", "--profile", self.profile, "--json",
            "swap", "close",
            "--instId", inst_id, "--mgnMode", margin_mode, "--posSide", "net",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                logger.info("Position closed: %s → %s", inst_id, r.stdout[:200])
                return True
            # Fallback: market order opposite side
            logger.warning("close failed, flattening with market order")
            return self._flatten(inst_id)
        except Exception as e:
            logger.error("close exception: %s", e)
            return self._flatten(inst_id)

    def _flatten(self, inst_id: str) -> bool:
        """Flatten by querying actual position and placing opposite order."""
        try:
            positions = _atk_json(["account", "positions"], self.profile)
            if not positions or not isinstance(positions, list):
                return True  # no position to flatten

            for p in positions:
                if p.get("instId") == inst_id:
                    pos_val = float(p.get("pos", 0))
                    if pos_val == 0:
                        return True
                    side = "sell" if pos_val > 0 else "buy"
                    sz = abs(int(pos_val))
                    if sz > 0:
                        return self._place_order(inst_id, side, sz)
            return True  # no matching position
        except Exception as e:
            logger.error("flatten exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # Dashboard / summary
    # ------------------------------------------------------------------

    def get_snapshot(self) -> Dict:
        """Return portfolio snapshot for dashboard."""
        capital = self.state.cumulative_invested
        unrealized = self._last_unrealized_pnl
        total_pnl = self.state.realized_pnl + unrealized - self.state.total_fees
        nav = capital + total_pnl

        positions = {}
        if self.state.inst_id and self.state.entry_price and self.state.sz > 0:
            ct_val = _get_contract_value(self.state.inst_id, self.profile)
            notional = self.state.sz * ct_val * self.state.entry_price
            positions[self.state.inst_id] = {
                "qty": self.state.sz * ct_val * (1 if self.state.side == "long" else -1),
                "entry": self.state.entry_price,
                "notional": round(notional, 2),
                "upnl": round(unrealized, 2),
                "side": self.state.side,
                "leverage": self.state.leverage,
            }

        return {
            "portfolio_id": "yolo_momentum",
            "strategy_id": "yolo_momentum",
            "nav": round(nav, 2),
            "capital": round(capital, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / capital * 100, 4) if capital > 0 else 0.0,
            "n_positions": 1 if self.state.inst_id else 0,
            "positions": positions,
            "strategy": "yolo_momentum",
            "round": self.state.round,
            "status": self.state.status,
            "target_profit": round(self.state.target_profit, 2),
            "current_margin": round(self.state.current_margin, 2),
            "total_fees": round(self.state.total_fees, 4),
            "history": self.state.history,
            "scan_status": self.state.scan_status,
            "last_block_reason": self.state.last_block_reason,
        }

    def _write_summary(self):
        """Write summary.json for dashboard."""
        portfolio = self.get_snapshot()
        summary = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "engine_status": "running",
            "pid": os.getpid(),
            "strategy": "yolo_momentum",
            "portfolios": {"yolo_momentum": portfolio},
            "total_nav": portfolio["nav"],
            "total_capital": portfolio["capital"],
            "total_pnl": portfolio["pnl"],
            "total_pnl_pct": portfolio["pnl_pct"],
        }
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LOGS_DIR / "summary.json.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2)
            os.replace(tmp, LOGS_DIR / "summary.json")
        except Exception as e:
            logger.warning("Failed to write summary.json: %s", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: Optional[Dict] = None, foreground: bool = True) -> Optional[YoloMomentumStrategy]:
    """Start YOLO Momentum. Called by main.py _run_custom_strategy."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    strategy = YoloMomentumStrategy(config)
    strategy.start()

    if not foreground:
        return strategy

    stop_flag = threading.Event()

    def _on_signal(sig, frame):
        logger.info("YoloMomentum: signal %d received — shutting down", sig)
        stop_flag.set()

    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    logger.info("YoloMomentum running in foreground (round=%d). Ctrl+C to stop.",
                strategy.state.round)
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

    parser = argparse.ArgumentParser(description="YOLO Momentum strategy runner")
    parser.add_argument("--profile", default="live", choices=["demo", "live"])
    parser.add_argument("--round", type=int, default=None, help="Force start at specific round")
    args = parser.parse_args()

    cfg = {"profile": args.profile}
    if args.round:
        # Reset state for specific round
        state = YoloState()
        for i in range(1, args.round):
            state.cumulative_invested += DEFAULT_CONFIG["round_margins"][i - 1]
        state.save()

    run(config=cfg)
