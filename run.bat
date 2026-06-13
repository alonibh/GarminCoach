@echo off
REM ============================================================
REM  GarminCoach - one-click setup & run for Windows
REM  Double-click this file, or run it from a terminal.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo(
echo === GarminCoach ===
echo(

REM --- 1) Find Python ----------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python was not found on your PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install, then re-run this file.
    pause
    exit /b 1
)
echo Using Python: %PY%

REM --- 2) Create virtual environment (first run only) --------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists.
)

set "VENV_PY=.venv\Scripts\python.exe"

REM --- 3) Install / update dependencies ----------------------
echo Installing dependencies (this may take a minute the first time) ...
"%VENV_PY%" -m pip install --upgrade pip >nul
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See the messages above.
    pause
    exit /b 1
)

REM --- 4) Create .env on first run, then stop for editing ----
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo(
        echo ============================================================
        echo A new .env file was created from .env.example.
        echo Open .env in a text editor and set GARMIN_EMAIL to your
        echo Garmin Connect email, then run this file again.
        echo ============================================================
        echo(
        start "" notepad ".env"
        pause
        exit /b 0
    ) else (
        echo [WARN] No .env or .env.example found; continuing with defaults.
    )
)

REM --- 5) Launch the app & open the browser ------------------
echo(
echo Starting GarminCoach at http://localhost:8000
echo (First time: click "Connect your Garmin account" to log in.)
echo Press Ctrl+C in this window to stop the server.
echo(
start "" "http://localhost:8000"
"%VENV_PY%" app.py

pause
endlocal
