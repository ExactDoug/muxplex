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
