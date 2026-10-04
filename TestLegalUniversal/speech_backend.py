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
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import diagnostics as diag

# One dictation session per process. stop()/abort() terminate the host; the
# results already streamed to the page are kept by the JS shim.
PS_SCRIPT = r"""
param([string]$Lang = 'ru-RU')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Emit([hashtable]$o) {
  [Console]::Out.WriteLine((ConvertTo-Json -InputObject $o -Compress))
  [Console]::Out.Flush()
}

$cult = $null
try { $cult = [System.Globalization.CultureInfo]::GetCultureInfo($Lang) } catch { }
if ($null -eq $cult) { Emit @{ type = 'error'; code = 'language-not-supported' }; exit 2 }

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

while ($true) {
  [System.Windows.Forms.Application]::DoEvents()
  Start-Sleep -Milliseconds 40
}
"""

# SpeechRecognition DOMException-compatible error names.
ERROR_NAMES = ("aborted", "audio-capture", "network", "no-speech",
               "not-allowed", "service-not-allowed", "language-not-supported")


class DictationEngine:
    """Spawns the PowerShell host and translates its events for the bridge.

    Each start() opens a new "generation"; events from older hosts are dropped
    so a fast stop/start can never interleave two sessions.
    """

    def __init__(self, on_event):
        self.on_event = on_event  # callable(type: str, data: dict)
        self._proc = None
        self._gen = 0
        self._got_result = False
        self._ended = True
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- state

    @property
    def active(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # --------------------------------------------------------------- control

    def start(self, lang: str):
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
        script = self._write_script()
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
                 "-ExecutionPolicy", "Bypass", "-File", script,
                 "-Lang", (lang or "ru-RU")[:16]],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=creationflags,
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
        diag.event("speech.started", lang=lang)

    def stop(self):
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

    def _handle(self, gen, msg) -> str:
        kind = str(msg.get("type") or "")
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
