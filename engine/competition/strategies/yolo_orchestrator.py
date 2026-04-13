"""
competition/strategies/yolo_orchestrator.py
============================================
YOLO Demo Test (10x) — Orchestrator for 10 staggered YOLO Momentum instances.

Manages 10 independent YOLO slots, each with $1,000 budget.
Slots are deployed staggered: one at a time (shared OKX demo account in net mode).
When a slot finishes (success = 20% ROI hit, or drained = all 4 rounds used),
the next pending slot is deployed immediately.  If no slot finishes within 1 hour,
the next pending slot is deployed anyway.

At most 1 slot is actively trading at any time to avoid merged positions
on the shared OKX demo account (net position mode).

State is written to engine/logs/yolo_orchestrator_<profile>.json every 10 seconds.

Entry point:
  run(config, foreground=True) <- called by main.py _run_custom_strategy
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as _signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Paths
LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"

def _orchestrator_state_file(profile: str) -> Path:
    suffix = "_live" if profile == "live" else ""
    return LOGS_DIR / f"yolo_orchestrator{suffix}.json"

# How many nav snapshots to keep per slot
MAX_NAV_HISTORY = 5000

DEFAULT_CONFIG: Dict = {
    "profile": "live",
    "num_slots": 10,
    "budget_per_slot": 1000,
    "total_budget": None,
    "stagger_interval_sec": 3600,       # 1 hour between staggered deploys
    "reconcile_sec": 10,                # poll / write state every 10s
    "slot_target_roi_pct": 0.20,        # 20% ROI to count as "succeeded"
    "slot_round_margins": [50, 100, 200, 400],
    # Pass-through to YoloMomentumStrategy per slot
    "yolo_config_overrides": {},
}


# ---------------------------------------------------------------------------
# Slot descriptor
# ---------------------------------------------------------------------------

@dataclass
class SlotInfo:
    """Tracking state for a single YOLO slot."""
    id: int
    status: str = "pending"                 # pending | running | succeeded | drained | recharge_required
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    round: int = 0
    cumulative_invested: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    roi_pct: float = 0.0
    nav_history: List[Dict] = field(default_factory=list)
    trades: List[Dict] = field(default_factory=list)
    current_position: Optional[Dict] = None
    scan_status: Dict = field(default_factory=dict)

    # Internal — not serialised to JSON
    _strategy: Any = field(default=None, repr=False)
    _state_file: Optional[Path] = field(default=None, repr=False)
    _resume_on_start: bool = field(default=False, repr=False)

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "round": self.round,
            "cumulative_invested": round(self.cumulative_invested, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "roi_pct": round(self.roi_pct, 4),
            "nav_history": self.nav_history[-MAX_NAV_HISTORY:],
            "trades": self.trades,
            "current_position": self.current_position,
            "scan_status": self.scan_status,
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class YoloOrchestrator:
    """
    Manages N sequential YOLO Momentum instances with staggered deployment.
    Only one slot runs at a time (shared OKX account, net position mode).
    """

    def __init__(self, config: Optional[Dict] = None):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        profile_override = os.getenv("STRATEGY_PROFILE")
        if profile_override in ("demo", "live"):
            self.cfg["profile"] = profile_override
        budget_override = os.getenv("YOLO_TOTAL_BUDGET")
        if budget_override:
            try:
                budget_value = float(budget_override)
                # In live mode we run one slot at a time, so each slot may use
                # the full account budget. Keep the account-level budget
                # separate so the dashboard does not multiply it by num_slots.
                self.cfg["total_budget"] = budget_value
                self.cfg["budget_per_slot"] = budget_value
            except ValueError:
                logger.warning("Ignoring invalid YOLO_TOTAL_BUDGET=%r", budget_override)
        self.profile = self.cfg["profile"]
        self.state_file = _orchestrator_state_file(self.profile)
        self.num_slots = self.cfg["num_slots"]
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Build slot list
        self.slots: List[SlotInfo] = []
        for i in range(1, self.num_slots + 1):
            slot = SlotInfo(id=i)
            suffix = "_live" if self.cfg["profile"] == "live" else ""
            slot._state_file = LOGS_DIR / f"yolo_slot_{i}{suffix}_state.json"
            self.slots.append(slot)

        # Deployment queue index (next slot to deploy)
        self._next_slot_idx = 0
        # Timestamp of last deployment (for stagger timer)
        self._last_deploy_time: float = 0.0

        # Try to restore state from disk
        self._load_orchestrator_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_orchestrator_state(self):
        """Restore orchestrator state from disk if available."""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            saved_slots = {s["id"]: s for s in data.get("slots", [])}
            for slot in self.slots:
                if slot.id in saved_slots:
                    sd = saved_slots[slot.id]
                    saved_status = sd.get("status", "pending")
                    slot.status = saved_status
                    slot.started_at = sd.get("started_at")
                    slot.finished_at = sd.get("finished_at")
                    slot.round = sd.get("round", 0)
                    slot.cumulative_invested = sd.get("cumulative_invested", 0.0)
                    slot.realized_pnl = sd.get("realized_pnl", 0.0)
                    slot.unrealized_pnl = sd.get("unrealized_pnl", 0.0)
                    slot.total_pnl = sd.get("total_pnl", 0.0)
                    slot.roi_pct = sd.get("roi_pct", 0.0)
                    slot.nav_history = sd.get("nav_history", [])
                    slot.trades = sd.get("trades", [])
                    slot.current_position = sd.get("current_position")
                    slot.scan_status = sd.get("scan_status", {})
                    if saved_status == "running":
                        slot._resume_on_start = True

            # Advance _next_slot_idx past already-deployed slots
            for i, slot in enumerate(self.slots):
                if slot.status == "pending":
                    self._next_slot_idx = i
                    break
            else:
                self._next_slot_idx = self.num_slots  # all deployed

            logger.info("Restored orchestrator state: %d slots loaded", len(saved_slots))
        except Exception as e:
            logger.warning("Failed to load orchestrator state: %s", e)

    def _build_summary(self) -> Dict:
        """Build the full orchestrator summary dict."""
        total_deployed = sum(1 for s in self.slots if s.status != "pending")
        total_succeeded = sum(1 for s in self.slots if s.status == "succeeded")
        total_drained = sum(1 for s in self.slots if s.status == "drained")
        total_running = sum(1 for s in self.slots if s.status == "running")
        total_pending = sum(1 for s in self.slots if s.status == "pending")
        total_recharge_required = sum(1 for s in self.slots if s.status == "recharge_required")

        overall_invested = sum(s.cumulative_invested for s in self.slots if s.status != "pending")
        overall_pnl = sum(s.total_pnl for s in self.slots if s.status != "pending")
        overall_roi = (overall_pnl / overall_invested * 100) if overall_invested > 0 else 0.0

        return {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "profile": self.profile,
            "strategy": "yolo_orchestrator",
            "total_budget": float(
                self.cfg.get("total_budget")
                or (self.cfg["budget_per_slot"] * self.num_slots)
            ),
            "total_deployed": total_deployed,
            "total_succeeded": total_succeeded,
            "total_drained": total_drained,
            "total_running": total_running,
            "total_pending": total_pending,
            "total_recharge_required": total_recharge_required,
            "overall_roi_pct": round(overall_roi, 4),
            "overall_pnl": round(overall_pnl, 2),
            "overall_invested": round(overall_invested, 2),
            "slots": [s.to_dict() for s in self.slots],
        }

    def _write_state(self):
        """Atomically write orchestrator state to disk."""
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        summary = self._build_summary()
        tmp = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(summary, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Failed to write orchestrator state: %s", e)

        # Also write a summary.json for the dashboard
        self._write_dashboard_summary(summary)

    def _write_dashboard_summary(self, orch_summary: Dict):
        """Write summary.json compatible with the main dashboard."""
        running_slot = None
        for s in self.slots:
            if s.status == "running":
                running_slot = s
                break

        total_capital = orch_summary["overall_invested"] or 1.0
        total_pnl = orch_summary["overall_pnl"]

        positions = {}
        if running_slot and running_slot.current_position:
            cp = running_slot.current_position
            positions[cp.get("inst_id", "unknown")] = {
                "side": cp.get("side", ""),
                "entry": cp.get("entry_price", 0),
                "leverage": cp.get("leverage", 0),
                "upnl": round(cp.get("unrealized_pnl", 0), 2),
            }

        portfolio = {
            "portfolio_id": "yolo_orchestrator",
            "strategy_id": "yolo_orchestrator",
            "nav": round(total_capital + total_pnl, 2),
            "capital": round(total_capital, 2),
            "pnl": round(total_pnl, 2),
            "pnl_pct": round(total_pnl / total_capital * 100, 4) if total_capital > 0 else 0.0,
            "n_positions": 1 if positions else 0,
            "positions": positions,
            "strategy": "yolo_orchestrator",
            "status": "running",
            "slots_summary": {
                "total": self.num_slots,
                "succeeded": orch_summary["total_succeeded"],
                "drained": orch_summary["total_drained"],
                "running": orch_summary["total_running"],
                "pending": orch_summary["total_pending"],
            },
        }

        dashboard = {
            "updated_at": orch_summary["updated_at"],
            "engine_status": "running",
            "pid": os.getpid(),
            "strategy": "yolo_orchestrator",
            "portfolios": {"yolo_orchestrator": portfolio},
            "total_nav": portfolio["nav"],
            "total_capital": portfolio["capital"],
            "total_pnl": portfolio["pnl"],
            "total_pnl_pct": portfolio["pnl_pct"],
        }
        tmp = LOGS_DIR / "summary.json.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(dashboard, f, indent=2)
            os.replace(tmp, LOGS_DIR / "summary.json")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Slot lifecycle
    # ------------------------------------------------------------------

    def _deploy_slot(self, slot: SlotInfo):
        """Deploy a single YOLO slot by creating a YoloMomentumStrategy instance."""
        from competition.strategies.yolo_momentum import YoloState, YoloMomentumStrategy

        logger.info("DEPLOYING slot %d", slot.id)

        resume_existing = bool(slot._resume_on_start and slot._state_file and slot._state_file.exists())

        # A resumed running slot must keep its state file so the nested
        # YoloMomentum strategy can recover the live position and round state.
        if not resume_existing and slot._state_file and slot._state_file.exists():
            slot._state_file.unlink()

        # Build per-slot config — use independent state file
        slot_cfg = {
            **DEFAULT_CONFIG.get("yolo_config_overrides", {}),
            "profile": self.profile,
            "round_margins": list(self.cfg["slot_round_margins"]),
            "target_roi_pct": self.cfg["slot_target_roi_pct"],
            "total_budget": self.cfg["budget_per_slot"],
            "reconcile_sec": 10,
        }

        strategy = YoloMomentumStrategy(config=slot_cfg, state_file=slot._state_file)

        slot._strategy = strategy
        slot._resume_on_start = False
        slot.status = "running"
        if not slot.started_at:
            slot.started_at = datetime.now(timezone.utc).isoformat()
        slot.round = strategy.state.round
        slot.cumulative_invested = strategy.state.cumulative_invested
        self._last_deploy_time = time.time()

        # Start the strategy (background thread with its own async loop)
        strategy.start()
        logger.info("Slot %d DEPLOYED: round=%d margin=%.0f",
                     slot.id, strategy.state.round, strategy.state.current_margin)

    def _poll_slot(self, slot: SlotInfo):
        """
        Poll a running slot's strategy state to update tracking fields.
        Detects success (TARGET_HIT), drain (DONE), and recharge-required pauses.
        """
        strategy = slot._strategy
        if strategy is None:
            return

        state = strategy.state
        now_iso = datetime.now(timezone.utc).isoformat()

        # Update tracking from strategy state
        slot.round = state.round
        slot.cumulative_invested = state.cumulative_invested
        slot.realized_pnl = state.realized_pnl - state.total_fees
        slot.unrealized_pnl = strategy._last_unrealized_pnl
        slot.total_pnl = slot.realized_pnl + slot.unrealized_pnl
        slot.roi_pct = (slot.total_pnl / slot.cumulative_invested * 100) if slot.cumulative_invested > 0 else 0.0
        slot.scan_status = {
            "scan_status": state.scan_status,
            "last_block_reason": getattr(state, "last_block_reason", ""),
            "strategy_status": state.status,
        }

        # Current position
        if state.inst_id and state.entry_price and state.sz > 0:
            slot.current_position = {
                "inst_id": state.inst_id,
                "side": state.side,
                "entry_price": state.entry_price,
                "leverage": state.leverage,
                "unrealized_pnl": round(strategy._last_unrealized_pnl, 2),
            }
        else:
            slot.current_position = None

        # Nav snapshot
        nav = slot.cumulative_invested + slot.total_pnl
        slot.nav_history.append({"ts": now_iso, "nav": round(nav, 2)})
        if len(slot.nav_history) > MAX_NAV_HISTORY:
            slot.nav_history = slot.nav_history[-MAX_NAV_HISTORY:]

        # Sync trade history from strategy state
        # Map strategy history entries to our trade format
        if len(state.history) > len(slot.trades):
            for h in state.history[len(slot.trades):]:
                leverage = h.get("leverage", 1)
                sz = h.get("sz", 0)
                entry_px = h.get("entry_price", 0)
                inst = h.get("inst_id", "")
                # Compute notional and margin
                ct_val_map = {
                    "BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1,
                    "SOL-USDT-SWAP": 1.0, "BNB-USDT-SWAP": 0.01,
                    "ADA-USDT-SWAP": 100.0, "AVAX-USDT-SWAP": 1.0,
                }
                ct_val = ct_val_map.get(inst, 1.0)
                notional = sz * ct_val * entry_px if entry_px else 0
                margin = notional / leverage if leverage else 0
                fee_est = notional * 0.0005 * 2  # entry + exit fee estimate
                slot.trades.append({
                    "ts": h.get("time", now_iso),
                    "inst_id": inst,
                    "side": h.get("side", ""),
                    "action": "close",
                    "pnl": round(h.get("pnl", 0), 2),
                    "reason": h.get("reason", ""),
                    "round": h.get("round", 0),
                    "capital_deployed": sz,
                    "leverage": leverage,
                    "entry_price": entry_px,
                    "margin": round(margin, 2),
                    "fee_est": round(fee_est, 4),
                })

        # Check terminal conditions
        finished = False
        if state.status == "TARGET_HIT":
            slot.status = "succeeded"
            finished = True
        elif state.status == "DONE":
            slot.status = "drained"
            finished = True
        elif state.status == "RECHARGE_REQUIRED":
            slot.status = "recharge_required"
            finished = True

        if finished:
            slot.finished_at = now_iso
            slot.unrealized_pnl = 0.0
            slot.total_pnl = slot.realized_pnl
            slot.roi_pct = (slot.total_pnl / slot.cumulative_invested * 100) if slot.cumulative_invested > 0 else 0.0
            slot.current_position = None
            # Stop the strategy cleanly
            try:
                strategy.stop()
            except Exception as e:
                logger.warning("Error stopping slot %d strategy: %s", slot.id, e)
            slot._strategy = None
            logger.info("Slot %d FINISHED: status=%s pnl=%.2f roi=%.2f%%",
                         slot.id, slot.status, slot.total_pnl, slot.roi_pct)

    def _get_next_pending_slot(self) -> Optional[SlotInfo]:
        """Return the next pending slot, or None if all deployed."""
        for slot in self.slots:
            if slot.status == "pending":
                return slot
        return None

    def _has_running_slot(self) -> bool:
        return any(s.status == "running" for s in self.slots)

    def _has_recharge_required_slot(self) -> bool:
        return any(s.status == "recharge_required" for s in self.slots)

    # ------------------------------------------------------------------
    # Main async loop
    # ------------------------------------------------------------------

    def start(self):
        """Start the orchestrator in a background thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(
            "YoloOrchestrator started: %d slots, $%.2f active-slot budget, $%.2f total budget",
            self.num_slots,
            float(self.cfg["budget_per_slot"]),
            float(self.cfg.get("total_budget") or (self.cfg["budget_per_slot"] * self.num_slots)),
        )

    def stop(self):
        """Stop orchestrator and all running slots."""
        self._stop_event.set()
        # Stop any running strategies
        for slot in self.slots:
            if slot._strategy is not None:
                try:
                    slot._strategy.stop()
                except Exception:
                    pass
        if self._thread:
            self._thread.join(timeout=60)
        logger.info("YoloOrchestrator stopped")

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as e:
            logger.error("YoloOrchestrator main loop error: %s", e)
        finally:
            loop.close()

    async def _async_main(self):
        """
        Core loop:
        1. If no slot running & pending slots exist -> deploy next
        2. Poll running slot every reconcile_sec
        3. If running slot finishes -> deploy next immediately
        4. If stagger timer fires (1h) & no slot running -> deploy next
        """
        # Resume an in-flight slot after restart if one was persisted as running.
        for slot in self.slots:
            if slot.status == "running" and slot._strategy is None:
                await asyncio.get_event_loop().run_in_executor(None, self._deploy_slot, slot)
                break
        else:
            first = self._get_next_pending_slot()
            if first and not self._has_running_slot():
                await asyncio.get_event_loop().run_in_executor(None, self._deploy_slot, first)

        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.cfg["reconcile_sec"])
                if self._stop_event.is_set():
                    break

                # Poll all running slots
                for slot in self.slots:
                    if slot.status == "running":
                        if slot._strategy is None:
                            await asyncio.get_event_loop().run_in_executor(None, self._deploy_slot, slot)
                        else:
                            self._poll_slot(slot)

                # Check if we need to deploy a new slot
                need_deploy = False
                running = self._has_running_slot()

                if not running:
                    if self._has_recharge_required_slot():
                        await asyncio.sleep(self.cfg["reconcile_sec"])
                        continue
                    # No slot running — check if a slot just finished or stagger timer hit
                    next_slot = self._get_next_pending_slot()
                    if next_slot:
                        # Check if a slot finished (immediate deploy) or stagger timer
                        any_finished = any(
                            s.status in ("succeeded", "drained") and s.finished_at
                            for s in self.slots
                        )
                        stagger_elapsed = (time.time() - self._last_deploy_time) >= self.cfg["stagger_interval_sec"]

                        if any_finished or stagger_elapsed or self._last_deploy_time == 0:
                            need_deploy = True

                if need_deploy:
                    next_slot = self._get_next_pending_slot()
                    if next_slot:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._deploy_slot, next_slot
                        )

                # Write state
                self._write_state()

                # Check if all slots are done
                all_done = all(s.status in ("succeeded", "drained") for s in self.slots)
                if all_done:
                    logger.info("ALL SLOTS COMPLETE. Orchestrator finished.")
                    self._write_state()
                    # Keep running so dashboard can read state, but just sleep
                    while not self._stop_event.is_set():
                        await asyncio.sleep(30)
                        self._write_state()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Orchestrator loop error: %s", e)
                await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(config: Optional[Dict] = None, foreground: bool = True) -> Optional[YoloOrchestrator]:
    """Start YOLO Orchestrator. Called by main.py _run_custom_strategy."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    orchestrator = YoloOrchestrator(config)
    orchestrator.start()

    if not foreground:
        return orchestrator

    stop_flag = threading.Event()

    def _on_signal(sig, frame):
        logger.info("YoloOrchestrator: signal %d received — shutting down", sig)
        stop_flag.set()

    _signal.signal(_signal.SIGTERM, _on_signal)
    _signal.signal(_signal.SIGINT, _on_signal)

    logger.info("YoloOrchestrator running in foreground (%d slots). Ctrl+C to stop.",
                orchestrator.num_slots)
    try:
        while not stop_flag.is_set() and not orchestrator._stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        orchestrator.stop()

    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="YOLO Orchestrator — 10x staggered YOLO Momentum")
    parser.add_argument("--profile", default="live", choices=["demo", "live"])
    parser.add_argument("--slots", type=int, default=10, help="Number of slots")
    parser.add_argument("--budget", type=int, default=1000, help="Budget per slot in USDT")
    args = parser.parse_args()

    run(config={
        "profile": args.profile,
        "num_slots": args.slots,
        "budget_per_slot": args.budget,
    })
