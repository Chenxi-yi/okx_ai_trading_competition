@echo off
setlocal EnableExtensions
set "ROOT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%platform\windows\okx_trading_system_windows.ps1"
echo.
pause
