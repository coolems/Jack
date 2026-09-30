@echo off
    setlocal enabledelayedexpansion
    title COOLEMS - Web Relay Manager
    echo ========================================
    echo  COOLEMS - Web Relay Manager (Tk GUI)
    echo ========================================

    REM --- Locate repo root and venv (one level up from this folder) ---
    set "REPO_ROOT=%~dp0.."
    if not exist "%REPO_ROOT%\venv\Scripts\python.exe" (
        echo [VENV] Creating virtual environment in %REPO_ROOT%\venv...
        python -m venv "%REPO_ROOT%\venv"
        if errorlevel 1 (
            echo [ERROR] Failed to create venv. Check Python installation.
            pause
            exit /b 1
        )
    )

    REM Activate virtual environment (server root)
    call "%REPO_ROOT%\venv\Scripts\activate.bat"

    REM --- Dependency check: websockets is required; tkinter must ship with Python ---
    python -c "import websockets" >nul 2>&1 || (
        echo [PIP] Installing dependencies from requirements.txt...
        pip install --upgrade pip >nul 2>&1
        pip install -r "%REPO_ROOT%\requirements.txt"
        if errorlevel 1 (
            echo.
            echo [ERROR] Failed to install dependencies. Check internet connection.
            pause
            exit /b 1
        )
    )

    python -c "import tkinter" >nul 2>&1 || (
        echo.
        echo [ERROR] tkinter is missing from this Python installation.
        echo         Reinstall Python from python.org and tick 'tcl/tk and IDLE'.
        pause
        exit /b 1
    )

    REM --- Start the manager GUI window (extra args pass through, e.g. --relay host:port) ---
    echo.
    echo [UI] A desktop window will open - no web server or port is used.
    echo [UI] Close the window (or press Ctrl+C here) to stop the manager.
    echo.
    python "%~dp0zzz_manage_server_relay.py" %*

    echo.
    echo ========================================
    echo Process exited with code: %ERRORLEVEL%
    echo ========================================
    pause
