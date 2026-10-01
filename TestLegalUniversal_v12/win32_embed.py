"""Embedding of an out-of-process Chromium/Chrome window into the Qt GUI.

Why this module exists
----------------------
The page's own microphone is a *browser* feature (Web Speech API and/or
getUserMedia streamed to Google). QtWebEngine cannot provide it: its
`SpeechRecognition` crashes the renderer (no Google API key in the build) and
`getUserMedia` is unavailable (QTBUG-55108). Windows System.Speech — our v1..v7
bridge — is not what the user asked for and is not the page's own voice input.

So the browser must be a real, branded Chromium running as its own process.
`release/` did exactly that and the microphone worked; what it did *not* get
right is stability. Two of the three reported failures are windowing bugs that
this module fixes:

* **"wide window with a single ИИ label"** — the child window was positioned
  with `GetClientRect` (PHYSICAL pixels) but Chromium is per-monitor DPI aware
  (PMv2) and reads window sizes as DIPs. On any display scaled to 125%/150%
  the browser therefore believed it was ~1.5x wider than it really was, laid
  the page out in the wide desktop variant and showed the centred wordmark
  instead of the chat. Sizing alone cannot fix this at every DPI, so the
  layout is *pinned* over CDP (`Emulation.setDeviceMetricsOverride`) and the
  window is only responsible for pixels.
* **"chrome starts, hangs, does nothing"** — window discovery raced window
  creation, silently took the first `Chrome_WidgetWin_*` it saw (helper
  surfaces included) and, when `SetParent` failed, rolled back to a 900x700
  window at (80,80) — i.e. a detached browser the user could not use.

Everything here is *checked*: every Win32 call reports its result to the
diagnostics log, `SetParent` is verified with `GetParent`, and any failure is
rolled back so the user is never left with an invisible or detached browser.
"""
import ctypes
import time
from ctypes import wintypes

import diagnostics as diag

# ----------------------------------------------------------------- constants
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_DLGFRAME = 0x00400000
WS_BORDER = 0x00800000
WS_CLIPCHILDREN = 0x02000000
WS_CLIPSIBLINGS = 0x04000000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020
SWP_ASYNCWINDOWPOS = 0x4000

BROWSER_WINDOW_CLASS = "Chrome_WidgetWin_1"
try:  # WINFUNCTYPE exists only on Windows; the module must still import
      # elsewhere so the test-suite can check its logic.
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
except Exception:  # pragma: no cover - Linux/macOS
    WNDENUMPROC = None


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _set_last_error(value=0):
    """ctypes.set_last_error/get_last_error exist only on Windows."""
    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        setter(value)


def _get_last_error():
    getter = getattr(ctypes, "get_last_error", None)
    return getter() if getter is not None else 0


def _win_error(code, message):
    factory = getattr(ctypes, "WinError", None)
    if factory is not None:
        return factory(code, message)
    return OSError(code, message)


def _user32():
    try:
        return ctypes.WinDLL("user32", use_last_error=True)
    except Exception:
        diag.exception("win32_embed.user32")
        return None


