import ast
import builtins
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import symtable
import tempfile
import threading
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'TestLegalQtWebEngine'
ORIGINAL = ROOT.parent / 'TestLegal'
sys.path.insert(0, str(ROOT))

import native_browser  # noqa: E402
import speech_backend  # noqa: E402
import win32_embed  # noqa: E402
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


class FakeCDPPage:
    """Минимальная страница CDP: метрики + реакция на переопределение вьюпорта.

    Отдаёт то, что отдал бы настоящий Chrome: после
    `Emulation.setDeviceMetricsOverride` layout становится шириной `width`
    CSS px, а `devicePixelRatio` — переданным dsf (при `fitWindow` вид
    дополнительно вписывается в окно, то есть dsf становится `window/width`).
    """

    def __init__(self, metrics=None, window_w=453):
        self.calls = []
        self.window_w = window_w
        self.metrics = metrics or {"w": 680, "h": 1102, "dpr": 2 / 3, "sw": 680}

    def send(self, method, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == 'Emulation.setDeviceMetricsOverride':
            width = int(params.get('width') or 0)
            dsf = float(params.get('deviceScaleFactor') or 1.0)
            self.metrics = {"w": width, "h": int(params.get('height') or 0),
                            "dpr": (self.window_w / width) if params.get('fitWindow') else dsf,
                            "sw": width}
        return {}

    def eval(self, expression, timeout=None):
        import json
        return {'value': json.dumps(self.metrics)}


class ZoomedCDPPage(FakeCDPPage):
    """Страница, у которой поверх эмуляции применён зум профиля (0.667).

    Так Chrome ведёт себя, когда зум из Preferences накладывается на
    `Emulation.setDeviceMetricsOverride`: видимая ширина = width / zoom,
    `devicePixelRatio` = dsf * zoom.
    """

    def __init__(self, zoom=1.0, metrics=None, window_w=453):
        super().__init__(metrics, window_w)
        self.zoom = float(zoom)

    def send(self, method, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == 'Emulation.setDeviceMetricsOverride':
            width = int(params.get('width') or 0)
            dsf = float(params.get('deviceScaleFactor') or 1.0)
            self.metrics = {"w": int(round(width / self.zoom)),
                            "h": int(round(int(params.get('height') or 0) / self.zoom)),
                            "dpr": dsf * self.zoom,
                            "sw": int(round(width / self.zoom))}
        return {}


class FitCDPPage(FakeCDPPage):
    """Страница сайта с минимальной шириной раскладки и полем ввода в потоке.

    `min_width` — во столько CSS px сайт верстает страницу (если окно уже,
    содержимое не влезает и «съезжает вправо»); `input_bottom` — абсолютная
    позиция низа поля ввода (если она ниже видимой части, «окно запроса
    улетело вниз»).
    """

    def __init__(self, min_width=620, input_bottom=0, start=(620, 977),
                 dpr=1.0, window_w=620):
        super().__init__({"w": start[0], "h": start[1], "dpr": dpr,
                          "sw": max(min_width, start[0])}, window_w)
        self.min_width = int(min_width)
        self.input_bottom = int(input_bottom)

    def send(self, method, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == 'Emulation.setDeviceMetricsOverride':
            width = int(params.get('width') or 0)
            dsf = float(params.get('deviceScaleFactor') or 1.0)
            self.metrics = {"w": width, "h": int(params.get('height') or 0),
                            "dpr": dsf, "sw": max(self.min_width, width)}
        return {}

    def eval(self, expression, timeout=None):
        import json
        w = int(self.metrics.get("w") or 0)
        h = int(self.metrics.get("h") or 0)
        if 'contenteditable' in (expression or ''):
            return {'value': json.dumps({"iw": w, "ih": h,
                                         "sw": max(self.min_width, w),
                                         "ib": self.input_bottom})}
        return {'value': json.dumps(self.metrics)}


class FakeUser32:
    """user32, у которого SetParent не срабатывает (худший случай)."""

    def __init__(self, parent_ok=False):
        self.style0 = 0x16CF0000
        self.exstyle0 = 0x00040100
        self.styles = []
        self.shown = False
        self.parent_ok = parent_ok

    def GetWindowLongPtrW(self, hwnd, index):
        return self.exstyle0 if index == win32_embed.GWL_EXSTYLE else self.style0

    def SetWindowLongPtrW(self, hwnd, index, value):
        raw = getattr(value, 'value', value)
        if isinstance(raw, bytes):
            raw = int.from_bytes(raw[:8], 'little')
        self.styles.append((hwnd, index, int(raw) & 0xFFFFFFFFFFFFFFFF))
        return 1

    def SetParent(self, hwnd, parent):
        return 0

    def GetParent(self, hwnd):
        return 1 if self.parent_ok else 0

    def ShowWindow(self, hwnd, cmd):
        self.shown = True
        return True

    def IsWindow(self, hwnd):
        return True

    def IsWindowVisible(self, hwnd):
        return True

    def GetDpiForWindow(self, hwnd):
        return 96

    def GetClientRect(self, hwnd, rect):
        return self._fill(rect)

    def GetWindowRect(self, hwnd, rect):
        return self._fill(rect)

    def _fill(self, rect):
        # win32_embed передаёт ctypes.byref(rect).
        target = getattr(rect, '_obj', rect)
        target.left = target.top = 0
        target.right, target.bottom = 453, 735
        return True

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        return True

    def GetWindowThreadProcessId(self, hwnd, pid):
        return 1


NODE = shutil.which('node')
skip_node = unittest.skipUnless(NODE, 'Node unavailable')


class SourceTests(unittest.TestCase):
    def setUp(self):
        # Подгонка запоминает ширину раскладки — между тестами её быть не должно.
        native_browser._FIT.update({"key": None, "css_w": None})

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

    def test_page_microphone_is_native_not_a_bridge(self):
        # Штатная запись голоса страницы живёт в настоящем браузере: Web Speech
        # API / getUserMedia привязаны к облаку вендора и отсутствуют в
        # QtWebEngine и в vanilla Chromium. Поэтому браузер — отдельный процесс.
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('class NativeHost(', main_src)
        self.assertIn('from native_browser import', main_src)
        self.assertIn('import win32_embed', main_src)
        # Никаких принудительных выходов и перехвата клавиатуры.
        self.assertNotIn('os._exit(', main_src)
        self.assertNotIn('SetWindowsHookEx', main_src)
        self.assertIn('HotkeyMonitor(', main_src)

    def test_native_mode_never_overrides_the_pages_speech_api(self):
        # В настоящем Chrome подменять SpeechRecognition НЕЛЬЗЯ и не нужно:
        # именно им сайт и записывает голос. Шим остаётся только для резерва.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'install_compat')
        body = ast.unparse(func)
        self.assertIn('if native:', body)
        self.assertIn('CONSENT_CLICK_JS', body)
        self.assertNotIn('SPEECH_SHIM_JS', body)
        self.assertNotIn('MIC_BUTTON_JS', body)
        # резервный путь со встроенным движком сохранён
        self.assertIn('_start_embedded_fallback', main_src)

    def test_mic_press_runs_the_site_recorder_and_verifies_it(self):
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        start = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_native_mic_start')
        verify = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_native_mic_verify')
        self.assertIn('JS_START_RECORDING', ast.unparse(start))
        self.assertIn('JS_CHECK_RECORDING', ast.unparse(verify))
        # Не проверялось раньше -> «нажимаю на микрофон и сразу отправка».
        self.assertIn('recording', ast.unparse(verify))
        toggle = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        self.assertIn('self._native_mic_start()', ast.unparse(toggle))

    def test_browser_recovery_is_a_ladder(self):
        # «Браузер висит»: сначала свежий профиль (single-ton владеет старым),
        # затем без GPU (Parsec/гибридная графика), затем следующий браузер.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        retry = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_retry')
        body = ast.unparse(retry)
        self.assertIn('self.attempt = 1', body)
        self.assertIn('self.gpu_off = True', body)
        self.assertIn('self.index += 1', body)
        self.assertIn('MAX_ATTEMPTS', ast.unparse(tree))

    def test_onefile_build_command_is_documented_and_matches_the_bat(self):
        # Один исполняемый файл: --onefile и НИКАКИХ других вариантов сборки
        # в релизной команде. Синтаксис проверяется и в README, и в .bat.
        build = (ROOT / 'BUILD_ONEFILE.txt').read_text()
        commands = [line for line in build.splitlines()
                    if line.startswith('.venv') and '-m nuitka' in line]
        onefile = [c for c in commands if '--onefile' in c]
        self.assertEqual(len(onefile), 1)
        command = onefile[0]
        for flag in ('--onefile', '--windows-console-mode=disable', '--enable-plugin=pyqt6',
                     '--include-package=requests', '--include-package=websocket',
                     '--include-package=cryptography', '--include-package=fpdf',
                     '--include-data-file=data/DejaVuSans.ttf=data/DejaVuSans.ttf',
                     '--windows-icon-from-ico=icon.ico', '--assume-yes-for-downloads',
                     '--output-dir=build', '--output-filename=Legalyze.exe'):
            self.assertIn(flag, command)
        self.assertTrue(command.rstrip().endswith('main.py'))
        self.assertNotIn('--standalone', command)
        # .bat содержит ту же команду (иначе документация врёт)
        bat = (ROOT / 'build_onefile.bat').read_text()
        self.assertIn(command.split('-m nuitka', 1)[1].strip().split(' main.py')[0], bat)
        # Эталонный синтаксис из release_reference совпадает по флагам
        reference = (ROOT.parent / 'release_reference' / 'BUILD_COMMAND.txt').read_text().strip()
        self.assertEqual(sorted(command.split()[3:]), sorted(reference.split()[3:]))

    def test_zoom_level_matches_the_working_release_build(self):
        # Chrome хранит зум как «уровень»: factor = 1.2 ** level.
        self.assertAlmostEqual(native_browser.ZOOM, 2.0 / 3.0, delta=1e-9)
        self.assertAlmostEqual(native_browser.ZOOM_LEVEL, -2.223901614059533, delta=1e-5)
        self.assertAlmostEqual(1.2 ** native_browser.ZOOM_LEVEL, 2.0 / 3.0, delta=1e-9)
        # Раскладка одинаковая при 100 %, 125 % и 150 %: окно 453 DIP / 0.667
        self.assertEqual(native_browser.css_size_for(453, 735), (680, 1102))

    def test_zoom_is_written_into_the_profile_before_the_launch(self):
        tmp = Path(tempfile.mkdtemp(prefix='legalyze-zoom-'))
        try:
            self.assertTrue(native_browser.write_zoom(tmp))
            prefs = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            partition = prefs['partition']
            self.assertAlmostEqual(partition['default_zoom_level']['x'],
                                   -2.223901614059533, delta=1e-4)
            host = partition['per_host_zoom_levels']['x']['google.com']
            self.assertAlmostEqual(host['zoom_level'], -2.223901614059533, delta=1e-4)
            # Повторный запуск не затирает то, что уже есть в профиле.
            (tmp / 'Default' / 'Preferences').write_text(
                json.dumps({'partition': partition, 'keepme': 1}), encoding='utf-8')
            self.assertTrue(native_browser.write_zoom(tmp))
            again = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            self.assertEqual(again['keepme'], 1)
            self.assertIn('default_zoom_level', again['partition'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_native_zoom_is_measured_and_left_alone(self):
        # Профиль уже дал 67 %: 680 CSS px при dpr 0.667 -> 453 физических px.
        page = FakeCDPPage({"w": 680, "h": 1102, "dpr": 2 / 3, "sw": 680})
        result = native_browser.apply_zoom(page, 453, 735)
        self.assertEqual(result['source'], 'native')
        self.assertEqual(page.calls, [])      # ничего не переопределяем

    def test_a_wide_unscaled_layout_is_detected(self):
        # РОВНО дефект v8: CSS-вьюпорт 680 px, но отрисовка не сжата
        # (dpr = 1) -> чат шире окна, текст за правой границей.
        page = FakeCDPPage({"w": 680, "h": 1102, "dpr": 1.0, "sw": 680})
        ok, metrics = native_browser.zoom_ok(page, 453, 735, 453, 735)
        self.assertFalse(ok)
        self.assertEqual(metrics['innerWidth'], 680)
        # Прежняя проверка (только по innerWidth) здесь ошибочно проходила:
        self.assertTrue(native_browser.verify_viewport(page, 453, 735))

    def test_zoom_is_forced_and_measured_when_prefs_are_ignored(self):
        page = FakeCDPPage({"w": 453, "h": 735, "dpr": 1.0, "sw": 453})
        result = native_browser.apply_zoom(page, 453, 735, settle=0)
        methods = [method for method, _params in page.calls]
        self.assertIn('Emulation.setDeviceMetricsOverride', methods)
        params = page.calls[-1][1]
        self.assertEqual(params['width'], 680)
        # Высота следует за клиентской областью окна: 735 / (453/680) = 1103.
        self.assertEqual(params['height'], 1103)
        # dsf = physical / css = 0.667: эмулируемое устройство == окно,
        # поэтому страница сжимается, а не обрезается справа.
        self.assertAlmostEqual(params['deviceScaleFactor'], 2 / 3, delta=0.01)
        # Результат ИЗМЕРЕН: первая же попытка дала нужные метрики.
        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'emulation-dsf')
        self.assertEqual(len(page.calls), 1)

    def test_zoom_works_the_same_at_125_percent(self):
        # 125 %: окно 453 DIP = 566 физических px, раскладка всё те же 680 CSS px.
        page = FakeCDPPage({"w": 566, "h": 919, "dpr": 1.25, "sw": 566}, window_w=566)
        result = native_browser.apply_zoom(page, 453, 735, 566, 919, settle=0)
        self.assertTrue(result['ok'])
        params = page.calls[-1][1]
        self.assertEqual(params['width'], 680)
        # 566 / 680 = 0.833 = dsf(1.25) * zoom(0.667)
        self.assertAlmostEqual(params['deviceScaleFactor'], 0.8333, delta=0.01)

    def test_zoom_guard_is_wired_into_the_native_path(self):
        source = (ROOT / 'main.py').read_text()
        tree = ast.parse(source)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        self.assertIn('_ensure_zoom', methods)
        self.assertIn('_placeholder_physical_size', methods)
        self.assertIn('_placeholder_logical_size', methods)
        guard = ast.unparse(methods['_ensure_zoom'])
        self.assertIn('zoom_ok(page', guard)
        self.assertIn('apply_zoom(page', guard)
        self.assertIn('daemon=True', guard)          # GUI не блокируется
        self.assertIn('zoom_timer', ast.unparse(methods['_on_browser_ready']))
        self.assertIn('zoom_timer', ast.unparse(methods['_cleanup']))
        # Резервный QtWebEngine не трогаем: зум нужен только нативному режиму.
        self.assertIn('not self.native_mode', guard)
        # Зум пишется в профиль до запуска браузера.
        self.assertIn('write_zoom(self.profile_dir)',
                      (ROOT / 'native_browser.py').read_text())

    def test_consent_walls_are_never_automated(self):
        # В ЕС (Испания) свежий профиль сначала отдаёт consent.google.com.
        # Прежний выбор цели брал «любую страницу google.com» и встаивался
        # в стену согласия — отсюда «браузер запускается и висит».
        targets = [
            {'type': 'page', 'url': 'https://consent.google.com/m?gl=ES',
             'webSocketDebuggerUrl': 'ws://1'},
            {'type': 'page', 'url': 'https://gemini.google.com/app',
             'webSocketDebuggerUrl': 'ws://2'},
        ]
        chosen = native_browser.choose_target(targets)
        self.assertEqual(chosen['url'], 'https://gemini.google.com/app')
        self.assertTrue(native_browser.is_blocked_target('https://consent.google.com/m'))
        self.assertTrue(native_browser.is_blocked_target('https://accounts.google.com/v3/signin'))
        self.assertFalse(native_browser.is_blocked_target('https://gemini.google.com/app'))
        # Стена — только крайний случай (чтобы кликер мог её закрыть).
        only_wall = [{'type': 'page', 'url': 'https://consent.google.com/m',
                      'webSocketDebuggerUrl': 'ws://1'}]
        self.assertEqual(native_browser.choose_target(only_wall)['url'],
                         'https://consent.google.com/m')
        # about:blank не выбирается: вызывающий продолжает опрос.
        self.assertIsNone(native_browser.choose_target(
            [{'type': 'page', 'url': 'about:blank', 'webSocketDebuggerUrl': 'ws://3'}]))

    def test_consent_clicker_covers_the_languages_google_ships(self):
        script = literal('CONSENT_CLICK_JS')
        for phrase in ('accept all', 'Принять все', 'Aceptar todo', 'Alle akzeptieren',
                       'Tout accepter', 'Accetta tutto', 'Aceitar tudo', 'Tümünü kabul',
                       'Zaakceptuj wszystko', 'Прийняти всі'):
            self.assertIn(phrase.lower(), script.lower())
        self.assertIn('L2AGLb', script)          # стабильный id кнопки Google
        self.assertIn('reject', script.lower())  # и никогда не «отклонить»
        self.assertIn('__legalyzeAcceptConsent', script)

    def test_browser_discovery_prefers_pinned_portable_then_chrome(self):
        seen = []

        def fake_exists(path):
            seen.append(str(path))
            return str(path).endswith(('chrome.exe', 'msedge.exe'))

        with patch.object(native_browser, '_exists', fake_exists):
            candidates = native_browser.discover_browsers(app_dir=Path('C:/app'))
        kinds = [(c.rank, c.kind) for c in candidates]
        # переносной/pinned браузер важнее установленного Chrome, Chrome важнее Edge
        self.assertEqual(kinds[0], (1, 'portable'))
        self.assertIn((2, 'chrome'), kinds)
        self.assertIn((3, 'edge'), kinds)
        self.assertTrue(any('browser' in p for p in seen))

    def test_launch_flags_prepare_the_microphone_and_the_window(self):
        args = native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                         'https://google.com/ai', 453, 735)
        joined = ' '.join(args)
        self.assertIn('--app=https://google.com/ai', joined)
        self.assertIn('--user-data-dir=C:/profile', joined)
        self.assertIn('--remote-debugging-port=9222', joined)
        self.assertIn('--window-size=453,735', joined)
        # Жёлтая полоса Chrome: этих флагов больше нет.
        self.assertNotIn('--use-fake-ui-for-media-stream', joined)
        self.assertNotIn('--use-fake-device-for-media-stream', joined)
        self.assertIn('--no-first-run', joined)
        self.assertNotIn('--disable-gpu', joined)
        off = ' '.join(native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                                 'https://google.com/ai', 453, 735,
                                                 gpu=False))
        self.assertIn('--disable-gpu', off)

    def _fetch_chrome(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('fetch_chrome', ROOT / 'tools' / 'fetch_chrome.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fetch_chrome_picks_the_branded_win64_build(self):
        fetch = self._fetch_chrome()
        index = {'channels': {'Stable': {'version': '140.0.7339.207', 'downloads': {'chrome': [
            {'platform': 'linux64', 'url': 'https://x/linux.zip'},
            {'platform': 'win64', 'url': 'https://x/chrome-win64.zip'},
        ]}}}}
        version, url = fetch.pick_url(index)
        self.assertEqual(version, '140.0.7339.207')
        self.assertTrue(url.endswith('chrome-win64.zip'))
        with self.assertRaises(SystemExit):
            fetch.pick_url(index, platform='win32')

    def test_fetch_chrome_fails_friendly_without_the_network(self):
        fetch = self._fetch_chrome()

        class DeadRequest:
            ProxyHandler = staticmethod(lambda *a, **k: None)

            def build_opener(self, *a, **k):
                raise OSError('TLS/SSL connection has been closed')

        with patch.object(fetch, 'urllib', SimpleNamespace(request=DeadRequest())):
            with self.assertRaises(fetch.FetchError) as caught:
                fetch.main(['--dry-run'])
        message = str(caught.exception)
        self.assertIn('--url', message)      # подсказка, как обойти проблему
        self.assertNotIn('Traceback', message)

    def test_fetch_chrome_dry_run_does_not_download(self):
        fetch = self._fetch_chrome()
        seen = []
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with patch.object(fetch, 'download', lambda *a, **k: seen.append(a)):
                code = fetch.main(['--dry-run', '--url', 'https://x/chrome-win64.zip',
                                   '--dest', str(ROOT / 'browser')])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        self.assertIn('https://x/chrome-win64.zip', buffer.getvalue())

    def test_retry_gets_a_fresh_profile_directory(self):
        # Профиль Chromium блокируется SingletonLock. Если повторный запуск идёт
        # в тот же каталог, новый процесс передаёт URL «мёртвому» владельцу и
        # сразу завершается — это и был «зависший chrome».
        browser = native_browser.NativeBrowser(profile_dir=Path('C:/profile'))
        self.assertEqual(browser.profile_dir, Path('C:/profile'))
        self.assertEqual(browser.profile_for(1), Path('C:/profile-retry1'))
        retry = native_browser.NativeBrowser(profile_dir=Path('C:/profile'), attempt=2)
        self.assertEqual(retry.profile_dir, Path('C:/profile-retry2'))
        # NativeHost обязан передавать номер попытки, иначе повтор бесполезен.
        self.assertIn('attempt=self.attempt', (ROOT / 'main.py').read_text())

    def test_every_module_level_name_is_bound(self):
        # Регрессия: `class NativeHost(QObject)` падала с NameError на импорте,
        # потому что QObject не был импортирован. Тесты на Linux не могут
        # импортировать main.py (нет PyQt6), поэтому проверяем таблицу символов:
        # любое имя, используемое на уровне модуля, должно быть определено.
        modules = ['main.py', 'native_browser.py', 'win32_embed.py', 'browser_focus.py',
                   'web_compat.py', 'qt_browser.py', 'speech_backend.py', 'diagnostics.py',
                   'config.py', 'storage_paths.py', 'updater.py', 'crypto_utils.py',
                   'hwid_gen.py', 'secure_store.py', 'win32_hotkeys.py', 'smoke_browser.py']
        extra_globals = {'__file__', '__name__', '__doc__', '__package__', '__spec__',
                         '__loader__', '__builtins__', '__debug__', '__path__'}
        problems = []
        for name in modules:
            source = (ROOT / name).read_text()
            table = symtable.symtable(source, name, 'exec')

            def bound(sym):
                return (sym.is_assigned() or sym.is_imported() or sym.is_namespace()
                        or sym.is_parameter())

            # только имена, которые модуль действительно связывает
            # (импорты, def/class, присваивания) — referenced-only имён здесь нет
            module_bound = ({sym.get_name() for sym in table.get_symbols() if bound(sym)}
                            | extra_globals | set(dir(builtins)))

            def walk(scope):
                for sym in scope.get_symbols():
                    # только имена, разрешаемые на уровне модуля: локальные,
                    # параметры и замыкания связаны внутри своей области видимости
                    if sym.is_global() and not bound(sym) and sym.get_name() not in module_bound:
                        problems.append('%s: %s' % (name, sym.get_name()))
                for child in scope.get_children():
                    walk(child)

            for sym in table.get_symbols():
                if not bound(sym) and sym.get_name() not in module_bound:
                    problems.append('%s: %s' % (name, sym.get_name()))
            walk(table)
        self.assertEqual(problems, [])

    def test_embed_is_checked_and_rolled_back(self):
        user32 = FakeUser32(parent_ok=False)
        self.assertFalse(win32_embed.embed(user32, 11, 22))
        # Стиль восстановлен, окно снова показано: пользователь не остаётся
        # с невидимым или оторванным окном браузера.
        restored = {}
        for _hwnd, index, value in user32.styles:
            restored[index] = value
        self.assertEqual(restored[win32_embed.GWL_STYLE], user32.style0)
        self.assertEqual(restored[win32_embed.GWL_EXSTYLE], user32.exstyle0)
        self.assertTrue(user32.shown)

    def test_upload_never_falls_back_to_body(self):
        source = (ROOT / 'main.py').read_text()
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
        self.assertIn('JS_MIC_START', source)       # настоящая диктовка — первая
        self.assertIn('JS_START_RECORDING', source)  # штатный сценарий — запасной
        self.assertIn('mic.dictation_started', source)
        self.assertIn('_chat_ready', source)

    def test_mic_press_starts_and_never_sends(self):
        # Реальный лог 20260930: первое нажатие F3/кнопки мгновенно отправляло
        # запрос, потому что состояние страницы рассинхронизировалось (движок
        # диктовки не прислал событий) и toggle() трактовался как «стоп».
        tree = ast.parse((ROOT / 'main.py').read_text())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        source = ast.unparse(func)
        self.assertIn('JS_MIC_START', source)   # явный старт записи
        self.assertIn('JS_MIC_STOP', source)    # стоп — только второе нажатие
        start_js = literal('JS_MIC_START')
        self.assertIn('m.start()', start_js)
        self.assertIn('m.stop()', start_js)     # залипшая сессия сбрасывается

    def test_upload_thread_survives_page_navigation(self):
        # CDP-соединение умирает при навигации: поток должен брать актуальный
        # page, иначе файлы не появятся до ручного Ctrl+R (лог 20260930).
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'UploadThread')
        self.assertIn('page_provider', ast.unparse(cls))
        self.assertIn('_refresh_page', ast.unparse(cls))
        self.assertIn('upload.page_refreshed', main_src)
        thread = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_start_upload_thread')
        body = ast.unparse(thread)
        self.assertIn('self.upload_thread.page = self.worker.page', body)
        self.assertIn('page_provider=self._current_page', body)

    def test_dictation_status_is_visible_to_user(self):
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('_hook_speech_feedback', main_src)
        self.assertIn('Слушаю… говорите', main_src)
        self.assertIn('Распознано: ', main_src)

    def test_recognizer_is_discovered_before_the_engine_is_built(self):
        # Реальный лог v6: speech.host_exit code=2 через 310 мс — движок
        # создавался «вслепую» для ru-RU, а System.Speech бросает
        # "No recognizer of the required ID found", если такой распознаватель
        # не зарегистрирован в Windows. Сначала спрашиваем, что есть.
        script = speech_backend.PS_SCRIPT
        self.assertIn('InstalledRecognizers()', script)
        self.assertLess(
            script.index('InstalledRecognizers()'),
            script.index('New-Object System.Speech.Recognition.SpeechRecognitionEngine($ri)'))
        # Подбор: точная культура -> тот же язык -> первый доступный.
        self.assertIn('$fallback = $true', script)
        # Распознавателей нет вообще -> понятная причина, а не «no-speech».
        self.assertIn('No Windows speech recognizer installed', script)

    def test_host_failures_carry_a_real_message(self):
        script = speech_backend.PS_SCRIPT
        # Текст .NET-исключения уходит в поле msg -> diag speech.error.
        self.assertIn('function Reason(', script)
        self.assertIn('msg = $msg', script)
        import re
        exits = re.findall(r'exit (\d+)', script)
        self.assertTrue(exits)
        # Единственный «тихий» выход — сторож смерти родителя; каждый
        # выход с кодом ошибки обязан сообщить причину (было: exit 2, stderr
        # пуст, причина потеряна).
        self.assertEqual(exits.count('0'), 1)
        self.assertGreaterEqual(script.count('EmitError '),
                                sum(1 for code in exits if code != '0'))

    def test_recognition_loop_does_not_depend_on_the_event_pump(self):
        # .NET-обработчики (add_SpeechRecognized) в -NonInteractive хосте
        # PowerShell не вызываются: v5/v6 не выдавали НИ ОДНОГО события даже
        # при живом процессе. Recognize() — синхронный вызов: насос не нужен.
        script = speech_backend.PS_SCRIPT
        self.assertIn('$res = $rc.Recognize()', script)
        self.assertNotIn('RecognizeAsync', script)
        self.assertNotIn('add_SpeechRecognized', script)
        # Явные таймауты: фраза не обрезается и пауза не рвёт сессию.
        self.assertIn('EndSilenceTimeout', script)
        self.assertIn('InitialSilenceTimeout', script)
        # Аварийный выход приложения не оставляет процесс, держащий микрофон.
        self.assertIn('ParentPid', script)
        self.assertIn('Get-Process -Id $ParentPid', script)

    def test_assembly_loading_has_a_fallback_chain(self):
        script = speech_backend.PS_SCRIPT
        self.assertIn('Add-Type -AssemblyName System.Speech', script)
        self.assertIn("LoadWithPartialName('System.Speech')", script)
        self.assertIn('GetRuntimeDirectory()', script)
        self.assertIn('System.Speech is not available', script)

    def test_mic_never_sends_an_empty_prompt(self):
        # Реальный дефект v6: «нажимаю на микрофон — сразу отправка без
        # записи». Отправка разрешена только если диктовка дала текст.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        body = ast.unparse(func)
        self.assertIn('if not self._mic_got_text', body)
        self.assertIn('self._mic_cancel(', body)
        self.assertIn('self._mic_got_text = False', body)
        self.assertIn('def _mic_cancel', main_src)
        self.assertIn('mic.cancelled_no_text', main_src)

    def test_failed_dictation_resets_the_mic_state(self):
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('def _mic_reset_state', main_src)
        self.assertIn('def _on_speech_error', main_src)
        self.assertIn('MIC_ERROR_MESSAGES', main_src)
        # Причина отказа видна пользователю, а не только в логе.
        for text in ('В Windows не установлен распознаватель речи',
                     'Нет распознавателя для этого языка',
                     'Микрофон не найден'):
            self.assertIn(text, main_src)
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_hook_speech_feedback')
        body = ast.unparse(func)
        self.assertIn('bridge.ended.connect', body)
        self.assertIn('bridge.hypothesis.connect', body)

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

    def test_build_scripts_keep_reference_onefile_syntax(self):
        # Релиз собирается onefile-командой из release_reference/BUILD_COMMAND.txt.
        bat = (ROOT / 'build_nuitka.bat').read_text()
        for token in ('--onefile', '--windows-console-mode=disable',
                      '--enable-plugin=pyqt6', '--include-package=requests',
                      '--include-package=websocket', '--include-package=cryptography',
                      '--include-package=fpdf', '--assume-yes-for-downloads',
                      '--output-filename=Legalyze.exe', 'main.py'):
            self.assertIn(token, bat)
        self.assertIn('data/DejaVuSans.ttf=data/DejaVuSans.ttf', bat)
        self.assertIn('data/FONT-LICENSE.txt=data/FONT-LICENSE.txt', bat)
        self.assertIn('--file-version=1.0.5.0', bat)
        self.assertTrue((ROOT / 'build_nuitka_standalone.bat').exists())

    # ---------------------------------------------------------------- v13 --
    def test_no_unsupported_media_flag_is_emitted(self):
        # Chrome показывает «вы используете неподдерживаемый флаг командной
        # строки» для --use-fake-ui-for-media-stream: именно это и было в отчёте.
        args = native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                         'https://google.com/ai', 453, 735)
        joined = ' '.join(args)
        self.assertNotIn('--use-fake-ui-for-media-stream', joined)
        self.assertNotIn('--use-fake-device-for-media-stream', joined)
        # Всё остальное из v9 осталось на месте: меняем только этот флаг.
        for kept in ('--remote-debugging-port=9222', '--user-data-dir=C:/profile',
                     '--app=https://google.com/ai', '--window-size=453,735',
                     '--no-first-run', '--noerrdialogs', '--disable-extensions'):
            self.assertIn(kept, joined)

    def test_microphone_is_granted_by_the_profile_not_by_a_flag(self):
        tmp = Path(tempfile.mkdtemp(prefix='legalyze-perm-'))
        try:
            self.assertTrue(native_browser.write_permissions(tmp))
            prefs = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            mic = prefs['profile']['content_settings']['exceptions']['media_stream_mic']
            entry = mic['https://google.com:443,*']
            self.assertEqual(entry['setting'], 1)          # 1 = ALLOW
            self.assertEqual(entry['secondary_pattern'], '*')
            self.assertTrue(str(entry['last_modified']).isdigit())
            self.assertIn('https://[*.]google.com:443,*', mic)
            # Повторная запись не затирает профиль.
            (tmp / 'Default' / 'Preferences').write_text(json.dumps({'keepme': 1}),
                                                         encoding='utf-8')
            self.assertTrue(native_browser.write_permissions(tmp))
            again = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            self.assertEqual(again['keepme'], 1)
            self.assertIn('media_stream_mic',
                          again['profile']['content_settings']['exceptions'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        source = ast.parse((ROOT / 'native_browser.py').read_text())
        host = next(n for n in source.body if isinstance(n, ast.ClassDef)
                    and n.name == 'NativeBrowser')
        launch = next(m for m in host.body if isinstance(m, ast.FunctionDef)
                      and m.name == 'launch')
        self.assertIn('write_permissions(self.profile_dir)', ast.unparse(launch))
        # Состояние права проверяется, а не предполагается.
        grant = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'grant_microphone')
        self.assertIn('permissions.query', ast.unparse(grant))

    def test_layout_fills_the_window_exactly(self):
        # Поле ввода и поле ответа стоят на своих местах, когда поверхность
        # ровно равна клиентской области окна.
        css_w, css_h, dsf = native_browser.target_css(453, 735, 680, 1103)
        self.assertEqual(css_w, 680)                       # 453 / 0.667
        self.assertAlmostEqual(css_w * dsf, 680, delta=1.0)    # ровно в ширину
        self.assertAlmostEqual(css_h * dsf, 1103, delta=1.0)   # ровно в высоту
        # 100 %: окно 453x735 физических px — раскладка та же 680 CSS px.
        css_w2, _css_h2, dsf2 = native_browser.target_css(453, 735, 453, 735)
        self.assertEqual(css_w2, 680)
        self.assertAlmostEqual(dsf2, 2.0 / 3.0, delta=1e-3)
        # Рамка/заголовок окна: низ страницы (поле ввода) не срезан.
        css_w3, css_h3, dsf3 = native_browser.target_css(453, 735, 680, 1058)
        self.assertAlmostEqual(css_h3 * dsf3, 1058, delta=1.0)
        self.assertLess(css_h3, css_h)
        # 125 % и 150 % дают ту же ширину раскладки.
        for phys in (566, 680):
            self.assertEqual(native_browser.target_css(453, 735, phys, 900)[0], 680)

    def test_profile_zoom_and_emulation_are_never_stacked(self):
        # Худший случай: зум профиля наложился на эмуляцию (видимая ширина
        # 1020 вместо 680). Ступень уточняется по измерению.
        page = ZoomedCDPPage(zoom=2.0 / 3.0, metrics={'w': 1020, 'h': 1653,
                                                      'dpr': 0.667, 'sw': 1020},
                             window_w=680)
        result = native_browser.apply_zoom(page, 453, 735, 680, 1103, settle=0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'emulation-adaptive')
        self.assertEqual(int(result['metrics']['innerWidth']), 680)
        self.assertTrue(all(method == 'Emulation.setDeviceMetricsOverride'
                            for method, _params in page.calls))

    def test_placeholder_size_is_the_whole_window_not_the_inner_slot(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        method = methods['_placeholder_logical_size']
        node = ast.parse(ast.unparse(method))
        node.body[0].body = node.body[0].body[1:]      # без docstring
        body = ast.unparse(node)
        self.assertIn('placeholder.width()', body)
        self.assertIn('placeholder.height()', body)
        self.assertNotIn('side_inset', body)
        self.assertNotIn('top_inset', body)
        self.assertNotIn('bottom_inset', body)
        # Физический размер берётся у клиентской области окна браузера.
        self.assertIn('_browser_physical_size', methods)
        self.assertIn('_browser_physical_size()', ast.unparse(methods['_zoom_size']))

    def test_zoom_ladder_never_restarts_the_browser_or_presses_keys(self):
        # Именно перезапуск браузера ради --force-device-scale-factor ломал
        # сборки: рвал CDP-сессию, поднимал шторку — кнопки переставали
        # нажиматься, файлы не грузились.
        import re
        source = ast.parse((ROOT / 'native_browser.py').read_text())
        zoom = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'apply_zoom')
        node = ast.parse(ast.unparse(zoom))
        node.body[0].body = node.body[0].body[1:]      # без docstring
        body = ast.unparse(node)
        for forbidden in ('press', 'restart', 'keybd', 'force-device-scale-factor',
                          'hooks', 'ctrl_key'):
            self.assertNotIn(forbidden, body)
        self.assertIn('adapt_viewport(page', body)
        module = re.sub(r'""".*?"""', '', (ROOT / 'native_browser.py').read_text(),
                        flags=re.S)
        self.assertNotIn('--force-device-scale-factor', module)
        self.assertNotIn('--force-device-scale-factor',
                         ' '.join(native_browser.build_args(
                             'C:/chrome.exe', 9222, 'C:/profile',
                             'https://google.com/ai', 453, 735)))
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        for forbidden in ('_restart_browser_dsf', '_press_zoom_key'):
            self.assertNotIn(forbidden, methods)
        guard = ast.unparse(methods['_ensure_zoom'])
        self.assertNotIn('press', guard)
        self.assertNotIn('restart', guard)
        self.assertIn('apply_zoom(page', guard)

    # ---------------------------------------------------------------- v14 --
    def test_browser_lives_inside_the_frame_not_under_it(self):
        # Панель сверху (54 px) срезала верх страницы, нижняя (30 px) — поле
        # ввода: плейсхолдер занимал всё окно. Теперь он ровно в видимом слоте.
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        body = ast.unparse(methods['__init__'])
        self.assertIn('self.slot_w = int(W - 2 * self.side_inset)', body)
        self.assertIn('self.slot_h = int(H - self.top_inset - self.bottom_inset)', body)
        self.assertIn('self.browser_placeholder.setGeometry(self.side_inset, '
                      'self.top_inset, self.slot_w, self.slot_h)', body)
        self.assertIn('self.browser_cover.setGeometry(0, 0, self.slot_w, self.slot_h)', body)
        self.assertNotIn('self.browser_placeholder.setGeometry(0, 0, W, H)', body)
        # Окно браузера создаётся сразу размером со слот — без «дотягивания».
        start = next(m for m in methods.values() if m.name == '_start_chrome')
        self.assertIn('width=slot_w, height=slot_h', ast.unparse(start))

    def test_layout_is_refined_by_measurement_when_the_page_overflows(self):
        # Сайт верстает 700 CSS px, окно даёт 620: содержимое не влезает и
        # «съезжает вправо». Подгонка расширяет раскладку до полного влезания.
        page = FitCDPPage(min_width=700, input_bottom=900)
        ok, css_w, metrics = native_browser.refine_fit(page, 413, 651, 620, 977,
                                                       settle=0)
        self.assertTrue(ok)
        self.assertGreaterEqual(css_w, 700)          # страница влезла целиком
        self.assertEqual(int(metrics['scrollWidth']), int(metrics['innerWidth']))
        # Поверхность осталась равной окну: ничего не обрезано и не растянуто.
        dsf = 620.0 / css_w
        self.assertAlmostEqual(css_w * dsf, 620, delta=1.0)
        # Запомненная ширина: сторож зума не возвращает раскладку назад.
        self.assertEqual(native_browser.target_css(413, 651, 620, 977)[0], css_w)

    def test_layout_is_raised_when_the_input_field_is_below_the_fold(self):
        # «Окно для запросов ИИ улетело вниз»: низ поля ввода (1200) ниже
        # видимой части (977). Раскладка растёт — поле поднимается в кадр.
        page = FitCDPPage(min_width=620, input_bottom=1200)
        ok, css_w, metrics = native_browser.refine_fit(page, 413, 651, 620, 977,
                                                       settle=0)
        self.assertTrue(ok)
        self.assertGreater(css_w, 620)
        self.assertLessEqual(int(metrics['inputBottom']), int(metrics['innerHeight']))

    def test_nothing_changes_when_the_page_already_fits(self):
        page = FitCDPPage(min_width=600, input_bottom=500)
        calls_before = len(page.calls)
        ok, css_w, _metrics = native_browser.refine_fit(page, 413, 651, 620, 977,
                                                        settle=0)
        self.assertTrue(ok)
        self.assertEqual(css_w, 620)
        self.assertEqual(len(page.calls), calls_before)   # лишний раз не трогаем

    def test_refinement_never_grows_without_a_limit(self):
        # Сайт «бесконечной» ширины: подгонка останавливается на пределе.
        page = FitCDPPage(min_width=100000, input_bottom=0)
        ok, css_w, _metrics = native_browser.refine_fit(page, 413, 651, 620, 977,
                                                        settle=0, limit=1.3)
        self.assertFalse(ok)
        self.assertLessEqual(css_w, int(620 * 1.3) + 1)

    def test_tray_icon_with_the_L_letter_and_quit_from_the_tray(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        self.assertIn('_build_tray', methods)
        self.assertIn('self._build_tray()', ast.unparse(methods['__init__']))
        tray = ast.unparse(methods['_build_tray'])
        self.assertIn('QSystemTrayIcon(icon, self)', tray)
        self.assertIn('self.tray.setContextMenu(menu)', tray)
        self.assertIn('self._close_app()', tray)      # выход через трей
        self.assertIn('self._toggle_visibility()', tray)
        self.assertIn('self.tray.show()', tray)
        # Иконка: файл icon.ico рядом с программой, иначе рисуется буква «L».
        icon = ast.unparse(methods['_app_icon'])
        self.assertIn('icon.ico', icon)
        self.assertIn('_drawn_icon()', icon)
        self.assertIn(", 'L')", ast.unparse(methods['_drawn_icon']))
        # Клик по значку переключает окно, при выходе значок убирается.
        self.assertIn('_toggle_visibility()', ast.unparse(methods['_tray_activated']))
        self.assertIn('self.tray.hide()', ast.unparse(methods['_cleanup']))

    def test_browser_window_is_kept_out_of_the_taskbar(self):
        # Значок «G» в панели задач: стиль + явный DeleteTab у оболочки.
        style = {"value": win32_embed.WS_EX_APPWINDOW}
        moved = []

        class FakeUser32Taskbar:
            def GetWindowLongPtrW(self, hwnd, index):
                return style["value"]

            def SetWindowLongPtrW(self, hwnd, index, value):
                # set_window_long передаёт ctypes.c_void_p
                style["value"] = int(getattr(value, 'value', value) or 0)
                return 1

            def SetWindowPos(self, *args):
                moved.append(args[0])
                return 1

        deleted = []
        with patch.object(win32_embed, 'taskbar_delete_tab',
                          lambda hwnd: deleted.append(hwnd) or True):
            ok = win32_embed.hide_from_taskbar(FakeUser32Taskbar(), 4242)
        self.assertTrue(ok)
        self.assertEqual(deleted, [4242])
        self.assertEqual(style["value"] & win32_embed.WS_EX_TOOLWINDOW,
                         win32_embed.WS_EX_TOOLWINDOW)
        self.assertEqual(style["value"] & win32_embed.WS_EX_APPWINDOW, 0)
        self.assertEqual(moved, [4242])
        # Все окна браузера, а не только встроенное.
        with patch.object(win32_embed, '_windows_of',
                          lambda user32, pids: [{'hwnd': 11}, {'hwnd': 12}]), \
                patch.object(win32_embed, 'process_tree', lambda pids: set(pids)), \
                patch.object(win32_embed, 'hide_from_taskbar',
                             lambda user32, hwnd: True):
            result = win32_embed.hide_browser_from_taskbar(object(), {7})
        self.assertEqual(result, [11, 12])
        # GUI вызывает это при встраивании и ещё дважды позже (Chrome может
        # вернуть кнопку после смены заголовка).
        tree = ast.parse((ROOT / 'main.py').read_text())
        host = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                    and n.name == 'NativeHost')
        methods = {m.name: m for m in host.body if isinstance(m, ast.FunctionDef)}
        self.assertIn('hide_taskbar', methods)
        self.assertIn('hide_browser_from_taskbar(self.user32, self.pids)',
                      ast.unparse(methods['hide_taskbar']))
        embed = ast.unparse(methods['_embed'])
        self.assertIn('self.hide_taskbar()', embed)
        self.assertIn('QTimer.singleShot(1200, self.hide_taskbar)', embed)
        self.assertIn('QTimer.singleShot(4000, self.hide_taskbar)', embed)

    def test_zoom_can_be_tuned_from_config_without_a_rebuild(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        code = ast.get_source_segment((ROOT / 'main.py').read_text(), methods['_zoom'])
        namespace = {'ZOOM': 2.0 / 3.0, 'float': float}
        exec(compile(code, '<_zoom>', 'exec'), namespace)
        zoom = namespace['_zoom']

        class Cfg:
            def __init__(self, value):
                self.value = value

            def get(self, key, default=None):
                return self.value

        self.assertAlmostEqual(zoom(SimpleNamespace(cfg=Cfg(0.62))), 0.62, delta=1e-9)
        self.assertAlmostEqual(zoom(SimpleNamespace(cfg=Cfg(None))), 2.0 / 3.0, delta=1e-9)
        self.assertAlmostEqual(zoom(SimpleNamespace(cfg=Cfg('nonsense'))),
                               2.0 / 3.0, delta=1e-9)   # вне диапазона ->默认值
        self.assertAlmostEqual(zoom(SimpleNamespace(cfg=Cfg(5))), 2.0 / 3.0, delta=1e-9)
        # Значение доходит и до сторожа зума, и до рабочего потока.
        guard = ast.unparse(methods['_ensure_zoom'])
        self.assertIn('zoom=self._zoom()', guard)
        automation = ast.unparse(methods['_start_web_automation'])
        self.assertIn('zoom=self._zoom()', automation)

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
        self.assertEqual(self.events[0][0], 'error')
        self.assertEqual(self.events[0][1]['code'], 'no-speech')

    def test_stale_generation_is_dropped(self):
        self.engine._ended = False
        self.engine._event(self.engine._gen + 5, 'result', {'text': 'старое', 'confidence': 1})
        self.assertEqual(self.events, [])

    def test_error_names_are_api_compatible(self):
        self.assertIn('language-not-supported', speech_backend.ERROR_NAMES)
        self.assertIn('not-allowed', speech_backend.ERROR_NAMES)

    def test_language_is_always_a_specific_culture(self):
        # System.Speech выбрасывает исключение на нейтральной культуре 'ru',
        # а страница отдаёт именно document.documentElement.lang = 'ru'.
        self.assertEqual(speech_backend.norm_lang('ru'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('en'), 'en-US')
        self.assertEqual(speech_backend.norm_lang('ru_RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('ru-RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang(''), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang(None), 'ru-RU')

    def test_powershell_host_is_hardened(self):
        script = speech_backend.PS_SCRIPT
        # System.Speech не подгружается сам в -NonInteractive хосте.
        self.assertIn('Add-Type -AssemblyName System.Speech', script)
        # Windows.Forms не загружен: DoEvents под ErrorActionPreference=Stop
        # убивал хост через ~40 мс после 'started'.
        self.assertNotIn('DoEvents', script)
        # Нейтральная культура -> первый конкретный язык той же буквенной пары.
        self.assertIn('IsNeutralCulture', script)
        self.assertIn('SpecificCultures', script)

    def _spawn_engine(self, created, lines=()):
        class WaitStream:
            """stdout живого хоста: отдаёт строки, пока процесс не снят."""

            def __init__(self, done, payload):
                self._done = done
                self._payload = list(payload)

            def __iter__(self):
                for line in self._payload:
                    yield (line + '\n').encode('utf-8')
                self._done.wait(5)

        class FakeProc:
            def __init__(self):
                self._done = threading.Event()
                self.stdout = WaitStream(self._done, lines)
                self.stderr = io.StringIO('')
                self.terminated = 0

            def poll(self):
                return None if self.terminated == 0 else 0

            def wait(self):
                self._done.wait(0.5)
                return 0

            def terminate(self):
                self.terminated += 1
                self._done.set()

        def popen(*args, **kwargs):
            proc = FakeProc()
            created.append({'args': args, 'kwargs': kwargs, 'proc': proc})
            return proc

        return popen

    def _wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_start_is_idempotent_for_the_same_language(self):
        # Реальный лог 20260930: страница рестартовала распознавание ~1 раз в
        # секунду; каждый start() вызывал stop() и убивал только что spawned
        # хост — ни одного события (started/result/error) не приходило.
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        spawn = self._spawn_engine(created, lines=['{"type": "started"}'])
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen', spawn):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: self.events == [('started', {})]),
                            self.events)
            engine.start('ru')          # повтор старта (гонка страницы)
            engine.start('ru-RU')       # та же конкретная культура
            # Живой хост НЕ перезапускается и НЕ снимается.
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]['proc'].terminated, 0)
            self.assertTrue(engine.active)
            # Повторный старт подтверждается событием started (страница ждёт).
            self.assertEqual([k for k, _ in self.events],
                             ['started', 'started', 'started'])
            # Другой язык — реальный перезапуск хоста.
            engine.start('en-US')
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0]['proc'].terminated, 1)
            self.assertEqual(created[0]['kwargs'].get('stderr'), speech_backend.subprocess.PIPE)
            self.assertEqual(created[0]['args'][0][0], 'powershell.exe')
            self.assertEqual(created[0]['args'][0][-1], 'ru-RU')
            self.assertEqual(created[1]['args'][0][-1], 'en-US')
        engine.shutdown()

    def test_host_events_reach_the_page(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "started"}',
                 '{"type": "speechstart"}',
                 '{"type": "hypothesis", "text": "при"}',
                 '{"type": "result", "text": "привет мир", "confidence": 0.9}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 4), self.events)
            kinds = [k for k, _ in self.events]
            self.assertEqual(kinds[:4], ['started', 'speechstart', 'hypothesis', 'result'])
            self.assertTrue(engine.speech_seen)
            self.assertEqual(self.events[3][1]['text'], 'привет мир')
        engine.shutdown()

    def test_stop_ignores_a_stale_page_session(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen', self._spawn_engine(created)):
            engine.start('ru', sid='session-1')
            engine.stop(sid='session-0')   # устаревшая сессия страницы
            self.assertEqual(created[0]['proc'].terminated, 0)
            self.assertTrue(engine.active)
            engine.stop(sid='session-1')   # текущая сессия — останавливается
            self.assertEqual(created[0]['proc'].terminated, 1)
            self.assertFalse(engine.active)
        engine.shutdown()

    def test_explicit_host_error_is_not_downgraded_to_no_speech(self):
        # v6: хост умирал с кодом 2 (нет распознавателя для ru-RU), Python
        # видел EOF и сообщал странице 'no-speech' -> GUI печатал
        # «Речь не распознана», mic_active оставался True, и следующее
        # нажатие уходило в ветку «стоп + отправка» — пустой запрос в чат.
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "recognizers", "count": 1, "langs": ["en-US"], "want": "ru-RU"}',
                 '{"type": "error", "code": "service-not-allowed",'
                 ' "msg": "No Windows speech recognizer installed"}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            # 'recognizers' уходит только в лог (diag), страница его не ждёт.
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 1), self.events)
            engine.stop()
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 2), self.events)
        self.assertEqual([(k, d.get('code')) for k, d in self.events],
                         [('error', 'service-not-allowed'), ('end', None)])
        # Причина сохранена: GUI показывает её вместо «no-speech».
        self.assertEqual(engine.last_error['code'], 'service-not-allowed')
        self.assertIn('recognizer', engine.last_error['msg'])

    def test_fallback_recognizer_is_reported_to_the_user(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "recognizers", "count": 1, "langs": ["en-US"], "want": "ru-RU"}',
                 '{"type": "started", "lang": "en-US", "requested": "ru-RU", "fallback": true}',
                 '{"type": "result", "text": "hello", "confidence": 0.4, "lang": "en-US"}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 2), self.events)
            self.assertEqual(engine.last_notice, ('fallback', 'en-US'))
            engine.stop()
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 3), self.events)
        self.assertEqual([k for k, _ in self.events], ['started', 'result', 'end'])



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
  constructor() { MockSR.last = this; this.active = false; this.starts = 0;
    MockSR.count = (MockSR.count || 0) + 1; }
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

    def test_explicit_start_never_sends_and_restarts_stuck_session(self):
        script = self.HARNESS + """
// Рассинхрон (как в реальном логе): запись «активна», но движок молчит.
const row = new El('div');
const siteMic = new El('button');
siteMic.setAttribute('data-xid', 'input-plate-voice-button');
siteMic.setAttribute('aria-label', 'Микрофон');
siteMic._sized = true;
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

// Явный старт: реплика/хоткей всегда НАЧИНАЕТ запись.
if (vm.runInContext(START_SRC, sandbox) !== true) throw Error('explicit start failed');
if (!MockSR.last || !MockSR.last.active) throw Error('dictation must be recording');
if (MockSR.count !== 1) throw Error('one recognizer expected: ' + MockSR.count);

// Повторный явный старт перезапускает запись, а не «отправляет» сообщение.
if (vm.runInContext(START_SRC, sandbox) !== true) throw Error('restart failed');
if (MockSR.count !== 2) throw Error('recognition must restart: ' + MockSR.count);
if (!MockSR.last.active) throw Error('must still be recording after restart');

// Текст по-прежнему пишется в поле ввода, а не отправляется сразу.
MockSR.last.give('привет мир', true);
if (ta.value !== 'привет мир') throw Error('dictated text must reach the editor');

// И только явный стоп (второе нажатие) завершает запись перед отправкой.
const res = vm.runInContext(STOP_SRC, sandbox);
if (!res || res.ok !== true) throw Error('stop_and_send failed');
if (MockSR.last.active) throw Error('send must stop dictation');
console.log('explicit start ok');
"""
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_MIC_START')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
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
