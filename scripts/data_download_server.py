#!/usr/bin/env python3
"""Run the standalone data download dashboard."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.download.server import main


if __name__ == "__main__":
    raise SystemExit(main())
