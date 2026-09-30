import ast
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'TestLegalQtWebEngine'
ORIGINAL = ROOT.parent / 'TestLegal'
sys.path.insert(0, str(ROOT))

import speech_backend  # noqa: E402
import win32_hotkeys  # noqa: E402
import web_compat  # noqa: E402


def literal(name, module='web_compat.py'):
    tree = ast.parse((ROOT / module).read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))


def class_dump(path, names):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.ClassDef) and n.name in names}


def func_dump(path, names):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name in names}


NODE = shutil.which('node')
skip_node = unittest.skipUnless(NODE, 'Node unavailable')


class SourceTests(unittest.TestCase):
    def test_compile(self):
        for p in ROOT.glob('*.py'):
            compile(p.read_text(), str(p), 'exec')

    def test_business_modules_unchanged(self):
        for name in ('config.py', 'crypto_utils.py', 'hwid_gen.py', 'secure_store.py',
                     'storage_paths.py', 'updater.py'):
            self.assertEqual((ROOT / name).read_bytes(), (ORIGINAL / name).read_bytes(), name)

    def test_business_worker_bodies_unchanged(self):
        names = ('AuthWorker', 'BalanceWorker', 'DecrementWorker', 'PromptLoaderWorker',
                 'LoginWindow', 'TemplateWindow')
        self.assertEqual(class_dump(ORIGINAL / 'main.py', names),
                         class_dump(ROOT / 'main.py', names))

    def test_chat_automation_preserved_from_tested_variant(self):
        before = ast.parse((BASE / 'main.py').read_text())
        after = ast.parse((ROOT / 'main.py').read_text())
        js_names = ('JS_START_RECORDING', 'JS_STOP_AND_SEND', 'JS_CHECK_RECORDING',
                    'JS_CHECK_FILES', 'JS_FIND_INPUT', 'JS_REMOVE_ALL_FILES', 'JS_CHECK_END',
                    'JS_DISABLE_CONTEXT_MENU', 'JS_DISABLE_DRAG')

        def literals(tree):
            out = {}
            for n in tree.body:
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id in js_names:
                            out[t.id] = ast.literal_eval(n.value)
            return out

        self.assertEqual(literals(before), literals(after))
        self.assertEqual(func_dump(BASE / 'main.py', ('upload_file_advanced', 'check_state',
                                                      '_chat_has_file', 'log_page_probe')),
                         func_dump(ROOT / 'main.py', ('upload_file_advanced', 'check_state',
                                                      '_chat_has_file', 'log_page_probe')))

    def test_no_external_browser_native_embed_or_forced_exit(self):
        source = (ROOT / 'main.py').read_text()
        self.assertNotIn('def launch_chrome(', source)
        self.assertNotIn('def nativeEvent(', source)
        self.assertNotIn('native_diag', source)
        self.assertNotIn('os._exit(', source)
        self.assertIn('GlobalHotkeys(', source)
        self.assertIn('install_compat(self.browser, self.page)', source)
        self.assertIn('grant_mic_permission(self.browser, self.page)', source)
        upload = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                      and n.name == 'upload_file_advanced')
        self.assertNotIn('|| document.body;', ast.unparse(upload))

    def test_purge_protects_input_and_microphone(self):
        purge = literal('JS_PURGE', 'main.py')
        self.assertIn('isProtected', purge)
        self.assertIn('PROTECTED_LABEL', purge)
        self.assertNotIn("'svg[width=\"26\"]", purge)
        for token in ('input-plate', 'икрофон', 'voice'):
            self.assertIn(token, purge)

    def test_hotkey_service_is_thread_independent(self):
        tree = ast.parse((ROOT / 'win32_hotkeys.py').read_text())
        source = ast.unparse(tree)
        self.assertIn('RegisterHotKey', source)
        self.assertIn('GetAsyncKeyState', source)
        self.assertIn('HWND_MESSAGE', source)
        self.assertIn('PeekMessageW', source)


class SpeechBackendTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.engine = speech_backend.DictationEngine(
            lambda kind, data: self.events.append((kind, data)))

    def test_event_translation(self):
        gen = self.engine._gen
        self.engine._handle(gen, {'type': 'started'})
        self.engine._handle(gen, {'type': 'hypothesis', 'text': 'привет'})
        self.engine._handle(gen, {'type': 'result', 'text': 'привет мир', 'confidence': 0.8})
        self.engine._handle(gen, {'type': 'error', 'code': 'not-allowed'})
        self.engine._handle(gen, {'type': 'error', 'code': 'weird'})
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ['started', 'hypothesis', 'result', 'error', 'error'])
        self.assertEqual(self.events[2][1], {'text': 'привет мир', 'confidence': 0.8})
        self.assertEqual(self.events[4][1]['code'], 'network')  # unknown mapped

    def test_finish_reports_no_speech_once(self):
        gen = self.engine._gen
        self.engine._ended = False
        self.engine._finish(gen, force_error=True)
        self.engine._finish(gen, force_error=True)
        self.assertEqual([k for k, _ in self.events], ['error', 'end'])
        self.assertEqual(self.events[0][1], {'code': 'no-speech'})

    def test_stale_generation_is_dropped(self):
        self.engine._ended = False
        self.engine._event(self.engine._gen + 5, 'result', {'text': 'старое', 'confidence': 1})
        self.assertEqual(self.events, [])

    def test_error_names_are_api_compatible(self):
        self.assertIn('language-not-supported', speech_backend.ERROR_NAMES)
        self.assertIn('not-allowed', speech_backend.ERROR_NAMES)


class HotkeyPollingTests(unittest.TestCase):
    def test_poll_fallback_fires_on_edge_with_debounce(self):
        fired = []
        state = {'down': False}
        keys = SimpleNamespace(GetAsyncKeyState=lambda vk: 0x8000 if state['down'] else 0)
        import win32_hotkeys as wk
        old = wk._win_dlls
        wk._win_dlls = lambda: (keys, None)
        try:
            hk = wk.GlobalHotkeys(on_event=lambda key_id: fired.append(key_id))
            hk._poll_vk = {wk.HOTKEY_MIC_ID: 0x72}
            now = time.monotonic()
            hk._poll_step(now)  # not pressed
            state['down'] = True
            hk._poll_step(now)  # press -> fire
            hk._poll_step(now + 0.05)  # held -> no repeat
            state['down'] = False
            hk._poll_step(now + 0.1)  # release
            state['down'] = True
            hk._poll_step(now + 0.2)  # bounce within debounce -> ignored
            state['down'] = False
            hk._poll_step(now + 0.3)  # release
            state['down'] = True
            hk._poll_step(now + 0.6)  # real second press -> fire
        finally:
            wk._win_dlls = old
        self.assertEqual(fired, [wk.HOTKEY_MIC_ID, wk.HOTKEY_MIC_ID])

    def test_status_dedup(self):
        statuses = []
        hk = win32_hotkeys.GlobalHotkeys(on_event=lambda i: None,
                                         on_status=lambda ok, m: statuses.append((ok, m)))
        hk._emit_status(False, 'заняты: окно')
        hk._emit_status(False, 'заняты: окно')
        hk._emit_status(True, 'ok')
        self.assertEqual(statuses, [(False, 'заняты: окно'), (True, 'ok')])


