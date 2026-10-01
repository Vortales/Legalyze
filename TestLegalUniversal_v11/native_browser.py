"""Managed out-of-process Chromium/Chrome host with the page's own microphone.

Why a real browser and not QtWebEngine
--------------------------------------
The chat page records voice with the browser's own speech pipeline (Web Speech
API and/or a `getUserMedia` stream sent to Google). That pipeline is not part
of open-source Chromium: it is wired to the browser vendor's cloud service and
is only present in *branded* builds.

* QtWebEngine exposes `SpeechRecognition`, but `start()` kills the renderer
  (no key) — proven by the v4 logs;
* vanilla/ungoogled Chromium and Electron expose the object and never return a
  result ("Build a desktop app on Electron, and the API may be present and
  simply never return a result" — AssemblyAI);
* Microsoft Edge exposes `webkitSpeechRecognition` but MDN issue #22126
  documents it returning nothing at all.

Therefore **Google Chrome (branded) is the reference target**; everything else
is a fallback and is verified at runtime (`browser.speech_surface`).

What made the old `release/` build unstable, and what is fixed here
---------------------------------------------------------------
1. **Consent interstitials (Spain: "Aceptar todo").** A fresh profile in the EU
   gets a consent page before the chat. Worse, the old target picker matched
   *any* `google.com` page, so automation attached to `consent.google.com` and
   waited for a chat that was never there — "chrome starts and hangs".
   Fixed: consent hosts are excluded from target selection, a multi-language
   consent clicker runs on every document, and the profile is persistent so
   consent is accepted exactly once per machine.
2. **"Wide window with a single ИИ label".** `GetClientRect` returns PHYSICAL
   pixels while a PMv2-aware Chromium reads window sizes as DIPs, so on a
   125%/150% display the browser thought it was ~1.5x wider and rendered the
   desktop layout (centred wordmark) instead of the chat. Fixed by pinning the
   layout over CDP (`Emulation.setDeviceMetricsOverride`) — the CSS viewport is
   identical on every machine, only the pixels follow the display.
3. **Hangs.** Caused by profile-singleton hand-off (a stale Chromium owning the
   profile makes the new process exit immediately), by GPU/driver stalls
   (Parsec/virtual adapters, hybrid Intel+NVIDIA) and by embedding a helper
   window. Fixed with a dedicated profile + automatic fresh-profile retry, a
   `--disable-gpu` retry, checked window discovery and full rollback on any
   embedding failure.

Microphone permission is granted up front over CDP
(`Browser.grantPermissions`, which works in real Chrome and used to fail with
-32000 in QtWebEngine) plus `--use-fake-ui-for-media-stream`, so no permission
bubble ever blocks the user anywhere in the world.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import diagnostics as diag
import win32_embed

DEFAULT_URL = "https://google.com/ai"
ZOOM = 2.0 / 3.0            # native 67 % window zoom, as in the working builds

# Chrome stores the page zoom as a "zoom level": factor = 1.2 ** level.
# log(2/3) / log(1.2) = -2.223901614059533 — the very value the working
# `release/` build wrote into the profile preferences.
ZOOM_LEVEL = math.log(ZOOM) / math.log(1.2)
ZOOM_HOSTS = ("google.com", "www.google.com", "gemini.google.com",
              "aistudio.google.com", "accounts.google.com")

# Hosts that are NOT the chat: consent/account walls. Attaching to one of these
# is what made the old build "hang" (Spain: EU consent interstitial).
NON_APP_HOSTS = (
    "consent.google.com",
    "accounts.google.com",
    "policies.google.com",
    "support.google.com",
    "myaccount.google.com",
    "gds.google.com",
    "ogs.google.com",
    "chrome-error",
    "devtools",
    "about:blank",
    "chrome://",
)
APP_HOSTS = (
    "gemini.google.com",
    "aistudio.google.com",
    "google.com/ai",
    "bard.google.com",
    "www.google.com",
)

BROWSER_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0")


# --------------------------------------------------------------- discovery --
class Candidate:
    __slots__ = ("name", "kind", "path", "rank")

    def __init__(self, name, kind, path, rank):
        self.name = name
        self.kind = kind
        self.path = str(path)
        self.rank = rank

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "path": self.path, "rank": self.rank}

    def __repr__(self):  # pragma: no cover - diagnostics only
        return "Candidate(%s, %s, %s)" % (self.name, self.kind, self.path)


def _exists(path):
    try:
        return bool(path) and Path(path).is_file()
    except Exception:
        return False


def _registry_app_path(exe_name):
    """`App Paths\\<exe>` is the documented, localisation-independent lookup."""
    try:
        import winreg
    except Exception:
        return None
    for hive, root in ((getattr(__import__("winreg"), "HKEY_CURRENT_USER"), "HKCU"),
                       (getattr(__import__("winreg"), "HKEY_LOCAL_MACHINE"), "HKLM")):
        try:
            key = winreg.OpenKey(
                hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe_name)
            value, _ = winreg.QueryValueEx(key, "")
            winreg.CloseKey(key)
            if value:
                return str(value).strip('"')
        except Exception:
            continue
    return None


def _env(name):
    return os.environ.get(name) or ""


def discover_browsers(app_dir=None, configured=None):
    """Ordered candidates: pinned portable build -> Chrome -> Edge -> others.

    A pinned portable build (see `tools/fetch_chrome.py`) is the most
    deterministic option: every user gets the same engine. Installed Chrome is
    the best microphone host. Edge/Chromium are last-resort fallbacks.
    """
    app_dir = Path(app_dir) if app_dir else Path.cwd()
    local = Path(_env("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    program_files = Path(_env("ProgramFiles") or r"C:\Program Files")
    program_files_x86 = Path(_env("ProgramFiles(x86)") or r"C:\Program Files (x86)")

    out = []

    def add(name, kind, path, rank):
        if _exists(path):
            out.append(Candidate(name, kind, path, rank))

    # 0. explicit override from config.json (support/debugging)
    add("configured", "configured", configured, 0)

    # 1. pinned portable build shipped/downloaded next to the application
    for sub in ("browser", "chromium"):
        for name in ("chrome.exe", "chromium.exe", "msedge.exe"):
            add("portable", "portable", app_dir / sub / name, 1)
    add("portable", "portable", app_dir / "chrome.exe", 1)

    # 2. installed Google Chrome (the reference Web Speech implementation)
    add("chrome", "chrome", _registry_app_path("chrome.exe"), 2)
    add("chrome", "chrome", program_files / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    add("chrome", "chrome", program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe", 2)
    add("chrome", "chrome", local / "Google" / "Chrome" / "Application" / "chrome.exe", 2)

    # 3. Edge (present on every Windows 10/11, weaker speech support)
    add("edge", "edge", _registry_app_path("msedge.exe"), 3)
    add("edge", "edge", program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe", 3)
    add("edge", "edge", program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe", 3)

    # 4. other Chromium-family browsers
    add("brave", "chromium", program_files / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", 4)
    add("brave", "chromium", local / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe", 4)
    add("vivaldi", "chromium", local / "Vivaldi" / "Application" / "vivaldi.exe", 4)
    add("chromium", "chromium", local / "Chromium" / "Application" / "chrome.exe", 5)

    # de-duplicate by resolved path, keep the best rank
    best = {}
    for cand in out:
        try:
            key = str(Path(cand.path).resolve()).lower()
        except Exception:
            key = cand.path.lower()
        if key not in best or cand.rank < best[key].rank:
            best[key] = cand
    ordered = sorted(best.values(), key=lambda c: (c.rank, c.name))
    diag.event("browser.discovered", count=len(ordered),
               candidates=[c.as_dict() for c in ordered][:8])
    return ordered


# ------------------------------------------------------------------ launch --
def build_args(exe, port, profile, url, width=453, height=735, gpu=True,
               staging=(0, 0), extra=(), dsf=None):
    """Flags: only switches that really exist in modern Chrome.

    Three of the inherited flags produced the yellow
    "You are using an unsupported command-line flag…" bar (and that bar both
    looks wrong and steals vertical space from the chat):

    * `--remote-debugging-address` — REMOVED from Chrome (crbug 40242234);
    * `--use-fake-device-for-media-stream=disabled` — a bogus value for a
      testing switch; worse, the switch itself makes `getUserMedia` return a
      FAKE capture device, which would silently kill the real microphone;
    * `--hide-crash-restore-window` — not a Chrome switch at all
      (the real one is `--hide-crash-restore-bubble`);
    * `--disable-infobars` — a no-op since Chrome 68, kept only for looks.

    `dsf` = `--force-device-scale-factor`: the last-resort way to make the page
    lay out at `window / zoom` CSS px and scale into the window.
    """
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % profile,
        "--app=%s" % url,
        # NOTE: `--use-fake-ui-for-media-stream` is deliberately NOT used:
        # modern Chrome lists it as an unsupported command-line flag and shows
        # the yellow bar. The microphone is granted in the profile instead
        # (`write_permissions`) and over CDP (`grant_microphone`).
        # `--use-fake-device-for-media-stream` is NOT used either: it replaces
        # the real microphone with a fake test signal.
        "--no-first-run",
        "--no-default-browser-check",
        "--noerrdialogs",
        "--disable-session-crashed-bubble",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        # The embedded window is a child that Windows may consider occluded.
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        ("--disable-features=Translate,OptimizationHints,MediaRouter,"
         "GlobalMediaControls,DialMediaRouteProvider,CalculateNativeWinOcclusion"),
        # Off-screen staging: the window exists (so it can be found and
        # embedded) but is never visible before the Qt cover is on top.
        "--window-position=%d,%d" % (int(staging[0]), int(staging[1])),
        # Logical (DIP) size of the placeholder: the browser is created with the
        # size it will have, so there is no "window is wider than the slot"
        # moment that pushes the chat to the right.
        "--window-size=%d,%d" % (int(width), int(height)),
    ]
    if not gpu:
        args.append("--disable-gpu")
        args.append("--disable-gpu-compositing")
        args.append("--disable-software-rasterizer")
    if dsf:
        args.append("--force-device-scale-factor=%s" % round(float(dsf), 4))
    if os.environ.get("LEGALYZE_BROWSER_LOG") == "1":
        args.append("--enable-logging")
        args.append("--log-file=%s" % (Path(profile).parent / "chromium.log"))
    args.extend(extra)
    return args


class NativeBrowser:
    """Owns the browser process, its DevTools port and (via win32_embed) its
    window. Knows nothing about Qt."""

    def __init__(self, url=DEFAULT_URL, profile_dir=None, app_dir=None,
                 report=None, gpu=None, attempt=0, force_dsf=None):
        self.url = url
        self.app_dir = Path(app_dir) if app_dir else Path.cwd()
        base = Path(profile_dir) if profile_dir else self.app_dir / "browser-profile"
        self.profile_base = Path(base)
        self.report = report or diag.event
        self.port = None
        self.proc = None
        self.exe = None
        self.kind = "unknown"
        # Last-resort zoom: --force-device-scale-factor makes the browser treat
        # the window as `1 / dsf` times bigger in DIP, so the page is laid out
        # at `window / zoom` CSS px and scaled into the window without any
        # profile preference or CDP emulation.
        self.force_dsf = float(force_dsf) if force_dsf else None
        # `attempt` is the retry index owned by the caller (NativeHost). A retry
        # MUST get a different profile directory: a Chromium profile is locked by
        # a SingletonLock, and when a stale process still owns it a new launch
        # hands the URL off to that dead process and exits immediately - which is
        # exactly the "chrome hangs / never opens" report.
        self.attempt = max(0, int(attempt or 0))
        self.profile_dir = self.profile_for(self.attempt)
        self.gpu = (os.environ.get("LEGALYZE_DISABLE_GPU") != "1") if gpu is None else gpu
        self.args = []

    def profile_for(self, attempt):
        """Profile directory used by retry `attempt` (0 = the main profile)."""
        attempt = max(0, int(attempt or 0))
        if attempt == 0:
            return self.profile_base
        return Path("%s-retry%d" % (self.profile_base, attempt))

    # -------------------------------------------------------------- lifecycle
    def _event(self, name, **data):
        try:
            self.report(name, **data)
        except Exception:
            diag.exception("native_browser.report")

    def launch(self, candidate, width=453, height=735, staging=(0, 0)):
        """Spawn the browser for `candidate`. Each retry gets a fresh profile so
        a stale singleton can never hand the launch off to a dead process."""
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self.exe = candidate.path
        self.kind = candidate.kind
        self.profile_dir = self.profile_for(self.attempt)
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            diag.exception("native_browser.profile_mkdir")
        # NATIVE 67 % zoom, written BEFORE the launch: Chrome starts the page at
        # `physical / 0.667` CSS px and scales it down, exactly like Ctrl+'-'.
        # Without it the chat is laid out wider than the window and its text is
        # pushed outside the right edge (the v8/v9 report).
        # Skipped when the zoom is forced by --force-device-scale-factor, so the
        # two mechanisms can never stack.
        if self.force_dsf is None:
            try:
                write_zoom(self.profile_dir)
            except Exception:
                diag.exception("native_browser.zoom_prefs")
        try:
            write_permissions(self.profile_dir)
        except Exception:
            diag.exception("native_browser.permissions")

        args = build_args(self.exe, self.port, self.profile_dir, self.url,
                          width, height, self.gpu, staging, dsf=self.force_dsf)
        self.args = list(args)
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE: findable, not stealing focus
        self._event("browser.launch", exe=self.exe, kind=self.kind, port=self.port,
                    profile=str(self.profile_dir), gpu=self.gpu,
                    width=width, height=height, attempt=self.attempt)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL,
                                     creationflags=creationflags,
                                     startupinfo=startupinfo)
        self._event("browser.spawned", pid=self.proc.pid, port=self.port)
        return self.proc

    def http_json(self, path, timeout=1.0):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.load(response)

    def wait_devtools(self, timeout=30.0):
        """Poll /json/version and /json/list. Returns (version, targets)."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                self._event("browser.exited_early", code=self.proc.returncode)
                return None, None
            try:
                version = self.http_json("/json/version", timeout=1.0)
                targets = self.http_json("/json/list", timeout=1.0)
                self._event("browser.devtools_ready", browser=version.get("Browser"),
                            protocol=version.get("Protocol-Version"),
                            targets=len(targets or []))
                return version, targets
            except Exception as exc:
                last = str(exc)
                time.sleep(0.2)
        self._event("browser.devtools_timeout", last=str(last)[:200], timeout=timeout)
        return None, None

    def probe(self):
        """Single non-blocking DevTools poll: ('waiting'|'ready'|'exited', version, targets)."""
        if self.proc is not None and self.proc.poll() is not None:
            return "exited", None, None
        try:
            version = self.http_json("/json/version", timeout=0.8)
            targets = self.http_json("/json/list", timeout=0.8)
            return "ready", version, targets
        except Exception:
            return "waiting", None, None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        """Остановить браузер вместе со ВСЕМ деревом процессов.

        Обычный `terminate()` убивает только корневой процесс: дочерние
        (renderer/GPU) могли пережить перезапуск и оставить своё окно — именно
        так в Alt+Tab появлялись «много браузеров размером с GUI».
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            win32_embed.kill_tree(int(proc.pid))
        except Exception:
            diag.exception("native_browser.kill_tree")
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            diag.exception("native_browser.terminate")
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                diag.exception("native_browser.kill")
        self._event("browser.closed", port=self.port)


# ------------------------------------------------------------ target choice --
def _host_of(url):
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return ""


def is_blocked_target(url):
    """Consent / sign-in / error surfaces: never automate these."""
    raw = (url or "").lower()
    host = _host_of(url)
    return any(token in raw or token == host for token in NON_APP_HOSTS)


def is_app_target(url):
    host = _host_of(url)
    raw = (url or "").lower()
    return (any(token in host for token in ("gemini.google.com", "aistudio.google.com",
                                            "bard.google.com"))
            or "/ai" in raw or "google.com/ai" in raw)


def choose_target(targets):
    """Pick the chat page, never a consent/account wall.

    The old picker took the first `google.com` page — `consent.google.com`
    matched, so automation attached to the wall and waited for a chat that was
    never there ("chrome starts, hangs, does nothing").

    Order: the chat page -> any ordinary page -> a consent/account wall (last
    resort, so the clicker can dismiss it and the redirect can happen) -> a
    blank/error page is never chosen (keeps the caller polling).
    """
    pages = [t for t in (targets or [])
             if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    real = [p for p in pages if (p.get("url") or "").lower().startswith(("http://", "https://"))]
    blocked = [p for p in real if is_blocked_target(p["url"])]
    neutral = [p for p in real if not is_blocked_target(p["url"])]
    app = [p for p in neutral if is_app_target(p["url"])]
    if app:
        chosen = app[0]
    elif neutral:
        chosen = neutral[0]
    elif blocked:
        chosen = blocked[0]
    elif real:
        chosen = real[0]
    else:
        chosen = None
    diag.event("browser.target_chosen",
               pages=[(p.get("url") or "")[:120] for p in pages][:8],
               chosen=(chosen.get("url") if chosen else "")[:120],
               wall_only=bool(blocked and not neutral))
    return chosen


def on_consent_page(url):
    host = _host_of(url)
    return host.endswith("consent.google.com") or "consent.google" in (url or "")


# --------------------------------------------------------------------- zoom --
def zoom_level(factor=ZOOM):
    """Chrome page-zoom level for `factor` (factor = 1.2 ** level)."""
    return math.log(factor) / math.log(1.2)


def _chrome_timestamp():
    """`last_modified` in Chrome's own time base (microseconds since 1601)."""
    import datetime
    epoch = datetime.datetime(1601, 1, 1)
    delta = datetime.datetime.utcnow() - epoch
    return str(int(delta.total_seconds() * 1000000))


