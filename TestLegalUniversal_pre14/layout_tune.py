"""Режим администратора: положение объектов страницы внутри GUI (сборка pre14).

Зачем этот модуль
-----------------
В v13 («полностью рабочая») единственный дефект — геометрия: поле ввода
промта оказалось слишком низко и уезжает под нижнюю панель окна. Причина
известна только по косвенным признакам, поэтому **pre14 ничего не
исправляет вслепую**: она даёт администратору ручки, которыми можно сдвинуть
объекты страницы живьём, и печатает ЧИСЛА — фактические значения сдвигов и
результат измерения. Эти числа затем вшиваются в постоянную сборку v14.

Принципы (нарушать нельзя)
--------------------------
1. **Поведение по умолчанию == v13 один в один.** Все сдвиги по умолчанию
   нулевые, масштаб 66.7 % (точное значение v13 — 2/3 = 66.667 %; разница
   0.03 % заведомо меньше допуска замера в :func:`ok`, поэтому
   переопределение окна НЕ включается). :func:`ok` сравнивает измеренную
   страницу с настроенной целью и при нулевых сдвигах отвечает так же, как
   ``native_browser.zoom_ok`` в v13 → профиль браузера по-прежнему рисует
   свой нативный зум 67 % сам.
2. **Никаких перезапусков браузера и нажатий клавиш.** Только CDP
   (``Emulation.setDeviceMetricsOverride`` + ``Runtime.evaluate``). Именно
   эскалация зума перезапуском ломала GUI в v10/v11.
3. **Только измерение.** Каждое применение заканчивается замером: ширина,
   высота, dpr, прямоугольник поля ввода — и сколько DIP оно выступает за
   видимую область окна.
4. **Никаких исключений наружу.** Все вызовы CDP обёрнуты: сбой логируется и
   возвращается словарь с ``ok=False``.

Геометрия: окно 453x735 DIP, панели закрывают сверху 54, снизу 30 и по 20
сбоку, поэтому страница видна только в слоте 413x651 DIP (20..433 по X,
54..705 по Y). Раскладка страницы считается в CSS px: ``css = логические DIP /
zoom``. Обратный переход: ``DIP = CSS * logical_w / css_w``.
"""

from __future__ import annotations

import json
import time

import diagnostics as diag
from native_browser import ZOOM, apply_viewport_raw, target_css

TUNE_VERSION = 1
CONFIG_KEY = "layout_tune"

#: Чем именно считается «поле ввода промта» (нижняя кромка берётся самая нижняя).
INPUT_SELECTOR = ('textarea, div[contenteditable="true"], rich-textarea, '
                  '[role="textbox"], input[type="text"]')

DEFAULT_FRAME = {"top": 54, "bottom": 30, "side": 20}

# ключ, минимум, максимум, шаг, значение по умолчанию, делитель, подпись, ед.
# Значения хранятся ЦЕЛЫМИ в единицах ползунка: zoom — десятые доли процента
# (667 = 66.7 %), остальные — целые CSS px. Так конфиг остаётся читаемым, а
# вычисления — детерминированными.
SPEC = (
    # Окно браузера — тоже объект: именно его сдвигом/размером лечится
    # «поле ввода уехало вниз» БЕЗ пустых полос (раскладка остаётся равной
    # самому окну, поэтому незакрашенных участков не появляется).
    ("win_dx",      -200,  200, 1,   0, 1.0,    "Окно браузера: сдвиг X", "DIP"),
    ("win_dy",      -200,  200, 1,   0, 1.0,    "Окно браузера: сдвиг Y", "DIP"),
    ("win_dw",      -300,  300, 1,   0, 1.0,    "Окно браузера: ширина ±", "DIP"),
    ("win_dh",      -400,  400, 1,   0, 1.0,    "Окно браузера: высота ±", "DIP"),
    ("zoom",         400, 1200, 1, 667, 10.0,   "Масштаб страницы", "%"),
    ("width_delta", -200,  200, 1,   0, 1.0,    "Ширина раскладки", "css px"),
    ("height_delta", -400, 400, 1,   0, 1.0,    "Высота раскладки", "css px"),
    ("scroll_x",       0,  400, 1,   0, 1.0,    "Прокрутка X", "css px"),
    ("scroll_y",       0,  800, 1,   0, 1.0,    "Прокрутка Y", "css px"),
    ("offset_x",    -150,  150, 1,   0, 1.0,    "Сдвиг содержимого X", "css px"),
    ("offset_y",    -300,  300, 1,   0, 1.0,    "Сдвиг содержимого Y", "css px"),
    ("input_dx",    -150,  150, 1,   0, 1.0,    "Сдвиг поля ввода X", "css px"),
    ("input_dy",    -300,  300, 1,   0, 1.0,    "Сдвиг поля ввода Y", "css px"),
    ("pad_bottom",     0,  400, 1,   0, 1.0,    "Нижний отступ чата", "css px"),
)

