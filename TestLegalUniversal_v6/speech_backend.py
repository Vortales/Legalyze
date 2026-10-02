"""Offline dictation for the in-page speech shim via Windows System.Speech.

The embedded QtWebEngine has no Web Speech API backend (recognition in real
Chrome is a Google service tied to Chrome's proprietary keys). The site hides
its microphone button when `SpeechRecognition` is absent. `web_compat.py`
installs a faithful SpeechRecognition shim; this module backs it with real
speech-to-text on every Windows 10/11 machine: a hidden PowerShell host runs
the inbox .NET `System.Speech` dictation engine and streams JSON events.

No third-party Python packages and no cloud calls: PowerShell 5.1 and
System.Speech ship with Windows. Audio stays on the machine. Recognition
quality/availability depends on the installed Windows speech language
(Settings > Time & language > Speech).

v6 hardening (real log 20260930): the PowerShell host must load System.Speech
explicitly, must receive a SPECIFIC culture (a neutral tag like "ru" makes the
recognizer constructor throw) and must never touch Windows.Forms (that type is
not loaded; its absence under `$ErrorActionPreference='Stop'` killed the host
milliseconds after "started"). start() is idempotent for the same language —
page-side restart storms no longer slaughter a live recognition session — and
every host exit is logged with its stderr tail.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import diagnostics as diag

# SpeechRecognition DOMException-compatible error names.
ERROR_NAMES = ("aborted", "audio-capture", "network", "no-speech",
               "not-allowed", "service-not-allowed", "language-not-supported")

# Neutral/short language tags -> specific cultures System.Speech can load.
LANG_ALIASES = {
    "ru": "ru-RU", "en": "en-US", "de": "de-DE", "fr": "fr-FR",
    "es": "es-ES", "uk": "uk-UA", "it": "it-IT", "pt": "pt-PT",
    "tr": "tr-TR", "pl": "pl-PL", "kk": "kk-KZ", "be": "be-BY",
}


def norm_lang(lang):
    """Return a SPECIFIC culture tag (ru-RU): System.Speech rejects "ru"."""
    raw = str(lang or "").strip().replace("_", "-")[:16]
    if not raw:
        return "ru-RU"
    low = raw.lower()
    if low in LANG_ALIASES:
        return LANG_ALIASES[low]
    parts = raw.split("-")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0].lower() + "-" + parts[1].upper()
    return raw


PS_SCRIPT = r"""
param([string]$Lang = 'ru-RU')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# System.Speech is NOT auto-loaded in -NonInteractive hosts: load it or the
# recognizer constructor throws and the host dies right after "started".
try { Add-Type -AssemblyName System.Speech } catch { }

function Emit([hashtable]$o) {
  [Console]::Out.WriteLine((ConvertTo-Json -InputObject $o -Compress))
  [Console]::Out.Flush()
}

# The recognizer requires a SPECIFIC culture: a neutral culture (ru, en)
# throws "Culture name ... is not supported". Fall back to the first specific
# culture of the same language, then to ru-RU.
$cult = $null
try { $cult = [System.Globalization.CultureInfo]::GetCultureInfo($Lang) } catch { }
if ($null -eq $cult -or $cult.IsNeutralCulture) {
  try {
    $two = $Lang.Substring(0, 2).ToLowerInvariant()
    foreach ($c in [System.Globalization.CultureInfo]::GetCultures(
        [System.Globalization.CultureTypes]::SpecificCultures)) {
      if ($c.TwoLetterISOLanguageName.ToLowerInvariant() -eq $two) { $cult = $c; break }
    }
  } catch { }
}
if ($null -eq $cult -or $cult.IsNeutralCulture) {
  try { $cult = [System.Globalization.CultureInfo]::GetCultureInfo('ru-RU') } catch { }
}

try {
  $rc = New-Object System.Speech.Recognition.SpeechRecognitionEngine($cult)
} catch {
  Emit @{ type = 'error'; code = 'language-not-supported' }; exit 2
}

