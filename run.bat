@echo off
title WidgetFM — Discord Stats Widget
color 0A
cd /d "%~dp0"

echo ============================================
echo    WidgetFM — Discord Stats Widget
echo ============================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found!
    echo Make sure Python is installed and added to PATH.
    echo Download: https://python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: Run the script
echo Starting upstats.py...
echo Press Ctrl+C to stop the script.
echo.
python upstats.py

echo.
echo ============================================
echo    Script stopped.
echo ============================================
pause
