"""C-Auto H24 regression strategy.

This is the personal-system port of the work-computer C-Auto idea: learn a
24-bar forward-return model from point-in-time feature panels, rank the latest
universe cross-sectionally, and emit Signal objects only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import exp
from typing import Any, Mapping

import numpy as np
import pandas as pd

from contracts import Signal, StrategyContext, StrategySpec
from features import build_feature_panel, build_label_panel

try:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
except Exception:  # pragma: no cover - optional research dependency fallback
    Ridge = None
    StandardScaler = None
    make_pipeline = None


DEFAULT_FEATURE_COLUMNS = (
    "ret_1",
    "ret_3",
    "ret_6",
    "ret_12",
    "ret_24",
    "mom_z_6",
    "mom_z_12",
    "mom_z_24",
    "rv_12",
    "rv_24",
    "vol_z_24",
    "funding_rate",
    "funding_mean_24",
    "funding_z_24",
    "range_pct",
    "range_mean_24",
    "trend_eff_24",
    "atr_14_pct",
    "downside_rv_24",
)


@dataclass
class CAutoH24RegressionStrategy:
    strategy_id: str = "core_c_auto_h24_regression_v1"
    params: Mapping[str, Any] = field(default_factory=dict)

    spec: StrategySpec = field(
        default_factory=lambda: StrategySpec(
            strategy_id="core_c_auto_h24_regression_v1",
            hypothesis=(
                "A rolling 24h forward-return model can identify a small set of "
                "high-conviction crypto perpetuals whose expected move is large "
                "enough to clear fees, slippage, and adverse path risk."
            ),
            book="core",
            timeframe="1h",
            holding_period="12h-36h",
            symbols_or_universe="Liquid OKX USDT perpetual universe",
            required_data=("1h OHLCV", "funding_rate"),
            required_features=DEFAULT_FEATURE_COLUMNS,
            allowed_regimes=("trend", "neutral", "risk_on"),
            entry_logic="Train rolling H24 regression, rank latest predictions, long top tail and short bottom tail.",
            exit_logic="Portfolio/risk layer owns target, stop, time stop, and de-risking.",
            position_sizing="Emit EV/probability metadata; portfolio arbiter sizes by risk budget.",
            risk_budget="Core book, default max 30% NAV before portfolio-level caps.",
            expected_failure_modes=(
                "Feature drift after market regime breaks",
                "Crowded liquidation cascades not represented by 1h bars",
                "Small-sample overfit on newly listed symbols",
                "Prediction edge smaller than fees and slippage",
            ),
            backtest_window="At least 12 months plus 14-day chunk analysis before promotion.",
            paper_requirement="Minimum 14 live market days, positive expectancy, no uncapped drawdown.",
            live_enable_default=False,
            owner_notes="First-class strategy-office version of c_auto.",
        )
    )

    def generate(self, context: StrategyContext) -> list[Signal]:
        cfg = self._config(context.config)
        price_data = self._extract_price_data(context)
        if len(price_data) < int(cfg["min_symbols"]):
            return []

        features = build_feature_panel(
            price_data,
            return_windows=cfg["return_windows"],
            rolling_windows=cfg["rolling_windows"],
        )
        labels = build_label_panel(price_data, horizons=(int(cfg["label_horizon_bars"]),))
        if features.empty or labels.empty:
            return []

        label_col = f"fwd_ret_net_long_{int(cfg['label_horizon_bars'])}"
        panel = features.join(labels[[label_col]], how="inner")
        feature_cols = [col for col in cfg["feature_columns"] if col in panel.columns]
        if not feature_cols or label_col not in panel.columns:
            return []

        latest_ts = features.index.get_level_values("timestamp").max()
        train = panel.loc[panel.index.get_level_values("timestamp") < latest_ts]
        train = train.dropna(subset=[label_col])
        latest = features.loc[features.index.get_level_values("timestamp") == latest_ts]
        latest = latest.dropna(how="all", subset=feature_cols)

        train_window = int(cfg["train_window_bars"]) * max(1, len(price_data))
        if train_window > 0:
            train = train.tail(train_window)
        if len(train) < int(cfg["min_train_rows"]) or latest.empty:
            return []

        predictions = self._predict(train, latest, feature_cols, label_col, float(cfg["ridge_alpha"]))
        if predictions.empty:
            return []

        predictions = predictions.dropna().sort_values("prediction")
        if predictions.empty:
            return []

        now = self._timestamp(context.market.timestamp)
        return self._signals_from_predictions(predictions, price_data, now, cfg)

    def _config(self, runtime_config: Mapping[str, Any]) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "label_horizon_bars": 24,
            "return_windows": (1, 3, 6, 12, 24, 72),
            "rolling_windows": (12, 24, 72),
            "train_window_bars": 2520,
            "min_train_rows": 400,
            "min_symbols": 2,
            "max_signals": 6,
            "long_quantile": 0.80,
            "short_quantile": 0.20,
            "min_abs_prediction": 0.0015,
            "target_pct": 0.03,
            "stop_pct": 0.015,
            "horizon_sec": 24 * 60 * 60,
            "ridge_alpha": 10.0,
            "feature_columns": DEFAULT_FEATURE_COLUMNS,
        }
        cfg.update(dict(self.params))
        strategy_cfg = runtime_config.get(self.strategy_id, {}) if runtime_config else {}
        if isinstance(strategy_cfg, Mapping):
            cfg.update(dict(strategy_cfg))
        cfg["return_windows"] = tuple(int(x) for x in cfg["return_windows"])
        cfg["rolling_windows"] = tuple(int(x) for x in cfg["rolling_windows"])
        cfg["feature_columns"] = tuple(str(x) for x in cfg["feature_columns"])
        return cfg

    def _extract_price_data(self, context: StrategyContext) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for symbol in context.market.universe:
            raw = context.market.ohlcv.get(symbol)
            if raw is None:
                continue
            if isinstance(raw, pd.DataFrame):
                df = raw.copy()
            else:
                df = pd.DataFrame(raw)
            if df.empty or "close" not in df.columns:
                continue
            if "funding_rate" not in df.columns and symbol in context.market.funding:
                df["funding_rate"] = float(context.market.funding[symbol])
            out[symbol] = df
        return out

    def _predict(
        self,
        train: pd.DataFrame,
        latest: pd.DataFrame,
        feature_cols: list[str],
        label_col: str,
        ridge_alpha: float,
    ) -> pd.DataFrame:
        x_train = train[feature_cols].apply(pd.to_numeric, errors="coerce")
        y_train = pd.to_numeric(train[label_col], errors="coerce")
        keep = y_train.notna()
        x_train = x_train.loc[keep]
        y_train = y_train.loc[keep]
        medians = x_train.median(numeric_only=True).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        x_train = x_train.replace([np.inf, -np.inf], np.nan).fillna(medians)
        x_latest = latest[feature_cols].apply(pd.to_numeric, errors="coerce")
        x_latest = x_latest.replace([np.inf, -np.inf], np.nan).fillna(medians).fillna(0.0)

        if x_train.empty or x_latest.empty:
            return pd.DataFrame()

        if Ridge is not None and StandardScaler is not None and make_pipeline is not None:
            model = make_pipeline(StandardScaler(), Ridge(alpha=ridge_alpha))
            model.fit(x_train.to_numpy(dtype=float), y_train.to_numpy(dtype=float))
            pred = model.predict(x_latest.to_numpy(dtype=float))
        else:
            corr = x_train.corrwith(y_train).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            score = (x_latest - x_train.mean()).divide(x_train.std().replace(0, np.nan)).fillna(0.0)
            pred = score.mul(corr, axis=1).sum(axis=1).to_numpy(dtype=float)
            pred = pred / max(1.0, float(len(feature_cols))) * max(float(y_train.std() or 0.0), 0.001)

        out = pd.DataFrame(index=x_latest.index)
        out["prediction"] = pred
        return out

    def _signals_from_predictions(
        self,
        predictions: pd.DataFrame,
        price_data: Mapping[str, pd.DataFrame],
        timestamp: datetime,
        cfg: Mapping[str, Any],
    ) -> list[Signal]:
        ranked = predictions.copy()
        ranked["rank_pct"] = ranked["prediction"].rank(pct=True, method="average")
        selected = ranked[
            (ranked["rank_pct"] >= float(cfg["long_quantile"]))
            | (ranked["rank_pct"] <= float(cfg["short_quantile"]))
        ]
        selected = selected[selected["prediction"].abs() >= float(cfg["min_abs_prediction"])]
        selected = selected.reindex(selected["prediction"].abs().sort_values(ascending=False).index)
        selected = selected.head(int(cfg["max_signals"]))

        signals: list[Signal] = []
        for (_, symbol), row in selected.iterrows():
            entry = self._latest_close(price_data.get(symbol))
            if entry is None or entry <= 0:
                continue
            pred = float(row["prediction"])
            side = "long" if pred > 0 else "short"
            target_pct = max(float(cfg["target_pct"]), abs(pred))
            stop_pct = float(cfg["stop_pct"])
            target = entry * (1.0 + target_pct) if side == "long" else entry * (1.0 - target_pct)
            stop = entry * (1.0 - stop_pct) if side == "long" else entry * (1.0 + stop_pct)
            confidence = self._confidence(pred, selected["prediction"].abs())
            signals.append(
                Signal(
                    strategy_id=self.strategy_id,
                    symbol=str(symbol),
                    side=side,
                    timestamp=timestamp,
                    entry=float(entry),
                    target=float(target),
                    stop=float(stop),
                    horizon_sec=int(cfg["horizon_sec"]),
                    p_target=0.50 + 0.25 * confidence,
                    adverse_pct_estimate=stop_pct,
                    confidence=confidence,
                    metadata={
                        "model": "ridge_h24" if Ridge is not None else "fallback_linear_score",
                        "prediction": pred,
                        "rank_pct": float(row["rank_pct"]),
                        "label_horizon_bars": int(cfg["label_horizon_bars"]),
                    },
                )
            )
        return signals

    @staticmethod
    def _latest_close(df: pd.DataFrame | None) -> float | None:
        if df is None or df.empty or "close" not in df.columns:
            return None
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        return None if close.empty else float(close.iloc[-1])

    @staticmethod
    def _confidence(prediction: float, absolute_predictions: pd.Series) -> float:
        scale = float(absolute_predictions.median()) if not absolute_predictions.empty else 0.0
        scale = max(scale, 0.001)
        raw = abs(prediction) / scale
        return float(max(0.05, min(1.0, 2.0 / (1.0 + exp(-raw)) - 1.0)))

    @staticmethod
    def _timestamp(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