try { $rc.SetInputToDefaultAudioDevice() } catch {
  Emit @{ type = 'error'; code = 'not-allowed' }; exit 3
}

try {
  $grammar = New-Object System.Speech.Recognition.DictationGrammar
  $grammar.Name = 'dictation'
  $rc.LoadGrammar($grammar)
} catch {
  Emit @{ type = 'error'; code = 'language-not-supported' }; exit 2
}

$rc.add_SpeechHypothesized({ param($s, $e)
  Emit @{ type = 'hypothesis'; text = [string]$e.Result.Text }
})
$rc.add_SpeechRecognized({ param($s, $e)
  Emit @{ type = 'result'; text = [string]$e.Result.Text; confidence = [double]$e.Result.Confidence }
})
$rc.add_SpeechRecognitionRejected({ param($s, $e)
  Emit @{ type = 'rejected'; text = [string]$e.Result.Text }
})
$rc.add_AudioStateChanged({ param($s, $e)
  if ($e.AudioState -eq [System.Speech.Recognition.AudioState]::Speech) {
    Emit @{ type = 'speechstart' }
  } elseif ($e.AudioState -eq [System.Speech.Recognition.AudioState]::Silence) {
    Emit @{ type = 'speechend' }
  }
})

Emit @{ type = 'started' }
try {
  $rc.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
} catch {
  Emit @{ type = 'error'; code = 'audio-capture' }; exit 4
}

