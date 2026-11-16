@echo off
setlocal EnableExtensions
chcp 65001 >nul
title COOLEMS - ZZZ initial init (fresh-clone setup)

rem ===========================================================================
rem  ZZZ_initial_init.bat - one double-click to make a FRESH GITHUB CLONE run.
rem
rem  This file does ONE job: find Python 3.12+. If it finds one, all real work
rem  is done by utils\zzz_init.py (GPU detect + model download + llama.cpp
rem  binaries + API keys + config wiring - see that script for details).
rem
rem  If no suitable Python is found, you are told exactly where to get it.
rem ===========================================================================

echo.
echo ============================================================
echo    COOLEMS - ZZZ initial init (fresh-clone setup)
echo ============================================================
echo.

cd /d "%~dp0" || (echo  [ERROR] Cannot cd to repo root. & pause & exit /b 1)

rem ---- STEP 1: find Python 3.12+ --------------------------------------------
set "PYCMD="
set "PYVER="

where py >nul 2>nul
if errorlevel 1 goto :try_plain
call :probe "py -3.12"
if not errorlevel 1 (
    set "PYCMD=py -3.12"
    goto :found
)
call :probe "py -3"
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :found
)

:try_plain
where python >nul 2>nul || goto :no_python
call :probe "python"
if not errorlevel 1 (
    set "PYCMD=python"
    goto :found
)
goto :no_python

:found
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
echo  [!] No Python 3.12 or newer found on this machine.
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

rem ---- probe: %~1 = python command; sets PYVER, returns 0 only when 3.12+ ----
:probe
set "PROBE_VER="
for /f "usebackq tokens=*" %%v in (`%~1 -c "import sys;print('.'.join(map(str,sys.version_info[:2])))" 2^>nul`) do set "PROBE_VER=%%v"
if not defined PROBE_VER exit /b 1
%~1 -c "import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)" 2>nul || exit /b 1
set "PYVER=%PROBE_VER%"
exit /b 0
