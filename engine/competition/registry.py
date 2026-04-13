"""
competition/registry.py
=======================
Loads and validates competition strategy definitions from
config/competition_strategies.json.

Each entry is a named, versioned strategy config that can be independently
backtested, demo-run, and compared. Adding a new strategy = one JSON entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR

_CONFIG_PATH = BASE_DIR / "config" / "competition_strategies.json"


class CompetitionRegistry:
    """
    Single source of truth for all named competition strategies.

    Usage:
        registry = CompetitionRegistry()
        registry.list_all()          # all strategy dicts
        registry.get("elite_flow")   # one strategy dict
        registry.ids()               # ["elite_flow"]
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path or _CONFIG_PATH)
        self._data: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._data is not None:
            return
        if not self._path.exists():
            raise FileNotFoundError(
                f"Competition strategies config not found: {self._path}\n"
                "Create engine/config/competition_strategies.json to define strategies."
            )
        with open(self._path) as f:
            self._data = json.load(f)

    def reload(self) -> None:
        """Force a reload from disk (useful when editing config mid-session)."""
        self._data = None
        self._load()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def competition_info(self) -> Dict[str, Any]:
        self._load()
        return self._data.get("competition", {})

    def list_all(self) -> List[Dict[str, Any]]:
        """Return all strategy definitions."""
        self._load()
        return list(self._data.get("strategies", []))

    def ids(self) -> List[str]:
        return [s["id"] for s in self.list_all()]

    def get(self, strategy_id: str) -> Dict[str, Any]:
        """Return one strategy dict by ID. Raises KeyError if not found."""
        for s in self.list_all():
            if s["id"] == strategy_id:
                return dict(s)
        valid = ", ".join(self.ids())
        raise KeyError(
            f"Unknown competition strategy: {strategy_id!r}\n"
            f"Available: {valid}"
        )

    def exists(self, strategy_id: str) -> bool:
        return strategy_id in self.ids()

    # ------------------------------------------------------------------
    # Engine config adapters
    # ------------------------------------------------------------------

    def current_capital(self, strategy_id: str) -> float:
        """
        Return the current deployed capital for a strategy.
        Reads `current_capital` (which may have been topped up beyond seed).
        Falls back to `seed_capital` then competition-level seed_capital.
        """
        s = self.get(strategy_id)
        seed = s.get("seed_capital", self.competition_info.get("seed_capital", 300))
        return float(s.get("current_capital", seed))

    def seed_capital(self, strategy_id: str) -> float:
        """Return the original organiser-provided seed capital (300 USDT)."""
        s = self.get(strategy_id)
        return float(s.get("seed_capital", self.competition_info.get("seed_capital", 300)))

    def set_capital(self, strategy_id: str, new_capital: float) -> None:
        """
        Update current_capital for a strategy in the config file.
        Call this after a top-up decision to persist the new amount.
        """
        self._load()
        for s in self._data["strategies"]:
            if s["id"] == strategy_id:
                s["current_capital"] = new_capital
                break
        with open(self._path, "w") as f:
            import json as _json
            _json.dump(self._data, f, indent=2)
        self._data = None  # force reload on next access

    def to_portfolio_config(self, strategy_id: str) -> Dict[str, Any]:
        """
        Convert a competition strategy to the engine portfolio config format
        used by main.py start --config.

        Returns a single config dict (not a list).
        """
        s = self.get(strategy_id)
        return {
            "id": s["id"],
            "strategy": "combined_portfolio",
            "profile": s["base_profile"],
            "capital": self.current_capital(strategy_id),
        }

    def to_engine_config_json(self, strategy_id: str) -> str:
        """Return JSON string for --config argument to main.py start."""
        return json.dumps([self.to_portfolio_config(strategy_id)])

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def print_all(self) -> None:
        info = self.competition_info
        print("=" * 65)
        print(f"  COMPETITION STRATEGIES — OKX AI Skills ({info.get('start_date','?')} → {info.get('end_date','?')})")
        print("=" * 65)
        for s in self.list_all():
            print(f"\n  [{s['id']}]  {s['name']}")
            seed    = s.get("seed_capital", self.competition_info.get("seed_capital", 300))
            current = s.get("current_capital", seed)
            top_up  = current - seed
            cap_str = f"${current} USDT (seed ${seed}" + (f" + topped up ${top_up})" if top_up else ")")
            print(f"  Profile  : {s['base_profile']} | Capital: {cap_str}")
            weights = s.get("profile_overrides", {}).get("portfolio_weights", {})
            if weights:
                w_str = "  ".join(f"{k}={v:.0%}" for k, v in weights.items())
                print(f"  Weights  : {w_str}")
            syms = s.get("symbols", [])
            print(f"  Symbols  : {', '.join(syms)}")
            print(f"  Note     : {s.get('notes', s.get('description', ''))}")
        print()
        print("  To backtest: python3 main.py competition backtest --strategy <id>")
        print("  To demo run: python3 main.py competition demo-start --strategy <id>")
        print("=" * 65)
