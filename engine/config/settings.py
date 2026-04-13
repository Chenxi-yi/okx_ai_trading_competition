"""
Central configuration for the phase-1 quantitative crypto trading framework.
"""

import os
from pathlib import Path
from typing import Dict, Iterable, Tuple


# Resolved dynamically — works regardless of where this folder is placed
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data" / "cache"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_CONFIG_DIR = Path(__file__).resolve().parent
_ENV_FILES = (
    _CONFIG_DIR / ".env",
    _CONFIG_DIR / ".env.local",
    _CONFIG_DIR / ".env.runtime",
)
_OKX_TOML = Path.home() / ".okx" / "config.toml"


def _load_env_files(paths: Iterable[Path]) -> None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return
    try:
        from dotenv import load_dotenv
        for path in existing:
            load_dotenv(path, override=True)
        return
    except ImportError:
        pass
    for path in existing:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


def _read_okx_toml() -> Dict[str, str]:
    """
    Read OKX credentials from ~/.okx/config.toml.
    Supports both tomllib (Python 3.11+) and a simple fallback parser.
    Returns dict with keys: api_key, secret_key, passphrase.
    """
    if not _OKX_TOML.exists():
        return {}
    try:
        # Try standard library tomllib (Python 3.11+)
        try:
            import tomllib
            with open(_OKX_TOML, "rb") as f:
                data = tomllib.load(f)
        except ImportError:
            # Fallback: simple line-by-line parser for our known config format
            data = _parse_toml_simple(_OKX_TOML)

        default = data.get("default_profile", "live")
        profile = data.get("profiles", {}).get(default, {})
        return {
            "api_key":    profile.get("api_key", ""),
            "secret_key": profile.get("secret_key", ""),
            "passphrase": profile.get("passphrase", ""),
        }
    except Exception:
        return {}


def _parse_toml_simple(path: Path) -> Dict:
    """Minimal TOML parser — handles our ~/.okx/config.toml structure only."""
    data: Dict = {}
    current_section: list = []

    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Section header: [profiles.demo] or [profiles.live]
            if line.startswith("[") and line.endswith("]"):
                parts = line[1:-1].split(".")
                current_section = parts
                # Ensure nested dicts exist
                node = data
                for part in parts:
                    node = node.setdefault(part, {})
                continue
            # Key = value
            if "=" in line:
                key, _, raw_val = line.partition("=")
                key = key.strip()
                val = raw_val.strip().strip('"').strip("'")
                # Handle triple-quoted values
                if val.startswith("'''") and val.endswith("'''"):
                    val = val[3:-3]
                # Boolean
                if val.lower() == "true":
                    val = True  # type: ignore
                elif val.lower() == "false":
                    val = False  # type: ignore
                if current_section:
                    node = data
                    for part in current_section:
                        node = node.setdefault(part, {})
                    node[key] = val
                else:
                    data[key] = val
    return data


def _resolve_secret(
    env_names: Tuple[str, ...],
    toml_key: str,
    _toml_cache: Dict[str, str] = {},
) -> Tuple[str, str]:
    # 1. Environment variable (highest priority)
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"env:{name}"

    # 2. ~/.okx/config.toml
    if not _toml_cache:
        _toml_cache.update(_read_okx_toml())
    value = _toml_cache.get(toml_key, "").strip()
    if value:
        return value, f"toml:{toml_key}"

    return "", "missing"


_load_env_files(_ENV_FILES)

OKX_API_KEY, OKX_API_KEY_SOURCE = _resolve_secret(
    ("OKX_API_KEY",),
    "api_key",
)
OKX_API_SECRET, OKX_API_SECRET_SOURCE = _resolve_secret(
    ("OKX_API_SECRET", "OKX_SECRET_KEY"),
    "secret_key",
)
OKX_PASSPHRASE, OKX_PASSPHRASE_SOURCE = _resolve_secret(
    ("OKX_PASSPHRASE",),
    "passphrase",
)
if OKX_API_KEY:
    os.environ.setdefault("OKX_API_KEY", OKX_API_KEY)
if OKX_API_SECRET:
    os.environ.setdefault("OKX_API_SECRET", OKX_API_SECRET)
    os.environ.setdefault("OKX_SECRET_KEY", OKX_API_SECRET)
if OKX_PASSPHRASE:
    os.environ.setdefault("OKX_PASSPHRASE", OKX_PASSPHRASE)
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"


TRADING_MODE = "futures"

LEVERAGE_LIMITS = {
    "spot": 1.0,
    "futures": 1.5,
    "margin": 1.25,
}


# Static fallback symbols (used for backtest or if dynamic fetch fails)
SYMBOLS_SPOT = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]

SYMBOLS_FUTURES = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]

SYMBOLS_MARGIN = SYMBOLS_FUTURES

# Dynamic universe settings (for live trading)
UNIVERSE_MIN_VOLUME_USD = 5_000_000.0   # 24h volume floor
UNIVERSE_MAX_SYMBOLS = 40               # cap to avoid API rate limits

# Cached dynamic symbols (populated lazily by get_symbols)
_dynamic_futures_symbols = None


