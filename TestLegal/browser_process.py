"""Direct, unelevated Chromium launch for elevated Windows 10 hosts.

Keep a real process handle rather than following arbitrary profile-matching PIDs.
Windows 11 and non-elevated hosts retain their existing subprocess launch path.
No credentials, command lines, token contents or SID values are logged here.
"""
import ctypes
from ctypes import wintypes as w
from contextlib import contextmanager
import os
import subprocess
import sys


class DesktopLaunchError(RuntimeError):
    pass


class ProfileInUseError(RuntimeError):
    def __init__(self, pids):
        super().__init__('Browser profile already in use before launch')
        self.pids = list(pids)


def needs_desktop_token(major, build, product_type, elevated):
    return major == 10 and 10240 <= build < 22000 and product_type == 1 and elevated is True


def launch_policy():
    if sys.platform != 'win32':
        return {'desktop_token': False, 'elevated': False, 'windows_build': None}
    version = sys.getwindowsversion()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.IsUserAnAdmin.argtypes = []
    shell.IsUserAnAdmin.restype = w.BOOL
    elevated = bool(shell.IsUserAnAdmin())
    return {'desktop_token': needs_desktop_token(version.major, version.build, version.product_type, elevated),
            'elevated': elevated, 'windows_build': version.build}


class STARTUPINFO(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR),
                ('lpTitle', w.LPWSTR), ('dwX', w.DWORD), ('dwY', w.DWORD),
                ('dwXSize', w.DWORD), ('dwYSize', w.DWORD), ('dwXCountChars', w.DWORD),
                ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
                ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                ('lpReserved2', ctypes.POINTER(ctypes.c_ubyte)),
                ('hStdInput', w.HANDLE), ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE),
                ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Sid', w.LPVOID), ('Attributes', w.DWORD)]


