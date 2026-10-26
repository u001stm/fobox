@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .lab-runtime
if errorlevel 1 goto failed
.lab-runtime\Scripts\python.exe -m pip install -r lab-requirements.txt
if errorlevel 1 goto failed
echo RAW-движок установлен.
pause
exit /b 0
:failed
echo Установка остановлена. Рабочие среды не изменены.
pause
exit /b 1
