"""
Legalyze AI client — main.py (обновленная и оптимизированная версия)

Изменения:
 1. Исправлена вечная загрузка при первом старте:
    - Из install_purge убран деструктивный Page.reload, сбивавший первичную инициализацию страницы.
    - В UploadThread устранён фатальный break при self.fails >= 5, добавлен надёжный цикл повторов с backoff.
    - В ChromeWorker реализовано корректное ожидание готовности DOM и динамическое обновление списка дочерних PID.
    - В upload_file_advanced повышена отказоустойчивость загрузки.
 2. Полностью убраны консольные окна при запуске и работе приложения (CREATE_NO_WINDOW + STARTUPINFO SW_HIDE).
 3. Обновлены значения шаблона по умолчанию и подсказок (Mike Macmillan, 85028, SANG, 11, MP, Заместитель командующего MP, сенатор NG).
 4. Адаптация браузера под любое разрешение и DPI-масштабирование (100%, 125%, 150%, 175%, 200%)
    через GetClientRect и автоматическую синхронизацию геометрии в _sync_chrome_geometry.
 5. Стандартный zoom страницы 67% при первом и последующих запусках (через addScriptToEvaluateOnNewDocument и CSS/JS).
 6. Удалены сторонние браузеры (Playwright, системный Chrome, реестр), оставлен только portable Chromium рядом с exe.
"""
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.request
import ctypes
import threading
import hashlib
import math
import base64
import requests
from ctypes import wintypes
from pathlib import Path

from websocket import create_connection
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QCheckBox, QDialog, QComboBox,
    QFormLayout, QMessageBox, QInputDialog, QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import QTimer, QThread, pyqtSignal, Qt, QPoint, QRectF
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QPen, QBrush, QPainterPath, QKeySequence,
)

from config import (
    load_config, save_config, get_template_filled,
    SERVER_URL, CURRENT_VERSION, APP_DIR, DATA_DIR, PROMPTS_DIR,
    TEMPLATE_FILE, normalize_template,
    ensure_template_file, write_template_file, save_template,
)
from crypto_utils import decrypt_data_auto
from hwid_gen import generate_hwid
from updater import check_for_update, restart_app

from secure_store import (
    SecureWorkspace, PromptCacheManifest,
    encrypt_secret, decrypt_secret,
    encrypt_blob, decrypt_blob,
    purge_plaintext_artifacts, shred_file,
    sha256_bytes, template_hash,
)

# FPDF нужен только как legacy-фолбэк для старых текстовых .enc (ТЗ 1.3).
try:
    from fpdf import FPDF
except Exception:
    FPDF = None


URL = "https://google.com/ai"
PROFILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GoogleAIWindow" / "Profile"


def _exe_dir() -> Path:
    # Папка запущенного exe (для Nuitka onefile — по argv[0], а не временная распаковка).
    try:
        argv0 = sys.argv[0] if sys.argv else ""
        if argv0 and argv0.lower().endswith(".exe") and os.path.isfile(argv0):
            return Path(os.path.abspath(argv0)).parent
    except Exception:
        pass
    try:
        return Path(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        return Path.cwd()


def resolve_browser_path() -> str:
    """
    Поиск браузера: только portable chromium рядом с .exe / скриптом (ТЗ п.8).
    Сторонние браузеры удалены.
    """
    exe_dir = _exe_dir()
    candidates = [
        exe_dir / "chromium" / "chrome.exe",
        exe_dir / "chromium" / "chromium.exe",
        exe_dir / "chrome.exe",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except Exception:
            continue
    return ""


X, Y, W, H = 1426, 200, 453, 735

GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_DLGFRAME = 0x00400000
WS_BORDER = 0x00800000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000

SWP_FRAMECHANGED = 0x0020
SWP_NOACTIVATE = 0x0010
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SW_SHOW = 5

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
HWND_TOP = 0
HWND_TOPMOST = -1
WM_HOTKEY = 0x0312

user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None


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
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


if user32:
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int

    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long

    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetWindowLongW.restype = ctypes.c_long

    if hasattr(user32, "GetWindowLongPtrW"):
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p

        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p

    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL

    user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.SetParent.restype = wintypes.HWND

    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL

    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    user32.RegisterHotKey.restype = wintypes.BOOL

    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL

if kernel32:
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL

    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def get_window_long(hwnd, index):
    if not user32:
        return 0
    try:
        if hasattr(user32, "GetWindowLongPtrW"):
            return int(user32.GetWindowLongPtrW(hwnd, index) or 0) & 0xFFFFFFFF
        return int(user32.GetWindowLongW(hwnd, index) or 0) & 0xFFFFFFFF
    except Exception:
        return 0


def set_window_long(hwnd, index, value):
    if not user32:
        return 0
    try:
        value = int(value) & 0xFFFFFFFF
        if hasattr(user32, "SetWindowLongPtrW"):
            return user32.SetWindowLongPtrW(hwnd, index, ctypes.c_void_p(value))
        return user32.SetWindowLongW(hwnd, index, value)
    except Exception:
        return 0


def force_topmost(hwnd):
    """Жёстко поднимает окно в слой TOPMOST (режим «картинка-в-картинке»)."""
    if not user32 or not hwnd:
        return
    try:
        user32.SetWindowPos(
            wintypes.HWND(int(hwnd)), wintypes.HWND(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


VK_MAP = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
    "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
    "F11": 0x7A, "F12": 0x7B,

    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45, "F": 0x46,
    "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "M": 0x4D, "N": 0x4E, "O": 0x4F, "P": 0x50, "Q": 0x51, "R": 0x52,
    "S": 0x53, "T": 0x54, "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58,
    "Y": 0x59, "Z": 0x5A,

    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,

    "Num0": 0x60, "Num1": 0x61, "Num2": 0x62, "Num3": 0x63,
    "Num4": 0x64, "Num5": 0x65, "Num6": 0x66, "Num7": 0x67,
    "Num8": 0x68, "Num9": 0x69,

    "Space": 0x20, "Enter": 0x0D, "Tab": 0x09, "Esc": 0x1B,

    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "`": 0xC0,

    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,

    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
}

QT_KEY_TO_NAME = {}
for _i in range(1, 13):
    QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_F{_i}")] = f"F{_i}"
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_{_c}")] = _c
for _d in "0123456789":
    QT_KEY_TO_NAME[getattr(Qt.Key, f"Key_{_d}")] = _d
QT_KEY_TO_NAME.update({
    Qt.Key.Key_Space: "Space", Qt.Key.Key_Return: "Enter", Qt.Key.Key_Enter: "Enter",
    Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Insert: "Insert", Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End", Qt.Key.Key_PageUp: "PageUp",
    Qt.Key.Key_PageDown: "PageDown", Qt.Key.Key_Up: "Up", Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Left: "Left", Qt.Key.Key_Right: "Right", Qt.Key.Key_Minus: "-",
    Qt.Key.Key_Equal: "=", Qt.Key.Key_BracketLeft: "[", Qt.Key.Key_BracketRight: "]",
    Qt.Key.Key_Backslash: "\\", Qt.Key.Key_Semicolon: ";", Qt.Key.Key_Apostrophe: "'",
    Qt.Key.Key_Comma: ",", Qt.Key.Key_Period: ".", Qt.Key.Key_Slash: "/",
    Qt.Key.Key_QuoteLeft: "`",
})

MODIFIER_MAP = {
    "Ctrl": 0x0002,
    "Alt": 0x0001,
    "Shift": 0x0004,
    "Win": 0x0008,
}

MOD_NOREPEAT = 0x4000

HOTKEY_TOGGLE_ID = 1
HOTKEY_MIC_ID = 2

THEME = {
    "bg": "#0e1020",
    "bg2": "#151830",
    "panel": "rgba(14, 16, 32, 0.96)",
    "line": "#262a4a",
    "text": "#e6e8f5",
    "muted": "#8f96bf",
    "accent": "#7c83ff",
    "accent2": "#5b61e8",
    "danger": "#ef4444",
    "ok": "#34d399",
}

BASE_QSS = f"""
QDialog, QWidget#Card {{
    background: {THEME['bg']};
    border: 1px solid {THEME['line']};
    border-radius: 14px;
}}
QLabel {{ color: {THEME['text']}; font-size: 13px; }}
QLabel#Caption {{ color: {THEME['muted']}; font-size: 11px; }}
QLineEdit {{
    background: {THEME['bg2']};
    border: 1px solid {THEME['line']};
    border-radius: 8px;
    padding: 10px 12px;
    color: {THEME['text']};
    font-size: 14px;
    selection-background-color: {THEME['accent2']};
}}
QLineEdit:focus {{ border: 1px solid {THEME['accent']}; background: #171b38; }}
QComboBox {{
    background: {THEME['bg2']};
    border: 1px solid {THEME['line']};
    border-radius: 8px;
    padding: 8px 10px;
    color: {THEME['text']};
    font-size: 13px;
}}
QComboBox:hover {{ border: 1px solid {THEME['accent']}; }}
QComboBox QAbstractItemView {{
    background: {THEME['bg2']};
    color: {THEME['text']};
    selection-background-color: {THEME['accent2']};
    border: 1px solid {THEME['line']};
    outline: none;
}}
QCheckBox {{ color: {THEME['muted']}; font-size: 12px; }}
QPushButton {{
    background: {THEME['bg2']};
    color: {THEME['text']};
    border: 1px solid {THEME['line']};
    border-radius: 9px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {THEME['accent']}; background: #1b1f40; }}
QPushButton:pressed {{ background: #222750; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {THEME['accent']}, stop:1 {THEME['accent2']});
    border: none;
    color: #ffffff;
}}
QPushButton#Primary:hover {{ background: {THEME['accent2']}; }}
QPushButton#Ghost {{ background: transparent; color: {THEME['muted']}; }}
QPushButton#Ghost:hover {{ color: {THEME['text']}; border-color: {THEME['accent']}; }}
QPushButton#Danger {{
    background: rgba(239, 68, 68, 0.14);
    color: #fca5a5;
    border: 1px solid rgba(239, 68, 68, 0.55);
}}
QPushButton#Danger:hover {{ background: rgba(239, 68, 68, 0.32); color: #fff; }}
"""


class CDP:
    def __init__(self, ws_url):
        self.ws = create_connection(ws_url, suppress_origin=True, max_size=None)
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method, params=None, timeout=60):
        with self._lock:
            self._id += 1
            mid = self._id
            payload = json.dumps({
                "id": mid,
                "method": method,
                "params": params or {},
            })

            self.ws.send(payload)
            deadline = time.time() + timeout

            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError(method)

                try:
                    self.ws.settimeout(max(0.05, left))
                    raw = self.ws.recv()
                except Exception:
                    if time.time() >= deadline:
                        raise TimeoutError(method)
                    continue

                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

    def eval(self, expr, await_promise=False, by_value=True, timeout=60):
        r = self.send("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": await_promise,
            "returnByValue": by_value,
            "userGesture": True,
            "generatePreview": False,
        }, timeout=timeout)

        if isinstance(r, dict) and "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"].get("text", "JS exception")))

        if isinstance(r, dict):
            return r.get("result", {})
        return {}

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_chrome():
    port = _free_port()
    PROFILE.mkdir(parents=True, exist_ok=True)

    browser_path = resolve_browser_path()
    if not browser_path:
        raise FileNotFoundError("browser not found (no chromium next to exe)")

    args = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={PROFILE}",
        f"--app={URL}",
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

    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    return proc, port


