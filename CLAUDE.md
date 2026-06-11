# muxplex — Agent Guide

Web-based tmux session dashboard: a FastAPI backend proxying ttyd WebSockets to an
xterm.js frontend, with multi-device federation, PAM/password auth, TLS, and
user-defined session Views.

**This repo (`ExactDoug/muxplex`) is a fork of `bkrabach/muxplex`** carrying UI/UX
improvements. Current version: **0.8.2** (on `main`).

**Next work — branch `feat/v0.9-session-ux`** (off `main` @ v0.8.2): three UI
improvements, scoped in `docs/plans/2026-06-11-v0.9-session-ux-requirements.md`:
(1) new session auto-becomes the active session; (2) rename tmux sessions from the
tile flyout + both header pill dropdowns (⚠ must cascade the rename to `view.sessions`,
`hidden_sessions`, and active-session state — no backend rename endpoint exists yet);
(3) style View pills distinctly from session pills in both headers.

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
   `navigator.clipboard.readText()`. The selection state MUST be sampled in a
   capture-phase `mousedown` handler: event order is mousedown → *xterm clears the
   selection* → contextmenu, so `hasSelection()` inside contextmenu is always false.
   `hasSelection()` is buffer-based — scrolling selection out of view doesn't affect it.
3. **No handler stacking** — `#terminal-container` is static and `openTerminal()`
   re-runs per session switch. Container-level listeners belong in module-level
   attach-once IIFEs (`initRightClickCopyPaste`, `initMobileTerminalScroll`), never
   inside `openTerminal()`.
4. **Shift+Enter** sends LF (`0x0a`, = Ctrl+J) so TUI apps (Claude Code) insert a
   newline instead of submitting; shells treat LF/CR identically.
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
- **NEXT — v0.9 session UX (NOT STARTED):**
  `docs/plans/2026-06-11-v0.9-session-ux-requirements.md` — auto-activate new
  session, rename tmux sessions (pills + tiles, with view/hidden/state cascade),
  distinct View-vs-session pill styling. Branch `feat/v0.9-session-ux`.
