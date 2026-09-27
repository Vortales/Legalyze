#!/usr/bin/env python3
"""
Legalyze Windows 11 Deep Diagnostics & Simulation Tool
=====================================================
Комплексный инструмент диагностики запуска клиента и встраивания Chromium:
1. Анализ ОС, версии Windows, архитектуры, DPI-масштабирования и путей.
2. Поиск и запуск Chromium с флагами из main.py.
3. Проверка подключения к Chrome DevTools Protocol (CDP HTTP + WebSocket).
4. Детальный аудит дерева процессов (все PID) и окон Windows (EnumWindows).
5. Симуляция SetParent встраивания в окно PyQt6 и проверка ошибок WinAPI.
6. Проверка сетевого соединения с https://legalyzeai.ru.

Результат выводится в консоль и сохраняется в win11_diagnostic_log.txt.
"""

import os
import sys
import time
import json
import socket
import struct
import platform
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# Логирование
LOG_FILE = Path("win11_diagnostic_log.txt")
log_buffer = []


def log(msg: str = "", level: str = "INFO"):
    timestamp = time.strftime("%H:%M:%S")
    prefix = f"[{timestamp}] [{level}] " if level else ""
    line = f"{prefix}{msg}"
    print(line)
    log_buffer.append(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def section(title: str):
    bar = "=" * 78
    log(f"\n{bar}\n  {title}\n{bar}", level="")


# Windows ctypes & WinAPI
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None

class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None

if user32:
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long
    if hasattr(user32, "GetWindowLongPtrW"):
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND

if kernel32:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 1. СИСТЕМНАЯ ИНФОРМАЦИЯ
# ---------------------------------------------------------------------------
def audit_system_environment():
    section("1. СИСТЕМНОЕ ОКРУЖЕНИЕ И ПУТИ")
    log(f"OS Platform: {platform.platform()} ({sys.platform})")
    log(f"OS Version: {platform.version()}, Release: {platform.release()}")
    log(f"Architecture: {platform.machine()} (Python: {struct.calcsize('P') * 8}-bit)")
    log(f"Python Executable: {sys.executable}")
    log(f"Python Version: {sys.version}")
    log(f"Current Working Dir: {os.getcwd()}")
    log(f"sys.argv[0]: {sys.argv[0] if sys.argv else ''}")
    log(f"NUITKA_ONEFILE_BINARY: {os.environ.get('NUITKA_ONEFILE_BINARY', '(not set)')}")
    log(f"LOCALAPPDATA: {os.environ.get('LOCALAPPDATA', '(not set)')}")
    log(f"APPDATA: {os.environ.get('APPDATA', '(not set)')}")
    log(f"TEMP: {os.environ.get('TEMP', '(not set)')}")


# ---------------------------------------------------------------------------
# 2. ПОИСК CHROMIUM
# ---------------------------------------------------------------------------
def _exe_dir() -> Path:
    nuitka_bin = os.environ.get("NUITKA_ONEFILE_BINARY")
    if nuitka_bin and os.path.exists(nuitka_bin):
        try:
            return Path(os.path.abspath(nuitka_bin)).parent
        except Exception:
            pass
    try:
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0 and os.path.exists(argv0):
            return Path(os.path.abspath(argv0)).parent
    except Exception:
        pass
    try:
        if sys.executable and os.path.exists(sys.executable):
            return Path(os.path.abspath(sys.executable)).parent
    except Exception:
        pass
    try:
        return Path(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    return Path.cwd()


def audit_chromium_discovery():
    section("2. ПОИСК И ПРОВЕРКА ПАПКИ CHROMIUM")
    base_dir = _exe_dir()
    cwd = Path.cwd()
    log(f"Вычисленная папка _exe_dir(): {base_dir}")
    log(f"Текущая рабочая папка CWD: {cwd}")

    candidates = [
        base_dir / "chromium" / "chrome.exe",
        base_dir / "chromium" / "chromium.exe",
        base_dir / "chrome.exe",
        base_dir / "chromium.exe",
        cwd / "chromium" / "chrome.exe",
        cwd / "chromium" / "chromium.exe",
        cwd / "chrome.exe",
    ]

    found_path = None
    for c in candidates:
        exists = c.is_file()
        status = "[НАЙДЕН]" if exists else "[НЕТ]"
        log(f"  {status} {c}")
        if exists and not found_path:
            found_path = c

    if not found_path:
        log("ОШИБКА: Ни один исполняемый файл Chromium не найден!", level="ERROR")
        return None

    log(f"Выбран исполняемый файл Chromium: {found_path}", level="OK")
    try:
        size_mb = os.path.getsize(found_path) / (1024 * 1024)
        log(f"Размер файла: {size_mb:.2f} MB")
    except Exception as e:
        log(f"Не удалось получить размер: {e}", level="WARN")

    # Проверка тестового запуска --version
    try:
        res = subprocess.run([str(found_path), "--version"], capture_output=True, text=True, timeout=5)
        log(f"Версия Chromium (--version): {(res.stdout or res.stderr).strip()}")
    except Exception as e:
        log(f"Ошибка вызова --version: {e}", level="WARN")

    return str(found_path)


# ---------------------------------------------------------------------------
# 3. ТЕСТ ЗАПУСКА CHROMIUM И CDP
# ---------------------------------------------------------------------------
def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def audit_chrome_launch_and_cdp(browser_path: str):
    section("3. ТЕСТ ЗАПУСКА CHROMIUM И CDP (DEVTOOLS PROTOCOL)")
    port = _free_port()
    log(f"Выделен локальный порт отладки: {port}")

    profile_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GoogleAIWindow" / "Profile_Diag"
    profile_dir.mkdir(parents=True, exist_ok=True)
    log(f"Профиль браузера: {profile_dir}")

    url = "https://google.com/ai"
    args = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        f"--app={url}",
        "--use-fake-ui-for-media-stream",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-window",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--disable-features=SystemTitlebar,CaptionButtons,TranslateUI,InProductHelp,MenuCommands,MediaRouter,GlobalMediaControls",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--noerrdialogs",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--window-position=-32000,-32000",
        "--window-size=453,735",
    ]

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE

    log(f"Запуск subprocess.Popen (PID launcher ожидается)...")
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        log(f"Chromium успешно запущен! Главный PID процесса: {proc.pid}", level="OK")
    except Exception as e:
        log(f"Критическая ошибка запуска Chromium: {e}", level="ERROR")
        return None, None, None

    # Ожидание открытия порта CDP
    log("Ожидание готовности порта CDP (GET http://127.0.0.1:{port}/json/version)...")
    connected = False
    browser_ws = None
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"ВНИМАНИЕ: Процесс Chromium неожиданно завершился с кодом {proc.returncode}!", level="ERROR")
            break
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.0) as r:
                data = json.load(r)
                browser_ws = data.get("webSocketDebuggerUrl")
                log(f"CDP ответил! WebSocket URL: {browser_ws}", level="OK")
                connected = True
                break
        except Exception:
            time.sleep(0.3)

    if not connected:
        log(f"ОШИБКА: CDP порт {port} не ответил за 15 секунд!", level="ERROR")

    # Проверка списка вкладок /json/list
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.0) as r:
            targets = json.load(r)
            log(f"Активные вкладки /json/list (всего: {len(targets)}):")
            for t in targets:
                log(f"  - [{t.get('type')}] {t.get('title')} | URL: {t.get('url')}")
    except Exception as e:
        log(f"Ошибка получения /json/list: {e}", level="WARN")

    return proc, port, browser_ws


