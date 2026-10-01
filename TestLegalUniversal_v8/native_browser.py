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
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import diagnostics as diag

DEFAULT_URL = "https://google.com/ai"
ZOOM = 2.0 / 3.0            # native 67 % window zoom, as in the working builds

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
               staging=(0, 0), extra=()):
    """Flags proven in `release/` plus the stability additions of this build."""
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--remote-debugging-address=127.0.0.1",
        "--user-data-dir=%s" % profile,
        "--app=%s" % url,
        # Microphone: no permission bubble, ever (see module docstring).
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream=disabled",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-window",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--noerrdialogs",
        "--disable-component-update",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=SystemTitlebar,CaptionButtons,Translate,OptimizationHints,"
        "MediaRouter,GlobalMediaControls,DialMediaRouteProvider,InProductHelp,"
        "MenuCommands,AcceptCHFrame",
        "--window-position=%d,%d" % (int(staging[0]), int(staging[1])),
        "--window-size=%d,%d" % (int(width), int(height)),
        "--log-file=%s" % (Path(profile).parent / "chromium.log"),
        "--enable-logging",
    ]
    if not gpu:
        args.append("--disable-gpu")
        args.append("--disable-gpu-compositing")
        args.append("--disable-software-rasterizer")
    args.extend(extra)
    return args


class NativeBrowser:
    """Owns the browser process, its DevTools port and (via win32_embed) its
    window. Knows nothing about Qt."""

    def __init__(self, url=DEFAULT_URL, profile_dir=None, app_dir=None,
                 report=None, gpu=None):
        self.url = url
        self.app_dir = Path(app_dir) if app_dir else Path.cwd()
        base = Path(profile_dir) if profile_dir else self.app_dir / "browser-profile"
        self.profile_base = Path(base)
        self.profile_dir = self.profile_base
        self.report = report or diag.event
        self.port = None
        self.proc = None
        self.exe = None
        self.kind = "unknown"
        self.attempt = 0
        self.gpu = (os.environ.get("LEGALYZE_DISABLE_GPU") != "1") if gpu is None else gpu

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
        self.attempt = 0 if self.exe != getattr(self, "_last_exe", None) else self.attempt + 1
        self._last_exe = self.exe
        self.profile_dir = self.profile_base if self.attempt == 0 else \
            Path("%s-%d" % (self.profile_base, self.attempt))
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            diag.exception("native_browser.profile_mkdir")

        args = build_args(self.exe, self.port, self.profile_dir, self.url,
                          width, height, self.gpu, staging)
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
        proc, self.proc = self.proc, None
        if proc is None:
            return
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


# ------------------------------------------------------------------ viewport
def apply_viewport(page, width, height, zoom=ZOOM):
    """Pin the CSS viewport so the layout is IDENTICAL on every machine.

    `width`/`height` are the window's logical size; the page is laid out at
    `width / zoom` CSS pixels and the browser scales that into the window. This
    is what removes the DPI lottery: at 100 %, 125 % or 150 % the chat renders
    the same narrow layout instead of the wide desktop variant (the old
    "just a wide ИИ label" report).
    """
    css_w = max(320, int(round(width / zoom)))
    css_h = max(320, int(round(height / zoom)))
    try:
        page.send("Emulation.setDeviceMetricsOverride", {
            "width": css_w, "height": css_h, "deviceScaleFactor": 1, "mobile": False,
        }, timeout=5)
        diag.event("browser.viewport", css=(css_w, css_h), window=(width, height),
                   zoom=round(zoom, 3))
        return True
    except Exception:
        diag.exception("native_browser.viewport")
        return False


def verify_viewport(page, width, height, zoom=ZOOM):
    """True when the page really reports the pinned viewport (metrics are reset
    by navigation, so this is re-checked after every load)."""
    try:
        value = page.eval("JSON.stringify([innerWidth, innerHeight, devicePixelRatio])",
                          timeout=3).get("value")
        data = json.loads(value or "[]")
    except Exception:
        return False
    if not data:
        return False
    css_w = max(320, int(round(width / zoom)))
    return abs(int(data[0]) - css_w) <= 24


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
