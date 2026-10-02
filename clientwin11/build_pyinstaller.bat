@echo off
setlocal
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onedir --console --name LegalyzeWin11 --icon icon.ico --add-data "data/DejaVuSans.ttf;data" main.py
if errorlevel 1 exit /b 1
echo Copy your portable chromium folder into dist\LegalyzeWin11\chromium before running.
