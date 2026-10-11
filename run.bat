@echo off
REM Run the uploader from source (dev). Creates the venv on first run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo First run: creating virtual environment and installing dependencies...
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :err
)

REM pythonw = no console window. Pass any args through (e.g. run.bat --once).
start "" ".venv\Scripts\pythonw.exe" run_app.py %*
goto :eof

:err
echo Setup failed.
exit /b 1