# ---------------------------------------------------------------------------
# 4. АУДИТ ДЕРЕВА ПРОЦЕССОВ И ОКОН WINDOWS 11
# ---------------------------------------------------------------------------
def audit_processes_and_windows(root_pid: int):
    section("4. АУДИТ ДЕРЕВА ПРОЦЕССОВ И ОКОН (WINDOWS 11 ENUMERATION)")
    if not kernel32 or not user32:
        log("WinAPI недоступен (не Windows)", level="WARN")
        return []

    # 1. Построение дерева процессов
    all_chrome_pids = set()
    tree = {}  # parent -> set(children)

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot and snapshot != -1:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(pe)):
            while True:
                p_id = pe.th32ProcessID
                parent_id = pe.th32ParentProcessID
                exe_name = str(pe.szExeFile).lower()
                if "chrome" in exe_name or "chromium" in exe_name:
                    all_chrome_pids.add(p_id)
                    tree.setdefault(parent_id, set()).add(p_id)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(snapshot)

    # Рекурсивные потомки root_pid
    descendant_pids = {root_pid}
    queue = [root_pid]
    while queue:
        curr = queue.pop(0)
        for child in tree.get(curr, []):
            if child not in descendant_pids:
                descendant_pids.add(child)
                queue.append(child)

    log(f"Корневой PID Chromium: {root_pid}")
    log(f"Собранные потомки (дерево root_pid): {descendant_pids}")
    log(f"Все процессы Chrome/Chromium в системе: {all_chrome_pids}")

    # 2. Перечисление всех окон в системе
    log("\nСканирование всех окон Windows через EnumWindows...")
    windows_found = []

    def enum_cb(hwnd, lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            buf_cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf_cls, 256)
            cls_name = buf_cls.value

            buf_title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf_title, 512)
            title = buf_title.value

            # Нас интересуют окна Chromium или связанные с нашими PID
            is_chrome_cls = "chrome" in cls_name.lower() or "widget" in cls_name.lower()
            is_our_pid = pid.value in descendant_pids or pid.value in all_chrome_pids

            if is_chrome_cls or is_our_pid:
                rect = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                visible = bool(user32.IsWindowVisible(hwnd))
                style = user32.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
                exstyle = user32.GetWindowLongW(hwnd, -20) & 0xFFFFFFFF
                parent_h = user32.GetParent(hwnd)

                info = {
                    "hwnd": int(hwnd),
                    "pid": pid.value,
                    "is_descendant": pid.value in descendant_pids,
                    "class": cls_name,
                    "title": title,
                    "visible": visible,
                    "size": f"{w}x{h} (at {rect.left},{rect.top})",
                    "width": w,
                    "height": h,
                    "area": w * h,
                    "style": f"0x{style:08X}",
                    "exstyle": f"0x{exstyle:08X}",
                    "parent_hwnd": int(parent_h) if parent_h else 0,
                }
                windows_found.append(info)
        except Exception:
            pass
        return True

    cb = WNDENUMPROC(enum_cb)
    user32.EnumWindows(cb, 0)

    log(f"Найдено окон, связанных с Chromium: {len(windows_found)}")
    for i, w in enumerate(windows_found, 1):
        tag = "[НАШ ПОТОМОК]" if w["is_descendant"] else "[ДРУГОЙ CHROME]"
        log(f"  {i}. HWND={w['hwnd']} | PID={w['pid']} {tag}")
        log(f"     Class='{w['class']}' | Title='{w['title']}'")
        log(f"     Visible={w['visible']} | Size={w['size']} | Style={w['style']}")

    return windows_found


