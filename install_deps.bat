@echo off
REM install_deps.bat — Install Python dependencies for dnstt-balancer
REM Tries pip install from the internet first; falls back to local vendor\ wheels.

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "REQ=%SCRIPT_DIR%requirements.txt"
set "VENDOR=%SCRIPT_DIR%vendor"

REM ── Locate Python ──────────────────────────────────────────────────────────
set "PYTHON="
where python3 >nul 2>&1 && set "PYTHON=python3" && goto :found_python
where python  >nul 2>&1 && set "PYTHON=python"  && goto :found_python
where py      >nul 2>&1 && set "PYTHON=py -3"    && goto :found_python

echo [ERROR] Python 3 is not installed or not in PATH.
echo         Please install Python 3.8+ and try again.
exit /b 1

:found_python
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>^&1') do set "PY_VER=%%v"
echo [*] Found: %PY_VER% (%PYTHON%)

REM ── Check if requirements.txt exists and has real packages ─────────────────
if not exist "%REQ%" (
    echo [*] No requirements.txt found — nothing to install.
    echo [OK] dnstt-balancer uses only the Python standard library.
    exit /b 0
)

set "HAS_DEPS=0"
for /f "usebackq tokens=*" %%L in ("%REQ%") do (
    set "LINE=%%L"
    REM Skip blank lines and comments
    if not "!LINE!"=="" (
        if not "!LINE:~0,1!"=="#" (
            set "HAS_DEPS=1"
        )
    )
)

if "%HAS_DEPS%"=="0" (
    echo [*] requirements.txt has no dependencies listed.
    echo [OK] dnstt-balancer uses only the Python standard library.
    exit /b 0
)

REM ── Try online install ─────────────────────────────────────────────────────
echo [*] Attempting online install via pip...
%PYTHON% -m pip install -r "%REQ%" 2>nul
if %ERRORLEVEL%==0 (
    echo [OK] Dependencies installed from the internet.
    exit /b 0
)

echo [!] Online install failed (no internet?). Trying local vendor\ wheels...

REM ── Fallback: offline install from vendor\ ─────────────────────────────────
if not exist "%VENDOR%\*.whl" (
    echo [ERROR] No .whl files found in %VENDOR%\
    echo         To prepare offline packages, run on a machine with internet:
    echo.
    echo           pip download -r requirements.txt -d vendor\ --only-binary=:all:
    echo.
    exit /b 1
)

echo [*] Installing from %VENDOR%\ ...
%PYTHON% -m pip install --no-index --find-links "%VENDOR%" -r "%REQ%"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Offline install failed.
    exit /b 1
)

echo [OK] Dependencies installed from local vendor\ wheels.
exit /b 0
