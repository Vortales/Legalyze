"""Early, stdlib-only diagnostics. Never serialize credentials, payloads or locals."""
import atexit
import ctypes
from datetime import datetime, timezone
import faulthandler
import functools
import importlib.metadata
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import traceback
import uuid
from storage_paths import app_dir

LOG_DIR = None
FAULT_FILE = None
_started = False
_exception_counts = {}
_last_beat = time.monotonic()
_log = logging.getLogger('legalyze.diagnostic')


def event(event_name, **fields):
    _log.info(json.dumps({'time': datetime.now(timezone.utc).isoformat(),
                          'pid': os.getpid(), 'thread': threading.current_thread().name,
                          'event': event_name, **fields}, ensure_ascii=False, default=str))


def exception(where, info=None):
    typ, value, tb = info or sys.exc_info()
    key = (where, getattr(typ, '__name__', str(typ)))
    count = _exception_counts.get(key, 0) + 1
    _exception_counts[key] = count
    if count > 3 and count & (count - 1):
        return
    # Exception messages and source lines may contain HTTP bodies, JS, tokens or PDFs.
    frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
              for f in traceback.extract_tb(tb)] if tb else []
    event('exception', where=where, occurrence=count, type=getattr(typ, '__name__', str(typ)),
          errno=getattr(value, 'errno', None), winerror=getattr(value, 'winerror', None), frames=frames)


