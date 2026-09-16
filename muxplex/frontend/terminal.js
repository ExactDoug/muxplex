// Phase 2b implementation — terminal.js
// xterm.js Terminal + FitAddon initialization (task-12)

// ─── Module-level state ───────────────────────────────────────────────────────
let _term = null;
let _fitAddon = null;
let _ws = null;
let _reconnectTimer = null;
let _currentSession = null;
let _vpHandler = null;
let _fitFrame = null;   // pending handle for the single terminal-geometry owner
let _fitCancel = null;  // canceller matching whatever scheduler produced _fitFrame
let _reconnectAttempts = 0; // tracks consecutive failed reconnect attempts for backoff + ttyd respawn
let _connOpenedAt = 0; // Date.now() when the live WS opened; 0 when none is open
let _searchAddon = null;
let _resizeObserver = null;

// Maximum consecutive reconnect attempts before the terminal gives up and
// presents a terminal-state message instead of an endless "Reconnecting…".
//
// The backoff is 1s, 2s, 4s, 8s then a 15s cap, so 8 attempts ≈ 75s of retrying
// — long enough to ride out a server restart, short enough that a genuinely
// unrecoverable connection stops spinning. This is a BACKSTOP: the common case
// (the session's process exited, so tmux destroyed the session) is caught much
// sooner and more precisely by the 404 from /connect — see endTerminalSession.
const MAX_RECONNECT_ATTEMPTS = 8;

// How long a WebSocket must stay open before we call it healthy and forgive the
// reconnect counter. A doomed `tmux attach` dies in ~11ms (measured); a real
// session lives for minutes. 5s sits far above the noise and far below anything
// a user would call "it was working".
const HEALTHY_CONNECTION_MS = 5000;

/**
 * Put the terminal into a terminal (non-retrying) state with an explanation.
 *
 * A session whose process exits can NEVER be reconnected to — tmux destroys the
 * session (`exit-empty on`), so `tmux attach -t <name>` fails and every ttyd we
 * respawn dies immediately. Before this existed the close handler simply
 * rescheduled forever and the user saw "Reconnecting…" indefinitely.
 *
 * Nulls _currentSession, which is the single latch every reconnect path checks
 * (`if (!_currentSession) return;`) — so this stops both the close handler and
 * any in-flight /connect continuation from scheduling further work.
 *
 * @param {string} message - user-facing explanation, e.g. "Session ended."
 */
function endTerminalSession(message) {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  _currentSession = null;
  _reconnectAttempts = 0;
  if (_ws) {
    try { _ws.close(); } catch (_) {}
    _ws = null;
  }
  var reconnectOverlay = document.getElementById('reconnect-overlay');
  if (reconnectOverlay) reconnectOverlay.classList.add('hidden');
  var endedMsg = document.getElementById('session-ended-msg');
  if (endedMsg) endedMsg.textContent = message;
  var ended = document.getElementById('session-ended-overlay');
  if (ended) ended.classList.remove('hidden');
}

/**
 * Decide whether a POST /connect response proves the session is gone for good.
 *
 * Local sessions: `connect_session` (main.py) raises 404 "Session 'x' not found"
 * when the name is absent from the cached session list — the poll cycle refreshes
 * that cache every ~2s, so a session destroyed by its process exiting 404s almost
 * immediately. That 404 is definitive.
 *
 * Federated sessions: the local `/api/federation/{id}/connect/{name}` proxy
 * translates ANY non-2xx from the peer into a 502 "Remote returned <code>"
 * (main.py federation_connect), so the peer's 404 arrives here as a 502 whose
 * detail names the original status. A bare 502 is NOT enough — it also covers a
 * peer that is merely unhealthy, which we want to keep retrying.
 *
 * Everything else (503 unreachable, 500, network error) is treated as possibly
 * transient and left to the retry cap.
 *
 * @param {Response|null} res
 * @param {string} [remoteId]
 * @returns {Promise<boolean>} true when the session is definitively gone
 */
function _connectSaysSessionGone(res, remoteId) {
  if (!res || typeof res.status !== 'number') return Promise.resolve(false);
  if (res.status === 404) return Promise.resolve(true);
  if (!remoteId || res.status !== 502) return Promise.resolve(false);
  // Federated: only a peer-side 404 counts. Read the proxied detail to tell a
  // dead remote session apart from a sick remote instance.
  if (typeof res.json !== 'function') return Promise.resolve(false);
  return Promise.resolve()
    .then(function() { return res.json(); })
    .then(function(body) {
      var detail = body && body.detail;
      return typeof detail === 'string' && detail.indexOf('404') !== -1;
    })
    .catch(function() { return false; });
}

/** Hide the "session ended" overlay (called when a new session is opened). */
function hideSessionEndedOverlay() {
  var ended = document.getElementById('session-ended-overlay');
  if (ended) ended.classList.add('hidden');
}

// Attach-once listener for the ended-overlay's "Back to sessions" button
// (contract #3: container/static-element listeners live in module-level IIFEs,
// never inside openTerminal). Delegates to the expanded header's back button so
// there is exactly one implementation of "return to the grid" — app.js owns it.
(function initSessionEndedBack() {
  if (typeof document === 'undefined' || !document.addEventListener) return;
  document.addEventListener('click', function(e) {
    var t = e && e.target;
    if (!t || !t.closest) return;
    if (!t.closest('#session-ended-back')) return;
    hideSessionEndedOverlay();
    var back = document.getElementById('back-btn');
    if (back && back.click) back.click();
  });
})();

// ─── Module-level encoding helpers ──────────────────────────────────────────
// Hoisted here so the clipboard key handler (in openTerminal) can also use them.
const _encoder = typeof TextEncoder !== 'undefined' ? new TextEncoder() : null;
// TextDecoder: used to decode UTF-8 bytes received from ttyd before writing to xterm.js.
// xterm.js write(Uint8Array) treats each byte as Latin-1, not UTF-8 — multi-byte characters
// like ─ (U+2500, bytes E2 94 80) render as â (Latin-1 0xE2) without decoding first.
// Matches ttyd's official client pattern: textDecoder.decode(payload) → _term.write(string).
const _decoder = typeof TextDecoder !== 'undefined' ? new TextDecoder() : null;

function _encodePayload(typeChar, str) {
  // Returns Uint8Array: [typeCharCode, ...utf8bytes]
  var strBytes = _encoder ? _encoder.encode(str) : new Uint8Array(Array.from(str).map(function(c) { return c.charCodeAt(0); }));
  var payload = new Uint8Array(1 + strBytes.length);
  payload[0] = typeChar;
  payload.set(strBytes, 1);
  return payload;
}

// ─── Clipboard helpers ───────────────────────────────────────────────────────
// Ctrl+Shift+C: copy terminal selection to system clipboard
// Ctrl+V / Ctrl+Shift+V: native browser paste event → xterm → WebSocket
//   (Ctrl+V needs the custom key handler to return false so xterm doesn't
//   swallow it as raw 0x16 — see attachCustomKeyEventHandler in openTerminal)
// Right-click: reads the browser clipboard via _pasteFromClipboard below

// Paste the BROWSER clipboard into the terminal via the async clipboard API.
// Used only where no native paste event exists (right-click). _term.paste()
// routes through xterm's bracketed-paste support so multi-line pastes arrive
// as one paste event. Returns true if the async clipboard API is available.
function _pasteFromClipboard() {
  if (_diagOn()) console.log('[seldebug] _pasteFromClipboard() called', new Error().stack.split('\n')[2]);
  if (!(navigator.clipboard && navigator.clipboard.readText)) return false;
  navigator.clipboard.readText().then(function(text) {
    if (_diagOn()) console.log('[seldebug] clipboard.readText resolved → _term.paste len=' + (text ? text.length : 0));
    if (text && _term) _term.paste(text);
  }).catch(function() {
    // Permission denied or empty clipboard — nothing to paste
  });
  return true;
}

