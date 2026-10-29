@echo off
setlocal
cd /d "%~dp0"

echo Installing Story2World frontend dependencies...
call npm.cmd install
if errorlevel 1 (
    echo.
    echo Installation failed. Check Node.js and network access.
    pause
    exit /b 1
)

echo.
echo Installation completed.
pause
