"""Human-readable reports from result artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def build_backtest_report(artifact_dir: Path | str, output_path: Path | str | None = None) -> str:
    root = Path(artifact_dir)
    summary = json.loads((root / "summary.json").read_text())
    manifest = json.loads((root / "manifest.json").read_text())
    attribution_path = root / "attribution.csv"
    attribution = pd.read_csv(attribution_path) if attribution_path.exists() else pd.DataFrame()
    lines = [
        f"# Backtest Report: {manifest.get('result_id', root.name)}",
        "",
        f"- Strategies: {', '.join(manifest.get('strategies', []))}",
        f"- Symbols: {', '.join(manifest.get('symbols', []))}",
        f"- Timeframe: {manifest.get('timeframe', '')}",
        f"- Bars: {summary.get('bars', 0)}",
        f"- Fills: {summary.get('fills', 0)}",
        f"- Total return: {float(summary.get('total_return_pct', 0.0)):.4%}",
        f"- Max drawdown: {float(summary.get('max_drawdown_pct', 0.0)):.4%}",
        f"- Fees: {float(summary.get('total_fees_usdt', 0.0)):.4f} USDT",
        f"- Funding: {float(summary.get('total_funding_usdt', 0.0)):.4f} USDT",
        "",
    ]
    if not attribution.empty:
        lines.extend(["## Attribution", "", attribution.to_markdown(index=False), ""])
    report = "\n".join(lines)
    if output_path:
        Path(output_path).write_text(report)
    return report
