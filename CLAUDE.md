# muxplex — Agent Guide

Web-based tmux session dashboard: a FastAPI backend proxying ttyd WebSockets to an
xterm.js frontend, with multi-device federation, PAM/password auth, TLS, and
user-defined session Views.

**This repo (`ExactDoug/muxplex`) is a fork of `bkrabach/muxplex`** carrying UI/UX
improvements. Current version: **0.9.6.dev2** (on branch `feat/v0.9-session-ux`) — a
**dev/experimental** build carrying the Mouse Lab selection-fix harness (see below); last
released version is **0.9.5**.

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
Built a **Mouse Lab** harness (Settings tab) — 6 per-device localStorage toggles + 6
profiles to A/B-test fixes live without reloads; diagnostics now log `hasSel`/`track`.
**Defaults reproduce shipped v0.9.5 behavior (tests stay green).** Design + test plan:
`docs/plans/2026-06-24-mouse-lab-harness.md`. Awaiting the user's over-time testing before
a winning fix is baked in and the harness removed.

## Running locally (development)

```bash
uv sync --extra dev          # one-time; dev extra needed for bs4 (HTML tests)
uv run muxplex serve         # http://127.0.0.1:8088 — settings from ~/.config/muxplex/settings.json
```

- `uv run` installs the project editable: frontend files in `muxplex/frontend/` are
  served live — a browser refresh picks up edits, no restart needed (backend `.py`
  changes DO need a restart).
- **Browser caching gotcha:** assets are served with `?v=<package version>` and no
  `Cache-Control` header. Same version ⇒ browsers replay cached JS without
  revalidating. When testing frontend changes, hard-refresh (`Ctrl+Shift+R`) or bump
  `version` in `pyproject.toml` (rotates the cache-buster for every client).
- Production-style usage: `uvx --refresh --from git+https://github.com/exactdoug/muxplex muxplex`
  (`--refresh` required or uvx replays its cached build).

## Tests

```bash
uv run pytest -q -m "not integration"          # Python suite (~1320 tests)
node muxplex/frontend/tests/test_app.mjs       # frontend app logic (~470 tests)
node muxplex/frontend/tests/test_terminal.mjs  # terminal/xterm contracts
```

⚠️ `test_terminal.mjs` has **27 pre-existing harness failures** (WebSocket/DOM mock
environment issues — "onData is registered exactly once" etc.). They are NOT
regressions; diff failing test names against a clean checkout before blaming a change.

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
  (6 levers + 6 profiles) to A/B-test candidate fixes for the stale-selection bug, after
  the tmux-`mouse on` "wrong-layer" reframing (Hypothesis A: stale highlight is tmux
  copy-mode, not xterm's). Defaults preserve shipped v0.9.5 behavior. Research artifact
  that prompted it: `docs/Claude Code + tmux + Mouse.md` (NOT muxplex-specific; its env-var
  fixes don't apply — different stack). No CHANGELOG entry yet (dev build, no release).