NUM_KEYS = tuple(row[0] for row in SPEC)
LABELS = {row[0]: row[6] for row in SPEC}
UNITS = {row[0]: row[7] for row in SPEC}
LIMITS = {row[0]: (row[1], row[2]) for row in SPEC}
STEPS = {row[0]: row[3] for row in SPEC}
DIVISOR = {row[0]: row[5] for row in SPEC}

DEFAULTS = {row[0]: row[4] for row in SPEC}
DEFAULTS["selector"] = INPUT_SELECTOR
# Рамка видимой области по умолчанию ВЫКЛЮЧЕНА: при полностью
# стандартной настройке страница вообще не должна модифицироваться.
DEFAULTS["frame"] = 0
KEYS = tuple(DEFAULTS)


# --------------------------------------------------------------------------- #
#  Значения: чтение, проверка, хранение
# --------------------------------------------------------------------------- #
def zoom_percent(tune=None):
    """Масштаб в процентах (66.7 по умолчанию — это 2/3 из v13)."""
    raw = float((tune or {}).get("zoom", DEFAULTS["zoom"]))
    return raw / 10.0


def zoom_of(tune=None):
    """Масштаб как множитель (0.667 = страница уменьшена до 67 %)."""
    return max(0.2, min(2.0, zoom_percent(tune) / 100.0))


def is_default(tune=None):
    """True, если настройка НЕ меняет страницу — тогда работает путь v13.

    Пока это так, main.py идёт прежним проверенным путём (`zoom_ok` /
    `apply_zoom`) и не внедряет в страницу ничего: ни сдвигов, ни рамки.
    Как только администратор что-то сдвинул или включил рамку — включается
    измерительный путь `layout_tune`.
    """
    tune = tune or {}
    if _int(tune.get("frame", 0), 0) != int(DEFAULTS["frame"]):
        return False
    if str(tune.get("selector") or INPUT_SELECTOR).strip() != INPUT_SELECTOR:
        return False
    for key in NUM_KEYS:
        if _num(tune.get(key, DEFAULTS[key])) != float(DEFAULTS[key]):
            return False
    return True


