@echo off
setlocal
cd /d "%~dp0"
python prepare_ui.py
if errorlevel 1 pause