def write_zoom(profile_dir, level=None, hosts=ZOOM_HOSTS):
    """Write the NATIVE 67 % page zoom into the profile before the launch.

    This is the same mechanism as pressing Ctrl+'-' down to 67 %: Chrome lays
    the page out at `physical / 0.667` CSS pixels and then scales the whole
    rendering down to fit the window. Nothing is clipped and nothing shifts to
    the right — the page is simply smaller, exactly as in the working builds.

    Written into `<profile>/Default/Preferences` (and `<profile>/Preferences`
    for older layouts), merged with whatever Chrome left there, atomically.
    """
    level = ZOOM_LEVEL if level is None else float(level)
    base = Path(profile_dir)
    written = []
    stamp = _chrome_timestamp()
    for sub in ("Default", ""):
        target_dir = base / sub if sub else base
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = target_dir / "Preferences"
            prefs = {}
            if prefs_file.is_file():
                try:
                    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    diag.exception("native_browser.prefs_read")
                    prefs = {}
            if not isinstance(prefs, dict):
                prefs = {}
            partition = prefs.setdefault("partition", {})
            if not isinstance(partition, dict):
                partition = {}
                prefs["partition"] = partition
            partition["default_zoom_level"] = {"x": level}
            per_host = partition.setdefault("per_host_zoom_levels", {})
            if not isinstance(per_host, dict):
                per_host = {}
                partition["per_host_zoom_levels"] = per_host
            bucket = per_host.setdefault("x", {})
            if not isinstance(bucket, dict):
                bucket = {}
                per_host["x"] = bucket
            for host in hosts:
                bucket[host] = {"zoom_level": level, "last_modified": stamp}
            tmp = prefs_file.with_name(prefs_file.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(prefs_file)
            written.append(str(prefs_file))
        except Exception:
            diag.exception("native_browser.write_zoom")
    diag.event("browser.zoom_prefs", level=round(level, 6), files=written)
    return bool(written)


def write_permissions(profile_dir, hosts=ZOOM_HOSTS, level=1):
    """Разрешить микрофон в самом профиле (без всплывашки и без флагов).

    Chrome показывает «вы используете неподдерживаемый флаг командной строки»
    для `--use-fake-ui-for-media-stream`, поэтому права выдаются так же, как их
    выдаёт сам браузер после нажатия «Разрешить» — через content settings
    профиля (`setting: 1` = allow).
    """
    base = Path(profile_dir)
    stamp = _chrome_timestamp()
    written = []
    for sub in ("Default", ""):
        target_dir = base / sub if sub else base
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            prefs_file = target_dir / "Preferences"
            prefs = {}
            if prefs_file.is_file():
                try:
                    prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
                except Exception:
                    diag.exception("native_browser.prefs_read")
                    prefs = {}
            if not isinstance(prefs, dict):
                prefs = {}
            profile = prefs.setdefault("profile", {})
            if not isinstance(profile, dict):
                profile = {}
                prefs["profile"] = profile
            settings = profile.setdefault("content_settings", {})
            if not isinstance(settings, dict):
                settings = {}
                profile["content_settings"] = settings
            exceptions = settings.setdefault("exceptions", {})
            if not isinstance(exceptions, dict):
                exceptions = {}
                settings["exceptions"] = exceptions
            for kind in ("media_stream_mic", "media_stream_camera"):
                bucket = exceptions.setdefault(kind, {})
                if not isinstance(bucket, dict):
                    bucket = {}
                    exceptions[kind] = bucket
                for host in hosts:
                    for pattern in ("https://%s:443,*" % host,
                                    "https://[*.]%s:443,*" % host,
                                    "https://%s,*" % host):
                        bucket[pattern] = {"last_modified": stamp, "setting": int(level),
                                           "secondary_pattern": "*"}
            tmp = prefs_file.with_name(prefs_file.name + ".tmp")
            tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(prefs_file)
            written.append(str(prefs_file))
        except Exception:
            diag.exception("native_browser.write_permissions")
    diag.event("browser.permissions", files=written)
    return bool(written)


def css_size_for(logical_w, logical_h, zoom=ZOOM):
    """CSS size of a page zoomed to `zoom` inside a window of `logical` DIPs.

    `logical` is DPI-independent (Qt works in DIP), so the layout is the SAME
    at 100 %, 125 % and 150 %: 453 DIP / 0.667 = 680 CSS px everywhere.
    """
    return (max(320, int(round(logical_w / zoom))),
            max(320, int(round(logical_h / zoom))))


def page_metrics(page):
    """/innerWidth, innerHeight, devicePixelRatio, scrollWidth/ of the page."""
    probe = ("JSON.stringify({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,"
             " sw: document.documentElement ? document.documentElement.scrollWidth : 0})")
    try:
        value = page.eval(probe, timeout=3).get("value")
        data = json.loads(value or "{}")
    except Exception:
        diag.exception("native_browser.metrics")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "innerWidth": int(data.get("w") or 0),
        "innerHeight": int(data.get("h") or 0),
        "dpr": float(data.get("dpr") or 0.0),
        "scrollWidth": int(data.get("sw") or 0),
    }


