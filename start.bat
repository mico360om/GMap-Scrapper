@echo off
setlocal
cd /d "%~dp0"

REM ---- Auto-update source (public GitHub repo). The Settings dialog can
REM ---- override these at runtime; they are the built-in defaults.
set GITHUB_REPO=mico360om/GMap-Scrapper
set GITHUB_BRANCH=main

REM =====================================================================
REM  IMPORTANT: this script never trusts a bare `python`/`pip` from PATH.
REM  On many machines PATH resolves `python` to the Microsoft Store stub,
REM  or to an unrelated Python 2 bundled with other software (e.g. BioTime),
REM  whose broken pip then fails the install. We create the venv with the
REM  `py -3` launcher and then call the venv's own python.exe by absolute
REM  path for every step after that.
REM =====================================================================

if exist ".venv\Scripts\python.exe" goto :have_venv

echo Creating virtual environment...
set "PYLAUNCH="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=py -3"
if defined PYLAUNCH goto :make_venv
python -c "import sys;raise SystemExit(0 if sys.version_info[0]>=3 else 1)" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=python"
if defined PYLAUNCH goto :make_venv
echo [ERROR] Could not find Python 3. Install Python 3.10+ from
echo         https://www.python.org/downloads/ and tick "Add to PATH".
pause
exit /b 1

:make_venv
%PYLAUNCH% -m venv .venv
if errorlevel 1 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )
if not exist ".venv\Scripts\python.exe" ( echo [ERROR] venv creation produced no python.exe. & pause & exit /b 1 )

:have_venv
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

REM ---- Offline / bundled packages (optional): if you drop wheel files into
REM ---- a "vendor\wheels" folder next to this script, pip installs from them
REM ---- (pip reads PIP_FIND_LINKS automatically), so the tool can set up with
REM ---- no internet. If the folder is absent, packages come from PyPI.
if exist "%~dp0vendor\wheels" set "PIP_FIND_LINKS=%~dp0vendor\wheels"

echo Installing / updating Python packages (idempotent - auto-heals anything missing)...
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

echo Ensuring the browser engine is installed (idempotent)...
"%VENV_PY%" -m playwright install chromium
if errorlevel 1 ( echo [ERROR] Playwright browser install failed. & pause & exit /b 1 )

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
"%VENV_PY%" -m uvicorn app:app --host 127.0.0.1 --port 8000
