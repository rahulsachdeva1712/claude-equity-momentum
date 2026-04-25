@echo off
rem =======================================================================
rem  Equity Momentum Rebalance - one-click launcher (Windows)
rem  - creates venv on first run
rem  - ensures %USERPROFILE%\Documents\shared\.env exists (seeded if absent)
rem  - starts worker + web in separate console windows
rem  - opens the UI in the default browser
rem  See docs/FRD.md B.2 (process topology) and B.10 (stale PID cleanup).
rem =======================================================================
setlocal enableextensions

set "ROOT=%~dp0"
pushd "%ROOT%"

rem ---- locate Python -----------------------------------------------------
where py >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo Python not found on PATH. Install Python 3.11+ from python.org and retry.
        echo.
        pause
        popd & exit /b 1
    )
    set "PY=python"
)

rem ---- create venv + install on first run --------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv || (
        echo Failed to create venv. & pause & popd & exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    echo Installing project ^(this takes a minute on first run^)...
    ".venv\Scripts\python.exe" -m pip install -e ".[dev]" || (
        echo pip install failed. & pause & popd & exit /b 1
    )
)

rem ---- ensure shared .env ------------------------------------------------
rem Credentials live at %USERPROFILE%\Documents\shared\.env so the same
rem file can be consumed by sibling tools running against the same Dhan
rem account. Override with the EMRB_ENV_FILE env var if needed.
set "SHARED_DIR=%USERPROFILE%\Documents\shared"
set "ENV_PATH=%SHARED_DIR%\.env"
if not exist "%SHARED_DIR%" mkdir "%SHARED_DIR%"
if not exist "%ENV_PATH%" (
    if exist "%ROOT%.env.example" (
        copy /y "%ROOT%.env.example" "%ENV_PATH%" >nul
    ) else (
        rem Seed a minimal .env if .env.example is missing.
        (
            echo # Credentials file. Paste a fresh Dhan access token daily.
            echo DHAN_CLIENT_ID=
            echo DHAN_ACCESS_TOKEN=
        ) > "%ENV_PATH%"
    )
    echo.
    echo First-time setup: paste DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN into
    echo   %ENV_PATH%
    echo Then re-run this file.
    echo.
    notepad "%ENV_PATH%"
    popd & exit /b 0
)

rem ---- launch --------------------------------------------------------------
rem Titled windows so stop.bat can find + close them.
start "emrb-worker" cmd /k ""%ROOT%.venv\Scripts\emrb-worker.exe""
rem Tiny gap so the worker's PID file is acquired before the web process checks it.
timeout /t 2 /nobreak >nul

start "emrb-web" cmd /k ""%ROOT%.venv\Scripts\emrb-web.exe""
timeout /t 3 /nobreak >nul

start "" "http://127.0.0.1:8766"

echo.
echo =====================================================================
echo  Equity Momentum Rebalance is running.
echo  - UI: http://127.0.0.1:8766
echo  - Stop: run stop.bat (or close the emrb-worker and emrb-web windows)
echo =====================================================================
echo.
popd
endlocal
