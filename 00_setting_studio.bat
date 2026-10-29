@echo off
setlocal
cd /d "%~dp0"
python scenario_studio.py
if errorlevel 1 pause
