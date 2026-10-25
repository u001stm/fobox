@echo off
chcp 65001 >nul
title LUMIO
rem Run from the folder of this file, wherever it was unpacked
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите Python 3.11+ с python.org ^(галочка "Add python.exe to PATH"^) и запустите снова.
  pause
  exit /b 1
)
py -3 -c "import PIL" 2>nul || (
  echo Первый запуск: устанавливаю Pillow для проверки качества...
  py -3 -m pip install --quiet --disable-pip-version-check pillow
)
py -3 lumio.py %*
if errorlevel 1 pause
