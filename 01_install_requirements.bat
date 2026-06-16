@echo off
setlocal
cd /d "%~dp0"
echo Installing NavelMaker requirements into the global Python environment...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Installation failed. Check the messages above.
) else (
    echo.
    echo Installation completed successfully.
)
pause
