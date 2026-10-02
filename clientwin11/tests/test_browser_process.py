"""No native Windows execution: policy, control-flow and process-handle regressions."""
import ast
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import browser_process as bp


class LaunchPolicyTests(unittest.TestCase):
    def test_only_elevated_windows_10_desktop_is_changed(self):
        for major, build, product, elevated, expected in [
            (10, 19045, 1, True, True), (10, 17763, 1, True, True),
            (10, 19045, 1, False, False), (10, 22000, 1, True, False),
            (10, 26100, 1, True, False), (10, 19045, 3, True, False),
            (6, 9600, 1, True, False),
        ]:
            with self.subTest(build=build, elevated=elevated, product=product):
                self.assertEqual(bp.needs_desktop_token(major, build, product, elevated), expected)

    def launch_namespace(self, tmp, desktop_token, owners):
        tree = ast.parse((ROOT/'main.py').read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'launch_chrome')
        fn.decorator_list = []
        process = SimpleNamespace(pid=123, token_source='linked')
        browser = SimpleNamespace(launch_policy=Mock(return_value={'desktop_token': desktop_token}),
                                  launch_unelevated=Mock(return_value=process), ProfileInUseError=bp.ProfileInUseError)
        sub = SimpleNamespace(Popen=Mock(return_value=process), STARTUPINFO=SimpleNamespace,
                              STARTF_USESHOWWINDOW=1)
        # STARTUPINFO normally initializes its bit field to zero.
        sub.STARTUPINFO = lambda: SimpleNamespace(dwFlags=0)
        ns = dict(PROFILE=Path(tmp)/'profile', browser_process=browser,
                  storage_paths=SimpleNamespace(busy_windows_paths=Mock(return_value=owners)),
                  diag=SimpleNamespace(event=Mock(), inspect_browser=Mock(), LOG_DIR=Path(tmp)),
                  _free_port=lambda: 12345, _set_browser_zoom_preferences=Mock(),
                  resolve_browser_path=lambda: 'C:/Legalyze/chromium/chrome.exe',
                  threading=SimpleNamespace(Thread=Mock()),
                  native_diag=SimpleNamespace(staging_position=lambda *a, **kw: (-8192, -8192)),
                  user32=None, os=SimpleNamespace(environ={}), URL='https://google.com/ai',
                  sys=SimpleNamespace(platform='win32'), subprocess=sub)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), 'launch_chrome', 'exec'), ns)
        return ns

    def test_win11_keeps_popen_and_existing_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self.launch_namespace(tmp, False, [])
            ns['launch_chrome']()
            ns['browser_process'].launch_unelevated.assert_not_called()
            ns['storage_paths'].busy_windows_paths.assert_not_called()
            call = ns['subprocess'].Popen.call_args
            args = call.args[0]
            self.assertIn('--window-position=-8192,-8192', args)
            self.assertEqual(call.kwargs['startupinfo'].wShowWindow, 4)
            self.assertIn('stdout', call.kwargs)
            self.assertIn('stderr', call.kwargs)
            self.assertNotIn('--no-sandbox', args)
            self.assertNotIn('--do-not-de-elevate', args)

    def test_elevated_win10_uses_returned_real_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self.launch_namespace(tmp, True, [])
            proc, port = ns['launch_chrome']()
            self.assertIs(proc, ns['browser_process'].launch_unelevated.return_value)
            self.assertEqual(port, 12345)
            ns['subprocess'].Popen.assert_not_called()
            args = ns['browser_process'].launch_unelevated.call_args.args[0]
            self.assertIn('--window-position=-8192,-8192', args)
            self.assertIn('--remote-debugging-port=12345', args)
            self.assertNotIn('--no-sandbox', args)

    def test_busy_profile_is_not_launched_or_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = self.launch_namespace(tmp, True, [7508])
            with self.assertRaises(bp.ProfileInUseError) as caught:
                ns['launch_chrome']()
            self.assertEqual(caught.exception.pids, [7508])
            ns['_set_browser_zoom_preferences'].assert_not_called()
            ns['browser_process'].launch_unelevated.assert_not_called()
            ns['subprocess'].Popen.assert_not_called()
            self.assertFalse(ns['PROFILE'].exists())