# ---------------------------------------------------------------------------
# 5. СИМУЛЯЦИЯ ВСТРАИВАНИЯ (PyQt6 + SetParent)
# ---------------------------------------------------------------------------
def audit_pyqt_embedding_simulation(target_hwnd: int):
    section("5. СИМУЛЯЦИЯ ВСТРАИВАНИЯ ОКНА В PYQT6 (SETPARENT ТЕСТ)")
    if not target_hwnd or not user32:
        log("Нет целевого HWND для симуляции встраивания", level="WARN")
        return

    try:
        from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QLabel, QVBoxLayout
        from PyQt6.QtCore import Qt, QTimer
    except ImportError:
        log("PyQt6 не установлен в окружении — симуляция GUI пропущена", level="WARN")
        return

    log(f"Инициализация тестового окна PyQt6 для захвата HWND {target_hwnd}...")
    app = QApplication.instance() or QApplication(sys.argv)

    main_win = QMainWindow()
    main_win.setWindowTitle("Legalyze — Win11 Embedding Diagnostic Sandbox")
    main_win.setFixedSize(480, 750)

    central = QWidget(main_win)
    main_win.setCentralWidget(central)
    layout = QVBoxLayout(central)
    layout.setContentsMargins(10, 10, 10, 10)

    info_label = QLabel(f"Диагностический контейнер (Target HWND: {target_hwnd})")
    info_label.setStyleSheet("color: #7c83ff; font-weight: bold;")
    layout.addWidget(info_label)

    placeholder = QWidget(central)
    placeholder.setStyleSheet("background: #111425; border: 2px dashed #3e447a;")
    layout.addWidget(placeholder)

    main_win.show()
    app.processEvents()

    parent_hwnd = int(placeholder.winId())
    log(f"HWND родительского контейнера Qt placeholder: {parent_hwnd}")

    # Попытка SetParent
    old_parent = user32.SetParent(wintypes.HWND(target_hwnd), wintypes.HWND(parent_hwnd))
    err_code = ctypes.get_last_error()
    log(f"Результат SetParent(target={target_hwnd}, parent={parent_hwnd}): old_parent={int(old_parent)}, GetLastError={err_code}")

    if not old_parent and err_code != 0:
        log(f"ОШИБКА: SetParent вернул 0 (код ошибки Windows: {err_code})!", level="ERROR")
    else:
        log("SetParent успешно выполнен!", level="OK")

    # Стили окна
    WS_POPUP = 0x80000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_CLIPCHILDREN = 0x02000000
    WS_CLIPSIBLINGS = 0x04000000

    style = user32.GetWindowLongW(target_hwnd, -16)
    style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME)
    style |= (WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
    style &= 0xFFFFFFFF
    user32.SetWindowLongW(target_hwnd, -16, style)

    user32.ShowWindow(target_hwnd, 5)  # SW_SHOW
    user32.SetWindowPos(
        wintypes.HWND(target_hwnd),
        wintypes.HWND(0),
        0, 0,
        placeholder.width(), placeholder.height(),
        0x0020 | 0x0040  # SWP_FRAMECHANGED | SWP_SHOWWINDOW
    )

    log("Окно перепозиционировано и активировано внутри Qt-виджета.")
    log("Отображение тестового окна на 3 секунды для визуальной проверки...")
    t_end = time.time() + 3.0
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.05)

    main_win.close()
    log("Тест встраивания успешно завершен!", level="OK")


