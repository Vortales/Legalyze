"""Checked native calls; only Chromium windows owned by this launch are eligible."""
import ctypes
from ctypes import wintypes
import os
import time
import diagnostics as diag


def configure(u):
    if not u:
        return
    for name, args, result in (
        ('GetParent', [wintypes.HWND], wintypes.HWND),
        ('IsWindow', [wintypes.HWND], wintypes.BOOL),
        ('GetWindowDpiAwarenessContext', [wintypes.HWND], ctypes.c_void_p),
        ('GetAwarenessFromDpiAwarenessContext', [ctypes.c_void_p], ctypes.c_int),
        ('GetDpiForWindow', [wintypes.HWND], wintypes.UINT),
    ):
        if hasattr(u, name):
            fn = getattr(u, name)
            fn.argtypes, fn.restype = args, result


def snapshot(m, hwnd):
    u = m.user32
    rect = m.RECT()
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    result = dict(hwnd=int(hwnd), pid=pid.value, visible=bool(u.IsWindowVisible(hwnd)),
                  rect=[rect.left, rect.top, rect.right, rect.bottom],
                  style=m.get_window_long(hwnd, m.GWL_STYLE), exstyle=m.get_window_long(hwnd, m.GWL_EXSTYLE),
                  parent=int(u.GetParent(hwnd) or 0))
    if hasattr(u, 'GetDpiForWindow'):
        result['dpi'] = u.GetDpiForWindow(hwnd)
    if hasattr(u, 'GetWindowDpiAwarenessContext'):
        result['awareness'] = u.GetAwarenessFromDpiAwarenessContext(u.GetWindowDpiAwarenessContext(hwnd))
    return result


_last_scan = 0

def find_window(m, pids):
    global _last_scan
    candidates, windows = [], []
    def callback(hwnd, unused):
        try:
            pid = wintypes.DWORD()
            m.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            buf = ctypes.create_unicode_buffer(256)
            m.user32.GetClassNameW(hwnd, buf, 256)
            if buf.value not in ('Chrome_WidgetWin_1', 'Chrome_WidgetWin_0'):
                return True
            info = snapshot(m, hwnd)
            info['class'] = buf.value
            windows.append(info)
            l, t, r, b = info['rect']
            if r-l >= 100 and b-t >= 100 and not info['parent']:
                candidates.append((info['visible'], buf.value == 'Chrome_WidgetWin_1', (r-l)*(b-t), int(hwnd)))
        except Exception:
            diag.exception('EnumWindows.callback')
        return True
    cb = m.WNDENUMPROC(callback)
    ctypes.set_last_error(0)
    result = m.user32.EnumWindows(cb, 0)
    if time.monotonic()-_last_scan > 2 or candidates:
        diag.event('win32.scan', pids=sorted(pids), windows=windows, result=bool(result), error=ctypes.get_last_error())
        _last_scan = time.monotonic()
    return max(candidates)[-1] if candidates else None


def checked_style(m, hwnd, index, value):
    ctypes.set_last_error(0)
    previous = m.set_window_long(hwnd, index, value)
    error = ctypes.get_last_error()
    diag.event('win32.style', hwnd=int(hwnd), index=index, previous=previous, new=value, error=error)
    if not previous and error:
        raise ctypes.WinError(error)


def embed(m, hwnd, parent):
    u = m.user32
    diag.event('embed.before', child=snapshot(m, hwnd), host=snapshot(m, parent))
    style = m.get_window_long(hwnd, m.GWL_STYLE)
    exstyle = m.get_window_long(hwnd, m.GWL_EXSTYLE)
    try:
        checked_style(m, hwnd, m.GWL_STYLE,
                      (style & ~(m.WS_POPUP | m.WS_CAPTION | m.WS_SYSMENU | m.WS_THICKFRAME |
                                 m.WS_MINIMIZEBOX | m.WS_MAXIMIZEBOX | m.WS_DLGFRAME | m.WS_BORDER)) |
                      m.WS_CHILD | m.WS_VISIBLE | m.WS_CLIPCHILDREN | m.WS_CLIPSIBLINGS)
        ctypes.set_last_error(0)
        previous = u.SetParent(hwnd, parent)
        error = ctypes.get_last_error()
        actual = int(u.GetParent(hwnd) or 0)
        diag.event('embed.SetParent', previous=int(previous or 0), error=error, actual=actual, expected=parent)
        # NULL can mean success (previously a top-level window); verify actual parent.
        if actual != parent:
            raise OSError(error, 'SetParent did not attach the browser')
        checked_style(m, hwnd, m.GWL_EXSTYLE,
                      (exstyle & ~(m.WS_EX_DLGMODALFRAME | m.WS_EX_WINDOWEDGE | m.WS_EX_CLIENTEDGE |
                                   m.WS_EX_STATICEDGE | m.WS_EX_APPWINDOW)) | m.WS_EX_TOOLWINDOW)
        u.ShowWindow(hwnd, m.SW_SHOW)
        diag.event('embed.after', child=snapshot(m, hwnd))
    except Exception:
        diag.exception('embed.failed')
        # Roll back instead of leaving an invisible child/off-screen browser.
        u.SetParent(hwnd, None)
        checked_style(m, hwnd, m.GWL_STYLE, style)
        checked_style(m, hwnd, m.GWL_EXSTYLE, exstyle)
        u.ShowWindow(hwnd, m.SW_SHOW)
        u.SetWindowPos(hwnd, None, 80, 80, 900, 700, m.SWP_NOZORDER | m.SWP_SHOWWINDOW | m.SWP_FRAMECHANGED)
        raise