function _copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(function() {});
  } else {
    // Fallback for non-HTTPS contexts (HTTP over LAN)
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch(e) {}
    document.body.removeChild(ta);
  }
}

// ─── Mouse Lab: experimental terminal-selection levers ──────────────────────
// A per-device (localStorage-backed) harness for A/B-testing the candidate
// fixes for the inadvertent-text-selection bug. Each lever gates one behavior
// in the deliberate-selection and diagnostic IIFEs below. Defaults reproduce the
// SHIPPED (v0.9.5) behavior, so the test suite and production users are
// unaffected unless a lever is deliberately changed. The settings UI (app.js
// "Mouse Lab" tab) writes this config via MouseLab.save(); handlers read it live
// at gesture time, so toggles take effect on the next mouse action — no reload.
//
// Levers (see CLAUDE.md frontend contract #4b for the behaviors they gate):
//   dragThreshold   — ~5px suppressor: sub-threshold left press is a focus click
//   zombieKiller    — buttonless-mousemove kill of a stale (zombie) xterm drag
//   focusClickClear — a focus-only click drops any stray selection + refocuses
//   honorTracking   — when on, the three above bail under mouseTrackingMode
//                     (TUI mouse app owns the mouse); off = act regardless
//   tmuxCopyClear   — on window refocus after a press was lost outside the
//                     window, send Esc to the PTY to cancel tmux copy-mode
//                     (Hypothesis A: the stale highlight is tmux's, not xterm's)
//   diagLogging     — console [seldebug] logging of every mouse event + state
window.MouseLab = (function () {
  var KEY = 'muxplex_mouselab';
  var DEFAULTS = {
    dragThreshold: true,
    zombieKiller: true,
    focusClickClear: true,
    honorTracking: true,
    tmuxCopyClear: false,
    rightClickPassThru: false,
    diagLogging: false,
  };
  var cfg = Object.assign({}, DEFAULTS);

  function load() {
    var next = Object.assign({}, DEFAULTS);
    try {
      var raw = localStorage.getItem(KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        for (var k in DEFAULTS) {
          if (typeof parsed[k] === 'boolean') next[k] = parsed[k];
        }
      }
    } catch (_) { /* blocked / malformed — fall back to defaults */ }
    cfg = next;
  }
  load();

  // Cross-tab + same-tab change propagation (UI in app.js dispatches the latter).
  try {
    window.addEventListener('storage', function (e) {
      if (!e || e.key === KEY || e.key === null) load();
    });
    window.addEventListener('muxplex:mouselab-changed', load);
  } catch (_) {}

  return {
    DEFAULTS: DEFAULTS,
    get: function (k) { return cfg[k]; },
    all: function () { return Object.assign({}, cfg); },
    reload: load,
    // Merge a partial config, persist, and notify readers (this tab + others).
    save: function (partial) {
      var merged = Object.assign({}, cfg, partial || {});
      cfg = merged;
      try { localStorage.setItem(KEY, JSON.stringify(merged)); } catch (_) {}
      try { window.dispatchEvent(new Event('muxplex:mouselab-changed')); } catch (_) {}
      return Object.assign({}, merged);
    },
  };
})();

// Shared diagnostic predicate — true when Mouse Lab lever 6 (diagLogging) is on,
// or the legacy ?seldebug=1 URL / localStorage override is set. Used by the paste
// + right-click probes and by initSelectionDebug below.
function _diagOn() {
  try {
    if (window.MouseLab && window.MouseLab.get('diagLogging')) return true;
    if (/[?&]seldebug=1/.test(location.search)) return true;
    if (localStorage.getItem('muxplex_seldebug') === '1') return true;
  } catch (_) {}
  return false;
}

// ─── Forward declarations ─────────────────────────────────────────────────────

