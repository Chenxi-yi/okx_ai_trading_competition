"""Feature and label metadata registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    family: str
    source_columns: List[str]
    lookback_bars: int
    frequency: str
    live_available: bool
    expected_nan_warmup_bars: int
    description: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class LabelSpec:
    name: str
    family: str
    horizon_bars: int
    source_columns: List[str]
    description: str

    def to_dict(self) -> Dict:
        return asdict(self)


def build_default_feature_registry(
    return_windows: Iterable[int] = (1, 3, 6, 12, 24, 72),
    rolling_windows: Iterable[int] = (12, 24, 72),
    frequency: str = "1h",
) -> Dict[str, FeatureSpec]:
    specs: Dict[str, FeatureSpec] = {}

    _add(
        specs,
        "close",
        "price",
        ["close"],
        0,
        frequency,
        True,
        0,
        "Close price at the current bar.",
    )
    _add(specs, "log_price", "price", ["close"], 0, frequency, True, 0, "Natural log of close price.")
    _add(specs, "range_pct", "price_range", ["high", "low", "close"], 0, frequency, True, 0, "High-low range divided by close.")
    _add(specs, "close_to_high", "price_range", ["close", "high"], 0, frequency, True, 0, "Close location versus current high.")
    _add(specs, "close_to_low", "price_range", ["close", "low"], 0, frequency, True, 0, "Close location versus current low.")
    _add(specs, "volume_usd", "volume", ["volume", "close"], 0, frequency, True, 0, "Quote-volume proxy from volume times close.")
    _add(specs, "funding_rate", "derivatives", ["funding_rate"], 0, frequency, True, 0, "Latest available funding rate aligned to bar.")

    for window in return_windows:
        _add(
            specs,
            f"ret_{window}",
            "momentum",
            ["close"],
            window,
            frequency,
            True,
            window,
            f"Backward close-to-close return over {window} bars.",
        )
        warmup = max(window * 4, 20)
        _add(
            specs,
            f"mom_z_{window}",
            "momentum",
            ["close"],
            window + warmup,
            frequency,
            True,
            max(window, warmup // 3),
            f"Rolling z-score of {window}-bar backward return.",
        )

    for window in rolling_windows:
        min_periods = max(3, window // 3)
        _add(specs, f"rv_{window}", "volatility", ["close"], window, frequency, True, min_periods, f"Rolling realized volatility over {window} bars.")
        _add(specs, f"vol_z_{window}", "volume", ["volume"], window, frequency, True, min_periods, f"Rolling z-score of volume over {window} bars.")
        _add(specs, f"funding_mean_{window}", "derivatives", ["funding_rate"], window, frequency, True, min_periods, f"Rolling mean funding over {window} bars.")
        _add(specs, f"funding_z_{window}", "derivatives", ["funding_rate"], window, frequency, True, min_periods, f"Rolling z-score of funding over {window} bars.")
        _add(specs, f"range_mean_{window}", "price_range", ["high", "low", "close"], window, frequency, True, min_periods, f"Rolling mean range percentage over {window} bars.")
        _add(specs, f"trend_eff_{window}", "momentum", ["close"], window, frequency, True, min_periods, f"Directional return divided by absolute-return path over {window} bars.")

    _add(specs, "atr_14_pct", "volatility", ["high", "low", "close"], 14, frequency, True, 4, "ATR divided by close.")
    _add(specs, "ret_1_abs", "volatility", ["close"], 1, frequency, True, 1, "Absolute one-bar return.")
    _add(specs, "downside_rv_24", "volatility", ["close"], 24, frequency, True, 8, "Rolling downside realized volatility over 24 bars.")
    return specs


def build_microstructure_feature_registry(frequency: str = "event") -> Dict[str, FeatureSpec]:
    specs: Dict[str, FeatureSpec] = {}
    for name, family, source, description in [
        ("ob_spread_bps", "orderbook", ["orderbook"], "Top-of-book spread in basis points."),
        ("ob_depth_imbalance", "orderbook", ["orderbook"], "Top-N bid minus ask notional divided by total depth."),
        ("ob_bid_notional_top", "orderbook", ["orderbook"], "Top-N bid-side notional depth."),
        ("ob_ask_notional_top", "orderbook", ["orderbook"], "Top-N ask-side notional depth."),
        ("ob_mid", "orderbook", ["orderbook"], "Order book mid price."),
        ("ob_microprice_proxy", "orderbook", ["orderbook"], "Top-level size-weighted microprice proxy."),
        ("tf_trade_count", "trade_flow", ["trades"], "Trade count in the aggregation bucket."),
        ("tf_trade_notional", "trade_flow", ["trades"], "Total trade notional in the aggregation bucket."),
        ("tf_buy_notional", "trade_flow", ["trades"], "Buyer-initiated notional proxy in the aggregation bucket."),
        ("tf_sell_notional", "trade_flow", ["trades"], "Seller-initiated notional proxy in the aggregation bucket."),
        ("tf_avg_trade_notional", "trade_flow", ["trades"], "Average trade notional in the aggregation bucket."),
        ("tf_trade_imbalance", "trade_flow", ["trades"], "Buy minus sell notional divided by total notional."),
        ("oi_open_interest_value", "derivatives", ["open_interest"], "Open interest value from OKX derivatives statistics."),
        ("oi_open_interest_value_chg_1", "derivatives", ["open_interest"], "One-step percentage change in open interest value."),
        ("oi_quote_volume", "derivatives", ["open_interest"], "Quote volume from OKX open-interest statistics."),
        ("oi_quote_volume_chg_1", "derivatives", ["open_interest"], "One-step percentage change in quote volume."),
        ("funding_funding_rate", "derivatives", ["funding"], "Funding rate history from OKX."),
        ("funding_funding_rate_chg_1", "derivatives", ["funding"], "One-step percentage change in funding rate."),
        ("ls_long_short_ratio", "crowding", ["long_short"], "OKX long/short account ratio."),
        ("ls_long_short_ratio_chg_1", "crowding", ["long_short"], "One-step percentage change in long/short ratio."),
    ]:
        _add(specs, name, family, source, 1, frequency, True, 1, description)
    return specs


def build_default_label_registry(
    horizons: Iterable[int] = (1, 3, 6, 12, 24),
) -> Dict[str, LabelSpec]:
    specs: Dict[str, LabelSpec] = {}
    for horizon in horizons:
        specs[f"fwd_ret_{horizon}"] = LabelSpec(
            f"fwd_ret_{horizon}",
            "forward_return",
            horizon,
            ["close"],
            f"Forward close-to-close return over {horizon} bars.",
        )
        specs[f"fwd_ret_net_long_{horizon}"] = LabelSpec(
            f"fwd_ret_net_long_{horizon}",
            "cost_adjusted_return",
            horizon,
            ["close"],
            f"Forward long return over {horizon} bars after fee/slippage/funding cost assumptions.",
        )
        specs[f"fwd_ret_net_short_{horizon}"] = LabelSpec(
            f"fwd_ret_net_short_{horizon}",
            "cost_adjusted_return",
            horizon,
            ["close"],
            f"Forward short return over {horizon} bars after fee/slippage/funding cost assumptions.",
        )
        specs[f"fwd_abs_edge_after_cost_{horizon}"] = LabelSpec(
            f"fwd_abs_edge_after_cost_{horizon}",
            "cost_adjusted_edge",
            horizon,
            ["close"],
            f"Absolute directional move over {horizon} bars after fee/slippage/funding cost assumptions.",
        )
        specs[f"fwd_dir_{horizon}"] = LabelSpec(
            f"fwd_dir_{horizon}",
            "direction",
            horizon,
            ["close"],
            f"Sign of forward return over {horizon} bars.",
        )
        specs[f"mfe_{horizon}"] = LabelSpec(
            f"mfe_{horizon}",
            "path_extreme",
            horizon,
            ["high", "close"],
            f"Maximum favorable excursion over next {horizon} bars.",
        )
        specs[f"mae_{horizon}"] = LabelSpec(
            f"mae_{horizon}",
            "path_extreme",
            horizon,
            ["low", "close"],
            f"Maximum adverse excursion over next {horizon} bars.",
        )
        specs[f"hit_1pct_before_down_1pct_{horizon}"] = LabelSpec(
            f"hit_1pct_before_down_1pct_{horizon}",
            "barrier",
            horizon,
            ["high", "low", "close"],
            f"Whether +1% barrier is hit before -1% barrier within {horizon} bars.",
        )
    return specs


def registry_to_dict(registry: Dict[str, FeatureSpec]) -> Dict[str, Dict]:
    return {name: spec.to_dict() for name, spec in sorted(registry.items())}


def label_registry_to_dict(registry: Dict[str, LabelSpec]) -> Dict[str, Dict]:
    return {name: spec.to_dict() for name, spec in sorted(registry.items())}


def _add(
    specs: Dict[str, FeatureSpec],
    name: str,
    family: str,
    source_columns: List[str],
    lookback_bars: int,
    frequency: str,
    live_available: bool,
    expected_nan_warmup_bars: int,
    description: str,
) -> None:
    specs[name] = FeatureSpec(
        name=name,
        family=family,
        source_columns=source_columns,
        lookback_bars=int(lookback_bars),
        frequency=frequency,
        live_available=bool(live_available),
        expected_nan_warmup_bars=int(expected_nan_warmup_bars),
        description=description,
    )
