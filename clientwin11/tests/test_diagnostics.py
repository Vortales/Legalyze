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
import browser_focus


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


class BrowserRegressionTests(unittest.TestCase):
    def test_rejects_hidden_helper_from_reported_windows_log(self):
        info = {'class': 'Chrome_WidgetWin_0', 'visible': False, 'parent': 0,
                'rect': [130, 130, 898, 649]}
        self.assertFalse(native.eligible_browser_window(info))
        info['visible'] = True
        self.assertFalse(native.eligible_browser_window(info))
        info['class'] = 'Chrome_WidgetWin_1'
        self.assertTrue(native.eligible_browser_window(info))
        info['visible'] = False
        self.assertFalse(native.eligible_browser_window(info))
        info['visible'], info['parent'] = True, 123
        self.assertFalse(native.eligible_browser_window(info))

    def test_page_probe_does_not_log_page_supplied_secrets(self):
        from unittest.mock import Mock
        tree = ast.parse((ROOT / 'main.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'log_page_probe')
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        namespace = {'diag': diag}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'page-probe', 'exec'), namespace)
        page = Mock()
        page.eval.return_value = {'value': {'category': 'PRIVATE', 'ready': 'PRIVATE',
                                           'editors': 'PRIVATE', 'title': 'PRIVATE', 'fileInputs': 0}}
        with patch.object(diag, 'event') as log:
            namespace['log_page_probe'](page)
        self.assertNotIn('PRIVATE', str(log.call_args))
        self.assertEqual(log.call_args.kwargs['fileInputs'], 0)

    def test_response_poll_is_single_flight_and_not_on_gui_thread(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import Mock
        tree = ast.parse((ROOT / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MainWindow')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_check_response_end_from_ui')
        namespace = {'threading': threading, 'diag': diag, 'JS_CHECK_END': 'probe'}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'response-poll', 'exec'), namespace)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        ids = []
        def evaluate(*args, **kwargs):
            ids.append(threading.get_ident())
            started.set()
            release.wait(2)
            return {'value': 3}
        page = Mock()
        page.eval.side_effect = evaluate
        signal = Mock()
        signal.emit.side_effect = lambda value: finished.set()
        window = SimpleNamespace(_closing=False, _response_poll_busy=False, pdf_attached=True,
                                  worker=SimpleNamespace(page=page), response_poll_finished=signal)
        poll = namespace['_check_response_end_from_ui']
        try:
            poll(window)
            self.assertTrue(started.wait(1))
            poll(window)
            self.assertEqual(page.eval.call_count, 1)
            self.assertNotEqual(ids[0], threading.get_ident())
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        signal.emit.assert_called_once_with(3)

    def test_attachment_worker_stops_at_deadline(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock
        tree = ast.parse((ROOT / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'UploadThread')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        namespace = {'time': time, 'diag': diag}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'upload-loop', 'exec'), namespace)
        worker = SimpleNamespace(_stop=False, wait_started=time.monotonic()-91, failed=Mock())
        namespace['run'](worker)
        worker.failed.emit.assert_called_once_with('attachment_deadline')


