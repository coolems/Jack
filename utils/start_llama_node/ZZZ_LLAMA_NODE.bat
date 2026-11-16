@echo off
setlocal EnableExtensions
title COOLEMS - Llama Node (admin)
cd /d "%~dp0..\.."

REM ===========================================================================
REM  COOLEMS LLAMA NODE - ADMIN LAUNCHER (2026-10 fleet)
REM
REM  For admins who want to configure and start ANY node they like. Regular
REM  users never need this: ZZZ_SERVER.bat auto-starts the local node from
REM  config/llama_node.json (init writes sane defaults there).
REM
REM  Runs on a GPU machine that is NOT the brain. Starts ONE llama-server with
REM  the configured model/slots, verifies it, prints the brain entry, and exits.
REM  The llama-server keeps running in the background.
REM
REM  First run: choose "2) Configure this machine" and answer the questions.
REM  After that, "1) Start" is idempotent - safe to schedule every 5 minutes
REM  (call this .bat with arguments, e.g.  ZZZ_LLAMA_NODE.bat start --force ).
REM ===========================================================================

REM ---- venv: create + install once on first run (repo-root venv) -------------
if not exist "venv\Scripts\python.exe" (
    echo [VENV] First run - creating virtual environment...
    py -3.12 -m venv venv 2>nul
    if errorlevel 1 py -3 -m venv venv 2>nul
    if not exist "venv\Scripts\python.exe" python -m venv venv 2>nul
    if not exist "venv\Scripts\python.exe" goto :no_python
    venv\Scripts\python.exe -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 3)"
    if errorlevel 1 goto :no_python
    call "venv\Scripts\activate.bat"
    pip install -r requirements.txt
) else (
    call "venv\Scripts\activate.bat"
)

REM ---- non-interactive passthrough: ZZZ_LLAMA_NODE.bat start --force --------
if not "%~1"=="" (
    python utils\start_llama_node\llama_node.py %*
    exit /b %ERRORLEVEL%
)

:menu
cls
echo ============================================================
echo    COOLEMS LLAMA NODE  (admin launcher - utils/start_llama_node)
echo ============================================================
echo.
echo   1) Start (saved settings; no-op if already running)
echo   2) Configure this machine (wizard)
echo   3) Status
echo   4) Restart
echo   5) Stop
echo   6) Show the raw llama-server command
echo   7) Exit
echo.
set "CHOICE="
set /p CHOICE=Select [1-7]:

if "%CHOICE%"=="1" python utils\start_llama_node\llama_node.py start
if "%CHOICE%"=="2" python utils\start_llama_node\llama_node.py configure
if "%CHOICE%"=="3" python utils\start_llama_node\llama_node.py status
if "%CHOICE%"=="4" python utils\start_llama_node\llama_node.py restart
if "%CHOICE%"=="5" python utils\start_llama_node\llama_node.py stop
if "%CHOICE%"=="6" python utils\start_llama_node\llama_node.py cmd
if "%CHOICE%"=="7" exit /b 0

echo.
pause
goto menu

:no_python
echo.
echo [ERROR] COOLEMS needs Python 3.12 or newer, and this machine has none.
echo         1. Open   https://www.python.org/downloads/
echo         2. Install a 3.12+ Windows installer and tick "Add python.exe to PATH".
echo         3. Run this file again.
echo.
pause
exit /b 1
