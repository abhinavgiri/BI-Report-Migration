@echo off
setlocal EnableExtensions
title BI Migration Assistant
cd /d "%~dp0"

echo.
echo  ==================================================
echo    BI Migration Assistant  -  Power BI to Tableau
echo  ==================================================
echo.

rem ---- 1. find Python (3.10 or newer) ----------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo  [!] Python was not found on this computer.
  echo      Install Python 3.10 or newer from https://www.python.org/downloads/
  echo      and tick "Add python.exe to PATH" during setup, then run this file again.
  echo.
  pause
  exit /b 1
)

rem ---- 2. private environment (created once) ---------------------------------
if not exist ".venv\Scripts\python.exe" (
  echo  [1/3] Creating a private Python environment in .venv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo  [!] Could not create the environment. Is Python 3.10+ installed correctly?
    pause
    exit /b 1
  )
) else (
  echo  [1/3] Environment found.
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3. libraries (installed automatically when requirements.txt changes) --
set "REQHASH="
for /f "skip=1 tokens=1" %%H in ('certutil -hashfile requirements.txt SHA256') do (
  if not defined REQHASH set "REQHASH=%%H"
)
set "STAMP=.venv\.requirements.sha256"
set "OLDHASH="
if exist "%STAMP%" set /p OLDHASH=<"%STAMP%"
if /i not "%OLDHASH%"=="%REQHASH%" (
  echo  [2/3] Installing libraries - this takes a minute the first time ...
  "%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
  "%VPY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
  if errorlevel 1 (
    echo  [!] Library installation failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
  >"%STAMP%" echo %REQHASH%
) else (
  echo  [2/3] Libraries are up to date.
)

rem ---- 4. run ------------------------------------------------------------------
echo  [3/3] Starting the app ... your browser will open at http://127.0.0.1:8765
echo        Keep this window open while you use the app. Close it to stop.
echo.
"%VPY%" -m src.web --open --port 8765
if errorlevel 1 (
  echo.
  echo  [!] The app stopped with an error - see the messages above.
  pause
)
endlocal
