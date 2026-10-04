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
        self.assertIn('HotkeyMonitor(', source)
        self.assertIn('install_compat(self.browser, self.page)', source)
        self.assertIn('grant_mic_permission(self.browser, self.page)', source)
        upload = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                      and n.name == 'upload_file_advanced')
        self.assertNotIn('|| document.body;', ast.unparse(upload))

    def test_purge_protects_input_and_microphone(self):
        purge = literal('JS_PURGE', 'web_compat.py')
        self.assertIn('isProtected', purge)
        self.assertIn('PROTECTED_LABEL', purge)
        self.assertIn('svg[width="26"][height="26"][viewBox="0 0 24 24"]', purge)
        for token in ('input-plate', 'икрофон', 'voice'):
            self.assertIn(token, purge)

    def test_requirements_have_no_invented_package(self):
        text = (ROOT / 'requirements.txt').read_text()
        self.assertNotIn('PyQt6-WebChannel', text)  # QtWebChannel ships inside PyQt6
        self.assertIn('PyQt6-WebEngine', text)

    def test_hotkey_monitor_is_polling_and_never_reports_conflict(self):
        source = ast.unparse(ast.parse((ROOT / 'win32_hotkeys.py').read_text()))
        # Никаких потоков, скрытых окон и циклов сообщений: только опрос.
        self.assertIn('GetAsyncKeyState', source)
        self.assertIn('argtypes', source)  # без усечения указателей на Win64
        self.assertNotIn('CreateWindowEx', source)
        self.assertNotIn('PeekMessageW', source)
        self.assertNotIn('threading.Thread', source)
        self.assertNotIn('WNDPROC', source)
        # RegisterHotKey — только подавление проброса клавиши, не условие работы.
        self.assertIn('RegisterHotKey', source)
        self.assertIn('try_suppress', source)
        main = (ROOT / 'main.py').read_text()
        self.assertIn('HotkeyMonitor(', main)
        self.assertIn('_hotkey_tick', main)
        # Ложный "конфликт" удалён из кода полностью.
        self.assertNotIn('Конфликт горячих клавиш', main)
        # Опрос QTimer в главном потоке Qt.
        self.assertIn('self._hotkey_timer', main)

    def test_mic_button_js_in_early_bundle(self):
        browser = (ROOT / 'qt_browser.py').read_text()
        chunk_line = next(line for line in browser.splitlines() if 'chunks = [' in line)
        self.assertIn('MIC_BUTTON_JS', chunk_line)
        self.assertLess(chunk_line.index('MIC_BUTTON_JS'), chunk_line.index('JS_PURGE'))
        for name in ('JS_MIC_TOGGLE', 'JS_MIC_STOP', 'MIC_BUTTON_JS'):
            self.assertTrue(literal(name), name)

    def test_no_flash_cover_until_clean_reveal(self):
        main = (ROOT / 'main.py').read_text()
        # Шторка остаётся поверх страницы до загрузки и зачистки: никаких
        # миганий и видимости элементов браузера во время ожидания страницы.
        self.assertIn('_reveal_page', main)
        self.assertIn('self.browser_cover.show()', main)
        self.assertIn('QTimer.singleShot(250, self._reveal_page)', main)
        self.assertNotIn('self._hide_overlay()  # Google login/consent', main)

    def test_renderer_crash_auto_recovery(self):
        main = (ROOT / 'main.py').read_text()
        browser = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('self.crashed = True', browser)
        self.assertIn('_crash_recoveries', main)
        self.assertIn('_recover_web', main)
        self.assertIn('Восстановление страницы', main)

    def test_mic_prefers_own_dictation_and_gates_readiness(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        source = ast.unparse(func)
        self.assertIn('JS_MIC_TOGGLE', source)      # настоящая диктовка — первая
        self.assertIn('JS_START_RECORDING', source)  # штатный сценарий — запасной
        self.assertIn('mic.dictation_started', source)
        self.assertIn('_chat_ready', source)

    def test_bundle_joins_are_asi_safe(self):
        # IIFE-чанки вида })() и (() => { нельзя склеивать одним переводом
        # строки: это разбирается как вызов и роняет остаток бандла.
        browser = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('ASI hazard', browser)
        self.assertIn("';" + chr(92) + "n'.join(chunks)", browser)
        self.assertTrue(literal('MIC_BUTTON_JS').rstrip().endswith(');'))

    def test_native_speech_is_never_used_and_cover_rises(self):
        web = (ROOT / 'web_compat.py').read_text()
        self.assertIn('__legalyzeNativeSpeechRecognition', web)
        self.assertNotIn("mode: 'native'", web)  # отказ от нативного SpeechRecognition
        self.assertIn('window.LegalyzeSpeechRecognition = SpeechRecognition', web)
        main = (ROOT / 'main.py').read_text()
        self.assertIn('self.browser_cover.raise_()', main)
        # восстановление останавливает потоки с мёртвым CDP-соединением
        tree = ast.parse(main)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_recover_web')
        self.assertIn('upload_thread', ast.unparse(func))

    def test_zoom_restored_to_native_67(self):
        source = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('ZOOM_FACTOR = 2.0 / 3.0', source)
        self.assertIn('setZoomFactor(ZOOM_FACTOR)', source)
        self.assertIn('JS_PURGE', source)  # early purge: no flash of site chrome


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
    def test_tick_fires_on_edge_with_debounce(self):
        fired = []
        state = {'down': False}
        mon = win32_hotkeys.HotkeyMonitor(on_event=fired.append)
        mon._get_state = lambda vk: state['down']
        self.assertTrue(mon.set_keys(0, 0x72))  # only mic key
        mon.tick(0.0)            # not pressed
        state['down'] = True
        mon.tick(0.1)            # press -> fire
        mon.tick(0.2)            # held -> no repeat
        state['down'] = False
        mon.tick(0.3)            # release
        state['down'] = True
        mon.tick(0.4)            # bounce within 0.35s debounce -> ignored
        state['down'] = False
        mon.tick(0.5)            # release
        state['down'] = True
        mon.tick(0.6)            # real second press -> fire
        self.assertEqual(fired, [win32_hotkeys.HOTKEY_MIC_ID, win32_hotkeys.HOTKEY_MIC_ID])

    def test_two_keys_fire_independently(self):
        fired = []
        state = {0x71: False, 0x72: False}
        mon = win32_hotkeys.HotkeyMonitor(on_event=fired.append)
        mon._get_state = lambda vk: state[vk]
        mon.set_keys(0x71, 0x72)
        mon.tick(0.0)
        state[0x71] = True
        mon.tick(0.1)
        state[0x71] = False
        state[0x72] = True
        mon.tick(0.6)
        self.assertEqual(fired, [win32_hotkeys.HOTKEY_TOGGLE_ID, win32_hotkeys.HOTKEY_MIC_ID])

    def test_set_keys_always_ready_and_status_never_conflicts(self):
        mon = win32_hotkeys.HotkeyMonitor(on_event=lambda i: None)
        # Опрос физической клавиатуры работает всегда — set_keys не может
        # вернуть False, статус никогда не содержит "конфликт".
        self.assertIs(mon.set_keys(0x71, 0x72), True)
        self.assertEqual(mon.status_text, 'Готов к работе')
        self.assertIs(mon.clear(), True)
        self.assertNotIn('onфликт', mon.status_text)
        self.assertNotIn('onflict', mon.status_text)

    def test_suppression_is_optional(self):
        mon = win32_hotkeys.HotkeyMonitor(on_event=lambda i: None)
        mon.set_keys(0x71, 0x72)
        # Отказ подавления (в т.ч. на не-Windows) ничего не блокирует.
        self.assertIn(mon.try_suppress(0), (True, False))
        self.assertTrue(mon.set_keys(0x71, 0x72))
        mon.release()


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

    def test_purge_hides_header_and_logo_but_never_mic(self):
        purge = literal('JS_PURGE', 'web_compat.py')
        harness = '''
const vm = require('vm');
function el(name, opts) {
  opts = opts || {};
  const self = {
    nodeType: 1,
    name: name,
    style: {setProperty: (k, v) => { self.hiddenStyle = (self.hiddenStyle || '') + k + ':' + v + ';'; }},
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
const logoSvg = el('svg', {matches: ['svg[width="26"][height="26"][viewBox="0 0 24 24"]']});
const form = el('form', {inputArea: true});
const mic = el('button', {parent: form, label: 'Микрофон', xid: 'input-plate-voice-button', matches: ['div.eT9Cje']});
const micIcon = el('svg', {parent: mic, matches: ['svg[width="26"][height="26"][viewBox="0 0 24 24"]']});
const send = el('button', {parent: form, label: 'Отправить', matches: ['div.qEn1od']});
const root = el('html', {all: [header, logoSvg, mic, micIcon, send]});
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
if (!logoSvg.hiddenStyle || !logoSvg.removed) throw Error('G-logo svg must be hidden');
if (mic.hiddenStyle || mic.removed) throw Error('mic must survive purge even when matching');
if (micIcon.hiddenStyle || micIcon.removed) throw Error('mic icon must survive purge even when matching logo selector');
if (send.hiddenStyle || send.removed) throw Error('send must survive purge even when matching');
console.log('purge ok');
'''
        self.run_node(harness.replace('PURGE', json.dumps(purge)))

    def test_consent_autoclick_accepts_only_consent_surfaces(self):
        harness = '''
const vm = require('vm');
function button(label, opts) {
  opts = opts || {};
  const el = {
    nodeType: 1,
    _label: label, _parent: opts.parent || null, clicked: 0,
    innerText: label, value: '',
    getAttribute(n) { return n === 'aria-label' ? el._label : null; },
    closest(sel) {
      let n = el;
      while (n) { if (n._isDialog) return n; n = n._parent; }
      return null;
    },
    click() { el.clicked++; }
  };
  return el;
}
const dialog = {nodeType: 1, _isDialog: true, _parent: null};
const accept = button('Принять все', {parent: dialog});
const random = button('Отправить', {parent: dialog});
const floating = button('Accept all');
const root = {
  nodeType: 1,
  querySelectorAll() { return [accept, random, floating]; },
  matches() { return false; }
};
const sandbox = {
  window: {addEventListener() {}},
  document: {body: {}, documentElement: root, addEventListener() {},
             querySelectorAll: (s) => root.querySelectorAll(s)},
  location: {hostname: 'gemini.google.com'},
  setInterval: (fn) => { fn(); return 1; }, clearInterval: () => {},
  console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(CONSENT, sandbox);
if (accept.clicked !== 1) throw Error('consent accept must be clicked, got ' + accept.clicked);
if (random.clicked !== 0) throw Error('unrelated button must not be clicked');
if (floating.clicked !== 0) throw Error('bare Accept outside consent surface must not be clicked');
console.log('consent ok');
'''
        self.run_node(harness.replace('CONSENT', json.dumps(literal('CONSENT_AUTOCLICK_JS'))))

    def test_webchannel_lite_matches_qt_protocol(self):
        harness = '''
const vm = require('vm');
const sent = [];
const handlers = {};
const transport = {
  send(raw) {
    const msg = JSON.parse(raw);
    sent.push(msg);
    if (msg.type === 3) {
      transport.onmessage({data: JSON.stringify({type: 10, id: msg.id, data: {
        legalyzeSpeech: {
          id: 'legalyzeSpeech',
          methods: [['start', 0], ['stop', 1], ['abort', 2], ['reportCapabilities', 3]],
          signals: [['started', 0], ['hypothesis', 1], ['result', 2], ['error', 3], ['ended', 4]],
          properties: [], enums: {}
        }
      }})});
    }
  },
  onmessage: null
};
const sandbox = {window: {}, console, JSON};
vm.createContext(sandbox);
vm.runInContext(LITE, sandbox);
let bridge = null;
new sandbox.window.QWebChannel(transport, (ch) => { bridge = ch.objects.legalyzeSpeech; });
if (!bridge) throw Error('init handshake failed');
if (!sent.some(m => m.type === 3)) throw Error('init message');
if (!sent.some(m => m.type === 4)) throw Error('idle after init');
const started = [];
bridge.started.connect(() => started.push(1));
if (!sent.some(m => m.type === 7 && m.signal === 0)) throw Error('connectToSignal');
transport.onmessage({data: JSON.stringify({type: 1, object: 'legalyzeSpeech', signal: 0, args: []})});
if (started.length !== 1) throw Error('signal dispatch');
let reply = null;
bridge.start('ru-RU', true, false, (v) => { reply = v; });
const invoke = sent.find(m => m.type === 6);
if (!invoke || invoke.method !== 'start' || invoke.args[0] !== 'ru-RU') throw Error('invokeMethod shape');
transport.onmessage({data: JSON.stringify({type: 10, id: invoke.id, data: 'ok'})});
if (reply !== 'ok') throw Error('response dispatch');
console.log('webchannel lite ok');
'''
        self.run_node(harness.replace('LITE', json.dumps(literal('QWEBCHANNEL_LITE_JS'))))

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


@skip_node
class MicButtonTests(unittest.TestCase):
    """Рабочая кнопка микрофона: реплика штатной кнопки + настоящая диктовка."""

    HARNESS = r'''
const vm = require('vm');

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentElement = null;
    this.parentNode = null;
    this.style = {cssText: '', display: '', visibility: ''};
    this.textContent = '';
    this.value = '';
    this._sized = false;
    this._listeners = {};
    this._html = '';
    this.classList = {
      set: new Set(),
      toggle: (c, on) => { if (on) this.classList.set.add(c); else this.classList.set.delete(c); },
      contains: (c) => this.classList.set.has(c)
    };
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(ev) {
    ev.target = this;
    if (!ev.stopImmediatePropagation) {
      ev._stopped = false;
      ev.defaultPrevented = false;
      ev.stopImmediatePropagation = () => { ev._stopped = true; };
      ev.stopPropagation = () => { ev._stopped = true; };
      ev.preventDefault = () => { ev.defaultPrevented = true; };
    }
    // Capture phase (document) runs before the target listeners, as in a browser.
    for (const fn of (document._listeners[ev.type] || [])) {
      if (ev._stopped) break;
      fn.call(this, ev);
    }
    for (const fn of (this._listeners[ev.type] || [])) {
      if (ev._stopped) break;
      fn(ev);
    }
    return true;
  }
  click() { this.dispatchEvent(new MouseEvent('click', {})); }
  focus() {}
  appendChild(c) { c.parentElement = c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.parentElement = c.parentNode = this;
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentElement = c.parentNode = null;
    return c;
  }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  getClientRects() { return this._sized ? [{width: 400, height: 40}] : []; }
  getBoundingClientRect() { return {x: 200, y: 20, left: 0, top: 0, right: 400, bottom: 40, width: 400, height: 40}; }
  get offsetWidth() { return this._sized ? 400 : 0; }
  get offsetHeight() { return this._sized ? 40 : 0; }
  get offsetParent() { return this._sized ? this.parentElement : null; }
  get isConnected() { return true; }
  matches(sel) { return matchSel(this, sel); }
  closest(sel) {
    let el = this;
    while (el) { if (el.matches && el.matches(sel)) return el; el = el.parentElement; }
    return null;
  }
  querySelector(sel) {
    return walk(this).find((el) => matchSel(el, sel)) || null;
  }
  querySelectorAll(sel) {
    return walk(this).filter((el) => matchSel(el, sel));
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
}

function matchOne(el, s) {
  let rest = String(s).trim();
  const m = rest.match(/^([a-zA-Z0-9_-]+|\*)/);
  if (m) {
    if (m[1] !== '*' && el.tagName !== m[1].toUpperCase()) return false;
    rest = rest.slice(m[1].length);
  }
  while (rest.length) {
    if (rest[0] === '#') {
      const id = rest.match(/^#([A-Za-z0-9_-]+)/);
      if (!id || (el.getAttribute('id') || el.id) !== id[1]) return false;
      rest = rest.slice(id[0].length);
    } else if (rest[0] === '.') {
      const cls = rest.match(/^\.([A-Za-z0-9_-]+)/);
      if (!cls || !el.classList.contains(cls[1])) return false;
      rest = rest.slice(cls[0].length);
    } else if (rest[0] === '[') {
      const a = rest.match(/^\[([A-Za-z0-9_:-]+)(\*?=)"?([^\]"]*)"?\]/);
      if (!a) return false;
      const val = el.getAttribute(a[1]);
      if (val === null) return false;
      if (a[2] === '=' && val !== a[3]) return false;
      if (a[2] === '*=' && val.indexOf(a[3]) < 0) return false;
      rest = rest.slice(a[0].length);
    } else return false;
  }
  return true;
}
function matchSel(el, selector) {
  return String(selector).split(',').some((s) => matchOne(el, s));
}
function walk(node, out) {
  out = out || [];
  for (const c of node.children || []) { out.push(c); walk(c, out); }
  return out;
}

const documentElement = new El('html');
documentElement.lang = 'ru-RU';
const head = new El('head');
documentElement.appendChild(head);
const body = new El('body');
documentElement.appendChild(body);

const document = {
  readyState: 'complete',
  documentElement,
  head,
  body,
  _listeners: {},
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
  dispatchEvent() { return true; },
  createElement: (tag) => new El(tag),
  getElementById(id) {
    return walk(documentElement).find((el) => (el.getAttribute('id') || el.id) === id) || null;
  },
  querySelector(sel) {
    return walk(documentElement).find((el) => matchSel(el, sel)) || null;
  },
  querySelectorAll(sel) {
    return walk(documentElement).filter((el) => matchSel(el, sel));
  }
};

class MockSR {
  constructor() { MockSR.last = this; this.active = false; this.starts = 0; }
  start() { this.active = true; this.starts += 1; if (this.onstart) this.onstart(); }
  stop() { if (!this.active) return; this.active = false; if (this.onend) this.onend(); }
  give(text, isFinal) {
    if (this.onresult) {
      this.onresult({resultIndex: 0, results: [{0: {transcript: text}, isFinal: !!isFinal, length: 1}]});
    }
  }
}

const window = {SpeechRecognition: MockSR, addEventListener() {}};
window.window = window;
class FakeEvent { constructor(type, opts) { this.type = type; Object.assign(this, opts || {}); } }
const sandbox = {
  window, document, navigator: {language: 'ru-RU'},
  MutationObserver: class { observe() {} disconnect() {} },
  Event: FakeEvent,
  InputEvent: FakeEvent,
  MouseEvent: FakeEvent,
  PointerEvent: FakeEvent,
  KeyboardEvent: FakeEvent,
  Date, Math, JSON, Object, Array, String, Number, Boolean, Set, Map, Error, RegExp, isNaN, parseInt, parseFloat,
  setTimeout, clearTimeout, setInterval, clearInterval, console
};
vm.createContext(sandbox);
'''

    def run_node(self, script):
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_replica_injected_when_site_mic_missing_and_drives_dictation(self):
        script = self.HARNESS + '''
vm.runInContext(MIC_SRC, sandbox);
vm.runInContext(START_SRC, sandbox);  // business JS стабильна до конца теста

// Страница БЕЗ штатной кнопки микрофона: панель ввода + textarea + отправка.
const plate = new El('div');
plate.setAttribute('data-xid', 'input-plate');
const row = new El('div');
plate.appendChild(row);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(plate);

// Первичная инъекция уже отработала (readyState=complete) — досоздать кнопку.
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);

const mic = document.getElementById('legalyze-mic');
if (!mic) throw Error('replica not injected');
if (mic.getAttribute('aria-label') !== 'Микрофон') throw Error('bad aria-label');
if (mic.getAttribute('data-xid') !== 'input-plate-voice-button') throw Error('bad data-xid');
if (mic.classList.contains('wdK4Nc')) throw Error('replica must not carry the send-button class');

// Бизнес-код (JS_START_RECORDING) находит реплику и "кликает" её.
mic._sized = true;
const res = vm.runInContext(START_SRC, sandbox);
if (!res || typeof res.x !== 'number') throw Error('business JS cannot find replica: ' + JSON.stringify(res));
if (!MockSR.last || !MockSR.last.active) throw Error('dictation did not start');

// Диктовка пишет текст в поле ввода (interim -> final).
MockSR.last.give('привет', false);
if (ta.value !== 'привет') throw Error('interim text not written: ' + ta.value);
MockSR.last.give('привет мир', true);
if (ta.value !== 'привет мир') throw Error('final text not written: ' + ta.value);

// Повторный клик по кнопке (через бизнес-код) останавливает запись.
// Выдерживаем антидребезг серии кликов (500 мс) синхронно.
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 600);
const res2 = vm.runInContext(START_SRC, sandbox);
if (!res2 || typeof res2.x !== 'number') throw Error('second business click failed');
if (MockSR.last.active) throw Error('dictation must stop on second toggle');

console.log('replica dictation ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_START_RECORDING', 'main.py'))))
        self.run_node(script)

    def test_speech_shim_overrides_native_constructor_and_never_calls_it(self):
        # Нативный SpeechRecognition в QtWebEngine без Google API-ключа РОНЯЕТ
        # рендер при start(). Шим обязан перехватить конструктор всегда.
        script = r'''
const vm = require('vm');
const signals = {};
const bridgeCalls = [];
const bridge = {
  start: (lang) => bridgeCalls.push(['start', lang]),
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
let nativeStarts = 0;
function MockNativeSpeechRecognition() {
  this.start = () => { nativeStarts += 1; throw new Error('renderer crash'); };
}
const sandbox = {
  window: {
    SpeechRecognition: MockNativeSpeechRecognition,
    webkitSpeechRecognition: MockNativeSpeechRecognition,
    __legalyzeSpeechBridge: bridge,
    __legalyzeSpeechBridgeWait: 10
  },
  navigator: {language: 'ru-RU'},
  setTimeout, clearTimeout, Promise, console
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);

const w = sandbox.window;
if (w.__legalyzeNativeSpeechRecognition !== MockNativeSpeechRecognition)
  throw Error('native must be preserved for diagnostics');
if (w.SpeechRecognition === MockNativeSpeechRecognition)
  throw Error('native constructor must be overridden');
if (w.LegalyzeSpeechRecognition !== w.SpeechRecognition)
  throw Error('explicit export must match the override');
if (w.__legalyzeSpeech.mode !== 'shim') throw Error('mode');

const rec = new w.LegalyzeSpeechRecognition();
rec.lang = 'ru-RU';
rec.start();
if (nativeStarts !== 0) throw Error('native start() must never be called');
if (!bridgeCalls.length || bridgeCalls[0][0] !== 'start')
  throw Error('bridge must be used');
console.log('shim override ok');
'''
        self.run_node(script.replace('SHIM', json.dumps(literal('SPEECH_SHIM_JS'))))

    def test_mic_click_hijack_blocks_site_handlers_and_starts_dictation(self):
        script = self.HARNESS + '''
// Штатная кнопка микрофона с СОБСТВЕННЫМИ обработчиками (как jsaction сайта):
// они вызывают getUserMedia и роняют рендер в QtWebEngine без WebRTC.
const row = new El('div');
const siteMic = new El('button');
siteMic.setAttribute('data-xid', 'input-plate-voice-button');
siteMic.setAttribute('aria-label', 'Микрофон');
siteMic._sized = true;
let siteClicks = 0, siteDowns = 0;
siteMic.addEventListener('click', () => siteClicks++);
siteMic.addEventListener('mousedown', () => siteDowns++);
siteMic.addEventListener('pointerdown', () => siteDowns++);
row.appendChild(siteMic);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(row);

vm.runInContext(MIC_SRC, sandbox);
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (document.getElementById('legalyze-mic')) throw Error('replica must not replace native mic');

// Бизнес-код нажимает штатную кнопку (synthetic-клики + el.click()).
const res = vm.runInContext(START_SRC, sandbox);
if (!res || typeof res.x !== 'number') throw Error('business JS cannot find button');
if (siteDowns !== 0) throw Error('site press handlers must be blocked: ' + siteDowns);
if (siteClicks !== 0) throw Error('site click handler must be blocked: ' + siteClicks);
if (!MockSR.last || !MockSR.last.active) throw Error('dictation must start via hijack');

// Серия кликов (synthetic + el.click()) даёт ровно один старт.
if (MockSR.last.starts !== 1) throw Error('exactly one start expected: ' + MockSR.last.starts);

// Повторное нажатие пользователем (вне антидребезга) останавливает запись.
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 600);
siteMic.dispatchEvent({type: 'click'});
if (MockSR.last.active) throw Error('second click must stop dictation');
if (siteClicks !== 0) throw Error('site click handler must stay blocked');

// Кнопка отправки не перехватывается: штатный сценарий отправки работает.
vm.runInContext('window.__legalyzeMic.toggle()', sandbox);
if (!MockSR.last.active) throw Error('dictation restart');
const res2 = vm.runInContext(STOP_SRC, sandbox);
if (!res2 || res2.ok !== true) throw Error('stop_and_send failed');
if (MockSR.last.active) throw Error('send must stop dictation');
console.log('hijack ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_START_RECORDING', 'main.py')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
        self.run_node(script)

    def test_native_site_button_prevents_replica_and_send_stops_dictation(self):
        script = self.HARNESS + '''
// Страница СО штатной кнопкой микрофона.
const row = new El('div');
const nativeMic = new El('button');
nativeMic.setAttribute('data-xid', 'input-plate-voice-button');
nativeMic.setAttribute('aria-label', 'Микрофон');
nativeMic._sized = true;
row.appendChild(nativeMic);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(row);

vm.runInContext(MIC_SRC, sandbox);
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (document.getElementById('legalyze-mic')) throw Error('replica must not replace native mic');

// Собственная диктовка (fallback F3): старт, затем штатная кнопка отправки
// через JS_STOP_AND_SEND останавливает запись перед отправкой.
if (vm.runInContext('window.__legalyzeMic.toggle()', sandbox) !== true) throw Error('dictation start');
if (!MockSR.last.active) throw Error('not recording');
const res = vm.runInContext(STOP_SRC, sandbox);
if (!res || res.ok !== true) throw Error('stop_and_send failed: ' + JSON.stringify(res));
if (MockSR.last.active) throw Error('send must stop dictation');

// Кнопка-реплика возвращается, если штатная исчезает (SPA-редеринг).
nativeMic.remove();
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (!document.getElementById('legalyze-mic')) throw Error('replica must appear after native mic gone');
console.log('native-first ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
        self.run_node(script)
