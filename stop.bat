@echo off
rem Graceful stop for the Equity Momentum Rebalance app (Windows).
rem First attempt: close titled console windows (runs shutdown handlers).
rem Fallback: kill by image name + known script names.
rem See docs/FRD.md B.10 (shutdown sequence).
setlocal enableextensions

echo Stopping Equity Momentum Rebalance...

rem --- polite stop via window title ------------------------------------
taskkill /FI "WINDOWTITLE eq emrb-worker*" /T >nul 2>&1
taskkill /FI "WINDOWTITLE eq emrb-web*"    /T >nul 2>&1

rem Give shutdown handlers a chance to delete PID + lock files.
timeout /t 2 /nobreak >nul

rem --- hard-kill fallback ----------------------------------------------
rem Kill the console script launchers directly (both names; harmless if absent).
taskkill /F /IM emrb-worker.exe >nul 2>&1
taskkill /F /IM emrb-web.exe    >nul 2>&1

rem Some installs run the module via python.exe. Kill only processes whose
rem command line includes our module path, so unrelated python processes
rem are left alone.
for /f "tokens=2 delims==" %%P in ('wmic process where "commandline like '%%app.worker.main%%' and name='python.exe'" get processid /format:value 2^>nul ^| findstr "="') do taskkill /F /PID %%P >nul 2>&1
for /f "tokens=2 delims==" %%P in ('wmic process where "commandline like '%%app.web.main%%' and name='python.exe'"    get processid /format:value 2^>nul ^| findstr "="') do taskkill /F /PID %%P >nul 2>&1

rem Any stale PID files left behind will be detected and cleaned by the
rem next run.bat (FRD B.10).
echo Done. If %USERPROFILE%\.claude-equity-momentum\run\ still has .pid or
echo .lock files, they will be cleaned automatically on next run.bat.
echo.
endlocal
