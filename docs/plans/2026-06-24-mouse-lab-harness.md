# Mouse Lab — experimental selection-fix harness (v0.9.6.dev2)

**Status: IN PROGRESS — awaiting user's over-time testing.** Built 2026-06-24 on branch
`feat/v0.9-session-ux`. Not yet committed at time of writing; dev version `0.9.6.dev2`.

## Why this exists

The "returning to the terminal selects a huge block of text from a stale anchor" bug has
survived three fixes (v0.9.3 drag threshold, v0.9.4 focus reset, v0.9.5 focus-independent
`e.buttons===0` zombie-killer). All three are **xterm.js-side** fixes that call
`_term.clearSelection()` and are gated on `!inMouseTracking()`.

This session surfaced a reframing that may explain why they haven't stuck. The user's
`~/.tmux.conf` has **`set -g mouse on`** (plus a "keep selection visible on drag-end"
binding). With tmux mouse mode on, tmux enables mouse tracking on its host terminal — which
in muxplex's stack is **xterm.js itself**:

```
browser xterm.js  ←  ttyd  ←  tmux (mouse on)  ←  Claude Code CLI
   "the terminal"                  ↑ both emit mouse-tracking DECSET sequences upward
```

So `_term.modes.mouseTrackingMode` *should* be non-`'none'` most of the time, which would
make all three prior fixes **bail** (they stand aside under tracking) or **target the wrong
layer**. The research doc `docs/Claude Code + tmux + Mouse.md` (a Claude-Code/tmux mouse
study) prompted this — its env-var/renderer fixes do **not** apply to muxplex (different
stack: browser xterm.js, not a native terminal), but its central theme — *who owns the
mouse: the selection layer, or an app via mouse-tracking escape sequences* — does.

## The two live hypotheses

