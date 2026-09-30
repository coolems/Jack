@echo off
setlocal EnableExtensions
chcp 65001 >nul
title COOLEMS - ZZZ initial init (fresh-clone setup)

rem ===========================================================================
rem  ZZZ_initial_init.bat - one double-click to make a FRESH GITHUB CLONE run.
rem
rem  This file does ONE job: find a good Python (>= 3.10). If it finds one,
rem  all real work is done by utils\zzz_init.py (GPU detect + model download +
rem  llama.cpp binaries + API keys + config wiring - see that script for details).
rem
rem  If no suitable Python is found, you are told exactly where to get it.
rem ===========================================================================

echo.
echo ============================================================
echo    COOLEMS - ZZZ initial init (fresh-clone setup)
echo ============================================================
echo.

cd /d "%~dp0" || (echo  [ERROR] Cannot cd to repo root. & pause & exit /b 1)

rem ---- STEP 1: find Python >= 3.10 ------------------------------------------
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
    where python >nul 2>nul && set "PYCMD=python"
)

if not defined PYCMD goto :no_python

rem Probe the real version (some Windows Store stubs answer 'python' but are useless).
set "PYVER="
for /f "usebackq tokens=*" %%v in (`%PYCMD% -c "import sys;print('.'.join(map(str,sys.version_info[:2])))" 2^>nul`) do set "PYVER=%%v"

if not defined PYVER goto :no_python
echo  [i] Found Python %PYVER% via: %PYCMD%
echo.

rem ---- Hand off to the real init script --------------------------------------
%PYCMD% utils\zzz_init.py
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:no_python
echo.
echo  [!] No usable Python found on this machine (need version 3.10 or newer).
echo.
echo      The init process needs Python to run. Please:
echo.
echo        1. Open   https://www.python.org/downloads/
echo        2. Download and install the latest Windows installer.
echo           IMPORTANT: tick "Add python.exe to PATH" on the first screen.
echo        3. Re-run this file (ZZZ_initial_init.bat).
echo.
pause
exit /b 1
