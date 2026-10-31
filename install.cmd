@echo off
cd /d "%~dp0"
chcp 65001 >nul
python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist .env copy /y .env.example .env >nul
echo Installed. Next: configure.cmd
pause
exit /b 0
:failed
echo Installation failed. Python 3.11+ and internet access are required.
pause
exit /b 1
