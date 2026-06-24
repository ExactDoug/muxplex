// Phase 2b implementation — terminal.js
// xterm.js Terminal + FitAddon initialization (task-12)

// ─── Module-level state ───────────────────────────────────────────────────────
let _term = null;
let _fitAddon = null;
let _ws = null;
let _reconnectTimer = null;
let _currentSession = null;
let _vpHandler = null;
let _reconnectAttempts = 0; // tracks consecutive failed reconnect attempts for backoff + ttyd respawn
let _searchAddon = null;
let _resizeObserver = null;

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
      // NOTE: do NOT reset _reconnectAttempts here. The server-side proxy accepts
      // the WS before confirming ttyd is alive (auto-spawning if needed), but the
      // browser 'open' event fires as soon as the proxy accepts — not when ttyd
      // is actually ready. Resetting here caused the 0→1→0→1 bounce. Instead,
      // reset on first data message (proves ttyd is alive and relaying).
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
      // First data message proves ttyd is alive and relaying — safe to reset counter.
      // We deliberately do NOT reset in the 'open' handler: the server-side proxy
      // accepts the browser WS before ttyd is fully confirmed alive, so 'open'
      // firing alone doesn't mean data will flow. Resetting here prevents the
      // 0→1→0→1 bounce that kept the reconnect loop from escalating to /connect.
      if (_reconnectAttempts > 0) _reconnectAttempts = 0;
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
      _reconnectAttempts++;
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

  function connect() {
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
        .catch(function() { return null; })
        .then(function() {
          // Brief delay for ttyd to bind its port after /connect spawns it
          setTimeout(_connectWebSocket, 800);
        });
      return; // Don't fall through — .then() handles the WebSocket creation
    }

    _connectWebSocket();
  }

  connect();
}
function initVisualViewport() {
  if (!window.visualViewport) return;
  if (_vpHandler) window.visualViewport.removeEventListener('resize', _vpHandler);

  _vpHandler = function() {
    if (!_term || !_fitAddon) return;
    var container = document.getElementById('terminal-container');
    if (!container) return;

    // Resize container to fill visual viewport above keyboard
    var headerHeight = 44; // matches --header-height CSS custom property
    var vvh = window.visualViewport.height;
    var termHeight = Math.max(100, vvh - headerHeight);
    container.style.height = termHeight + 'px';

    // Refit xterm.js to new container size
    try { _fitAddon.fit(); } catch (_) {}
  };

  window.visualViewport.addEventListener('resize', _vpHandler);
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
  if (_vpHandler) {
    if (window.visualViewport) window.visualViewport.removeEventListener('resize', _vpHandler);
    _vpHandler = null;
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

  container.addEventListener('mousedown', function (e) {
    if (e.button !== 2) return; // right button only
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
    e.preventDefault();
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
    return !(k === 'tmuxCopyClear' || k === 'diagLogging');
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


