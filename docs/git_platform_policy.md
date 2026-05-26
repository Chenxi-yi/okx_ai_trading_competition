# Git And Platform Policy

This repository is the single source of truth for both macOS and Windows.
Do not fork the trading system into separate platform repositories.

## Layout

- `engine/`, `launcher/`, and `scripts/` contain platform-neutral trading logic.
- `platform/mac/` contains macOS launcher wrappers and setup helpers.
- `platform/windows/` contains Windows launcher wrappers and setup helpers.
- Root-level launchers are compatibility shims only, so existing double-click
  habits keep working.

## Rules

- Strategy code emits candidates only; it must not become a platform launcher.
- Investment committee, position management, risk, execution, accounting, and
  reconciliation remain shared across platforms.
- Keep one strategy registry and one committee policy set.
- Isolate platform-specific shell, path, PowerShell, and startup behavior under
  `platform/` or a small shared helper.
- Do not commit runtime logs, cache data, pid/lock/stop files, credentials, or
  AppleDouble `._*` files.
- Before pushing platform changes, run at least syntax checks for changed Python
  and JavaScript files plus a launcher health check when the machine is local.
