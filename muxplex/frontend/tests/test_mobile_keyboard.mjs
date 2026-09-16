// Focused tests for mobile-keyboard.js terminal sequence handling.

import { createRequire } from 'node:module';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const require = createRequire(import.meta.url);
const modulePath = join(__dirname, '..', 'mobile-keyboard.js');

function loadFresh(rootOverrides = {}) {
  delete require.cache[require.resolve(modulePath)];

  const previous = {
    document: globalThis.document,
    localStorage: globalThis.localStorage,
    WebSocket: globalThis.WebSocket,
    _term: globalThis._term,
    _ws: globalThis._ws,
    _encodePayload: globalThis._encodePayload,
    _pasteFromClipboard: globalThis._pasteFromClipboard,
  };

  Object.assign(globalThis, rootOverrides);
  const api = require(modulePath);

  return {
    api,
    restore() {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete globalThis[key];
        else globalThis[key] = value;
      }
      delete require.cache[require.resolve(modulePath)];
    },
  };
}

test('controlSequence emits terminal control bytes', () => {
  const { api, restore } = loadFresh();
  try {
    assert.equal(api.controlSequence('a').charCodeAt(0), 1);
    assert.equal(api.controlSequence('b').charCodeAt(0), 2);
    assert.equal(api.controlSequence('c').charCodeAt(0), 3);
    assert.equal(api.controlSequence('[').charCodeAt(0), 27);
    assert.equal(api.controlSequence('\\').charCodeAt(0), 28);
    assert.equal(api.controlSequence('ab'), null);
  } finally {
    restore();
  }
});

test('arrowSequence supports normal and application cursor modes', () => {
  const { api, restore } = loadFresh();
  try {
    assert.equal(api.arrowSequence('up', false), '\x1b[A');
    assert.equal(api.arrowSequence('left', false), '\x1b[D');
    assert.equal(api.arrowSequence('down', true), '\x1bOB');
    assert.equal(api.arrowSequence('right', true), '\x1bOC');
    assert.equal(api.arrowSequence('unknown', false), null);
  } finally {
    restore();
  }
});

test('sendSequence prefers xterm input and refocuses terminal', () => {
  const calls = [];
  const term = {
    input(data, wasUserInput) { calls.push(['input', data, wasUserInput]); },
    focus() { calls.push(['focus']); },
  };
  const { api, restore } = loadFresh({ _term: term });
  try {
    assert.equal(api.sendSequence('\x1b'), true);
    assert.deepEqual(calls, [['input', '\x1b', true], ['focus']]);
  } finally {
    restore();
  }
});

test('sendSequence falls back to ttyd transport when xterm input is unavailable', () => {
  const sent = [];
  const socket = { readyState: 1, send(payload) { sent.push(payload); } };
  const encoder = (type, data) => ({ type, data });
  const { api, restore } = loadFresh({
    _term: null,
    _ws: socket,
    _encodePayload: encoder,
    WebSocket: { OPEN: 1 },
  });
  try {
    assert.equal(api.sendSequence('\t'), true);
    assert.deepEqual(sent, [{ type: 0x30, data: '\t' }]);
  } finally {
    restore();
  }
});

test('sendSequence is a safe no-op with no terminal or open socket', () => {
  const { api, restore } = loadFresh({ _term: null, _ws: null });
  try {
    assert.equal(api.sendSequence('\x1b'), false);
    assert.equal(api.sendSequence(''), false);
  } finally {
    restore();
  }
});

test('init degrades safely when no browser DOM exists', () => {
  const { api, restore } = loadFresh({ document: undefined });
  try {
    assert.equal(api.init(), false);
  } finally {
    restore();
  }
});

