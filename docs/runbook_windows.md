# Windows Runbook

Use the root `okx_trading_system_windows.cmd` launcher. It delegates to
`platform/windows/okx_trading_system_windows.ps1`.

The PowerShell script starts:

- `launcher/launcher_server.py`
- `engine/data/refresh_scheduler.py`

The script uses `-NoProfile -ExecutionPolicy Bypass` so Windows profile policy
errors do not pop up during normal double-click startup.

Runtime logs and pid files remain under `engine/logs/` and `engine/control/`.
Those files are ignored by Git.
