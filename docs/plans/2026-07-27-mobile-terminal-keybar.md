# Mobile terminal keybar (v0.9.6.dev5)

**Status: IMPLEMENTED (2026-07-28)** — shipped via PR #9 (merge `fd54427`).

A touch-friendly row of terminal control keys (Esc, Ctrl-combos, Tab, arrows, PgUp/PgDn,
Home/End, Del) for phones and tablets, where the stock software keyboard offers none of
them. Entirely a frontend enhancement: no xterm.js fork, no ttyd change, no backend change.

## Shape

| Piece | Where |
|---|---|
| Whole feature | `muxplex/frontend/mobile-keyboard.js` (self-contained UMD module) |
| Load | `<script src="/mobile-keyboard.js" defer>` in `index.html` |
| Tests | `muxplex/frontend/tests/test_mobile_keyboard.mjs` |

The module builds its own DOM, injects its own `<style>`, and appends itself to
`.terminal-wrapper`. It is **fail-soft by construction**: `init()` wraps everything in a
try/catch and returns false on any error, so a broken keybar can never prevent the
terminal or settings UI from loading.

**Enablement is per-browser** (`localStorage` key `muxplex_mobile_keybar_enabled`),
surfaced as Settings → Display → "Mobile terminal keybar". Deliberately *not* a server
setting: it is a per-device ergonomic choice, and a device-local toggle is also a failsafe
— turning it off on the phone cannot disturb any other client or federated peer.

Two key groups (normal / Ctrl) swap in place rather than wrapping to a second row, keeping
the bar one row tall on every device.

## The two on-device problems, and why the fixes look the way they do

Both were found only by testing on a physical iPhone; neither reproduces on a desktop
browser's device-emulation mode.

### 1. Rounded display corners clip the outermost keys

The bar sits in the home-indicator gutter. On a rounded-corner display, part of the first
and last key's rectangle is behind the corner arc — physically nonexistent screen, so
those taps land nowhere.

Fix: the bar pads its sides by `max(14px, env(safe-area-inset-left/right))` so no key is
ever drawn into the arc, and the first/last keys carry a larger `min-width` (56px; 50px in
landscape) with extra outer padding.

### 2. The software keyboard buried the bar — the substantive one

**iOS Safari does not shrink the layout viewport when the software keyboard appears; it
overlays it.** `window.innerHeight` is unchanged, `100dvh` is unchanged, and normal
document flow knows nothing about the keyboard. So a bar positioned at the bottom of the
layout viewport is *guaranteed* to be covered by the keyboard — and the keyboard is up
precisely when Esc/Ctrl are most needed, which made the feature close to useless.

Fix: dock the bar to the **visual viewport** instead.

- `keyboardOverlap()` = `innerHeight - (visualViewport.height + visualViewport.offsetTop)`
  — the keyboard's true height, `0` when it is down. A 2px floor rejects rounding noise.
- `syncDock()` publishes it as the CSS var `--keybar-lift`; the bar is `position: fixed`
  at `bottom: 0` and rides `translateY(calc(-1 * var(--keybar-lift)))`, landing directly
  on top of the keyboard.
- While the keyboard is up, the safe-area bottom padding is dropped
  (`--keybar-pad-bottom`): the keyboard already covers the home-indicator gutter, so
  padding for it there only wastes a row of screen.
- **`visualViewport`'s `scroll` event is observed alongside `resize`.** iOS reports
  keyboard show/hide as an `offsetTop` change — a *scroll* — as often as a resize. With
  only `resize` bound, the bar visibly lags behind the keyboard.

Because the bar is now `position: fixed` it left normal flow, so `--keybar-height`
reserves its space via `.terminal-wrapper` padding, and the session pill offsets by
lift + height. The bar remains a **DOM child of `.terminal-wrapper`**, which matters: it
inherits `#view-expanded.hidden`'s `display: none !important`, so it disappears on the
dashboard with no extra gating code.

## Notes for future work

- Terminal sizing: `resizeForVisualViewport()` sets `#terminal-container`'s height from
  `visualViewport.height` minus header, search bar, and keybar, then calls `fitAddon.fit()`.
  The wrapper's `--keybar-height` padding and that subtraction are complementary, not
  double-counted — the container height excludes the bar, the padding holds its space.
- Key input goes through `term.input(seq, true)` with a guarded fallback to
  `socket.send(_encodePayload(0x30, seq))` for the window where a terminal has been
  disposed mid-session-switch.
- Arrow keys respect `applicationCursorKeysMode` (`ESC O A` vs `ESC [ A`), so they work
  in both the shell and full-screen TUI apps.
- Buttons act on `pointerdown` with `preventDefault()`, which keeps focus in the terminal
  and avoids the ~300ms tap delay.

## Anything bottom-docked must repeat the visual-viewport dance

If another bottom-anchored mobile affordance is ever added, it faces the same iOS
behavior. Reuse `--keybar-lift` / `keyboardOverlap()` rather than re-deriving it, and
remember `safe-area-inset-bottom` is *wrong* while a keyboard is up.