class OwnedProcessTests(unittest.TestCase):
    def test_retains_handle_for_wait_and_termination_not_pid_lookup(self):
        api = Mock()
        api.wait.return_value = False
        process = bp.OwnedProcess(api, 0x123456789, 42)
        self.assertIsNone(process.poll())
        process.terminate()
        api.terminate.assert_called_once_with(0x123456789)
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(0.25)
        api.wait.assert_called_with(0x123456789, 250)
        api.wait.return_value = True
        api.exit_code.return_value = 0
        self.assertEqual(process.wait(1), 0)
        self.assertEqual(process.poll(), 0)
        process.terminate()
        self.assertEqual(api.terminate.call_count, 1)
        process.__del__()
        process.__del__()
        api.close.assert_called_once_with(0x123456789)

    def test_exit_259_is_not_mistaken_for_running(self):
        api = Mock()
        api.wait.return_value = True
        api.exit_code.return_value = 259
        self.assertEqual(bp.OwnedProcess(api, 100, 42).poll(), 259)

    def test_token_scope_releases_on_success_and_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                released = Mock()
                @contextmanager
                def token():
                    try:
                        yield 99
                    finally:
                        released()
                api = Mock()
                api.linked_token = token
                api.desktop_token = token
                api.create.return_value = (123, 42)
                if fails:
                    api.create.side_effect = OSError('access denied')
                    with self.assertRaises(bp.DesktopLaunchError):
                        bp.launch_unelevated(['chrome.exe'], api)
                else:
                    process = bp.launch_unelevated(['chrome.exe'], api)
                    self.assertEqual(process.pid, 42)
                self.assertEqual(released.call_count, 2 if fails else 1)
                self.assertEqual(api.create.call_count, 2 if fails else 1)
                api.create.assert_called_with(99, ['chrome.exe'])

    def test_rejected_token_never_creates_process(self):
        @contextmanager
        def rejected():
            raise bp.DesktopLaunchError('Different desktop user')
            yield
        api = Mock()
        api.linked_token = rejected
        api.desktop_token = rejected
        with self.assertRaises(bp.DesktopLaunchError):
            bp.launch_unelevated(['chrome.exe'], api)
        api.create.assert_not_called()

class DesktopTokenValidationTests(unittest.TestCase):
    def test_native_token_validation_and_handle_release(self):
        import ctypes
        for same_user, session, elevated in [(True, 7, 0), (False, 7, 0), (True, 8, 0), (True, 7, 1)]:
            with self.subTest(same_user=same_user, session=session, elevated=elevated):
                api = bp.WindowsAPI.__new__(bp.WindowsAPI)
                closed = []
                api.close = lambda h: closed.append(getattr(h, 'value', h)) if h else None
                api.checked = lambda value, *args: value
                def set_pid(hwnd, pointer):
                    ctypes.cast(pointer, ctypes.POINTER(bp.w.DWORD))[0] = 100
                    return 10
                def open_token(process, access, pointer):
                    ctypes.cast(pointer, ctypes.POINTER(bp.w.HANDLE))[0] = 22 if process == 11 else 33
                    return True
                def duplicate(token, access, security, level, kind, pointer):
                    self.assertEqual(access, 0x02000000)
                    self.assertEqual((level, kind), (2, 1))
                    ctypes.cast(pointer, ctypes.POINTER(bp.w.HANDLE))[0] = 44
                    return True
                def info(token, kind):
                    if kind == 1:
                        value = bp.SID_AND_ATTRIBUTES(123, 0)
                    else:
                        value = bp.w.DWORD((session if token.value == 22 else 7) if kind == 12 else elevated)
                    return ctypes.create_string_buffer(ctypes.string_at(ctypes.byref(value), ctypes.sizeof(value)))
                api.user = SimpleNamespace(GetShellWindow=lambda: 1, GetWindowThreadProcessId=set_pid)
                api.kernel = SimpleNamespace(OpenProcess=lambda *args: 11, GetCurrentProcess=lambda: -1)
                api.advapi = SimpleNamespace(OpenProcessToken=open_token, EqualSid=lambda *args: same_user,
                                              DuplicateTokenEx=Mock(side_effect=duplicate))
                api.token_info = info
                if same_user and session == 7 and not elevated:
                    with api.desktop_token() as token:
                        self.assertEqual(token.value, 44)
                    self.assertCountEqual(closed, [11, 22, 33, 44])
                else:
                    with self.assertRaises(bp.DesktopLaunchError):
                        with api.desktop_token():
                            self.fail('Rejected token was yielded')
                    api.advapi.DuplicateTokenEx.assert_not_called()
                    self.assertCountEqual(closed, [11, 22, 33])

    def test_create_uses_offscreen_args_no_activation_and_closes_thread_handle(self):
        import ctypes
        api = bp.WindowsAPI.__new__(bp.WindowsAPI)
        api.checked = lambda value, *args: value
        api.close = Mock()
        args = ['C:/Program Files/Chromium/chrome.exe', '--user-data-dir=C:/Users/Test/Profile',
                '--window-position=-8192,-8192']
        def create(token, logon, exe, command, flags, env, cwd, startup_ptr, info_ptr):
            self.assertEqual(token, 44)
            self.assertEqual(exe, args[0])
            self.assertEqual(command.value, subprocess.list2cmdline(args))
            self.assertEqual(flags, 0x08000000)
            startup = ctypes.cast(startup_ptr, ctypes.POINTER(bp.STARTUPINFO)).contents
            self.assertEqual(startup.cb, ctypes.sizeof(bp.STARTUPINFO))
            self.assertEqual((startup.dwFlags, startup.wShowWindow), (1, 4))
            result = ctypes.cast(info_ptr, ctypes.POINTER(bp.PROCESS_INFORMATION)).contents
            result.hProcess, result.hThread, result.dwProcessId = 123, 456, 789
            return True
        api.advapi = SimpleNamespace(CreateProcessWithTokenW=create)
        self.assertEqual(api.create(44, args), (123, 789))
        api.close.assert_called_once_with(456)