class FocusAndStartupTests(unittest.TestCase):
    def test_staging_is_outside_negative_origin_multimonitor_desktop(self):
        x, y = native.staging_position(-3840, -2160)
        self.assertLess(x + 453*4, -3840)
        self.assertLess(y + 735*4, -2160)
        self.assertEqual(native.staging_position(-3840, -2160, external=True), (80, 80))

    def test_no_extra_startup_dialog(self):
        source = (ROOT / 'main.py').read_text()
        self.assertNotIn('startup = QDialog()', source)
        self.assertIn('SW_SHOWNOACTIVATE', source)
        self.assertIn('--window-position={staging_x},{staging_y}', source)

    def fake_native(self):
        from unittest.mock import Mock
        u, k = Mock(), Mock()
        u.GetForegroundWindow.return_value = 100
        u.IsWindow.return_value = True
        u.IsChild.side_effect = lambda root, child: root == 200 and child == 201
        u.GetWindowThreadProcessId.return_value = 20
        u.AttachThreadInput.return_value = True
        u.SetFocus.return_value = 100
        k.GetCurrentThreadId.return_value = 10
        return u, k

    def test_focus_joins_and_detaches_input_queues(self):
        u, k = self.fake_native()
        with patch.object(ctypes, 'set_last_error', create=True), \
             patch.object(ctypes, 'get_last_error', return_value=0, create=True), \
             patch.object(browser_focus, 'thread_focus', side_effect=lambda u, tid: {'focus': 100 if tid == 0 else 201}):
            self.assertTrue(browser_focus.handoff(u, k, 100, 200, 201))
        self.assertEqual([c.args for c in u.AttachThreadInput.call_args_list], [(10, 20, True), (10, 20, False)])
        u.SetFocus.assert_called_once_with(201)

    def test_focus_never_steals_from_other_application(self):
        u, k = self.fake_native()
        u.GetForegroundWindow.return_value = 999
        self.assertFalse(browser_focus.handoff(u, k, 100, 200, 201))
        u.AttachThreadInput.assert_not_called()
        u.SetFocus.assert_not_called()

    def test_focus_detaches_even_when_setfocus_raises(self):
        u, k = self.fake_native()
        u.SetFocus.side_effect = OSError('failed')
        with patch.object(ctypes, 'set_last_error', create=True), \
             patch.object(ctypes, 'get_last_error', return_value=0, create=True), \
             patch.object(browser_focus, 'thread_focus', return_value={'focus': 100}):
            with self.assertRaises(OSError):
                browser_focus.handoff(u, k, 100, 200, 201)
        self.assertEqual(u.AttachThreadInput.call_args.args, (10, 20, False))

    def test_poll_does_not_repeatedly_focus_or_touch_qt_controls(self):
        u, k = self.fake_native()
        u.GetAsyncKeyState.side_effect = [0x8000, 0x8000, 0, 0x8000, 0, 0x8000]
        u.GetCursorPos.return_value = True
        u.WindowFromPoint.side_effect = [201, 300, 201]
        bridge = browser_focus.BrowserFocusBridge(u, k)
        with patch.object(browser_focus, 'handoff') as focus:
            for _ in range(6):
                bridge.poll(100, 200, True)
        self.assertEqual(focus.call_count, 2)

    def test_disabled_bridge_does_not_focus(self):
        u, k = self.fake_native()
        u.GetAsyncKeyState.return_value = 0x8000
        bridge = browser_focus.BrowserFocusBridge(u, k)
        with patch.object(browser_focus, 'handoff') as focus:
            bridge.poll(100, 200, False)
        focus.assert_not_called()


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


class QtSlotRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
        cls.QObject, cls.pyqtSignal, cls.pyqtSlot = QObject, pyqtSignal, pyqtSlot

    def test_original_variadic_wrapper_receives_unwanted_checked_argument(self):
        QObject, pyqtSignal = self.QObject, self.pyqtSignal
        class Emitter(QObject):
            clicked = pyqtSignal(bool)
        class Receiver(QObject):
            @diag.stage
            def handle(self):
                raise AssertionError('Body must not be reached in this reproduction')
        sender, receiver = Emitter(), Receiver()
        errors = []
        sender.clicked.connect(receiver.handle)
        with patch.object(sys, 'excepthook', side_effect=lambda *info: errors.append(info)), \
             patch.object(diag, 'event') as log:
            sender.clicked.emit(False)
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0][0], TypeError)
        self.assertTrue(any(c.args[0] == 'call.signature_mismatch' for c in log.call_args_list))

    def test_actual_zero_argument_slot_declarations_discard_clicked_bool(self):
        # Extract the production declarations/decorators, stub only their side effects.
        # QtCore is enough: no display, OpenGL, auth server, Chromium or os._exit.
        import copy
        QObject, pyqtSignal = self.QObject, self.pyqtSignal
        class Emitter(QObject):
            clicked = pyqtSignal(bool)
        expected = {'_do_login', '_close_app', '_restart', '_toggle_visibility',
                    '_start_chrome', '_sync_template_from_server', '_on_failed'}
        tree = ast.parse((ROOT / 'main.py').read_text())
        methods = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in expected:
                node = copy.deepcopy(node)
                node.body = ast.parse('self.calls += 1').body
                methods.append(node)
        self.assertEqual({node.name for node in methods}, expected)
        cls_node = ast.ClassDef(name='Receiver', bases=[ast.Name(id='QObject', ctx=ast.Load())],
                                keywords=[], body=methods, decorator_list=[])
        namespace = {'QObject': QObject, 'pyqtSlot': self.pyqtSlot, 'stage': diag.stage}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls_node], type_ignores=[])),
                     'production-slot-declarations', 'exec'), namespace)
        for method in expected:
            with self.subTest(method=method):
                sender, receiver = Emitter(), namespace['Receiver']()
                receiver.calls = 0
                errors = []
                sender.clicked.connect(getattr(receiver, method))
                with patch.object(sys, 'excepthook', side_effect=lambda *info: errors.append(info)):
                    sender.clicked.emit(False)
                    sender.clicked.emit(True)
                    getattr(receiver, method)()  # Enter / direct Python invocation
                self.assertEqual(errors, [])
                self.assertEqual(receiver.calls, 3)


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
            for timer in (window.init_timer, window.response_end_timer, window.topmost_timer, window.focus_timer):
                timer.stop()
            window.overlay.finish()
            window.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
