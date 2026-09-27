@echo off
chcp 65001 > nul
echo ============================================================================
echo   Сборка диагностического инструмента для Windows 11 (Nuitka)
echo ============================================================================
echo.

REM Установка зависимостей
pip install --upgrade pip
pip install nuitka pyqt6

echo.
echo Запуск компиляции Nuitka (с открытой консолью)...
python -m nuitka --onefile --windows-console-mode=force --enable-plugin=pyqt6 --assume-yes-for-downloads --output-dir=build --output-filename=Legalyze_Diagnostic.exe diagnose_win11.py

echo.
echo ============================================================================
echo   Сборка завершена!
echo   Исполняемый файл: build\Legalyze_Diagnostic.exe
echo   Скопируйте Legalyze_Diagnostic.exe в папку с папкой «chromium» и запустите.
echo ============================================================================
pause
