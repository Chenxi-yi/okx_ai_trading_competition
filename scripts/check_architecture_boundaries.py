#!/usr/bin/env python3
"""Check high-risk architecture boundary violations.

This is a lightweight guardrail for the full refactor. It is intentionally
pattern-based so it can run without importing strategy modules or touching OKX.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTION_ALLOWED = {
    "engine/execution/broker.py",
    "engine/execution/router.py",
    "engine/kit/execution_gateway.py",
    "engine/kit/client.py",
    "launcher/launcher_server.py",
}
LEGACY_WARNING = {
    "engine/competition/strategies/elite_flow.py",
    "engine/competition/strategies/yolo_momentum.py",
    "engine/competition/strategies/yolo_orchestrator.py",
}
RETIRED_MARKER = "ARCHITECTURE_STATUS: retired_legacy_runner"
COMPAT_ALLOWED = {
    # Compatibility adapters are still being migrated. These may call the Kit
    # gateway, but direct okx swap place/close/leverage patterns are forbidden.
    "scripts/run_research_sleeve_paper.py",
    "scripts/run_c_auto_v2_micro_live.py",
}
SEARCH_ROOTS = ("engine", "scripts", "launcher")
DIRECT_TRADE_PATTERNS = (
    re.compile(r'"swap"\s*,\s*"place"'),
    re.compile(r'"swap"\s*,\s*"close"'),
    re.compile(r'"swap"\s*,\s*"leverage"'),
    re.compile(r"okx\s+.*swap\s+(place|close|leverage)"),
)


def main() -> int:
    violations: list[str] = []
    for root_name in SEARCH_ROOTS:
        for path in (ROOT / root_name).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in EXECUTION_ALLOWED:
                continue
            if rel == "scripts/check_architecture_boundaries.py":
                continue
            text = path.read_text(errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                    continue
                if not any(pattern.search(line) for pattern in DIRECT_TRADE_PATTERNS):
                    continue
                if rel in LEGACY_WARNING and RETIRED_MARKER in text:
                    continue
                if rel in LEGACY_WARNING:
                    violations.append(f"{rel}:{line_no}: legacy live runner missing retired marker")
                    continue
                if rel in COMPAT_ALLOWED and "_run_okx" not in line and "subprocess" not in line:
                    continue
                violations.append(f"{rel}:{line_no}: {line.strip()}")
    if violations:
        print("Architecture boundary violations:")
        for item in violations:
            print(f"- {item}")
        return 1
    print("Architecture boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