def _http_json(port, path, timeout=1.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.load(r)


def attach_chrome(port, timeout=30):
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            version = _http_json(port, "/json/version")
            browser_ws = version.get("webSocketDebuggerUrl")

            targets = _http_json(port, "/json/list")
            pages = [
                t for t in targets
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]

            google_pages = [t for t in pages if "google.com" in t.get("url", "")]
            chosen = google_pages[0] if google_pages else (pages[0] if pages else None)

            if browser_ws and chosen:
                return CDP(browser_ws), CDP(chosen["webSocketDebuggerUrl"])
        except Exception:
            pass

        time.sleep(0.1)

    raise TimeoutError("chrome/devtools not ready")


def grant_mic_permission(browser: CDP, page: CDP):
    origins = set()

    try:
        origin = page.eval("location.origin", timeout=5).get("value")
        if origin and origin != "null":
            origins.add(origin)
    except Exception:
        pass

    origins.update([
        "https://google.com",
        "https://www.google.com",
        "https://gemini.google.com",
        "https://accounts.google.com",
    ])

    browser_context_id = None
    try:
        info = browser.send("Target.getBrowserContexts", timeout=5)
        ids = info.get("browserContextIds") or []
        if ids:
            browser_context_id = ids[0]
    except Exception:
        pass

    for origin in origins:
        params = {"origin": origin, "permissions": ["audioCapture", "videoCapture"]}
        if browser_context_id:
            params["browserContextId"] = browser_context_id

        try:
            browser.send("Browser.grantPermissions", params, timeout=5)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Zoom 67% по стандарту (ТЗ п.5)
# --------------------------------------------------------------------------- #
JS_ZOOM = r"""
(() => {
    const applyZoom = () => {
        try {
            if (document.documentElement) {
                document.documentElement.style.setProperty('zoom', '67%', 'important');
            }
            if (document.body) {
                document.body.style.setProperty('zoom', '67%', 'important');
            }
        } catch (e) {}
    };
    applyZoom();
    document.addEventListener('DOMContentLoaded', applyZoom);
    window.addEventListener('load', applyZoom);
    try {
        const styleId = '__legalyze_zoom_style__';
        if (!document.getElementById(styleId)) {
            const style = document.createElement('style');
            style.id = styleId;
            style.innerHTML = 'html, body { zoom: 67% !important; }';
            (document.head || document.documentElement).appendChild(style);
        }
    } catch (e) {}
})();
"""

JS_PURGE = r"""
(() => {
    if (window.__purgeInstalled) {
        if (window.__purgeRun) window.__purgeRun();
        return;
    }

    window.__purgeInstalled = true;

    const SELECTORS = [
        'div.qEn1od[jsname="NlVIob"]',
        'div.qEn1od',
        'div.P3mIxe.Hw60ud',
        'div.FSUH7d[jsname="xcvsnc"]',
        'div.GG4mbd[role="navigation"]',
        'div.eT9Cje',
        'span.gb',
        'div[jscontroller="SJpD2c"][jsname="uZkjhb"]',
        'header#gb',
        'div#gbwa',
        'g-snackbar[jsname="PWj1Zb"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"] *'
    ];

    const S = SELECTORS.join(',');

    const hide = (el) => {
        if (el && el.style) {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
        }
        try { el.remove(); } catch (e) {}
    };

    const kill = (node) => {
        if (!node || node.nodeType !== 1) return;

        if (node.matches && node.matches(S)) {
            hide(node);
            return;
        }

        if (node.querySelectorAll) {
            const list = node.querySelectorAll(S);
            for (let i = list.length - 1; i >= 0; i--) hide(list[i]);
        }
    };

    window.__purgeRun = () => kill(document.documentElement);

    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) kill(n);
            } else if (m.type === 'attributes') {
                kill(m.target);
            }
        }
    });

    const start = () => {
        const root = document.documentElement;
        if (!root) return false;

        obs.observe(root, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'id', 'jsname', 'jscontroller']
        });

        kill(root);
        return true;
    };

    if (!start()) {
        const t = setInterval(() => {
            if (start()) clearInterval(t);
        }, 10);
    }

    document.addEventListener('DOMContentLoaded', () => kill(document.documentElement), { once: true });
    window.addEventListener('load', () => kill(document.documentElement), { once: true });
})();
"""

# Детект файлов в чате Google AI
JS_CHECK_FILES = r"""
(() => {
const chips = Array.from(document.querySelectorAll('div.ArblTe'));
let done = 0;
let uploading = false;
const names = [];
for (const chip of chips) {
    let fname = '';
    try {
        const host = chip.closest('div[role="button"]');
        if (host) {
            fname = (host.getAttribute('title') || host.getAttribute('aria-label') || '').trim();
            if (!fname) {
                const label = host.querySelector('div.zrI2ad');
                const txt = (label ? label.innerText : host.innerText) || '';
                fname = txt.replace(/\s+/g, ' ').trim();
            }
        }
        if (!fname) fname = ((chip.innerText || '').replace(/\s+/g, ' ').trim());
        fname = fname.slice(0, 160);
    } catch (e) { fname = ''; }
    if (chip.querySelector('div.zXgSbc')) { done += 1; names.push(fname); continue; }
    const bar = chip.querySelector('div[data-progressvalue]');
    if (bar) {
        const val = parseFloat(bar.getAttribute('data-progressvalue') || '0');
        if (val < 0.99) { uploading = true; continue; }
    }
    done += 1; names.push(fname);
}
return { present: done, uploading: uploading, names: names };
})()
"""

JS_FIND_INPUT = r"""
(() => {
    const ins = Array.from(document.querySelectorAll('input[type=file]'));
    if (!ins.length) return null;

    const suitable = ins.find((input) => {
        if (!input.accept) return true;
        const accept = String(input.accept).toLowerCase();
        return accept.includes('pdf') || accept.includes('application') || accept.includes('*');
    });

    return suitable || ins[0];
})()
"""

JS_DISABLE_CONTEXT_MENU = "window.addEventListener('contextmenu', (e) => { e.preventDefault(); }, true);"

JS_DISABLE_DRAG = r"""
(() => {
    const s = document.createElement('style');
    s.innerHTML = 'html, body, *:not(input):not(textarea):not([contenteditable="true"]) { -webkit-app-region: no-drag !important; user-select: none !important; }';
    document.head.appendChild(s);
})();
"""

# Микрофон: только СТАРТ записи
JS_CLICK_MIC = r"""
(() => {
    const btn = document.querySelector('button[aria-label="Микрофон"]')
             || document.querySelector('button[aria-label*="икрофон"]')
             || document.querySelector('button[aria-label*="icrophon"]');
    if (!btn) return { ok: false, reason: 'no-mic-button' };
    btn.click();
    setTimeout(() => {
        if (document.activeElement === btn) btn.blur();
        const ta = document.querySelector('textarea');
        if (ta) { ta.focus(); ta.click(); }
    }, 50);
    return { ok: true };
})()
"""

# Отправка
JS_CLICK_SEND = r"""
(() => {
    const stopMic = document.querySelector('button[aria-label*="становить"]')
                 || document.querySelector('button[aria-label*="top listening"]');
    if (stopMic) { stopMic.click(); }

    const send = () => {
        const btn = document.querySelector('button[aria-label="Отправить"]')
                 || document.querySelector('button[aria-label*="тправ"]')
                 || document.querySelector('button[aria-label*="end"]');
        if (btn && !btn.disabled) {
            btn.click();
            setTimeout(() => {
                if (document.activeElement === btn) btn.blur();
                const ta = document.querySelector('textarea');
                if (ta) { ta.focus(); ta.click(); }
            }, 50);
            return true;
        }
        return false;
    };

    if (send()) return { ok: true };
    setTimeout(send, 250);
    return { ok: true, deferred: true };
})()
"""

JS_MIC_STATE = r"""
(() => {
    const rec = document.querySelector('button[aria-label*="становить"]')
             || document.querySelector('button[aria-label*="top listening"]')
             || document.querySelector('button[aria-pressed="true"][aria-label*="икрофон"]');
    return { recording: !!rec };
})()
"""

# Удаление файлов из чата
JS_REMOVE_ALL_FILES = r"""
(() => {
    let clicked = 0;
    const fire = (el) => {
        try {
            const r = el.getBoundingClientRect();
            const x = r.left + r.width / 2, y = r.top + r.height / 2;
            for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, clientX: x, clientY: y}));
            }
            clicked++;
        } catch (e) {}
    };
    const chips = document.querySelectorAll('div.ArblTe');
    for (const chip of chips) {
        const x = chip.querySelector('div.zXgSbc');
        if (x) fire(x);
    }
    if (!clicked) {
        const old = document.querySelectorAll(
            'button[aria-label*="Delete"], button[aria-label*="Remove"], button[aria-label*="Удалить"]');
        for (const el of old) { try { el.click(); clicked++; } catch (e) {} }
    }
    return clicked;
})()
"""

JS_CHECK_END = r"""
(() => {
    const bodyText = (document.body ? document.body.innerText : '') || '';
    const cleanText = bodyText.toUpperCase().replace(/[\s=*/\-_]+/g, '');
    const matches = cleanText.match(/КОНЕЦОТВЕТА/g);
    return matches ? matches.length : 0;
})()
"""


def install_purge(page: CDP):
    for src in (JS_DISABLE_CONTEXT_MENU, JS_DISABLE_DRAG, JS_PURGE, JS_ZOOM):
        try:
            page.send("Page.addScriptToEvaluateOnNewDocument", {"source": src}, timeout=5)
        except Exception:
            pass

    try:
        page.send("Page.enable", timeout=5)
    except Exception:
        pass

    for src in (JS_DISABLE_CONTEXT_MENU, JS_DISABLE_DRAG, JS_PURGE, JS_ZOOM):
        try:
            page.eval(src, timeout=5)
        except Exception:
            pass


def check_state(page: CDP):
    """Состояние чата: present — число готовых файлов, names — их имена."""
    default = {"present": 0, "uploading": False, "names": []}

    try:
        r = page.eval(JS_CHECK_FILES, timeout=2)
        value = r.get("value")
        if isinstance(value, dict):
            try:
                present = int(value.get("present", 0) or 0)
            except Exception:
                present = 1 if value.get("present") else 0
            names = value.get("names", []) or []
            return {
                "present": present,
                "uploading": bool(value.get("uploading", False)),
                "names": [str(n) for n in names] if isinstance(names, list) else [],
            }
    except Exception:
        pass

    return default


def _chat_has_file(names, wanted: str) -> bool:
    """Есть ли файл wanted среди прикреплённых (поиск по подстроке имени)."""
    want = (wanted or "").strip().lower()
    if not want:
        return False
    for n in names or []:
        text = str(n or "").lower()
        if want in text or text in want:
            return True
    return False


def _mime_for_path(path) -> str:
    """MIME-тип по расширению: PDF и TXT грузятся каждый со своим типом."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".txt", ".json", ".md", ".csv"):
        return "text/plain"
    return "application/octet-stream"


def upload_file_advanced(page: CDP, path: Path, mime: str | None = None) -> bool:
    """
    Надежный метод загрузки файла через эмуляцию Drag & Drop с DataTransfer API и File Input.
    Работает с любыми современными SPA, так как полностью имитирует действие
    пользователя (перетаскивание файла), обходя ограничения скрытых input'ов.
    Одинаково используется и для PDF промта, и для Шаблон.txt.
    """
    try:
        if not path or not Path(path).exists():
            return False

        file_path = Path(path)
        file_name = file_path.name.replace("\\", "").replace('"', "").replace("\n", "")
        mime = mime or _mime_for_path(file_path)
        file_data = file_path.read_bytes()
        b64_data = base64.b64encode(file_data).decode('ascii')

        js_inject = f"""
        (() => {{
            try {{
                const b64 = "{b64_data}";
                const fileName = "{file_name}";

                const binary = atob(b64);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {{
                    bytes[i] = binary.charCodeAt(i);
                }}

                const blob = new Blob([bytes], {{ type: '{mime}' }});
                const file = new File([blob], fileName, {{ type: '{mime}' }});
                const dt = new DataTransfer();
                dt.items.add(file);

                let matched = false;

                // 1. Устанавливаем в скрытый input
                const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
                for (const input of inputs) {{
                    try {{
                        input.files = dt.files;
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        matched = true;
                    }} catch (e) {{}}
                }}

                // 2. Эмулируем Drag & Drop на текстовое поле (основной триггер для Google AI)
                const dropZone = document.querySelector('.esoFne') 
                              || document.querySelector('textarea') 
                              || document.querySelector('[role="textbox"]')
                              || document.querySelector('main')
                              || document.body;

                if (dropZone) {{
                    dropZone.dispatchEvent(new DragEvent('dragenter', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                    dropZone.dispatchEvent(new DragEvent('dragover', {{ bubbles: true, cancelable: true, dataTransfer: dt }}));
                    dropZone.dispatchEvent(new DragEvent('drop', {{
                        bubbles: true,
                        cancelable: true,
                        dataTransfer: dt
                    }}));
                    matched = true;
                }}

                return matched;
            }} catch (err) {{
                return false;
            }}
        }})()
        """

        res = page.eval(js_inject, timeout=10)
        return bool(res.get("value"))

    except Exception:
        return False


def get_child_pids(pid):
    pids = set()
    if not kernel32:
        return pids

    try:
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE or not snapshot:
            return pids

        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)

        if kernel32.Process32FirstW(snapshot, ctypes.byref(pe)):
            while True:
                if pe.th32ParentProcessID == pid:
                    pids.add(pe.th32ProcessID)

                if not kernel32.Process32NextW(snapshot, ctypes.byref(pe)):
                    break

        kernel32.CloseHandle(snapshot)
    except Exception:
        pass

    return pids


_enum_callbacks = []


def find_chrome_hwnd(pids):
    if not user32:
        return None
    result = []

    def callback(hwnd, lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            if pid.value in pids:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                cls = buf.value

                if cls == "Chrome_WidgetWin_1":
                    result.insert(0, int(hwnd))
                    return False

                if cls == "Chrome_WidgetWin_0":
                    result.append(int(hwnd))
        except Exception:
            pass

        return True

    cb = WNDENUMPROC(callback)
    _enum_callbacks.append(cb)

    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    finally:
        try:
            _enum_callbacks.remove(cb)
        except Exception:
            pass

    return result[0] if result else None


def text_to_pdf_bytes(text: bytes, title: str = "") -> bytes:
    if FPDF is None:
        raise RuntimeError("fpdf is not installed")

    pdf = FPDF()
    try:
        if title:
            pdf.set_title(title)
    except Exception:
        pass

    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    try:
        font_path = DATA_DIR / "DejaVuSans.ttf"
        if font_path.exists():
            pdf.add_font("DejaVu", "", str(font_path))
            pdf.set_font("DejaVu", size=10)
        else:
            pdf.set_font("Helvetica", size=10)
    except Exception:
        try:
            pdf.set_font("Helvetica", size=10)
        except Exception:
            pass

    decoded = text.decode("utf-8", errors="replace")

    for line in decoded.splitlines():
        line = line.strip()

        if not line:
            try:
                pdf.ln(6)
            except Exception:
                pass
            continue

        try:
            pdf.multi_cell(0, 6, line)
        except Exception:
            try:
                safe_line = line.encode("latin-1", errors="replace").decode("latin-1")
                pdf.multi_cell(0, 6, safe_line)
            except Exception:
                pass

    return bytes(pdf.output())


_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def prompt_display_name(filename: str) -> str:
    """Имя так, как оно отображается в выпадающем списке (без служебных расширений)."""
    name = (filename or "").strip()
    name = name.replace("\\", "/").split("/")[-1]
    for ext in (".enc", ".bin", ".dat", ".txt", ".json", ".prompt", ".pdf"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name or "prompt"


def pdf_file_name(filename: str) -> str:
    """Итоговое имя PDF, которое увидит ИИ-чат."""
    safe = _ILLEGAL_FS.sub("_", prompt_display_name(filename)).strip(" .")
    return f"{safe or 'prompt'}.pdf"


def cache_key(filename: str) -> str:
    return _ILLEGAL_FS.sub("_", (filename or "").strip()) or "prompt"


TEMPLATE_EXPORT_NAME = "Шаблон.txt"
SHABLON_MANIFEST_KEY = "__shablon_txt__"
REQUIRED_CHAT_FILES = 2

WORKSPACE = SecureWorkspace(prefix="legalyze_")
MANIFEST = PromptCacheManifest(DATA_DIR / "prompt_cache.json")


class AuthWorker(QThread):
    done = pyqtSignal(bool, str, dict)

    def __init__(self, login, password, remember):
        super().__init__()
        self.login = login
        self.password = password
        self.remember = remember

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/auth",
                json={"login": self.login, "password": self.password},
                timeout=10,
            )
            data = res.json()

            if not data.get("success"):
                self.done.emit(False, data.get("error", "Ошибка"), {})
                return

            self.done.emit(True, "", data.get("data", {}))
        except Exception as e:
            self.done.emit(False, f"Ошибка подключения: {e}", {})


class HWIDWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, token, hwid):
        super().__init__()
        self.token = token
        self.hwid = hwid

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/hwid",
                json={"hwid": self.hwid},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if data.get("success"):
                self.done.emit(True, "")
            else:
                self.done.emit(False, data.get("error", "Ошибка HWID"))
        except Exception as e:
            self.done.emit(False, str(e))


class BalanceWorker(QThread):
    done = pyqtSignal(bool, int, bool, str)
    ENDPOINTS = ("/api/client/queries", "/api/client/me", "/api/client/profile")

    def __init__(self, token):
        super().__init__()
        self.token = token

    def run(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        for ep in self.ENDPOINTS:
            try:
                res = requests.get(f"{SERVER_URL}{ep}", headers=headers, timeout=8)
                if res.status_code != 200:
                    continue
                data = res.json()
                if not isinstance(data, dict) or not data.get("success", True):
                    continue
                payload = data.get("data", data)
                if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
                    payload = payload["user"]
                if not isinstance(payload, dict) or "queriesRemaining" not in payload:
                    continue
                remaining = int(payload.get("queriesRemaining") or 0)
                unlimited = bool(payload.get("hasUnlimited", False))
                unlimited_until = str(payload.get("unlimitedUntil") or "")
                self.done.emit(True, remaining, unlimited, unlimited_until)
                return
            except Exception:
                continue
        self.done.emit(False, 0, False, "")


class DecrementWorker(QThread):
    done = pyqtSignal(bool, int)

    def __init__(self, token):
        super().__init__()
        self.token = token

    def run(self):
        try:
            res = requests.post(
                f"{SERVER_URL}/api/client/queries",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if data.get("success"):
                remaining = int(data.get("data", {}).get("queriesRemaining", 0) or 0)
                self.done.emit(True, remaining)
            else:
                self.done.emit(False, 0)
        except Exception:
            self.done.emit(False, 0)


class PromptLoaderWorker(QThread):
    progress = pyqtSignal(str)
    loaded = pyqtSignal(bool, object)

    def __init__(self, cfg, token, force=False):
        super().__init__()
        self.cfg = cfg
        self.token = token
        self.force = force

    def _server_checksum(self, filename: str) -> str:
        try:
            res = requests.head(
                f"{SERVER_URL}/api/client/prompts",
                params={"filename": filename},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=4,
            )
            return res.headers.get("X-Checksum", "") or res.headers.get("ETag", "").strip('"')
        except Exception:
            return ""

    def _download(self, filename: str, dst: Path) -> bool:
        res = requests.get(
            f"{SERVER_URL}/api/client/prompts",
            params={"filename": filename},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        if res.status_code == 200 and res.content:
            dst.write_bytes(res.content)
            return True
        return False

    def _prepare_template_file(self) -> str:
        tpl = normalize_template(self.cfg.get("template", {}) or {})
        tpl_hash = template_hash(tpl)
        entry = MANIFEST.get(SHABLON_MANIFEST_KEY)

        need_write = (
            self.force
            or not TEMPLATE_FILE.exists()
            or entry.get("template_hash") != tpl_hash
        )
        if need_write:
            self.progress.emit("Обновление Шаблон.txt…")
            write_template_file(tpl)
            try:
                file_sha = sha256_bytes(TEMPLATE_FILE.read_bytes())
            except Exception:
                file_sha = ""
            MANIFEST.put(
                SHABLON_MANIFEST_KEY,
                template_hash=tpl_hash,
                file_sha256=file_sha,
                updated_at=int(time.time()),
            )
        else:
            self.progress.emit("Шаблон не изменился — используем готовый Шаблон.txt…")
        return str(TEMPLATE_FILE)

    def run(self):
        try:
            sel = self.cfg.get("selected_prompt", "")
            if not sel:
                self.loaded.emit(False, "Промт не выбран — откройте «Промт» и выберите файл")
                return

            PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
            key = cache_key(sel)
            enc_source = PROMPTS_DIR / key
            enc_pdf = PROMPTS_DIR / f"{key}.pdfenc"
            out_name = pdf_file_name(sel)

            self.progress.emit("Проверка версии промта на сервере…")
            remote_sum = self._server_checksum(sel)
            entry = MANIFEST.get(key)

            local_sum = ""
            if enc_source.exists():
                try:
                    local_sum = sha256_bytes(enc_source.read_bytes())
                except Exception:
                    local_sum = ""

            unchanged = (
                not self.force
                and enc_pdf.exists()
                and entry.get("pdf_name") == out_name
                and (
                    (remote_sum and entry.get("server_checksum") == remote_sum)
                    or (not remote_sum and entry.get("source_sha256") == local_sum and local_sum)
                )
            )

            pdf_bytes = None

            if unchanged:
                try:
                    self.progress.emit("Промт не изменился — мгновенный экспорт из кэша…")
                    cached = decrypt_blob(enc_pdf.read_bytes())
                    if cached[:4] == b"%PDF":
                        pdf_bytes = cached
                except Exception:
                    pdf_bytes = None

            if pdf_bytes is None:
                need_download = self.force or not enc_source.exists() or (
                    remote_sum and remote_sum != entry.get("server_checksum", "")
                )

                if need_download:
                    self.progress.emit("Скачивание зашифрованного промта…")
                    try:
                        if not self._download(sel, enc_source) and not enc_source.exists():
                            self.loaded.emit(False, "Сервер не отдал файл промта")
                            return
                    except Exception as e:
                        if not enc_source.exists():
                            self.loaded.emit(False, f"Нет связи с сервером: {e}")
                            return

                if not enc_source.exists():
                    self.loaded.emit(False, "Файл промта не найден локально")
                    return

                raw = enc_source.read_bytes()
                local_sum = sha256_bytes(raw)

                self.progress.emit("Расшифровка PDF промта…")
                decrypted = decrypt_data_auto(raw)
                if isinstance(decrypted, str):
                    decrypted = decrypted.encode("utf-8")

                if decrypted[:4] == b"%PDF":
                    pdf_bytes = bytes(decrypted)
                else:
                    if FPDF is None:
                        self.loaded.emit(False, "Устаревший текстовый промт на сервере, обновите его до PDF")
                        return
                    self.progress.emit("Старый формат промта — формирование PDF…")
                    pdf_bytes = text_to_pdf_bytes(decrypted, title=prompt_display_name(sel))

                decrypted = b""

                try:
                    enc_pdf.write_bytes(encrypt_blob(pdf_bytes))
                except Exception:
                    pass

                MANIFEST.put(
                    key,
                    server_checksum=remote_sum or entry.get("server_checksum", ""),
                    source_sha256=local_sum,
                    pdf_name=out_name,
                    pdf_sha256=sha256_bytes(pdf_bytes),
                    updated_at=int(time.time()),
                )

            pdf_path = WORKSPACE.write(out_name, pdf_bytes)
            template_path = self._prepare_template_file()

            self.progress.emit("Экспорт в чат…")
            self.loaded.emit(True, {"pdf": str(pdf_path), "template": template_path})

        except Exception as e:
            self.loaded.emit(False, f"Ошибка подготовки промта: {e}")


class LoginWindow(QDialog):
    login_success = pyqtSignal(dict, str, dict)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

        self.setWindowTitle("Legalyze — Авторизация")
        self.setFixedSize(380, 320)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        title = QLabel("Legalyze")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(QFont("Segoe UI", 20, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {THEME['accent']};")
        layout.addWidget(title)

        subtitle = QLabel("Вход в аккаунт")
        subtitle.setObjectName("Caption")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(6)

        self.login_input = QLineEdit()
        self.login_input.setPlaceholderText("Логин")
        self.login_input.setText(cfg.get("login", ""))
        layout.addWidget(self.login_input)

        self.pass_input = QLineEdit()
        self.pass_input.setPlaceholderText("Пароль")
        self.pass_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_input.setText(decrypt_secret(cfg.get("password", "")))
        layout.addWidget(self.pass_input)

        self.remember_cb = QCheckBox("Запомнить меня")
        self.remember_cb.setChecked(cfg.get("remember", False))
        layout.addWidget(self.remember_cb)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #f87171; font-size: 11px;")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setObjectName("Ghost")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)

        self.login_btn = QPushButton("Войти")
        self.login_btn.setObjectName("Primary")
        self.login_btn.clicked.connect(self._do_login)
        btn_layout.addWidget(self.login_btn)

        layout.addLayout(btn_layout)

        self.pass_input.returnPressed.connect(self._do_login)
        self.login_input.returnPressed.connect(self._do_login)

        self._worker = None
        self._hwid_worker = None
        self._auth_data = {}

    def _do_login(self):
        login = self.login_input.text().strip()
        password = self.pass_input.text().strip()

        if not login or not password:
            self.error_label.setText("Введите логин и пароль")
            return

        self.login_btn.setEnabled(False)
        self.error_label.setText("Подключение…")

        self._worker = AuthWorker(login, password, self.remember_cb.isChecked())
        self._worker.done.connect(self._on_auth)
        self._worker.start()

    def _on_auth(self, ok, err, data):
        self.login_btn.setEnabled(True)
        if not ok:
            self.error_label.setText(err)
            return

        self._auth_data = data
        token = data.get("token", "")
        user = data.get("user", {})

        if not token:
            self.error_label.setText("Ошибка: сервер не вернул токен")
            return

        cfg = self.cfg.copy()
        cfg["login"] = self.login_input.text().strip()
        cfg["token"] = token

        if self.remember_cb.isChecked():
            cfg["password"] = encrypt_secret(self.pass_input.text().strip())
            cfg["remember"] = True
        else:
            cfg["password"] = ""
            cfg["remember"] = False

        game_fields = {
            "Name": user.get("gameName", ""),
            "ID": user.get("gameId", ""),
            "Fraction": user.get("gameFraction", ""),
            "Rang": user.get("gameRang", ""),
            "Department": user.get("gameDepartment", ""),
            "JobTitle": user.get("gameJobTitle", ""),
        }
        local_template = cfg.get("template", {}) or {}
        for key, val in game_fields.items():
            if val and str(val).strip():
                local_template[key] = str(val).strip()
        cfg["template"] = normalize_template(local_template)

        save_config(cfg)
        try:
            write_template_file(local_template)
        except Exception:
            pass

        if not user.get("hwidBound"):
            hwid = generate_hwid()
            self.error_label.setText("Регистрация устройства…")
            self._hwid_worker = HWIDWorker(token, hwid)
            self._hwid_worker.done.connect(lambda ok2, err2: self._on_hwid(ok2, err2, cfg, user))
            self._hwid_worker.start()
        else:
            self.login_success.emit(cfg, token, user)
            self.accept()

    def _on_hwid(self, ok, err, cfg, user):
        if not ok:
            self.error_label.setText(f"HWID: {err}")
            return

        self.login_success.emit(cfg, cfg.get("token", ""), user)
        self.accept()


class TemplateWindow(QDialog):
    """Вкладка «Шаблон» — выровненная форма (ТЗ п.3)."""

    FIELDS = [
        ("Name", "Имя и фамилия", "Mike Macmillan"),
        ("ID", "ID", "85028"),
        ("Fraction", "Фракция", "SANG"),
        ("Rang", "Ранг", "11"),
        ("Department", "Отдел", "MP"),
        ("JobTitle", "Должность", "Заместитель командующего MP, сенатор NG"),
    ]

    def __init__(self, cfg, token=""):
        super().__init__()
        self.cfg = cfg
        self.token = token
        self.setWindowTitle("Шаблон данных")
        self.setFixedSize(560, 480)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(6)

        title = QLabel("Шаблон данных")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 19px; font-weight: 700; color: {THEME['accent']};")
        root.addWidget(title)

        hint = QLabel("Поля подставляются в промт перед экспортом в чат")
        hint.setObjectName("Caption")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)

        root.addSpacing(14)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.fields = {}
        template = cfg.get("template", {}) or {}

        for key, caption, placeholder in self.FIELDS:
            lbl = QLabel(f"{caption}")
            lbl.setMinimumWidth(130)
            lbl.setStyleSheet(f"color: {THEME['muted']}; font-size: 13px;")
            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            val = template.get(key, "")
            inp.setText(val if val is not None else "")
            inp.setMinimumHeight(40)
            inp.setMinimumWidth(300)
            self.fields[key] = inp
            form.addRow(lbl, inp)

        root.addLayout(form)
        root.addStretch()

        self.sync_label = QLabel("")
        self.sync_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sync_label.setStyleSheet("color: #f59e0b; font-size: 11px;")
        root.addWidget(self.sync_label)

        btn = QPushButton("Сохранить")
        btn.setObjectName("Primary")
        btn.setMinimumHeight(46)
        btn.setMinimumWidth(200)
        btn.clicked.connect(self._save)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.setMinimumHeight(46)
        cancel_btn.setMinimumWidth(200)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(btn)
        btn_layout.addStretch()
        root.addLayout(btn_layout)

    def _save(self):
        save_template(self.cfg, self.get_template())
        self._sync_to_server()
        self.accept()

    def _sync_to_server(self):
        if not self.token:
            return
        try:
            template = self.get_template()
            requests.post(
                f"{SERVER_URL}/api/client/template",
                json={"template": template},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
        except Exception:
            pass

    def get_template(self):
        return {k: v.text().strip() for k, v in self.fields.items()}


class HotkeyDialog(QDialog):
    def __init__(self, current_key, title="Горячая клавиша", taken=None, parent=None):
        super().__init__(parent)

        self.selected = current_key
        self.taken = set(taken or [])

        self.setWindowTitle(title)
        self.setFixedSize(360, 250)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        head = QLabel(title)
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {THEME['accent']};")
        layout.addWidget(head)

        self.capture_label = QLabel("Нажмите любую клавишу…")
        self.capture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.capture_label.setMinimumHeight(54)
        self.capture_label.setStyleSheet(
            f"background: {THEME['bg2']}; border: 1px dashed {THEME['accent']};"
            f"border-radius: 10px; font-size: 20px; font-weight: 700; color: {THEME['text']};"
        )
        layout.addWidget(self.capture_label)

        cap = QLabel("или выберите из списка")
        cap.setObjectName("Caption")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(cap)

        self.combo = QComboBox()
        self.combo.addItems(list(VK_MAP.keys()))
        if current_key in VK_MAP:
            self.combo.setCurrentText(current_key)
            self.capture_label.setText(current_key)
        self.combo.currentTextChanged.connect(self._on_combo)
        layout.addWidget(self.combo)

        self.error = QLabel("")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setStyleSheet("color: #f87171; font-size: 11px;")
        layout.addWidget(self.error)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        self.ok_btn = QPushButton("Применить")
        self.ok_btn.setObjectName("Primary")
        self.ok_btn.clicked.connect(self._accept_if_valid)
        btn_layout.addWidget(self.ok_btn)

        layout.addLayout(btn_layout)
        self.setFocus()

    def _on_combo(self, text):
        self.selected = text
        self.capture_label.setText(text)
        self._validate()

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return

        key = Qt.Key(event.key())
        if key in (Qt.Key.Key_Escape,):
            self.reject()
            return

        name = QT_KEY_TO_NAME.get(key)
        if not name:
            seq = QKeySequence(event.key()).toString().upper()
            name = seq if seq in VK_MAP else None

        if not name:
            self.error.setText("Эта клавиша не поддерживается")
            return

        self.selected = name
        self.capture_label.setText(name)
        self.combo.blockSignals(True)
        self.combo.setCurrentText(name)
        self.combo.blockSignals(False)
        self._validate()

    def _validate(self) -> bool:
        if self.selected in self.taken:
            self.error.setText("Эта клавиша уже занята другой функцией")
            self.ok_btn.setEnabled(False)
            return False
        self.error.setText("")
        self.ok_btn.setEnabled(True)
        return True

    def _accept_if_valid(self):
        if self.selected and self._validate():
            self.accept()

    def get_key(self):
        return self.selected


class HotkeyChip(QWidget):
    clicked = pyqtSignal()

    def __init__(self, caption: str, key: str, parent=None):
        super().__init__(parent)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)

        self.caption = QLabel(caption)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setStyleSheet(
            f"color: {THEME['muted']}; font-size: 9px; letter-spacing: 0.4px; border: none;"
        )
        lay.addWidget(self.caption)

        self.button = QPushButton(key)
        self.button.setFixedHeight(22)
        self.button.setMinimumWidth(40)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setStyleSheet(f"""
            QPushButton {{
                background: rgba(124, 131, 255, 0.16);
                color: #cdd2ff;
                border: 1px solid rgba(124, 131, 255, 0.55);
                border-radius: 6px;
                font-size: 11px;
                font-weight: 700;
                padding: 1px 8px;
            }}
            QPushButton:hover {{ background: rgba(124, 131, 255, 0.34); color: #fff; }}
        """)
        self.button.clicked.connect(self.clicked.emit)
        lay.addWidget(self.button)

    def set_key(self, key: str):
        self.button.setText(key)

    def set_active(self, active: bool):
        if not self.button.isEnabled():
            self.button.setStyleSheet("""
                QPushButton {
                    background: rgba(255, 255, 255, 0.05);
                    color: #555875;
                    border: 1px solid #262a4a;
                    border-radius: 6px;
                    font-size: 11px;
                    font-weight: 700;
                    padding: 1px 8px;
                }
            """)
            return

        color = THEME["ok"] if active else "rgba(124, 131, 255, 0.55)"
        bg = "rgba(52, 211, 153, 0.22)" if active else "rgba(124, 131, 255, 0.16)"
        self.button.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                color: #e6e8f5;
                border: 1px solid {color};
                border-radius: 6px;
                font-size: 11px;
                font-weight: 700;
                padding: 1px 8px;
            }}
            QPushButton:hover {{ background: rgba(124, 131, 255, 0.34); }}
        """)


class LoadingOverlay(QWidget):
    def __init__(self, parent_window):
        super().__init__(None)
        self._owner = parent_window
        self._angle = 0.0
        self._pulse = 0.0
        self._title = "Подготовка промта"
        self._message = "Пожалуйста, подождите…"
        self._error = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.BusyCursor)

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.setInterval(16)

    def show_state(self, title: str, message: str, error: bool = False):
        if (
            self.isVisible()
            and self._title == title
            and self._message == message
            and self._error == error
        ):
            if not self._anim.isActive():
                self._anim.start()
            return
        self._title = title
        self._message = message
        self._error = error
        self.sync_geometry()
        if not self.isVisible():
            self.show()
        if not self._anim.isActive():
            self._anim.start()
        force_topmost(int(self.winId()))
        self.update()

    def set_message(self, message: str):
        self._message = message
        self.update()

    def finish(self):
        self._anim.stop()
        self.hide()

    def sync_geometry(self):
        try:
            owner = self._owner
            if not owner:
                return
            g = owner.geometry()
            top = owner.top_inset
            bottom = owner.bottom_inset
            self.setGeometry(g.x(), g.y() + top, g.width(), g.height() - top - bottom)
        except Exception:
            pass

    def _tick(self):
        self._angle = (self._angle + 4.2) % 360.0
        self._pulse = (self._pulse + 0.035) % (2 * math.pi)
        self.update()

    def mousePressEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        p.fillRect(rect, QColor(9, 11, 24, 238))

        cx = rect.width() / 2
        cy = rect.height() / 2

        glow = QColor(124, 131, 255, 26)
        radius = 90 + 12 * math.sin(self._pulse)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(cx - radius, cy - radius - 40, radius * 2, radius * 2))

        card_w = min(rect.width() - 40, 340)
        card_h = 210
        card = QRectF(cx - card_w / 2, cy - card_h / 2, card_w, card_h)
        path = QPainterPath()
        path.addRoundedRect(card, 16, 16)
        p.setBrush(QBrush(QColor(21, 24, 48, 245)))
        p.setPen(QPen(QColor(38, 42, 74), 1))
        p.drawPath(path)

        spin_r = 26.0
        spin_rect = QRectF(cx - spin_r, card.top() + 34, spin_r * 2, spin_r * 2)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(42, 47, 84), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(spin_rect, 0, 360 * 16)

        accent = QColor(239, 68, 68) if self._error else QColor(124, 131, 255)
        p.setPen(QPen(accent, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span = int((110 + 40 * math.sin(self._pulse)) * 16)
        p.drawArc(spin_rect, int(-self._angle * 16), span)

        p.setPen(QColor(230, 232, 245))
        f = QFont("Segoe UI", 12, QFont.Weight.Bold)
        p.setFont(f)
        title_rect = QRectF(card.left() + 16, card.top() + 100, card.width() - 32, 26)
        p.drawText(title_rect, int(Qt.AlignmentFlag.AlignCenter), self._title)

        p.setPen(QColor(143, 150, 191))
        p.setFont(QFont("Segoe UI", 9))
        msg_rect = QRectF(card.left() + 18, card.top() + 128, card.width() - 36, 52)
        p.drawText(
            msg_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            self._message,
        )

        bar_w = card.width() - 44
        bar = QRectF(card.left() + 22, card.bottom() - 26, bar_w, 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(38, 42, 74))
        p.drawRoundedRect(bar, 2, 2)

        seg_w = bar_w * 0.32
        pos = (self._angle / 360.0) * (bar_w + seg_w) - seg_w
        seg = QRectF(bar.left() + max(0.0, pos), bar.top(),
                     min(seg_w, bar_w - max(0.0, pos)), bar.height())
        p.setBrush(accent)
        p.drawRoundedRect(seg, 2, 2)

        p.end()


class ChromeWorker(QThread):
    hwnd_ready = pyqtSignal(int)
    failed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc = None
        self.browser = None
        self.page = None

    def run(self):
        try:
            self.proc, port = launch_chrome()
            self.browser, self.page = attach_chrome(port)

            grant_mic_permission(self.browser, self.page)
            install_purge(self.page)

            # Ожидаем завершения загрузки базовой страницы
            for _ in range(150):
                try:
                    ready = self.page.eval("document.readyState", timeout=5).get("value")
                    url = self.page.eval("location.href", timeout=5).get("value", "")
                    if ready in ("interactive", "complete") and url and url != "about:blank":
                        break
                except Exception:
                    pass
                time.sleep(0.1)

            grant_mic_permission(self.browser, self.page)

            # Применяем масштаб 67%
            try:
                self.page.eval(JS_ZOOM, timeout=5)
            except Exception:
                pass

            hwnd = None
            for _ in range(200):
                pids = {self.proc.pid}
                pids.update(get_child_pids(self.proc.pid))
                hwnd = find_chrome_hwnd(pids)
                if hwnd:
                    break
                time.sleep(0.1)

            if hwnd:
                self.hwnd_ready.emit(int(hwnd))
            else:
                self.failed.emit()

        except Exception:
            self.failed.emit()


class UploadThread(QThread):
    """Держит в чате ОБА файла: PDF промт и Шаблон.txt (ТЗ 1.3)."""

    decrement_signal = pyqtSignal()
    attached = pyqtSignal(bool)

    def __init__(self, page, pdf_path, template_path, token):
        super().__init__()
        self.page = page
        self.pdf_path = pdf_path
        self.template_path = template_path
        self.token = token

        self._stop = False
        self.last_upload = 0
        self.files_loaded = False
        self._dedup_done = False
        self.fails = 0
        self.end_check_counter = 0

    def _missing_files(self, names):
        missing = []
        try:
            pdf_name = Path(self.pdf_path).name if self.pdf_path else ""
        except Exception:
            pdf_name = ""
        if pdf_name and self.pdf_path and not _chat_has_file(names, pdf_name):
            missing.append(self.pdf_path)
        if self.template_path and not _chat_has_file(names, TEMPLATE_EXPORT_NAME):
            missing.append(self.template_path)
        return missing

    def _has_both_files(self, names):
        try:
            pdf_name = Path(self.pdf_path).name if self.pdf_path else ""
        except Exception:
            pdf_name = ""
        return bool(
            pdf_name
            and _chat_has_file(names, pdf_name)
            and _chat_has_file(names, TEMPLATE_EXPORT_NAME)
        )

    def run(self):
        while not self._stop:
            try:
                pdf_ok = bool(self.pdf_path) and Path(self.pdf_path).exists()
                tpl_ok = bool(self.template_path) and Path(self.template_path).exists()
                if not self.page or not pdf_ok or not tpl_ok:
                    self.attached.emit(False)
                    time.sleep(0.2)
                    continue

                state = check_state(self.page)

                if state["uploading"]:
                    time.sleep(0.05)
                    continue

                present = int(state.get("present", 0) or 0)
                names = state.get("names", []) or []
                names_ok = any(str(n or "").strip() for n in names)

                if names_ok:
                    missing = self._missing_files(names)
                    complete = present >= REQUIRED_CHAT_FILES and not missing
                else:
                    missing = []
                    complete = present >= REQUIRED_CHAT_FILES

                if complete:
                    if (
                        names_ok
                        and not self._dedup_done
                        and present > REQUIRED_CHAT_FILES + 2
                        and self._has_both_files(names)
                    ):
                        self._dedup_done = True
                        self.files_loaded = False
                        self.attached.emit(False)
                        try:
                            self.page.eval(JS_REMOVE_ALL_FILES, timeout=3)
                        except Exception:
                            pass
                        self.last_upload = 0
                        time.sleep(0.8)
                        continue
                    if not self.files_loaded:
                        self.attached.emit(True)
                    self.files_loaded = True
                    self.fails = 0
                    time.sleep(0.15)
                    continue

                if self.files_loaded:
                    self.files_loaded = False
                    self.last_upload = 0
                    self.end_check_counter = 0
                    self.attached.emit(False)

                if (time.time() - self.last_upload) > 0.8:
                    time.sleep(random.uniform(0.05, 0.2))

                    if names_ok:
                        to_upload = list(missing) or (
                            [self.pdf_path, self.template_path]
                            if present < REQUIRED_CHAT_FILES else []
                        )
                    elif present == 0:
                        to_upload = [self.pdf_path, self.template_path]
                    else:
                        try:
                            self.page.eval(JS_REMOVE_ALL_FILES, timeout=3)
                        except Exception:
                            pass
                        self.last_upload = time.time()
                        time.sleep(0.6)
                        continue

                    if not to_upload:
                        time.sleep(0.15)
                        continue

                    ok = True
                    for path in to_upload:
                        if not upload_file_advanced(self.page, path):
                            ok = False
                            break
                        time.sleep(0.3)

                    if ok:
                        self.last_upload = time.time()
                        self.fails = 0
                        time.sleep(0.3)
                        continue

                    self.fails += 1
                    # Безопасный повтор без аварийного выхода из цикла
                    backoff = min(3.0, 0.5 + 0.3 * min(self.fails, 8))
                    time.sleep(backoff)
                    continue

                time.sleep(0.05)

            except Exception:
                time.sleep(0.2)

    def stop(self):
        self._stop = True


class PromptSelectionWindow(QDialog):
    def __init__(self, items, display_names, current_index, parent):
        super().__init__(parent)
        self.items = items
        self.choice = None

        self.setWindowTitle("Выбор промта")
        self.setFixedSize(280, 160)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Dialog
        )
        self.setStyleSheet(BASE_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel("Выберите рабочий промт")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {THEME['accent']};")
        layout.addWidget(title)

        self.combo = QComboBox()
        self.combo.addItems(display_names)
        self.combo.setCurrentIndex(current_index)
        self.combo.setMinimumHeight(38)
        layout.addWidget(self.combo)

        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        cancel_btn = QPushButton("Отмена")
        cancel_btn.setObjectName("Ghost")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("Выбрать")
        ok_btn.setObjectName("Primary")
        ok_btn.clicked.connect(self._on_accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)
        self.sync_position()

    def sync_position(self):
        parent = self.parentWidget()
        if parent:
            pg = parent.geometry()
            self.move(pg.x() - self.width() + 2, pg.y())

    def _on_accept(self):
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self.items):
            self.choice = self.items[idx]
        self.accept()


class MainWindow(QMainWindow):
    overlay_state = pyqtSignal(str, str, bool)

    def __init__(self, cfg, token, user_data):
        super().__init__()
        self.cfg = cfg
        self.token = token
        self.user_data = user_data or {}
        self.queries_remaining = int(self.user_data.get("queriesRemaining", 0) or 0)
        self.has_unlimited = bool(self.user_data.get("hasUnlimited", False))
        self.unlimited_until = str(self.user_data.get("unlimitedUntil") or "")

        self.mic_active = False
        self._mic_last_ts = 0.0
        self._mic_busy = False

        self.worker = None
        self.chrome_hwnd = None
        self.upload_thread = None
        self.pdf_path = None
        self.template_path = None
        self.prompt_loader = None
        self.pdf_attached = False
        self.pdf_was_loaded_once = False
        self._last_attached_state = None
        self._closing = False
        self._cleaned = False
        self._decrement_worker = None
        self._balance_worker = None
        self._hotkeys_registered = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowTitle("Legalyze AI")
        self.drag_position = QPoint()

        self.setFixedSize(W, H)
        self.setGeometry(X, Y, W, H)

        self.top_inset = 54
        self.bottom_inset = 30
        self.side_inset = 20

        self.central_widget = QWidget()
        self.central_widget.setStyleSheet(f"background: {THEME['bg']};")
        self.setCentralWidget(self.central_widget)

        self.browser_placeholder = QWidget(self.central_widget)
        self.browser_placeholder.setGeometry(0, 0, W, H)
        self.browser_placeholder.setStyleSheet(f"background: {THEME['bg']};")
        self.browser_cover = QWidget(self.browser_placeholder)
        self.browser_cover.setGeometry(0, 0, W, H)
        self.browser_cover.setStyleSheet(f"background: {THEME['bg']};")
        self.browser_cover.hide()

        self._build_chrome_ui()

        self.overlay = LoadingOverlay(self)
        self.overlay_state.connect(self._apply_overlay_state)
        self.overlay_state.emit("Инициализация", "Запуск защищённого браузера…", False)

        self._register_hotkeys()
        self._refresh_balance()
        self._sync_template_from_server()

        self.init_timer = QTimer(self)
        self.init_timer.timeout.connect(self._start_chrome)
        self.init_timer.setSingleShot(True)

        self._last_decremented_response_state = 0

        self.response_end_timer = QTimer(self)
        self.response_end_timer.timeout.connect(self._check_response_end_from_ui)
        self.response_end_timer.start(1000)

        self.topmost_timer = QTimer(self)
        self.topmost_timer.timeout.connect(self._enforce_topmost)
        self.topmost_timer.start(1200)

        self.mic_sync_timer = QTimer(self)
        self.mic_sync_timer.timeout.connect(self._sync_mic_state)
        self.mic_sync_timer.start(1500)

    def _build_chrome_ui(self):
        panel = f"background-color: {THEME['panel']};"
        side_height = H - self.top_inset - self.bottom_inset

        self.overlay_top = QWidget(self.central_widget)
        self.overlay_top.setGeometry(0, 0, W, self.top_inset)
        self.overlay_top.setStyleSheet(
            panel + f"border-bottom: 1px solid {THEME['line']};"
        )

        shadow = QGraphicsDropShadowEffect(self.overlay_top)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.overlay_top.setGraphicsEffect(shadow)

        icon_btn = f"""
            QPushButton {{
                background: rgba(255, 255, 255, 0.04);
                color: {THEME['text']};
                border: 1px solid {THEME['line']};
                border-radius: 7px;
                font-size: 11px;
                font-weight: 600;
                padding: 5px 9px;
            }}
            QPushButton:hover {{
                background: rgba(124, 131, 255, 0.24);
                border-color: {THEME['accent']};
                color: #ffffff;
            }}
            QPushButton:pressed {{ background: rgba(124, 131, 255, 0.4); }}
        """

        top_layout = QHBoxLayout(self.overlay_top)
        top_layout.setContentsMargins(12, 6, 10, 6)
        top_layout.setSpacing(6)

        brand = QVBoxLayout()
        brand.setSpacing(0)

        title_label = QLabel("Legalyze")
        title_label.setStyleSheet(
            f"color: {THEME['accent']}; font-weight: 800; font-size: 15px;"
            "letter-spacing: 0.3px; border: none;"
        )
        brand.addWidget(title_label)

        self.status_label = QLabel("подключение…")
        self.status_label.setStyleSheet(
            f"color: {THEME['muted']}; font-size: 9px; border: none;"
        )
        brand.addWidget(self.status_label)
        top_layout.addLayout(brand)

        top_layout.addStretch()

        self.chip_mic = HotkeyChip("Микрофон", self.cfg.get("hotkey_mic", "F3"))
        self.chip_mic.button.setEnabled(False)
        self.chip_mic.clicked.connect(self._choose_mic_hotkey)
        top_layout.addWidget(self.chip_mic)

        self.chip_toggle = HotkeyChip("Окно", self.cfg.get("hotkey_toggle", "F2"))
        self.chip_toggle.clicked.connect(self._choose_hotkey)
        top_layout.addWidget(self.chip_toggle)

        sep = QLabel("")
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background: {THEME['line']}; border: none;")
        top_layout.addWidget(sep)

        self.btn_template = QPushButton("Шаблон")
        self.btn_template.setToolTip("Данные шаблона")
        self.btn_template.setStyleSheet(icon_btn)
        self.btn_template.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_template.clicked.connect(self._open_template)
        top_layout.addWidget(self.btn_template)

        self.btn_prompt = QPushButton("Промт")
        self.btn_prompt.setToolTip("Выбор промта")
        self.btn_prompt.setStyleSheet(icon_btn)
        self.btn_prompt.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prompt.clicked.connect(self._choose_prompt)
        top_layout.addWidget(self.btn_prompt)

        btn_restart = QPushButton("⟳")
        btn_restart.setToolTip("Перезапуск")
        btn_restart.setStyleSheet(icon_btn)
        btn_restart.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_restart.clicked.connect(self._restart)
        top_layout.addWidget(btn_restart)

        btn_close = QPushButton("✕")
        btn_close.setToolTip("Закрыть")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: rgba(239, 68, 68, 0.16);
                color: #fca5a5;
                border: 1px solid rgba(239, 68, 68, 0.5);
                border-radius: 7px;
                font-size: 12px;
                font-weight: 700;
                padding: 5px 10px;
            }}
            QPushButton:hover {{ background: rgba(239, 68, 68, 0.55); color: #fff; }}
        """)
        btn_close.clicked.connect(self._close_app)
        top_layout.addWidget(btn_close)

        self.overlay_left = QWidget(self.central_widget)
        self.overlay_left.setGeometry(0, self.top_inset, self.side_inset, side_height)
        self.overlay_left.setStyleSheet(panel + f"border-right: 1px solid {THEME['line']};")

        self.overlay_right = QWidget(self.central_widget)
        self.overlay_right.setGeometry(W - self.side_inset, self.top_inset, self.side_inset, side_height)
        self.overlay_right.setStyleSheet(panel + f"border-left: 1px solid {THEME['line']};")

        self.overlay_bottom = QWidget(self.central_widget)
        self.overlay_bottom.setGeometry(0, H - self.bottom_inset, W, self.bottom_inset)
        self.overlay_bottom.setStyleSheet(panel + f"border-top: 1px solid {THEME['line']};")

        bottom_layout = QHBoxLayout(self.overlay_bottom)
        bottom_layout.setContentsMargins(12, 4, 12, 4)
        bottom_layout.setSpacing(8)

        self.query_label = QLabel()
        self.query_label.setStyleSheet(
            f"color: {THEME['text']}; font-size: 11px; font-weight: 600; border: none;"
        )
        bottom_layout.addWidget(self.query_label)

        bottom_layout.addStretch()

        self.prompt_label = QLabel("промт: —")
        self.prompt_label.setStyleSheet(f"color: {THEME['muted']}; font-size: 10px; border: none;")
        bottom_layout.addWidget(self.prompt_label)

        self.dot_label = QLabel("●")
        self.dot_label.setStyleSheet("color: #f59e0b; font-size: 11px; border: none;")
        bottom_layout.addWidget(self.dot_label)

        self._update_query_label()
        self._update_prompt_label()

        for w in (self.overlay_top, self.overlay_left, self.overlay_right, self.overlay_bottom):
            w.raise_()

    def _update_query_label(self):
        if self.has_unlimited:
            date_str = ""
            if self.unlimited_until:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(
                        self.unlimited_until.replace("Z", "+00:00")
                    )
                    date_str = dt.strftime("%d.%m.%Y")
                except Exception:
                    date_str = ""
            if date_str:
                self.query_label.setText(f"Безлимит до {date_str}")
            else:
                self.query_label.setText("Безлимит")
        else:
            self.query_label.setText(f"Запросы:  {self.queries_remaining}")

    def _update_prompt_label(self):
        sel = self.cfg.get("selected_prompt", "")
        self.prompt_label.setText(f"промт: {prompt_display_name(sel) if sel else '—'}")

    def _set_status(self, text: str, color: str = None):
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color: {color or THEME['muted']}; font-size: 9px; border: none;"
        )

    def _set_dot(self, color: str):
        self.dot_label.setStyleSheet(f"color: {color}; font-size: 11px; border: none;")

    def _apply_overlay_state(self, title, message, error):
        if not title:
            self.overlay.finish()
            return
        self.overlay.show_state(title, message, error)

    def _show_overlay(self, title, message, error=False):
        self.browser_cover.show()
        self.overlay_state.emit(title, message, error)

    def _hide_overlay(self):
        self.overlay_state.emit("", "", False)
        self.browser_cover.hide()

    def _enforce_topmost(self):
        if self._closing or not self.isVisible():
            return

        hwnd = int(self.winId())
        force_topmost(hwnd)

        if self.overlay.isVisible():
            self.overlay.sync_geometry()
            force_topmost(int(self.overlay.winId()))

    def moveEvent(self, event):
        super().moveEvent(event)
        try:
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_chrome_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_chrome_geometry()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._sync_chrome_geometry()

    def _sync_chrome_geometry(self):
        """
        Адаптация встроенного браузера под текущее разрешение и DPI-масштаб Windows (ТЗ п.4).
        Использует GetClientRect для точного соответствия физическим пикселям контейнера.
        """
        if not getattr(self, "chrome_hwnd", None) or not self.browser_placeholder or not user32:
            return
        try:
            parent_hwnd = int(self.browser_placeholder.winId())
            if not parent_hwnd:
                return

            rect = RECT()
            if user32.GetClientRect(wintypes.HWND(parent_hwnd), ctypes.byref(rect)):
                p_width = rect.right - rect.left
                p_height = rect.bottom - rect.top
            else:
                dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
                p_width = int(self.browser_placeholder.width() * dpr)
                p_height = int(self.browser_placeholder.height() * dpr)

            dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
            x_offset = int(-10 * dpr)
            chrome_w = p_width - x_offset + int(10 * dpr)
            chrome_h = p_height

            user32.SetWindowPos(
                wintypes.HWND(int(self.chrome_hwnd)),
                wintypes.HWND(HWND_TOP),
                x_offset, 0,
                chrome_w,
                chrome_h,
                SWP_FRAMECHANGED | SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def _register_hotkeys(self):
        if not user32:
            return False
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return False

            self._unregister_hotkeys()

            toggle_key = self.cfg.get("hotkey_toggle", "F2")
            mic_key = self.cfg.get("hotkey_mic", "F3")

            vk_toggle = VK_MAP.get(toggle_key, 0x71)
            vk_mic = VK_MAP.get(mic_key, 0x72)

            ok1 = bool(user32.RegisterHotKey(
                wintypes.HWND(hwnd), HOTKEY_TOGGLE_ID, MOD_NOREPEAT, vk_toggle))
            ok2 = bool(user32.RegisterHotKey(
                wintypes.HWND(hwnd), HOTKEY_MIC_ID, MOD_NOREPEAT, vk_mic))

            self._hotkeys_registered = ok1 and ok2

            if not self._hotkeys_registered:
                self._set_status("горячие клавиши заняты другой программой", "#f59e0b")
            return self._hotkeys_registered
        except Exception:
            return False

    def _unregister_hotkeys(self):
        if not user32:
            return
        try:
            hwnd = wintypes.HWND(int(self.winId()))
            user32.UnregisterHotKey(hwnd, HOTKEY_TOGGLE_ID)
            user32.UnregisterHotKey(hwnd, HOTKEY_MIC_ID)
        except Exception:
            pass
        self._hotkeys_registered = False

    def _change_hotkey(self, cfg_key: str, default: str, caption: str, chip: HotkeyChip, other_cfg_key: str):
        current = self.cfg.get(cfg_key, default)
        taken = {self.cfg.get(other_cfg_key, "")} - {current}

        self._unregister_hotkeys()
        try:
            dlg = HotkeyDialog(current, title=caption, taken=taken, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                key = dlg.get_key()
                if key and key in VK_MAP:
                    self.cfg[cfg_key] = key
                    save_config(self.cfg)
                    chip.set_key(key)
        finally:
            if self._register_hotkeys():
                self._set_status("готово", THEME["ok"])

    def _choose_hotkey(self):
        self._change_hotkey(
            "hotkey_toggle", "F2", "Клавиша: скрыть/показать окно",
            self.chip_toggle, "hotkey_mic",
        )

    def _choose_mic_hotkey(self):
        self._change_hotkey(
            "hotkey_mic", "F3", "Клавиша: микрофон",
            self.chip_mic, "hotkey_toggle",
        )

    def nativeEvent(self, eventType, message):
        try:
            et = bytes(eventType)
        except Exception:
            et = eventType

        if et == b"windows_generic_MSG":
            try:
                msg_ptr = int(message)
                if msg_ptr:
                    msg = wintypes.MSG.from_address(msg_ptr)

                    if msg.message == WM_HOTKEY:
                        if msg.wParam == HOTKEY_TOGGLE_ID:
                            self._toggle_visibility()
                            return True, 0

                        if msg.wParam == HOTKEY_MIC_ID:
                            self._toggle_mic()
                            return True, 0
            except Exception:
                pass

        try:
            result = super().nativeEvent(eventType, message)
            if isinstance(result, tuple) and len(result) == 2:
                return bool(result[0]), int(result[1] or 0)
            return False, 0
        except Exception:
            return False, 0

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
            if self.overlay.isVisible():
                self.overlay.hide()
        else:
            self.show()
            self.activateWindow()
            force_topmost(int(self.winId()))
            if self.overlay._anim.isActive():
                self.overlay.show()
                self.overlay.sync_geometry()
                force_topmost(int(self.overlay.winId()))

    def _page_eval_async(self, expression, timeout=3, callback=None):
        page = getattr(self.worker, "page", None) if self.worker else None
        if not page:
            return

        def task():
            value = None
            try:
                value = page.eval(expression, timeout=timeout).get("value")
            except Exception:
                value = None
            if callback:
                try:
                    callback(value)
                except Exception:
                    pass

        threading.Thread(target=task, daemon=True).start()

    def _toggle_mic(self):
        if not self.chip_mic.button.isEnabled():
            return

        if not self.worker or not getattr(self.worker, "page", None):
            return

        now = time.monotonic()
        if self._mic_busy or (now - self._mic_last_ts) < 0.45:
            return

        self._mic_last_ts = now
        self._mic_busy = True

        def release(_value=None):
            self._mic_busy = False

        if not self.mic_active:
            self.mic_active = True
            self.chip_mic.set_active(True)
            self._set_status("запись… (нажмите ещё раз для отправки)", THEME["ok"])
            self._page_eval_async(JS_CLICK_MIC, 3, release)
        else:
            self.mic_active = False
            self.chip_mic.set_active(False)
            self._set_status("отправка запроса…", THEME["accent"])
            self._page_eval_async(JS_CLICK_SEND, 3, release)

    def _sync_mic_state(self):
        if self._mic_busy or not self.worker or not getattr(self.worker, "page", None):
            return

        def apply(value):
            if not isinstance(value, dict):
                return
            recording = bool(value.get("recording"))
            if recording != self.mic_active:
                self.mic_active = recording
                self.chip_mic.set_active(recording)

        self._page_eval_async(JS_MIC_STATE, 2, apply)

    def _open_template(self):
        dlg = TemplateWindow(self.cfg, token=self.token)
        if dlg.exec():
            save_template(self.cfg, dlg.get_template())
            self._reload_prompt()

    def _sync_template_from_server(self):
        if not self.token:
            return
        try:
            res = requests.get(
                f"{SERVER_URL}/api/client/template",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=8,
            )
            data = res.json()
            if data.get("success") and data.get("data", {}).get("template"):
                server_template = data["data"]["template"]
                local_template = self.cfg.get("template", {}) or {}
                changed = False
                for key in ("Name", "ID", "Fraction", "Rang", "Department", "JobTitle"):
                    server_val = str(server_template.get(key, "") or "").strip()
                    local_val = str(local_template.get(key, "") or "").strip()
                    if server_val and server_val != local_val:
                        local_template[key] = server_val
                        changed = True
                if changed:
                    save_template(self.cfg, local_template)
        except Exception:
            pass

    def _choose_prompt(self):
        try:
            res = requests.get(
                f"{SERVER_URL}/api/client/prompts",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            data = res.json()

            if not data.get("success") or not data.get("data"):
                QMessageBox.information(self, "Промты", "Нет доступных промтов на сервере")
                return

            files = data.get("data", [])
            items = [f.get("filename", "") for f in files if f.get("filename")]

            if not items:
                QMessageBox.information(self, "Промты", "Нет доступных промтов на сервере")
                return

            display = [prompt_display_name(i) for i in items]
            current = self.cfg.get("selected_prompt", "")
            idx = items.index(current) if current in items else 0

            overlay_was_visible = self.overlay.isVisible()
            if overlay_was_visible:
                self.overlay.hide()

            dlg = PromptSelectionWindow(items, display, idx, self)
            dialog_result = dlg.exec()

            if dialog_result != QDialog.DialogCode.Accepted or not dlg.choice:
                if overlay_was_visible:
                    self.overlay.show()
                    self.overlay.sync_geometry()
                    force_topmost(int(self.overlay.winId()))
                return

            item = dlg.choice

            res2 = requests.post(
                f"{SERVER_URL}/api/client/prompts",
                json={"filename": item},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )

            if res2.json().get("success"):
                self.cfg["selected_prompt"] = item
                save_config(self.cfg)
                self._update_prompt_label()
                self._reload_prompt()
            else:
                QMessageBox.warning(self, "Ошибка", "Сервер не подтвердил выбор промта")
                if overlay_was_visible:
                    self.overlay.show()

        except Exception as e:
            QMessageBox.warning(self, "Ошибка", str(e))
            if 'overlay_was_visible' in locals() and overlay_was_visible:
                self.overlay.show()

    def _reload_prompt(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return

        self.pdf_attached = False
        self.pdf_was_loaded_once = False
        self._last_attached_state = None
        self._set_dot("#f59e0b")
        self._show_overlay("Обновление файлов", "Очистка предыдущих файлов…")
        self._page_eval_async(JS_REMOVE_ALL_FILES, 3)
        QTimer.singleShot(650, lambda: self._load_prompt(force=True))

    def _start_chrome(self):
        self._show_overlay("Инициализация", "Запуск защищённого браузера…")
        self.worker = ChromeWorker()
        self.worker.hwnd_ready.connect(self._on_hwnd)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_hwnd(self, hwnd):
        try:
            if not hwnd:
                self._on_failed()
                return

            self.chrome_hwnd = hwnd
            self.mic_active = False
            self.chip_mic.set_active(False)

            if user32:
                parent_hwnd = int(self.browser_placeholder.winId())
                if parent_hwnd:
                    user32.SetParent(hwnd, parent_hwnd)

                style = get_window_long(hwnd, GWL_STYLE)
                style &= ~(
                    WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME |
                    WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_DLGFRAME | WS_BORDER
                )
                style |= (WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
                style &= 0xFFFFFFFF
                set_window_long(hwnd, GWL_STYLE, style)

                exstyle = get_window_long(hwnd, GWL_EXSTYLE)
                exstyle &= ~(
                    WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE |
                    WS_EX_CLIENTEDGE | WS_EX_STATICEDGE | WS_EX_APPWINDOW
                )
                exstyle |= WS_EX_TOOLWINDOW
                exstyle &= 0xFFFFFFFF
                set_window_long(hwnd, GWL_EXSTYLE, exstyle)

                user32.ShowWindow(hwnd, SW_SHOW)
                self._sync_chrome_geometry()

            self.browser_placeholder.update()

            force_topmost(int(self.winId()))
            self._set_status("браузер готов", THEME["ok"])

            self._load_prompt()

        except Exception:
            self._on_failed()

    def _load_prompt(self, force=False) -> bool:
        sel = self.cfg.get("selected_prompt", "")
        if not sel:
            self._show_overlay(
                "Промт не выбран",
                "Нажмите «Промт» в верхней панели и выберите файл — без него "
                "отправка запросов заблокирована.",
                error=True,
            )
            return False

        if self.prompt_loader and self.prompt_loader.isRunning():
            return True

        self._last_attached_state = None
        self._show_overlay("Подготовка промта", "Проверка версии на сервере…")
        self._set_dot("#f59e0b")

        self.prompt_loader = PromptLoaderWorker(self.cfg, self.token, force=force)
        self.prompt_loader.progress.connect(lambda m: self.overlay.set_message(m))
        self.prompt_loader.loaded.connect(self._on_prompt_loaded)
        self.prompt_loader.start()
        return True

    def _on_prompt_loaded(self, success, payload):
        if success and isinstance(payload, dict):
            self.pdf_path = Path(payload.get("pdf", ""))
            self.template_path = Path(payload.get("template", "") or str(TEMPLATE_FILE))
            self.overlay.set_message(
                f"Экспорт «{self.pdf_path.name}» + «{TEMPLATE_EXPORT_NAME}» в чат…"
            )
            self._start_upload_thread()
        else:
            self.pdf_attached = False
            self._set_dot(THEME["danger"])
            self._show_overlay("Промт недоступен", str(payload), error=True)
            QTimer.singleShot(8000, lambda: self._load_prompt(force=True))

    def _start_upload_thread(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return

        if not self.pdf_path or not Path(self.pdf_path).exists():
            self._load_prompt(force=True)
            return
        if not self.template_path or not Path(self.template_path).exists():
            self._load_prompt(force=True)
            return

        if self.upload_thread and self.upload_thread.isRunning():
            self.upload_thread.pdf_path = self.pdf_path
            self.upload_thread.template_path = self.template_path
            self.upload_thread.fails = 0
            self.upload_thread.last_upload = 0
            self.upload_thread.end_check_counter = 0
            self.upload_thread.files_loaded = False
            self.upload_thread._dedup_done = False
            return

        if self.upload_thread:
            try:
                self.upload_thread.wait(100)
            except Exception:
                pass

        self.upload_thread = UploadThread(
            self.worker.page, self.pdf_path, self.template_path, self.token
        )
        self.upload_thread.decrement_signal.connect(self._on_decrement)
        self.upload_thread.attached.connect(self._on_files_attached)
        self.upload_thread.start()

    def _on_files_attached(self, attached: bool):
        attached = bool(attached)
        if attached == self._last_attached_state:
            return
        self._last_attached_state = attached
        self.pdf_attached = attached

        if attached:
            self.pdf_was_loaded_once = True
            self._set_dot(THEME["ok"])
            self._set_status("промт + шаблон подключены", THEME["ok"])
            self._hide_overlay()
        else:
            self._set_dot("#f59e0b")
            if not self.pdf_was_loaded_once:
                if self.pdf_path and Path(self.pdf_path).exists():
                    self._show_overlay(
                        "Экспорт файлов...",
                        f"Прикрепляем «{Path(self.pdf_path).name}» и «{TEMPLATE_EXPORT_NAME}». "
                        "Ввод заблокирован, чтобы запрос не ушёл без файлов.",
                    )
            else:
                self._set_status("переприкрепление файлов...", "#f59e0b")

    def _refresh_balance(self):
        self._balance_worker = BalanceWorker(self.token)
        self._balance_worker.done.connect(self._on_balance)
        self._balance_worker.start()

    def _on_balance(self, ok, remaining, unlimited, unlimited_until):
        if ok:
            self.queries_remaining = int(remaining or 0)
            self.has_unlimited = bool(unlimited) or self.has_unlimited
            if unlimited_until:
                self.unlimited_until = unlimited_until
            self._update_query_label()

        if not self.has_unlimited and self.queries_remaining <= 0:
            self._set_dot(THEME["danger"])
            self._show_overlay(
                "Запросы исчерпаны",
                "На вашем аккаунте 0 доступных запросов. Доступ заблокирован.",
                error=True
            )
            return

        if hasattr(self, 'init_timer'):
            self.init_timer.start(50)

    def _on_decrement(self):
        self._decrement_worker = DecrementWorker(self.token)
        self._decrement_worker.done.connect(self._on_decrement_finished)
        self._decrement_worker.start()

    def _check_response_end_from_ui(self):
        if not self.worker or not getattr(self.worker, "page", None):
            return

        try:
            r = self.worker.page.eval(JS_CHECK_END, timeout=1)
            current_markers_count = int(r.get("value") if isinstance(r, dict) else (r or 0))

            if not hasattr(self, "_first_check_done"):
                self._first_check_done = True
                self._last_decremented_response_state = current_markers_count
                return

            if current_markers_count > self._last_decremented_response_state:
                self._last_decremented_response_state = current_markers_count
                self._on_decrement()
            elif current_markers_count < self._last_decremented_response_state:
                self._last_decremented_response_state = current_markers_count
        except Exception:
            pass

    def _on_decrement_finished(self, ok, remaining):
        if ok:
            self.queries_remaining = int(remaining or 0)
            self._update_query_label()

            if not self.has_unlimited and self.queries_remaining <= 0:
                QMessageBox.critical(
                    self,
                    "Запросы исчерпаны",
                    "Доступ к ИИ приостановлен. Пополните баланс для продолжения работы."
                )
                self._close_app()

    def _on_failed(self):
        if self._closing:
            return

        QMessageBox.critical(
            self,
            "Ошибка",
            "Не удалось запустить браузер.\nПроверьте папку «chromium» рядом с программой."
        )
        self._close_app()

    def _cleanup(self):
        if self._cleaned:
            return

        self._cleaned = True

        try:
            self.overlay.finish()
            self.overlay.deleteLater()
        except Exception:
            pass

        try:
            if self.upload_thread:
                self.upload_thread.stop()
                self.upload_thread.wait(1500)
        except Exception:
            pass

        try:
            if self.prompt_loader and self.prompt_loader.isRunning():
                self.prompt_loader.wait(1000)
        except Exception:
            pass

        worker = self.worker

        if worker:
            try:
                if getattr(worker, "browser", None):
                    try:
                        worker.browser.send("Browser.close", timeout=3)
                    except Exception:
                        pass
                    try:
                        worker.browser.close()
                    except Exception:
                        pass

                if getattr(worker, "page", None):
                    try:
                        worker.page.close()
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                if getattr(worker, "proc", None):
                    worker.proc.terminate()
                    try:
                        worker.proc.wait(timeout=1)
                    except Exception:
                        worker.proc.kill()
            except Exception:
                pass

        self._unregister_hotkeys()

        try:
            if self.pdf_path and WORKSPACE.path in Path(self.pdf_path).parents:
                shred_file(self.pdf_path)
        except Exception:
            pass

        try:
            WORKSPACE.dispose()
            purge_plaintext_artifacts(DATA_DIR)
        except Exception:
            pass

    def _close_app(self):
        if self._closing:
            return

        self._closing = True
        self._cleanup()

        try:
            QApplication.instance().quit()
        except Exception:
            pass

        os._exit(0)

    def _restart(self):
        if self._closing:
            return

        self._closing = True
        self._cleanup()
        restart_app()
        os._exit(0)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if event.position().y() <= self.top_inset:
                self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and not self.drag_position.isNull():
            self.move(event.globalPosition().toPoint() - self.drag_position)
            if self.overlay.isVisible():
                self.overlay.sync_geometry()
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_position = QPoint()
        event.accept()

    def closeEvent(self, event):
        self._close_app()
        event.accept()


def _migrate_config_secrets(cfg: dict) -> dict:
    try:
        pwd = cfg.get("password", "")
        if pwd and not pwd.startswith(("lgz1:", "lgz2:")):
            cfg["password"] = encrypt_secret(pwd)
            save_config(cfg)
    except Exception:
        pass
    return cfg


def main():
    cfg = load_config()
    cfg = _migrate_config_secrets(cfg)
    try:
        ensure_template_file(cfg.get("template", {}))
    except Exception:
        pass
    purge_plaintext_artifacts(DATA_DIR)

    app = QApplication(sys.argv)
    app.setStyleSheet(BASE_QSS)

    if not resolve_browser_path():
        QMessageBox.critical(
            None,
            "Ошибка",
            "Браузер Chromium не найден.\n\n"
            "Положите папку «chromium» с файлом chrome.exe рядом с программой.",
        )
        sys.exit(1)

    login_win = LoginWindow(cfg)
    result = {"cfg": cfg, "token": cfg.get("token", ""), "user": {}}

    def on_success(c, t, u):
        result["cfg"] = c
        result["token"] = t
        result["user"] = u

    login_win.login_success.connect(on_success)
    if login_win.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)

    cfg = result["cfg"]
    token = result["token"]
    user_data = result["user"]

    _enforce_latest_version(token)

    if not get_template_filled(cfg):
        tpl_win = TemplateWindow(cfg, token=token)
        if tpl_win.exec() == QDialog.DialogCode.Accepted:
            save_template(cfg, tpl_win.get_template())

    window = MainWindow(cfg, token, user_data)
    window.show()
    force_topmost(int(window.winId()))

    sys.exit(app.exec())


def _enforce_latest_version(token: str):
    update_info = None
    try:
        update_info = check_for_update(token)
    except Exception:
        pass
    if not update_info:
        return
    server_version = str(update_info.get("version", "") or "")
    QMessageBox.warning(
        None,
        "Доступна новая версия",
        f"Ваша версия: {CURRENT_VERSION}\n"
        f"Новая версия: {server_version}\n\n"
        "Скачайте новую версию с сайта и вручную замените файлы.\n"
        "Программа будет закрыта.",
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