def stage(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        start = time.monotonic()
        event('stage.begin', name=fn.__qualname__)
        try:
            result = fn(*args, **kwargs)
        except SystemExit as exc:
            event('stage.exit', name=fn.__qualname__, code=exc.code if isinstance(exc.code, int) else None)
            raise
        except TypeError:
            # Diagnose argument binding without exposing argument values or exception text.
            try:
                signature.bind(*args, **kwargs)
            except TypeError:
                event('call.signature_mismatch', name=fn.__qualname__,
                      positional_count=len(args), keyword_count=len(kwargs),
                      parameter_count=len(signature.parameters))
            exception(fn.__qualname__)
            raise
        except BaseException:
            exception(fn.__qualname__)
            raise
        event('stage.end', name=fn.__qualname__, seconds=round(time.monotonic()-start, 3))
        return result
    return wrapped


def setup():
    global LOG_DIR, FAULT_FILE, _started
    if _started:
        return
    session = time.strftime('%Y%m%d-%H%M%S') + f'-{os.getpid()}-{uuid.uuid4().hex[:6]}'
    for base in (app_dir() / 'logs',):
        try:
            directory = base / session
            directory.mkdir(parents=True, exist_ok=False)
            handler = RotatingFileHandler(directory / 'diagnostic.jsonl', maxBytes=8*1024*1024,
                                          backupCount=4, encoding='utf-8')
            LOG_DIR = directory
            break
        except OSError:
            continue
    else:
        raise RuntimeError('Cannot create diagnostic log directory')
    handler.setFormatter(logging.Formatter('%(message)s'))
    _log.setLevel(logging.INFO)
    _log.addHandler(handler)
    _log.propagate = False
    _started = True
    FAULT_FILE = open(LOG_DIR / 'stacks.log', 'a', encoding='utf-8')
    faulthandler.enable(FAULT_FILE, all_threads=True)
    # Also works before QApplication exists, during imports/config/HWID processing.
    faulthandler.dump_traceback_later(60, repeat=False, file=FAULT_FILE)
    def uncaught(typ, value, tb):
        exception('sys.excepthook', (typ, value, tb))
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None,
                f'Ошибка {typ.__name__}. Диагностика:\n{LOG_DIR}', 'Legalyze — диагностика', 0x10)
    sys.excepthook = uncaught
    threading.excepthook = lambda args: exception('thread.excepthook',
                                                 (args.exc_type, args.exc_value, args.exc_traceback))
    versions = {}
    for package in ('PyQt6', 'PyQt6-Qt6', 'requests', 'websocket-client', 'cryptography', 'fpdf2'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not in metadata'
    event('session.start', platform=platform.platform(), python=sys.version,
          bitness=ctypes.sizeof(ctypes.c_void_p)*8, executable=sys.executable,
          cwd=str(Path.cwd()), frozen=bool(getattr(sys, 'frozen', False)),
          nuitka_compiled=('__compiled__' in globals()),
          release_revision='qt-click-adapters-v1', packages=versions,
          windows_build=str(sys.getwindowsversion()) if sys.platform == 'win32' else None,
          modes={k: os.environ.get(k) for k in ('LEGALYZE_DISABLE_GPU', 'LEGALYZE_EXTERNAL_BROWSER',
                                                'LEGALYZE_FRESH_PROFILE')})
    threading.Thread(target=collect_system, name='SystemInventory', daemon=True).start()
    atexit.register(lambda: event('session.atexit'))


def install_qt(app):
    from PyQt6.QtCore import QTimer, qInstallMessageHandler, QT_VERSION_STR, PYQT_VERSION_STR
    qInstallMessageHandler(lambda kind, ctx, message: event('qt.message', kind=str(kind), message=message))
    event('qt.start', qt=QT_VERSION_STR, pyqt=PYQT_VERSION_STR, platform=app.platformName())
    for screen in app.screens():
        event('qt.screen', geometry=screen.geometry().getRect(), available=screen.availableGeometry().getRect(),
              dpi=screen.logicalDotsPerInch(), dpr=screen.devicePixelRatio())
    def beat():
        global _last_beat
        _last_beat = time.monotonic()
    timer = QTimer(app)
    timer.timeout.connect(beat)
    timer.start(1000)
    app._diagnostic_timer = timer
    def watch():
        dumps = 0
        while True:
            time.sleep(10)
            age = round(time.monotonic() - _last_beat, 1)
            event('gui.heartbeat', age_seconds=age)
            if age > 15 and dumps < 20:
                faulthandler.dump_traceback(file=FAULT_FILE, all_threads=True)
                dumps += 1
    threading.Thread(target=watch, name='DiagnosticWatchdog', daemon=True).start()
    app.aboutToQuit.connect(lambda: event('qt.aboutToQuit'))
    app.lastWindowClosed.connect(lambda: event('qt.lastWindowClosed'))


def install_http():
    import requests
    from urllib.parse import urlsplit
    original = requests.sessions.Session.request
    @functools.wraps(original)
    def request(self, method, url, *args, **kwargs):
        parsed = urlsplit(url)
        # Never record query, headers, request/response body, HWID or authorization.
        fields = dict(method=method, host=parsed.hostname,
                      path=parsed.path if parsed.path.startswith('/api/client/') else '<omitted>')
        started = time.monotonic()
        event('http.begin', **fields)
        try:
            response = original(self, method, url, *args, **kwargs)
        except Exception:
            exception('http.request')
            raise
        event('http.end', **fields, status=response.status_code,
              seconds=round(time.monotonic()-started, 3))
        return response
    requests.sessions.Session.request = request


def place_window(window, app):
    """Use Qt logical coordinates, not physical pixels from a different monitor."""
    screen = app.primaryScreen()
    g = screen.availableGeometry()
    # Keep existing fixed-size layout; top-align if the screen is shorter than it.
    window.move(g.x() + max(0, g.width()-window.width()-16),
                g.y() + max(0, (g.height()-window.height())//2))
    event('main.geometry', geometry=window.geometry().getRect(), available=g.getRect(),
          dpr=window.devicePixelRatioF(), visible=window.isVisible(), hwnd=int(window.winId()))


def collect_system():
    """Bounded, background-only inventory. No serials, user list, IPs or environment dump."""
    if sys.platform != 'win32':
        return
    import subprocess
    command = (
        '$ErrorActionPreference="Stop"; '
        '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); '
        '@{GPU=@(Get-CimInstance Win32_VideoController | '
        'Select-Object Name,DriverVersion,Status,AdapterRAM); '
        'OS=(Get-CimInstance Win32_OperatingSystem | '
        'Select-Object Caption,Version,BuildNumber,OSArchitecture)} | ConvertTo-Json -Depth 4'
    )
    try:
        event('windows.privilege', elevated=bool(ctypes.windll.shell32.IsUserAnAdmin()),
              remote_session=bool(ctypes.windll.user32.GetSystemMetrics(0x1000)),
              proxy_variables_present=[k for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY') if os.environ.get(k)])
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=20, creationflags=0x08000000)
        if result.returncode == 0:
            event('windows.inventory', data=result.stdout.decode('utf-8', errors='replace'))
        else:
            event('windows.inventory.failed', returncode=result.returncode)
    except Exception:
        exception('windows.inventory')


def monitor_window(window):
    from PyQt6.QtCore import QTimer
    def sample():
        worker = window.worker
        event('main.snapshot', visible=window.isVisible(), minimized=window.isMinimized(),
              geometry=window.geometry().getRect(), dpr=window.devicePixelRatioF(),
              chrome_hwnd=window.chrome_hwnd, worker_running=bool(worker and worker.isRunning()),
              chromium_exit=worker.proc.poll() if worker and worker.proc else None,
              overlay_visible=window.overlay.isVisible())
    timer = QTimer(window)
    timer.timeout.connect(sample)
    timer.start(5000)
    window._diagnostic_snapshot_timer = timer
    sample()


def inspect_browser(path):
    import hashlib
    try:
        target = Path(path)
        digest = hashlib.sha256()
        with target.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b''):
                digest.update(chunk)
        event('chromium.binary', path=str(target), bytes=target.stat().st_size, sha256=digest.hexdigest())
    except Exception:
        exception('chromium.binary')
