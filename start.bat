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

REM Reuse an existing venv only if it actually RUNS. Its base Python can be
REM moved or removed (e.g. the Windows Python install manager updating a
REM runtime), which leaves the venv's python.exe pointing at a missing target.
if not exist ".venv\Scripts\python.exe" goto :setup_python
".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
if not errorlevel 1 goto :have_venv
echo Existing environment is broken (its Python went missing) - rebuilding...
rmdir /s /q ".venv" >nul 2>nul

:setup_python
echo Preparing Python environment...
set "PYLAUNCH="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=py -3"
if defined PYLAUNCH goto :make_venv
python -c "import sys;raise SystemExit(0 if sys.version_info[0]>=3 else 1)" >nul 2>nul
if not errorlevel 1 set "PYLAUNCH=python"
if defined PYLAUNCH goto :make_venv

REM ---- No system Python 3, or it can't build a working venv: fetch a private
REM ---- self-contained CPython into ".python" and use only that. Nothing is
REM ---- installed system-wide.
:bootstrap_python
set "USED_BOOTSTRAP=1"
set "PYDIR=%~dp0.python"
set "PYEXE=%PYDIR%\python\python.exe"
if exist "%PYEXE%" goto :make_venv_local
where tar >nul 2>nul
if errorlevel 1 (
  echo [ERROR] No Python 3 found and Windows 'tar' is unavailable.
  echo         Install Python 3.10+ from https://www.python.org/downloads/
  echo         ^(tick "Add to PATH"^) and run this again.
  pause & exit /b 1
)
echo No Python found on this PC - downloading a private copy ^(one-time, ~44 MB^)...
set "PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%%2B20260901-x86_64-pc-windows-msvc-install_only.tar.gz"
set "PY_TGZ=%TEMP%\gmaps-python.tar.gz"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri $env:PY_URL -OutFile $env:PY_TGZ } catch { Write-Host $_; exit 1 }"
if errorlevel 1 ( echo [ERROR] Could not download Python. Check your internet connection. & pause & exit /b 1 )
if not exist "%PYDIR%" mkdir "%PYDIR%"
echo Extracting Python...
tar -xf "%PY_TGZ%" -C "%PYDIR%"
if errorlevel 1 ( echo [ERROR] Could not extract Python. & pause & exit /b 1 )
del "%PY_TGZ%" >nul 2>nul
if not exist "%PYEXE%" ( echo [ERROR] Local Python missing after extract. & pause & exit /b 1 )
goto :make_venv_local

:make_venv
echo Creating virtual environment...
%PYLAUNCH% -m venv .venv
if errorlevel 1 ( echo [ERROR] Failed to create venv. & pause & exit /b 1 )
goto :check_venv

:make_venv_local
echo Creating virtual environment (using the private Python)...
"%PYEXE%" -m venv .venv
if errorlevel 1 ( echo [ERROR] Failed to create venv from the private Python. & pause & exit /b 1 )
goto :check_venv

:check_venv
if not exist ".venv\Scripts\python.exe" goto :venv_broken
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 goto :venv_broken
goto :have_venv

:venv_broken
if defined USED_BOOTSTRAP ( echo [ERROR] Could not build a working Python environment. Delete the .venv and .python folders, then run this again. & pause & exit /b 1 )
echo That Python could not build a working environment - switching to a private Python...
rmdir /s /q ".venv" >nul 2>nul
goto :bootstrap_python

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