# ---------------------------------------------------------------------------
# 6. ТЕСТ СЕРВЕРА
# ---------------------------------------------------------------------------
def audit_server_connection():
    section("6. ПРОВЕРКА СЕТЕВОГО СОЕДИНЕНИЯ С СЕРВЕРОМ")
    urls = [
        "https://legalyzeai.ru",
        "https://legalyzeai.ru/api/client/update?action=check",
    ]
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Legalyze-Diagnostic/1.0"})
            with urllib.request.urlopen(req, timeout=8.0) as r:
                log(f"[OK] {u} -> Status {r.status} {r.reason}")
        except Exception as e:
            log(f"[ОШИБКА] {u} -> {e}", level="WARN")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    try:
        if os.path.exists(LOG_FILE):
            os.remove(LOG_FILE)
    except Exception:
        pass

    section("ЗАПУСК КОМПЛЕКСНОЙ ДИАГНОСТИКИ WINDOWS 11")
    log("Инструмент проверки запуска Legalyze и захвата Chromium")

    audit_system_environment()
    browser_path = audit_chromium_discovery()

    proc = None
    if browser_path:
        proc, port, ws = audit_chrome_launch_and_cdp(browser_path)
        if proc:
            windows = audit_processes_and_windows(proc.pid)

            # Выбираем наилучшее окно
            best_hwnd = None
            if windows:
                # Фильтруем: сначала наши потомки с наибольшей площадью
                descendants = [w for w in windows if w["is_descendant"] and w["area"] > 5000]
                if descendants:
                    descendants.sort(key=lambda x: x["area"], reverse=True)
                    best_hwnd = descendants[0]["hwnd"]
                else:
                    windows.sort(key=lambda x: x["area"], reverse=True)
                    best_hwnd = windows[0]["hwnd"]

            if best_hwnd:
                log(f"\nГлавный целевой HWND для встраивания: {best_hwnd}", level="OK")
                audit_pyqt_embedding_simulation(best_hwnd)
            else:
                log("\nНе найдено подходящего окна браузера для встраивания!", level="ERROR")

            # Завершаем тестовый процесс Chrome
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                pass

    audit_server_connection()

    section("ИТОГ ДИАГНОСТИКИ")
    log(f"Полный отчет сохранен в файл: {os.path.abspath(LOG_FILE)}")
    log("Скопируйте содержимое файла win11_diagnostic_log.txt или пришлите сюда вывод.")
    input("\nНажмите Enter для выхода...")


if __name__ == "__main__":
    main()
