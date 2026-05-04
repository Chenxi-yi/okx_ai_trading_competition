"""Professional Signal-pipeline backtest loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from accounting import AccountingConfig, PortfolioAccounting
from arbitration import PortfolioArbiter
from contracts import InstrumentSpec, MarketState, Strategy
from execution.router import BacktestExecutionRouter, ExecutionConfig
from position import PositionManager
from risk.account import AccountRiskArbiter
from runtime.pipeline import PipelineConfig, TradingPipeline


@dataclass(frozen=True)
class ProBacktestConfig:
    initial_nav_usdt: float = 10_000.0
    timeframe: str = "1h"
    rebalance_every_bars: int = 1
    execution_delay_bars: int = 1
    journal_dir: Path | str = Path("engine/logs/backtest_journal")
    result_dir: Path | str | None = Path("engine/results/pro_backtest")
    result_id: str | None = None
    execution: ExecutionConfig = field(default_factory=lambda: ExecutionConfig(profile="backtest"))


@dataclass(frozen=True)
class ProBacktestResult:
    nav: pd.Series
    fills: pd.DataFrame
    attribution: pd.DataFrame
    summary: Mapping[str, float | int | str]
    artifacts_dir: str | None = None


class ProBacktestEngine:
    """Runs Strategy Protocol strategies through the canonical runtime pipeline."""

    def __init__(
        self,
        strategies: Sequence[Strategy],
        price_data: Mapping[str, pd.DataFrame],
        instruments: Mapping[str, InstrumentSpec] | None = None,
        config: ProBacktestConfig | None = None,
        position_manager: PositionManager | None = None,
    ):
        self.strategies = list(strategies)
        self.price_data = {symbol: _normalize_ohlcv(df) for symbol, df in price_data.items()}
        self.config = config or ProBacktestConfig()
        self.instruments = dict(instruments or _default_instruments(self.price_data))
        self.accounting = PortfolioAccounting(AccountingConfig(initial_nav_usdt=self.config.initial_nav_usdt))
        self.pipeline = TradingPipeline(
            strategies=self.strategies,
            arbiter=PortfolioArbiter(),
            account_risk=AccountRiskArbiter(),
            execution_router=BacktestExecutionRouter(self.config.execution),
            position_manager=position_manager,
            config=PipelineConfig(
                environment="backtest",
                journal_dir=self.config.journal_dir,
                execution=self.config.execution,
            ),
        )

    def run(self, start: str | None = None, end: str | None = None) -> ProBacktestResult:
        timestamps = _common_timestamps(self.price_data, start=start, end=end)
        nav_rows: list[tuple[pd.Timestamp, float]] = []
        fill_rows: list[dict] = []

        for bar_idx, ts in enumerate(timestamps):
            frame = _slice_to_timestamp(self.price_data, ts)
            marks = _mark_prices(frame)
            funding = _funding_rates(frame)
            self.accounting.apply_funding(ts.to_pydatetime(), marks, funding)
            portfolio = self.accounting.state(ts.to_pydatetime(), marks)
            if bar_idx % max(1, self.config.rebalance_every_bars) != 0:
                nav_rows.append((ts, portfolio.nav_usdt))
                continue

            decision_idx = bar_idx - max(0, self.config.execution_delay_bars)
            if decision_idx < 0:
                nav_rows.append((ts, portfolio.nav_usdt))
                continue
            decision_ts = timestamps[decision_idx]
            decision_frame = _slice_to_timestamp(self.price_data, decision_ts)
            decision_marks = _mark_prices(decision_frame)
            market = MarketState(
                timestamp=decision_ts.to_pydatetime(),
                universe=tuple(decision_frame),
                ohlcv=decision_frame,
                instruments=self.instruments,
            )
            result = self.pipeline.run_once(market, portfolio, decision_marks, execution_prices=marks)
            orders_by_decision = {order.decision_id: order for order in result.orders}
            for fill in result.fills:
                order = orders_by_decision.get(fill.decision_id)
                if order is None:
                    continue
                state = self.accounting.apply_fill(fill, order, ts.to_pydatetime())
                fill_rows.append(
                    {
                        "timestamp": ts,
                        "decision_id": fill.decision_id,
                        "inst_id": fill.inst_id,
                        "side": fill.side,
                        "fill_price": fill.fill_price,
                        "fill_size": fill.fill_size,
                        "fee": fill.fee,
                        "strategy_id": order.metadata.get("strategy_id", "unknown"),
                        "position_action": order.metadata.get("position_action"),
                        "nav_usdt": state.nav_usdt,
                    }
                )
            nav_rows.append((ts, self.accounting.state(ts.to_pydatetime(), marks).nav_usdt))

        nav = pd.Series([row[1] for row in nav_rows], index=pd.DatetimeIndex([row[0] for row in nav_rows]), name="nav")
        fills = pd.DataFrame(fill_rows)
        attribution = _attribution(fills, self.accounting, _mark_prices(_slice_to_timestamp(self.price_data, timestamps[-1])) if len(timestamps) else {})
        summary = _summary(nav, fills, self.accounting)
        artifacts_dir = self._write_artifacts(nav, fills, attribution, summary, start, end)
        return ProBacktestResult(nav=nav, fills=fills, attribution=attribution, summary=summary, artifacts_dir=artifacts_dir)

    def _write_artifacts(
        self,
        nav: pd.Series,
        fills: pd.DataFrame,
        attribution: pd.DataFrame,
        summary: Mapping[str, float | int | str],
        start: str | None,
        end: str | None,
    ) -> str | None:
        if self.config.result_dir is None:
            return None
        result_id = self.config.result_id or f"pro_backtest_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        out = Path(self.config.result_dir) / result_id
        out.mkdir(parents=True, exist_ok=True)
        nav.to_csv(out / "nav.csv", header=True)
        fills.to_csv(out / "fills.csv", index=False)
        attribution.to_csv(out / "attribution.csv")
        (out / "summary.json").write_text(json.dumps(dict(summary), indent=2, sort_keys=True))
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result_id": result_id,
            "strategies": [strategy.strategy_id for strategy in self.strategies],
            "symbols": sorted(self.price_data),
            "start": start,
            "end": end,
            "timeframe": self.config.timeframe,
            "initial_nav_usdt": self.config.initial_nav_usdt,
            "rebalance_every_bars": self.config.rebalance_every_bars,
            "execution_delay_bars": self.config.execution_delay_bars,
            "artifacts": {
                "nav": "nav.csv",
                "fills": "fills.csv",
                "attribution": "attribution.csv",
                "summary": "summary.json",
            },
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return str(out)


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.index = pd.to_datetime(out.index, utc=True)
    out = out.sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in out.columns:
            raise ValueError(f"Missing OHLCV column: {col}")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "funding_rate" not in out.columns:
        out["funding_rate"] = 0.0
    return out


def _common_timestamps(price_data: Mapping[str, pd.DataFrame], start: str | None, end: str | None) -> pd.DatetimeIndex:
    values: pd.DatetimeIndex | None = None
    for df in price_data.values():
        idx = pd.DatetimeIndex(df.index)
        values = idx if values is None else values.union(idx)
    if values is None:
        return pd.DatetimeIndex([], tz="UTC")
    values = values.sort_values()
    if start:
        values = values[values >= pd.Timestamp(start, tz="UTC")]
    if end:
        values = values[values <= pd.Timestamp(end, tz="UTC")]
    return values


def _slice_to_timestamp(price_data: Mapping[str, pd.DataFrame], timestamp: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {
        symbol: df.loc[:timestamp].copy()
        for symbol, df in price_data.items()
        if not df.loc[:timestamp].empty
    }


def _mark_prices(price_data: Mapping[str, pd.DataFrame]) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol, df in price_data.items():
        close = pd.to_numeric(df["close"], errors="coerce").dropna()
        if close.empty:
            continue
        out[symbol] = float(close.iloc[-1])
    return out


def _default_instruments(price_data: Mapping[str, pd.DataFrame]) -> dict[str, InstrumentSpec]:
    return {
        symbol: InstrumentSpec(
            inst_id=symbol,
            symbol=symbol,
            ct_val=1.0,
            lot_sz=1.0,
            min_sz=1.0,
            source="backtest_default",
            timestamp=datetime.now(timezone.utc),
        )
        for symbol in price_data
    }


def _summary(nav: pd.Series, fills: pd.DataFrame, accounting: PortfolioAccounting) -> dict[str, float | int | str]:
    if nav.empty:
        return {"status": "empty", "bars": 0, "fills": 0}
    total_return = nav.iloc[-1] / nav.iloc[0] - 1.0 if nav.iloc[0] else 0.0
    drawdown = nav / nav.cummax() - 1.0
    return {
        "status": "ok",
        "bars": int(len(nav)),
        "fills": int(len(fills)),
        "start_nav": float(nav.iloc[0]),
        "end_nav": float(nav.iloc[-1]),
        "total_return_pct": float(total_return),
        "max_drawdown_pct": float(drawdown.min()),
        "realized_pnl_usdt": float(accounting.realized_pnl_usdt),
        "unrealized_pnl_usdt": float(accounting.unrealized_pnl_usdt),
        "total_fees_usdt": float(accounting.total_fees_usdt),
        "total_funding_usdt": float(accounting.total_funding_usdt),
    }


def _attribution(fills: pd.DataFrame, accounting: PortfolioAccounting, mark_prices: Mapping[str, float]) -> pd.DataFrame:
    if fills.empty:
        return pd.DataFrame(columns=["fills", "fees_usdt", "funding_usdt", "gross_turnover_contracts", "realized_pnl_usdt", "unrealized_pnl_usdt"])
    grouped = fills.groupby("strategy_id", dropna=False)
    out = grouped.agg(
        fills=("decision_id", "count"),
        fees_usdt=("fee", "sum"),
        gross_turnover_contracts=("fill_size", lambda x: float(pd.to_numeric(x, errors="coerce").abs().sum())),
    )
    strategies = set(out.index) | set(accounting.strategy_realized_pnl) | set(accounting.strategy_funding) | set(accounting.strategy_unrealized(mark_prices))
    out = out.reindex(sorted(strategies)).fillna(0.0)
    out["fees_usdt"] = [float(accounting.strategy_fees.get(str(idx), out.loc[idx, "fees_usdt"])) for idx in out.index]
    out["funding_usdt"] = [float(accounting.strategy_funding.get(str(idx), 0.0)) for idx in out.index]
    out["realized_pnl_usdt"] = [float(accounting.strategy_realized_pnl.get(str(idx), 0.0)) for idx in out.index]
    unrealized = accounting.strategy_unrealized(mark_prices)
    out["unrealized_pnl_usdt"] = [float(unrealized.get(str(idx), 0.0)) for idx in out.index]
    out["net_pnl_usdt"] = out["realized_pnl_usdt"] + out["unrealized_pnl_usdt"] - out["fees_usdt"] - out["funding_usdt"]
    for position in accounting.positions.values():
        strategy_id = str(position.metadata.get("strategy_id") or "unknown")
        if strategy_id not in out.index:
            out.loc[strategy_id, ["fills", "fees_usdt", "funding_usdt", "gross_turnover_contracts", "realized_pnl_usdt", "unrealized_pnl_usdt", "net_pnl_usdt"]] = 0.0
        out.loc[strategy_id, "open_positions"] = float(out.get("open_positions", pd.Series(dtype=float)).get(strategy_id, 0.0) + 1.0)
    out = out.fillna(0.0)
    return out.sort_index()


def _funding_rates(price_data: Mapping[str, pd.DataFrame]) -> dict[str, float]:
    out: dict[str, float] = {}
    for symbol, df in price_data.items():
        if "funding_rate" not in df.columns:
            out[symbol] = 0.0
            continue
        rates = pd.to_numeric(df["funding_rate"], errors="coerce").dropna()
        out[symbol] = 0.0 if rates.empty else float(rates.iloc[-1])
    return out
