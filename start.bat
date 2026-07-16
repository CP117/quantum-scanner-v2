@echo off
REM ============================================================
REM  Market Refinement Dashboard - Windows automated startup
REM ============================================================
REM  What this script does:
REM    1. Finds a Python 3.11+ interpreter
REM    2. Creates a virtual environment in .\venv (first run only)
REM    3. Installs all required packages on first run
REM    4. Downloads cloudflared.exe on first run (off-network access)
REM    5. Launches the FastAPI backend on http://localhost:8001
REM    6. Launches Cloudflare Quick Tunnel for a public https URL
REM    7. Prints the URLs (local, LAN, public) and opens the local one
REM
REM  Usage:  double-click start.bat
REM  Stop :  press Ctrl+C in the window that appears (kills both
REM          uvicorn and cloudflared cleanly)
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
title Market Refinement Dashboard

echo.
echo ============================================================
echo  Market Refinement Dashboard - starting up
echo ============================================================
echo.

REM ---- 1) Locate Python -----------------------------------------
set "PYEXE="
call :find_py py     -3
if not defined PYEXE call :find_py python
if not defined PYEXE call :find_py python3
if not defined PYEXE call :find_py py

if not defined PYEXE (
    echo [ERROR] Python 3.11 or newer was not found on PATH.
    echo.
    echo         Install Python from https://www.python.org/downloads/
    echo         IMPORTANT: tick the box "Add Python to PATH" during install.
    echo.
    echo         After installing, close this window and double-click
    echo         start.bat again.
    echo.
    pause
    exit /b 1
)
echo [ok] Python interpreter located.
echo.

REM ---- 2) Create venv if missing --------------------------------
if not exist ".\venv\Scripts\python.exe" (
    echo [info] Creating virtual environment in .\venv ...
    %PYEXE% -m venv venv
    if errorlevel 1 goto venv_failed
)
set "VENV_PY=%~dp0venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto venv_failed
echo [ok] Virtual environment ready.
echo.

REM ---- 3) Install dependencies on first run ---------------------
if not exist ".deps_installed" (
    echo [info] Installing Python dependencies on first run.
    echo        This takes a few minutes the very first time only.
    echo.
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto pip_failed
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto pip_failed
    echo done > ".deps_installed"
    echo [ok] Dependencies installed.
    echo.
) else (
    echo [ok] Dependencies already installed.
    echo      To force a fresh install delete the file .deps_installed
    echo.
)

REM ---- 4) Make sure cloudflared.exe is available ----------------
REM       cloudflared = Cloudflare's official tunnel binary. Free,
REM       no account required, gives us an anonymous public https://
REM       URL that proxies straight to the local backend so users
REM       on OTHER networks can connect using the URL we print below.
if not exist "cloudflared.exe" (
    echo [info] Downloading cloudflared.exe ^(~30 MB, one-time only^).
    echo        This is Cloudflare's official tunnel client and
    echo        powers the public URL that lets devices on OTHER
    echo        networks connect.
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ProgressPreference='SilentlyContinue';" ^
      "Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile 'cloudflared.exe'"
    if errorlevel 1 (
        echo [WARN] cloudflared download failed. The app will still run
        echo        locally and on the LAN, just without a public URL.
        echo        Re-run start.bat once you have internet access to
        echo        download it automatically.
    ) else (
        echo [ok] cloudflared.exe downloaded.
    )
    echo.
) else (
    echo [ok] cloudflared.exe already present.
    echo.
)

REM ---- 5) Pick a free port (default 8001, fallback 8011) --------
set "PORT=8001"
netstat -ano | findstr "LISTENING" | findstr ":%PORT% " >nul 2>&1
if not errorlevel 1 (
    echo [warn] Port %PORT% is already in use, trying 8011 instead.
    set "PORT=8011"
)

REM ---- 6) Schedule a delayed browser open in a separate process -
set "OPENER=%TEMP%\mrd_open_browser.bat"
> "%OPENER%" echo @echo off
>> "%OPENER%" echo ping -n 6 127.0.0.1 ^>nul
>> "%OPENER%" echo start "" http://localhost:%PORT%/ui
start "" /b cmd /c "%OPENER%"

