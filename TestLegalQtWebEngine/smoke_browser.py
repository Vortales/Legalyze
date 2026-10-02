"""Run on Windows before testing the real service: no auth, uploads or network required."""
import sys
from qt_browser import BrowserPane, READY_SCRIPT
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from storage_paths import app_dir
import diagnostics as diag

diag.setup()
app = QApplication(sys.argv)
pane = BrowserPane(app_dir() / 'qtwebengine-smoke-profile')
pane.resize(480, 640)
pane.show()
result = {'exit': 1}

def finished(ok):
    if not ok:
        app.quit()
        return
    def checked(ready):
        result['exit'] = 0 if ready is True else 1
        print('QtWebEngine local page readiness:', ready)
        app.quit()
    pane.page.runJavaScript(READY_SCRIPT, checked)

pane.view.loadFinished.connect(finished)
from PyQt6.QtCore import QUrl
pane.view.setHtml('<html><body><h2>Legalyze QtWebEngine smoke test</h2>'
                  '<textarea></textarea><input type="file"></body></html>', QUrl('https://google.com/ai'))
QTimer.singleShot(20000, app.quit)
app.exec()
pane.shutdown()
sys.exit(result['exit'])
