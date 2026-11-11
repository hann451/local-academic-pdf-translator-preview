@echo off
cd /d "%~dp0"
wscript.exe //nologo "%~dp0scripts\run_silent.vbs" -UseCpuInference %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto :failed
exit /b 0

:failed
echo.
echo [ERROR] Failed to start PaperBridge Local in CPU mode. Exit code: %EXIT_CODE%
echo Try running diagnostic mode console batch file for details.
pause
exit /b %EXIT_CODE%
