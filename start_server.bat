@echo off
setlocal
chcp 65001 >nul
title DuanLayLink Studio
cd /d "%~dp0"
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=%~1"
if "%PORT%"=="" set "PORT=9000"
"%PY%" scripts/run_personal.py --port "%PORT%"
if errorlevel 1 pause
endlocal
