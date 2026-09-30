@echo off
setlocal enabledelayedexpansion
echo ========================================
echo COOLEMS CLIENT - Launcher
echo ========================================

REM --- Auto-create venv if missing ---
if not exist "%~dp0venv\Scripts\python.exe" (
    echo [VENV] Creating virtual environment in CLIENT/venv...
    python -m venv "%~dp0venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv. Check Python installation.
        pause
        exit /b 1
    )
)

REM --- Activate virtual environment ---
call "%~dp0venv\Scripts\activate.bat"
echo [VENV] Activated CLIENT/venv

REM --- Auto-install requirements if httpx or playwright is missing (first run or new package added) ---
set DEPS_OK=1
python -c "import httpx" >nul 2>&1
if errorlevel 1 set DEPS_OK=0
python -c "import playwright" >nul 2>&1
if errorlevel 1 set DEPS_OK=0

if %DEPS_OK%==0 (
    echo [PIP] Installing dependencies from requirements.txt...
    pip install --upgrade pip >nul 2>&1
    pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies. Check internet connection.
        pause
        exit /b 1
    )
    echo [PIP] Dependencies installed successfully!
)

REM --- Install playwright browsers if missing ---
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.executable_path; p.stop()" >nul 2>&1
if errorlevel 1 (
    echo [PLAYWRIGHT] Installing browser binaries...
    python -m playwright install chromium
)

REM --- Kill any stale process on port 8000 before starting ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo [CLEANUP] Killing old process PID %%a on port 8000...
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

REM --- Start the client application ---
python code_client.py %*

echo.
echo ========================================
echo Process exited with code: %ERRORLEVEL%
echo ========================================
pause