function connectWebSocket(name, remoteId) {
  // Always connect to the same origin — remote sessions route through the
  // federation proxy (ws://host/federation/{remoteId}/terminal/ws) so that
  // no cross-origin WebSocket connections are made from the browser.
  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var url;
  if (remoteId) {
    // Remote session via federation proxy — same origin, different path
    url = proto + '//' + location.host + '/federation/' + remoteId + '/terminal/ws';
  } else {
    // Local session: same origin
    url = proto + '//' + location.host + '/terminal/ws';
  }
  const reconnectOverlay = document.getElementById('reconnect-overlay');
  // Use module-level _encodePayload (hoisted above connectWebSocket)
  var encodePayload = _encodePayload;

  // Register terminal event handlers once on this _term instance.
  // These handlers read the module-level _ws at call time (not a captured reference),
  // so they always target the live socket. createTerminal() disposes _term before
  // the next session, removing these handlers automatically.
  if (_term) {
    _term.onData(function(data) {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        // Diagnostic: log every PTY send so a right-click double-paste shows
        // whether the clipboard text is sent once or twice (and whether xterm
        // also forwards an SGR mouse sequence). JSON.stringify reveals control
        // bytes like the \e[200~ bracketed-paste markers and \e[<2;..M mouse.
        if (_diagOn()) console.log('[seldebug] onData→PTY', JSON.stringify(data).slice(0, 80));
        // ttyd protocol: input is type 0x30 ('0') + UTF-8 keystroke bytes
        _ws.send(encodePayload(0x30, data));
      }
    });
    _term.onResize(function(size) {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        // ttyd protocol: resize is type 0x31 ('1') + UTF-8 JSON
        _ws.send(encodePayload(0x31, JSON.stringify({ columns: size.cols, rows: size.rows })));
      }
    });
  }

  // _connectWebSocket — creates the WebSocket instance and registers all event handlers.
  // Called directly for normal reconnects (ttyd still alive), or after a brief delay
  // following the /connect POST (ttyd was dead and needed respawning).
  //
  // Local const `ws` captures this specific instance so each handler can check
  // `if (ws !== _ws) return;` (stale guard). Without it, rapid reconnects or
  // session switches cause old handlers to fire on the new _ws while it is still
  // CONNECTING → send error → close → reconnect → infinite loop (Bug 2).
  function _connectWebSocket() {
    // 'tty' subprotocol is REQUIRED — without it ttyd never starts the PTY.
    // Confirmed via raw Python WebSocket tests: ttyd accepts the TCP upgrade but
    // sits completely silent (no child process spawned) when subprotocol is omitted.
    const ws = new WebSocket(url, ['tty']);
    _ws = ws;
    ws.binaryType = 'arraybuffer';

    ws.addEventListener('open', function() {
      if (ws !== _ws) return; // stale connection — superseded by a newer one, ignore
      // Stamp when this connection opened. The close handler uses it to decide
      // whether the connection LIVED long enough to count as healthy — see the
      // HEALTHY_CONNECTION_MS discussion there. Do NOT reset _reconnectAttempts
      // here: the proxy accepts the browser WS before ttyd is confirmed alive,
      // so 'open' alone proves nothing (that was the original 0→1→0→1 bounce).
      _connOpenedAt = Date.now();
      if (reconnectOverlay) reconnectOverlay.classList.add('hidden');
      // Step 1: TEXT frame auth handshake — ttyd checks AuthToken before starting PTY
      ws.send(JSON.stringify({ AuthToken: '' }));
      // Step 2: BINARY frame with initial terminal dimensions — [0x31] + JSON({columns, rows})
      if (_term) {
        ws.send(encodePayload(0x31, JSON.stringify({ columns: _term.cols, rows: _term.rows })));
      }
      // Auto-focus the terminal so user can type immediately without clicking
      if (_term) _term.focus();
    });

    ws.addEventListener('message', function(e) {
      if (ws !== _ws) return; // stale connection — superseded by a newer one, ignore
      if (!_term) return;
      // NOTE: _reconnectAttempts is deliberately NOT reset here.
      //
      // It used to be, on the theory that "a data frame proves ttyd is alive and
      // relaying". That theory is false in the exact case this whole reconnect
      // path exists to handle: when the tmux session is gone, ttyd dutifully
      // forks `tmux attach`, which writes "can't find session: <name>" to the
      // PTY and dies in ~11ms. That error text arrives as an ordinary 0x30
      // OUTPUT frame — the FAILURE REPORT was being read as a health signal.
      // ttyd also sends 0x31/0x32 (title/preferences) frames on every connect,
      // and the reset ran before the type dispatch, so even a silent connection
      // reset it. Result: the counter oscillated ~0↔1, escalation to /connect
      // took ~5 cycles instead of 2, and the retry cap was nearly unreachable.
      //
      // Health is now judged by how long the connection SURVIVED (see the close
      // handler), which is the version-independent invariant. Do not reintroduce
      // a data-based reset here in any form.
      if (e.data instanceof ArrayBuffer) {
        var msg = new Uint8Array(e.data);
        if (msg.length < 1) return;
        var msgType = msg[0];
        var payload = msg.slice(1);
        if (msgType === 0x30) {  // '0' = terminal output — write to xterm.js
          // decode: Uint8Array → UTF-8 string. write(Uint8Array) treats bytes as Latin-1.
          _term.write(_decoder ? _decoder.decode(payload) : payload);
        }
        // 0x31 ('1') = window title, 0x32 ('2') = preferences — ignore for now
      } else if (typeof e.data === 'string') {
        _term.write(e.data);  // fallback for text frames
      }
    });

    ws.addEventListener('close', function() {
      if (ws !== _ws) return; // stale connection — don't reconnect for old sockets
      if (!_currentSession) return; // intentional close — don't reconnect
      if (reconnectOverlay) reconnectOverlay.classList.remove('hidden');
      // Health check: did this connection LIVE, or did it die on arrival?
      //
      // A connection that carried a real session for a while and then dropped
      // (server restart, network blip, laptop sleep) is a fresh problem — reset
      // the counter so the user gets the full patient backoff. A connection that
      // died within HEALTHY_CONNECTION_MS never worked, so it counts toward
      // escalation no matter how many bytes it delivered on its way out.
      //
      // Duration is deliberate: it is the one signal that does not depend on
      // parsing tmux's error text (fragile across versions and locales) and it
      // covers every instant-death cause, not just a missing session.
      var lived = _connOpenedAt ? Date.now() - _connOpenedAt : 0;
      if (lived >= HEALTHY_CONNECTION_MS) _reconnectAttempts = 0;
      _connOpenedAt = 0;
      _reconnectAttempts++;
      // Bounded retry: never spin forever. Without this cap a session that can
      // never be reconnected to (its tmux session was destroyed, the server is
      // gone) left "Reconnecting…" on screen indefinitely.
      if (_reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
        endTerminalSession('Lost connection to this session.');
        return;
      }
      // Exponential backoff: 1s, 2s, 4s, 8s, cap at 15s. Add jitter to avoid thundering herd.
      var delay = Math.min(1000 * Math.pow(2, _reconnectAttempts - 1), 15000);
      delay += Math.random() * 500; // jitter
      _reconnectTimer = setTimeout(connect, delay);
    });

    ws.addEventListener('error', function() {
      if (ws !== _ws) return; // stale connection — ignore
      console.warn('tmux-web: WebSocket error on', url);
    });
  }

  // Schedule the post-/connect settle that gives ttyd time to bind its port.
  //
  // Guarded on _currentSession because this runs from a fetch continuation that
  // may resolve AFTER the terminal was ended (by the retry cap, by the user
  // navigating away, or by a session switch). Without the guard the
  // continuation resurrects the loop we just stopped and clobbers the
  // _reconnectTimer = null that endTerminalSession set. Nulling
  // _currentSession only halts reconnects if every path actually READS it —
  // this one did not.
  function _scheduleSettle() {
    if (!_currentSession) return;
    _reconnectTimer = setTimeout(function() {
      if (!_currentSession) return; // ended while the settle timer was pending
      _connectWebSocket();
    }, 800);
  }

  function connect() {
    if (!_currentSession) return; // terminal was ended — never reconnect
    // After 2 failed WS attempts, ttyd is likely dead (e.g. after service restart).
    // AWAIT the /connect POST before opening the WebSocket — ttyd must be alive first.
    // fetch() includes cookies automatically for same-origin requests so auth is transparent.
    //
    // Critical: this path uses .then() so _connectWebSocket() runs only AFTER the POST
    // response (plus an 800ms settle delay for ttyd to bind its port). The early return
    // prevents falling through to the direct _connectWebSocket() call below.
    if (_reconnectAttempts >= 2 && _currentSession) {
      var connectPath;
      if (remoteId) {
        // Remote session: route through federation proxy
        connectPath = '/api/federation/' + encodeURIComponent(remoteId) + '/connect/' + encodeURIComponent(_currentSession);
      } else {
        // Local session
        connectPath = '/api/sessions/' + encodeURIComponent(_currentSession) + '/connect';
      }
      fetch(connectPath, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })
        // NOTE: the .catch() MUST come after the .then() below. It previously
        // came first, which meant the success path ran unconditionally and the
        // response was never inspected at all — a 404 ("session not found")
        // looked exactly like a success and we respawned the WebSocket anyway,
        // forever. fetch() only rejects on network failure; an HTTP 404 is a
        // perfectly RESOLVED promise, so swallowing rejections was never the
        // thing hiding the error — ignoring res.ok was.
        .then(function(res) {
          return _connectSaysSessionGone(res, remoteId).then(function(gone) {
            if (gone) {
              // The tmux session no longer exists (its process exited, so
              // tmux's `exit-empty on` destroyed it). Every future attempt
              // would spawn a `tmux attach` that dies instantly. Stop; explain.
              endTerminalSession('Session ended.');
              return null;
            }
            // Brief delay for ttyd to bind its port after /connect spawns it
            _scheduleSettle();
            return null;
          });
        })
        .catch(function() {
          // Network-level failure (server down, offline). Not proof the session
          // died — retry via the normal path; the attempt cap bounds it.
          _scheduleSettle();
          return null;
        });
      return; // Don't fall through — .then() handles the WebSocket creation
    }

    _connectWebSocket();
  }

  connect();
}
// ─── Terminal geometry — SINGLE OWNER ────────────────────────────────────────
//
// Exactly ONE place computes #terminal-container's height and calls fit(). Two
// owners used to race here: this handler (resize-only, synchronous, and blind to
// the mobile keybar — it subtracted a hardcoded 44px header and nothing else)
// and mobile-keyboard.js's rAF-deferred one (which correctly subtracted the
// keybar). Both ran per keyboard event, so the container was sized twice at two
// different heights and fit() fired twice. Each fit sends a resize down the wire
// to ttyd, so tmux resized twice per keyboard event and every TUI in the session
// redrew for an intermediate geometry ~54px too tall — which is why the prompt
// ended up drawn underneath the keybar. See issue #15.
//
// mobile-keyboard.js no longer sizes anything: it publishes --keybar-lift /
// --keybar-height and asks us to refit. If that module is absent, _keybarHeight()
// returns 0 and this still produces correct geometry.