# Keep the process alive while recognition events stream. A plain sleep loop:
# the Windows.Forms event pump is NOT loaded here, and calling it under
# $ErrorActionPreference='Stop' used to kill the host ~40 ms after "started"
# on machines where that assembly was not in the load context.
try {
  while ($true) {
    Start-Sleep -Milliseconds 200
  }
} catch {
  Emit @{ type = 'error'; code = 'audio-capture' }
  exit 5
}
"""

# One dictation session per process. stop()/abort() terminate the host; the
# results already streamed to the page are kept by the JS shim.


class DictationEngine:
    """Spawns the PowerShell host and translates its events for the bridge.

    Each start() opens a new "generation"; events from older hosts are dropped
    so a fast stop/start can never interleave two sessions. A start() while a
    host for the SAME language is already running is a no-op that only
    re-announces "started" (the page freely restarts its recognizers; the
    engine must keep the one live host).
    """

    def __init__(self, on_event):
        self.on_event = on_event  # callable(type: str, data: dict)
        self._proc = None
        self._gen = 0
        self._got_result = False
        self._ended = True
        self._lock = threading.Lock()
        self._lang = None
        self._sid = ""
        self._speech_seen = False

    # ----------------------------------------------------------------- state

    @property
    def active(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def speech_seen(self):
        """True once the current host produced any speech activity."""
        with self._lock:
            return self._speech_seen

    @property
    def lang(self):
        with self._lock:
            return self._lang

    # --------------------------------------------------------------- control

    def start(self, lang="ru-RU", sid=""):
        norm = norm_lang(lang)
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            same = bool(running and self._lang == norm)
            if same:
                self._sid = sid or self._sid
                gen = self._gen
        if same:
            # Идемпотентный старт: повторный start (гонки страницы, эхо
            # хоткея, ретраи сайта) не убивает живый хост распознавания.
            diag.event("speech.start_ignored", lang=norm)
            self._event(gen, "started", {})
            return
        self.stop()
        if sys.platform != "win32":
            self._event(self._gen, "error", {"code": "service-not-allowed"})
            self._event(self._gen, "end", {})
            return
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._got_result = False
            self._ended = False
            self._lang = norm
            self._sid = str(sid or "")
            self._speech_seen = False
        script = self._write_script()
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-Lang", norm],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=creationflags,
            )
        except Exception:
            diag.exception("speech.spawn")
            with self._lock:
                self._ended = True
            self._event(gen, "error", {"code": "service-not-allowed"})
            self._event(gen, "end", {})
            return
        with self._lock:
            self._proc = proc
        threading.Thread(target=self._read_loop, args=(gen, proc),
                         name="SpeechPS%d" % gen, daemon=True).start()
        threading.Thread(target=self._watch_exit, args=(gen, proc),
                         name="SpeechPSX%d" % gen, daemon=True).start()
        diag.event("speech.started", lang=norm, session=str(sid)[:16])

    def stop(self, sid=None):
        """Stop the host. `sid` from a stale page session is ignored, so a
        superseded recognizer can never kill the live one. No sid (or an empty
        one) forces the stop."""
        with self._lock:
            current = self._sid
        if sid and current and str(sid) != current:
            diag.event("speech.stop_ignored", sid=str(sid)[:24], current=str(current)[:24])
            return
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                diag.exception("speech.terminate")
        self._finish(self._gen)

    def shutdown(self):
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                diag.exception("speech.terminate")
        with self._lock:
            self._ended = True

    # ---------------------------------------------------------------- intake

    def _read_loop(self, gen, proc):
        got = False
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    diag.event("speech.bad_line")
                    continue
                if self._handle(gen, msg) == "result":
                    got = True
        except Exception:
            diag.exception("speech.read")
        finally:
            with self._lock:
                if gen == self._gen:
                    self._got_result = self._got_result or got
            self._finish(gen, force_error=not got)

    def _watch_exit(self, gen, proc):
        """Report host deaths that used to vanish silently (stderr=DEVNULL)."""
        try:
            err = b""
            try:
                if proc.stderr is not None:
                    err = proc.stderr.read()[-800:]
            except Exception:
                pass
            code = proc.wait()
            tail = err.decode("utf-8", "replace").strip().replace("\r", " ")
            tail = " ".join(tail.split())[:200]
            with self._lock:
                stale = gen != self._gen
            if not stale:
                diag.event("speech.host_exit", code=code, stderr=tail)
        except Exception:
            diag.exception("speech.host_watch")

    def _handle(self, gen, msg) -> str:
        kind = str(msg.get("type") or "")
        if kind in ("speechstart", "hypothesis", "result", "rejected"):
            with self._lock:
                self._speech_seen = True
        if kind == "started":
            self._event(gen, "started", {})
        elif kind == "hypothesis":
            self._event(gen, "hypothesis", {"text": str(msg.get("text") or "")})
        elif kind == "result":
            self._event(gen, "result", {"text": str(msg.get("text") or ""),
                                        "confidence": float(msg.get("confidence") or 0.0)})
        elif kind == "rejected":
            self._event(gen, "rejected", {"text": str(msg.get("text") or "")})
        elif kind in ("speechstart", "speechend"):
            self._event(gen, kind, {})
        elif kind == "error":
            code = str(msg.get("code") or "network")
            if code not in ERROR_NAMES:
                code = "network"
            self._event(gen, "error", {"code": code})
        else:
            diag.event("speech.unknown_event", kind=kind[:32])
        return kind

    def _finish(self, gen, force_error=False):
        with self._lock:
            if gen != self._gen or self._ended:
                return
            self._ended = True
            got = self._got_result
        if force_error and not got:
            self._event(gen, "error", {"code": "no-speech"})
        self._event(gen, "end", {})

    def _event(self, gen, kind, data):
        with self._lock:
            stale = gen != self._gen
        if stale:
            return
        try:
            self.on_event(kind, data)
        except Exception:
            diag.exception("speech.event")

    # ---------------------------------------------------------------- script

    def _write_script(self) -> str:
        base = Path(os.environ.get("APPDATA") or Path.home()) / "Legalyze" / "runtime"
        payload = b"\xef\xbb\xbf" + PS_SCRIPT.encode("utf-8")
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / "dictation.ps1"
            path.write_bytes(payload)
            return str(path)
        except Exception:
            diag.exception("speech.script_write")
            import tempfile
            path = Path(tempfile.gettempdir()) / "legalyze-dictation.ps1"
            path.write_bytes(payload)
            return str(path)
