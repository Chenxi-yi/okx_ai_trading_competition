"""
competition/backtester.py
=========================
Runs backtests for named competition strategies with intensive logging.

Each backtest writes results to:
  logs/competition/<strategy_id>/backtest_latest.json    ← always latest
  logs/competition/<strategy_id>/backtest_<YYYYMMDD>.json ← archived copy

Usage:
  bt = CompetitionBacktester()
  results = bt.run("elite_flow", start="2025-01-01", end="2026-03-31")
  bt.print_results(results)

  all_results = bt.run_all(start="2025-01-01", end="2026-03-31")
  CompetitionBacktester.print_comparison(all_results)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR
from competition.registry import CompetitionRegistry

logger = logging.getLogger(__name__)

LOGS_DIR = BASE_DIR / "logs"
COMP_LOGS_DIR = LOGS_DIR / "competition"

_DEFAULT_START = "2025-01-01"
_DEFAULT_END   = "2026-03-31"


class CompetitionBacktester:
    """
    Runs BacktestRunner for each named competition strategy with full
    profile + risk overrides, then saves results with intensive logging.
    """

    def __init__(self, registry: Optional[CompetitionRegistry] = None):
        self.registry = registry or CompetitionRegistry()

    # ------------------------------------------------------------------
    # Single strategy
    # ------------------------------------------------------------------

    def run(
        self,
        strategy_id: str,
        start: str = _DEFAULT_START,
        end: str = _DEFAULT_END,
        use_cache: bool = True,
        verbose: bool = True,
    ) -> Dict[str, Any]:
        """
        Run a full backtest for one named competition strategy.

        Returns the results dict from BacktestRunner with added strategy metadata.
        Also saves results to logs/competition/<id>/.
        """
        cfg = self.registry.get(strategy_id)
        profile_name   = cfg["base_profile"]
        symbols        = cfg.get("symbols", [])
        capital        = self.registry.current_capital(strategy_id)
        profile_ovr    = cfg.get("profile_overrides", {})
        risk_ovr       = cfg.get("risk_overrides", {})

        if verbose:
            print(f"\n{'='*60}")
            print(f"  BACKTEST: {cfg['name']}  [{strategy_id}]")
            print(f"  Period : {start} → {end}")
            print(f"  Profile: {profile_name}  Capital: ${capital}")
            print(f"{'='*60}")

        # Fetch price data
        timeframe = "1h" if profile_name == "hourly" else "1d"
        if verbose:
            print(f"  Fetching {len(symbols)} symbols ({timeframe}) …")

        t0 = time.monotonic()
        price_data = self._fetch_data(symbols, start, end, timeframe, use_cache)

        if not price_data:
            logger.error("No price data returned for strategy %s", strategy_id)
            return {"error": "no_data", "strategy_id": strategy_id}

        if verbose:
            print(f"  Data ready: {len(price_data)} symbols in {time.monotonic()-t0:.1f}s")
            print("  Running backtest …")

        # Build and run BacktestRunner with overrides
        from backtest.runner import BacktestRunner

        runner = BacktestRunner(
            profile_name=profile_name,
            mode="futures",
            initial_capital=capital,
            profile_overrides=profile_ovr,
            risk_overrides=risk_ovr,
        )

        t1 = time.monotonic()
        results = runner.run(price_data, start=start, end=end)
        elapsed = time.monotonic() - t1

        # Attach strategy metadata
        results["strategy_id"]   = strategy_id
        results["strategy_name"] = cfg["name"]
        results["backtest_start"] = start
        results["backtest_end"]   = end
        results["capital"]        = capital
        results["elapsed_sec"]    = round(elapsed, 2)
        results["run_at"]         = datetime.now(timezone.utc).isoformat()

        if verbose:
            print(f"  Done in {elapsed:.1f}s")
            self.print_results(results)

        self._save_results(strategy_id, results)
        return results

    # ------------------------------------------------------------------
    # All strategies
    # ------------------------------------------------------------------

    def run_all(
        self,
        start: str = _DEFAULT_START,
        end: str = _DEFAULT_END,
        use_cache: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """Run backtests for every strategy and return {id: results}."""
        all_results: Dict[str, Dict[str, Any]] = {}
        strategies = self.registry.list_all()
        print(f"\nRunning backtests for {len(strategies)} strategies …\n")

        for s in strategies:
            sid = s["id"]
            try:
                all_results[sid] = self.run(sid, start=start, end=end, use_cache=use_cache, verbose=False)
                m = all_results[sid].get("metrics", {})
                ret = m.get("total_return_pct", 0)
                sharpe = m.get("sharpe_ratio", 0)
                print(f"  ✓ {sid:20s}  return={ret:+.1f}%  sharpe={sharpe:.2f}")
            except Exception as e:
                logger.error("Backtest failed for %s: %s", sid, e)
                all_results[sid] = {"error": str(e), "strategy_id": sid}
                print(f"  ✗ {sid:20s}  ERROR: {e}")

        print()
        self.print_comparison(all_results)
        return all_results

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    @staticmethod
    def print_results(results: Dict[str, Any]) -> None:
        """Print a single strategy's backtest results."""
        if "error" in results:
            print(f"  ERROR: {results['error']}")
            return

        m = results.get("metrics", {})
        trades_df = results.get("trades")
        n_trades = len(trades_df) if trades_df is not None and hasattr(trades_df, "__len__") else 0

        print(f"\n  Strategy : {results.get('strategy_name', results.get('strategy_id', '?'))}")
        print(f"  Period   : {results.get('backtest_start')} → {results.get('backtest_end')}")
        print(f"  Capital  : ${results.get('capital', 0):.0f}")
        print()
        print(f"  {'Total Return':<22} {m.get('total_return_pct', 0):>+8.2f}%")
        print(f"  {'CAGR':<22} {m.get('cagr_pct', 0):>+8.2f}%")
        print(f"  {'Sharpe Ratio':<22} {m.get('sharpe_ratio', 0):>8.3f}")
        print(f"  {'Sortino Ratio':<22} {m.get('sortino_ratio', 0):>8.3f}")
        print(f"  {'Calmar Ratio':<22} {m.get('calmar_ratio', 0):>8.3f}")
        print(f"  {'Max Drawdown':<22} {m.get('max_drawdown_pct', 0):>+8.2f}%")
        print(f"  {'Volatility (ann)':<22} {m.get('annualized_vol_pct', 0):>8.2f}%")
        print(f"  {'Win Rate':<22} {m.get('win_rate_pct', 0):>8.1f}%")
        print(f"  {'Profit Factor':<22} {m.get('profit_factor', 0):>8.2f}")
        print(f"  {'Total Trades':<22} {n_trades:>8d}")
        print(f"  {'Total Fees':<22} ${results.get('total_fees', 0):>7.2f}")
        print(f"  {'Funding Earned':<22} ${results.get('total_funding', 0):>+7.2f}")

        monthly = results.get("monthly_pnl")
        if monthly is not None and not monthly.empty:
            print("\n  Monthly Returns (%):\n")
            print(monthly.to_string(float_format=lambda x: f"{x:+.1f}"))
        print()

    @staticmethod
    def print_comparison(results: Dict[str, Dict[str, Any]]) -> None:
        """Print a side-by-side comparison table of all strategies."""
        if not results:
            print("No results to compare.")
            return

        # Collect rows
        rows: List[Dict[str, Any]] = []
        for sid, r in results.items():
            if "error" in r:
                rows.append({"id": sid, "error": r["error"]})
                continue
            m = r.get("metrics", {})
            trades_df = r.get("trades")
            n_trades = len(trades_df) if trades_df is not None and hasattr(trades_df, "__len__") else 0
            rows.append({
                "id":       sid,
                "name":     r.get("strategy_name", sid)[:20],
                "profile":  r.get("profile", "?"),
                "return":   m.get("total_return_pct", 0),
                "cagr":     m.get("cagr_pct", 0),
                "sharpe":   m.get("sharpe_ratio", 0),
                "sortino":  m.get("sortino_ratio", 0),
                "max_dd":   m.get("max_drawdown_pct", 0),
                "calmar":   m.get("calmar_ratio", 0),
                "win_pct":  m.get("win_rate_pct", 0),
                "trades":   n_trades,
                "fees":     r.get("total_fees", 0),
                "funding":  r.get("total_funding", 0),
                "capital":  r.get("capital", 0),
            })

        # Sort by Sharpe descending
        rows.sort(key=lambda x: x.get("sharpe", -999), reverse=True)

        w = 22
        sep = "─" * (w + 67)
        header = (
            f"{'Strategy':<{w}}  {'Return':>8}  {'CAGR':>7}  {'Sharpe':>7}  "
            f"{'MaxDD':>7}  {'Calmar':>7}  {'Win%':>6}  {'Trades':>6}  {'Fees':>6}"
        )

        print()
        print("=" * (w + 69))
        print("  COMPETITION BACKTEST COMPARISON")
        print("=" * (w + 69))
        print(f"  {header}")
        print(f"  {sep}")
        for row in rows:
            if "error" in row:
                print(f"  {row['id']:<{w}}  ERROR: {row['error']}")
                continue
            ret_str    = f"{row['return']:>+7.1f}%"
            cagr_str   = f"{row['cagr']:>+6.1f}%"
            sharpe_str = f"{row['sharpe']:>7.3f}"
            dd_str     = f"{row['max_dd']:>+7.1f}%"
            calmar_str = f"{row['calmar']:>7.2f}"
            win_str    = f"{row['win_pct']:>5.1f}%"
            trades_str = f"{row['trades']:>6d}"
            fees_str   = f"${row['fees']:>5.1f}"
            name_str   = f"{row['name']:<{w}}"
            print(f"  {name_str}  {ret_str}  {cagr_str}  {sharpe_str}  {dd_str}  {calmar_str}  {win_str}  {trades_str}  {fees_str}")
        print(f"  {sep}")
        print()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_results(self, strategy_id: str, results: Dict[str, Any]) -> None:
        """Save backtest results to logs/competition/<id>/."""
        out_dir = COMP_LOGS_DIR / strategy_id
        out_dir.mkdir(parents=True, exist_ok=True)

        # Serialise (nav_series and trades are DataFrames/Series)
        payload = self._serialise(results)

        # Always-latest file
        latest = out_dir / "backtest_latest.json"
        with open(latest, "w") as f:
            json.dump(payload, f, indent=2, default=str)

        # Dated archive
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        archive = out_dir / f"backtest_{date_str}.json"
        with open(archive, "w") as f:
            json.dump(payload, f, indent=2, default=str)

        logger.info("Backtest results saved → %s", latest)

    @staticmethod
    def _serialise(results: Dict[str, Any]) -> Dict[str, Any]:
        """Convert DataFrames/Series in results to JSON-serialisable form."""
        import pandas as pd
        out = {}
        for k, v in results.items():
            if isinstance(v, pd.DataFrame):
                out[k] = v.to_dict(orient="records") if not v.empty else []
            elif isinstance(v, pd.Series):
                out[k] = {str(i): float(val) for i, val in v.items() if not pd.isna(val)}
            else:
                out[k] = v
        return out

    @staticmethod
    def load_latest(strategy_id: str) -> Optional[Dict[str, Any]]:
        """Load the latest saved backtest results for a strategy."""
        path = COMP_LOGS_DIR / strategy_id / "backtest_latest.json"
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_data(
        symbols: List[str],
        start: str,
        end: str,
        timeframe: str,
        use_cache: bool,
    ) -> Dict[str, Any]:
        from data.fetcher import fetch_universe
        return fetch_universe(
            symbols,
            start=start,
            end=end,
            mode="futures",
            timeframe=timeframe,
            use_cache=use_cache,
        )
