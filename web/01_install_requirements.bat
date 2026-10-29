@echo off
setlocal
cd /d "%~dp0"

echo Creating local Python environment...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo Existing environment is broken or belongs to a removed Python installation.
        echo Rebuilding .venv...
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :failed
)

echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

echo Installing API and NavelMaker requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Installation completed.
pause
exit /b 0

:failed
echo.
echo Installation failed. Confirm that Python 3.10+ is installed.
pause
exit /b 1