def _num(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _int(value, fallback=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(fallback)


def clamp(key, value):
    key = str(key)
    if key not in LIMITS:
        return value
    low, high = LIMITS[key]
    return max(int(low), min(int(high), _int(value, DEFAULTS.get(key, 0))))


def normalize(data=None):
    """Привести сырой словарь к полному набору ключей в допустимых границах."""
    data = data if isinstance(data, dict) else {}
    tune = {}
    for key in NUM_KEYS:
        tune[key] = clamp(key, data.get(key, DEFAULTS[key]))
    selector = str(data.get("selector") or INPUT_SELECTOR).strip()
    tune["selector"] = selector or INPUT_SELECTOR
    tune["frame"] = 1 if _int(data.get("frame", DEFAULTS["frame"]), 0) else 0
    return tune


def load(cfg=None):
    """Прочитать настройки из конфига (отсутствуют — значения по умолчанию)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    stored = cfg.get(CONFIG_KEY)
    return normalize(stored if isinstance(stored, dict) else {})


def save(cfg, tune):
    """Записать настройки в конфиг (возвращает тот же словарь)."""
    if not isinstance(cfg, dict):
        cfg = {}
    cfg[CONFIG_KEY] = normalize(tune)
    cfg[CONFIG_KEY]["version"] = TUNE_VERSION
    return cfg


def as_floats(tune=None):
    """Значения в «человеческих» единицах: проценты и CSS px (для отчёта)."""
    tune = normalize(tune)
    out = {}
    for key in NUM_KEYS:
        raw = float(tune[key]) / float(DIVISOR[key])
        # Целые px остаются целыми: отчёт должен вшиваться в код как есть.
        out[key] = int(round(raw)) if float(DIVISOR[key]) == 1.0 else round(raw, 3)
    out["selector"] = tune["selector"]
    out["frame"] = tune["frame"]
    return out


# --------------------------------------------------------------------------- #
#  Геометрия
# --------------------------------------------------------------------------- #
def split_geom(geom=None):
    """Разобрать описание: логические DIP, физические пиксели, рамка, окно GUI.

    `logical` / `physical` — размер САМОГО окна браузера (оно может быть
    сдвинуто и уменьшено ползунками), `top`/`bottom`/`side` — сколько этого
    окна закрыто панелями GUI. `window` и `gui_*` нужны `auto_fill`, чтобы
    посчитать видимый слот в координатах всего окна приложения.
    """
    geom = geom if isinstance(geom, dict) else {}
    logical = tuple(geom.get("logical") or (0, 0))[:2]
    physical = tuple(geom.get("physical") or logical)[:2]
    frame = dict(DEFAULT_FRAME)
    for key in ("top", "bottom", "side"):
        frame[key] = max(0, _int(geom.get(key, frame[key]), frame[key]))
    meta = {
        "window": tuple(geom.get("window") or logical)[:2],
        "gui_top": max(0, _int(geom.get("gui_top", frame["top"]), frame["top"])),
        "gui_bottom": max(0, _int(geom.get("gui_bottom", frame["bottom"]), frame["bottom"])),
        "gui_side": max(0, _int(geom.get("gui_side", frame["side"]), frame["side"])),
    }
    return logical, physical, frame, meta


def target(logical, physical=None, tune=None):
    """(css_w, css_h, dsf, zoom) — цель раскладки с учётом сдвигов."""
    tune = normalize(tune)
    logical_w, logical_h = int(logical[0] or 0), int(logical[1] or 0)
    physical_w, physical_h = int(physical[0] or logical_w), int(physical[1] or logical_h)
    zoom = zoom_of(tune)
    css_w, css_h, _dsf = target_css(logical_w, logical_h, physical_w, physical_h, zoom)
    css_w = max(320, int(round(css_w + float(tune["width_delta"]))))
    css_h = max(320, int(round(css_h + float(tune["height_delta"]))))
    dsf = (float(physical_w) / css_w) if css_w else 1.0
    return css_w, css_h, dsf, zoom


def auto_fill(geom=None, tune=None):
    """Поставить ОКНО БРАУЗЕРА ровно в видимый слот (старт подбора).

    Не догадка, а арифметика: окно приложения 453x735 DIP, панели закрывают
    сверху 54, снизу 30 и по 20 сбоку — страница видна только в слоте 413x651
    DIP. Пока окно браузера занимает весь плейсхолдер, его нижние 84 DIP
    уходят под панели: отсюда «поле ввода промта уехало слишком низко вниз».

    Лечим сдвигом и уменьшением САМОГО окна, а не урезанием раскладки внутри
    него: тогда раскладка остаётся равной клиентской области окна и пустых
    незакрашенных полос не появляется вовсе.
    """
    tune = normalize(tune)
    _logical, _physical, _frame, meta = split_geom(geom)
    window = meta["window"]
    if not window[0] or not window[1]:
        return tune
    tune["win_dx"] = clamp("win_dx", meta["gui_side"])
    tune["win_dy"] = clamp("win_dy", meta["gui_top"])
    tune["win_dw"] = clamp("win_dw", -(int(meta["gui_side"]) * 2))
    tune["win_dh"] = clamp("win_dh", -(int(meta["gui_top"]) + int(meta["gui_bottom"])))
    # Окно стало слотом: внутри него больше ничего подгонять не нужно.
    tune["width_delta"] = 0
    tune["height_delta"] = 0
    return tune


def window_rect(geom=None, tune=None):
    """Прямоугольник окна браузера (x, y, w, h) в координатах окна GUI, DIP."""
    _logical, _physical, _frame, meta = split_geom(geom)
    tune = normalize(tune)
    window = meta["window"]
    return (int(tune["win_dx"]), int(tune["win_dy"]),
            max(1, int(window[0]) + int(tune["win_dw"])),
            max(1, int(window[1]) + int(tune["win_dh"])))


# --------------------------------------------------------------------------- #
#  JS: замер и применение
# --------------------------------------------------------------------------- #
_PROBE_TEMPLATE = """(function(){
  function box(el){
    var r = el.getBoundingClientRect();
    return {l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right),
            b: Math.round(r.bottom), w: Math.round(r.width), h: Math.round(r.height)};
  }
  var el = null, best = -1e9, used = '';
  try {
    var list = document.querySelectorAll(__SELECTOR__);
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      var r = e.getBoundingClientRect();
      if (!r || r.width < 60 || r.height < 10) { continue; }
      if (r.bottom > best) { best = r.bottom; el = e; }
    }
    if (el) {
      var tag = (el.tagName || '?').toLowerCase();
      used = tag;
      if (el.getAttribute && el.getAttribute('role')) {
        used = used + '[' + el.getAttribute('role') + ']';
      } else if (el.getAttribute && el.getAttribute('contenteditable') === 'true') {
        used = used + '[contenteditable]';
      }
      if (el.id) { used = used + '#' + el.id; }
    }
  } catch (err) { used = ''; }
  var de = document.documentElement;
  return JSON.stringify({
    iw: window.innerWidth, ih: window.innerHeight,
    dpr: window.devicePixelRatio || 0,
    sw: de ? de.scrollWidth : 0, sh: de ? de.scrollHeight : 0,
    sx: window.scrollX || window.pageXOffset || 0,
    sy: window.scrollY || window.pageYOffset || 0,
    found: el ? 1 : 0, sel: used, rect: el ? box(el) : null,
    url: (location && location.href) ? String(location.href).slice(0, 200) : ''
  });
})()"""

_TUNE_TEMPLATE = """(function(t){
  var d = document;
  var out = [];
  if (t.offset_x || t.offset_y) {
    out.push('body{transform:translate(' + t.offset_x + 'px,' + t.offset_y + 'px);}');
  }
  if (t.pad_bottom) {
    out.push('body{padding-bottom:' + t.pad_bottom + 'px;}');
  }
  if (t.selector && (t.input_dx || t.input_dy)) {
    out.push(t.selector + '{transform:translate(' + t.input_dx + 'px,' +
             t.input_dy + 'px);}');
  }
  var s = d.getElementById('legalyze-tune-css');
  if (!s) {
    s = d.createElement('style');
    s.id = 'legalyze-tune-css';
    s.setAttribute('type', 'text/css');
    (d.head || d.documentElement).appendChild(s);
  }
  s.textContent = out.join('\\n');
  var f = d.getElementById('legalyze-tune-frame');
  if (t.frame && t.fw > 0 && t.fh > 0) {
    if (!f) {
      f = d.createElement('div');
      f.id = 'legalyze-tune-frame';
      f.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;' +
        'box-sizing:border-box;border:2px dashed #ff2d55;margin:0;padding:0;';
      (d.body || d.documentElement).appendChild(f);
    }
    f.style.left = t.fx + 'px';
    f.style.top = t.fy + 'px';
    f.style.width = t.fw + 'px';
    f.style.height = t.fh + 'px';
  } else if (f && f.parentNode) {
    f.parentNode.removeChild(f);
  }
  if (t.scroll_x || t.scroll_y) {
    try { window.scrollTo(t.scroll_x, t.scroll_y); } catch (e) {}
  }
  return JSON.stringify({ok: 1, rules: out.length});
})(__TUNE__)"""


def probe_js(selector=None):
    return _PROBE_TEMPLATE.replace("__SELECTOR__",
                                   json.dumps(str(selector or INPUT_SELECTOR)))


def tune_js(payload):
    return _TUNE_TEMPLATE.replace("__TUNE__", json.dumps(payload))


def _probe(page, selector=None):
    """Один замер страницы: окно, прокрутка, прямоугольник поля ввода."""
    if page is None:
        return {}
    try:
        raw = page.eval(probe_js(selector), timeout=4).get("value")
    except Exception:
        diag.exception("layout_tune.probe")
        return {}
    try:
        data = json.loads(raw or "{}")
    except Exception:
        diag.exception("layout_tune.probe_json")
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
#  Применение
# --------------------------------------------------------------------------- #
def injected(page):
    """Жив ли ещё внедрённый стиль (после перезагрузки страницы он исчезает)."""
    if page is None:
        return False
    try:
        value = page.eval("!!document.getElementById('legalyze-tune-css')",
                          timeout=2).get("value")
    except Exception:
        diag.exception("layout_tune.injected")
        return False
    return bool(value)


def inject(page, tune=None, geom=None):
    """Внедрить сдвиги (transform / padding / прокрутку) и рамку видимой области."""
    tune = normalize(tune)
    payload = {
        "offset_x": int(tune["offset_x"]),
        "offset_y": int(tune["offset_y"]),
        "input_dx": int(tune["input_dx"]),
        "input_dy": int(tune["input_dy"]),
        "pad_bottom": int(tune["pad_bottom"]),
        "scroll_x": int(tune["scroll_x"]),
        "scroll_y": int(tune["scroll_y"]),
        "selector": tune["selector"],
        "frame": 1 if int(tune["frame"]) else 0,
        "fx": 0, "fy": 0, "fw": 0, "fh": 0,
    }
    logical, physical, frame, _meta = split_geom(geom)
    if logical[0] and logical[1]:
        css_w, css_h, _dsf, _zoom = target(logical, physical, tune)
        css_per_dip = (float(css_w) / float(logical[0])) if logical[0] else 1.0
        payload["fx"] = int(round(frame["side"] * css_per_dip))
        payload["fy"] = int(round(frame["top"] * css_per_dip))
        payload["fw"] = int(round(max(0.0, float(logical[0]) - 2.0 * frame["side"]) * css_per_dip))
        payload["fh"] = int(round(max(0.0, float(logical[1]) - frame["top"] - frame["bottom"]) * css_per_dip))
    if page is None:
        return False
    nothing = not (payload["frame"] or payload["offset_x"] or payload["offset_y"]
                   or payload["input_dx"] or payload["input_dy"]
                   or payload["pad_bottom"] or payload["scroll_x"]
                   or payload["scroll_y"])
    # Внедрять нечего и раньше ничего не внедряли — не трогаем страницу вообще.
    if nothing and not injected(page):
        return True
    try:
        page.eval(tune_js(payload), timeout=4)
    except Exception:
        diag.exception("layout_tune.inject")
        return False
    return True


def ok(page, tune=None, geom=None, tol_w=28, tol_h=48):
    """Совпадает ли ИЗМЕРЕННАЯ страница с настроенной целью.

    Допуски — как в ``native_browser.zoom_ok``: при нулевых сдвигах ответ
    такой же, как у v13, поэтому лишний раз окно не переопределяется.
    """
    tune = normalize(tune)
    logical, physical, _frame, _meta = split_geom(geom)
    css_w, css_h, _dsf, _zoom = target(logical, physical, tune)
    data = _probe(page, tune["selector"])
    iw = int(data.get("iw") or 0)
    if not iw:
        return False, data
    if abs(iw - css_w) > tol_w:
        return False, data
    if abs(int(data.get("ih") or 0) - css_h) > tol_h:
        return False, data
    if physical[0]:
        rendered = iw * float(data.get("dpr") or 0.0)
        if abs(rendered - float(physical[0])) > max(6.0, 0.03 * float(physical[0])):
            return False, data
    return True, data


#: Последняя цель, которую уже пробовали поставить: (цель, время, сошлась,
#: страница). Ссылка на страницу хранится, чтобы `id()` не переиспользовался.
_LAST = {}


def reset_history():
    """Забыть историю попыток (для тестов и полного сброса)."""
    _LAST.clear()


def _last_attempt(page, target):
    entry = _LAST.get(id(page))
    if entry and entry[-1] is page and entry[0] == target:
        return entry
    return None


def apply(page, tune=None, geom=None, settle=0.2, steps=3, force=False,
          cooldown=30.0):
    """Применить настройку и ВЕРНУТЬ ИЗМЕРЕННЫЙ результат (без исключений).

    1. Сначала замер: если страница уже такой ширины/высоты — ничего не
       трогаем (это ровно поведение v13 при нулевых сдвигах).
    2. Иначе — лестница ``Emulation.setDeviceMetricsOverride`` с уточнением по
       измерению: ни перезапуска браузера, ни нажатий клавиш, ни смены профиля.
    3. Затем внедряются сдвиги и рамка видимой области.
    4. Всё заканчивается замером — по нему администратор видит эффект.

    `cooldown`: если та же самая цель уже пробовалась и НЕ сошлась
    (например, сайт жёстко задаёт минимальную высоту), лестница не
    повторяется раньше чем через `cooldown` секунд. Без этого сторож зума
    каждые 5 с заново перестраивал раскладку — и страница «гуляла».
    """
    tune = normalize(tune)
    logical, physical, frame, _meta = split_geom(geom)
    result = {
        "ok": False, "source": "none", "tune": dict(tune),
        "logical": tuple(int(v) for v in logical),
        "physical": tuple(int(v) for v in physical),
        "frame": dict(frame), "css": (0, 0), "dsf": 0.0, "zoom": round(zoom_of(tune), 4),
    }
    if page is None or not logical[0] or not logical[1]:
        return result
    css_w, css_h, dsf, _zoom = target(logical, physical, tune)
    result["css"] = (css_w, css_h)
    result["dsf"] = round(dsf, 4)

    attempt = None if force else _last_attempt(page, result["css"])
    cooling = bool(attempt and (time.monotonic() - attempt[1]) < float(cooldown))

    already, _data = (False, {})
    if not force:
        already, _data = ok(page, tune, geom)
    if already:
        result["source"] = "native"
        _LAST.pop(id(page), None)
    elif cooling:
        # Та же цель, и в прошлый раз она НЕ сошлась (например, сайт жёстко
        # задаёт минимальную высоту): не перестраиваем раскладку каждые 5 с —
        # иначе сторож зума заставляет страницу «гулять». Сдвиги и рамка всё
        # равно внедряются ниже.
        result["source"] = "cooldown"
    else:
        _LAST.pop(id(page), None)
        width, height = css_w, css_h
        fitted = False
        for _step in range(max(1, int(steps))):
            dsf = (float(physical[0] or logical[0]) / width) if width else 1.0
            if not apply_viewport_raw(page, width, height, dsf):
                break
            time.sleep(settle)
            data = _probe(page, tune["selector"])
            iw = int(data.get("iw") or 0)
            ih = int(data.get("ih") or 0)
            if iw and abs(iw - width) <= 8 and (not ih or abs(ih - height) <= 8):
                fitted = True
                break
            if not iw or not width:
                break
            scale = float(width) / float(iw)
            if abs(scale - 1.0) < 0.02:
                # Ширина верная, но условие не сошлось — крутить бессмысленно.
                break
            scale = max(0.5, min(2.0, scale))
            width = max(320, int(round(width * scale)))
            height = max(320, int(round(height * scale)))
        result["css"] = (width, height)
        result["dsf"] = round((float(physical[0] or logical[0]) / width) if width else 1.0, 4)
        result["source"] = "emulation-adaptive" if fitted else "emulation-failed"
        result["ok"] = bool(fitted)
        if not fitted:
            _LAST[id(page)] = (result["css"], time.monotonic(), False, page)
    inject(page, tune, geom)
    if not force and already:
        result["ok"] = True
    result["measure"] = measure(page, tune, geom)
    diag.event("layout.tune_applied", source=result["source"], ok=bool(result["ok"]),
               css=list(result["css"]), dsf=result["dsf"], zoom=result["zoom"],
               default=is_default(tune))
    return result


# --------------------------------------------------------------------------- #
#  Измерение и отчёт
# --------------------------------------------------------------------------- #
def measure(page, tune=None, geom=None):
    """Полный замер: окно, видимый слот, поле ввода и его выход за границы."""
    tune = normalize(tune)
    logical, physical, frame, _meta = split_geom(geom)
    css_w, css_h, dsf, zoom = target(logical, physical, tune)
    data = _probe(page, tune["selector"])
    lw = int(logical[0] or 0)
    lh = int(logical[1] or 0)
    css_per_dip = (float(css_w) / float(lw)) if lw else 0.0
    dip_per_css = (float(lw) / float(css_w)) if css_w else 0.0

    def dip(value):
        return round(float(value or 0) * dip_per_css, 1)

    vis_left = frame["side"]
    vis_top = frame["top"]
    vis_right = max(vis_left, lw - frame["side"])
    vis_bottom = max(vis_top, lh - frame["bottom"])
    rect = data.get("rect") if isinstance(data.get("rect"), dict) else None
    out = {
        "css": (css_w, css_h), "dsf": round(dsf, 4), "zoom": round(zoom, 4),
        "logical": (lw, lh),
        "physical": (int(physical[0] or lw), int(physical[1] or lh)),
        "frame": dict(frame),
        "css_per_dip": round(css_per_dip, 4),
        "inner": {
            "w": int(data.get("iw") or 0), "h": int(data.get("ih") or 0),
            "dpr": round(float(data.get("dpr") or 0.0), 4),
            "sw": int(data.get("sw") or 0), "sh": int(data.get("sh") or 0),
            "sx": int(data.get("sx") or 0), "sy": int(data.get("sy") or 0),
        },
        "visible_dip": {"left": vis_left, "top": vis_top,
                        "right": vis_right, "bottom": vis_bottom},
        "input": {"found": int(data.get("found") or 0),
                  "selector": str(data.get("sel") or ""),
                  "css": rect, "dip": None},
        "url": str(data.get("url") or ""),
        "overflow_x": max(0, int(data.get("sw") or 0) - int(data.get("iw") or 0)),
    }
    if rect:
        out["input"]["dip"] = {k: dip(rect.get(k, 0)) for k in ("l", "t", "r", "b", "w", "h")}
    box = out["input"]["dip"] or {}
    out["below_dip"] = int(round(float(box.get("b", 0) or 0) - vis_bottom)) if box else None
    out["above_dip"] = int(round(vis_top - float(box.get("t", 0) or 0))) if box else None
    out["left_dip"] = int(round(vis_left - float(box.get("l", 0) or 0))) if box else None
    out["right_dip"] = int(round(float(box.get("r", 0) or 0) - vis_right)) if box else None
    return out


def _line(values):
    parts = []
    for key in NUM_KEYS:
        if key == "zoom":
            continue
        parts.append("%s=%s" % (key, values[key]))
    return " ".join(parts)


def report(tune=None, measured=None, geom=None):
    """Готовый к отправке отчёт: значения сдвигов + результат измерения."""
    tune = normalize(tune)
    measured = measured if isinstance(measured, dict) else {}
    values = as_floats(tune)
    logical, physical, frame, _meta = split_geom(geom)
    inner = measured.get("inner") or {}
    visible = measured.get("visible_dip") or {}
    box = measured.get("input") or {}
    dip_box = box.get("dip") or {}
    lines = []
    lines.append("=== Legalyze pre14 — режим администратора, положение объектов ===")
    lines.append("")
    lines.append("Окно (DIP): %sx%s   рамка GUI: верх %s, низ %s, бока %s"
                 % (int(logical[0] or 0), int(logical[1] or 0),
                    frame["top"], frame["bottom"], frame["side"]))
    if visible:
        lines.append("Видимая область (DIP): x %s..%s, y %s..%s  (%sx%s)"
                     % (visible.get("left"), visible.get("right"),
                        visible.get("top"), visible.get("bottom"),
                        (visible.get("right") or 0) - (visible.get("left") or 0),
                        (visible.get("bottom") or 0) - (visible.get("top") or 0)))
    rect = window_rect(geom, tune)
    lines.append("Окно браузера (DIP): x %d..%d, y %d..%d  (%dx%d)"
                 % (rect[0], rect[0] + rect[2], rect[1], rect[1] + rect[3],
                    rect[2], rect[3]))
    lines.append("Раскладка: css %sx%s, dsf %s, зум %s"
                 % (measured.get("css", ("?", "?"))[0], measured.get("css", ("?", "?"))[1],
                    measured.get("dsf"), measured.get("zoom")))
    if inner:
        lines.append("Измерено: innerWidth=%s innerHeight=%s dpr=%s scrollWidth=%s"
                     % (inner.get("w"), inner.get("h"), inner.get("dpr"), inner.get("sw")))
    lines.append("")
    lines.append("--- ЗНАЧЕНИЯ СДВИГОВ (вшить в сборку) ---")
    lines.append("zoom=%s %s" % (values["zoom"], _line(values)))
    lines.append("selector=%s" % values["selector"])
    lines.append("")
    lines.append("--- JSON ---")
    lines.append(json.dumps(values, ensure_ascii=False))
    lines.append("")
    lines.append("--- РЕЗУЛЬТАТ ---")
    if box.get("found"):
        lines.append("поле ввода: %s" % (box.get("selector") or "?"))
        lines.append("  прямоугольник css: %s" % (box.get("css") or {}))
        lines.append("  прямоугольник DIP: %s" % (dip_box or {}))
        for key, hint in (("below_dip", "ниже видимой нижней кромки (норма <= 0)"),
                          ("above_dip", "выше видимой верхней кромки (норма <= 0)"),
                          ("left_dip", "левее видимой левой кромки (норма <= 0)"),
                          ("right_dip", "правее видимой правой кромки (норма <= 0)")):
            value = measured.get(key)
            lines.append("  %s = %s   %s" % (key, "н/д" if value is None else value, hint))
    else:
        lines.append("поле ввода НЕ найдено селектором: %s" % values["selector"])
    lines.append("  overflow_x = %s css px (норма 0)" % measured.get("overflow_x"))
    if measured.get("url"):
        lines.append("  url = %s" % measured["url"])
    return "\n".join(lines)


def default_zoom_percent():
    """Масштаб по умолчанию в процентах (66.667 = 2/3 — нативный зум v13)."""
    return round(float(ZOOM) * 100.0, 3)
