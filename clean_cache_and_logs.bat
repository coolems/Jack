@echo off
REM ============================================
REM COOLEMS Agent - Clean Startup Script
REM Clears logs and cache before starting server
REM ============================================

echo ============================================
echo   COOLEMS Agent - Clean Startup
echo ============================================
echo.

REM Clear logs
echo [2/3] Clearing old logs...
python utils\clear_logs.py
echo.

REM Clear Python cache
echo [3/3] Clearing Python bytecode cache...
python utils\clear_pycache.py
echo.