def zoom_ok(page, logical_w, logical_h, physical_w=None, physical_h=None,
            zoom=ZOOM, tol_w=28, tol_h=48):
    """True when the page is REALLY zoomed to `zoom` and really fits.

    v8 forced the CSS viewport to 680 px but never scaled the rendering down:
    the chat was laid out wider than the window, so its text ended up outside
    the right edge ("весь текст съехал вправо"). Two things are checked here:
    * the layout is `logical / zoom` CSS px — not wider, not narrower;
    * `innerWidth * dpr` equals the window's PHYSICAL width, i.e. the layout is
      mapped onto the window instead of overflowing it.
    """
    css_w, css_h = css_size_for(logical_w, logical_h, zoom)
    metrics = page_metrics(page)
    if not metrics or metrics["innerWidth"] <= 0:
        return False, metrics
    if abs(metrics["innerWidth"] - css_w) > tol_w:
        return False, metrics
    if abs(metrics["innerHeight"] - css_h) > tol_h:
        return False, metrics
    if physical_w:
        rendered = metrics["innerWidth"] * metrics["dpr"]
        if abs(rendered - physical_w) > max(6.0, 0.03 * physical_w):
            return False, metrics
    return True, metrics


def apply_viewport(page, logical_w, logical_h, physical_w=None, physical_h=None,
                   zoom=ZOOM, dsf=None, fit_window=False):
    """One `Emulation.setDeviceMetricsOverride` (low level).

    `deviceScaleFactor = physical / css` makes the emulated device exactly as
    many device pixels as the window has, so the page is laid out at
    `logical / zoom` CSS px AND scaled to fit — no clipping, no right shift.
    """
    css_w, css_h = css_size_for(logical_w, logical_h, zoom)
    physical_w = physical_w or logical_w
    params = {
        "width": css_w,
        "height": css_h,
        "deviceScaleFactor": (physical_w / css_w) if dsf is None else float(dsf),
        "mobile": False,
        "fitWindow": bool(fit_window),
    }
    try:
        page.send("Emulation.setDeviceMetricsOverride", params, timeout=5)
    except Exception:
        diag.exception("native_browser.viewport")
        return False
    diag.event("browser.viewport", css=(css_w, css_h),
               logical=(logical_w, logical_h), physical=(physical_w, physical_h),
               dsf=round(params["deviceScaleFactor"], 4), fit=bool(fit_window))
    return True


