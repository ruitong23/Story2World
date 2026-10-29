@echo off
setlocal
cd /d "%~dp0"

if not exist "node_modules" (
    echo Dependencies are missing. Run 01_install_frontend.bat first.
    pause
    exit /b 1
)

echo Starting Story2World frontend...
echo Open: http://localhost:5173
call npm.cmd run dev
if errorlevel 1 pause