class LinkedTokenRegressionTests(unittest.TestCase):
    def test_linked_success_does_not_open_desktop(self):
        @contextmanager
        def linked():
            yield 7
        api = Mock()
        api.linked_token = linked
        api.create.return_value = (123, 42)
        process = bp.launch_unelevated(['chrome.exe'], api)
        self.assertEqual(process.token_source, 'linked')
        api.desktop_token.assert_not_called()

    def test_fallback_reports_only_operation_and_error_code(self):
        @contextmanager
        def denied():
            error = OSError('secret path and account must not appear in log')
            error.winerror = 5
            error.operation = 'GetTokenInformation.19'
            raise error
            yield
        @contextmanager
        def desktop():
            yield 8
        api = Mock()
        api.linked_token, api.desktop_token = denied, desktop
        api.create.return_value = (123, 42)
        report = Mock()
        process = bp.launch_unelevated(['chrome.exe'], api, report)
        self.assertEqual(process.token_source, 'desktop')
        report.assert_called_once_with('chromium.token_attempt_failed', source='linked',
                                      operation='GetTokenInformation.19', winerror=5, type='OSError')
        api.create.assert_called_once_with(8, ['chrome.exe'])

    def test_linked_token_is_validated_duplicated_and_all_handles_closed(self):
        import ctypes
        for valid in (True, False):
            with self.subTest(valid=valid):
                api = bp.WindowsAPI.__new__(bp.WindowsAPI)
                api.checked = lambda result, *args: result
                closed = []
                api.close = lambda h: closed.append(h.value) if h else None
                def opened(process, access, pointer):
                    ctypes.cast(pointer, ctypes.POINTER(bp.w.HANDLE))[0] = 11
                    return True
                def duplicate(token, access, security, level, kind, pointer):
                    self.assertEqual(token.value, 22)
                    self.assertEqual(access, 0x02000000)
                    ctypes.cast(pointer, ctypes.POINTER(bp.w.HANDLE))[0] = 33
                    return True
                value = bp.w.HANDLE(22)
                buffer = ctypes.create_string_buffer(ctypes.string_at(ctypes.byref(value), ctypes.sizeof(value)))
                api.kernel = SimpleNamespace(GetCurrentProcess=lambda: -1)
                api.advapi = SimpleNamespace(OpenProcessToken=opened, DuplicateTokenEx=Mock(side_effect=duplicate))
                api.token_info = Mock(return_value=buffer)
                api.validate_token = Mock(side_effect=None if valid else bp.DesktopLaunchError('invalid token'))
                if valid:
                    with api.linked_token() as token:
                        self.assertEqual(token.value, 33)
                    self.assertCountEqual(closed, [11, 22, 33])
                else:
                    with self.assertRaises(bp.DesktopLaunchError):
                        with api.linked_token():
                            self.fail('Invalid token yielded')
                    api.advapi.DuplicateTokenEx.assert_not_called()
                    self.assertCountEqual(closed, [11, 22])
                api.validate_token.assert_called_once()
                self.assertEqual(api.token_info.call_args.args[1], 19)


if __name__ == '__main__':
    unittest.main()