def apply_zoom(page, logical_w, logical_h, physical_w=None, physical_h=None,
               zoom=ZOOM, settle=0.25, hooks=None):
    """Гарантировать зум 67 %: всё измеряется, ничего не предполагается.

    Лестница (каждая ступень проверяется измерением `zoom_ok`):

    1. `native`           — зум уже применился из Preferences профиля;
    2. `emulation-dsf`    — CDP: `deviceScaleFactor = physical / css`
                            (эмулируемое устройство точно равно окну);
    3. `emulation-dsf-fit`— то же + `fitWindow`;
    4. `emulation-fit`    — `dsf = 1` + `fitWindow`;
    5. `keyboard`         — Ctrl+'-' / Ctrl+'=' настоящим браузером;
    6. `restart-dsf`      — перезапуск с `--force-device-scale-factor`
                            (последняя ступень, один раз за сессию).

    Ступени 5 и 6 выполняются через `hooks` (фокус окна и перезапуск живут в
    GUI-потоке). Что реально сработало — видно в логе `browser.zoom`.
    """
    css_w, css_h = css_size_for(logical_w, logical_h, zoom)
    physical_w = physical_w or logical_w
    physical_h = physical_h or logical_h
    hooks = hooks or {}

    def done(ok, source, metrics):
        if metrics:
            diag.event("browser.zoom", source=source, ok=bool(ok), css=(css_w, css_h),
                       logical=(logical_w, logical_h), physical=(physical_w, physical_h),
                       zoom=round(zoom_factor(metrics, logical_w) or 0.0, 4),
                       overflow=max(0, int(metrics.get("scrollWidth") or 0)
                                    - int(metrics.get("innerWidth") or 0)),
                       **metrics)
        else:
            diag.event("browser.zoom", source=source, ok=False, css=(css_w, css_h))
        return {"ok": bool(ok), "source": source, "metrics": metrics}

    ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h, zoom)
    if ok:
        return done(True, "native", metrics)

    attempts = (
        ("emulation-dsf", {}),
        ("emulation-dsf-fit", {"fit_window": True}),
        ("emulation-fit", {"dsf": 1.0, "fit_window": True}),
    )
    for name, kwargs in attempts:
        if not apply_viewport(page, logical_w, logical_h, physical_w, physical_h,
                              zoom, **kwargs):
            continue
        time.sleep(settle)
        ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h, zoom)
        if ok:
            return done(True, name, metrics)

    source = "emulation-failed"
    press = hooks.get("press")
    if press:
        ok, source, metrics = calibrate_by_keyboard(
            page, logical_w, logical_h, physical_w, physical_h, press, zoom)
        if ok:
            return done(True, source, metrics)

    restart = hooks.get("restart")
    if restart:
        try:
            if restart(physical_w / css_w):
                return done(False, "restart-dsf", metrics)
        except Exception:
            diag.exception("native_browser.zoom_restart")

    return done(False, source, metrics)


