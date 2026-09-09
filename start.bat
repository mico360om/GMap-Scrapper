@echo off
setlocal
cd /d "%~dp0"

REM ---- Auto-update source (public GitHub repo). The Settings dialog can
REM ---- override these at runtime; they are the built-in defaults.
set GITHUB_REPO=mico360om/GMap-Scrapper
set GITHUB_BRANCH=main

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is not installed or not on PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

REM Detect the Windows Store stub which opens the Store on `python` calls
python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] `python` runs but does not work. You may have the Windows Store stub.
  echo Install a real Python from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )
  set FIRST_RUN=1
)

call .venv\Scripts\activate.bat

echo Syncing Python packages (idempotent)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

if defined FIRST_RUN (
  echo Installing Playwright Chromium ^(one-time, ~150MB^)...
  python -m playwright install chromium
  if errorlevel 1 ( echo [ERROR] playwright install failed. & pause & exit /b 1 )
)

echo.
echo ================================================
echo   Google Maps Scraper running at:
echo   http://127.0.0.1:8000
echo   Press Ctrl+C to stop.
echo ================================================
echo.

REM NOTE: do NOT add --reload here. On Windows, uvicorn's reload mode runs the
REM worker on a SelectorEventLoop, which cannot launch Playwright's Chromium
REM subprocess (raises NotImplementedError at startup). The default (no reload)
REM uses the ProactorEventLoop, which works. After editing files, just stop and
REM re-run this script.
start "" http://127.0.0.1:8000
python -m uvicorn app:app --host 127.0.0.1 --port 8000
