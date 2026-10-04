@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"

"%PYTHON%" -c "import sys, struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Build on 64-bit Windows with 64-bit Python'"
if errorlevel 1 exit /b 1
"%PYTHON%" -m nuitka --version
if errorlevel 1 (
    echo Install requirements-build.txt first. See README.md.
    exit /b 1
)

rem Onefile distribution: Legalyze.exe plus the FULL external chromium folder.
rem No administrator manifest, no sandbox changes, no runtime algorithm changes.
"%PYTHON%" -m nuitka ^
    --onefile ^
    --msvc=latest ^
    --enable-plugin=pyqt6 ^
    --windows-console-mode=disable ^
    --windows-icon-from-ico=icon.ico ^
    --include-data-files=data/DejaVuSans.ttf=data/DejaVuSans.ttf ^
    --include-data-files=data/FONT-LICENSE.txt=data/FONT-LICENSE.txt ^
    --output-dir=build-onefile ^
    --output-filename=Legalyze.exe ^
    --report=build-onefile/compilation-report.xml ^
    main.py
if errorlevel 1 exit /b 1
if not exist "build-onefile\Legalyze.exe" (
    echo Expected output build-onefile\Legalyze.exe was not found. Check the Nuitka output.
    exit /b 1
)

rem Chromium is an external portable runtime, not a Python dependency.
if exist "chromium\chrome.exe" goto copy_chromium
if exist "chromium\chromium.exe" goto copy_chromium
echo EXE built. Before distribution add the FULL portable chromium folder
echo to build-onefile\chromium. Use the same Chromium tested on Win10 and Win11.
goto done

:copy_chromium
robocopy "chromium" "build-onefile\chromium" /E /R:1 /W:1 /NFL /NDL /NJH /NJS
if errorlevel 8 (
    echo Chromium copy failed. Do not distribute this incomplete folder.
    exit /b 1
)

:done
echo Output: %~dp0build-onefile\Legalyze.exe
echo Distribute Legalyze.exe and chromium together. Test on Windows 10 and Windows 11.
exit /b 0