@skip_node
class JavascriptTests(unittest.TestCase):
    def run_node(self, script):
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            self.fail('node failed: ' + res.stderr[-2000:])

    def test_ready_script_gate(self):
        script = literal('READY_SCRIPT', 'qt_browser.py')
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
        self.run_node(harness)

    def test_speech_shim_feature_detection_and_events(self):
        shim = literal('SPEECH_SHIM_JS')
        harness = '''
const vm = require('vm');
const signals = {};
const bridgeCalls = [];
const bridge = {
  start: (lang, c, i) => bridgeCalls.push(['start', lang, c, i]),
  stop: () => bridgeCalls.push(['stop']),
  abort: () => bridgeCalls.push(['abort']),
  started: {connect: f => signals.started = f},
  speechStart: {connect: f => signals.speechstart = f},
  speechEnd: {connect: f => signals.speechend = f},
  hypothesis: {connect: f => signals.hypothesis = f},
  result: {connect: f => signals.result = f},
  rejected: {connect: f => signals.rejected = f},
  error: {connect: f => signals.error = f},
  ended: {connect: f => signals.ended = f}
};
const sandbox = {
  window: {__legalyzeSpeechBridge: bridge, __legalyzeSpeechBridgeWait: 10},
  navigator: {language: 'ru-RU'},
  setTimeout, clearTimeout, Promise, console
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);

const w = sandbox.window;
if (typeof w.SpeechRecognition !== 'function') throw Error('SpeechRecognition missing');
if (w.__legalyzeSpeech.mode !== 'shim') throw Error('mode');
if (!('webkitSpeechRecognition' in w)) throw Error('webkit prefix missing');

const events = [];
const rec = new w.SpeechRecognition();
for (const n of ['audiostart','start','speechstart','result','error','end','audioend','nomatch'])
  rec['on' + n] = (e) => events.push([n, e]);

// detection surface used by the site
if (!('SpeechRecognition' in w || 'webkitSpeechRecognition' in w)) throw Error('detection');

rec.interimResults = true;
rec.start();
if (bridgeCalls[0][0] !== 'start' || bridgeCalls[0][1] !== 'ru-RU') throw Error('bridge start');
signals.started();
signals.speechstart();
signals.hypothesis('при');
signals.result('привет мир', 0.87);
if (bridgeCalls[1][0] !== 'stop') throw Error('auto stop expected, got ' + JSON.stringify(bridgeCalls));
signals.ended();

const names = events.map(e => e[0]).join(',');
if (names !== 'audiostart,start,speechstart,result,result,audioend,end') throw Error('order: ' + names);
const resEvents = events.filter(e => e[0] === 'result');
if (resEvents[0][1].results[0].isFinal) throw Error('first result must be interim');
if (!resEvents[1][1].results[0].isFinal) throw Error('second result must be final');
const res = resEvents[1][1].results;
if (res[0][0].transcript !== 'привет мир') throw Error('final result');
if (resEvents[1][1].resultIndex !== 0) throw Error('resultIndex');

// error path and double-start guard
const rec2 = new w.SpeechRecognition();
rec2.onerror = (e) => events.push(['error2', e.error]);
rec2.onend = () => events.push(['end2', {}]);
rec2.start();
let threw = false;
try { rec2.start(); } catch (e) { threw = e.name === 'InvalidStateError'; }
if (!threw) throw Error('InvalidStateError expected');
signals.error('not-allowed');
signals.ended();
if (!events.some(e => e[0] === 'error2' && e[1] === 'not-allowed')) throw Error('error event');
if (!events.some(e => e[0] === 'end2')) throw Error('end event');

// abort path
const rec3 = new w.SpeechRecognition();
rec3.onerror = () => {};
rec3.onend = () => events.push(['end3', {}]);
rec3.start();
rec3.abort();
if (!events.some(e => e[0] === 'end3')) throw Error('abort end');
console.log('shim ok');
'''
        self.run_node(harness.replace('SHIM', json.dumps(literal('SPEECH_SHIM_JS'))))

    def test_speech_shim_without_bridge_reports_network(self):
        shim = literal('SPEECH_SHIM_JS')
        harness = '''
const vm = require('vm');
const sandbox = {
  window: {__legalyzeSpeechBridgeWait: 5},
  navigator: {language: 'ru-RU'},
  setTimeout, console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);
const rec = new sandbox.window.SpeechRecognition();
let err = null, ended = false;
rec.onerror = (e) => { err = e.error; };
rec.onend = () => { ended = true; };
rec.start();
setTimeout(() => {
  if (err !== 'network' || !ended) throw Error('network error expected: ' + err + '/' + ended);
  console.log('no-bridge ok');
}, 50);
'''
        self.run_node(harness.replace('SHIM', json.dumps(shim)))

    def test_ua_brand_patch(self):
        harness = '''
const vm = require('vm');
const sandbox = {
  navigator: {userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'},
  window: {}, setTimeout, Promise, console
};
vm.createContext(sandbox);
vm.runInContext(UA, sandbox);
const d = sandbox.navigator.userAgentData;
const brands = d.brands.map(b => b.brand);
if (!brands.includes('Google Chrome') || !brands.includes('Chromium')) throw Error('brands: ' + brands);
if (d.brands[0].version !== '151') throw Error('version');
d.getHighEntropyValues({}).then(v => {
  if (v.platform !== 'Windows' || v.uaFullVersion.indexOf('151') !== 0) throw Error('high entropy');
  console.log('ua ok');
});
'''
        self.run_node(harness.replace('UA', json.dumps(literal('UA_BRAND_JS'))))

    def test_capability_probe(self):
        harness = '''
const vm = require('vm');
const sandbox = {
  window: {isSecureContext: true},
  navigator: {userAgentData: {brands: [{brand: 'Chromium', version: '151'}]},
              mediaDevices: {getUserMedia: () => {}}},
  MediaRecorder: function(){}, console
};
vm.createContext(sandbox);
const caps = vm.runInContext(CAPS, sandbox);
if (!caps.secure || !caps.mediaDevices || !caps.mediaRecorder) throw Error(JSON.stringify(caps));
if (caps.speechNative || caps.speechShim) throw Error('speech flags');
if (caps.brands !== 'Chromium/151') throw Error(caps.brands);
console.log('caps ok');
'''
        self.run_node(harness.replace('CAPS', json.dumps(literal('CAPABILITY_JS'))))

    def test_purge_hides_header_but_never_mic(self):
        purge = literal('JS_PURGE', 'main.py')
        harness = '''
const vm = require('vm');
function el(name, opts) {
  opts = opts || {};
  const self = {
    nodeType: 1,
    name: name,
    style: {setProperty: (k, v) => { self.hiddenStyle = k + ':' + v; }},
    hiddenStyle: '',
    removed: false,
    remove() { self.removed = true; },
    _parent: opts.parent || null,
    _matches: opts.matches || [],
    _label: opts.label || '',
    _text: opts.text || '',
    _isInputArea: !!opts.inputArea,
    matches(sel) {
      const parts = sel.split(',').map(s => s.trim());
      return self._matches.some(m => parts.includes(m));
    },
    querySelectorAll(sel) { return (opts.all || []).filter(e => e.matches(sel)); },
    getAttribute(name) { return name === 'aria-label' ? self._label : (name === 'data-xid' ? (opts.xid || '') : (name === 'title' ? '' : null)); },
    closest(sel) {
      let n = self;
      while (n) { if (n._isInputArea) return n; n = n._parent; }
      return null;
    },
    textContent: opts.text || '',
    addEventListener() {}
  };
  return self;
}
const header = el('header', {matches: ['header#gb']});
const svgIcon = el('svg', {matches: []});
const form = el('form', {inputArea: true});
const mic = el('button', {parent: form, label: 'Микрофон', xid: 'input-plate-voice-button', matches: ['div.eT9Cje']});
const send = el('button', {parent: form, label: 'Отправить', matches: ['div.qEn1od']});
const root = el('html', {all: [header, svgIcon, mic, send]});
const sandbox = {
  window: {addEventListener() {}},
  document: {documentElement: root, addEventListener() {}},
  MutationObserver: class { observe() {} },
  console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(PURGE, sandbox);
sandbox.window.__purgeRun();
if (!header.hiddenStyle || !header.removed) throw Error('header must be hidden');
if (mic.hiddenStyle || mic.removed) throw Error('mic must survive purge even when matching');
if (send.hiddenStyle || send.removed) throw Error('send must survive purge even when matching');
if (svgIcon.hiddenStyle || svgIcon.removed) throw Error('generic svg must survive purge');
console.log('purge ok');
'''
        self.run_node(harness.replace('PURGE', json.dumps(purge)))

    def test_compatibility_bundle_parses(self):
        qwebchannel_stub = '''var qt = {webChannelTransport: {}};
var QWebChannel = function(transport, cb) {
  const sig = () => ({connect: () => {}});
  cb({objects: {legalyzeSpeech: {
    reportCapabilities() {}, started: sig(), speechStart: sig(), speechEnd: sig(),
    hypothesis: sig(), result: sig(), rejected: sig(), error: sig(), ended: sig()
  }}});
};'''
        harness = '''
const vm = require('vm');
const sandbox = {window: {}, navigator: {language: 'ru-RU'}, setTimeout, Promise, console};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(BUNDLE, sandbox);
if (typeof sandbox.window.SpeechRecognition !== 'function') throw Error('bundle');
if (!sandbox.window.__legalyzeSpeechBridge) throw Error('bridge after channel init');
console.log('bundle ok');
'''
        bundle = '\n'.join([literal('UA_BRAND_JS'), literal('SPEECH_SHIM_JS'),
                            qwebchannel_stub, literal('CHANNEL_INIT_JS')])
        self.run_node(harness.replace('BUNDLE', json.dumps(bundle)))


