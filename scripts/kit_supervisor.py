#!/usr/bin/env python3
"""Run the local OKX Agent Trade Kit supervisor."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from kit import AccountProbe, KitClient, KitClientConfig, KitSupervisor, KitSupervisorConfig, MarketProbe
from kit.supervisor import inst_ids_from_symbols


def main() -> int:
    parser = argparse.ArgumentParser(description="Run low-token OKX Agent Trade Kit market/account supervisor")
    parser.add_argument("--profile", default="demo")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    parser.add_argument("--interval-sec", type=float, default=30.0)
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument("--status-path", default="engine/logs/kit/supervisor_status.json")
    parser.add_argument("--no-account", action="store_true")
    parser.add_argument("--orderbook", action="store_true")
    args = parser.parse_args()

    symbols = tuple(item.strip() for item in args.symbols.split(",") if item.strip())
    inst_ids = inst_ids_from_symbols(symbols)
    client = KitClient(KitClientConfig(default_profile=args.profile))
    market_probe = MarketProbe(client, profile=args.profile)
    account_probe = None if args.no_account else AccountProbe(client, profile=args.profile)
    supervisor = KitSupervisor(
        market_probe=market_probe,
        account_probe=account_probe,
        config=KitSupervisorConfig(
            symbols=inst_ids,
            interval_sec=args.interval_sec,
            max_cycles=args.max_cycles,
            status_path=args.status_path,
            include_orderbook=args.orderbook,
            include_account=not args.no_account,
        ),
    )
    status = supervisor.run()
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
