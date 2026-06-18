# Changelog

## v0.9.1 (2026-06-18)

Multi-browser session-sync fixes.

### Fixes

- **The sidebar now tracks the session that's actually on screen.** muxplex serves
  every browser/​tab from a single shared ttyd (one global `active_session`), so when a
  second browser on the same machine opened a different session, the first browser's
  terminal silently followed the shared ttyd — but its sidebar kept highlighting the
  *old* session, and re-clicking that stale highlight was a dead no-op (the click was
  short-circuited because the local "current session" variable still matched). The poll
  cycle now reconciles against the server's `active_session` while a session is open
  (`reconcileViewingSession`): it adopts whatever session is really being displayed so
  the sidebar highlight, header, and pills follow reality, and clicking is never stuck.
  The re-attach is client-only — it never re-issues `/connect`, so it can't kill+respawn
  the shared ttyd and disrupt the browser that made the switch. A short grace window
  after a local open prevents an in-flight state write from being read back stale and
  yanking you off the session you just clicked. (This makes all your browsers/​tabs
  *converge* on one session; independent per-browser sessions remain future work.)

- **The hover-preview overlay no longer pops for the session you're already viewing.**
  On the interactive page, hovering the active session's sidebar thumbnail (or having
  just clicked it open) showed the large light-box preview of the very session already
  filling the main viewer — redundant and distracting. `showPreview` now early-returns
  when the hovered session is the one currently displayed; previews for *other* sessions
  are unchanged. (A global on/off toggle already exists at Settings → Display → "Show
  hover preview".)

## v0.9.0 (2026-06-11)

Session-UX improvements.

### Features

- **New sessions now open straight into their terminal.** Creating a session from
  any entry point (header/​sidebar/​FAB "+" buttons) lands you in the new session's
  fullscreen terminal instead of leaving you on the grid. The previous auto-open
  silently failed for *local* sessions: the create-poll matched the new session by a
  bare name, but the backend tags every session (local included) with a canonical
  `device_id:name` `sessionKey`, so the match never succeeded and the poll always
  timed out. The poll now builds the local key from the device id (with a bare-name
  fallback) and waits generously (~120 s) for slow-to-start sessions — opening the
  moment the session appears rather than on a fixed delay.
- **Rename tmux sessions from the UI** (local sessions; v0.9 scope). A new
  `POST /api/sessions/{name}/rename` runs `tmux rename-session` and **atomically
  cascades** the rename everywhere the old name was stored — every view's membership
  list, `hidden_sessions` (legacy bare-name entries are healed to canonical form), and
  persistent state (`active_session`, `session_order`, per-session bell, the device's
  `viewing_session`). New names are validated (no empty/whitespace, no tmux-illegal
  `.`/`:`, not the reserved `dir:` auto-view prefix, no duplicates). Reachable from the
  tile/card flyout (grid **and** sidebar) and from the expanded terminal header's
  session dropdown (a ✎ control). The existing ttyd attach survives the rename, so the
  live terminal keeps running. Remote/federated rename is intentionally out of scope
  (it would leave peers' membership keys stale until the stale-key prune); the UI hides
  Rename for remote sessions.
- **View pills are now visually distinct from session pills** in both headers. User
  views carry a leading ⧉ layers glyph; auto/​directory views keep their 📁 folder
  glyph; session pills stay glyph-free capsules. Shape/glyph cue (not color) so it
  reads for color-blind users, and it doesn't disturb the width-aware pill collapse.

### Fixes

- **Readable session names in the narrow-viewport list.** When the grid collapses to a
  single-column list on very small screens, idle-tier session names were rendered in
  `--text-dim` (#4A5060) on the header background — only ~2.3:1 contrast, below WCAG AA.
  They now use `--text-muted` (~6:1): comfortably readable without the harsh glare of
  full-brightness text.

## v0.8.2 (2026-06-10)

### Fixes

- **Session pill no longer balloons across the viewport** — opening a session ran a
  FLIP "zoom" animation that pinned a tile `position:fixed` and expanded it to
  `100vw`/`100vh`. It selected the tile with a document-wide, name-only
  `querySelector('[data-session="…"]')`, but `data-session` is non-unique (it is also
  emitted on expanded-header nav-pills, tile-options buttons, view-dropdown items, and
  search rows). When the clicked session had **no grid tile in the active view**
  (a cross-view, auto-view, or "Other Sessions" pill), the first match was a header
  nav-pill `<button>` — which then got the full-viewport styles slammed onto it and
  was drawn down the left side of the screen (behind other layers, since it had no
  `z-index`). The lookup is now scoped to `#session-grid article[data-session]` **and**
  matched on `remoteId`; a header-only pill click leaves no match and the animation is
  correctly skipped instead of hijacking the pill. Leftover inline zoom styles are also
  proactively cleared once the overview is hidden. (Regression shipped with v0.7.0
  expanded-header session pills, which introduced the first header-resident
  `data-session` elements.)
- **Hover-preview click attaches the correct federated session** — the desktop
  hover-preview resolved its target session by bare **name** only
  (`find(s => s.name === name)`), so when two federated devices exposed sessions with
  the same name, clicking the preview could attach the **wrong device's** session.
  The preview now tracks the hovered tile's `remoteId` and resolves by **name +
  remoteId**, mirroring the unique `sessionKey = device_id:name` identity. The same
  unscoped name-only selector above also animated the wrong same-named tile,
  reinforcing the mismatch.

## v0.8.1 (2026-06-05)

### Fixes

- **Search multi-select checkboxes work again** — clicking a checkbox in the
  universal-search results closed the dropdown instead of toggling the selection.
  The checkbox click re-renders the results synchronously, detaching the clicked
  node before the document-level "click outside closes search" listener ran — which
  then couldn't see the click was inside the dropdown. Detached targets are now
  never treated as outside. (Bug shipped with the v0.7.3 search multi-select.)

## v0.8.0 (2026-06-05)

### Features

- **Directory auto-views** — for every project directory with **2+ live sessions**, a
  virtual view is synthesized automatically (grouping key: **git repo name**, falling
  back to the cwd top-leaf directory — so `qw-animas` running in `…/qw-bridge` groups
  with `qw-bridge`/`qw-bridge-2`, and hash-suffixed session pairs land together).
  Auto-views appear alongside user views — in the main-page pill row (dashed pills with
  a 📁 glyph), both view dropdowns, and the expanded terminal header — and filter the
  grid/sidebar exactly like a user view. They are **never persisted or synced**:
  derived state, recomputed every poll, vanishing when their sessions end (the active
  view then falls back to All Sessions automatically).
  - **Read-only by design**: no manage panel, no rename/delete, and they are never
    offered as targets in bulk add-to-view, the new-session views picker, or the
    search bulk chips. The `dir:` name prefix is reserved so a user view can never
    collide.
  - **Hidden sessions** are excluded from auto-view membership and counts.
  - **Width-aware pill priority** — user-view pills always win the header real estate:
    as the viewport narrows, auto-view pills collapse out first (they stay reachable
    via the dropdown); below 600px the existing collapse-to-dropdown behaviour is
    unchanged. The active auto-view pill is always kept visible.
  - **Expanded terminal header** — a "same directory" sibling pill group joins the
    view-based groups for the current session (deduped against them), and other
    directory groups appear as dropdown pills; directory-grouped sessions no longer
    duplicate into Other Sessions.
  - **Search integration** — auto-view names participate in tag matching: searching a
    directory/repo name surfaces all of that group's sessions, tagged with an outlined
    📁 chip.
  - **Settings → Display → "Directory auto-views"** toggle (default on) disables the
    whole feature.
- **Group-by-directory grid mode** — a third grid layout that clusters session tiles
  under alphabetical 📁 directory headers (same grouping key as auto-views, but no
  2-session minimum); sessions without path metadata (older federation remotes,
  brand-new sessions for one poll cycle) cluster under a final **Other** bucket. Works
  with or without multi-device; device badges, status tiles, view filtering, sort
  order, and select mode behave exactly as in the device-grouped mode.
- **Settings relocation** — the grid layout picker moved from Multi-Device to
  **Settings → Display → "Grid Grouping"** (`Flat` / `Group by device` / `Group by
  directory`), since directory grouping doesn't require multi-device; the "Group by
  device" option is disabled while multi-device is off. The stored key is unchanged
  (`gridViewMode`, now also `cwd`), so existing settings carry over — pick `Flat` to
  restore the previous interface exactly.

### Fixes

- A deleted-while-active user view (e.g. removed on another device) now falls back to
  All Sessions on the next poll instead of leaving a stale highlighted pill over an
  empty grid.

## v0.7.3 (2026-06-04)

### Features

- **Bulk multi-select → add to Views**, on three surfaces:
  - **Grid select mode** — a ☑ toggle in the main header makes tiles
    checkbox-selectable (click toggles a highlight instead of opening); a floating
    action bar offers `Add to View ▾` (selection kept, so the same set can go into a
    second view), `Hide`, and `Done` (Escape also exits). One settings write per action
    regardless of how many sessions are selected.
  - **Manage View panel** — checkbox changes are now **batched**: an `Apply (N)` button
    commits everything in a single update (Close discards). The panel can be opened for
    **any view directly** from Settings → Views without switching the active view first;
    rename/delete inside the panel only touch the active view when it is the one being
    managed.
  - **Search-results multi-select** — universal-search rows now carry checkboxes; with a
    selection, a footer shows `+ ViewName` chips — search a pattern, check the matches,
    one click tags them all.
  - Bulk adds preserve the add-implies-unhide invariant; bulk hide removes sessions from
    all views (same semantics as single-session operations).

## v0.7.2 (2026-06-04)

### Features

- **Universal session search** — a 🔍 control in both headers (right of the wordmark on
  the main page; right of the sidebar toggle in the terminal view). Typing drops down
  sessions matching — partially, case-insensitively — the **session name**, the **cwd
  top-leaf directory**, the **git repo name**, or a **view/tag name** (a matching view
  expands to its member sessions, each shown with the view as a tag chip). Hidden
  sessions are included with a `hidden` badge; rows show match badges (`dir:`, `repo:`),
  device badges, and activity dots. `/` focuses search on the main page (never
  intercepted inside the terminal); ArrowUp/Down + Enter navigate, Escape closes. One
  click switches to the session.
- **Session metadata for search** — `/api/sessions` (and federation payloads) now carry
  `cwd`, `cwdLeaf`, and `gitRepo` per session: the active pane's working directory is
  read with a single `tmux list-panes` call per poll cycle, and the repo name resolves
  via a memoized pure-Python `.git` walk-up (no git subprocess).

## v0.7.1 (2026-06-04)

### Features

- **New-session button in the expanded header** — the terminal view's header now has the
  same `+` control as the main page (far right, next to settings), so new sessions can be
  created without leaving the current session.
- **Views picker on session creation** — the new-session element (header `+` buttons,
  sidebar `+ New`, and the mobile FAB) now includes a Views dropdown of checkboxes, so a
  session can be assigned to one or more views/tags at creation time. The active user
  view is pre-checked (matching the previous auto-add behaviour); unchecking it is now
  possible, and all selected views are written in a single settings update.

## v0.7.0 (2026-06-04)

### Bug Fixes

- **Views appeared empty after a hard refresh (single-device mode)** — `/api/sessions`
  did not include `sessionKey`, so clients stored bare session names in view membership.
  The server's background normalize cycle then rewrote those entries to the canonical
  `device_id:name` form in `settings.json`; the in-memory SPA kept working, but after a
  page reload the frontend could no longer match canonical members against bare-name live
  sessions — views rendered empty. `/api/sessions` now tags every session with its
  canonical `sessionKey` (same form as `/api/federation/sessions`), and
  `normalize_session_keys()` dedupes entries (re-adding "lost" sessions had been creating
  exact duplicates next to their canonical siblings).

### Features

- **Session pills in the expanded (terminal) header** — the mostly-empty header above the
  terminal is now a session-level navigation strip. Left to right: a distinctly-styled pill
  for the **current session**, then one group of click-to-switch **sibling pills** per view
  the session belongs to (alphabetical, vertical-bar separators between groups, siblings in
  several of those views deduped into their first group), then a **dropdown pill per other
  view** listing its sessions (any session anywhere is 2 clicks away), and a right-aligned
  **Other Sessions** dropdown for view-less sessions. Hidden sessions are excluded
  everywhere. The strip is width-aware: each sibling group collapses into a
  `ViewName +N ▾` overflow dropdown as the viewport narrows (round-robin fair-share
  expansion as it widens — never collapsing exactly one session, since a dropdown pill is
  as wide as the pill it replaces) and is guaranteed at least one pill per view. Pills show
  amber activity dots (respecting the activity-indicator setting) and re-render each poll
  cycle behind a string-compare guard. Below 600 px the strip hides and the plain session
  name returns (mobile keeps the bottom-sheet switcher). Switching via pills does not
  change the active view. Design: `docs/plans/2026-06-04-expanded-header-session-pills-design.md`.

## v0.6.8 (2026-06-04)

### Bug Fixes

- **Ctrl+V paste never reached the terminal** — xterm.js translates Ctrl+V keydown into the
  raw `0x16` (SYN) control byte, cancels the browser event, and sends it to the PTY. TUI apps
  like Claude Code then attempt to read the *server-side* clipboard (headless/empty), so the
  browser clipboard was never pasted. The custom key handler now returns `false` for Ctrl+V —
  xterm skips its keydown translation *without* `preventDefault`, letting the browser's native
  paste event fire on xterm's hidden textarea and flow through the normal bracketed-paste path.
  No clipboard API call in this path, so no double-paste and no permission prompt.

- **Right-click now pastes the browser clipboard** — matches Windows terminal conventions
  (PuTTY, Windows Terminal). Uses `navigator.clipboard.readText()` → `_term.paste()` since no
  native paste event exists for right-click (one-time browser permission prompt).
  Shift+right-click / Ctrl+right-click still open the browser context menu as escape hatches.

- **Shift+Enter inserts a newline instead of submitting** — xterm.js sends CR for Enter
  regardless of Shift. Shift+Enter is now intercepted and sent as LF (`0x0a`, same as Ctrl+J),
  which TUI apps like Claude Code treat as "insert newline" vs CR "submit". Plain shells treat
  LF and CR identically, so behaviour elsewhere is unchanged.

### Features

- **View pills in the header** — one pill per view (All Sessions, user views, and Hidden when
  non-empty) rendered across the overview header, each with a live session count; a single
  click activates the view. The dropdown button remains as the management menu, now labelled
  "Views" on desktop. Below 600px the pills collapse and the dropdown trigger reverts to
  showing the active view name, functioning as the compact switcher exactly as before. Pills
  refresh each poll cycle with a string-compare guard to avoid needless innerHTML churn.

## v0.6.4 (2026-05-17)

### Bug Fixes

- **Empty device block still showing in grouped grid view** — Remote federation devices with
  zero tmux sessions were producing a visible "No sessions" block in the grouped grid view.
  The v0.6.3 fix targeted `renderGroupedGrid` but missed the unconditional `status:empty`
  status-tile append in `renderGrid` itself.  In grouped mode, `status:empty` tiles are now
  suppressed (`auth_failed` and `unreachable` tiles still appear in all modes).

- **`muxplex update` fails when uv/pip is installed outside PATH** — On Unraid (root user),
  macOS (user installs), and snap-packaged systems, `shutil.which("uv")` returned None even
  though uv was present at `~/.local/bin/uv`, `/snap/bin/uv`, or `/root/.local/bin/uv`.
  New helpers `_find_uv()` / `_find_pip()` probe a curated list of known install locations
  after PATH lookup fails, so the upgrade flow works on stripped-PATH environments
  (systemd, launchd, non-login SSH shells).

- **`muxplex update` exit code propagation** — Tests added to confirm that a failed install
  exits with code 1 after the `try/finally` service-recovery block runs (behaviour was
  implemented in v0.6.2; regression test coverage added here).

## v0.5.0 (2026-05-06)

### Features
- **`muxplex setup-tls --method ca`** — generate a persistent local Certificate Authority and sign a 13-month leaf TLS certificate with it. Install the CA once on each client device to get browser-trusted HTTPS for plain LAN names (`my-host`, `192.168.1.5`) without requiring Tailscale on every client and without buying a public domain. The CA persists across regenerations, so leaf rotation does **not** require re-trusting on clients. The leaf SAN auto-discovers the host's primary outbound LAN IPv4 address and the Tailscale MagicDNS name (when Tailscale is connected), in addition to the existing `<hostname>`, `<hostname>.local`, `localhost`, `127.0.0.1`, and `::1` entries. The CA cert has proper `BasicConstraints CA:TRUE pathlen:0` and `KeyUsage keyCertSign+cRLSign` extensions, so OS / browser trust stores accept it cleanly as a Root.
- **PWA install reliability** — the `ca` method specifically addresses the symptom where an installed PWA with a self-signed-cert origin gets kicked back into a regular browser tab on relaunch. With the CA installed in the OS trust store, the PWA shell stays in standalone mode across reopens.
- **New documentation** — [`docs/TRUSTING_THE_LOCAL_CA.md`](docs/TRUSTING_THE_LOCAL_CA.md) walks through CA install on Windows (PowerShell, no admin), macOS (`security` CLI), Linux (`update-ca-certificates` / `update-ca-trust`), iOS (Profile + Trust Settings), Android, and Firefox (separate trust store).

### API
- **`muxplex.tls.generate_local_ca(ca_cert_path, ca_key_path, days_valid=3650)`** — idempotent CA generator. Reuses the existing CA if both files exist; generates a new one otherwise. Returns metadata including a `regenerated` boolean.
- **`muxplex.tls.generate_leaf_signed_by_ca(ca_cert_path, ca_key_path, leaf_cert_path, leaf_key_path, hostnames, ip_addresses=None, days_valid=397)`** — generates a leaf TLS cert signed by an existing local CA. Builds proper `KeyUsage`, `ExtendedKeyUsage serverAuth`, `SubjectKeyIdentifier`, and `AuthorityKeyIdentifier` extensions, plus `SubjectAlternativeName` from the supplied DNS + IP lists.
- **`muxplex.tls._default_lan_ip()`** — returns the primary outbound IPv4 address (no actual packets sent; uses a connected UDP socket to ask the kernel which interface would route external traffic). Returns `None` on failure.
- **`muxplex.tls._default_tailnet_name()`** — returns the host's MagicDNS name from `tailscale status --self --json`, or `None` if Tailscale is unavailable / disconnected. Best-effort with a 5-second timeout.

## v0.3.5 (2026-04-14)

### Bug Fixes
- **Connection pool exhaustion fix** — replaced `setInterval` with self-scheduling `setTimeout` for both `pollSessions` and `sendHeartbeat` loops; prevents `ERR_INSUFFICIENT_RESOURCES` death spiral when federation requests time out during 2-second poll cycles

## v0.3.4 (2026-04-13)

### Bug Fixes
- **Zero-session devices visible** — devices with no tmux sessions now show a "No sessions" status tile instead of being invisible
- **Flapping prevention** — server-side cache of last-known-good federation results per remote; returns cached sessions for up to 3 consecutive failures before marking unreachable
- **Status tiles show device name** — offline/unreachable tiles display the device name instead of blank (was passing session.name which is undefined for status entries)
- **Status entries filtered from session list** — unreachable/auth_failed entries no longer render as blank session tiles in dashboard or sidebar
- **remoteId=0 falsy bug in mobile sheet** — first remote instance (index 0) now works correctly in the mobile bottom sheet session switcher

## v0.3.3 (2026-04-13)

### Bug Fixes
- **iOS/iPadOS touch scrolling** — fix touch scroll handling for Safari on iOS and iPadOS devices (PR #4, @samueljklee)

## v0.3.2 (2026-04-09)

### Bug Fixes
- **Hidden sessions filter now applies to federated sessions** -- hiding a session now hides it everywhere (local and remote), completing the federation-aware hidden sessions feature

## v0.3.1 (2026-04-08)

### Bug Fixes
- **Federation auth stale key** -- the auth middleware now reads the federation key fresh from disk on each request instead of caching it at startup; key generation and rotation no longer require a server restart
- **Settings sync silent push failures** -- the PUT response from `/api/settings/sync` is now checked; 409 (remote newer) is handled gracefully, other errors are logged

## v0.3.0 (2026-04-08)

### Features
- **Federation settings sync** -- user preferences (font size, sort order, hidden sessions, etc.) now sync across all connected muxplex servers using a P2P last-write-wins protocol with per-server timestamps; offline servers catch up automatically on reconnect
- **Heartbeat-driven bell clearing across federation** -- viewing a remote session now clears its activity bell on the remote server automatically; no more stale activity indicators for federated sessions

### Bug Fixes
- **`remoteId: 0` falsy bug** -- sessions from the first remote instance were incorrectly subject to the hidden-sessions filter due to a JavaScript falsy-0 check; fixed `!s.remoteId` to `s.remoteId == null`
- **Browser indicators ignore hidden sessions** -- tab title `(N)` count and favicon activity badge now filter through `getVisibleSessions()` so hidden sessions don't contribute to activity counts

### API
- **`GET /api/settings/sync`** -- returns syncable settings + timestamp for federation sync (Bearer token auth)
- **`PUT /api/settings/sync`** -- accepts synced settings; applies if incoming timestamp is newer (200), rejects if older (409 with local state)

## v0.2.0 (2026-04-08)

### Features
- **Server-side settings consolidation** -- all display preferences (font size, grid columns, hover delay, view mode, device badges, hover preview, activity indicator, grid view mode, sidebar state) moved from browser localStorage to server-side `settings.json`; settings now survive browser clears and are consistent per-server
- **Federation session deletion** -- kill sessions on remote devices from any muxplex client
- **Session creation error reporting** -- replaced fire-and-forget subprocess with async process that checks exit codes, surfaces stderr, and pre-flight checks the command binary on PATH
- **TTY-attach resilience** -- session commands that exit non-zero but still create the tmux session (e.g. `amplifier-workspace` which tries to attach after create) are detected and treated as success

### Bug Fixes
- **Federation key preservation on URL edit** -- editing a remote instance URL (e.g. `http://` to `https://`) no longer erases the federation key; added position-based fallback alongside the existing URL-based key restoration
- **PWA manifest auth bypass** -- added `.json` to the static extension allowlist so `/manifest.json` is not auth-gated; previously produced "Syntax error" in the browser console
- **`auto_open` toggle** -- fixed three-way key mismatch (`auto_open` vs `auto_open_created`) that made the auto-open setting completely non-functional
- **Session enumeration crash** -- `enumerate_sessions()` now catches `FileNotFoundError` when the session command binary is missing from PATH, preventing poll loop crashes
- **Settings PATCH key leak** -- the `PATCH /api/settings` response now redacts sensitive keys, matching the existing `GET /api/settings` behavior
- **Federation 503 diagnostics** -- all federation proxy 503 errors now include the exception type and message instead of just the remote URL
- **FastAPI version string** -- corrected the hardcoded `version` in the FastAPI app from `0.1.0` to match the release

## v0.1.1 (2026-04-07)

### Features
- **TLS/HTTPS support** — `muxplex setup-tls` auto-detects Tailscale → mkcert → self-signed certificates
- **TLS nudge** in `doctor` and `service install` when clipboard requires HTTPS
- **Session device selector** — create sessions on remote devices when multi-device enabled
- **Activity count in page title** — browser tab shows `(2) hostname - muxplex` for unseen bells
- **Favicon activity badge** — amber dot overlay on favicon for unseen notifications
- **Terminal search** — Ctrl+F to search scrollback (xterm-addon-search)
- **Clickable URLs** — Ctrl+Click / Cmd+Click opens URLs in terminal output (xterm-addon-web-links)
- **Inline image rendering** — Sixel and iTerm2 graphic protocols (xterm-addon-image)

### Bug Fixes
- **Federation SSL** — federation client accepts self-signed TLS certificates on remote instances
- **Federation empty key** — skip Authorization header when federation key is empty
- **Federation WebSocket SSL** — WebSocket proxy accepts self-signed certs on wss:// remotes
- **Remote session connect** — terminal reconnect uses federation connect path for remote sessions
- **Remote session restore** — persist `active_remote_id` in state for page refresh restore
- **Bell clearing for remote sessions** — federation bell-clear endpoint + unique sessionKey
- **Service crash-loop prevention** — kill stale port holders on startup, TimeoutStopSec in systemd
- **UTF-8 terminal display** — decode WebSocket output with TextDecoder before xterm.js write
- **Clean clipboard handling** — removed custom paste handlers per COE review, native xterm.js paste
- **Guard empty session name** — openSession bails on empty name from unreachable federation tiles
- **Clean Ctrl+C exit** — `muxplex service logs` exits cleanly on keyboard interrupt

### Infrastructure
- **PyPI publish** — available as `pip install muxplex`
- **GitHub Actions CI** — tests run on push/PR (Python 3.11-3.13)
- **Self-hosted vendor libs** — eliminates Edge Tracking Prevention console noise

## v0.1.0 (2026-04-04)

Initial release.