def zoom_factor(metrics, logical_w):
    """Эффективный зум страницы: factor = логический размер (DIP) / innerWidth.

    1.0 — зума нет (именно так и выглядит «текст уехал вправо» на широком
    сайте), 0.667 — всё верно, 0.5 — слишком мелко.
    """
    if not metrics or not metrics.get("innerWidth") or not logical_w:
        return None
    return float(logical_w) / float(metrics["innerWidth"])


def calibrate_by_keyboard(page, logical_w, logical_h, physical_w, physical_h,
                          press, zoom=ZOOM, max_steps=8, settle=0.3):
    """Ctrl+'-' / Ctrl+'=' — настоящий зум браузера, как его делает человек.

    Единственный механизм, который масштабирует отрисовку в ЛЮБОЙ сборке
    Chrome: настройки профиля могут игнорироваться, `deviceScaleFactor` в
    `Emulation` может прижиматься к 1, а Accelerator Ctrl+'-' обрабатывается
    самим браузером. Каждое нажатие сопровождается ИЗМЕРЕНИЕМ.
    """
    metrics = page_metrics(page)
    for _step in range(max_steps):
        factor = zoom_factor(metrics, logical_w)
        if factor is None or abs(factor - zoom) <= 0.03:
            break
        direction = "-" if factor > zoom else "+"
        if not press(direction):
            return False, "keyboard-press-failed", metrics
        time.sleep(settle)
        metrics = page_metrics(page)
        after = zoom_factor(metrics, logical_w)
        if after is not None and abs(after - factor) < 0.005:
            # Нажатие ничего не меняет (браузер его не получает) — жать дальше
            # бессмысленно и опасно: можно уйти на 25 %.
            break
    ok, metrics = zoom_ok(page, logical_w, logical_h, physical_w, physical_h, zoom)
    return ok, ("keyboard" if ok else "keyboard-failed"), metrics


