@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
title PocketMind

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found in this folder.
  echo This project must use its own Python environment, not system python.
  echo Please screenshot this window and send it to the assistant.
  echo.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   PocketMind M1
echo ============================================================
echo Starting... browser will open automatically.
echo If the browser does not open, copy the URL shown below.
echo Close this window to stop the server.
echo.

".venv\Scripts\python.exe" -m pocketmind.launcher
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
  echo [ERROR] Exit code: %EXITCODE%
  echo Please screenshot this window and send it to the assistant.
) else (
  echo Server stopped.
)
echo.
pause
