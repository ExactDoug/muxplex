# Universal Session Search — Design

**Date:** 2026-06-04
**Status:** Approved (user decisions incorporated)
**Related:** `2026-06-04-expanded-header-session-pills-design.md` (header layout),
bulk multi-select design (search results are Surface 3 of bulk add-to-views)

## Goal

A compact search control in both headers that, as you type, drops down sessions
matching (partial, case-insensitive) any of:

1. **Session name**
2. **cwd top-leaf directory name** (basename of the session's active pane cwd)
3. **Git repo name** (toplevel directory name containing `.git`, walked up from cwd)
4. **Tag** = View name — a query matching a view's name expands to that view's
   member sessions (each row shows the matched view as a tag chip)

One click (or Enter) opens the session. Quick switching from anywhere.

## Placement

- **Overview header:** search icon (🔍 magnifier button) immediately right of the
  wordmark — i.e. right of the left-anchored elements, before the view pills.
  Clicking (or pressing `/`) expands it into an input.
- **Expanded header:** same relative location — right of the back/sidebar-toggle
  buttons, before the session-pill strip. Click-to-open only (no key
  interception in the terminal — preserves the key-handling contracts).

The collapsed state is just the icon button (header space is precious,
especially next to the pill strip). Expanding shows a ~220px input; the results
dropdown is the shared fixed-positioned menu pattern (same trick as
`#expanded-pill-menu` — escapes overflow clipping).

## Decided behaviours

- **Hidden sessions ARE searchable**, shown dimmed with a `hidden` badge.
  Opening one does not unhide it.
- **`/` focuses search on the overview page only** (ignored when typing in an
  input/textarea). No global shortcut inside the terminal view.
- **Keyboard:** ArrowDown/ArrowUp move an active-row highlight, Enter opens the
  highlighted (or first) result, Escape closes and restores the icon.
- **View-name matches expand to member sessions** — view rows are not shown.
- Results sorted: match-quality groups (name-prefix → name-substring → cwd-leaf
  → repo → tag), alphabetical within each group; a session matching several
  fields appears once with all its match badges, ranked by its best field.
- Status sentinels (`unreachable`/`auth_failed`) never match.

## Backend — new session metadata

`/api/sessions` items (and therefore federation payloads, which are produced by
each remote's own server) gain:

| Field | Source | Example |
|---|---|---|
| `cwd` | active pane's `#{pane_current_path}` of the session's active window | `/mnt/c/dev/projects/github/muxplex` |
| `cwdLeaf` | `basename(cwd)` | `muxplex` |
| `gitRepo` | basename of the first ancestor of `cwd` (inclusive) containing `.git` (dir or file — worktrees yield the worktree dir name); `null` if none | `muxplex` |

Implementation:

- `sessions.py`: new `list_session_paths()` — ONE subprocess per poll cycle:
  `tmux list-panes -a -F '#{session_name}\t#{window_active}\t#{pane_active}\t#{pane_current_path}'`,
  keep rows where both actives are `1` → `{session: path}`. Cached in-module
  beside `_snapshots` (`get_session_paths()` accessor), refreshed in the same
  poll-cycle step that snapshots are.
- `gitrepo` resolution: pure Python walk-up (`os.path` only — no `git`
  subprocess), memoized per cwd in a small dict cache (cleared when it exceeds
  ~512 entries). Worst case a few stat calls per *new* directory ever seen.
- `/api/sessions` and the federation local-session builder attach the three
  fields. Old remotes simply omit them — the frontend treats missing fields as
  non-matching (graceful).

## Frontend

- `index.html`: per-header `<div class="session-search">` (icon button +
  collapsible input) + ONE shared `#session-search-results` fixed-position
  dropdown at body level.
- `app.js`:
  - `searchSessions(query, sessions, settings)` — pure, exported, tested.
    Returns `[{ session(slim), matchedFields: ['name'|'dir'|'repo'|'tag'...],
    tags: [viewNames...], hidden: bool, rank }]`. Empty/whitespace query → [].
  - `renderSearchResults()` / open/close/position helpers; delegated
    attach-once listeners (contract #3); input handler re-renders per keystroke
    (pure in-memory filter over `_currentSessions` — no network).
  - Result row: name, device badge (multi-device), bell dot, match badges
    (`dir:muxplex`, `repo:muxplex`, tag chips), `hidden` badge.
  - Click/Enter → `openSession(name, { remoteId })`, close, clear.
  - `/` handling added to `handleGlobalKeydown` (overview only, not in inputs).
- `style.css`: icon button, expanding input, results dropdown reusing
  `view-dropdown__menu`/`__item` visual language, active-row highlight,
  badges/chips. Inserted BEFORE the `@media (max-width: 959px)` overlay (file
  contract: that block stays last).

## Mobile

<600px: the icon remains in both headers (it's small); the expanded input
overlays the header row (absolute, full-width) instead of inline expansion.

## Testing

- Python: `list_session_paths()` parsing (active filtering, tabs in paths
  edge), git walk-up (repo found / none / `.git` file), `/api/sessions` field
  presence, cache behaviour.
- Frontend (`test_app.mjs`): `searchSessions` — each match field, partial +
  case-insensitive, hidden flagged not excluded, status excluded, view-name
  expansion with tag chips, ranking order, multi-field dedup, missing
  cwd/gitRepo fields, empty query. Render-contract tests with the mock-DOM
  pattern.

## Out of scope (later)

- Searching snapshot text (content search) — different cost profile.
- Multi-select checkboxes in results — Surface 3 of the bulk add-to-views
  design; the result-row DOM leaves room for a leading checkbox.
