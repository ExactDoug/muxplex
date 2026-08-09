# muxplex — Agent Guide

Web-based tmux session dashboard: a FastAPI backend proxying ttyd WebSockets to an
xterm.js frontend, with multi-device federation, PAM/password auth, TLS, and
user-defined session Views.

**This repo (`ExactDoug/muxplex`) is a fork of `bkrabach/muxplex`** carrying UI/UX
improvements. Current version: **0.9.6.dev6**, on **`main`** — a **dev/experimental**
build carrying the Mouse Lab selection-fix harness *and* the mobile terminal keybar (both
below); last released version is **0.9.5**. All feature branches through PR #12 are
merged and their branches/worktrees deleted — **start new work from `main`**.

---

## ⇢ CURRENT STATE (2026-08-09)

**Just landed — resource efficiency (PRs #11, #12, both merged).** The poll cycle is now
**O(1) in session count**. Per-cycle tmux spawns went `2N+2` → `N+3` → **`C+3`** where C =
panes that actually changed; measured on a live 45-session fleet as 45 captures cold then
**1** on the next cycle. Full design, measurements, rejected options and a revision log:
**`docs/plans/2026-08-08-resource-efficiency-plan.md`**. Contracts that came out of it are
in "Hard-won backend contracts" below — read those before touching the poll cycle.

**Just fixed — the "Reconnecting…" infinite loop (v0.9.6.dev6, branch
`investigate/terminal-reconnect-loop`).** Killing/exiting the process a tmux session was
invoked to run destroyed the tmux session, so ttyd's `tmux attach` failed forever and the
terminal retried every ~15 s with no explanation. The hypothesis in the briefing was
verified and correct. Fix is frontend-only — the backend was already reporting the truth
(a 404 from `/connect`) and it was being discarded. Full write-up, including the one thing
the briefing got wrong:
**`docs/plans/2026-08-09-terminal-reconnect-loop-investigation.md`** (§"Verification and
outcome"). See frontend contract #8 below.

**Server:** normally run detached — `setsid nohup .venv/bin/muxplex serve >> ~/.local/state/muxplex/serve.log 2>&1 &`.

---

**v0.9 session UX (DONE on `feat/v0.9-session-ux`)** — see `CHANGELOG.md` v0.9.0–v0.9.2:
(1) new sessions reliably auto-open (createNewSession poll now keys off the canonical
`device_id:name` sessionKey and waits ~120s); (2) **session rename** —
`POST /api/sessions/{name}/rename` (`tmux rename-session`) with an atomic cascade
(`views.rename_session_key` → view membership + `hidden_sessions`; plus state
`active_session`/`session_order`/bell/`viewing_session`); reachable from the tile
flyout (grid + sidebar) and the expanded-header session dropdown (✎). **Local-only**
in v0.9 (remote rename would stale peers' keys); (3) View pills carry a leading ⧉
glyph (auto-views keep 📁) to read distinctly from session pills. Also: narrow-viewport
idle session-name contrast bumped `--text-dim`→`--text-muted`.
**v0.9.1**: cross-browser session-view convergence (`reconcileViewingSession`) +
suppressed redundant hover preview. **v0.9.2**: cwd auto-grouping now spans git
worktrees — `resolve_git_repo` resolves a linked worktree's `.git`-*file* to the
**main repo** name (see auto-views contract #6 below), so worktree sessions group
with their parent repo instead of forming a lone `dir:<worktree>` view.
**v0.9.3**: terminal mouse fixes — a focus-click no longer starts a text selection
(`initDeliberateSelection` drag threshold, contract #4b) and right-click-to-copy never
also pastes (OR-based contextmenu gate, contract #2). **v0.9.4**: returning focus to a
terminal no longer drags a selection from a stale anchor — first click after refocus is
a reset+focus click; stale drags are torn down on focus loss (contract #4b part B).
**v0.9.5**: that v0.9.4 focus approach didn't actually work — root-caused to xterm
extending selections on buttonless mousemoves (no physical-button check); replaced with
a focus-independent zombie-drag killer keyed on `e.buttons === 0` (contract #4b part B).
**v0.9.6.dev2 (IN PROGRESS, uncommitted-then-committed this checkpoint):** the v0.9.5 fix
may still not stick because the user's tmux has **`set -g mouse on`**, so xterm.js is in
mouse-tracking mode most of the time and the xterm-side fixes bail / may target the wrong
layer. Two hypotheses — **A:** the stale highlight is **tmux copy-mode** (server-side), not
xterm's, so `_term.clearSelection()` is aimed wrong (fix = send Esc to PTY); **B:** tracking
mode desync. Decisive read: `_term.hasSelection()` false while a highlight is visible ⇒ A.
Built a **Mouse Lab** harness (Settings tab) — per-device localStorage toggles + profiles
to A/B-test fixes live without reloads; diagnostics now log `hasSel`/`track`.
**Defaults reproduce shipped v0.9.5 behavior (tests stay green).** Design + test plan:
`docs/plans/2026-06-24-mouse-lab-harness.md`. Awaiting the user's over-time testing before
a winning fix is baked in and the harness removed.
**v0.9.6.dev3–dev4 (committed): a SECOND, distinct bug — right-click double-paste.** It
started when the user accepted Claude Code's **fullscreen** prompt (`~/.claude/settings.json`
`tui:fullscreen`), which turns Claude Code's **mouse capture ON**; with tmux `mouse on`, a
right-click is handled by BOTH muxplex's `contextmenu` paste AND the click forwarded to
Claude Code → double paste (Ctrl+V is immune — keystroke, not forwarded). dev3 added
paste-path diagnostics (`_pasteFromClipboard`/`onData→PTY`/right-click branch, gated by
lever 7). dev4 added **lever 6 `rightClickPassThru`** (default OFF): when an app owns the
mouse, muxplex suppresses the browser menu but lets the forwarded right-click reach the app
instead of also pasting. The Mouse Lab now has **7 levers + 7 profiles**. Open question the
lever-6 test settles: ON makes double→single ⇒ fix confirmed; double→zero ⇒ muxplex was
double-sending (different fix).

**v0.9.6.dev5 (MERGED, PR #9): mobile terminal keybar** — a one-row bar of terminal
control keys (Esc/Tab/arrows/PgUp/PgDn/Home/End/Del + a swap-in-place Ctrl group) for
phones, all in `muxplex/frontend/mobile-keyboard.js`, enabled per-browser via Settings →
Display. Two iPhone-only bugs were found and fixed on-device (neither reproduces in
desktop device-emulation): rounded display corners clipped the outermost keys, and the
software keyboard buried the whole bar — see **contract #7** below for the
visual-viewport rule that came out of it. Design doc:
`docs/plans/2026-07-27-mobile-terminal-keybar.md`. Current version: **0.9.6.dev5**.

## Running locally (development)

```bash
uv sync --extra dev          # one-time; dev extra needed for bs4 (HTML tests)
uv run muxplex serve         # http://127.0.0.1:8088 — settings from ~/.config/muxplex/settings.json
```

- `uv run` installs the project editable: frontend files in `muxplex/frontend/` are
  served live — a browser refresh picks up edits, no restart needed (backend `.py`
  changes DO need a restart).
- **For a long-lived server, invoke `.venv/bin/muxplex serve` directly** (or use the
  systemd unit). `uv run` leaves a **~30 MB supervisor process** resident for the whole
  lifetime of the server — more than half the server's own ~58 MB RSS, for no runtime
  benefit once the environment is resolved. `uv run` remains the right call for one-shot
  commands and tests. Measured 2026-08-08; see
  `docs/plans/2026-08-08-resource-efficiency-plan.md` item 5.1.
- **Stopping a foreground `serve` leaves no orphan** as of the Phase 0/1 efficiency work:
  ttyd is spawned with `start_new_session=True` (so it survives the spawning HTTP
  request) and therefore does NOT receive the Ctrl-C SIGINT; the lifespan shutdown now
  SIGTERMs it explicitly. That kill is deliberately **single-PID** — it detaches the tmux
  client, sessions and panes keep running. Never widen it to a process-group or
  cgroup-wide kill (that is upstream issue #7, which destroys hosted sessions).
- **Browser caching gotcha:** assets are served with `?v=<package version>` and no
  `Cache-Control` header. Same version ⇒ browsers replay cached JS without
  revalidating. When testing frontend changes, hard-refresh (`Ctrl+Shift+R`) or bump
  `version` in `pyproject.toml` (rotates the cache-buster for every client).
- Production-style usage: `uvx --refresh --from git+https://github.com/exactdoug/muxplex muxplex`
  (`--refresh` required or uvx replays its cached build).

## Tests

```bash
uv run pytest -q -m "not integration"              # Python suite (~1427 tests, all green)
node muxplex/frontend/tests/test_app.mjs           # frontend app logic (523 tests)
node muxplex/frontend/tests/test_terminal.mjs      # terminal/xterm contracts (26 pass / 27 fail)
node muxplex/frontend/tests/test_mobile_keyboard.mjs # mobile keybar (8 tests)
```

⚠️ **One pre-existing failure — NOT a regression.** `test_terminal.mjs` has **27 harness
failures**, all from one root cause: `container.addEventListener is not a function` at
`terminal.js:733` during module require (a DOM-stub gap in the mock, not product code).
Diff failing test names against a clean checkout before blaming a change. Everything else
should be green — the Python suite and the other two JS suites pass completely.

**`uv run pytest` broken with `ModuleNotFoundError` / `PackageNotFoundError`?** Check
`head -1 .venv/bin/pytest`. Venvs created before the 2026-06-17 ext4 migration have
console-script shebangs pointing at the dead `/mnt/c/dev/...` overlay path, so the script
runs under the wrong interpreter and cannot see the venv's packages. `uv sync` will NOT
fix it — it audits *packages* (which are fine) and never rewrites shims. Recreate the
venv: `rm -rf .venv && uv sync --extra dev`. Note `uv run python -m pytest` works around
it, which makes the failure look like a test problem rather than an environment one.

**Test isolation:** `muxplex/tests/conftest.py` redirects every state/config path
(`STATE_PATH`, `SETTINGS_PATH`, `PRUNING_STATE_PATH`, `IDENTITY_PATH`, `TTYD_PID_PATH`)
to `tmp_path`. Before it existed, a new test file wrote to the **real**
`~/.config/muxplex` — which has destroyed saved views. The 22 older modules still carry
their own equivalent autouse fixtures; those layer on top and win. Do not remove either.

**`bells.py` mocking gotcha:** `bells.py` does `from muxplex.sessions import run_tmux` at
import time, so it holds its own reference — patching `muxplex.sessions.run_tmux` does
NOT intercept the bell path and your test will spawn **real tmux processes**. Patch
`muxplex.bells.run_tmux`.

## Architecture quick map

| Area | Files |
|---|---|
| HTTP/WS server, federation proxy, asset cache-buster | `muxplex/main.py` |
| CLI (`serve`, `service`, `doctor`, `setup-tls`, …) | `muxplex/cli.py` |
| ttyd process management | `muxplex/ttyd.py` |
| Settings + federation sync | `muxplex/settings.py`, `muxplex/state.py` |
| Views model (mutual exclusion with hidden) | `muxplex/views.py` |
| Frontend app (grid, sidebar, views UI, settings) | `muxplex/frontend/app.js` |
| Frontend terminal (xterm, WS protocol, clipboard) | `muxplex/frontend/terminal.js` |
| Mobile keybar (self-contained; own DOM + styles) | `muxplex/frontend/mobile-keyboard.js` |

## Hard-won frontend contracts (do NOT re-litigate; tests enforce them)

Decided 2026-06-04 (fork PRs #1/#2); details in `CHANGELOG.md` v0.6.8 and
`muxplex/frontend/tests/test_terminal.mjs`:

1. **Ctrl+V paste** — the custom key handler branch for Ctrl+V must ONLY
   `return false` (no clipboard read, no `preventDefault`). xterm otherwise swallows
   the key as raw `0x16`/SYN sent to the PTY (TUI apps then read the *server-side*
   clipboard — the original "paste does nothing" bug). Returning false lets the
   browser's native paste event reach xterm's hidden textarea (bracketed paste).
   Reading the clipboard in this path = **double-paste** (COE).
2. **Right-click copy-or-paste** — gesture semantics: right-click WITH a selection
   completes a copy (never pastes); right-click with NO selection pastes via
   `navigator.clipboard.readText()`. Selection is sampled in a capture-phase
   `mousedown` handler (ahead of xterm) AND the contextmenu handler treats it as
   copy-only if a selection existed at **either** mousedown **or** contextmenu time
   (`hadSelectionOnRightDown || _term.hasSelection()`) — the OR closes a race (v0.9.3)
   where the mousedown sample read false (stale latch when contextmenu fires without a
   button-2 mousedown, or cross-client selection desync) while a selection was live,
   which let one click both copy and paste. The copy branch re-copies + clears and
   `return`s before `_pasteFromClipboard()`; copy and paste must NEVER both fire.
   `hasSelection()` is buffer-based — scrolling selection out of view doesn't affect it.
   (Do NOT restore the old comment claiming xterm clears the selection on right-down /
   that `hasSelection()` in contextmenu is always false — with `rightClickSelectsWord`
   unset it does not, and that false premise is what left the race open.)
   **v0.9.6.dev4 note:** `initRightClickCopyPaste` is now **lever-gated** by Mouse Lab
   lever 6 (`rightClickPassThru`, default OFF → contract unchanged). When ON and an app
   owns the mouse (`mouseTrackingMode !== 'none'`), the handler suppresses the browser menu
   but does NOT copy/paste — it lets the forwarded right-click reach the app, fixing the
   fullscreen-Claude-Code right-click double-paste (see v0.9.6 paragraph above).
3. **No handler stacking** — `#terminal-container` is static and `openTerminal()`
   re-runs per session switch. Container-level listeners belong in module-level
   attach-once IIFEs (`initRightClickCopyPaste`, `initMobileTerminalScroll`), never
   inside `openTerminal()`.
4. **Shift+Enter** sends LF (`0x0a`, = Ctrl+J) so TUI apps (Claude Code) insert a
   newline instead of submitting; shells treat LF/CR identically.
4b. **Deliberate text selection + zombie-drag killer** (`initDeliberateSelection`,
   v0.9.3/v0.9.5) — xterm.js 5.3.0 anchors a selection on a left `mousedown`, attaches
   document mousemove/mouseup listeners, and extends from the anchor on every mousemove
   **with NO physical-button check** (verified in the vendored bundle: the move handler's
   only gate is `if (!selectionStart) return`); it removes the listeners ONLY on mouseup.
   Two fixes:
   **(A, v0.9.3) drag threshold** — a **capture-phase document `mousemove`** that
   `stopImmediatePropagation()`s while the pointer stays within ~5px of the press, until a
   real drag crosses the threshold; then it steps aside and xterm selects normally. A
   sub-threshold left press is a focus click — no selection.
   **(B, v0.9.5) ZOMBIE-DRAG KILLER** — if a drag's mouseup never reaches the page
   (released outside the window, blurred mid-drag), xterm's drag is never torn down and
   re-extends a huge selection from the stale anchor on the next **buttonless** mousemove
   when the pointer returns — *before any click*. Fix is **focus-INDEPENDENT** (the
   v0.9.4 focus-tracking / first-click-reset was unreliable — `focusin` can fire before
   `mousedown`, focus may never move — and was REMOVED). A `dragMaybeActive` latch is set
   on a qualifying left mousedown and cleared by any real document `mouseup`; an always-on
   **capture-phase document `mousemove`** fires `killDrag` when `e.buttons === 0 &&
   dragMaybeActive && !inMouseTracking()` — `stopImmediatePropagation` (so xterm's
   bubble-phase move can't extend) + `_term.clearSelection()` (full teardown: nulls anchor
   AND removes xterm's listeners). Guards (do NOT remove): left button only; `e.detail
   === 1` (leaves dbl/triple-click select alone); unmodified only; bails when
   `_term.modes.mouseTrackingMode !== 'none'` (TUI mouse apps own the drag, and a
   buttonless move is real app input there) and when `e.buttons !== 0` (real drag in
   progress). Do NOT reintroduce focus-based gating. Module-level attach-once IIFE
   (contract #3).
   **v0.9.6.dev2 note:** parts A/B (and the focus-click clear) are now **lever-gated** by
   the Mouse Lab harness (`window.MouseLab` in terminal.js) so the fix can be A/B-tested
   live. `inMouseTracking()` now also folds the `honorTracking` lever (returns false when
   that lever is off, making the killer act even under mouse tracking). **Lever defaults
   reproduce exactly this shipped behavior**, so the contract still holds by default. A new
   lever 5 (`tmuxCopyClear`) sends Esc to the PTY on window refocus to cancel a possibly-
   stranded tmux copy-mode selection (Hypothesis A). See
   `docs/plans/2026-06-24-mouse-lab-harness.md`.
5. **View pills** (`renderViewPills` in `app.js`) — one pill per view in the header,
   single-click activates; collapse below 600px where the dropdown trigger swaps to
   the dynamic active-view label (static "Views" label on desktop). Pills re-render
   each poll cycle guarded by a string compare (no innerHTML churn).
6. **Auto-views are a SEPARATE synthesized list** (v0.8.0) — never merged into
   `_serverSettings.views`, never persisted/synced/pruned. Identity is namespaced
   `dir:<key>` (key = gitRepo ‖ cwdLeaf); the `dir:` prefix is reserved in every
   view-name validation site (frontend ×5 + `views.py`). Membership is computed
   per poll (`buildAutoViews`): live, non-hidden, ≥2 sessions per group. Surfaces
   that must exclude them (bulk ops, new-session picker, search chips, keyboard
   digits, federation sync) are correct BECAUSE the list is separate — do not
   "simplify" by merging it into the views array.
   **Worktree grouping (v0.9.2):** the group key comes from `gitRepo` (backend
   `sessions.resolve_git_repo`). A linked git worktree (e.g. `<repo>/.worktrees/<branch>`)
   roots a `.git` *file*, not a directory; the resolver follows it (via the worktree
   gitdir's `commondir`, falling back to stripping `worktrees/<name>`) to the **main
   repo** name, so a repo's main checkout and all its worktrees share one `dir:` group.
   Unparseable `.git` files fall back to the worktree dir's own name. Pure-Python, no
   `git` subprocess. Do NOT revert `resolve_git_repo` to stopping at the first `.git`.

7. **Bottom-docked mobile UI must ride the VISUAL viewport** (v0.9.6.dev5,
   `mobile-keyboard.js`) — **iOS Safari does not shrink the layout viewport when the
   software keyboard opens; it overlays it.** `window.innerHeight` and `100dvh` are
   unchanged, and normal flow knows nothing about the keyboard, so anything anchored to
   the bottom of the page is *guaranteed* to be drawn underneath it. The keybar therefore
   is `position:fixed; bottom:0` and lifts itself by
   `innerHeight - (visualViewport.height + visualViewport.offsetTop)` (published as
   `--keybar-lift` by `syncDock()`), riding directly above the keyboard. Three parts that
   look optional and are not: (a) listen to `visualViewport`'s **`scroll`** as well as
   `resize` — iOS signals keyboard show/hide via an `offsetTop` change as often as a
   resize, and without it the bar lags visibly; (b) **drop `safe-area-inset-bottom` while
   the keyboard is up** — the keyboard already covers the home-indicator gutter, so
   padding for it wastes a row; (c) keep the bar a **DOM child of `.terminal-wrapper`**
   despite being `position:fixed`, so `#view-expanded.hidden`'s `display:none !important`
   hides it on the dashboard with no extra gating. Also: on rounded-corner displays the
   gutter's corner arc physically clips the outermost keys, hence the
   `max(14px, env(safe-area-inset-left/right))` side inset and the wider first/last keys.
   Any future bottom-docked affordance should reuse `--keybar-lift` rather than
   re-deriving it. Details: `docs/plans/2026-07-27-mobile-terminal-keybar.md`.

8. **Reconnect must be able to STOP** (v0.9.6.dev6, `terminal.js`) — a tmux session whose
   process exits is destroyed by tmux (`exit-empty on`), so `tmux attach -t <name>` fails
   forever and no reconnect can ever succeed. Three parts, all load-bearing:
   (a) **`POST /connect`'s status is inspected.** A **404** (local: `connect_session`
   raises it once the ~2 s poll cache drops the name) is definitive → end the terminal.
   Federated sessions arrive as a **502 whose detail reads `Remote returned 404`**, because
   `federation_connect` flattens every non-2xx from the peer into 502 — so a *bare* 502 is
   deliberately NOT enough, or a merely-sick peer would be declared dead. 503/500/network
   errors stay retryable. Note `fetch()` resolves on a 404: a `.catch()` was never what hid
   this, ignoring `res.status` was. Do not "simplify" back to an unconditional `.then()`.
   (b) **`MAX_RECONNECT_ATTEMPTS = 8`** — a cause-independent backstop. Never remove it in
   favour of (a) alone.
   (c) **`endTerminalSession()` nulls `_currentSession`**, the single latch every reconnect
   path checks, and shows `#session-ended-overlay` whose Back button delegates to
   `#back-btn` (app.js keeps sole ownership of returning to the grid). A session ending must
   present as an explained outcome, never an indefinite spinner.
   The 800 ms post-`/connect` settle timer is tracked in `_reconnectTimer` — keep it so, or
   a late callback reattaches to a stale session.

## Hard-won backend contracts (2026-08-08 efficiency work; tests enforce them)

Full rationale and measurements: `docs/plans/2026-08-08-resource-efficiency-plan.md`.
`muxplex/tests/test_poll_cycle_perf.py` pins the per-cycle spawn counts, so a regression
fails loudly rather than silently costing O(N) again.

1. **The poll cycle must stay O(1) in N.** Per cycle: 1 `list-sessions`, 1 `list-panes -a`,
   1 `list-windows -a`, plus one `capture-pane` **only for panes that changed**. Never
   reintroduce a per-session tmux query — that is what `2N+2` was.
2. **Bells are ONE batched `list-windows -a`**, OR-aggregated across a session's windows,
   TAB-delimited (session names may contain spaces). `window_bell_flag` is a *window*
   variable; there is no session-level equivalent. Rejected and re-rejected:
   `window_activity_flag` (alert flag, gated on `monitor-activity`),
   `pane_unseen_changes` (copy-mode only), `session_activity` (bumps on client attach),
   `pane_last_activity` (does not exist).
3. **Snapshot change key is composite**: `window_activity|pane_id|pane_width|pane_height`.
   The timestamp alone is insufficient — `capture_pane` targets `-t <session>`, which
   resolves to the *current window's active pane*, so switching window/pane or resizing
   changes content with no new output. **Every ambiguous branch must fail toward
   capturing**, and the forced full sweep every 15th cycle is a correctness backstop —
   do not remove it.
4. **The `list-panes -a` format keeps `pane_current_path` LAST** and parses with a fixed
   maxsplit derived from the field count, so paths containing tabs survive. New fields go
   *before* the path.
5. **Ordering in `_run_poll_cycle` is load-bearing:** `list_session_paths` (publishes the
   change keys) must precede `snapshot_all` (consumes them). A one-shot freshness
   handshake enforces it — a mis-ordered call sees no keys and captures everything, so
   mistakes cost a fork, never a stale tile.
6. **`kill_ttyd` is SINGLE-PID, SIGTERM → SIGKILL.** Never `killpg`, never a process
   group, never the tmux server: a group kill destroys live sessions and everything in
   them (upstream issue #7). Its bool return means "there was something to clean up and
   it was dealt with" — **not** "the process is confirmed dead".
7. **`/api/sessions` caches its serialized body under a CONTENT-derived key**, not a list
   of invalidation sites. Bell state is mutated *outside* the poll cycle by the tmux
   alert-bell hook, so a generation-counter-only key would delay bells by up to 2 s. Keep
   the key derived from the payload's actual inputs.
8. **`settings.json` / `pruning.json` writes are atomic** (tmp + `os.replace`). A torn
   read makes `load_settings` fall back to `DEFAULT_SETTINGS` and a concurrent PATCH then
   destroys every saved view. Never revert to a bare `write_text`.
9. **The pruning-state write is guarded on the BOOKKEEPING changing**, not on
   `_prune_changed`. That flag is only true when a key was *removed*, while the grace
   clock is started by a bookkeeping-only mutation — guarding on it silently disables
   stale-key pruning forever.

## Documentation map

- `CHANGELOG.md` — user-facing release history (newest first)
- `docs/plans/` — design + implementation docs per feature, dated (dashboard, sidebar,
  auth, settings, federation, CLI, TLS, views, hidden-state redesign)
- `docs/TRUSTING_THE_LOCAL_CA.md` — client CA-trust walkthrough for `setup-tls --method ca`
- Views navigation: `docs/plans/2026-04-15-views-design.md` (+ phase1–3 implementation
  docs); header pills (2026-06-04) extend it — see CHANGELOG v0.6.8
- Expanded-header session pills (v0.7.0):
  `docs/plans/2026-06-04-expanded-header-session-pills-design.md` — grouped sibling
  pills + view dropdowns + width-aware collapse in the terminal header
- Universal session search (v0.7.2):
  `docs/plans/2026-06-04-universal-session-search-design.md` — name/cwd-leaf/git-repo/tag
  matching; backend cwd+gitRepo session metadata
- Bulk multi-select → Views (v0.7.3):
  `docs/plans/2026-06-04-bulk-multiselect-views-design.md` — grid select mode, batched
  Manage View panel, search-results multi-select
- cwd auto-grouping (v0.8.0): requirements
  `docs/plans/2026-06-05-cwd-auto-grouping-requirements.md`, code audit
  `docs/plans/2026-06-05-cwd-auto-grouping-audit.md`, implementation plan
  `docs/plans/2026-06-05-cwd-auto-grouping-plan.md` — directory auto-views
  (virtual `dir:` views, user-pill collapse priority) + group-by-directory grid
  mode + Grid Grouping settings relocation
- Pill zoom-hijack + federated-attach fix (v0.8.2): see `CHANGELOG.md`. The
  session-open zoom animation must select its tile via `_findZoomTile()`
  (`#session-grid article[data-session]` scoped + remoteId-matched) — never an
  unscoped `document.querySelector('[data-session=…]')`, which hijacked header
  nav-pills into full-viewport elements. Hover-preview resolves sessions by
  name + remoteId. Enforced by regression tests in `test_app.mjs`.
- v0.9 session UX (DONE, branch `feat/v0.9-session-ux`):
  requirements `docs/plans/2026-06-11-v0.9-session-ux-requirements.md`; shipped in
  `CHANGELOG.md` v0.9.0 — reliable new-session auto-open, **local** session rename
  (`POST /api/sessions/{name}/rename` + `views.rename_session_key` cascade; flyout +
  expanded-header ✎; remote rename out of scope), ⧉ View-pill glyph, and a
  narrow-viewport idle-name contrast fix. **v0.9.1** (`CHANGELOG.md`): cross-browser
  session-view convergence + suppressed redundant hover preview. **v0.9.2**
  (`CHANGELOG.md`): cwd auto-grouping spans git worktrees — backend-only
  `resolve_git_repo` change (see auto-views contract #6); no design doc.
- Mouse Lab selection-fix harness (v0.9.6.dev2, IN PROGRESS):
  `docs/plans/2026-06-24-mouse-lab-harness.md` — per-device localStorage toggle harness
  (**7 levers + 7 profiles** as of dev4) to A/B-test candidate fixes for TWO bugs: the
  stale-selection bug (tmux-`mouse on` "wrong-layer" reframing; Hyp. A = tmux copy-mode,
  not xterm's; levers 4/5) **and** the right-click double-paste from fullscreen Claude Code
  mouse capture (lever 6 `rightClickPassThru`). Defaults preserve shipped v0.9.5 behavior.
  Research artifact that prompted the reframing: `docs/Claude Code + tmux + Mouse.md` (NOT
  muxplex-specific; its env-var fixes don't apply — different stack). No CHANGELOG entry yet
  (dev build, no release).
- Mobile terminal keybar (v0.9.6.dev5, DONE — PR #9):
  `docs/plans/2026-07-27-mobile-terminal-keybar.md` — module shape, the per-browser
  enablement rationale, and the two iPhone-only bugs (rounded-corner key clipping; the
  software keyboard burying the bar) with the visual-viewport dock that fixes the second.
  See contract #7. Shipped in `CHANGELOG.md` v0.9.6.dev5.
- **Resource efficiency (DONE — PRs #11/#12, merged 2026-08-09):**
  `docs/plans/2026-08-08-resource-efficiency-plan.md` — the poll cycle made O(1) in N.
  Read this before touching the poll cycle, snapshots, bells, the `/api/sessions` cache,
  or ttyd lifecycle; the backend contracts above are its distilled output. Notable for
  what it *declined*: async disk I/O (measured at 0.137% of wall clock — `to_thread`
  costs more per hop than the `save_state` it would offload), ttyd `-t scrollback`
  (verified no-op — muxplex serves its own xterm bundle and ignores ttyd's
  SET_PREFERENCES), WS backpressure hardening (already bounded end-to-end), and
  visible-set snapshot scoping (unsound — federation hands every local snapshot to peers
  who filter by their *own* view). The doc carries a revision log of what its own first
  draft got wrong, after adversarial review corrected four risk ratings.
- **Terminal "Reconnecting…" loop (FIXED — v0.9.6.dev6):**
  `docs/plans/2026-08-09-terminal-reconnect-loop-investigation.md` — killing the process
  a session was invoked to run left the terminal retrying forever. Pre-existing; the
  briefing's hypothesis (the reconnect path has no notion of session liveness and
  re-POSTs `/connect` for a destroyed session every ~15 s) was **verified and correct**.
  §"Verification and outcome" records what was confirmed, the fix (directions 1 + 2,
  both), and the brief's one wrong claim — it blamed `.catch(() => null)`, but `fetch()`
  resolves on a 404, so the real defect was never reading `res.status`. Distilled into
  frontend contract #8.