def get_symbols(mode: str = TRADING_MODE, dynamic: bool = False):
    """
    Return the symbol list for a given mode.

    When dynamic=True and mode='futures', fetches all tradable USDT perps
    from OKX filtered by volume. Falls back to static list on failure.
    """
    global _dynamic_futures_symbols

    if dynamic and mode == "futures":
        if _dynamic_futures_symbols is None:
            try:
                from data.fetcher import fetch_tradable_futures_symbols
                _dynamic_futures_symbols = fetch_tradable_futures_symbols(
                    min_daily_volume_usd=UNIVERSE_MIN_VOLUME_USD,
                    max_symbols=UNIVERSE_MAX_SYMBOLS,
                )
            except Exception as e:
                import logging
                _log = logging.getLogger(__name__)
                _log.warning("Dynamic universe fetch failed: %s. Trying testnet discovery.", e)
                try:
                    _dynamic_futures_symbols = fetch_tradable_futures_symbols(
                        min_daily_volume_usd=0,  # testnet has no real volume
                        max_symbols=UNIVERSE_MAX_SYMBOLS,
                        sandbox=True,
                    )
                except Exception as e2:
                    _log.warning("Testnet discovery also failed: %s. Using static list.", e2)
                    _dynamic_futures_symbols = SYMBOLS_FUTURES
        return _dynamic_futures_symbols

    mapping = {
        "spot": SYMBOLS_SPOT,
        "futures": SYMBOLS_FUTURES,
        "margin": SYMBOLS_MARGIN,
    }
    if mode not in mapping:
        raise ValueError(f"Unknown trading mode '{mode}'.")
    return mapping[mode]


BACKTEST_START = "2020-01-01"
BACKTEST_END = "2024-12-31"
TIMEFRAME = "1d"
FUNDING_TIMEFRAME = "1d"
EXECUTION_PRICE_FIELD = "open"
MARK_PRICE_FIELD = "close"


INITIAL_CAPITAL = 5_000.0
MAX_POSITION_PCT = 0.25
MAX_GROSS_LEVERAGE = 1.5
MAX_NET_EXPOSURE = 0.50
VOL_TARGET = 0.15

TRANSACTION_COSTS = {
    "spot": 0.0010,
    "futures": 0.0004,
    "margin": 0.0010,
}

TAKER_FEE = 0.0004
MAKER_FEE = 0.0002
SLIPPAGE_FACTOR = 0.10


PORTFOLIO_WEIGHTS = {
    "trend": 0.50,
    "cross_sectional": 0.35,
    "carry": 0.15,
}

PORTFOLIO = {
    "target_vol": VOL_TARGET,
    "max_gross_leverage": MAX_GROSS_LEVERAGE,
    "max_net_exposure": MAX_NET_EXPOSURE,
    "max_position_pct": MAX_POSITION_PCT,
    "min_rebalance_notional_usd": 10.0,
}

BACKTEST_GUARDS = {
    "min_history_bars": 120,
    "min_adv_usd": 1_000_000.0,
    "max_ffill_days": 1,
    "liquidity_lookback": 30,
    "apply_tradeability_filter": True,
}


TREND_MOMENTUM = {
    "preferred_mode": "futures",
    "fast_windows": [20, 30],
    "slow_windows": [100, 150, 200],
    "breakout_windows": [20, 50, 100],
    "momentum_windows": [20, 60, 120],
    "band": 0.005,
    "vol_lookback": 30,
    "target_sleeve_vol": 0.10,
    "rebalance_freq": "1D",
}

CROSS_SECTIONAL_MOMENTUM = {
    "preferred_mode": "futures",
    "lookbacks": [7, 30, 90],
    "vol_lookback": 20,
    "funding_lookback": 7,
    "top_n": 2,
    "bottom_n": 2,
    "target_sleeve_vol": 0.10,
    "rebalance_freq": "1D",
    "require_min_universe": 4,
}

FUNDING_CARRY = {
    "preferred_mode": "futures",
    "funding_lookbacks": [3, 7, 14],
    "trend_veto_lookback": 20,
    "action_threshold": 0.0001,
    "max_abs_funding": 0.005,
    "target_sleeve_vol": 0.06,
    "rebalance_freq": "1D",
}


ATR_MULTIPLIER = 2.0

# Risk manager v1 settings
MARGIN_WARN_THRESHOLD = 0.80
STOP_LOSS_PCT = 0.05

# Risk manager v2 settings
DRAWDOWN_CIRCUIT_BREAKER_1 = 0.10
DRAWDOWN_CIRCUIT_BREAKER_2 = 0.20
HIGH_VOL_PERCENTILE = 80
LOW_VOL_PERCENTILE = 20
CORRELATION_THRESHOLD = 0.85

# Intraday risk monitor thresholds
INTRADAY_DD_FROM_SESSION_OPEN = 0.05   # 5% drop from session-open equity
INTRADAY_DD_FROM_PEAK = 0.15           # 15% drop from all-time peak

# Smart execution settings
SPREAD_THRESHOLD_PCT = 0.001           # 0.1% — above this, use limit order
LIMIT_ORDER_TIMEOUT_SEC = 30           # seconds to wait for limit fill
TWAP_DEPTH_THRESHOLD_PCT = 0.05        # order > 5% of book depth → TWAP
TWAP_NUM_SLICES = 4
TWAP_INTERVAL_SEC = 15

# Drift-check settings
DRIFT_THRESHOLD_RELATIVE = 0.25        # 25% relative drift triggers partial rebalance
DRIFT_DEBOUNCE_HOURS = 2               # skip if last rebalance was < 2h ago

LOG_LEVEL = "INFO"
