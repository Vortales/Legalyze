"""Native Qt browser widget; configure loopback-only CDP before QtWebEngine imports."""
import os
import socket
from pathlib import Path

# A per-process port; never expose DevTools on LAN. No sandbox-disabling flags.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    DEBUG_PORT = listener.getsockname()[1]
os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = f'127.0.0.1:{DEBUG_PORT}'

from PyQt6.QtCore import QUrl, QCoreApplication, Qt
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEnginePermission
from PyQt6.QtWebEngineWidgets import QWebEngineView
import diagnostics as diag

HOME = 'https://google.com/ai'
ALLOWED_HOSTS = {'google.com', 'www.google.com', 'gemini.google.com'}
READY_SCRIPT = """(() => {
    if (location.protocol !== 'https:' ||
        !['google.com','www.google.com','gemini.google.com'].includes(location.hostname)) return false;
    if (!['interactive','complete'].includes(document.readyState)) return false;
    return Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]'))
        .some(e => e.getClientRects().length > 0 && !e.disabled && !e.readOnly);
})()"""


class BrowserPane(QWidget):
    def __init__(self, profile_path, parent=None):
        super().__init__(parent)
        root = Path(profile_path)
        root.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile('LegalyzeQtWebEngine', self)
        self.profile.setPersistentStoragePath(str(root / 'storage'))
        self.profile.setCachePath(str(root / 'cache'))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.page.permissionRequested.connect(self._permission)
        # Login popups stay in the same managed view instead of orphan native windows.
        self.page.newWindowRequested.connect(lambda request: request.openIn(self.page))
        self.page.renderProcessTerminated.connect(self._terminated)
        self.view.loadFinished.connect(lambda ok: diag.event('webengine.load_finished', success=bool(ok)))
        for shortcut, callback in [('Ctrl+R', self.view.reload), ('F5', self.view.reload),
                                   ('Alt+Left', self.view.back), ('Ctrl+Shift+H', self.open_home)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, fn=callback: fn())
            self.addAction(action)
        diag.event('webengine.created', port=DEBUG_PORT, profile=str(root))

    def _permission(self, permission):
        origin = permission.origin()
        allowed = (origin.scheme() == 'https' and origin.host() in ALLOWED_HOSTS and
                   permission.permissionType() == QWebEnginePermission.PermissionType.MediaAudioCapture)
        if allowed:
            permission.grant()
        else:
            permission.deny()
        diag.event('webengine.permission', granted=allowed)

    def _terminated(self, status, code):
        diag.event('webengine.render_terminated', code=code, status=str(status))
        self.view.loadFinished.emit(False)

    def open_home(self):
        self.view.setUrl(QUrl(HOME))

    def shutdown(self):
        self.view.stop()
        # Delete the page before its non-default profile; flush cookies/storage via Qt teardown.
        from PyQt6 import sip
        sip.delete(self.page)
        sip.delete(self.view)
        sip.delete(self.profile)
        self.deleteLater()