class WorkerStateTests(unittest.TestCase):
    def worker_case(self, ready):
        tree = ast.parse((ROOT / 'main.py').read_text())
        worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChromeWorker')
        clock = iter(range(0, 10000, 20))
        page = Mock()
        page.eval.return_value = {'value': ready}
        calls = []
        ns = dict(
            QThread=type('QThread', (), {'__init__': lambda self, parent=None: None,
                                         'msleep': lambda self, ms: None,
                                         'isInterruptionRequested': lambda self: False}),
            pyqtSignal=lambda *a: None,
            time=SimpleNamespace(monotonic=lambda: next(clock)),
            attach_chrome=lambda *a, **k: (Mock(), page),
            install_compat=lambda *a, **k: calls.append('compat'),
            grant_mic_permission=lambda *a, **k: calls.append('grant'),
            install_purge=lambda *a, **k: calls.append('purge'),
            READY_SCRIPT='x', DEBUG_PORT=1, diag=Mock(),
        )
        worker_node = ast.Module(body=[worker], type_ignores=[])
        exec(compile(worker_node, 'worker', 'exec'), ns)
        obj = ns['ChromeWorker']()
        received, failed = [], []
        obj.hwnd_ready = SimpleNamespace(connect=lambda f: received.append(f),
                                         emit=lambda v: received.append(v))
        obj.failed = SimpleNamespace(connect=lambda f: failed.append(f),
                                     emit=lambda: failed.append(True))
        obj.failure = {}
        obj.run()
        return calls, received, failed

    def test_blank_page_never_signals_ready(self):
        calls, received, failed = self.worker_case(False)
        self.assertIn('compat', calls)  # compatibility is installed before readiness
        self.assertIn('grant', calls)
        self.assertNotIn('purge', calls)
        self.assertIn(True, failed)

    def test_real_editor_signals_ready_once(self):
        calls, received, failed = self.worker_case(True)
        self.assertEqual(calls, ['compat', 'grant', 'purge'])
        self.assertIn(True, received)
        self.assertEqual(failed, [])


if __name__ == '__main__':
    unittest.main()
