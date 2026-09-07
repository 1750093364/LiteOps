@echo off
rem LiteOps one-click launcher (pure ASCII: parses identically under any console codepage)
rem Start the app, then open http://127.0.0.1:8000 in your browser. See README.md (Chinese docs).
cd /d "%~dp0"
setlocal enabledelayedexpansion

rem Force UTF-8 for console codepage AND Python/pip (avoid GBK decode errors on zh-CN Windows)
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

rem Locate Python (prefer python, fallback to py launcher)
set "PYLAUNCHER=python"
where python >nul 2>&1
if !errorlevel! neq 0 (
    set "PYLAUNCHER=py -3"
    where py >nul 2>&1
    if !errorlevel! neq 0 (
        echo [LiteOps] Python not found. Install Python 3.11 and check "Add to PATH".
        pause
        exit /b 1
    )
)

rem Create venv on first run
if not exist ".venv" (
    echo [LiteOps] First run: creating virtual environment...
    %PYLAUNCHER% -m venv .venv
    if !errorlevel! neq 0 (
        echo [LiteOps] Failed to create venv. Check your Python installation.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

rem Install dependencies when missing (abort on failure instead of half-installed start)
python -c "import fastapi" >nul 2>&1
if !errorlevel! neq 0 (
    echo [LiteOps] Installing dependencies, takes a few minutes on first run...
    python -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo [LiteOps] Dependency install failed. Check your network and retry.
        pause
        exit /b 1
    )
)

echo [LiteOps] Starting server: http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause