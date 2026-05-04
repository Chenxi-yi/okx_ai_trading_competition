"""Low-token, local Kit supervisor for market/account I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Iterable

from .account_probe import AccountProbe
from .market_probe import MarketProbe


@dataclass(frozen=True)
class KitSupervisorConfig:
    symbols: tuple[str, ...] = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    interval_sec: float = 30.0
    max_cycles: int | None = None
    status_path: Path | str = Path("engine/logs/kit/supervisor_status.json")
    stop_path: Path | str | None = Path("engine/control/kit_supervisor.stop")
    include_orderbook: bool = False
    include_account: bool = True


@dataclass
class KitSupervisor:
    market_probe: MarketProbe
    account_probe: AccountProbe | None = None
    config: KitSupervisorConfig = field(default_factory=KitSupervisorConfig)
    cycles: int = 0

    def run(self) -> dict[str, object]:
        while True:
            if self._stop_requested():
                status = self._status("stopped", "stop file present")
                self._write_status(status)
                return status
            if self.config.max_cycles is not None and self.cycles >= self.config.max_cycles:
                status = self._status("completed", "max_cycles reached")
                self._write_status(status)
                return status
            status = self.run_once()
            self._write_status(status)
            if self.config.max_cycles is not None and self.cycles >= self.config.max_cycles:
                completed = {**status, "scheduler_status": "completed", "reason": "max_cycles reached"}
                self._write_status(completed)
                return completed
            time.sleep(max(0.0, self.config.interval_sec))

    def run_once(self) -> dict[str, object]:
        started = datetime.now(timezone.utc).isoformat()
        errors: list[str] = []
        tickers = {}
        funding = {}
        open_interest = {}
        orderbooks = {}
        account = {}
        for inst_id in self.config.symbols:
            try:
                tickers[inst_id] = self.market_probe.ticker(inst_id)
            except Exception as exc:
                errors.append(f"ticker {inst_id}: {exc}")
            try:
                funding[inst_id] = self.market_probe.funding_rate(inst_id)
            except Exception as exc:
                errors.append(f"funding {inst_id}: {exc}")
            try:
                open_interest[inst_id] = self.market_probe.open_interest(inst_id)
            except Exception as exc:
                errors.append(f"open_interest {inst_id}: {exc}")
            if self.config.include_orderbook:
                try:
                    orderbooks[inst_id] = self.market_probe.orderbook(inst_id)
                except Exception as exc:
                    errors.append(f"orderbook {inst_id}: {exc}")
        if self.config.include_account and self.account_probe:
            for name, fn in (("balance", self.account_probe.balance), ("positions", self.account_probe.positions)):
                try:
                    account[name] = fn()
                except Exception as exc:
                    errors.append(f"account {name}: {exc}")
        self.cycles += 1
        return {
            "status": "ok" if not errors else "warn",
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "cycle_started_at": started,
            "cycles": self.cycles,
            "symbols": list(self.config.symbols),
            "errors": errors,
            "tickers": tickers,
            "funding": funding,
            "open_interest": open_interest,
            "orderbooks": orderbooks,
            "account": account,
        }

    def _status(self, status: str, reason: str) -> dict[str, object]:
        return {
            "status": status,
            "reason": reason,
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "cycles": self.cycles,
            "symbols": list(self.config.symbols),
        }

    def _stop_requested(self) -> bool:
        return bool(self.config.stop_path and Path(self.config.stop_path).exists())

    def _write_status(self, payload: dict[str, object]) -> None:
        path = Path(self.config.status_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def inst_ids_from_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
    inst_ids = []
    for symbol in symbols:
        if symbol.endswith("-USDT-SWAP"):
            inst_ids.append(symbol)
        else:
            inst_ids.append(symbol.replace("/", "-").replace(":USDT", "") + "-SWAP")
    return tuple(inst_ids)