def verify_viewport(page, logical_w, logical_h, zoom=ZOOM):
    """Только ширина раскладки (БЕЗ проверки вписывания — см. zoom_ok).

    Оставлена как раз потому, что одна эта проверка и пропустила дефект v8:
    при CSS 680 px и dpr 1 она отвечает «ок», хотя страница шире окна и её
    текст уехал за правую границу. Настоящая проверка — `zoom_ok`.
    """
    css_w, css_h = css_size_for(logical_w, logical_h, zoom)
    metrics = page_metrics(page)
    if not metrics:
        return False
    return abs(metrics["innerWidth"] - css_w) <= 28


def grant_microphone(browser, page):
    """Pre-grant the microphone so the page's own recorder never hits a prompt.

    `Browser.grantPermissions` is a real Chrome feature (it failed with -32000
    in QtWebEngine, which is one more reason the embedded engine cannot do the
    page's native voice input).
    """
    origins = set()
    try:
        origin = page.eval("location.origin", timeout=5).get("value")
        if origin and origin != "null":
            origins.add(origin)
    except Exception:
        diag.exception("native_browser.origin")
    origins.update(["https://google.com", "https://www.google.com",
                    "https://gemini.google.com", "https://aistudio.google.com"])
    context_id = None
    try:
        info = browser.send("Target.getBrowserContexts", timeout=5)
        ids = info.get("browserContextIds") or []
        if ids:
            context_id = ids[0]
    except Exception:
        diag.exception("native_browser.contexts")
    granted = []
    for origin in origins:
        params = {"origin": origin, "permissions": ["audioCapture", "videoCapture"]}
        if context_id:
            params["browserContextId"] = context_id
        try:
            browser.send("Browser.grantPermissions", params, timeout=5)
            granted.append(origin)
        except Exception:
            diag.exception("native_browser.grant")
    # Belt and braces: the modern, origin-scoped method.
    try:
        browser.send("Browser.setPermission", {
            "origin": (sorted(origins)[0] if origins else "https://google.com"),
            "permission": {"name": "audioCapture"},
            "setting": "granted",
        }, timeout=5)
    except Exception:
        diag.exception("native_browser.set_permission")
    diag.event("browser.mic_granted", origins=granted, context=bool(context_id))
    return granted


def speech_surface(page):
    """Report whether this build can actually do the page's voice input."""
    probe = ("JSON.stringify({sr: !!(window.SpeechRecognition||window.webkitSpeechRecognition),"
             " gum: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),"
             " perm: !!(navigator.permissions && navigator.permissions.query)})")
    try:
        value = page.eval(probe, timeout=5).get("value")
        data = json.loads(value or "{}")
    except Exception:
        diag.exception("native_browser.speech_surface")
        data = {}
    diag.event("browser.speech_surface", **data)
    return data


def copy_profile_defaults(profile_dir):
    """Nothing to seed today; keeps the profile path explicit for the caller."""
    return Path(profile_dir)
