"""Low-frequency public market probes through OKX Agent Trade Kit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import KitClient
from .schemas import KitCommand


@dataclass(frozen=True)
class MarketProbe:
    client: KitClient
    profile: str = "demo"

    def ticker(self, inst_id: str) -> Any:
        return self.client.run(KitCommand("market", "ticker", (inst_id,), profile=self.profile)).require_ok().data

    def orderbook(self, inst_id: str, depth: int = 20) -> Any:
        return self.client.run(
            KitCommand("market", "orderbook", (inst_id, "--sz", str(depth)), profile=self.profile)
        ).require_ok().data

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 100) -> Any:
        return self.client.run(
            KitCommand("market", "candles", (inst_id, "--bar", bar, "--limit", str(limit)), profile=self.profile)
        ).require_ok().data

    def funding_rate(self, inst_id: str, history: bool = False, limit: int | None = None) -> Any:
        args = [inst_id]
        if history:
            args.append("--history")
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return self.client.run(KitCommand("market", "funding-rate", tuple(args), profile=self.profile)).require_ok().data

    def open_interest(self, inst_id: str) -> Any:
        return self.client.run(
            KitCommand("market", "open-interest", ("--instType", "SWAP", "--instId", inst_id), profile=self.profile)
        ).require_ok().data
