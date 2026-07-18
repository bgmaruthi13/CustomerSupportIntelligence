@echo off
setlocal enabledelayedexpansion
title Correlate - Install and Run
cd /d "%~dp0"

echo ============================================================
echo  Correlate - AI-Driven Problem Management
echo  Install / Launch script for Windows
echo ============================================================
echo.

REM --- 1. Find a Python interpreter -----------------------------------
set "PYEXE="
where python >nul 2>nul
if %errorlevel%==0 (
    set "PYEXE=python"
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set "PYEXE=py"
    )
)

if "%PYEXE%"=="" (
    echo [ERROR] Python was not found on this machine.
    echo Install Python 3.12 from https://www.python.org/downloads/
    echo   (during setup, tick "Add python.exe to PATH"^)
    echo Then re-run this script.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PYEXE% --version 2^>^&1') do set "PYVER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set "PYMAJOR=%%a"
    set "PYMINOR=%%b"
)
echo Using Python %PYVER%

if not "%PYMAJOR%.%PYMINOR%"=="3.12" (
    echo.
    echo [WARNING] This app is tested and pinned against Python 3.12 ^(e.g. 3.12.10^).
    echo You have Python %PYVER% on PATH. Dependency versions in requirements.txt -
    echo especially the PyTorch CPU wheel - are pinned for Python 3.12 and may fail
    echo to install, or install a mismatched build, on a different version.
    echo.
    echo Recommended: install Python 3.12 from https://www.python.org/downloads/
    echo and re-run this script. Continuing with %PYVER% in 5 seconds anyway...
    timeout /t 5 >nul
)
echo.

REM --- 2. Create the virtual environment + install dependencies (first run only)
if not exist "venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment...
    %PYEXE% -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )

    set "VENV_PY=venv\Scripts\python.exe"
    set "VENV_PIP=venv\Scripts\pip.exe"

    echo [2/4] Upgrading pip...
    "!VENV_PY!" -m pip install --upgrade pip --quiet

    echo [3/4] Installing dependencies from requirements.txt...
    echo This includes the PyTorch CPU build - the biggest download - please be patient.
    "!VENV_PIP!" install --no-cache-dir -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies. See the error above.
        echo Common causes: no internet connection, or a Python version other than
        echo 3.12 ^(you have %PYVER%^) that doesn't have a matching PyTorch wheel.
        echo Delete the "venv" folder after fixing the issue, then re-run this script.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists - skipping dependency install.
    echo Delete the "venv" folder and re-run this script to force a clean reinstall.
)

set "VENV_PY=venv\Scripts\python.exe"

REM --- 3. Local .env bootstrap (first run only) ---------------------------
if not exist ".env" (
    echo.
    echo Creating .env with local-development defaults...
    > ".env" echo DJANGO_DEBUG=True
    >> ".env" echo DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
    >> ".env" echo DJANGO_BEHIND_TLS=False
    "%VENV_PY%" -c "from django.core.management.utils import get_random_secret_key; print('DJANGO_SECRET_KEY=' + get_random_secret_key())" >> ".env"
    echo Created .env - see .env.example for production settings ^(DEBUG=False etc.^)
)

REM --- 4. Database setup -------------------------------------------------
echo.
echo [4/4] Applying database migrations and collecting static files...
"%VENV_PY%" manage.py migrate --noinput
if errorlevel 1 (
    echo [ERROR] Database migration failed. See the error above.
    pause
    exit /b 1
)

"%VENV_PY%" manage.py collectstatic --noinput >nul
if errorlevel 1 (
    echo [ERROR] collectstatic failed. See the error above.
    pause
    exit /b 1
)

REM --- 5. Launch --------------------------------------------------------
echo.
echo ============================================================
echo  Correlate is starting at http://127.0.0.1:8000/
echo.
echo  No account exists yet. Create one with:
echo    venv\Scripts\python.exe manage.py createsuperuser
echo.
echo  Press Ctrl+C in this window to stop the server.
echo ============================================================
echo.

start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000/"
"%VENV_PY%" manage.py runserver 8000

pause
