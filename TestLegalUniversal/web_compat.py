"""Early page-compatibility scripts for the embedded QtWebEngine.

The site hides its microphone button when the Web Speech API is missing. In
real Chrome `SpeechRecognition` is a Google cloud service tied to Chrome's
proprietary keys; QtWebEngine does not ship it. `SPEECH_SHIM_JS` defines a
faithful SpeechRecognition surface backed (through QWebChannel, which is not
subject to page CSP) by the Windows dictation engine in `speech_backend.py`,
so the site renders its microphone and dictation works for real.

`UA_BRAND_JS` makes JS-side client hints consistent with the Chrome-branded
UA we send over the network, so feature gating by browser brand behaves the
same as in Chrome. Both scripts run at document creation, before any site
script, and are purely additive.
"""

# ---------------------------------------------------------------------------
UA_BRAND_JS = r"""
(() => {
  try {
    const m = /Chrome\/(\d+)/.exec(navigator.userAgent || '');
    const major = m ? m[1] : '120';
    const current = navigator.userAgentData;
    const brands = (current && current.brands) ? current.brands.map(b => b.brand) : [];
    if (current && brands.indexOf('Google Chrome') !== -1) return;
    const list = [
      { brand: 'Chromium', version: major },
      { brand: 'Google Chrome', version: major },
      { brand: 'Not;A=Brand', version: '24' }
    ];
    const data = {
      brands: list,
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: (hints) => Promise.resolve({
        brands: list, mobile: false, platform: 'Windows',
        platformVersion: '15.0.0', architecture: 'x86', bitness: '64',
        model: '', uaFullVersion: major + '.0.0.0', fullVersionList: list,
        wow64: false
      }),
      toJSON() { return { brands: list, mobile: false, platform: 'Windows' }; }
    };
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => data, configurable: true
    });
    window.__legalyzeUaPatched = true;
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
SPEECH_SHIM_JS = r"""
(() => {
  try {
    if (window.SpeechRecognition || window.webkitSpeechRecognition) {
      window.__legalyzeSpeech = { mode: 'native' };
      return;
    }
    if (window.__legalyzeSpeechShimInstalled) return;
    window.__legalyzeSpeechShimInstalled = true;

    const waiters = [];
    const bridgeReady = (cb) => {
      if (window.__legalyzeSpeechBridge) { cb(window.__legalyzeSpeechBridge); return; }
      waiters.push(cb);
      setTimeout(() => {
        for (let i = 0; i < waiters.length; i++) {
          try { waiters[i](null); } catch (e) {}
        }
        waiters.length = 0;
      }, window.__legalyzeSpeechBridgeWait || 4000);
    };
    window.__legalyzeSpeechWaiters = waiters;

    class SpeechRecognitionAlternative {
      constructor(transcript, confidence) {
        this.transcript = transcript; this.confidence = confidence;
      }
    }
    class SpeechRecognitionResult extends Array {
      constructor(alternative, isFinal) {
        super(alternative);
        this.isFinal = isFinal;
      }
    }
    class SpeechRecognitionEvent {
      constructor(type, items, resultIndex) {
        this.type = type; this.resultIndex = resultIndex;
        this.results = items.map((it) =>
          new SpeechRecognitionResult(
            new SpeechRecognitionAlternative(it.transcript, it.confidence), it.final));
      }
    }
    class SpeechRecognitionErrorEvent {
      constructor(error) { this.type = 'error'; this.error = error; }
    }

    class SpeechRecognition {
      constructor() {
        this.lang = (navigator.language || 'ru-RU');
        this.continuous = false;
        this.interimResults = false;
        this.maxAlternatives = 1;
        this.serviceURI = '';
        this.grammars = null;
        this.onaudiostart = null; this.onaudioend = null;
        this.onspeechstart = null; this.onspeechend = null;
        this.onstart = null; this.onend = null;
        this.onresult = null; this.onerror = null; this.onnomatch = null;
        this._items = []; this._active = false; this._stopping = false;
      }

      _emit(name, ev) {
        const fn = this['on' + name];
        if (typeof fn === 'function') { try { fn.call(this, ev); } catch (e) {} }
      }

      _pushResult(text, isFinal, confidence) {
        const interimIndex = this._items.findIndex((it) => !it.final);
        const item = { transcript: text, confidence: confidence || 0.9, final: isFinal };
        if (interimIndex >= 0 && !isFinal) {
          this._items[interimIndex] = item;
          return interimIndex;
        }
        if (interimIndex >= 0 && isFinal) {
          this._items[interimIndex] = item;
          return interimIndex;
        }
        this._items.push(item);
        return this._items.length - 1;
      }

      _dispatchResult(text, isFinal, confidence) {
        if (!isFinal && !this.interimResults) return;
        const idx = this._pushResult(text, isFinal, confidence);
        this._emit('result', new SpeechRecognitionEvent('result', this._items, idx));
        if (isFinal && !this.continuous && !this._stopping) {
          this._stopping = true;
          bridgeReady((b) => { if (b) { try { b.stop(); } catch (e) {} } });
        }
      }

      start() {
        if (this._active) {
          const err = new Error('already started');
          err.name = 'InvalidStateError';
          throw err;
        }
        this._active = true;
        this._stopping = false;
        this._items = [];
        window.__legalyzeSpeechActive = this;
        bridgeReady((bridge) => {
          if (!bridge) {
            this._failed = true;
            this._emit('error', new SpeechRecognitionErrorEvent('network'));
            this._finish();
            return;
          }
          try {
            bridge.start(String(this.lang || 'ru-RU'), !!this.continuous, !!this.interimResults);
          } catch (e) {
            this._emit('error', new SpeechRecognitionErrorEvent('network'));
            this._finish();
          }
        });
      }

      stop() {
        if (!this._active) return;
        this._stopping = true;
        bridgeReady((b) => { if (b) { try { b.stop(); } catch (e) {} } });
      }

      abort() {
        if (!this._active) return;
        this._stopping = true;
        this._items = [];
        bridgeReady((b) => { if (b) { try { b.abort(); } catch (e) {} } });
        this._emit('error', new SpeechRecognitionErrorEvent('aborted'));
        this._finish();
      }

      _finish() {
        if (!this._active) return;
        this._active = false;
        if (window.__legalyzeSpeechActive === this) window.__legalyzeSpeechActive = null;
        this._emit('audioend', {});
        this._emit('end', {});
      }

      // ---- bridge events (called by the QWebChannel dispatch below) --------
      _bridgeEvent(kind, data) {
        if (!this._active) return;
        const d = data || {};
        if (kind === 'started') {
          this._emit('audiostart', {});
          this._emit('start', {});
        } else if (kind === 'speechstart') {
          this._emit('speechstart', {});
        } else if (kind === 'speechend') {
          this._emit('speechend', {});
        } else if (kind === 'hypothesis') {
          this._dispatchResult(String(d.text || ''), false, 0.0);
        } else if (kind === 'result') {
          this._dispatchResult(String(d.text || ''), true,
                               Number(d.confidence || 0.9));
        } else if (kind === 'rejected') {
          this._emit('nomatch', {});
        } else if (kind === 'error') {
          this._emit('error', new SpeechRecognitionErrorEvent(String(d.code || 'network')));
        } else if (kind === 'end') {
          this._finish();
        }
      }
    }

    window.SpeechRecognition = window.webkitSpeechRecognition = SpeechRecognition;
    window.__legalyzeSpeech = { mode: 'shim', SpeechRecognition };

    const dispatch = (kind, data) => {
      const inst = window.__legalyzeSpeechActive;
      if (inst) inst._bridgeEvent(kind, data || {});
    };
    window.__legalyzeSpeechDispatch = dispatch;

    bridgeReady((bridge) => {
      if (!bridge) return;
      bridge.started.connect(() => dispatch('started'));
      bridge.speechStart.connect(() => dispatch('speechstart'));
      bridge.speechEnd.connect(() => dispatch('speechend'));
      bridge.hypothesis.connect((text) => dispatch('hypothesis', { text: text }));
      bridge.result.connect((text, confidence) =>
        dispatch('result', { text: text, confidence: confidence }));
      bridge.rejected.connect(() => dispatch('rejected'));
      bridge.error.connect((code) => dispatch('error', { code: code }));
      bridge.ended.connect(() => dispatch('end'));
    });
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
# Appended after the Qt qwebchannel.js library source.
CHANNEL_INIT_JS = r"""
new QWebChannel(qt.webChannelTransport, function(channel) {
  try {
    window.__legalyzeSpeechBridge = channel.objects.legalyzeSpeech;
    const waiters = window.__legalyzeSpeechWaiters || [];
    window.__legalyzeSpeechWaiters = [];
    for (let i = 0; i < waiters.length; i++) {
      try { waiters[i](window.__legalyzeSpeechBridge); } catch (e) {}
    }
    try { window.__legalyzeSpeechBridge.reportCapabilities(JSON.stringify(window.__legalyzeSpeech || {})); } catch (e) {}
  } catch (e) {}
});
"""

# ---------------------------------------------------------------------------
CAPABILITY_JS = r"""(() => {
  const d = navigator.userAgentData;
  return {
    secure: window.isSecureContext === true,
    mediaDevices: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    mediaRecorder: typeof MediaRecorder !== 'undefined',
    speechNative: !!(window.SpeechRecognition || window.webkitSpeechRecognition) &&
                  !(window.__legalyzeSpeechShimInstalled),
    speechShim: window.__legalyzeSpeechShimInstalled === true,
    uaPatched: window.__legalyzeUaPatched === true,
    brands: (d && d.brands) ? d.brands.map((b) => b.brand + '/' + b.version).join(',') : ''
  };
})()"""
