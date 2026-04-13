"""
session.py
==========
Multi-session management for competition strategies.

Each session is a unique run of a strategy with its own ID, state, logs,
and capital tracking.  The SessionDaemon manages all active sessions and
writes a combined summary.json for the dashboard.

CLI (via main.py):
    python3 main.py session create -s elite_flow [--id flow_001]
    python3 main.py session list
    python3 main.py session stop <session_id>
    python3 main.py session stop-all
    python3 main.py session daemon --foreground
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import BASE_DIR

LOGS_DIR = BASE_DIR / "logs"
SESSIONS_DIR = LOGS_DIR / "sessions"
REGISTRY_PATH = LOGS_DIR / "sessions.json"

logger = logging.getLogger(__name__)

# Strategy short prefixes for auto-generated IDs
_STRATEGY_PREFIX = {
    "elite_flow": "flow",
}


# ---------------------------------------------------------------------------
# SessionRegistry — CRUD on logs/sessions.json
# ---------------------------------------------------------------------------

class SessionRegistry:
    """Read/write session definitions in logs/sessions.json."""

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path or REGISTRY_PATH)

    def _read(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {"sessions": {}}
        with open(self._path) as f:
            return json.load(f)

    def _write(self, data: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def create(
        self,
        strategy_id: str,
        session_id: Optional[str] = None,
        capital: float = 300.0,
        profile: str = "demo",
        config_overrides: Optional[Dict] = None,
    ) -> str:
        data = self._read()
        sessions = data.setdefault("sessions", {})

        if session_id is None:
            session_id = self._generate_id(strategy_id, sessions)

        if session_id in sessions:
            raise ValueError(f"Session {session_id!r} already exists")

        sessions[session_id] = {
            "session_id": session_id,
            "strategy_id": strategy_id,
            "capital": capital,
            "profile": profile,
            "config_overrides": config_overrides or {},
            "status": "running",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)
        return session_id

    def stop(self, session_id: str) -> None:
        data = self._read()
        sessions = data.get("sessions", {})
        if session_id not in sessions:
            raise KeyError(f"Session {session_id!r} not found")
        sessions[session_id]["status"] = "stopped"
        sessions[session_id]["stopped_at"] = datetime.now(timezone.utc).isoformat()
        self._write(data)

    def stop_all(self) -> List[str]:
        data = self._read()
        stopped = []
        for sid, s in data.get("sessions", {}).items():
            if s.get("status") == "running":
                s["status"] = "stopped"
                s["stopped_at"] = datetime.now(timezone.utc).isoformat()
                stopped.append(sid)
        self._write(data)
        return stopped

    def list_all(self) -> List[Dict[str, Any]]:
        data = self._read()
        return list(data.get("sessions", {}).values())

    def get_running(self) -> List[Dict[str, Any]]:
        return [s for s in self.list_all() if s.get("status") == "running"]

    def get(self, session_id: str) -> Dict[str, Any]:
        data = self._read()
        sessions = data.get("sessions", {})
        if session_id not in sessions:
            raise KeyError(f"Session {session_id!r} not found")
        return sessions[session_id]

    def _generate_id(self, strategy_id: str, existing: Dict) -> str:
        prefix = _STRATEGY_PREFIX.get(strategy_id, strategy_id[:4])
        for i in range(1, 1000):
            candidate = f"{prefix}_{i:03d}"
            if candidate not in existing:
                return candidate
        raise RuntimeError("Too many sessions")


# ---------------------------------------------------------------------------
# SessionDaemon — manages strategy lifecycle
# ---------------------------------------------------------------------------

class SessionDaemon:
    """
    Long-running process that:
    1. Reads sessions.json and launches all 'running' sessions
    2. Writes combined summary.json every 15s
    3. Picks up registry changes (new sessions, stopped sessions)
    4. Handles SIGTERM for graceful shutdown
    """

    def __init__(self):
        self._registry = SessionRegistry()
        self._active: Dict[str, _ManagedSession] = {}
        self._stop_event = threading.Event()
        self._registry_mtime: float = 0.0

    def run(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger.info("SessionDaemon starting")

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        self._sync_sessions()
        logger.info("SessionDaemon running — %d active session(s)", len(self._active))

        while not self._stop_event.is_set():
            self._write_aggregated_summary()
            time.sleep(15)
            self._sync_sessions()

        logger.info("SessionDaemon shutting down — stopping %d session(s)", len(self._active))
        self._stop_all()
        self._write_aggregated_summary()
        logger.info("SessionDaemon stopped")

    def _on_signal(self, sig, _frame):
        logger.info("SessionDaemon: signal %d received", sig)
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def _sync_sessions(self) -> None:
        """Compare registry against active sessions, start/stop as needed."""
        try:
            running = {s["session_id"]: s for s in self._registry.get_running()}
        except Exception as e:
            logger.warning("SessionDaemon: failed to read registry: %s", e)
            return

        # Stop sessions that are no longer in registry or marked stopped
        for sid in list(self._active.keys()):
            if sid not in running:
                logger.info("SessionDaemon: stopping session %s", sid)
                self._stop_session(sid)

        # Start sessions that aren't active yet
        for sid, sdef in running.items():
            if sid not in self._active:
                self._launch_session(sdef)

    def _launch_session(self, sdef: Dict[str, Any]) -> None:
        sid = sdef["session_id"]
        strategy_id = sdef["strategy_id"]

        # Create session log directory
        session_dir = SESSIONS_DIR / sid
        session_dir.mkdir(parents=True, exist_ok=True)

        # Build config — merge competition_strategies.json definition with overrides
        try:
            from competition.registry import CompetitionRegistry
            registry = CompetitionRegistry()
            strategy_def = registry.get(strategy_id)
        except Exception:
            strategy_def = {}

        # For elite_* strategies, use their dedicated config block
        base_config = strategy_def.get(f"{strategy_id}_config", {})

        # For bar-based strategies, pass through profile/risk overrides and symbols
        config = {
            **base_config,
            "strategy_id": strategy_id,
            "base_profile": strategy_def.get("base_profile", "daily"),
            "profile_overrides": strategy_def.get("profile_overrides", {}),
            "risk_overrides": strategy_def.get("risk_overrides", {}),
            "symbols": strategy_def.get("symbols", []),
            **sdef.get("config_overrides", {}),
            "session_id": sid,
            "capital": sdef.get("capital", 300.0),
            "profile": sdef.get("profile", "demo"),
            "session_log_dir": str(session_dir),
            "session_state_file": str(session_dir / "state.json"),
        }

        # Configure per-session logging — capture both strategy module and bar_adapter logs
        log_file = session_dir / "strategy.log"
        handler = logging.FileHandler(log_file)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        strategy_logger = logging.getLogger(f"competition.strategies.{strategy_id}")
        strategy_logger.addHandler(handler)
        # Also attach to bar_adapter logger so rebalance/risk logs are captured
        bar_logger = logging.getLogger("competition.strategies.bar_adapter")
        bar_logger.addHandler(handler)

        # Import and launch
        try:
            module_path = f"competition.strategies.{strategy_id}"
            mod = importlib.import_module(module_path)
            strategy_obj = mod.run(config=config, foreground=False)

            self._active[sid] = _ManagedSession(
                session_id=sid,
                strategy_id=strategy_id,
                strategy_obj=strategy_obj,
                config=config,
                log_handler=handler,
            )
            logger.info("SessionDaemon: launched session %s (strategy=%s)", sid, strategy_id)
        except Exception as e:
            logger.error("SessionDaemon: failed to launch session %s: %s", sid, e, exc_info=True)
            strategy_logger.removeHandler(handler)

    def _stop_session(self, sid: str) -> None:
        ms = self._active.pop(sid, None)
        if ms is None:
            return
        try:
            if ms.strategy_obj and hasattr(ms.strategy_obj, "stop"):
                ms.strategy_obj.stop()
        except Exception as e:
            logger.warning("SessionDaemon: error stopping %s: %s", sid, e)

        # Remove log handlers
        strategy_logger = logging.getLogger(f"competition.strategies.{ms.strategy_id}")
        strategy_logger.removeHandler(ms.log_handler)
        bar_logger = logging.getLogger("competition.strategies.bar_adapter")
        bar_logger.removeHandler(ms.log_handler)
        logger.info("SessionDaemon: stopped session %s", sid)

    def _stop_all(self) -> None:
        for sid in list(self._active.keys()):
            self._stop_session(sid)

    # ------------------------------------------------------------------
    # Summary aggregation
    # ------------------------------------------------------------------

    def _write_aggregated_summary(self) -> None:
        """Collect snapshots from all active sessions, write combined summary.json."""
        portfolios: Dict[str, Any] = {}
        total_nav = 0.0
        total_capital = 0.0
        total_pnl = 0.0

        for sid, ms in self._active.items():
            try:
                snapshot = None
                if hasattr(ms.strategy_obj, "get_snapshot"):
                    snapshot = ms.strategy_obj.get_snapshot()
                if snapshot:
                    portfolios[sid] = snapshot
                    total_nav += snapshot.get("nav", 0)
                    total_capital += snapshot.get("capital", 0)
                    total_pnl += snapshot.get("pnl", 0)
            except Exception as e:
                logger.debug("SessionDaemon: snapshot error for %s: %s", sid, e)

        summary = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "engine_status": "running" if self._active else "idle",
            "pid": os.getpid(),
            "n_sessions": len(self._active),
            "portfolios": portfolios,
            "total_nav": round(total_nav, 2),
            "total_capital": round(total_capital, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl / total_capital * 100, 4) if total_capital > 0 else 0.0,
        }

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LOGS_DIR / "summary.json.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2)
            os.replace(tmp, LOGS_DIR / "summary.json")
        except Exception as e:
            logger.warning("SessionDaemon: failed to write summary: %s", e)

        # Append NAV snapshot to history (for dashboard chart)
        if portfolios:
            nav_entry = {
                "ts": summary["updated_at"],
            }
            for sid, snap in portfolios.items():
                nav_entry[sid] = round(snap.get("nav", 0), 2)
            nav_entry["total"] = round(total_nav, 2)
            try:
                with open(LOGS_DIR / "nav_history.jsonl", "a") as f:
                    f.write(json.dumps(nav_entry) + "\n")
            except Exception:
                pass


class _ManagedSession:
    __slots__ = ("session_id", "strategy_id", "strategy_obj", "config", "log_handler")

    def __init__(self, session_id, strategy_id, strategy_obj, config, log_handler):
        self.session_id = session_id
        self.strategy_id = strategy_id
        self.strategy_obj = strategy_obj
        self.config = config
        self.log_handler = log_handler


# ---------------------------------------------------------------------------
# CLI dispatcher — called from main.py
# ---------------------------------------------------------------------------

def cmd_session(args) -> None:
    registry = SessionRegistry()

    if args.sess_cmd == "create":
        config_overrides = {}
        if args.config:
            config_overrides = json.loads(args.config)
        capital = config_overrides.pop("capital", 300.0)
        profile = config_overrides.pop("profile", "demo")
        sid = registry.create(
            strategy_id=args.strategy,
            session_id=getattr(args, "id", None),
            capital=capital,
            profile=profile,
            config_overrides=config_overrides,
        )
        print(f"Session created: {sid}")
        print(f"  strategy: {args.strategy}")
        print(f"  capital:  {capital}")
        print(f"  profile:  {profile}")
        print(f"  status:   running")
        print()
        print("The daemon will auto-start this session.")
        print("If daemon is not running: python3 main.py session daemon --foreground")

    elif args.sess_cmd == "list":
        sessions = registry.list_all()
        if not sessions:
            print("No sessions. Create one: python3 main.py session create -s elite_flow")
            return

        # Read summary for live PnL
        summary = {}
        summary_path = LOGS_DIR / "summary.json"
        if summary_path.exists():
            try:
                with open(summary_path) as f:
                    summary = json.load(f)
            except Exception:
                pass
        portfolios = summary.get("portfolios", {})

        print(f"{'ID':<16} {'Strategy':<14} {'Capital':>8} {'PnL':>10} {'Status':<10} {'Created'}")
        print("-" * 80)
        for s in sessions:
            sid = s["session_id"]
            pnl_str = "—"
            p = portfolios.get(sid, {})
            if p:
                pnl = p.get("pnl", 0)
                pnl_str = f"${pnl:+.2f}"
            created = s.get("created_at", "")[:16]
            print(
                f"{sid:<16} {s['strategy_id']:<14} ${s.get('capital', 300):>7.0f} "
                f"{pnl_str:>10} {s.get('status', '?'):<10} {created}"
            )

    elif args.sess_cmd == "stop":
        sid = args.session_id
        registry.stop(sid)
        print(f"Session {sid} marked as stopped.")
        print("The daemon will stop it on the next cycle (~15s).")

    elif args.sess_cmd == "stop-all":
        stopped = registry.stop_all()
        if stopped:
            print(f"Stopped {len(stopped)} session(s): {', '.join(stopped)}")
        else:
            print("No running sessions to stop.")

    elif args.sess_cmd == "daemon":
        daemon = SessionDaemon()
        daemon.run()
