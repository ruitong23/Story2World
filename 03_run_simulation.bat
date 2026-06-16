@echo off
setlocal
cd /d "%~dp0"
python simulation_ui.py
if errorlevel 1 pause
