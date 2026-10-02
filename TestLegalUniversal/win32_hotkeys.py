"""Global Windows hotkeys served by a dedicated message-only window.

Works independently of Qt window handles and of the embedded browser: the
hotkeys are registered to a private native window on its own thread, so they
survive HWND recreation, focus inside the web view and message-pump changes.
If RegisterHotKey fails (another application holds the key) a low-frequency
GetAsyncKeyState polling fallback is used for that key, plus periodic
re-registration attempts. Callbacks are invoked on the hotkey thread and MUST
marshal to the Qt thread themselves (e.g. by emitting a queued signal).
"""
import ctypes
import threading
import time
from ctypes import wintypes

import diagnostics as diag

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
HWND_MESSAGE = wintypes.HWND(-3)
WM_APP_RECONFIGURE = 0x8001
WM_APP_STOP = 0x8002

HOTKEY_TOGGLE_ID = 1
HOTKEY_MIC_ID = 2

_POLL_INTERVAL = 0.02
_POLL_FALLBACK_INTERVAL = 0.03
_RETRY_INTERVAL = 10.0


def _win_dlls():
    if not hasattr(ctypes, "WinDLL"):
        return None, None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return user32, kernel32
    except Exception:
        diag.exception("win32_hotkeys.dlls")
        return None, None


