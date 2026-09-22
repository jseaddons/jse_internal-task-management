@echo off
title JSE Task Log Server
cd /d "%~dp0"

echo ============================================================
echo   JSE Task Log - making sure the server is running
echo.
echo   You can close this window afterwards. The server keeps
echo   running in the background (no window to leave open).
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1"
if errorlevel 1 (
  echo.
  echo   Could not start. Open watchdog.log in this folder and
  echo   send it to whoever looks after the Task Log.
  echo.
  pause
  exit /b 1
)

echo.
echo   The page is up.
echo   This PC:     http://localhost:8000
echo   Teammates:   double-click  JSE Task Log.url  on the Timesheet share.
echo                They must NOT use localhost.
echo.
pause