test('keyboardOverlap reports the software keyboard height, and 0 when it is down', () => {
  const previousVV = globalThis.visualViewport;
  const previousIH = globalThis.innerHeight;
  const { api, restore } = loadFresh();
  try {
    globalThis.innerHeight = 844;

    // Keyboard down: visual viewport fills the layout viewport.
    globalThis.visualViewport = { height: 844, offsetTop: 0 };
    assert.equal(api.keyboardOverlap(), 0);

    // Sub-pixel rounding must not be mistaken for a keyboard.
    globalThis.visualViewport = { height: 843.4, offsetTop: 0 };
    assert.equal(api.keyboardOverlap(), 0);

    // Keyboard up: iOS keeps the layout viewport at 844 and shrinks only the
    // visual viewport — this delta is what the bar must be lifted by.
    globalThis.visualViewport = { height: 508, offsetTop: 0 };
    assert.equal(api.keyboardOverlap(), 336);

    // No visualViewport support at all: never lift.
    globalThis.visualViewport = undefined;
    assert.equal(api.keyboardOverlap(), 0);
  } finally {
    globalThis.visualViewport = previousVV;
    globalThis.innerHeight = previousIH;
    restore();
  }
});

test('syncDock publishes lift/height vars and drops the safe-area pad while the keyboard is up', () => {
  const previousVV = globalThis.visualViewport;
  const previousIH = globalThis.innerHeight;
  const props = new Map();
  const { api, restore } = loadFresh({
    document: {
      documentElement: { style: { setProperty: (k, v) => props.set(k, v) } },
      createElement: () => ({ style: {}, classList: { toggle() {}, add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
      querySelector: () => null,
      getElementById: () => null,
      head: { appendChild() {} },
    },
  });
  try {
    globalThis.innerHeight = 844;
    globalThis.visualViewport = { height: 508, offsetTop: 0 };
    assert.equal(api.syncDock(), 336);
    assert.equal(props.get('--keybar-lift'), '336px');
    assert.equal(props.get('--keybar-pad-bottom'), '4px');

    globalThis.visualViewport = { height: 844, offsetTop: 0 };
    assert.equal(api.syncDock(), 0);
    assert.equal(props.get('--keybar-lift'), '0px');
    assert.equal(props.get('--keybar-pad-bottom'), '');   // falls back to safe-area inset
    assert.ok(props.has('--keybar-height'));
  } finally {
    globalThis.visualViewport = previousVV;
    globalThis.innerHeight = previousIH;
    restore();
  }
});

// ---------------------------------------------------------------------------
// Paste key (frontend contract #1)
//
// The bar must never put 0x16/SYN on the PTY: that is the raw Ctrl+V byte that
// makes TUI apps read the SERVER-side clipboard — the original "paste does
// nothing" bug. Pasting goes through _pasteFromClipboard() instead.
// ---------------------------------------------------------------------------

test('ctrl group offers a Paste key and never a control-byte Ctrl+V', () => {
  const { api, restore } = loadFresh();
  try {
    const ctrl = api.ctrlKeys();

    const paste = ctrl.find((k) => k.action === 'paste');
    assert.ok(paste, 'ctrl group must expose a paste action');
    assert.equal(paste.label, 'Paste',
      'label must not read "Ctrl+V" — it does not send Ctrl+V');

    // No definition anywhere on the bar may map to the v control byte.
    for (const group of [api.normalKeys(), api.ctrlKeys()]) {
      for (const key of group) {
        assert.notEqual(String(key.control || '').toLowerCase(), 'v',
          'contract #1: no key may emit 0x16/SYN');
      }
    }
  } finally {
    restore();
  }
});

test('Paste sits at the left of the ctrl group, beside Ctrl+C', () => {
  const { api, restore } = loadFresh();
  try {
    const labels = api.ctrlKeys().map((k) => k.label);
    assert.deepEqual(labels.slice(0, 4), ['Esc', 'Back', 'Paste', 'Ctrl+C'],
      'paste and interrupt must be reachable without horizontal scrolling');
  } finally {
    restore();
  }
});

test('activating Paste reads the browser clipboard and sends nothing to the PTY', () => {
  const written = [];
  const term = {
    input(data) { written.push(data); },
    focus() {},
  };
  let pasteCalls = 0;
  const { api, restore } = loadFresh({
    _term: term,
    _pasteFromClipboard() { pasteCalls += 1; return true; },
  });
  try {
    const paste = api.ctrlKeys().find((k) => k.action === 'paste');
    assert.equal(api.activateKey(paste), true);
    assert.equal(pasteCalls, 1, 'must route through _pasteFromClipboard');
    assert.deepEqual(written, [],
      'contract #1: the paste path must not write to the terminal/PTY directly');
  } finally {
    restore();
  }
});

test('Paste degrades safely when the clipboard helper is unavailable', () => {
  const { api, restore } = loadFresh({ _term: null, _ws: null, _pasteFromClipboard: undefined });
  try {
    const paste = api.ctrlKeys().find((k) => k.action === 'paste');
    assert.equal(api.activateKey(paste), false);
  } finally {
    restore();
  }
});

// ---------------------------------------------------------------------------
// Terminal geometry is owned by terminal.js alone (issue #15)
//
// This module must NOT size #terminal-container or call fit(). Two owners racing
// meant two fits per keyboard event, so tmux resized twice and every TUI redrew
// for an intermediate geometry a keybar too tall — which drew the prompt under
// the keybar.
// ---------------------------------------------------------------------------

function geometryHarness() {
  const container = { style: {} };
  const fits = [];
  const props = new Map();
  return {
    container,
    fits,
    props,
    overrides: {
      _term: { input() {}, focus() {} },
      _fitAddon: { fit() { fits.push('keybar-fit'); } },
      _fitTerminalToViewport() { fits.push('requested'); },
      document: {
        documentElement: { style: { setProperty: (k, v) => props.set(k, v) } },
        createElement: () => ({
          style: {}, classList: { toggle() {}, add() {}, remove() {} },
          setAttribute() {}, appendChild() {},
        }),
        querySelector: () => null,
        getElementById: (id) => (id === 'terminal-container' ? container : null),
        head: { appendChild() {} },
      },
    },
  };
}

test('resizeForVisualViewport never sizes the terminal or calls fit itself', () => {
  const h = geometryHarness();
  const previousVV = globalThis.visualViewport;
  const previousIH = globalThis.innerHeight;
  const { api, restore } = loadFresh(h.overrides);
  try {
    globalThis.innerHeight = 844;
    globalThis.visualViewport = { height: 508, offsetTop: 0 };

    api.resizeForVisualViewport();

    assert.equal(h.container.style.height, undefined,
      'issue #15: this module must not own #terminal-container height');
    assert.ok(!h.fits.includes('keybar-fit'),
      'issue #15: this module must not call fitAddon.fit()');
  } finally {
    globalThis.visualViewport = previousVV;
    globalThis.innerHeight = previousIH;
    restore();
  }
});

test('resizeForVisualViewport syncs the dock and delegates the fit to terminal.js', () => {
  const h = geometryHarness();
  const previousVV = globalThis.visualViewport;
  const previousIH = globalThis.innerHeight;
  const { api, restore } = loadFresh(h.overrides);
  try {
    globalThis.innerHeight = 844;
    globalThis.visualViewport = { height: 508, offsetTop: 0 };

    api.resizeForVisualViewport();

    assert.equal(h.props.get('--keybar-lift'), '336px', 'dock vars stay this module\'s job');
    assert.deepEqual(h.fits, ['requested'], 'exactly one refit request, delegated');
  } finally {
    globalThis.visualViewport = previousVV;
    globalThis.innerHeight = previousIH;
    restore();
  }
});

test('resizeForVisualViewport is a safe no-op when terminal.js exposes no fitter', () => {
  const h = geometryHarness();
  delete h.overrides._fitTerminalToViewport;
  const { api, restore } = loadFresh(h.overrides);
  try {
    assert.doesNotThrow(() => api.resizeForVisualViewport());
  } finally {
    restore();
  }
});

test('toolbarHeight is exported so terminal.js can subtract the keybar', () => {
  const { api, restore } = loadFresh();
  try {
    assert.equal(typeof api.toolbarHeight, 'function');
    assert.equal(api.toolbarHeight(), 0, 'no toolbar built in this harness → contributes nothing');
  } finally {
    restore();
  }
});
