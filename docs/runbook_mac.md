# macOS Runbook

Use the root `okx_trading_system.command` launcher. It delegates to
`platform/mac/okx_trading_system.command`.

The macOS launcher starts:

- `launcher/launcher_server.py`
- `engine/data/refresh_scheduler.py`
- `scripts/run_system_watchdog.py`

Runtime logs and pid files remain under `engine/logs/` and `engine/control/`.
Those files are ignored by Git.

The watchdog writes status to `engine/logs/system_watchdog/status.json`. It
keeps data refresh, ownership reconciliation, and both environment runners alive
through the unified runner/control path.