class GlobalHotkeys:
    """RegisterHotKey on a message-only window; polling fallback per key."""

    def __init__(self, on_event, on_status=None):
        self.on_event = on_event
        self.on_status = on_status
        self._thread = None
        self._running = False
        self._hwnd = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._desired = {HOTKEY_TOGGLE_ID: 0, HOTKEY_MIC_ID: 0}  # id -> vk
        self._registered = {}  # id -> vk actually held via RegisterHotKey
        self._poll_vk = {}  # id -> vk currently polled
        self._poll_down = {}  # vk -> bool key was down
        self._poll_last = {}  # vk -> monotonic time of last dispatch
        self._next_retry = 0.0
        self._last_status = None

    # ------------------------------------------------------------------ setup

    def start(self):
        if self._thread:
            return True
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="GlobalHotkeys", daemon=True)
        self._thread.start()
        ok = self._ready.wait(3.0)
        if not ok:
            diag.event("hotkeys.thread_timeout")
        return ok

    def set_keys(self, toggle_vk, mic_vk):
        """Thread-safe; blocks briefly for the registration verdict."""
        with self._lock:
            self._desired[HOTKEY_TOGGLE_ID] = int(toggle_vk or 0)
            self._desired[HOTKEY_MIC_ID] = int(mic_vk or 0)
        if not self._thread:
            return False
        user32, _ = _win_dlls()
        if user32 and self._hwnd:
            user32.PostMessageW(wintypes.HWND(self._hwnd), WM_APP_RECONFIGURE, 0, 0)
        time.sleep(0.15)  # the message loop registers synchronously on this message
        return self.is_registered()

    def clear(self):
        return self.set_keys(0, 0)

    def is_registered(self):
        with self._lock:
            return (self._desired[HOTKEY_TOGGLE_ID] != 0 and
                    self._desired[HOTKEY_MIC_ID] != 0 and
                    all(self._registered.get(k) == v
                        for k, v in self._desired.items() if v))

    def stop(self):
        self._running = False
        user32, _ = _win_dlls()
        if user32 and self._hwnd:
            try:
                user32.PostMessageW(wintypes.HWND(self._hwnd), WM_APP_STOP, 0, 0)
            except Exception:
                diag.exception("win32_hotkeys.stop")
        if self._thread:
            self._thread.join(3.0)
            self._thread = None

    # ------------------------------------------------------------ message loop

    def _run(self):
        user32, kernel32 = _win_dlls()
        if not user32:
            self._emit_status(False, "Win32 недоступен")
            return

        class_name = "LegalyzeHotkeys%d" % (os_getpid(),)
        try:
            wndproc_t = wintypes.WNDPROC
        except AttributeError:  # pragma: no cover - depends on Python build
            wndproc_t = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM)
        wndproc = wndproc_t(self._wnd_proc)

        try:
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                              wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = ctypes.c_long
        except Exception:
            diag.exception("win32_hotkeys.defwindowproc")

        hinstance = kernel32.GetModuleHandleW(None)

        wc = wintypes.WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = wintypes.HINSTANCE(hinstance)
        wc.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            diag.event("hotkeys.register_class_failed", error=ctypes.get_last_error())

        hwnd = 0
        try:
            created = user32.CreateWindowExW(
                0, wintypes.LPWSTR(class_name), "LegalyzeHotkeys",
                0, 0, 0, 0, 0, HWND_MESSAGE, None, wintypes.HINSTANCE(hinstance), None)
            hwnd = int(created or 0)
        except Exception:
            diag.exception("win32_hotkeys.create_window")
        self._hwnd = hwnd
        if not hwnd:
            # Polling fallback must keep working even without a message window.
            diag.event("hotkeys.window_failed", error=ctypes.get_last_error())
        else:
            diag.event("hotkeys.service_started", hwnd=hwnd)
        self._ready.set()

        msg = wintypes.MSG()
        try:
            while self._running:
                if hwnd:
                    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                        if msg.message == 0x0012:  # WM_QUIT
                            self._running = False
                            break
                        self._dispatch(msg.message, int(msg.wParam or 0))
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                if not self._running:
                    break
                now = time.monotonic()
                if now >= self._next_retry:
                    self._next_retry = now + _RETRY_INTERVAL
                    self._apply_keys()
                self._poll_step(now)
                time.sleep(_POLL_FALLBACK_INTERVAL if self._poll_vk else _POLL_INTERVAL)
        finally:
            try:
                for key_id in list(self._registered):
                    user32.UnregisterHotKey(wintypes.HWND(hwnd), key_id)
                self._registered.clear()
                if hwnd:
                    user32.DestroyWindow(wintypes.HWND(hwnd))
                user32.UnregisterClassW(wintypes.LPWSTR(class_name), wintypes.HINSTANCE(hinstance))
            except Exception:
                diag.exception("win32_hotkeys.cleanup")
            self._hwnd = None
            diag.event("hotkeys.service_stopped")

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        # Messages are consumed by the PeekMessage loop; DefWindowProc is still
        # required here: returning 0 for WM_NCCREATE aborts CreateWindowExW.
        user32, _ = _win_dlls()
        if user32:
            try:
                return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam) or 0)
            except Exception:
                diag.exception("win32_hotkeys.wnd_proc")
        return 0

    def _dispatch(self, message, wparam):
        if message == WM_APP_RECONFIGURE:
            self._apply_keys()
        elif message == WM_APP_STOP:
            self._running = False
        elif message == WM_HOTKEY and wparam in (HOTKEY_TOGGLE_ID, HOTKEY_MIC_ID):
            self._fire(wparam)

    def _fire(self, key_id, source="registered"):
        try:
            diag.event("hotkeys.pressed", key_id=key_id, source=source)
            self.on_event(key_id)
        except Exception:
            diag.exception("win32_hotkeys.fire")

    # --------------------------------------------------------------- register

    def _apply_keys(self):
        user32, _ = _win_dlls()
        if not user32:
            return
        with self._lock:
            desired = dict(self._desired)

        problems = []
        for key_id, vk in desired.items():
            held = self._registered.get(key_id)
            if vk and held == vk:
                continue
            if held:
                user32.UnregisterHotKey(wintypes.HWND(self._hwnd), key_id)
                self._registered.pop(key_id, None)
            if not vk:
                continue
            if not self._hwnd:
                problems.append((key_id, vk))  # polling fallback only
                continue
            ok = bool(user32.RegisterHotKey(wintypes.HWND(self._hwnd), key_id,
                                            MOD_NOREPEAT, vk))
            if ok:
                self._registered[key_id] = vk
                diag.event("hotkeys.registered", key_id=key_id, vk=vk)
            else:
                problems.append((key_id, vk))
                diag.event("hotkeys.register_failed", key_id=key_id, vk=vk,
                           error=ctypes.get_last_error())

        # Polling fallback covers keys that could not be registered.
        wanted_poll = {key_id: vk for key_id, vk in problems}
        for key_id in list(self._poll_vk):
            if key_id not in wanted_poll:
                self._poll_vk.pop(key_id, None)
        for key_id, vk in wanted_poll:
            self._poll_vk[key_id] = vk

        self._emit_status(not problems, self._describe(problems))

    @staticmethod
    def _describe(problems):
        if not problems:
            return "ok"
        names = {HOTKEY_TOGGLE_ID: "окно", HOTKEY_MIC_ID: "микрофон"}
        return "заняты: " + ", ".join(names.get(i, str(i)) for i, _ in problems)

    def _emit_status(self, ok, message):
        state = (bool(ok), str(message))
        if state == self._last_status:
            return
        self._last_status = state
        try:
            if self.on_status:
                self.on_status(ok, message)
        except Exception:
            diag.exception("win32_hotkeys.status")

    # -------------------------------------------------------- polling fallback

    def _poll_step(self, now):
        if not self._poll_vk:
            return
        user32, _ = _win_dlls()
        if not user32:
            return
        for key_id, vk in list(self._poll_vk.items()):
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            was_down = self._poll_down.get(vk, False)
            self._poll_down[vk] = down
            if down and not was_down:
                last = self._poll_last.get(vk, 0.0)
                if now - last >= 0.35:  # debounce key repeat and bounce
                    self._poll_last[vk] = now
                    self._fire(key_id, source="poll")


def os_getpid():
    import os
    return os.getpid()
