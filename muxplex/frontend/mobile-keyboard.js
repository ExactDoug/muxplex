/* muxplex mobile terminal keybar
 *
 * Adds touch-friendly terminal control keys without modifying xterm.js or ttyd.
 * The feature is deliberately isolated and device-local: its enable/disable
 * preference is stored in this browser only, providing a quick failsafe without
 * changing settings on other muxplex clients or federated instances.
 */
(function (root, factory) {
  var api = factory(root || globalThis);
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.MuxplexMobileKeyboard = api;
    api.init();
  }
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';

  var STORAGE_KEY = 'muxplex_mobile_keybar_enabled';
  var initialized = false;
  var enabled = true;
  var toolbar = null;
  var normalGroup = null;
  var ctrlGroup = null;
  var settingCheckbox = null;
  var originalOpenTerminal = null;
  var originalCloseTerminal = null;
  var resizeFrame = null;

  var ARROW_CODES = { up: 'A', down: 'B', right: 'C', left: 'D' };

  var NORMAL_KEYS = [
    { label: 'Esc', sequence: '\x1b', className: 'mobile-keybar__key--escape', aria: 'Escape' },
    { label: 'Ctrl', action: 'ctrl', className: 'mobile-keybar__key--modifier', aria: 'Control key combinations' },
    { label: 'Tab', sequence: '\t', aria: 'Tab' },
    { label: '\u2191', arrow: 'up', aria: 'Up arrow' },
    { label: '\u2193', arrow: 'down', aria: 'Down arrow' },
    { label: '\u2190', arrow: 'left', aria: 'Left arrow' },
    { label: '\u2192', arrow: 'right', aria: 'Right arrow' },
    { label: 'PgUp', sequence: '\x1b[5~', aria: 'Page Up' },
    { label: 'PgDn', sequence: '\x1b[6~', aria: 'Page Down' },
    { label: 'Home', sequence: '\x1b[H', aria: 'Home' },
    { label: 'End', sequence: '\x1b[F', aria: 'End' },
    { label: 'Del', sequence: '\x1b[3~', aria: 'Delete' },
  ];

  // NOTE: there is deliberately no { control: 'v' } entry here, and there must
  // never be one. controlSequence('v') is 0x16/SYN, which is exactly the byte
  // frontend contract #1 exists to keep off the PTY — TUI apps then read the
  // *server-side* clipboard (the original "paste does nothing" bug). Pasting on
  // this bar goes through action:'paste' → _pasteFromClipboard(), which reads the
  // BROWSER clipboard and routes it through xterm's bracketed paste. The key is
  // labelled "Paste" rather than "Ctrl+V" so the label never invites someone to
  // "simplify" it back into the control path.
  var CTRL_KEYS = [
    { label: 'Esc', sequence: '\x1b', className: 'mobile-keybar__key--escape', aria: 'Escape' },
    { label: 'Back', action: 'normal', className: 'mobile-keybar__key--modifier', aria: 'Return to normal keys' },
    { label: 'Paste', action: 'paste', className: 'mobile-keybar__key--paste', aria: 'Paste from clipboard' },
    { label: 'Ctrl+C', control: 'c', aria: 'Control C' },
    { label: 'Ctrl+B', control: 'b', aria: 'Control B, default tmux prefix' },
    { label: 'Ctrl+A', control: 'a', aria: 'Control A' },
    { label: 'Ctrl+D', control: 'd', aria: 'Control D' },
    { label: 'Ctrl+L', control: 'l', aria: 'Control L' },
    { label: 'Ctrl+R', control: 'r', aria: 'Control R' },
    { label: 'Ctrl+U', control: 'u', aria: 'Control U' },
    { label: 'Ctrl+W', control: 'w', aria: 'Control W' },
    { label: 'Ctrl+Z', control: 'z', aria: 'Control Z' },
    { label: 'Ctrl+[', control: '[', aria: 'Control left bracket, Escape' },
    { label: 'Ctrl+\\', control: '\\', aria: 'Control backslash' },
  ];

  var STYLE_TEXT = [
    // Docked to the VISUAL viewport, not the layout viewport: iOS Safari overlays the
    // software keyboard instead of shrinking the layout viewport, so a bar positioned
    // at the layout bottom is drawn *under* the keyboard exactly when it is needed
    // most. JS keeps --keybar-lift equal to the keyboard's overlap so the bar rides
    // directly above it (and sits on the safe-area gutter when no keyboard is up).
    '.mobile-keybar{display:none;position:fixed;left:0;right:0;bottom:0;transform:translateY(calc(-1 * var(--keybar-lift,0px)));background:var(--bg-header,#0D1117);border-top:1px solid var(--border,#2A3040);padding:4px max(14px,env(safe-area-inset-right)) var(--keybar-pad-bottom,max(4px,env(safe-area-inset-bottom))) max(14px,env(safe-area-inset-left));z-index:24;}',
    '.mobile-keybar__group{display:flex;align-items:center;gap:4px;overflow-x:auto;overflow-y:hidden;scrollbar-width:none;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;touch-action:pan-x;padding:0 1px;}',
    '.mobile-keybar__group::-webkit-scrollbar{display:none;}',
    '.mobile-keybar__key{appearance:none;-webkit-appearance:none;flex:0 0 auto;min-width:42px;height:38px;padding:0 9px;border:1px solid var(--border,#2A3040);border-radius:6px;background:var(--bg-surface,#1A1F2B);color:var(--text,#F0F6FF);font:600 12px/1 var(--font-ui,system-ui,-apple-system,sans-serif);white-space:nowrap;user-select:none;-webkit-user-select:none;touch-action:manipulation;}',
    // Rounded display corners physically clip the outermost keys: give the first and
    // last key extra width so their reachable area matches the others.
    '.mobile-keybar__group>.mobile-keybar__key:first-child{min-width:56px;padding-left:16px;}',
    '.mobile-keybar__group>.mobile-keybar__key:last-child{min-width:56px;padding-right:16px;}',
    '.mobile-keybar__key:active{background:var(--accent-dim,rgba(0,217,245,.15));border-color:var(--accent,#00D9F5);transform:translateY(1px);}',
    '.mobile-keybar__key:focus-visible{outline:2px solid var(--accent,#00D9F5);outline-offset:1px;}',
    '.mobile-keybar__key--escape{border-color:rgba(241,166,64,.75);color:var(--bell,#F1A640);}',
    '.mobile-keybar__key--modifier{border-color:rgba(0,217,245,.55);color:var(--accent,#00D9F5);}',
    // Paste is an action, not a raw key — give it its own tint so it does not
    // read as another Ctrl+<letter> in the row.
    '.mobile-keybar__key--paste{border-color:rgba(63,185,80,.6);color:var(--ok,#3fb950);}',
    '.mobile-keybar__setting-label{display:flex;flex-direction:column;align-items:flex-start;gap:2px;}',
    '.mobile-keybar__setting-note{font-size:11px;font-weight:400;color:var(--text-muted,#8E95A3);}',
    '@media (max-width:899px) and (hover:none),(pointer:coarse){.mobile-keybar.mobile-keybar--enabled{display:block;}body.muxplex-mobile-keybar-enabled #session-pill:not(.hidden){bottom:calc(var(--keybar-lift,0px) + var(--keybar-height,54px) + 8px);}}',
    // The bar is fixed (out of flow) — reserve its height so it never covers terminal rows.
    'body.muxplex-mobile-keybar-enabled .terminal-wrapper{padding-bottom:var(--keybar-height,0px);}',
    '@media (max-height:500px) and (orientation:landscape){.mobile-keybar{padding-top:2px;padding-bottom:var(--keybar-pad-bottom,2px);}.mobile-keybar__key{height:32px;min-width:38px;padding:0 7px;font-size:11px;}.mobile-keybar__group>.mobile-keybar__key:first-child,.mobile-keybar__group>.mobile-keybar__key:last-child{min-width:50px;}}',
    '@media (prefers-reduced-motion:reduce){.mobile-keybar__key:active{transform:none;}}',
  ].join('\n');

  function getDocument() { return root && root.document ? root.document : null; }

  function getTerminal() {
    try { if (typeof _term !== 'undefined' && _term) return _term; } catch (_) {}
    return root && root._term ? root._term : null;
  }

  function getSocket() {
    try { if (typeof _ws !== 'undefined' && _ws) return _ws; } catch (_) {}
    return root && root._ws ? root._ws : null;
  }

  function getEncoder() {
    try { if (typeof _encodePayload === 'function') return _encodePayload; } catch (_) {}
    return root && typeof root._encodePayload === 'function' ? root._encodePayload : null;
  }

  // terminal.js is a classic script, so its top-level function declarations are
  // reachable both as bare identifiers and as window properties. Probe both, the
  // same way getTerminal()/getEncoder() do, so load order can never matter.
  function getPasteFromClipboard() {
    try { if (typeof _pasteFromClipboard === 'function') return _pasteFromClipboard; } catch (_) {}
    return root && typeof root._pasteFromClipboard === 'function' ? root._pasteFromClipboard : null;
  }

  function isSocketOpen(socket) {
    if (!socket) return false;
    var openState = 1;
    try { if (root.WebSocket && typeof root.WebSocket.OPEN === 'number') openState = root.WebSocket.OPEN; } catch (_) {}
    return socket.readyState === openState;
  }

  function focusTerminal() {
    var term = getTerminal();
    if (!term || typeof term.focus !== 'function') return;
    try { term.focus(); } catch (_) {}
  }

  function sendSequence(sequence) {
    if (typeof sequence !== 'string' || sequence.length === 0) return false;

    var term = getTerminal();
    if (term && typeof term.input === 'function') {
      try {
        term.input(sequence, true);
        focusTerminal();
        return true;
      } catch (_) {
        // A terminal may be disposed during a session switch. Fall through to
        // the guarded transport path rather than surfacing an exception.
      }
    }

    var socket = getSocket();
    var encodePayload = getEncoder();
    if (isSocketOpen(socket) && encodePayload) {
      try {
        socket.send(encodePayload(0x30, sequence));
        focusTerminal();
        return true;
      } catch (_) {
        // The existing reconnect loop owns socket recovery.
      }
    }

    return false;
  }

  // Paste the BROWSER clipboard. Never emits a control byte — see the note on
  // CTRL_KEYS and frontend contract #1. Runs inside the key's pointerdown, which
  // is the user gesture iOS requires before it will grant a clipboard read.
  function pasteFromClipboard() {
    var paste = getPasteFromClipboard();
    if (!paste) return false;
    var started = false;
    try { started = paste() !== false; } catch (_) { return false; }
    focusTerminal();
    return started;
  }

  function controlSequence(key) {
    if (typeof key !== 'string' || key.length !== 1) return null;
    var code = key.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) return String.fromCharCode(code & 0x1f);
    return null;
  }

  function arrowSequence(direction, applicationMode) {
    var code = ARROW_CODES[direction];
    if (!code) return null;
    return applicationMode ? '\x1bO' + code : '\x1b[' + code;
  }

  function currentArrowSequence(direction) {
    var term = getTerminal();
    var applicationMode = false;
    try { applicationMode = !!(term && term.modes && term.modes.applicationCursorKeysMode); } catch (_) {}
    return arrowSequence(direction, applicationMode);
  }

  function setMode(mode) {
    if (!normalGroup || !ctrlGroup) return;
    var ctrl = mode === 'ctrl';
    normalGroup.classList.toggle('hidden', ctrl);
    ctrlGroup.classList.toggle('hidden', !ctrl);
    try { (ctrl ? ctrlGroup : normalGroup).scrollLeft = 0; } catch (_) {}
    scheduleViewportFit();
  }

  function activateKey(definition) {
    if (!definition) return false;
    if (definition.action === 'ctrl') { setMode('ctrl'); return true; }
    if (definition.action === 'normal') { setMode('normal'); return true; }
    if (definition.action === 'paste') return pasteFromClipboard();

    var sequence = definition.sequence;
    if (definition.control) sequence = controlSequence(definition.control);
    if (definition.arrow) sequence = currentArrowSequence(definition.arrow);
    return sendSequence(sequence);
  }

  function makeButton(definition) {
    var doc = getDocument();
    var button = doc.createElement('button');
    button.type = 'button';
    button.className = 'mobile-keybar__key' + (definition.className ? ' ' + definition.className : '');
    button.textContent = definition.label;
    button.setAttribute('aria-label', definition.aria || definition.label);
    button.setAttribute('title', definition.aria || definition.label);

    button.addEventListener('pointerdown', function (event) {
      event.preventDefault();
      activateKey(definition);
    });
    button.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        activateKey(definition);
      }
    });
    return button;
  }

  function buildGroup(definitions, className) {
    var doc = getDocument();
    var group = doc.createElement('div');
    group.className = 'mobile-keybar__group ' + className;
    definitions.forEach(function (definition) { group.appendChild(makeButton(definition)); });
    return group;
  }

  function installStyles() {
    var doc = getDocument();
    if (!doc || doc.getElementById('mobile-keybar-styles')) return;
    var style = doc.createElement('style');
    style.id = 'mobile-keybar-styles';
    style.textContent = STYLE_TEXT;
    (doc.head || doc.documentElement).appendChild(style);
  }

  function createToolbar() {
    var doc = getDocument();
    if (!doc || toolbar) return toolbar;
    var wrapper = doc.querySelector('.terminal-wrapper');
    if (!wrapper) return null;

    toolbar = doc.createElement('div');
    toolbar.id = 'mobile-terminal-keybar';
    toolbar.className = 'mobile-keybar';
    toolbar.setAttribute('role', 'toolbar');
    toolbar.setAttribute('aria-label', 'Terminal special keys');

    normalGroup = buildGroup(NORMAL_KEYS, 'mobile-keybar__group--normal');
    ctrlGroup = buildGroup(CTRL_KEYS, 'mobile-keybar__group--ctrl hidden');
    toolbar.appendChild(normalGroup);
    toolbar.appendChild(ctrlGroup);
    wrapper.appendChild(toolbar);
    return toolbar;
  }

  function readPreference() {
    try { return root.localStorage.getItem(STORAGE_KEY) !== '0'; } catch (_) { return true; }
  }

  function writePreference(value) {
    try { root.localStorage.setItem(STORAGE_KEY, value ? '1' : '0'); } catch (_) {}
  }

  function setEnabled(value, persist) {
    enabled = value !== false;
    if (toolbar) {
      toolbar.classList.toggle('mobile-keybar--enabled', enabled);
      toolbar.setAttribute('aria-hidden', enabled ? 'false' : 'true');
    }
    var doc = getDocument();
    if (doc && doc.body) doc.body.classList.toggle('muxplex-mobile-keybar-enabled', enabled);
    if (settingCheckbox) settingCheckbox.checked = enabled;
    if (!enabled) setMode('normal');
    if (persist !== false) writePreference(enabled);
    scheduleViewportFit();
    return enabled;
  }

  function createSettingsToggle() {
    var doc = getDocument();
    if (!doc || settingCheckbox) return settingCheckbox;
    var panel = doc.querySelector('.settings-panel[data-tab="display"]');
    if (!panel) return null;

    var row = doc.createElement('div');
    row.className = 'settings-field';
    row.id = 'mobile-keybar-settings-row';

    var label = doc.createElement('label');
    label.className = 'settings-label mobile-keybar__setting-label';
    label.setAttribute('for', 'setting-mobile-keybar-enabled');
    label.appendChild(doc.createTextNode('Mobile terminal keybar'));

    var note = doc.createElement('span');
    note.className = 'mobile-keybar__setting-note';
    note.textContent = 'Esc, Ctrl, Tab and navigation keys; stored in this browser';
    label.appendChild(note);

    settingCheckbox = doc.createElement('input');
    settingCheckbox.type = 'checkbox';
    settingCheckbox.id = 'setting-mobile-keybar-enabled';
    settingCheckbox.className = 'settings-checkbox';
    settingCheckbox.checked = enabled;
    settingCheckbox.addEventListener('change', function () {
      setEnabled(settingCheckbox.checked, true);
      try {
        var toast = doc.getElementById('toast');
        if (toast) {
          toast.textContent = settingCheckbox.checked ? 'Mobile keybar enabled' : 'Mobile keybar disabled';
          toast.classList.remove('hidden');
          root.setTimeout(function () { toast.classList.add('hidden'); }, 3000);
        }
      } catch (_) {}
    });

    row.appendChild(label);
    row.appendChild(settingCheckbox);

    var autoViews = doc.getElementById('setting-auto-views-enabled');
    var anchor = autoViews && autoViews.closest ? autoViews.closest('.settings-field') : null;
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(row, anchor.nextSibling);
    else panel.appendChild(row);
    return settingCheckbox;
  }

  function toolbarHeight() {
    if (!toolbar) return 0;
    try {
      if (root.getComputedStyle && root.getComputedStyle(toolbar).display === 'none') return 0;
      return Math.ceil(toolbar.getBoundingClientRect().height || toolbar.offsetHeight || 0);
    } catch (_) { return toolbar.offsetHeight || 0; }
  }

  // How much of the layout viewport the software keyboard (or other browser UI) is
  // covering at the bottom. iOS Safari does NOT shrink the layout viewport for the
  // keyboard, so this is the only reliable signal; it is 0 with no keyboard up.
  function keyboardOverlap() {
    var visualViewport = root.visualViewport;
    if (!visualViewport) return 0;
    var layoutHeight = root.innerHeight || visualViewport.height;
    var covered = layoutHeight - (visualViewport.height + (visualViewport.offsetTop || 0));
    if (!isFinite(covered) || covered < 2) return 0;   // sub-2px is rounding noise
    return Math.round(covered);
  }

  function syncDock() {
    var doc = getDocument();
    if (!doc || !doc.documentElement) return 0;
    var overlap = keyboardOverlap();
    var style = doc.documentElement.style;
    try {
      style.setProperty('--keybar-lift', overlap + 'px');
      // With the keyboard up, the home-indicator gutter is covered by the keyboard —
      // padding for it there would just waste a row of screen.
      style.setProperty('--keybar-pad-bottom', overlap > 0 ? '4px' : '');
      style.setProperty('--keybar-height', toolbarHeight() + 'px');
    } catch (_) {}
    return overlap;
  }

  // This module does NOT size the terminal. terminal.js is the single owner of
  // #terminal-container's height (see the geometry note there and issue #15):
  // two owners racing here meant two fits, hence two tmux resizes per keyboard
  // event, with an intermediate geometry a keybar too tall. Our job is to keep
  // --keybar-lift/--keybar-height current and ask terminal.js to refit.
  function resizeForVisualViewport() {
    resizeFrame = null;
    syncDock();
    requestTerminalFit();
  }

  function requestTerminalFit() {
    try {
      if (typeof root._fitTerminalToViewport === 'function') root._fitTerminalToViewport();
    } catch (_) {}
  }

  function scheduleViewportFit() {
    if (!getDocument()) return;
    if (resizeFrame !== null && typeof root.cancelAnimationFrame === 'function') {
      try { root.cancelAnimationFrame(resizeFrame); } catch (_) {}
    }
    var schedule = typeof root.requestAnimationFrame === 'function'
      ? root.requestAnimationFrame.bind(root)
      : function (fn) { return root.setTimeout(fn, 0); };
    resizeFrame = schedule(resizeForVisualViewport);
  }

  // Viewport listeners intentionally live in terminal.js alongside the geometry
  // owner, so a single handler drives both the dock vars and the fit. Binding a
  // second set here is what produced the double-resize in issue #15.

  function installLifecycleHooks() {
    if (typeof root._openTerminal === 'function' && !originalOpenTerminal) {
      originalOpenTerminal = root._openTerminal;
      root._openTerminal = function () {
        var result = originalOpenTerminal.apply(this, arguments);
        setMode('normal');
        scheduleViewportFit();
        return result;
      };
    }

    if (typeof root._closeTerminal === 'function' && !originalCloseTerminal) {
      originalCloseTerminal = root._closeTerminal;
      root._closeTerminal = function () {
        var doc = getDocument();
        var container = doc && doc.getElementById('terminal-container');
        if (container) {
          try { container.style.height = ''; } catch (_) {}
        }
        setMode('normal');
        return originalCloseTerminal.apply(this, arguments);
      };
    }
  }

  function init() {
    if (initialized) return true;
    var doc = getDocument();
    if (!doc || typeof doc.createElement !== 'function') return false;

    try {
      installStyles();
      if (!createToolbar()) return false;
      enabled = readPreference();
      createSettingsToggle();
      installLifecycleHooks();
      initialized = true;
      setEnabled(enabled, false);
      return true;
    } catch (error) {
      // This enhancement must never prevent the core terminal or settings UI
      // from loading. Failure is contained to the optional keybar.
      try { console.warn('[mobile-keyboard] initialization skipped:', error); } catch (_) {}
      return false;
    }
  }

  return {
    init: init,
    setEnabled: setEnabled,
    isEnabled: function () { return enabled; },
    sendSequence: sendSequence,
    controlSequence: controlSequence,
    arrowSequence: arrowSequence,
    activateKey: activateKey,
    normalKeys: function () { return NORMAL_KEYS.slice(); },
    ctrlKeys: function () { return CTRL_KEYS.slice(); },
    resizeForVisualViewport: resizeForVisualViewport,
    keyboardOverlap: keyboardOverlap,
    syncDock: syncDock,
    toolbarHeight: toolbarHeight,
  };
});
