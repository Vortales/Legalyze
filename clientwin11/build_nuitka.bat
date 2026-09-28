@echo off
setlocal
cd /d "%~dp0"
python -m nuitka --mode=standalone --enable-plugin=pyqt6 --windows-console-mode=force --windows-icon-from-ico=icon.ico --include-data-files=data/DejaVuSans.ttf=data/DejaVuSans.ttf --output-dir=build --output-filename=LegalyzeWin11.exe main.py
if errorlevel 1 exit /b 1
echo Copy your portable chromium folder into build\main.dist\chromium before running.
