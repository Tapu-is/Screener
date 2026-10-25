@echo off
rem Launcher for Windows: runs stk from anywhere, e.g.  stk quote RELIANCE
setlocal
set "DIR=%~dp0.."
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
set "PYTHONUTF8=1"
if exist "%DIR%\.venv\Scripts\python.exe" (
  "%DIR%\.venv\Scripts\python.exe" -m screener %*
) else (
  python -m screener %*
)
exit /b %ERRORLEVEL%
