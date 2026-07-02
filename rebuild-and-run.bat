@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
cd /d "%ROOT%"

set "EXE_NAME=smsFetcher.exe"
set "EXE_PATH=%ROOT%%EXE_NAME%"
set "BUILD_SCRIPT=%ROOT%build.py"
set "BUILD_LOG=%TEMP%\smsFetcher-build.log"

rem Gracefully stop the currently running executable, then wait for it to exit.
set "PID="
for /f %%I in ('powershell -NoProfile -Command "Get-Process -Name smsFetcher -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id -ErrorAction SilentlyContinue | Select-Object -First 1" 2^>nul') do if not "%%I"=="" set "PID=%%I"

if defined PID (
    powershell -NoProfile -Command "Get-Process -Id %PID% -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() | Out-Null }" >nul 2>&1
    for /l %%N in (1,1,10) do (
        tasklist /FI "PID eq %PID%" /NH | findstr /I "%PID%" >nul
        if errorlevel 1 goto build
        timeout /t 1 /nobreak >nul
    )
    taskkill /F /IM "%EXE_NAME%" /T >nul 2>&1
)

:build
if not exist "%BUILD_SCRIPT%" (
    echo Build script not found: %BUILD_SCRIPT%
    pause
    exit /b 1
)

if exist "%BUILD_LOG%" del /f /q "%BUILD_LOG%" >nul 2>&1

where python >nul 2>&1
if not errorlevel 1 (
    python "%BUILD_SCRIPT%" >"%BUILD_LOG%" 2>&1
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 "%BUILD_SCRIPT%" >"%BUILD_LOG%" 2>&1
    ) else (
        echo Python was not found on PATH.
        pause
        exit /b 1
    )
)

if errorlevel 1 (
    echo Build failed.
    if exist "%BUILD_LOG%" type "%BUILD_LOG%"
    pause
    exit /b 1
)

rem Wait until the rebuilt executable exists at the project root.
for /l %%N in (1,1,60) do (
    if exist "%EXE_PATH%" goto run_exe
    timeout /t 1 /nobreak >nul
)

echo Timed out waiting for %EXE_PATH%
pause
exit /b 1

:run_exe
start "" "%EXE_PATH%"
if errorlevel 1 (
    echo Failed to launch %EXE_PATH%
    pause
    exit /b 1
)

exit /b 0
