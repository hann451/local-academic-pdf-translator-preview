@echo off
cd /d "%~dp0"
echo [PaperBridge Local] Starting first-time setup wizard...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_wizard.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :failed
exit /b 0

:failed
echo.
echo [ERROR] Setup did not complete. Exit code: %EXIT_CODE%
echo Check the error dialog or logs\setup.log under your PaperBridge UserDataDir.
pause
exit /b %EXIT_CODE%