class WindowsAPI:
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.advapi = ctypes.WinDLL('advapi32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        signatures = [
            (self.kernel, 'OpenProcess', [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (self.kernel, 'CloseHandle', [w.HANDLE], w.BOOL),
            (self.kernel, 'GetCurrentProcess', [], w.HANDLE),
            (self.kernel, 'WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
            (self.kernel, 'GetExitCodeProcess', [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.kernel, 'TerminateProcess', [w.HANDLE, w.UINT], w.BOOL),
            (self.user, 'GetShellWindow', [], w.HWND),
            (self.user, 'GetWindowThreadProcessId', [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            (self.advapi, 'OpenProcessToken', [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
            (self.advapi, 'GetTokenInformation', [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD,
                                                ctypes.POINTER(w.DWORD)], w.BOOL),
            (self.advapi, 'EqualSid', [w.LPVOID, w.LPVOID], w.BOOL),
            (self.advapi, 'DuplicateTokenEx', [w.HANDLE, w.DWORD, w.LPVOID, ctypes.c_int,
                                             ctypes.c_int, ctypes.POINTER(w.HANDLE)], w.BOOL),
            (self.advapi, 'CreateProcessWithTokenW', [w.HANDLE, w.DWORD, w.LPCWSTR, w.LPWSTR,
                 w.DWORD, w.LPVOID, w.LPCWSTR, ctypes.POINTER(STARTUPINFO),
                 ctypes.POINTER(PROCESS_INFORMATION)], w.BOOL),
        ]
        for dll, name, args, result in signatures:
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result

    @staticmethod
    def checked(result, operation="Win32"):
        if not result:
            error = ctypes.WinError(ctypes.get_last_error())
            error.operation = operation
            raise error
        return result

    def close(self, handle):
        if handle:
            self.kernel.CloseHandle(handle)

    def token_info(self, token, kind):
        size = w.DWORD()
        self.advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        if not size.value:
            self.checked(False, "GetTokenInformation.size." + str(kind))
        buffer = ctypes.create_string_buffer(size.value)
        self.checked(self.advapi.GetTokenInformation(token, kind, buffer, size, ctypes.byref(size)),
                     "GetTokenInformation." + str(kind))
        return buffer

    @contextmanager
    def desktop_token(self):
        """Use the current desktop's non-elevated token, only for the same user/session."""
        shell_process = None
        shell_token, current_token, primary = w.HANDLE(), w.HANDLE(), w.HANDLE()
        try:
            hwnd = self.user.GetShellWindow()
            if not hwnd:
                raise DesktopLaunchError('No interactive desktop shell')
            pid = w.DWORD()
            self.checked(self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)), "GetWindowThreadProcessId")
            shell_process = self.checked(self.kernel.OpenProcess(0x1000, False, pid.value), "OpenProcess.desktop")
            self.checked(self.advapi.OpenProcessToken(shell_process, 0x000A, ctypes.byref(shell_token)), "OpenProcessToken.desktop")
            self.checked(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008,
                                                     ctypes.byref(current_token)), "OpenProcessToken.current")
            self.validate_token(shell_token, current_token)
            # Ask for rights granted by the token DACL, not additional privileges.
            self.checked(self.advapi.DuplicateTokenEx(shell_token, 0x02000000, None, 2, 1, ctypes.byref(primary)),
                         "DuplicateTokenEx.desktop")
            yield primary
        finally:
            for handle in (primary, current_token, shell_token, shell_process):
                self.close(handle)

    def validate_token(self, candidate, current):
        candidate_user = self.token_info(candidate, 1)
        current_user = self.token_info(current, 1)
        same_user = self.advapi.EqualSid(SID_AND_ATTRIBUTES.from_buffer(candidate_user).Sid,
                                        SID_AND_ATTRIBUTES.from_buffer(current_user).Sid)
        candidate_session = w.DWORD.from_buffer(self.token_info(candidate, 12)).value
        current_session = w.DWORD.from_buffer(self.token_info(current, 12)).value
        elevated = w.DWORD.from_buffer(self.token_info(candidate, 20)).value
        if not same_user or candidate_session != current_session or elevated:
            error = DesktopLaunchError('Token must be unelevated and belong to the same user/session')
            error.operation = 'validate_token'
            raise error

    @contextmanager
    def linked_token(self):
        """Prefer our own UAC-linked limited token; no access to Explorer needed."""
        current, linked, primary = w.HANDLE(), w.HANDLE(), w.HANDLE()
        try:
            self.checked(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008,
                         ctypes.byref(current)), 'OpenProcessToken.current')
            info = self.token_info(current, 19)  # TOKEN_LINKED_TOKEN owns a HANDLE to close.
            linked = w.HANDLE(w.HANDLE.from_buffer(info).value)
            self.validate_token(linked, current)
            self.checked(self.advapi.DuplicateTokenEx(linked, 0x02000000, None, 2, 1, ctypes.byref(primary)),
                         'DuplicateTokenEx.linked')
            yield primary
        finally:
            for handle in (primary, linked, current):
                self.close(handle)

    def create(self, token, args):
        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = 1  # STARTF_USESHOWWINDOW, same as the normal Popen path.
        startup.wShowWindow = 4  # SW_SHOWNOACTIVATE: don't hide the owned _1 window.
        info = PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        # No handle inheritance; native Chromium diagnostics still use --log-file.
        self.checked(self.advapi.CreateProcessWithTokenW(
            token, 0, args[0], command, 0x08000000, None, os.getcwd(),
            ctypes.byref(startup), ctypes.byref(info)), "CreateProcessWithTokenW")
        self.close(info.hThread)
        return info.hProcess, info.dwProcessId

    def wait(self, handle, milliseconds):
        value = self.kernel.WaitForSingleObject(handle, milliseconds)
        if value == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        if value not in (0, 258):
            raise DesktopLaunchError('Unexpected process wait result')
        return value == 0

    def exit_code(self, handle):
        code = w.DWORD()
        self.checked(self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
        return code.value

    def terminate(self, handle):
        self.checked(self.kernel.TerminateProcess(handle, 1))


class OwnedProcess:
    """Small Popen-compatible interface backed by a retained, non-reopened HANDLE."""
    def __init__(self, api, handle, pid):
        self._api, self._handle, self.pid = api, handle, pid
        self.returncode = None

    def poll(self):
        if self.returncode is None and self._api.wait(self._handle, 0):
            self.returncode = self._api.exit_code(self._handle)
        return self.returncode

    def wait(self, timeout=None):
        milliseconds = 0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, max(0, int(timeout * 1000)))
        if self.returncode is None:
            if not self._api.wait(self._handle, milliseconds):
                raise subprocess.TimeoutExpired('owned Chromium', timeout)
            self.returncode = self._api.exit_code(self._handle)
        return self.returncode

    def terminate(self):
        if self.poll() is None:
            try:
                self._api.terminate(self._handle)
            except OSError:
                if self.poll() is None:
                    raise

    kill = terminate

    def __del__(self):
        handle = getattr(self, '_handle', None)
        if handle:
            self._handle = None
            self._api.close(handle)


def launch_unelevated(args, api=None, report=None):
    api = api if api is not None else WindowsAPI()
    report = report if report is not None else lambda *args, **kwargs: None
    attempts = []
    for source, token_factory in (("linked", api.linked_token), ("desktop", api.desktop_token)):
        try:
            with token_factory() as token:
                handle, pid = api.create(token, args)
            process = OwnedProcess(api, handle, pid)
            process.token_source = source
            return process
        except (OSError, DesktopLaunchError) as exc:
            attempt = {"source": source, "operation": getattr(exc, 'operation', 'token_setup'),
                       "winerror": getattr(exc, 'winerror', None), "type": type(exc).__name__}
            attempts.append(attempt)
            report('chromium.token_attempt_failed', **attempt)
    # No elevated/no-sandbox fallback and no process adoption by a matching profile.
    error = DesktopLaunchError('Cannot launch as same user; run Legalyze and its IDE without administrator rights')
    error.winerror = attempts[-1]['winerror']
    error.operation = attempts[-1]['operation']
    error.attempts = attempts
    raise error
