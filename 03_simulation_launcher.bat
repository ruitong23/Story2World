@echo off
setlocal
cd /d "%~dp0"
python simulation_project_launcher.py
if errorlevel 1 pause
