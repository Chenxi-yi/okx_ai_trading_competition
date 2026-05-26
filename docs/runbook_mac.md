# macOS Runbook

Use the root `okx_trading_system.command` launcher. It delegates to
`platform/mac/okx_trading_system.command`.

The macOS launcher starts:

- `launcher/launcher_server.py`
- `engine/data/refresh_scheduler.py`

Runtime logs and pid files remain under `engine/logs/` and `engine/control/`.
Those files are ignored by Git.