/** Let the keybar republish its dock vars. Cheap; safe when the module is absent. */
function _syncKeybarDock() {
  try {
    var kb = window.MuxplexMobileKeyboard;
    if (kb && typeof kb.syncDock === 'function') kb.syncDock();
  } catch (_) {}
}

/** Height the mobile keybar currently occupies, or 0 when it is absent/hidden. */
function _keybarHeight() {
  try {
    var kb = window.MuxplexMobileKeyboard;
    if (kb && typeof kb.toolbarHeight === 'function') return kb.toolbarHeight() || 0;
  } catch (_) {}
  return 0;
}

/** Measured height of a chrome element, or `fallback` when it is missing/hidden. */
function _chromeHeight(el, fallback) {
  if (!el) return fallback;
  try {
    if (el.classList && el.classList.contains('hidden')) return 0;
    return Math.ceil(el.getBoundingClientRect().height || el.offsetHeight || fallback);
  } catch (_) {
    return fallback;
  }
}

function fitTerminalToViewport() {
  _fitFrame = null;
  _fitCancel = null;
  if (!_term || !_fitAddon) return;

  var container = null;
  var header = null;
  var search = null;
  try {
    container = document.getElementById('terminal-container');
    header = document.querySelector('.expanded-header');
    search = document.getElementById('terminal-search-bar');
  } catch (_) {
    return;
  }
  if (!container) return;

  // visualViewport.height already excludes the software keyboard on iOS (and the
  // whole keyboard assembly: accessory bar + keys + emoji row). innerHeight does
  // NOT — iOS overlays the keyboard rather than shrinking the layout viewport.
  var vv = window.visualViewport;
  var available = (vv && vv.height) || window.innerHeight || 0;
  if (!available) return;

  var height = Math.max(
    80,
    Math.floor(available - _chromeHeight(header, 44) - _chromeHeight(search, 0) - _keybarHeight())
  );

  // maxHeight, NOT height. #terminal-container is `flex: 1` inside a column flex
  // .terminal-wrapper (style.css), and `flex: 1` means `flex-basis: 0%`, which
  // OVERRIDES the height property for a flex item's main size. Assigning
  // container.style.height here is therefore inert — it always was, in both of the
  // handlers that used to race, which is why the prompt stayed buried even after
  // the geometry arithmetic was correct. The container simply grew to fill the
  // wrapper, and the wrapper is sized by the LAYOUT viewport, which iOS does not
  // shrink for the keyboard.
  //
  // A max-height constraint IS honoured by the flex algorithm (flex items are
  // clamped to their min/max), so the container grows as before and is capped at
  // the visual viewport's usable height. It is also fail-safe: it can only ever
  // make the terminal smaller than today's behaviour, never larger, so a wrong
  // measurement degrades toward the status quo instead of breaking desktop.
  // height is set alongside it for non-flex contexts and harmless where flex wins.
  try {
    container.style.maxHeight = height + 'px';
    container.style.height = height + 'px';
  } catch (_) {}
  try { _fitAddon.fit(); } catch (_) {}
}

/**
 * Coalesce every geometry request into ONE fit, so a burst of viewport events
 * (iOS emits resize AND scroll for a single keyboard open) still costs exactly
 * one container resize and therefore one tmux resize.
 *
 * Prefers requestAnimationFrame, falls back to a timer, and finally runs inline:
 * a host without either must still get correct geometry rather than none. Every
 * lookup is guarded because this runs in browsers, in tests, and under a DOM stub.
 */
function scheduleTerminalFit() {
  if (_fitFrame !== null) {
    try { if (_fitCancel) _fitCancel(_fitFrame); } catch (_) {}
    _fitFrame = null;
    _fitCancel = null;
  }

  if (typeof window.requestAnimationFrame === 'function') {
    _fitCancel = typeof window.cancelAnimationFrame === 'function'
      ? window.cancelAnimationFrame.bind(window)
      : null;
    _fitFrame = window.requestAnimationFrame(fitTerminalToViewport);
    return;
  }

  var timer = typeof window.setTimeout === 'function'
    ? window.setTimeout.bind(window)
    : (typeof setTimeout === 'function' ? setTimeout : null);
  if (timer) {
    _fitCancel = typeof window.clearTimeout === 'function'
      ? window.clearTimeout.bind(window)
      : (typeof clearTimeout === 'function' ? clearTimeout : null);
    _fitFrame = timer(fitTerminalToViewport, 0);
    return;
  }

  fitTerminalToViewport();
}

function _unbindVisualViewport() {
  if (!_vpHandler) return;
  try {
    if (window.visualViewport) {
      window.visualViewport.removeEventListener('resize', _vpHandler);
      window.visualViewport.removeEventListener('scroll', _vpHandler);
    }
    window.removeEventListener('resize', _vpHandler);
    window.removeEventListener('orientationchange', _vpHandler);
  } catch (_) {}
  _vpHandler = null;
}

function initVisualViewport() {
  _unbindVisualViewport();

  _vpHandler = function () {
    // Dock synchronously so the keybar never lags the keyboard, but defer the
    // terminal fit so a burst of events still costs exactly one tmux resize.
    _syncKeybarDock();
    scheduleTerminalFit();
  };

  try {
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', _vpHandler);
      // iOS signals keyboard show/hide via an offsetTop change (a *scroll*) as
      // often as a resize; without this the terminal lags visibly. Contract #7.
      window.visualViewport.addEventListener('scroll', _vpHandler);
    }
    window.addEventListener('resize', _vpHandler);
    window.addEventListener('orientationchange', _vpHandler);
  } catch (_) {}

  scheduleTerminalFit();
}

// ─── Terminal creation ────────────────────────────────────────────────────────

/**
 * Create (or recreate) the xterm.js Terminal and FitAddon instances.
 * Disposes any existing terminal first.
 * Stores the results in module-level _term and _fitAddon.
 * @param {number} [fontSize=14] - font size in pixels, from server display settings
 */
function createTerminal(fontSize) {
  // Dispose any existing instance
  if (_term) {
    _term.dispose();
    _term = null;
    _fitAddon = null;
  }

  // Use the fontSize passed from app.js (getDisplaySettings().fontSize), defaulting to 14.
  var storedFontSize = (typeof fontSize === 'number' && fontSize > 0) ? fontSize : 14;

  const mobile = window.innerWidth < 600; // matches MOBILE_THRESHOLD in app.js
  const effectiveFontSize = mobile ? Math.min(storedFontSize, 12) : storedFontSize;

  _term = new window.Terminal({
    cursorBlink: true,
    fontSize: effectiveFontSize,
    fontFamily: "'SF Mono', 'Fira Code', Consolas, monospace",
    theme: {
      background: '#000000',
      foreground: '#c9d1d9',
      cursor: '#58a6ff',
    },
    scrollback: mobile ? 500 : 5000,
    allowProposedApi: true,
  });

  _fitAddon = new window.FitAddon.FitAddon();
  _term.loadAddon(_fitAddon);

  // Clickable URLs — Ctrl+Click (Windows/Linux) or Cmd+Click (macOS) opens in new tab.
  // xterm-addon-web-links auto-detects URLs and adds hover underlines.
  // Plain click is preserved for normal terminal text selection.
  var WebLinksAddon = window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon;
  if (WebLinksAddon) {
    _term.loadAddon(new WebLinksAddon(function(event, uri) {
      if (event.ctrlKey || event.metaKey) {
        window.open(uri, '_blank');
      }
    }));
  }

  // Search addon — Ctrl+F to find text in terminal buffer
  var SearchAddon = window.SearchAddon && window.SearchAddon.SearchAddon;
  if (SearchAddon) {
    _searchAddon = new SearchAddon();
    _term.loadAddon(_searchAddon);
  }

  // Image addon — inline image rendering (Sixel, iTerm2 IIP, Kitty graphics)
  // Needed for tools like yazi file manager that use graphic protocols
  var ImageAddon = window.ImageAddon && window.ImageAddon.ImageAddon;
  if (ImageAddon) {
    _term.loadAddon(new ImageAddon());
  }
}

