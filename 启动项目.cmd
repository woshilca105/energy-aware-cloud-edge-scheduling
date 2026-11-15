@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if not exist ".venv\Scripts\python.exe" (
  echo Project Python not found: .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "research\project_runner.py" %*
set "PROJECT_EXIT=%ERRORLEVEL%"
if not defined PROJECT_NO_PAUSE pause
exit /b %PROJECT_EXIT%