def configure(user32):
    """Declare argtypes/restype: on 64-bit Windows the default int conversions
    truncate HWND/HANDLE and every call silently fails (release-era bug)."""
    if not user32:
        return
    for name, args, result in (
        ("GetParent", [wintypes.HWND], wintypes.HWND),
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
        ("SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND),
        ("ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        ("GetClientRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL),
        ("SetWindowPos", [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        ("MoveWindow", [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, wintypes.BOOL], wintypes.BOOL),
        ("EnumWindows", [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
        ("GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
         wintypes.DWORD),
        ("SetFocus", [wintypes.HWND], wintypes.HWND),
        ("GetFocus", [], wintypes.HWND),
        ("GetForegroundWindow", [], wintypes.HWND),
        ("IsChild", [wintypes.HWND, wintypes.HWND], wintypes.BOOL),
        ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
    ):
        try:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, result
        except Exception:
            pass
    for name, args, result in (
        ("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, ctypes.c_void_p], ctypes.c_void_p),
        ("GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_void_p),
        ("GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
    ):
        try:
            fn = getattr(user32, name)
            fn.argtypes, fn.restype = args, result
        except Exception:
            pass


def get_window_long(user32, hwnd, index):
    getter = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _set_last_error(0)
    value = getter(hwnd, index)
    return int(value) & 0xFFFFFFFFFFFFFFFF if value else 0


def set_window_long(user32, hwnd, index, value):
    setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    _set_last_error(0)
    previous = setter(hwnd, index, ctypes.c_void_p(value & 0xFFFFFFFFFFFFFFFF))
    error = _get_last_error()
    diag.event("win32.style", hwnd=int(hwnd), index=index,
               previous=int(previous) & 0xFFFFFFFFFFFFFFFF if previous else 0,
               new=value, error=error)
    if not previous and error:
        raise _win_error(error, 'win32 style change failed')
    return int(previous) & 0xFFFFFFFFFFFFFFFF if previous else 0


def snapshot(user32, hwnd):
    rect = RECT()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    info = {"hwnd": int(hwnd), "pid": pid.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "style": get_window_long(user32, hwnd, GWL_STYLE),
            "exstyle": get_window_long(user32, hwnd, GWL_EXSTYLE),
            "parent": int(user32.GetParent(hwnd) or 0)}
    try:
        info["dpi"] = int(user32.GetDpiForWindow(hwnd))
    except Exception:
        pass
    return info


def class_name(user32, hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def client_size(user32, hwnd):
    rect = RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0)
    return (rect.right - rect.left, rect.bottom - rect.top)


def eligible(info, name):
    """Only the real app window: never a hidden helper/startup surface."""
    left, top, right, bottom = info["rect"]
    return (name == BROWSER_WINDOW_CLASS and info["visible"] and not info["parent"]
            and right - left >= 120 and bottom - top >= 120)


def _windows_of(user32, pids):
    found = []

    def callback(hwnd, _lparam):
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            name = class_name(user32, hwnd)
            if name not in (BROWSER_WINDOW_CLASS, "Chrome_WidgetWin_0"):
                return True
            info = snapshot(user32, hwnd)
            info["class"] = name
            found.append(info)
        except Exception:
            diag.exception("win32_embed.enum_callback")
        return True

    _set_last_error(0)
    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def child_pids(pid):
    """Direct children via Toolhelp32: no wmic, no subprocess, no locale."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
    except Exception:
        return set()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot:
        return set()
    found = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32ParentProcessID) == int(pid):
                    found.add(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    except Exception:
        diag.exception("win32_embed.child_pids")
    finally:
        try:
            kernel32.CloseHandle(snapshot)
        except Exception:
            pass
    return found


def process_tree(pids):
    return set(int(p) for p in pids or ())


def find_browser_window(user32, pids, timeout=25.0, poll=0.2, extra_pids=None):
    """Wait for the app window instead of racing it.

    Chromium creates its window asynchronously and opens helper surfaces first.
    The release build sampled once and happily embedded a helper, or failed and
    left a detached 900x700 browser behind.
    """
    if not user32:
        return None
    wanted = process_tree(pids)
    deadline = time.monotonic() + max(0.5, timeout)
    best = None
    seen = []
    while time.monotonic() < deadline:
        windows = _windows_of(user32, wanted)
        if windows and not seen:
            seen = windows
        candidates = [w for w in windows if eligible(w, w["class"])]
        if candidates:
            # Largest eligible surface: the app window, not a transient popup.
            best = max(candidates, key=lambda w: (w["rect"][2] - w["rect"][0]) *
                       (w["rect"][3] - w["rect"][1]))["hwnd"]
            break
        if extra_pids:
            wanted |= process_tree(extra_pids())
        time.sleep(poll)
    diag.event("win32.find_window", wanted=sorted(wanted), found=int(best or 0),
               windows=seen[:12])
    return best


def embed(user32, hwnd, parent):
    """Attach the browser as a child of the Qt placeholder. Rolls back on any
    failure so the user never ends up with an invisible or floating browser."""
    if not user32 or not hwnd or not parent:
        return False
    diag.event("embed.before", child=snapshot(user32, hwnd),
               host=snapshot(user32, parent))
    style = get_window_long(user32, hwnd, GWL_STYLE)
    exstyle = get_window_long(user32, hwnd, GWL_EXSTYLE)
    try:
        set_window_long(user32, hwnd, GWL_STYLE,
                        (style & ~(WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME |
                                   WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_DLGFRAME | WS_BORDER))
                        | WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS)
        _set_last_error(0)
        user32.SetParent(hwnd, parent)
        error = _get_last_error()
        actual = int(user32.GetParent(hwnd) or 0)
        diag.event("embed.SetParent", error=error, actual=actual, expected=int(parent))
        if actual != int(parent):
            raise OSError(error, "SetParent did not attach the browser")
        set_window_long(user32, hwnd, GWL_EXSTYLE,
                        (exstyle & ~(WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE |
                                     WS_EX_STATICEDGE | WS_EX_APPWINDOW | WS_EX_NOACTIVATE))
                        | WS_EX_TOOLWINDOW)
        user32.ShowWindow(hwnd, SW_SHOW)
        sync(user32, hwnd, parent)
        diag.event("embed.after", child=snapshot(user32, hwnd))
        return True
    except Exception:
        diag.exception("win32_embed.embed")
        try:
            # Detach first: a half-attached child is worse than a normal window.
            user32.SetParent(hwnd, None)
            set_window_long(user32, hwnd, GWL_STYLE, style)
            set_window_long(user32, hwnd, GWL_EXSTYLE, exstyle)
            user32.ShowWindow(hwnd, SW_SHOW)
        except Exception:
            diag.exception("win32_embed.rollback")
        return False


def sync(user32, hwnd, parent):
    """Keep the child exactly over the placeholder, in the placeholder's own
    client coordinates (child windows are never DPI-virtualised)."""
    if not user32 or not hwnd or not parent:
        return False
    width, height = client_size(user32, parent)
    if width <= 0 or height <= 0:
        return False
    _set_last_error(0)
    ok = user32.SetWindowPos(hwnd, None, 0, 0, width, height,
                             SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW |
                             SWP_FRAMECHANGED | SWP_ASYNCWINDOWPOS)
    if not ok:
        diag.event("win32.SetWindowPos.failed", error=_get_last_error())
    return bool(ok)


SW_HIDE = 0
PROCESS_TERMINATE = 0x0001
_kernel32_cache = {"dll": None}


def _kernel32():
    """kernel32, созданный один раз (и подменяемый в тестах)."""
    if _kernel32_cache["dll"] is None:
        _kernel32_cache["dll"] = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32_cache["dll"]


def kill_tree(pid):
    """Убить процесс и ВСЕХ его детей.

    `Popen.terminate()` убивает только корневой процесс Chrome: дочерние
    (renderer, GPU, utility) могут пережить перезапуск и оставить окно — так в
    Alt+Tab появлялись «много браузеров размером с GUI».
    """
    if not pid:
        return []
    kernel32 = _kernel32()
    pids = set()

    def collect(root):
        root = int(root)
        if root in pids:
            return
        pids.add(root)
        try:
            for child in child_pids(root):
                collect(child)
        except Exception:
            diag.exception("win32_embed.kill_tree_walk")

    collect(int(pid))
    killed = []
    for target in sorted(pids):
        try:
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, target)
        except Exception:
            diag.exception("win32_embed.open_process")
            continue
        if not handle:
            continue
        try:
            if kernel32.TerminateProcess(handle, 1):
                killed.append(target)
        except Exception:
            diag.exception("win32_embed.terminate")
        finally:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
    diag.event("win32.kill_tree", pid=int(pid), killed=killed)
    return killed


def hide_other_windows(user32, pids, keep=0):
    """Спрятать все окна этого браузера, кроме встроенного.

    Если запуск передался уже работавшему экземпляру или окно не успело стать
    дочерним, лишние окна попадают в Alt+Tab и перекрывают GUI. Встроенное окно
    (`keep`) не трогается.
    """
    if not user32:
        return []
    keep = int(keep or 0)
    hidden = []
    try:
        windows = _windows_of(user32, process_tree(pids))
    except Exception:
        diag.exception("win32_embed.stray_enum")
        windows = []
    for info in windows:
        hwnd = int(info.get("hwnd") or 0)
        if not hwnd or hwnd == keep:
            continue
        try:
            if user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, SW_HIDE)
                hidden.append(hwnd)
        except Exception:
            diag.exception("win32_embed.hide_window")
    diag.event("win32.stray_hidden", hidden=hidden, keep=keep)
    return hidden


def is_window(user32, hwnd):
    try:
        return bool(hwnd and user32.IsWindow(hwnd))
    except Exception:
        return False
