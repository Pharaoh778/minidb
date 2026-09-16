@echo off
REM Launcher for test_data.sql on Windows.
REM Usage: double-click, or "run_test_data.bat [--clean] [-d demo_data] [-s FIFO] [-p 8]"

chcp 65001 >nul
cd /d "%~dp0"

set "PY_CMD="
where python >nul 2>nul && set "PY_CMD=python"
if not defined PY_CMD ( where py >nul 2>nul && set "PY_CMD=py -3" )

if not defined PY_CMD (
    echo [ERROR] Python not found. Please install Python 3 and add it to PATH.
    pause
    exit /b 1
)

%PY_CMD% run_test_data.py %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo [FAILED] Exit code: %EXIT_CODE%
) else (
    echo [OK] Finished successfully.
)
pause
exit /b %EXIT_CODE%
