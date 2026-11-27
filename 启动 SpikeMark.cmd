@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python.exe"
"%PYTHON%" -B editor.py --open
if errorlevel 1 pause
