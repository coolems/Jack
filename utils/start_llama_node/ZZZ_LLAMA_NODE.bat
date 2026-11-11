@echo off
setlocal EnableExtensions EnableDelayedExpansion
title COOLEMS - Llama Fleet (admin)
cd /d "%~dp0..\.."

REM ===========================================================================
REM  COOLEMS LLAMA FLEET - ADMIN LAUNCHER (2026-11 multi-config fleet)
REM
REM  ONE menu for every llama-service on this machine. The configs live in
REM  config/llama_servers.json - a JSON LIST, one entry per llama-server:
REM  name, host, port, model, slots, ctx/slot, threads, gpu layers, bind,
REM  extra_args, auto_start. Three entries on three ports = three llama-services
REM  with three different models on one machine (if the RAM allows it).
REM
REM  Regular users never need this: ZZZ_SERVER.bat starts the LOCAL entry itself.
REM
REM  Before any start, the memory gate checks free RAM/VRAM. If Strata or another
REM  llama already loaded the machine, the start is REFUSED with the numbers -
REM  we never load a second model and go out of memory.
REM
REM  Non-interactive passthrough (scheduled-task friendly):
REM     ZZZ_LLAMA_NODE.bat start local
REM     ZZZ_LLAMA_NODE.bat list
REM ===========================================================================

REM ---- venv: create + install once on first run (repo-root venv) -------------
if not exist "venv\Scripts\python.exe" (
    echo [VENV] First run - creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Could not create the venv. Install Python 3.10+ and retry.
        pause
        exit /b 1
    )
    call "venv\Scripts\activate.bat"
    pip install -r requirements.txt
) else (
    call "venv\Scripts\activate.bat"
)

REM ---- non-interactive passthrough: ZZZ_LLAMA_NODE.bat start local ----------
REM  !ERRORLEVEL! (not %ERRORLEVEL%): inside a parenthesized block cmd expands %VAR% when
REM  the block is PARSED, i.e. before python runs - the exit code would always be the old
REM  one (0). Delayed expansion reads it after the command actually finished.
if not "%~1"=="" (
    python utils\start_llama_node\llama_node.py %*
    exit /b !ERRORLEVEL!
)

REM ---- menu -----------------------------------------------------------------
REM  NOTE: every command runs under its own label, NOT inside an if-block.
REM  A "set /p NAME=Config name:" prompt inside a parenthesized block is parsed
REM  as a label by cmd (the ':' in the prompt text) and the whole block dies with
REM  ": was unexpected at this time." - so prompts live outside blocks.
REM  Each action clears the screen first, so you always see one fresh result.
:menu
cls
REM Fresh menu: clear any stale 'stay on page' left by a previous visit.
set "BACKTO="
echo ============================================================
echo    COOLEMS LLAMA FLEET  (admin menu - utils/start_llama_node)
echo ============================================================
echo.
echo   1) List configs              (config/llama_servers.json + what is running)
echo   2) Start one config          (asks for the name; 'all' = every auto_start)
echo   3) Configure / add a config  (wizard, saves the whole list)
echo   4) Status                    (all configs, or one by name)
echo   5) Stop one config
echo   6) Restart one config
echo   7) Show the raw llama-server command
echo   8) Delete a config
echo   9) Exit
echo.
set "CHOICE="
set /p CHOICE=Select [1-9]: 

REM Empty input (or a closed stdin, e.g. a scheduled task) exits the menu instead of
REM re-printing it forever.
if "%CHOICE%"=="" goto done

if "%CHOICE%"=="1" goto do_list
if "%CHOICE%"=="2" goto do_start
if "%CHOICE%"=="3" goto do_configure
if "%CHOICE%"=="4" goto do_status
if "%CHOICE%"=="5" goto do_stop
if "%CHOICE%"=="6" goto do_restart
if "%CHOICE%"=="7" goto do_cmd
if "%CHOICE%"=="8" goto do_delete
if "%CHOICE%"=="9" goto done

echo   invalid choice '%CHOICE%' - pick 1-9
goto menu

REM ---- actions: clear the screen, show the fleet, then do the thing ----------

:do_list
cls
REM Display-only page: NO BACKTO, so after the pause it goes back to the MENU.
python utils\start_llama_node\llama_node.py list
goto after

:do_start
cls
set "BACKTO=do_start"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config to start - name or number, 'all' = every auto_start, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py start "%NAME%"
goto after

:do_configure
cls
set "BACKTO=do_configure"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config to modify - existing = edit it, new name = add it, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py configure "%NAME%"
goto after

:do_status
cls
set "BACKTO=do_status"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config to check - name or number, empty = all of them: 
python utils\start_llama_node\llama_node.py status "%NAME%"
goto after

:do_stop
cls
set "BACKTO=do_stop"
python utils\start_llama_node\llama_node.py list
echo.
:stop_prompt
set "NAME="
set /p NAME=Which config to stop - name or number, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py stop "%NAME%"
echo.
set "AGAIN="
set /p AGAIN=Stop another config? [y/n]: 
if /i "!AGAIN!"=="y" goto stop_prompt
goto after

:do_restart
cls
set "BACKTO=do_restart"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config to restart - name or number, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py restart "%NAME%"
goto after

:do_cmd
cls
set "BACKTO=do_cmd"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config's command to show - name or number, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py cmd "%NAME%"
goto after

:do_delete
cls
set "BACKTO=do_delete"
python utils\start_llama_node\llama_node.py list
echo.
set "NAME="
set /p NAME=Which config to delete - name or number, empty = back to menu: 
if "%NAME%"=="" goto cancel_menu
python utils\start_llama_node\llama_node.py delete "%NAME%"
goto after

REM Cancelled (empty input): nothing ran, so no pause - straight back to the menu.
:cancel_menu
goto menu

REM After the pause, stay on the SAME page (re-run that action) so you can e.g. try or stop
REM another config right away - press Enter at its prompt to get back to the main menu.
REM The list page is display-only and has no BACKTO: it always returns to the menu.
:after
echo.
pause
if defined BACKTO goto %BACKTO%
goto menu

:done
exit /b 0
