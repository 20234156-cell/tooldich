@echo off
REM ==========================================================
REM  DuanLayLink - Khoi dong REST API + Web Dashboard
REM  Web UI:   http://127.0.0.1:8088/
REM  API docs: http://127.0.0.1:8088/docs
REM  Doi cong/dia chi: start_server.bat 0.0.0.0 9000
REM ==========================================================
setlocal
chcp 65001 >nul
title DuanLayLink Server

REM Luon chay tu thu muc chua file bat (de .env, storage/ dung duong dan)
cd /d "%~dp0"

set "HOST=%~1"
if "%HOST%"=="" set "HOST=127.0.0.1"
set "PORT=%~2"
if "%PORT%"=="" set "PORT=8088"

REM Uu tien moi truong ao neu co, khong thi dung python he thong
set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Khong tim thay Python. Hay cai Python 3.12+ va them vao PATH.
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        echo [CANH BAO] Chua co .env - tao tu .env.example. Nho dien khoa API.
        copy ".env.example" ".env" >nul
    )
)

REM Kiem tra thu vien, thieu thi cai tu requirements.txt
"%PY%" -c "import fastapi, uvicorn, typer, sqlalchemy, pydantic_settings" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Dang cai thu vien tu requirements.txt ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [LOI] Cai thu vien that bai.
        pause
        exit /b 1
    )
)

REM Kiem tra va giai phong cong neu co tien trinh dang chiem
"%PY%" -c "import socket; s = socket.socket(); s.bind(('%HOST%', int('%PORT%'))); s.close()" >nul 2>&1
if errorlevel 1 (
    echo [CANH BAO] Cong %PORT% dang bi chiem boi mot tien trinh khac.
    set /p "CONFIRM=Ban co muon giai phong cong %PORT% de tiep tuc? (Y/N, mac dinh Y): "
    if /i "%CONFIRM%"=="N" (
        echo [INFO] Da huy khoi dong server. Vui long chon cong khac: start_server.bat %HOST% 9000
        pause
        exit /b 1
    )
    echo [THONG BAO] Dang giai phong cong %PORT%...
    "%PY%" -c "import subprocess, re; res = subprocess.run(['netstat', '-ano'], capture_output=True, text=True); pids = [l.split()[-1] for l in res.stdout.splitlines() if 'LISTENING' in l and re.search(r':%PORT%\b', l)]; [subprocess.run(['taskkill', '/F', '/PID', p], capture_output=True) for p in set(pids)]" >nul 2>&1
    timeout /t 1 /nobreak >nul
)

REM Mo trinh duyet sau vai giay, khi server da san sang
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%PORT%/"

"%PY%" main.py serve --host %HOST% --port %PORT%

echo.
echo Server da dung.
pause
endlocal