// ─── Search helpers ──────────────────────────────────────────────────────────────────────────────────────────────────

function _openSearch() {
  var bar = document.getElementById('terminal-search-bar');
  var input = document.getElementById('terminal-search-input');
  if (bar) {
    bar.classList.remove('hidden');
    if (input) {
      input.focus();
      input.select();
    }
  }
}

function _closeSearch() {
  var bar = document.getElementById('terminal-search-bar');
  if (bar) bar.classList.add('hidden');
  if (_searchAddon) _searchAddon.clearDecorations();
  if (_term) _term.focus();
}

function _searchNext() {
  var input = document.getElementById('terminal-search-input');
  if (input && input.value && _searchAddon) {
    _searchAddon.findNext(input.value);
  }
}

function _searchPrev() {
  var input = document.getElementById('terminal-search-input');
  if (input && input.value && _searchAddon) {
    _searchAddon.findPrevious(input.value);
  }
}

// ─── Open / close ─────────────────────────────────────────────────────────────

/**
 * Open a terminal session inside #terminal-container.
 * @param {string} sessionName
 * @param {string} [remoteId]  Optional federation remote ID.
 *   When provided, the WebSocket connects via the federation proxy path
 *   ws://host/federation/{remoteId}/terminal/ws (same origin, no cross-origin).
 */
function openTerminal(sessionName, remoteId, fontSize) {
  // Null _currentSession first so any in-flight close handler on the old WS won't
  // schedule a reconnect (it checks `if (!_currentSession) return;`).
  _currentSession = null;
  _reconnectAttempts = 0; // reset backoff on new session open
  hideSessionEndedOverlay(); // clear any "Session ended." state from a prior session

  // Cancel any pending reconnect timer from the previous session.
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }

  // Close existing WebSocket so it can't write to the new terminal (Bug 1 fix).
  if (_ws) {
    _ws.close();
    _ws = null;
  }

  _currentSession = sessionName;

  const container = document.getElementById('terminal-container');
  if (!container) {
    console.warn('[openTerminal] #terminal-container not found');
    return;
  }

  createTerminal(fontSize);

  _term.open(container);

  // --- Auto-refit on container resize (sidebar toggle, etc.) ---
  // xterm.js FitAddon only resizes on explicit fit() calls. A ResizeObserver
  // on the container handles ALL layout changes: sidebar toggle, window resize,
  // and any future CSS geometry change. Debounced to coalesce rapid events
  // (e.g. during CSS transition animation frames).
  if (_resizeObserver) { _resizeObserver.disconnect(); _resizeObserver = null; }
  if (typeof ResizeObserver !== 'undefined') {
    var _roTimer = null;
    _resizeObserver = new ResizeObserver(function() {
      clearTimeout(_roTimer);
      _roTimer = setTimeout(function() {
        if (_fitAddon) try { _fitAddon.fit(); } catch (_) {}
      }, 50);
    });
    _resizeObserver.observe(container);
  }

  // --- Clipboard integration ---
  // Copy: Ctrl+Shift+C intercepts and copies selection to system clipboard
  // Paste: handled natively by xterm.js (browser paste event → hidden textarea → onData → WebSocket)
  //   Cmd+V (macOS) and Ctrl+Shift+V (Linux) both trigger native browser paste events
  _term.attachCustomKeyEventHandler(function(e) {
    if (e.type !== 'keydown') return true;

    // Ctrl+Shift+C → copy selection to clipboard
    if (e.ctrlKey && e.shiftKey && (e.key === 'C' || e.code === 'KeyC')) {
      var sel = _term.getSelection();
      if (sel) _copyToClipboard(sel);
      return false;  // prevent xterm from processing
    }

    // Ctrl+V → paste (Windows convention). By default xterm translates this
    // keydown into raw 0x16 (SYN) sent to the PTY *and* cancels the event, so
    // the browser clipboard is never read (apps like Claude Code then try the
    // server-side clipboard, which is headless/empty). Returning false here
    // skips xterm's keydown processing WITHOUT preventDefault — the browser's
    // native paste event then fires on xterm's hidden textarea and xterm
    // pastes it through its normal bracketed-paste path. No clipboard API
    // call, so no double-paste and no permission prompt (COE: custom paste
    // handlers that read the clipboard caused double-paste).
    if (e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 'v' || e.key === 'V')) {
      return false;
    }

    // Shift+Enter → send LF (0x0a, same as Ctrl+J) instead of CR. TUI apps
    // like Claude Code treat LF as "insert newline" vs CR "submit", matching
    // Shift+Enter behavior in desktop terminals. Plain shells treat LF and CR
    // identically, so this is harmless everywhere else.
    if (e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey && e.key === 'Enter') {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(_encodePayload(0x30, '\n'));
      }
      e.preventDefault();
      return false;
    }

    // Ctrl+F → open search bar
    if (e.ctrlKey && !e.shiftKey && (e.key === 'f' || e.key === 'F' || e.code === 'KeyF')) {
      _openSearch();
      return false;
    }

    return true;  // let xterm handle all other keys normally
  });

  // Auto-copy: when mouse selection ends, copy to system clipboard.
  // Matches terminal emulator conventions (iTerm2, WezTerm, ttyd native).
  // onSelectionChange fires whenever selection changes — copy if text is selected.
  // When selection is cleared (empty string), we skip the clipboard write.
  _term.onSelectionChange(function() {
    var sel = _term.getSelection();
    if (sel) {
      _copyToClipboard(sel);
    }
  });

  // OSC 52 clipboard integration — bridges tmux clipboard to the browser.
  // When tmux copies text (with `set-clipboard on` in .tmux.conf), it sends
  // an OSC 52 escape sequence to the terminal. xterm.js surfaces this via the
  // parser API. We intercept and write the decoded text to the system clipboard
  // so that: Ctrl+B [ → select → Enter (tmux copy) → system clipboard receives it.
  _term.parser.registerOscHandler(52, function(data) {
    // OSC 52 format: Pc ; Pd — Pc = selection target (c/p/q/s/0-7), Pd = base64 text
    var parts = data.split(';');
    if (parts.length >= 2) {
      try {
        var text = atob(parts[1]);
        _copyToClipboard(text);
      } catch (e) {
        // Invalid base64 or unsupported — silently ignore
      }
    }
    return true;  // Handled — don't pass to xterm's default handler
  });

  if (_fitAddon) {
    // requestAnimationFrame guarantees one full browser layout pass after the flex
    // container becomes visible before fit() measures dimensions.
    // iOS Safari defers flex layout — calling fit() synchronously here gives 0px width
    // → 2-column terminal. The RAF and 500ms fallback fix this race condition.
    // Falls back to immediate execution in Node.js test environments where RAF is absent.
    const fitAddonRef = _fitAddon;
    const raf = typeof requestAnimationFrame !== 'undefined' ? requestAnimationFrame : (fn) => fn();
    raf(function() {
      try { fitAddonRef.fit(); } catch (_) {}
      // 500ms fallback for slow mobile layout engines (e.g. first paint on low-end devices)
      setTimeout(function() {
        try { if (_fitAddon) _fitAddon.fit(); } catch (_) {}
      }, 500);
    });
  }

  // Wire search bar buttons + keyboard handlers (idempotent — elements are static)
  var searchInput = document.getElementById('terminal-search-input');
  var searchClose = document.getElementById('terminal-search-close');
  var searchNextBtn = document.getElementById('terminal-search-next');
  var searchPrevBtn = document.getElementById('terminal-search-prev');

  if (searchInput) {
    // Remove old listeners by replacing with cloned element (avoids duplicate handlers on reconnect)
    var newInput = searchInput.cloneNode(true);
    searchInput.parentNode.replaceChild(newInput, searchInput);
    searchInput = newInput;
    searchInput.addEventListener('input', function() {
      if (_searchAddon && searchInput.value) {
        _searchAddon.findNext(searchInput.value);
      } else if (_searchAddon) {
        _searchAddon.clearDecorations();
      }
    });
    searchInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        if (e.shiftKey) _searchPrev(); else _searchNext();
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        _closeSearch();
      }
    });
  }
  if (searchClose) {
    var newClose = searchClose.cloneNode(true);
    searchClose.parentNode.replaceChild(newClose, searchClose);
    newClose.addEventListener('click', _closeSearch);
  }
  if (searchNextBtn) {
    var newNext = searchNextBtn.cloneNode(true);
    searchNextBtn.parentNode.replaceChild(newNext, searchNextBtn);
    newNext.addEventListener('click', _searchNext);
  }
  if (searchPrevBtn) {
    var newPrev = searchPrevBtn.cloneNode(true);
    searchPrevBtn.parentNode.replaceChild(newPrev, searchPrevBtn);
    newPrev.addEventListener('click', _searchPrev);
  }

  connectWebSocket(sessionName, remoteId);
  initVisualViewport(); /* defined in Task 14 */
}

