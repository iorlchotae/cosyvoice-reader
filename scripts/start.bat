@echo off
chcp 65001 >nul
title CosyVoice Reader
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

echo ============================================
echo   CosyVoice 3.0 Reader
echo   First start loads the model: 30-120 seconds.
echo   The browser opens automatically at
echo   http://127.0.0.1:7860
echo.
echo   Do NOT double-click the shortcut again:
echo   the server detects a running instance and
echo   just re-opens the browser.
echo ============================================
echo.

rem -u = unbuffered stdout: without it the console looks frozen for the
rem whole 30-120 s model load because Python buffers its output.
rem
rem Redirect to a log file. If stdout is a pipe nobody reads (the usual case when
rem the launcher process has exited), its buffer fills up and the NEXT write blocks
rem forever; the logging handler lock is held while blocked, so every thread that
rem logs afterwards deadlocks too. That silently froze a 2-document run once.
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" -u app.py >> "logs\reader.log" 2>&1
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="3" (
  echo [already running] Browser opened. Closing this window in 3 seconds...
  ping -n 4 127.0.0.1 >nul
  exit /b 0
)
if not "%RC%"=="0" (
  echo [start failed] exit code %RC%
  echo If the port is busy, run scripts\stop.bat first, then retry.
) else (
  echo Reader stopped.
)
echo.
pause
