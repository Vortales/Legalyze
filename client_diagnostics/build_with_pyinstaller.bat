@echo off
chcp 65001 > nul
echo ============================================================================
echo   Сборка диагностического инструмента для Windows 11 (PyInstaller)
echo ============================================================================
echo.

REM Установка зависимостей
pip install --upgrade pip
pip install pyinstaller pyqt6

echo.
echo Запуск компиляции PyInstaller (с включенной консолью для просмотра логов)...
pyinstaller --onefile --console --name=Legalyze_Diagnostic diagnose_win11.py

echo.
echo ============================================================================
echo   Сборка завершена!
echo   Исполняемый файл: dist\Legalyze_Diagnostic.exe
echo   Скопируйте Legalyze_Diagnostic.exe в папку, где лежит папка «chromium»,
echo   и запустите его двойным кликом на проблемном компьютере с Windows 11.
echo ============================================================================
pause