/**
 * Close the current terminal session and clean up all resources.
 */
function closeTerminal() {
  _unbindVisualViewport();
  try {
    var _c = document.getElementById('terminal-container');
    if (_c) { _c.style.maxHeight = ''; _c.style.height = ''; }
  } catch (_) {}
  if (_fitFrame !== null) {
    try { if (_fitCancel) _fitCancel(_fitFrame); } catch (_) {}
    _fitFrame = null;
    _fitCancel = null;
  }

  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }

  if (_ws) {
    _ws.close();
    _ws = null;
  }

  if (_resizeObserver) { _resizeObserver.disconnect(); _resizeObserver = null; }

  if (_term) {
    _term.dispose();
    _term = null;
    _fitAddon = null;
    _searchAddon = null;
  }

  _closeSearch();
  hideSessionEndedOverlay();
  _currentSession = null;
  _reconnectAttempts = 0; // reset backoff on intentional close
}

// ─── Expose to app.js ─────────────────────────────────────────────────────────
window._openTerminal = openTerminal;
window._closeTerminal = closeTerminal;
window._openSearch = _openSearch;
window._closeSearch = _closeSearch;

// ---------------------------------------------------------------------------
// setTerminalFontSize — live font-size update without reconnecting
// ---------------------------------------------------------------------------

/**
 * Update the terminal font size at runtime without reconnecting.
 * Modifies _term.options.fontSize and refits the terminal to recalculate dimensions.
 * No-op when no terminal is open.
 * @param {number} size - font size in pixels
 */
function setTerminalFontSize(size) {
  if (!_term) return;
  _term.options.fontSize = size;
  if (_fitAddon) {
    try { _fitAddon.fit(); } catch (_) {}
  }
}

window._setTerminalFontSize = setTerminalFontSize;

// Sole entry point for "the terminal's available space may have changed".
// mobile-keyboard.js calls this instead of sizing the container itself.
window._fitTerminalToViewport = scheduleTerminalFit;

// ---------------------------------------------------------------------------
// Right-click copy-or-paste — module-level, attached ONCE to the static
// #terminal-container (same pattern as initMobileTerminalScroll below), so
// session switches can never stack duplicate handlers.
//
// Gesture semantics (Windows terminal convention):
//   right-click WITH an active selection  → completes the COPY, never pastes
//   right-click with NO selection         → pastes the browser clipboard
//
// CRITICAL ordering detail: a right-click fires mousedown → contextmenu, and
// depending on browser/input (touchpad two-finger tap, synthetic contextmenu)
// the button-2 mousedown may not fire at all, or selection state may diverge
// between the two events. The selection is therefore sampled (and the copy
// performed) in a capture-phase mousedown handler, before xterm's own mouse
// handling runs. contextmenu then treats the gesture as a COPY if a selection
// was present at EITHER moment — the sampled mousedown flag OR a live
// hasSelection() re-check at contextmenu time — and only pastes when no
// selection existed at either point. The OR closes the race where the
// mousedown sample read false (stale flag / cross-client selection desync)
// while a selection is in fact live, which previously let one right-click both
// copy (auto-copy on select) AND paste.
//
// hasSelection() is buffer-based, not viewport-based — scrolling the selected
// text out of view does not affect it, so no selection tracking of our own
// is needed.
//
// Shift+RMB and Ctrl+RMB still open the browser context menu as escape hatches.
// ---------------------------------------------------------------------------
;(function initRightClickCopyPaste() {
  var container = document.getElementById('terminal-container');
  if (!container) return;

  var hadSelectionOnRightDown = false;

  // Mouse Lab lever 7 (rightClickPassThru): when ON and a full-screen TUI owns
  // the mouse (raw mouseTrackingMode, independent of the left-click honorTracking
  // lever), muxplex does NOT run its own right-click copy/paste — it lets xterm
  // forward the right-click to the app so the app handles it, instead of BOTH
  // acting on one click (the fullscreen-Claude-Code double-paste). Default OFF =
  // current behavior (contract #2 unchanged, tests green).
  function rightClickOwnedByApp() {
    return !!(window.MouseLab && window.MouseLab.get('rightClickPassThru') &&
              _term && _term.modes && _term.modes.mouseTrackingMode !== 'none');
  }

  container.addEventListener('mousedown', function (e) {
    if (e.button !== 2) return; // right button only
    if (rightClickOwnedByApp()) return; // app owns the mouse — don't copy here
    hadSelectionOnRightDown = !!(_term && _term.hasSelection());
    if (_diagOn()) console.log('[seldebug] rclick mousedown hadSel=' + hadSelectionOnRightDown +
      ' track=' + ((_term && _term.modes && _term.modes.mouseTrackingMode) || 'none'));
    // Copy NOW while the selection still exists (auto-copy on select already
    // ran; copying again is idempotent and covers any clipboard divergence).
    if (hadSelectionOnRightDown) _copyToClipboard(_term.getSelection());
  }, true); // capture phase — ahead of xterm's mousedown handling

  container.addEventListener('contextmenu', function (e) {
    if (_diagOn()) console.log('[seldebug] contextmenu fired, hadSelectionOnRightDown=' + hadSelectionOnRightDown);
    if (e.shiftKey || e.ctrlKey || e.metaKey) return; // let modified clicks through
    e.preventDefault(); // always suppress the browser context menu
    // lever 7: app owns the mouse — suppress the menu but don't copy/paste here
    // (let the forwarded right-click reach the app), avoiding the double-paste.
    if (rightClickOwnedByApp()) { hadSelectionOnRightDown = false; return; }
    if (!_term) return;
    // A selection counts as present for this gesture if it existed at the
    // right-button mousedown (sampled flag) OR is still live right now. Either
    // way the gesture is a COPY and must NEVER also paste on the same click.
    var hadSelection = hadSelectionOnRightDown || _term.hasSelection();
    hadSelectionOnRightDown = false; // consume the latch regardless of branch
    if (hadSelection) {
      // COPY gesture. Re-copy if a selection is still live (covers inputs that
      // fired contextmenu without a button-2 mousedown, so nothing copied yet),
      // then clear it. Do NOT paste — the next selection-free right-click pastes.
      if (_term.hasSelection()) {
        _copyToClipboard(_term.getSelection());
        _term.clearSelection();
      }
      return;
    }
    _pasteFromClipboard();
  });
})();

