# Комплексная диагностика запуска и встраивания Chromium (Windows 11)

Данный инструмент разработан для глубокого пошагового анализа проблем с запуском Chromium и встраиванием окна на **Windows 11**.

---

## Что проверяет диагностический скрипт:

1. **Система и переменные окружения:**
   - Версия сборки Windows 11, архитектура (64-bit), DPI-масштабирование.
   - Значения путей: `sys.argv[0]`, `sys.executable`, `NUITKA_ONEFILE_BINARY`, `TEMP`, `LOCALAPPDATA`.

2. **Поиск исполняемого файла Chromium:**
   - Поиск по всем возможным директориям (`_exe_dir()`, `CWD`, аргументы запуска).
   - Проверка размера файла и тестовый вызов `chrome.exe --version`.

3. **Запуск процесса и Chrome DevTools Protocol (CDP):**
   - Запуск с теми же флагами, что используются в `main.py`.
   - Проверка доступности портов `http://127.0.0.1:{port}/json/version` и `http://127.0.0.1:{port}/json/list`.
   - Проверка WebSocket-подключения и статуса вкладок.

4. **Полный аудит дерева процессов и окон Windows (EnumWindows):**
   - Сбор полного графа дочерних процессов (все уровни PID потомков).
   - Сканирование всех окон Windows с классом `Chrome_WidgetWin_1` и `Chrome_WidgetWin_0`.
   - Фиксация видимости (`IsWindowVisible`), реальных геометрических размеров (`GetWindowRect`), стилей `GWL_STYLE` и `GWL_EXSTYLE`.

5. **Симуляция встраивания в окно PyQt6 (SetParent):**
   - Создание тестового Qt-окна и попытка вызова `SetParent` для захвата окна Chromium.
   - Проверка кодов ошибок `GetLastError()`.
   - Демонстрация результата в течение 3 секунд.

6. **Проверка сетевого соединения:**
   - Проверка доступности нового HTTPS-сервера `https://legalyzeai.ru`.

---

## Как собрать и запустить на Windows 11:

### Способ 1: Сборка через PyInstaller (Рекомендуется)
Выполните в командной строке внутри папки `client_diagnostics`:
```cmd
pip install pyinstaller pyqt6
pyinstaller --onefile --console --name=Legalyze_Diagnostic diagnose_win11.py
```
Итоговый файл появится в `dist\Legalyze_Diagnostic.exe`.

### Способ 2: Сборка через Nuitka
```cmd
pip install nuitka pyqt6
python -m nuitka --onefile --windows-console-mode=force --enable-plugin=pyqt6 --assume-yes-for-downloads --output-dir=build --output-filename=Legalyze_Diagnostic.exe diagnose_win11.py
```
Итоговый файл появится в `build\Legalyze_Diagnostic.exe`.

### Способ 3: Запуск без сборки (через Python)
Если на Windows 11 установлен Python:
```cmd
pip install pyqt6
python diagnose_win11.py
```

---

## Запуск на проблемном компьютере:
1. Положите полученный `Legalyze_Diagnostic.exe` в ту же папку, где лежит папка `chromium`.
2. Запустите `Legalyze_Diagnostic.exe`.
3. Скрипт покажет все этапы в открывшемся окне консоли и создаст рядом файл **`win11_diagnostic_log.txt`**.
4. Пришлите содержимое файла **`win11_diagnostic_log.txt`** сюда для точного определения причины сбоя.
