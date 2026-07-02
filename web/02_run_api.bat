@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Local environment not found. Run 01_install_requirements.bat first.
    pause
    exit /b 1
)

echo Starting NavelMaker API at http://0.0.0.0:8000
echo API documentation: http://localhost:8000/docs
".venv\Scripts\python.exe" -m uvicorn server.main:app --host 0.0.0.0 --port 8000
if errorlevel 1 pause
