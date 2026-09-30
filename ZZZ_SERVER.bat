@echo off
setlocal enabledelayedexpansion
echo ========================================
echo  COOLEMS - Server / Client Launcher
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

REM Activate virtual environment (server root)
call "%~dp0venv\Scripts\activate.bat"
pip install -r requirements.txt

python code.py %*

echo.
echo ========================================
echo Process exited with code: %ERRORLEVEL%
echo ========================================
pause