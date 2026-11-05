@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Instala Python desde https://www.python.org/downloads/ marcando "Add python.exe to PATH" & pause & exit /b 1)
python -m pip install -q -r requirements.txt
python -m playwright install chromium
if "%~1"=="" (set CSV=informes.csv) else (set CSV=%~1)
python descargar_informes.py "%CSV%" %2 %3 %4 %5
pause