// ---------------------------------------------------------------------------
// Deliberate text selection + zombie-drag killer.
//
// xterm.js 5.3.0's SelectionService stores a selection anchor on a left
// mousedown, attaches document-level mousemove/mouseup listeners, and extends
// the selection from that anchor on EVERY mousemove — with NO check of whether
// a mouse button is physically held (verified in the vendored bundle: the move
// handler's only gate is `if (!selectionStart) return`). Those listeners are
// removed ONLY on mouseup. Two failure modes follow:
//
//   (A) Accidental selection on a focus click. Clicking the terminal just to
//       regain focus + the tiniest pointer drift starts a selection and
//       keystrokes look "stuck". Fix: a drag threshold — a left press that
//       never drags past ~5px starts no selection.
//
//   (B) ZOMBIE DRAG (the big one). If a drag's mouseup never reaches the page —
//       button released outside the window, or the window blurred mid-drag —
//       xterm's drag is never torn down: its document mousemove stays live and
//       the anchor stays set. When you return and merely MOVE the pointer
//       toward your next click (no button held), xterm extends a huge selection
//       from the stale anchor to the cursor BEFORE any click happens. A
//       mousedown- or focus-based reset can't catch this — the damage is done
//       on a buttonless mousemove. Fix: track whether a drag may be open, and
//       the instant a mousemove arrives with `e.buttons === 0` (no button
//       physically down) while a drag is supposedly open, it's a zombie — kill
//       it in CAPTURE phase (ahead of xterm's bubble-phase move) before it can
//       extend. `_term.clearSelection()` is a full teardown in 5.3.0: it nulls
//       the anchor AND removes xterm's document listeners, so nothing can
//       re-extend. This is focus-INDEPENDENT — the earlier focus-tracking
//       approach was unreliable (focusin can fire before mousedown; focus may
//       never move) and is gone.
//
// Guards: left button only (right button is the copy/paste gesture above);
// single click only (e.detail === 1) so double/triple-click word/line selection
// is untouched; unmodified only; never act when a full-screen TUI has mouse
// tracking on (_term.modes.mouseTrackingMode !== 'none') — there a buttonless
// move is legitimate app input. Module-level attach-once on the static
// container/document, same as the handlers above (no per-session stacking).
// ---------------------------------------------------------------------------
;(function initDeliberateSelection() {
  var container = document.getElementById('terminal-container');
  if (!container) return;

  // Lever read helper — reads the live Mouse Lab config at gesture time so UI
  // toggles take effect on the next mouse action (no reload). Falls back to the
  // shipped defaults if MouseLab somehow failed to initialize (all levers on
  // except the two opt-in experimental ones).
  function ML(k) {
    if (window.MouseLab) return window.MouseLab.get(k);
    return !(k === 'tmuxCopyClear' || k === 'rightClickPassThru' || k === 'diagLogging');
  }

  var DRAG_THRESHOLD_SQ = 5 * 5; // squared CSS px of movement before selecting
  var armed = false;             // a qualifying left press is in progress
  var passedThreshold = false;   // pointer has moved far enough to select
  var startX = 0, startY = 0;
  // True while a left mousedown that reached xterm may have an open selection
  // drag. Cleared by any real mouseup; if a mouseup is lost, a later buttonless
  // mousemove exposes the zombie and we kill it.
  var dragMaybeActive = false;
  // Tracking-independent latch for lever 5 (tmuxCopyClear): any left press is
  // "open" until its mouseup. If the window blurs while open, the mouseup was
  // likely lost outside the window → tmux copy-mode may be stranded.
  var leftPressOpen = false;
  var blurredWithPress = false;

  // Lever 4 (honorTracking): when on, the selection levers stand aside while a
  // full-screen TUI owns the mouse (mouseTrackingMode !== 'none') — there a
  // buttonless move is real app input. Turning the lever off makes this return
  // false unconditionally, so the levers act regardless of tracking — to test
  // whether the guard itself is suppressing the killer. (Name kept as
  // inMouseTracking: it answers "should the tracking guard suppress us?")
  function inMouseTracking() {
    if (!ML('honorTracking')) return false;
    return !!(_term && _term.modes && _term.modes.mouseTrackingMode !== 'none');
  }

  // Kill a (possibly zombie) xterm selection drag. clearSelection() nulls the
  // anchor and removes xterm's own document mousemove/mouseup listeners, so no
  // further move can re-extend.
  function killDrag(e) {
    if (e) e.stopImmediatePropagation(); // this buttonless move must not reach xterm
    dragMaybeActive = false;
    armed = false;
    passedThreshold = false;
    document.removeEventListener('mousemove', onMouseMove, true);
    document.removeEventListener('mouseup', onMouseUp, true);
    if (_term) { try { _term.clearSelection(); } catch (_) {} }
  }

  // (A / lever 1) Per-gesture threshold suppressor: swallow xterm's selection-
  // extending move (capture phase, ahead of xterm's bubble move) until a real
  // drag crosses ~5px; then step aside and let xterm select normally. When the
  // lever is off, do not suppress — xterm selects from the first pixel.
  function onMouseMove(e) {
    if (!armed || passedThreshold) return;
    if (!ML('dragThreshold')) { passedThreshold = true; return; }
    var dx = e.clientX - startX;
    var dy = e.clientY - startY;
    if (dx * dx + dy * dy >= DRAG_THRESHOLD_SQ) {
      passedThreshold = true; // real drag — hand the rest to xterm
      return;
    }
    e.stopImmediatePropagation(); // below threshold — xterm never extends
  }

  // (C / lever 3) Focus-only click: on a sub-threshold left release, drop any
  // stray selection and keep the keyboard live.
  function onMouseUp() {
    document.removeEventListener('mousemove', onMouseMove, true);
    document.removeEventListener('mouseup', onMouseUp, true);
    if (ML('focusClickClear') && armed && !passedThreshold && _term) {
      if (_term.hasSelection()) _term.clearSelection();
      _term.focus();
    }
    armed = false;
    passedThreshold = false;
  }

  // (B / lever 2) Always-on zombie-drag killer, focus-independent. Capture phase
  // so it runs before xterm's bubble-phase document mousemove. If a drag may be
  // open and a move arrives with NO button physically held, it's a zombie: kill
  // it before xterm extends. Suppressed under the tracking guard (lever 4).
  document.addEventListener('mousemove', function (e) {
    if (!ML('zombieKiller')) return;
    if (e.buttons !== 0 || !dragMaybeActive || inMouseTracking()) return;
    killDrag(e);
  }, true);

  // Any real mouseup ends the drag latch cleanly — a zombie only exists when
  // this never fires (released outside the window / blurred mid-drag).
  document.addEventListener('mouseup', function () { dragMaybeActive = false; }, true);
  // The tracking-independent lever-5 latch clears on the same real mouseup.
  document.addEventListener('mouseup', function () { leftPressOpen = false; }, true);

  // (lever 5 / tmuxCopyClear) tmux copy-mode lives server-side, independent of
  // xterm's selection. If a left press was open when the window blurred, the
  // mouseup was likely lost outside the window and tmux may be sitting in
  // copy-mode with a stale highlight. On refocus, send Esc to the PTY to cancel
  // it. The blurred-with-press gate keeps Esc from firing on ordinary alt-tab.
  window.addEventListener('blur', function () { blurredWithPress = leftPressOpen; });
  window.addEventListener('focus', function () {
    if (ML('tmuxCopyClear') && blurredWithPress &&
        _ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(_encodePayload(0x30, '\x1b')); } catch (_) {}
    }
    blurredWithPress = false;
    leftPressOpen = false;
  });

  container.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;        // left button only
    leftPressOpen = true;              // lever-5 latch (tracking-independent)
    if (inMouseTracking()) return;     // TUI mouse app owns the drag (lever 4)
    // Any left press that reaches xterm opens a selection drag — track it so a
    // lost mouseup can be detected later as a zombie.
    dragMaybeActive = true;
    if (e.detail !== 1) return;        // leave dbl/triple-click selection alone
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return; // modified → xterm
    // Arm the threshold/focus-click machinery only if a lever needs it.
    if (!ML('dragThreshold') && !ML('focusClickClear')) return;
    armed = true;
    passedThreshold = false;
    startX = e.clientX;
    startY = e.clientY;
    // Do NOT preventDefault — xterm still focuses its textarea on this press.
    document.addEventListener('mousemove', onMouseMove, true);
    document.addEventListener('mouseup', onMouseUp, true);
  }, true); // capture phase — register the move suppressor before xterm reacts
})();

