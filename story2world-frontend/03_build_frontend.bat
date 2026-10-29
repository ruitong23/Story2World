@echo off
setlocal
cd /d "%~dp0"

if not exist "node_modules" (
    echo Dependencies are missing. Run 01_install_frontend.bat first.
    pause
    exit /b 1
)

echo Building production files...
call npm.cmd run build
if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build completed. Upload the contents of dist\ to your web server.
pause
