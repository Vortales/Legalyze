import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def literal(name):
    tree = ast.parse((ROOT / 'qt_browser.py').read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))


class BackendTests(unittest.TestCase):
    def test_compile(self):
        for p in ROOT.glob('*.py'):
            compile(p.read_text(), str(p), 'exec')

    def test_business_modules_unchanged(self):
        for name in ('config.py', 'crypto_utils.py', 'hwid_gen.py', 'secure_store.py', 'storage_paths.py', 'updater.py'):
            self.assertEqual((ROOT/name).read_bytes(), (ROOT.parent/'TestLegal'/name).read_bytes())

    def test_business_worker_bodies_unchanged(self):
        def classes(path):
            return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text()).body
                    if isinstance(n, ast.ClassDef)}
        before, after = classes(ROOT.parent/'TestLegal'/'main.py'), classes(ROOT/'main.py')
        for name in ('AuthWorker','BalanceWorker','DecrementWorker','PromptLoaderWorker','LoginWindow','TemplateWindow'):
            self.assertEqual(before[name], after[name], name)

    def test_no_external_browser_or_native_embed(self):
        source = (ROOT/'main.py').read_text()
        self.assertNotIn('def launch_chrome(', source)
        self.assertNotIn('native_diag.embed(', source)
        self.assertNotIn('Browser.close', source)
        self.assertNotIn('os._exit(', source)
        self.assertIn('BrowserPane(PROFILE, self.browser_placeholder)', source)
        upload = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'upload_file_advanced')
        self.assertNotIn('|| document.body;', ast.unparse(upload))

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable for JavaScript gate test')
    def test_actual_javascript_gate(self):
        script = literal('READY_SCRIPT')
        harness = '''const vm = require('vm');
const script = SCRIPT;
for (const [host, protocol, visible, disabled, ready, expected] of [
 ['', 'about:', true, false, 'complete', false],
 ['accounts.google.com','https:',true,false,'complete',false],
 ['evilgoogle.com','https:',true,false,'complete',false],
 ['google.com','http:',true,false,'complete',false],
 ['google.com','https:',true,false,'loading',false],
 ['google.com','https:',false,false,'complete',false],
 ['google.com','https:',true,true,'complete',false],
 ['www.google.com','https:',true,false,'complete',true],
 ['gemini.google.com','https:',true,false,'interactive',true]
]) {
 const result = vm.runInNewContext(script, {location:{hostname:host,protocol}, document:{
 readyState:ready, querySelectorAll:()=>[{disabled,readOnly:false,getClientRects:()=>visible?[{}]:[]}]}});
 if (result !== expected) throw Error('Readiness mismatch: '+host);
}
'''.replace('SCRIPT', json.dumps(script))
        subprocess.run(['node', '-e', harness], check=True, capture_output=True)

    def test_blank_page_never_signals_ready(self):
        self.worker_case(False)

    def test_real_editor_signals_ready_once(self):
        self.worker_case(True)

    def worker_case(self, ready):
        from PyQt6.QtCore import QThread, pyqtSignal
        tree = ast.parse((ROOT/'main.py').read_text())
        worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChromeWorker')
        clock = iter(range(0, 10000, 20))
        page = Mock()
        page.eval.return_value = {'value': ready}
        ns = dict(QThread=QThread, pyqtSignal=pyqtSignal, DEBUG_PORT=123, READY_SCRIPT=literal('READY_SCRIPT'),
                  attach_chrome=lambda *a, **k:(Mock(), page), install_purge=Mock(), diag=Mock(),
                  time=SimpleNamespace(monotonic=lambda:next(clock)))
        exec(compile(ast.Module(body=[worker], type_ignores=[]), 'worker', 'exec'), ns)
        obj = ns['ChromeWorker']()
        obj.msleep = lambda ms: None
        received, failed = [], []
        obj.hwnd_ready.connect(received.append)
        obj.failed.connect(lambda:failed.append(True))
        obj.run()
        self.assertEqual(received, [True] if ready else [])
        self.assertEqual(failed, [] if ready else [True])
        self.assertEqual(ns['install_purge'].call_count, 1 if ready else 0)


if __name__ == '__main__':
    unittest.main()
