@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 lumio.py --port 8776 --db "%USERPROFILE%\.lumio-unit-alpha01\lumio.sqlite3"
if errorlevel 1 pause
