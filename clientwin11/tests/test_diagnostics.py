"""Offline tests: no real auth, Chromium or Windows is required for these checks."""
import ast
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
_TMP = tempfile.TemporaryDirectory()
os.environ['LOCALAPPDATA'] = _TMP.name
os.environ['APPDATA'] = _TMP.name
os.environ['HOME'] = _TMP.name
import diagnostics as diag
import win32_diagnostics as native


class DiagnosticsTests(unittest.TestCase):
    def test_compile_all_sources(self):
        for path in ROOT.glob('*.py'):
            compile(path.read_text(encoding='utf-8'), str(path), 'exec')

    def test_exception_does_not_emit_message_or_locals(self):
        try:
            raise ValueError('Bearer VERY_SECRET_PAYLOAD')
        except ValueError:
            with patch.object(diag, 'event') as log:
                diag.exception('test')
        self.assertNotIn('VERY_SECRET', str(log.call_args))
        self.assertIn('ValueError', str(log.call_args))
        self.assertIn('frames', str(log.call_args))

    def test_stage_propagates_exception(self):
        @diag.stage
        def sample():
            raise RuntimeError('secret')
        with self.assertRaises(RuntimeError):
            sample()

    def test_hwnd_signal_is_object(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('hwnd_ready = pyqtSignal(object)', source)
        self.assertNotIn('--window-position=-32000', source)
        self.assertNotIn('startupinfo.wShowWindow = 0', source)

    def test_profile_and_config_are_isolated(self):
        import config
        self.assertIn('LegalyzeWin11', str(config.APP_DIR))
        self.assertFalse((ROOT / 'config.json').exists())

    def test_setparent_null_success_is_not_failure(self):
        # SetParent returns NULL when the old parent is the desktop. GetParent is decisive.
        from types import SimpleNamespace
        class U:
            parent = 0
            def SetParent(self, hwnd, parent):
                self.parent = parent
                return 0
            def GetParent(self, hwnd): return self.parent
            def ShowWindow(self, *args): return 1
        u = U()
        m = SimpleNamespace(user32=u, get_window_long=lambda *a: 0,
                            set_window_long=lambda *a: 0)
        for name in ('GWL_STYLE', 'GWL_EXSTYLE', 'WS_POPUP', 'WS_CAPTION', 'WS_SYSMENU',
                     'WS_THICKFRAME', 'WS_MINIMIZEBOX', 'WS_MAXIMIZEBOX', 'WS_DLGFRAME',
                     'WS_BORDER', 'WS_CHILD', 'WS_VISIBLE', 'WS_CLIPCHILDREN', 'WS_CLIPSIBLINGS',
                     'WS_EX_DLGMODALFRAME', 'WS_EX_WINDOWEDGE', 'WS_EX_CLIENTEDGE',
                     'WS_EX_STATICEDGE', 'WS_EX_APPWINDOW', 'WS_EX_TOOLWINDOW', 'SW_SHOW'):
            setattr(m, name, 1)
        with patch.object(native, 'snapshot', return_value={}), \
             patch.object(ctypes, 'set_last_error', create=True), \
             patch.object(ctypes, 'get_last_error', return_value=0, create=True):
            native.embed(m, 0x123456789, 0x234567890)
        self.assertEqual(u.parent, 0x234567890)


class CDPTests(unittest.TestCase):
    def make_cdp(self):
        import threading
        import time
        from unittest.mock import Mock
        tree = ast.parse((ROOT / 'main.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CDP')
        namespace = dict(diag=diag, stage=diag.stage, threading=threading, json=json, time=time)
        ws = Mock()
        namespace['create_connection'] = lambda *a, **kw: ws
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'CDP-isolated', 'exec'), namespace)
        return namespace['CDP']('ws://127.0.0.1/placeholder'), ws

    def test_skips_events_and_returns_matching_response(self):
        cdp, ws = self.make_cdp()
        ws.recv.side_effect = ['{"method":"some.event"}', '{"id":1,"result":{"ok":true}}']
        self.assertEqual(cdp.send('Browser.getVersion', timeout=1), {'ok': True})
        self.assertFalse(cdp._lock.locked())

    def test_closed_socket_does_not_spin(self):
        cdp, ws = self.make_cdp()
        ws.recv.return_value = ''
        with self.assertRaises(ConnectionError):
            cdp.send('Runtime.evaluate', timeout=1)
        self.assertEqual(ws.recv.call_count, 1)
        self.assertFalse(cdp._lock.locked())

    def test_protocol_error_does_not_expose_payload(self):
        cdp, ws = self.make_cdp()
        ws.recv.return_value = '{"id":1,"error":{"code":-1,"message":"TOP_SECRET"}}'
        with self.assertRaises(RuntimeError) as raised:
            cdp.send('Runtime.evaluate', timeout=1)
        self.assertNotIn('TOP_SECRET', str(raised.exception))
        self.assertFalse(cdp._lock.locked())

    def test_lock_has_timeout(self):
        cdp, ws = self.make_cdp()
        cdp._lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                cdp.send('Runtime.evaluate', timeout=0.01)
            ws.send.assert_not_called()
        finally:
            cdp._lock.release()


class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PyQt6.QtWidgets import QApplication
        except ImportError as exc:
            raise unittest.SkipTest(str(exc))
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_can_be_constructed_and_shown_without_server(self):
        # Import uses a temporary profile and no real server calls.
        import main
        cfg = main.load_config()
        with patch.object(main.MainWindow, '_refresh_balance'), \
             patch.object(main.MainWindow, '_sync_template_from_server'), \
             patch.object(main.MainWindow, '_register_hotkeys'):
            window = main.MainWindow(cfg, '', {'queriesRemaining': 10})
            diag.place_window(window, self.app)
            window.show()
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            self.assertTrue(self.app.primaryScreen().availableGeometry().intersects(window.geometry()))
            from PyQt6.QtCore import Qt
            self.assertNotEqual(window.windowType(), Qt.WindowType.Tool)
            window.hide()
            for timer in (window.init_timer, window.response_end_timer, window.topmost_timer):
                timer.stop()
            window.overlay.finish()
            window.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
