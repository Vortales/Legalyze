"""Release signal adapters; optional generated smoke program for a real Nuitka compile.

python test_release_signals.py --emit-smoke /path/to/smoke.py
Compile that generated source with Nuitka, following the release diagnostics module.
Tests remain outside the minimal release source distribution.
"""
import ast
from pathlib import Path
import sys
import textwrap
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

RELEASE = Path(__file__).resolve().parents[2] / 'release'


def clicked_adapters():
    tree = ast.parse((RELEASE / 'main.py').read_text())
    return [node.args[0] for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'connect' and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == 'clicked']


def smoke_source():
    tree = ast.parse((RELEASE / 'main.py').read_text())
    login = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LoginWindow')
    handler = next(n for n in login.body if isinstance(n, ast.FunctionDef) and n.name == '_do_login')
    initializer = next(n for n in login.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    connection = next(n for n in ast.walk(initializer) if isinstance(n, ast.Expr)
                      and ast.unparse(n).startswith('self.login_btn.clicked.connect('))
    # Static generated Python: Nuitka compiles the ACTUAL handler, decorators and
    # connection, not a function later reconstructed by exec inside the binary.
    return '''from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, QCoreApplication
from types import SimpleNamespace
import sys
from diagnostics import stage

class Button(QObject):
    clicked = pyqtSignal(bool)
    def setEnabled(self, enabled):
        self.enabled = enabled

class AuthWorker(QObject):
    done = pyqtSignal(bool, str, dict)
    def __init__(self, login, password, remember):
        super().__init__()
        self.started = 0
    def start(self):
        self.started += 1
    def isRunning(self):
        return False

class LoginWindow(QObject):
    def __init__(self):
        super().__init__()
        self._is_logging_in = False
        self._worker = None
        self.login_input = SimpleNamespace(text=lambda: 'test-user')
        self.pass_input = SimpleNamespace(text=lambda: 'test-only')
        self.remember_cb = SimpleNamespace(isChecked=lambda: False)
        self.error_label = SimpleNamespace(setText=lambda message: None)
        self.login_btn = Button()
        self.cancel_btn = Button()
        ''' + ast.unparse(connection) + '''
    def _on_auth(self, ok, err, data):
        pass

''' + textwrap.indent(ast.unparse(handler), '    ') + '''

app = QCoreApplication([])
errors = []
sys.excepthook = lambda *args: errors.append(args[0].__name__)
for checked in (False, True):
    window = LoginWindow()
    window.login_btn.clicked.emit(checked)
    assert not errors, errors
    assert window._worker is not None, 'Login handler did not start authentication'
    assert window._worker.started == 1
    assert window._is_logging_in
    # An internal TypeError must NOT cause decorator-level catch-and-retry.
    class Broken:
        def text(self):
            raise TypeError('deliberate handler error')
    window = LoginWindow()
    window.login_input = Broken()
    window.login_btn.clicked.emit(checked)
    assert errors == ['TypeError'], errors
    assert window._worker is None
    errors.clear()
print('PASS: login clicked(False/True), one auth start, body TypeError not retried')
'''


class ReleaseSignalTests(unittest.TestCase):
    def test_every_clicked_connection_explicitly_discards_checked(self):
        adapters = clicked_adapters()
        self.assertEqual(len(adapters), 15)
        for adapter in adapters:
            self.assertIsInstance(adapter, ast.Lambda, ast.unparse(adapter))
            self.assertEqual(len(adapter.args.args), 1)
            self.assertEqual(adapter.args.args[0].arg, '_checked')
            self.assertEqual(len(adapter.body.args), 0)
            self.assertEqual(len(adapter.body.keywords), 0)
            path = ast.unparse(adapter.body.func).split('.')[1:]
            calls = Mock()
            obj = SimpleNamespace()
            current = obj
            for part in path[:-1]:
                child = SimpleNamespace()
                setattr(current, part, child)
                current = child
            setattr(current, path[-1], calls)
            bridge = eval(compile(ast.Expression(adapter), 'clicked-adapter', 'eval'), {'self': obj})
            for checked in (False, True):
                bridge(checked)
                calls.assert_called_with()
            bridge()  # Custom HotkeyChip.clicked is a zero-argument signal.
            self.assertEqual(calls.call_count, 3)

    def test_actual_login_body_with_qtcore_bool_signal(self):
        try:
            import PyQt6.QtCore
        except ImportError as exc:
            self.skipTest(str(exc))
        old_hook = sys.excepthook
        sys.path.insert(0, str(RELEASE))
        try:
            exec(compile(smoke_source(), 'release_login_smoke.py', 'exec'), {'__name__': '__main__'})
        finally:
            sys.excepthook = old_hook
            sys.path.pop(0)

    def test_browser_and_security_modules_are_unchanged(self):
        source = RELEASE.parent / 'clientwin11'
        for name in ('browser_focus.py', 'browser_process.py', 'win32_diagnostics.py',
                     'config.py', 'storage_paths.py', 'secure_store.py', 'hwid_gen.py',
                     'crypto_utils.py', 'updater.py'):
            self.assertEqual((source / name).read_bytes(), (RELEASE / name).read_bytes(), name)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--emit-smoke':
        Path(sys.argv[2]).write_text(smoke_source())
    else:
        unittest.main()
