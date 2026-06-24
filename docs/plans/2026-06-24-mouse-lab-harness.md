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
| 6 | `diagLogging`     | console `[seldebug]` logging of every event + `hasSel`/`track` | OFF |

Lever 4 directly probes the paradox (does the tracking guard suppress the killer when it
shouldn't?). Lever 5 is the new candidate fix for Hypothesis A.

### Profiles (dropdown + Apply; one-shot, not enforced)

| Set | Levers on |
|---|---|
| Shipped (v0.9.5) | 1,2,3,4 |
| Baseline (raw bug) | none |
| Ignore tracking guard (Hyp. B) | 1,2,3 |
| tmux copy-mode clear (Hyp. A) | 1,2,3,4,5 |
| Diagnostics only | 6 |
| Everything on | all |

Applying writes the toggles once; flipping any lever afterward sets **Current → "Custom"**
(derived by exact-match against profiles — never falsely shows a profile you've diverged
from). UI: `initMouseLabUI` / `renderMouseLabUI` / `MOUSELAB_PROFILES` in `app.js`.

## Known limitation

Lever 5 fires only on **OS window refocus** (`window` blur/focus), **not** on switching
tmux sessions *inside* muxplex — those are same-window navigations with no blur/focus event.
So lever 5 addresses the "return to the muxplex window" trigger, not the in-app
session-switch trigger.

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
