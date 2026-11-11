@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_desktop_shortcut.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :failed
exit /b 0

:failed
echo.
echo [ERROR] Failed to create desktop shortcut. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
