@echo off
setlocal enabledelayedexpansion
echo ========================================
echo  COOLEMS - Server / Client Launcher
echo ========================================

REM --- Auto-create venv if missing (needs Python 3.12+) ---
if not exist "%~dp0venv\Scripts\python.exe" (
    echo [VENV] Creating virtual environment in venv\ ...
    py -3.12 -m venv "%~dp0venv" 2>nul
    if errorlevel 1 py -3 -m venv "%~dp0venv" 2>nul
    if not exist "%~dp0venv\Scripts\python.exe" python -m venv "%~dp0venv" 2>nul
    if not exist "%~dp0venv\Scripts\python.exe" goto :no_python
    "%~dp0venv\Scripts\python.exe" -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 3)"
    if errorlevel 1 goto :no_python
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
exit /b %ERRORLEVEL%

:no_python
echo.
echo [ERROR] COOLEMS needs Python 3.12 or newer, and this machine has none.
echo         1. Open   https://www.python.org/downloads/
echo         2. Install a 3.12+ Windows installer and tick "Add python.exe to PATH".
echo         3. Run this file again.
echo.
pause
exit /b 1