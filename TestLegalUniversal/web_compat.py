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
# Accepted-consent clicker: Google shows cookie/consent cards on a fresh
# profile. Hiding them without accepting leaves the page half-blocked (the
# first-ever run then "spins forever" on a query). Only exact accept labels
# inside consent surfaces are clicked.
CONSENT_AUTOCLICK_JS = r"""
(() => {
  try {
    if (window.__legalyzeConsentInstalled) return;
    window.__legalyzeConsentInstalled = true;
    const ACCEPT = /^(принять все|принять|accept all|accept|i agree|agree|согласен|согласиться|подтвердить)$/i;
    const SURFACE = 'div[role="dialog"], .qEn1od, form[action*="consent"], div.lJwFBd, div[data-consent], div.tDoycf';
    const clickAccepts = () => {
      try {
        if (!document.body) return 0;
        const onConsentHost = /(^|\.)consent\.google\.com$/.test(location.hostname || '');
        let clicked = 0;
        const nodes = document.querySelectorAll('button, input[type="submit"], div[role="button"]');
        for (const el of nodes) {
          const label = String(el.getAttribute('aria-label') || el.innerText || el.value || '').trim();
          if (!ACCEPT.test(label)) continue;
          if (!onConsentHost && !(el.closest && el.closest(SURFACE))) continue;
          try { el.click(); clicked++; } catch (e) {}
        }
        return clicked;
      } catch (e) { return 0; }
    };
    window.__legalyzeConsent = clickAccepts;
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      clickAccepts();
      if (tries >= 30) clearInterval(timer);
    }, 3000);
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
# Minimal QWebChannel client (fallback when the Qt resource copy of
# qwebchannel.js is unavailable). Mirrors the message protocol of the official
# Qt client: init/handshake (type 3/10), invokeMethod (6), connectToSignal (7),
# signal delivery (1). Covered by tests against a protocol-accurate mock.
QWEBCHANNEL_LITE_JS = r"""
window.QWebChannel = window.QWebChannel || function(transport, initCallback) {
  if (!transport || typeof transport.send !== 'function') {
    console.error('QWebChannel: invalid transport');
    return;
  }
  const T = {signal: 1, propertyUpdate: 2, init: 3, idle: 4, invokeMethod: 6,
             connectToSignal: 7, disconnectFromSignal: 8, response: 10};
  const channel = this;
  this.objects = {};
  let execId = 0;
  const execCallbacks = {};
  const send = (data) => { try { transport.send(JSON.stringify(data)); } catch (e) {} };
  const exec = (data, callback) => {
    if (callback) { data.id = execId++; execCallbacks[data.id] = callback; }
    send(data);
  };
  transport.onmessage = function(message) {
    let data = message && message.data;
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch (e) { return; } }
    if (!data) return;
    if (data.type === T.response) {
      const cb = execCallbacks[data.id];
      delete execCallbacks[data.id];
      if (cb) { try { cb(data.data); } catch (e) {} }
    } else if (data.type === T.signal) {
      const obj = channel.objects[data.object];
      if (obj && obj.__emit) obj.__emit(data.signal, data.args);
    } else if (data.type === T.propertyUpdate) {
      exec({type: T.idle});
    }
  };

  function wrapObject(name, meta) {
    const obj = {__id__: name};
    channel.objects[name] = obj;
    const byIndex = {};
    (meta.signals || []).forEach((sig) => {
      const sigName = sig[0], sigIndex = sig[1];
      const handlers = [];
      byIndex[sigIndex] = handlers;
      obj[sigName] = {
        connect(cb) {
          if (typeof cb !== 'function') return;
          handlers.push(cb);
          if (handlers.length === 1) exec({type: T.connectToSignal, object: name, signal: sigIndex});
        },
        disconnect(cb) {
          const i = handlers.indexOf(cb);
          if (i >= 0) handlers.splice(i, 1);
          if (!handlers.length) exec({type: T.disconnectFromSignal, object: name, signal: sigIndex});
        }
      };
    });
    obj.__emit = (key, args) => {
      const handlers = byIndex[key] || [];
      handlers.forEach((cb) => { try { cb.apply(null, args || []); } catch (e) {} });
    };
    (meta.methods || []).forEach((m) => {
      const methodName = String(m[0]), methodIdx = m[1];
      const invoke = methodName.charAt(methodName.length - 1) === ')' ? methodIdx : methodName;
      obj[methodName.split('(')[0]] = function() {
        const args = [];
        let callback = null;
        for (let i = 0; i < arguments.length; i++) {
          if (typeof arguments[i] === 'function') callback = arguments[i];
          else args.push(arguments[i]);
        }
        exec({type: T.invokeMethod, object: name, method: invoke, args: args},
             callback ? (response) => callback(response) : null);
      };
    });
    return obj;
  }

  exec({type: T.init}, (data) => {
    Object.keys(data || {}).forEach((name) => wrapObject(name, data[name] || {}));
    if (typeof initCallback === 'function') { try { initCallback(channel); } catch (e) {} }
    exec({type: T.idle});
  });
};
"""

# ---------------------------------------------------------------------------
# Appended after the qwebchannel.js library (Qt resource or the lite client).
# Retries: `qt` and `QWebChannel` may appear slightly after document creation.
CHANNEL_INIT_JS = r"""
(() => {
  const connect = (tries) => {
    try {
      if (typeof qt === 'undefined' || !qt.webChannelTransport ||
          typeof QWebChannel === 'undefined') {
        if (tries > 0) setTimeout(() => connect(tries - 1), 50);
        return;
      }
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
    } catch (e) {
      if (tries > 0) setTimeout(() => connect(tries - 1), 50);
    }
  };
  connect(40);
})();
"""

# ---------------------------------------------------------------------------
# Purges unneeded site chrome (Google header, account menu, navigation,
# snackbars, the G-logo). Runs at document creation to avoid any visual flash
# and is re-applied over CDP after attach. Protected: the whole input area,
# microphone/send/file controls and their icons.
JS_PURGE = r"""
(() => {
    if (window.__purgeInstalled) {
        if (window.__purgeRun) window.__purgeRun();
        return;
    }

    window.__purgeInstalled = true;

    const SELECTORS = [
        'div.qEn1od[jsname="NlVIob"]',
        'div.qEn1od',
        'div.P3mIxe.Hw60ud',
        'div.FSUH7d[jsname="xcvsnc"]',
        'div.GG4mbd[role="navigation"]',
        'div.eT9Cje',
        'span.gb',
        'div[jscontroller="SJpD2c"][jsname="uZkjhb"]',
        'header#gb',
        'div#gbwa',
        'g-snackbar[jsname="PWj1Zb"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"] *'
    ];

    const S = SELECTORS.join(',');

    // Никогда не трогаем панель ввода, микрофон, отправку и файлы.
    const INPUT_AREA = 'form, footer, .esoFne, .Txyg0d, .CEpIFc, [role="region"], [data-xid*="input-plate"]';
    const PROTECTED_LABEL = /икрофон|микрофон|mic|voice|голос|отправ|send|dictat|дикт|вопрос|attach|прикреп|файл|file/i;
    const isProtected = (el) => {
        try {
            if (!el || el.nodeType !== 1) return true;
            if (el.closest && el.closest(INPUT_AREA)) return true;
            const label = ((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('data-xid') || el.getAttribute('title'))) || '');
            if (PROTECTED_LABEL.test(label)) return true;
            const text = (el.textContent || '').slice(0, 120);
            if (PROTECTED_LABEL.test(text)) return true;
            return false;
        } catch (e) { return true; }
    };

    const hide = (el) => {
        if (!el || isProtected(el)) return;
        if (el.style) {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
        }
        try { el.remove(); } catch (e) {}
    };

    const kill = (node) => {
        if (!node || node.nodeType !== 1) return;

        if (node.matches && node.matches(S)) {
            hide(node);
            return;
        }

        if (node.querySelectorAll) {
            const list = node.querySelectorAll(S);
            for (let i = list.length - 1; i >= 0; i--) hide(list[i]);
        }
    };

    window.__purgeRun = () => kill(document.documentElement);

    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) kill(n);
            } else if (m.type === 'attributes') {
                kill(m.target);
            }
        }
    });

    const start = () => {
        const root = document.documentElement;
        if (!root) return false;

        obs.observe(root, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'id', 'jsname', 'jscontroller']
        });

        kill(root);
        return true;
    };

    if (!start()) {
        const t = setInterval(() => {
            if (start()) clearInterval(t);
        }, 10);
    }

    document.addEventListener('DOMContentLoaded', () => kill(document.documentElement), { once: true });
    window.addEventListener('load', () => kill(document.documentElement), { once: true });
})();
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
