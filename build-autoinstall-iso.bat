@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PS_CMD=powershell.exe"
rem Environment settings, including NOTIFY_TOPIC, MIHOMO_IMAGE, OFFLINE_BUNDLE_REFRESH,
rem and OFFLINE_BUNDLE_CACHE, are inherited by PowerShell.

where pwsh.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set "PS_CMD=pwsh.exe"
)

"%PS_CMD%" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build-autoinstall-iso.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE% neq 0 (
    echo.
    echo [ERROR] Build failed with exit code %EXIT_CODE%.
    if "%CI%"=="" pause
)

exit /b %EXIT_CODE%
