@echo off
rem Graceful stop for the Equity Momentum Rebalance app (Windows).
rem Closes the worker and web console windows by title, which fires Ctrl-C /
rem window close and lets each process run its shutdown handler:
rem - worker: stops scheduler, closes Dhan, releases run\worker.pid
rem - web: releases run\web.pid
rem See docs/FRD.md B.10 (shutdown sequence).
setlocal

echo Stopping Equity Momentum Rebalance...

rem /T terminates the child process tree; /FI filters by window title.
taskkill /FI "WINDOWTITLE eq emrb-worker*" /T >nul 2>&1
taskkill /FI "WINDOWTITLE eq emrb-web*"    /T >nul 2>&1

echo If any windows remain open, close them manually.
echo If a PID file is left behind after a crash, the next run.bat will
echo detect and clean it on startup (see FRD B.10).
echo.
endlocal
