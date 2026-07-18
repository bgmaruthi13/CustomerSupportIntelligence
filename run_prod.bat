@echo off
setlocal enabledelayedexpansion
title Correlate - Production Server
cd /d "%~dp0"

echo ============================================================
echo  Correlate - Production Launch (Waitress)
echo ============================================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] No virtual environment found. Run install.bat first.
    pause
    exit /b 1
)
set "VENV_PY=venv\Scripts\python.exe"

if not exist ".env" (
    echo [ERROR] No .env file found. Copy .env.example to .env and configure it
    echo         ^(DJANGO_SECRET_KEY, DJANGO_DEBUG=False, DJANGO_ALLOWED_HOSTS^) first.
    pause
    exit /b 1
)

findstr /b /c:"DJANGO_DEBUG=False" ".env" >nul
if errorlevel 1 (
    echo [WARNING] .env does not set DJANGO_DEBUG=False - this will run in DEBUG mode.
    echo           Edit .env before deploying to a real environment.
    echo.
)

echo [1/3] Applying database migrations...
"%VENV_PY%" manage.py migrate --noinput
if errorlevel 1 (
    echo [ERROR] Database migration failed. See the error above.
    pause
    exit /b 1
)

echo.
echo [2/3] Collecting static files...
"%VENV_PY%" manage.py collectstatic --noinput >nul
if errorlevel 1 (
    echo [ERROR] collectstatic failed. See the error above.
    pause
    exit /b 1
)

echo.
echo [3/3] Starting Waitress on http://0.0.0.0:8000/  (Ctrl+C to stop)
echo ============================================================
echo.

"%VENV_PY%" -m waitress --host=0.0.0.0 --port=8000 --threads=8 correlate.wsgi:application

pause
