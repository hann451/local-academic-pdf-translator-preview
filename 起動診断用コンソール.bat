@echo off
cd /d "%~dp0"
echo [PaperBridge Local] Starting diagnostic console mode...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launcher.ps1" -ConsoleDiagnostic %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [Exit code: %EXIT_CODE%] Check logs\launcher.log under your PaperBridge UserDataDir.
pause
exit /b %EXIT_CODE%