REM ---- 6b) Launch cloudflared in the background, capture URL ----
set "CFD_LOG=%TEMP%\mrd_cloudflared.log"
del "%CFD_LOG%" >nul 2>&1
set "CFD_RUNNING="
set "PUBLIC_URL="

REM Phase 26.5: persist the captured public URL + LAN URLs so the dashboard
REM can show them in the header. Wipe the file on every launch so a stale
REM URL from a previous session can't mislead users.
set "PUBLIC_URL_FILE=app\data\public_url.txt"
if not exist "app\data" mkdir "app\data" >nul 2>&1
type nul > "%PUBLIC_URL_FILE%"

if exist "cloudflared.exe" (
    REM Use --no-autoupdate so cloudflared doesn't write to Program Files.
    REM Write logs to TEMP so we can grep the public URL out shortly.
    start "MRD-Cloudflared" /b cmd /c ".\cloudflared.exe --no-autoupdate tunnel --url http://localhost:%PORT% > ""%CFD_LOG%"" 2>&1"
    set "CFD_RUNNING=1"
    REM Wait up to 12 seconds for cloudflared to print its trycloudflare URL.
    REM We use a PowerShell one-liner here instead of nested cmd.exe FOR loops
    REM because nested-FOR + delayedexpansion on cloudflared's box-drawing
    REM log output reliably trips "The syntax of the command is incorrect"
    REM warnings (purely cosmetic but ugly).  PowerShell's -match operator
    REM handles unicode chars cleanly and only echoes the captured URL.
    for /l %%I in (1,1,12) do (
        if "!PUBLIC_URL!"=="" (
            ping -n 2 127.0.0.1 >nul
            for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command "$m = Select-String -Path '%CFD_LOG%' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue ^| Select-Object -First 1; if ($m) { $m.Matches[0].Value }"`) do (
                set "PUBLIC_URL=%%U"
            )
        )
    )
)

REM ---- 6c) Persist URL + LAN URLs to app\data\public_url.txt -----
REM Build the file synchronously (one URL per line, no /ui suffix) so the
REM backend's /api/public-url endpoint has it ready before the dashboard
REM loads. Public URL first, then LAN URLs, then localhost.
REM
REM Guard: require the captured value to actually START with "https://"
REM before writing it.  Previously the guard was `if not ""=="" ...` which
REM the PowerShell capture could accidentally pass with a whitespace-only
REM string, causing cmd.exe to emit the literal text "ECHO is off." into
REM the URL file (bare `echo` with no visible args prints the state of
REM echo mode).  That polluted the frontend banner AND caused the backend
REM to think a URL had been published when in fact none had.
if "!PUBLIC_URL:~0,8!"=="https://" (
    >>"%PUBLIC_URL_FILE%" echo !PUBLIC_URL!
)
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /R /C:"IPv4 Address"') do (
    set "_IP=%%A"
    set "_IP=!_IP: =!"
    if not "!_IP!"=="127.0.0.1" if not "!_IP:~0,7!"=="169.254" (
        >>"%PUBLIC_URL_FILE%" echo http://!_IP!:%PORT%
    )
)
>>"%PUBLIC_URL_FILE%" echo http://localhost:%PORT%

REM ---- 6d) Background watcher for late-arriving cloudflared URL --
REM If cloudflared hasn't printed its URL by the 12s deadline, keep
REM watching the log for up to 3 minutes and rewrite public_url.txt the
REM moment it appears so the frontend banner picks it up automatically.
REM
REM Earlier versions of this script tried to inline the PowerShell loop
REM directly inside `cmd /c "powershell -Command ^"...^""`. cmd.exe's
REM tokenizer mis-parses the `{` immediately after `for(...)`, producing
REM the famous "Start-Sleep was unexpected at this time" error. The fix
REM is to ship the watcher as a standalone .ps1 file with normal quoting
REM and invoke it with `-File`.
REM
REM Guard: only launch the watcher if we ALSO have no captured URL AND
REM the standalone `tunnel_watcher.ps1` is present.  Skip silently if
REM the .ps1 was stripped from the archive — the backend's own
REM `ensure_public_url_on_startup()` hook will still spawn cloudflared
REM as a fallback so the user isn't left without a public URL.
if defined CFD_RUNNING (
    if "!PUBLIC_URL:~0,8!" NEQ "https://" (
        if exist "tunnel_watcher.ps1" (
            start "MRD-PublicURLWatcher" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "tunnel_watcher.ps1" "%CFD_LOG%" "%PUBLIC_URL_FILE%"
        )
    )
)

REM ---- 7) Discover LAN IPv4 address(es) -------------------------
echo.
echo ============================================================
echo  Backend launching on:
echo    http://localhost:%PORT%/ui          ^(this machine only^)
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /R /C:"IPv4 Address"') do (
    set "_IP=%%A"
    set "_IP=!_IP: =!"
    if not "!_IP!"=="127.0.0.1" if not "!_IP:~0,7!"=="169.254" (
        echo    http://!_IP!:%PORT%/ui    ^(any device on this LAN^)
    )
)
echo.
if defined PUBLIC_URL (
    if "!PUBLIC_URL:~0,8!"=="https://" (
        echo  PUBLIC URL ^(works from ANY network -- share with anyone^):
        echo    !PUBLIC_URL!/ui
        echo.
        echo    ^* this URL is generated fresh every time you start the
        echo      app. It only works while this window stays open.
        echo    ^* anyone with the URL can reach the dashboard, so don't
        echo      paste it in public chats if you only want one or two
        echo      people to see it.
    ) else (
        echo  PUBLIC URL: still negotiating with Cloudflare ^(may take
        echo    a few more seconds^). Check %CFD_LOG% if it doesn't
        echo    show up -- your firewall may be blocking outbound TCP.
    )
) else (
    if defined CFD_RUNNING (
        echo  PUBLIC URL: still negotiating with Cloudflare ^(may take
        echo    a few more seconds^). Check %CFD_LOG% if it doesn't
        echo    show up -- your firewall may be blocking outbound TCP.
    ) else (
        echo  PUBLIC URL: not available ^(cloudflared.exe missing^).
        echo    Re-run start.bat once you have internet so it can
        echo    download cloudflared automatically.
    )
)
echo.
echo  Phones / tablets on the same WiFi can use any LAN URL above.
echo  Windows Firewall may prompt the first time another device
echo  connects -- click Allow once and it'll stop asking.
echo.
echo  Keep this window OPEN. Press Ctrl+C to stop the server.
echo ============================================================
echo.

REM ---- 8) Run uvicorn -------------------------------------------
set "PYTHONWARNINGS=ignore::FutureWarning,ignore::DeprecationWarning"
"%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
set "RC=%ERRORLEVEL%"

REM ---- 9) Clean up cloudflared when uvicorn exits ---------------
if defined CFD_RUNNING (
    echo.
    echo [info] Shutting down cloudflared tunnel...
    taskkill /F /IM cloudflared.exe >nul 2>&1
)
REM Phase 26.5: wipe the captured public-URL file so a stale URL from this
REM session can't mislead the user on the next launch.
if exist "%PUBLIC_URL_FILE%" type nul > "%PUBLIC_URL_FILE%" 2>nul

echo.
echo [info] Server exited with code %RC%.
pause
exit /b %RC%


REM =============================================================
REM  Helpers
REM =============================================================

:find_py
set "_TRY=%~1"
where %_TRY% >nul 2>&1
if errorlevel 1 goto :eof

%_TRY% %~2 -c "import sys,os;sys.exit(0 if sys.version_info>=(3,11) else 9)" >nul 2>&1
if errorlevel 1 goto :eof

if "%~2"=="" (
    set "PYEXE=%_TRY%"
) else (
    set "PYEXE=%_TRY% %~2"
)
goto :eof


:venv_failed
echo.
echo [ERROR] Failed to create the virtual environment.
echo         Your Python install may be missing the "venv" module.
echo         Re-install Python from python.org with default options.
echo.
pause
exit /b 1


:pip_failed
echo.
echo [ERROR] pip install failed. See messages above.
echo         Common causes:
echo           - no internet connection
echo           - corporate firewall blocking pypi.org
echo           - disk full
echo.
echo         You can retry by deleting the file .deps_installed
echo         and re-running start.bat.
echo.
pause
exit /b 1
