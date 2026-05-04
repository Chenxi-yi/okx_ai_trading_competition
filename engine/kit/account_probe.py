"""Private account probes through OKX Agent Trade Kit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import KitClient
from .schemas import KitCommand


@dataclass(frozen=True)
class AccountProbe:
    client: KitClient
    profile: str = "demo"

    def balance(self, ccy: str | None = None) -> Any:
        args = (ccy,) if ccy else ()
        return self.client.run(KitCommand("account", "balance", args, profile=self.profile)).require_ok().data

    def positions(self, inst_type: str = "SWAP", inst_id: str | None = None) -> Any:
        args = ["--instType", inst_type]
        if inst_id:
            args.extend(["--instId", inst_id])
        return self.client.run(KitCommand("account", "positions", tuple(args), profile=self.profile)).require_ok().data

    def bills(self, inst_type: str = "SWAP", limit: int = 100) -> Any:
        return self.client.run(
            KitCommand("account", "bills", ("--instType", inst_type, "--limit", str(limit)), profile=self.profile)
        ).require_ok().data

    def fees(self, inst_type: str = "SWAP", inst_id: str | None = None) -> Any:
        args = ["--instType", inst_type]
        if inst_id:
            args.extend(["--instId", inst_id])
        return self.client.run(KitCommand("account", "fees", tuple(args), profile=self.profile)).require_ok().data

    def audit(self, limit: int = 100) -> Any:
        return self.client.run(KitCommand("account", "audit", ("--limit", str(limit)), profile=self.profile)).require_ok().data