- **Hypothesis A — wrong layer.** The visible highlight is **tmux copy-mode** (server-side),
  not xterm's selection. A drag whose `mouseup` is lost leaves *tmux* in copy-mode with a
  stale selection (the user's "keep selection visible" binding makes this plausible).
  `_term.clearSelection()` cannot clear that. The fix is to **send Esc to the PTY** to
  cancel tmux copy-mode.
- **Hypothesis B — mode desync.** The tracking DECSET sequences aren't faithfully reaching
  xterm (ttyd relay gap, or Claude Code toggling its own mode underneath tmux), so xterm
  sees `'none'`, does browser-side selection, the zombie forms, and our guard mistimes.

**Decisive test:** at the moment the bad highlight is visible, is `_term.hasSelection()`
true? **false + visible highlight ⇒ Hypothesis A** (and the entire xterm-side approach is
aimed at the wrong layer). The `?seldebug=1` / lever-6 diagnostic now logs `hasSel=` and
`track=` (mouseTrackingMode) on every mouse event for exactly this read.

## The harness

A per-device, `localStorage`-backed Settings → **Mouse Lab** tab. Config lives in
`window.MouseLab` (terminal.js, key `muxplex_mouselab`); mouse handlers read it live at
gesture time, so toggles take effect on the next mouse action — no reload. Changes in one
browser tab propagate via the `storage` event + a `muxplex:mouselab-changed` custom event.

### Levers (defaults reproduce shipped v0.9.5 behavior → tests stay green)

| # | key | Gates | Default |
|---|---|---|---|
| 1 | `dragThreshold`   | ~5px suppressor: sub-threshold left press = focus click, no select | ON |
| 2 | `zombieKiller`    | buttonless-mousemove (`e.buttons===0`) kill of a stale xterm drag | ON |
| 3 | `focusClickClear` | a focus-only click drops any stray selection + refocuses | ON |
| 4 | `honorTracking`   | when ON, levers 1–3 bail under `mouseTrackingMode`; **OFF = act regardless** | ON |
| 5 | `tmuxCopyClear`   | **NEW** — on window refocus after a press was lost outside the window, send Esc to PTY to cancel tmux copy-mode (**Hypothesis A fix**) | OFF |
| 6 | `rightClickPassThru` | **NEW (dev4)** — when an app owns the mouse (`mouseTrackingMode != none`), muxplex suppresses the browser menu but does NOT run its own right-click copy/paste — lets the forwarded click reach the app (**right-click double-paste fix**, see below) | OFF |
| 7 | `diagLogging`     | console `[seldebug]` logging of every event + `hasSel`/`track`, plus the paste-path probes (`_pasteFromClipboard`, `onData→PTY`, right-click branch) | OFF |

Lever 4 directly probes the paradox (does the tracking guard suppress the killer when it
shouldn't?). Lever 5 is the candidate fix for Hypothesis A (stale-selection). Lever 6 is the
candidate fix for the **second, distinct bug** (right-click double-paste) documented below.

### Profiles (dropdown + Apply; one-shot, not enforced)

| Set | Levers on |
|---|---|
| Shipped (v0.9.5) | 1,2,3,4 |
| Baseline (raw bug) | none |
| Ignore tracking guard (Hyp. B) | 1,2,3 |
| tmux copy-mode clear (Hyp. A) | 1,2,3,4,5 |
| Right-click pass-through (double-paste fix) | 1,2,3,4,6 |
| Diagnostics only | 7 |
| Everything on | all |

Applying writes the toggles once; flipping any lever afterward sets **Current → "Custom"**
(derived by exact-match against profiles — never falsely shows a profile you've diverged
from). UI: `initMouseLabUI` / `renderMouseLabUI` / `MOUSELAB_PROFILES` in `app.js`.

## Known limitation

Lever 5 fires only on **OS window refocus** (`window` blur/focus), **not** on switching
tmux sessions *inside* muxplex — those are same-window navigations with no blur/focus event.
So lever 5 addresses the "return to the muxplex window" trigger, not the in-app
session-switch trigger.

## Second bug: right-click double-paste (dev3–dev4, distinct from stale-selection)

**Symptom:** right-click-paste inserts the clipboard text **twice**; Ctrl+V pastes once.
**Started when** the user accepted Claude Code's **fullscreen** prompt (`~/.claude/settings.json`
→ `"tui": "fullscreen"`), which turns Claude Code's **mouse capture ON**.

**Mechanism:** with Claude Code capturing the mouse and tmux `mouse on`, xterm.js is in
mouse-tracking mode, so a right-click is handled by **two** layers at once — muxplex's
`contextmenu` handler pastes the browser clipboard via `_term.paste()`, **and** xterm.js
forwards the right-click to the PTY → tmux → Claude Code. Ctrl+V is immune because it's a
keystroke (no mouse event forwarded). The code asymmetry that allowed it: the **left-click**
selection handler already bails under `mouseTrackingMode` (contract #4b), but
`initRightClickCopyPaste` (contract #2) never had that guard.

**Open mechanism question:** the forwarded right-click is a position report — it cannot carry
the browser clipboard text, so it's unclear how Claude Code reproduces the *same* text. So
two outcomes are possible and the lever-6 test distinguishes them:
- **double → single** when lever 6 is ON ⇒ the app was the second paster; lever 6 is the fix.
- **double → zero** (nothing pastes) ⇒ the app does NOT paste; muxplex was double-sending —
  fix the other way (keep muxplex's paste, stop xterm forwarding the right-click).

**dev3 diagnostics (lever 7 / `?seldebug=1`)** pin it precisely: `_pasteFromClipboard()` call
count, `onData→PTY` (JSON.stringify exposes `\e[200~…\e[201~` bracketed paste vs `\e[<2;…M`
mouse), and the right-click branch + `mouseTrackingMode`. One right-click with logging on
shows whether the clipboard text is sent once or twice.

**Fix lever (6 / `rightClickPassThru`, default OFF):** when ON and `mouseTrackingMode != none`,
`initRightClickCopyPaste` suppresses the browser menu but does not copy/paste — the app owns
the right-click. Raw tracking check (independent of the left-click `honorTracking` lever).

**Non-muxplex confirmation of the trigger:** launch Claude Code with `CLAUDE_CODE_DISABLE_MOUSE=1`
(keeps fullscreen rendering, drops mouse capture). If the double-paste vanishes, Claude Code's
fullscreen mouse capture is confirmed as the second handler. The muxplex lever is the better
permanent fix — it keeps Claude Code's fullscreen mouse features working.

## Suggested test order

1. **Diagnostics only** → reproduce once → read `hasSel`/`track` in the console. This may
   settle A vs B before any extended per-profile testing and collapse the matrix.
2. If A: test **tmux copy-mode clear**. If B: test **Ignore tracking guard**.
3. **Baseline** confirms the bug still reproduces raw (control).

## Where things live (code map)

- `terminal.js`: `window.MouseLab` IIFE (config + `get`/`all`/`save`/`reload`);
  `initDeliberateSelection` (levers 1–5, `ML()` reader, `inMouseTracking()` now folds
  lever 4); `initSelectionDebug` (lever 6, always-attached + `isOn()`-gated).
- `index.html`: `<button data-tab="mouselab">` + `<div class="settings-panel" data-tab="mouselab">`.
- `app.js`: `MOUSELAB_LEVERS`, `MOUSELAB_PROFILES`, `_mouselabMatchProfile`,
  `renderMouseLabUI`, `initMouseLabUI` (wired in the tab-init block; re-synced in
  `switchSettingsTab`).
- `tests/test_app.mjs`: tab-count assertion updated 5 → 6.

## Cleanup when a winner is picked

Once the bug is settled: bake the winning lever(s) into the unconditional code path, update
CLAUDE.md contract #4b, remove the Mouse Lab tab + `window.MouseLab` + the `?seldebug`
diagnostic, restore the tab-count test, and cut a real (non-`dev`) version + CHANGELOG entry.