// ---------------------------------------------------------------------------
// Diagnostic logging (Mouse Lab lever 6 / diagLogging). Logs every mouse event +
// the xterm selection range + mouse-ownership state so we can see exactly how a
// plain click produces a stale-anchor selection. Listeners are always attached
// but each is gated on isOn() at call time, so the lever toggles logging live
// (no reload). The legacy ?seldebug=1 URL / localStorage flag forces it on too.
// REMOVE once the selection bug is root-caused.
// ---------------------------------------------------------------------------
;(function initSelectionDebug() {
  var override = false;
  try {
    override = /[?&]seldebug=1/.test(location.search) ||
               localStorage.getItem('muxplex_seldebug') === '1';
  } catch (_) {}
  function isOn() {
    return override || !!(window.MouseLab && window.MouseLab.get('diagLogging'));
  }

  function selInfo() {
    if (!_term) return 'no _term';
    var pos = null, len = 0, hasSel = '?', track = '?';
    try { pos = _term.getSelectionPosition(); } catch (_) {}
    try { len = (_term.getSelection() || '').length; } catch (_) {}
    // Decisive for Hypothesis A vs B: if a highlight is visible while
    // hasSel=false, the selection is tmux copy-mode (server side), not xterm's
    // — our _term.clearSelection() fixes would be aimed at the wrong layer.
    // track is the mouse-ownership state (set by tmux mouse-on / Claude Code).
    try { hasSel = _term.hasSelection(); } catch (_) {}
    try { track = (_term.modes && _term.modes.mouseTrackingMode) || 'none'; } catch (_) {}
    return 'sel.len=' + len + ' hasSel=' + hasSel + ' track=' + track +
           ' range=' + (pos ? JSON.stringify(pos) : 'none');
  }
  function log(tag, e) {
    if (!isOn()) return;
    var t = e.target;
    var desc = t && t.tagName
      ? t.tagName + (t.className ? '.' + String(t.className).split(' ')[0] : '')
      : String(t);
    console.log('[seldebug]', tag,
      'btn=' + e.button, 'buttons=' + e.buttons, 'detail=' + e.detail,
      'x=' + e.clientX, 'y=' + e.clientY,
      'mods=' + (e.shiftKey ? 'S' : '') + (e.altKey ? 'A' : '') +
                (e.ctrlKey ? 'C' : '') + (e.metaKey ? 'M' : ''),
      'tgt=' + desc, '|', selInfo());
    // The selection usually forms right after the event — sample again next frame.
    requestAnimationFrame(function () {
      if (isOn()) console.log('[seldebug]', tag + '+raf', selInfo());
    });
  }
  ['mousedown', 'mouseup', 'click', 'dblclick'].forEach(function (type) {
    document.addEventListener(type, function (e) { log(type, e); }, true);
  });
  window.addEventListener('blur', function () {
    if (isOn()) console.log('[seldebug] window blur |', selInfo());
  });
  window.addEventListener('focus', function () {
    if (isOn()) console.log('[seldebug] window focus |', selInfo());
  });
  if (override) {
    console.log('[seldebug] enabled — reproduce the bug, then copy ALL [seldebug] console lines back to Claude');
  }
})();

// ---------------------------------------------------------------------------
// Mobile touch scroll — rAF-batched WheelEvent dispatch
// Mobile devices batch touchmove events irregularly; dispatching one WheelEvent
// per frame (via requestAnimationFrame) smooths over burst delivery.
// Applies to Android, iOS, and iPadOS touch devices.
// ---------------------------------------------------------------------------
;(function initMobileTerminalScroll() {
  var isTouchDevice = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) ||
                      (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (!isTouchDevice) return;

  var container = document.getElementById('terminal-container');
  if (!container) return;

  var _lastY      = 0;
  var _accumulated = 0;  // pixel debt between rAF ticks
  var _rafId       = null;
  var SCROLL_PX    = 20; // pixels of touch movement = one WheelEvent dispatch

  function flushScroll() {
    _rafId = null;
    if (!_term || Math.abs(_accumulated) < SCROLL_PX) return;

    var viewport = container.querySelector('.xterm-viewport');
    if (!viewport) { _accumulated = 0; return; }

    // One WheelEvent per frame — dir * 120 = one standard scroll click
    var dir = _accumulated > 0 ? 1 : -1;
    viewport.dispatchEvent(new WheelEvent('wheel', {
      deltaY: dir * 120,
      deltaMode: WheelEvent.DOM_DELTA_PIXEL,
      bubbles: true,
      cancelable: true,
    }));
    _accumulated -= dir * SCROLL_PX;

    // Self-schedule until remainder is consumed
    if (Math.abs(_accumulated) >= SCROLL_PX) {
      _rafId = requestAnimationFrame(flushScroll);
    }
  }

  container.addEventListener('touchstart', function (e) {
    _lastY       = e.touches[0].clientY;
    _accumulated = 0;
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
  }, { passive: true });

  container.addEventListener('touchmove', function (e) {
    if (!_term) return;
    e.preventDefault(); // block outer-container scroll

    var y      = e.touches[0].clientY;
    _accumulated += _lastY - y;   // positive = swipe up = newer content
    _lastY = y;

    if (!_rafId) {
      _rafId = requestAnimationFrame(flushScroll);
    }
  }, { passive: false }); // passive:false required for preventDefault

  container.addEventListener('touchend', function () {
    _lastY       = 0;
    _accumulated = 0;
    if (_rafId) { cancelAnimationFrame(_rafId); _rafId = null; }
  }, { passive: true });
})();


