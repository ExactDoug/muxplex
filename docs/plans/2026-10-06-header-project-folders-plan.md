# Header strip: every project folder as a pill, most-recently-used first — Plan

**Issue:** #24 (owns acceptance criteria and open/closed state; not restated here)
**Branch:** `feat/header-project-groups-fill-width`, merged to `main` in PR #25 on 2026-10-06 (branch and worktree since removed)
**Extends:** `2026-06-04-expanded-header-session-pills-design.md` (strip layout and allocator),
`2026-06-05-cwd-auto-grouping-*.md` (auto-views, A7/A8/A9)

## Requirements (rev 2, 2026-10-06, from the user)

1. **Every project folder that has a live session gets a 📁 pill** in the terminal-view header
   strip, with the same icon and look whether it holds 1 session or several. The ≥2 threshold no
   longer applies in the strip.
2. **Folders are ordered left → right by how recently the user accessed any session in them.**
3. Pills **fill the strip's width** up to the right-aligned **Other Sessions** pill. Whatever
   doesn't fit goes into Other Sessions.
4. **Other Sessions is organised by folder**: each folder is a row, and its sessions open as a
   submenu branching off the row (hover with a mouse, tap on touch). Clicking or tapping a session
   switches to it.
5. **Views are effectively unused.** No views are defined and none are planned, and the feature
   may be retired. Keep it working, but make no new investment in it.

## Phase status

| Phase | What | Status |
|---|---|---|
| 0 | Research, issue, plan, Codex adversarial review (rev 3) | DONE (2026-10-06) |
| 1 | Backend: per-session recency fields in `/api/sessions` | DONE (2026-10-06) |
| 2 | Frontend model: strip-local folder groups, recency-ordered | DONE (2026-10-06) |
| 3 | Layout: fill width, overflow list, Other Sessions always visible | DONE (2026-10-06) |
| 4 | Other Sessions folder → session menu with submenu; Escape fix | DONE (2026-10-06) |
| 5 | Docs, version (0.9.6.dev10); manual check on the live instance | DONE (2026-10-06): the user accepted it on the live instance and merged PR #25. The separate touch-device tap check (§5) was not individually recorded |

## Revision log

- **rev 1:** alphabetical order, ≥1-session folders, overflow into a two-level Other Sessions menu.
- **rev 2:** the user chose recency order; views are deprioritised. Recency comes from tmux
  `session_last_attached`.
- **rev 3 (Codex adversarial review):** Codex raised 16 findings and I verified each one against
  the code. Fixed here:
  - **Parse bug.** tmux prints an *empty* `session_last_attached` for never-attached sessions.
    Combined with the existing `.strip()`, the fields would collapse and a timestamp would be
    read as part of the session name, so the session would look gone and get its ttyd reaped
    (§3.1).
  - **Pre-existing Escape bug.** With a pill menu open, Escape also exits the terminal (§3.5).
  - **Other Sessions could still scroll off-screen.** It lived inside the scrolling strip (§3.4).
  - **Circular reservation-release step in placement** (§3.4).
  - **Wrong hover mechanics.** `pointerenter` doesn't bubble, the submenu had no click
    delegation, and on a mouse a hover-open followed by a click would toggle closed (§3.5).
  - **Name keys interpolated raw into a CSS selector** (§3.5).
  - **The `autoViewsEnabled` toggle was being bypassed**, and the folder of a hidden current
    session was mis-resolved (§3.3).
  - **Overflow counts double-counted** (§3.4).
  - **No submenu lifecycle** on resize, scroll or a hidden strip (§3.5).
  - **Side-map lifecycle was unspecified** (§3.1).
  - **Two factual errors** (§1.4).
  - **The "~2 s" convergence claim** is really up to about 4 s (§2).

  Not adopted:
  - The elaborate test-server isolation. The user accepts disruption of the live instance.
  - Full arrow-key menu navigation. This is an enhancement, not a bug; focus preservation across
    re-renders *is* adopted.
  - The "attach ≠ access" objection, which is downgraded to a documented limitation (§1.3).

---

## 1. Findings (verified 2026-10-06)

### 1.1 Why only 2–3 folders show: the ≥2-session rule, not a count

`buildExpandedPillsModel()` (`app.js:3977`) takes its folder pills from `buildAutoViews()`
(`app.js:838`), which drops single-session folders:

```js
if (groups[k].length < 2) continue;   // app.js:859 — requirement A8
```

I ran the real `buildExpandedPillsModel` against live `/api/sessions` (20 sessions, 18 folders).
It produced 2 folder pills, 0 home groups, and 15 sessions flat in Other Sessions.

### 1.2 Latent problems that appear once every folder gets a pill

1. **Folder pills bypass the width allocator.** Every other-view pill is counted as `fixed`
   (`app.js:4263–4266`); only home-group siblings are allocated (`allocateExpandedPills`,
   `app.js:4099`). `.expanded-pills` is `overflow-x:auto` with a **hidden scrollbar**
   (`style.css:2210`), and the Other Sessions pill sits *inside* it (`margin-left:auto`,
   `style.css:2289`). Surplus pills, and Other Sessions with them, go silently off-screen.
2. **Index-keyed menus.** Dropdown keys are positional (`view:i` / `overflow:i`, `app.js:4321`).
   When the render signature changes, the open menu is re-rendered (`app.js:4305–4317`). With
   folders appearing, disappearing and reordering, an open menu can silently switch lists. The
   re-open lookup also interpolates the key into a selector:
   `nav.querySelector('[data-pill-menu="' + _expandedPillMenuFor + '"]')` (`app.js:4308`).
3. **No recency data reaches the browser.** The payload carries
   `bell, cwd, cwdLeaf, gitRepo, name, sessionKey, snapshot`. Side finding: the grid's
   Settings → Sort → **"Recent" option is a no-op** (`app.js:2306`).
4. **Pre-existing Escape bug.** The menu's Escape listener (`app.js:6314`) closes the dropdown,
   but `handleGlobalKeydown` (registered at `app.js:6468`) has no open-menu check. Its fullscreen
   branch (`app.js:5515–5517`) then runs `closeSession()`. **Pressing Escape to dismiss a header
   dropdown today also exits the terminal**, and for a local session it sends
   `DELETE /api/sessions/current`.

### 1.3 Where "last accessed" comes from: tmux already records it

**Mechanism (verified):** a session open goes through `connect_session` (`main.py:986`), which
respawns ttyd as `ttyd … tmux attach -t <name>` (`main.py:1000–1001`, `ttyd.py:250`). ttyd
forks that attach **per WebSocket client**, so every terminal connection stamps
`#{session_last_attached}`. That includes a cross-browser follow (`reconcileOnly`,
`app.js:394`), which skips `/connect` but still opens a socket. tmux is 3.4.

The live read confirms it. The viewed session reads 0 m and the two before it 19 m and 20 m. Seven
sessions were launched detached (c-tmux / phone remote-control) and never opened in muxplex. For
those, tmux prints an **empty** `session_last_attached`; the conditional format
`#{?session_last_attached,#{session_last_attached},0}` was verified to yield `0`.

Simulated strip order from live data (folder recency = newest session; ties by name):

| recency = max(lastAttached, created) *(adopted, R2)* | recency = lastAttached only |
|---|---|
| timebot 1m · muxplex 19m · docker-harbor 20m · **quotewerks 51m** · datto-rmm-component-installer 127m · customer-subscription-manager · connectwise_psa_smart_mcp · prtg_smart_mcp · … | timebot · muxplex · docker-harbor · datto-rmm-component-installer 1143m · … · **7 folders "never", alphabetical at the end** |

**Options considered**

| Source | Verdict | Why |
|---|---|---|
| **tmux `session_last_attached` (+ `session_created`)** | **Chosen** | Already maintained by tmux. Cross-device, counts the user's own external `tmux attach`, survives muxplex restarts, costs **zero new tmux spawns**, and passes through federation untouched (`**s`, `main.py:1814`). |
| Frontend `localStorage` MRU | Rejected | Per-browser; misses other devices and external attaches. |
| Server-recorded access map in `state.json` | Rejected | Duplicates tmux; adds persisted state needing the rename cascade, pruning and federation keys. |
| `session_activity` / `window_activity` | Rejected | Measures **output**, not access. |

**Known limitation: attach time is not usage time.** muxplex has **one shared active session
across all browsers** (memory: shared-ttyd model), so visits are sequential. Ordering by
switch-*in* time therefore gives the same order as last-*used* time for every non-current folder.

There are two residual differences, both accepted:
- Time spent inside a session after attaching isn't counted.
- A background reconnect re-stamps the current session, which is the current one anyway.

Federated sessions use the remote host's clock; seconds of skew are irrelevant at this
granularity.

### 1.4 Constraints carried in from existing contracts

- **Backend contract #1:** the poll cycle stays O(1). The fields ride the existing single
  `list-sessions` (`test_poll_cycle_perf.py:233` asserts one call).
- **Backend contract #7:** `_sessions_payload_key` must cover every payload input.
- **Frontend contract #9** (ttyd reaping, `CLAUDE.md` frontend list; *not* backend #9, which is
  pruning): a name missing from enumeration clears `active_session` and kills ttyd
  (`main.py:267–269`, `336`). The new parse must be **never worse than today** at keeping names
  (§3.1).
- **Frontend contract #6 / A8:** `buildAutoViews()` stays at ≥2. It feeds the dashboard view
  pills, the sidebar and search tags. (Correction from rev 2: the grid's group-by-directory mode
  does **not** use it. `renderCwdGroupedGrid` calls `sessionGroupKey` directly, and singletons
  already get headers there, `app.js:1257–1274`.)
- **A9:** the current session's folder renders as a home group of inline sibling pills.
- **Contract #3:** listeners attach once, delegated (`app.js:6266–6316`).
- The strip is `display:none` below 600 px (`style.css:2237`).
- `#expanded-pill-menu` scrolls (`overflow-y:auto`, 200–280 px wide), so the submenu must be a
  sibling fixed-position element.

## 2. Decisions

- **D0 (decided): strip-local grouping.** A new pure helper groups by `sessionGroupKey()` with no
  minimum. `buildAutoViews()` / A8 are untouched, and a regression test pins that.
- **D0a (decided): scope is the terminal-view header strip.** Dashboard pills keep ≥2.
- **D0b (decided): the Settings "Directory auto-views" toggle still governs the strip.** When it
  is off, there are no folder pills and Other Sessions reverts to today's flat list, which keeps
  the existing test at `test_app.mjs:7279` valid.
- **D1 (decided by the user): recency order, most-recently-accessed folder leftmost**, sourced from
  tmux (§1.3).
- **R2 (decided): recency = `max(lastAttached, created)`.** Launching a session counts as accessing
  it.
- **R3 (decided): sessions inside a folder are most-recent-first too.** This covers dropdowns,
  submenus and inline siblings, with name as the tie-break.
- **D2 (decided): current-folder siblings get width first**, then other folders, so
  `allocateExpandedPills` is unchanged.
- **D3 (decided by the user): uniform folders.** A 1-session folder looks the same as any other,
  both in the strip and in Other Sessions.
- **D4 (decided): views are untouched.** They keep their position before folders (A7), settings
  order, and the 7-view cap; there is no new view work.
- **D5 (decided): phones below 600 px keep the strip hidden** (see #17).
- **D6 (decided): prefix placement.** Other Sessions holds exactly "everything older than the last
  visible pill". This can leave a little unused width when a wide pill blocks narrower later
  ones; that is accepted for predictability.
- **D7 (decided): menu and submenu keys are name-based** (`f:<folderKey>`, `v:<viewName>`,
  `ungrouped`), and elements are found by **exact `dataset` comparison**, never by interpolating
  the key into a selector.
- **D8 (decided): fields are read through the existing `list-sessions`.** `enumerate_sessions()`
  keeps its `list[str]` return.

**Expected MRU behaviour:** when you switch to a session in folder B, B becomes the home group and
the folder you left moves to the first candidate slot. This reaches the screen after the next
backend poll *and* the next browser poll, so typically within 2–4 s; federated peers can take
longer. Session output never reorders the strip.

## 3. Design

### 3.1 Backend (Phase 1)

**Format.** `enumerate_sessions()` uses

```
#{?session_last_attached,#{session_last_attached},0}\t#{?session_created,#{session_created},0}\t#{session_name}
```

The conditionals make both time fields **always non-empty digits**. The name goes **last**, so a
name containing a tab survives the split. tmux itself rewrites `.` and `:` in names, and
`validate_session_name` (`sessions.py:93`) forbids them.

**Parse:** apply these rules to each line from `output.splitlines()`.

1. **Never `.strip()` before splitting.** Stripping is what turned an empty leading field into a
   corrupted name.
2. **Two or more tabs:** `split("\t", 2)` gives `(la, cr, name)`. The name is the third field
   verbatim. Each time is parsed independently with `int()`; a non-numeric time becomes `None`
   and the name is still kept.
3. **No tab:** the whole line is a bare name with unknown times. This is the legacy shape the
   existing `run_tmux` mocks produce.
4. **Exactly one tab:** unreachable with this format. Log a warning and keep the whole line as the
   name, which is exactly what today's code does with any line. The guarantee is **never worse
   than today**, not "can never drop".
5. Skip lines that are empty after removing `\r`, as today.

A **pinning test** round-trips the format string through the parser. It covers a never-attached
session (fields `0`), a name containing a tab, and a name with leading or trailing spaces.

**Side map lifecycle.**
- Each successful enumeration **replaces** a module-level `{name: (lastAttached, created)}` map
  wholesale (rebind, never mutate), so deleted or recreated names can't keep stale times.
- On a tmux error, `enumerate_sessions` returns `[]` as today and leaves the map alone. Every name
  is gone then anyway.
- The getter returns the map reference, documented read-only. Each response reads it **once**,
  with no per-session copies.
- All three callers (`main.py:211/941/1153`) publishing is fine, because each publish is a
  complete, consistent snapshot.
- Test fixtures that reset session globals (`test_poll_cycle_perf.py:129–151`) reset this map
  too.

**Payload.** Add `lastAttached` and `created` (int epoch seconds, or `null` for 0 or unknown) in
`_build_session_items` **and** in the federation local-item builder (`main.py:1757`). Use one
shared normalising helper, and the same values in `_sessions_payload_key`.

**Tests:** the parser rules above, payload fields on both endpoints, the cache key changing when
only the times change, the poll-cycle `list-sessions` count still being 1, and the map being
replaced (not merged) across enumerations.

### 3.2 Strip layout (left → right)

```
┌──────────────────── #expanded-pills (scrolls only in the degenerate case) ───────────────────┐ ┌ outside ┐
[● current] [sib][sib] │ [📁 timebot 1 ▾] [📁 docker-harbor 1 ▾] [📁 quotewerks 1 ▾] …          [Other Sessions 12 ▾]
```

**Other Sessions moves out of the scrolling strip.** It becomes its own `flex-shrink:0` element
in `.expanded-header`, right after `#expanded-pills`, so no layout can push it off-screen.
The strip keeps `flex:1; min-width:0; overflow-x:auto` for the degenerate case where the
current pill plus collapsed home groups alone exceed the width.

### 3.3 Model (`buildExpandedPillsModel`, Phase 2)

- Add `sessionRecency(s)`, returning `max(lastAttached, created)`, or 0 when both are absent.
- `buildStripFolderGroups(sessions, settings, pool)`:
  - Returns no groups when `settings.autoViewsEnabled === false` (D0b).
  - Otherwise groups `pool` by `sessionGroupKey()` with no minimum.
  - Sets `recency = max(member recency)` and sorts by recency, newest first, then by name.
    Members are ordered the same way (R3).
  - Groups in a `Map` or a null-prototype object, so folder names like `__proto__` are safe.
- **Current folder** is resolved from the **unfiltered** session list: the current session's own
  `sessionGroupKey`, even when that session is hidden. It therefore stays a home group and never
  becomes a candidate.
- Home groups are unchanged apart from R3 sibling order.
- `candidates` = views (as today, D4) followed by recency-ordered folders, each with a stable `key`.
- `ungrouped` = keyless live, non-hidden sessions (no cwd, e.g. an older federated peer). It
  **excludes** the current session and anything already rendered in a home group.
- When D0b is off, the model emits today's flat `otherSessions` instead of folders.
- The model carries order only, never timestamps, so the signature changes only when the order
  does.

### 3.4 Layout (Phase 3)

`layoutStrip(model, measure, available)` is **pure**. It takes injected widths and returns
`{ siblingCounts, placedCount, overflow, showOther }`.

1. `mandatory` = current pill + home groups collapsed.
2. **Case A, no Other Sessions.** This applies only if `ungrouped` is empty. Run
   `allocateExpandedPills` on the full width; if *every* candidate also fits after it, the result
   has no Other Sessions pill.
3. **Case B, Other Sessions shown.** Subtract the Other Sessions pill width (measured with the
   worst-case count) from the available width. Run `allocateExpandedPills`, then
   `placeStripCandidates` (prefix, D6) on what remains.
4. Overflow = `candidates.slice(placedCount)` + `ungrouped`. The pill count is the number of
   **unique sessions** in the overflow, by `sessionKey` (a view and a folder can share a
   session), excluding the current session.

Because Other Sessions sits outside the scroll container (§3.2), Case B can never hide it. The
worst outcome is the strip scrolling in the degenerate case, as today.

### 3.5 Other Sessions menu with submenu (Phase 4)

- **Rows:** one per overflow entry, in overflow order: `📁 folder` (or `⧉ view`), session count,
  aggregated bell dot, trailing `›`. Each is a `<button>` with `aria-haspopup="menu"`,
  `aria-expanded` and `aria-controls="expanded-pill-submenu"`. A final `(no folder)` row appears
  when `ungrouped` is non-empty.
- **Submenu:** a new `#expanded-pill-submenu` (sibling of `#expanded-pill-menu`, `position:fixed`).
  Its rows reuse `_epMenuItemHTML`. **The existing open-session / ✎-rename click handler is
  factored into one function bound to both menu roots.**
- **Element lookup:** pills, rows and menus are found by exact `dataset` comparison over the
  candidates, never `querySelector('[data-…="' + key + '"]')`.
- **Mouse hover:** a delegated **`pointerover`** on the parent menu, filtered with
  `closest('[data-sub-key]')`. It ignores `e.pointerType !== 'mouse'` and moves within the same
  row.
  - Opening a different row's submenu waits **150 ms** and is cancelled if the pointer enters the
    open submenu first. That is the diagonal-travel grace.
- **Click/tap on a row:** if the row's submenu was just opened by mouse hover, the click **keeps
  it open** instead of toggling. Otherwise it toggles. Touch and pen get tap-to-toggle.
- **Escape has one precedence chain:** submenu, then menu, then terminal. `handleGlobalKeydown`'s
  fullscreen Escape branch first checks `_expandedPillMenuFor` / `_expandedPillSubmenuFor` (and
  the search dropdown) and only closes what is open. This **also fixes the pre-existing bug**
  (§1.2.4) for today's single-level dropdowns.
- **Click-outside:** both menu roots and the pill triggers count as inside. Choosing a session
  closes both menus.
- **Positioning:** measure the actual free space left and right of the parent menu's rect; open on
  the side that fits, else the larger side clamped to the viewport. Align the top with the row and
  clamp to the viewport height (`max-height: 60vh`, scrolls).
- **Lifecycle:**
  - Close the submenu when the parent menu scrolls.
  - Close both menus on window resize if the strip is hidden (<600 px) or the trigger is no longer
    rendered.
  - Re-anchor by key after any re-render.
- **Re-render survival:**
  - The open menu and submenu are kept by key (D7). If the submenu's entry left the overflow, the
    submenu closes.
  - **Focus is preserved:** before an `innerHTML` rewrite, record the focused element's
    `data-session` / `data-sub-key`, then re-focus the matching element afterwards. Scroll
    position is preserved the same way.
- Inline folder pills keep today's single-level dropdown, R3-ordered.

### 3.6 Files

| File | Change |
|---|---|
| `muxplex/sessions.py` | format, split-first parse, wholesale-replaced times map and getter |
| `muxplex/main.py` | `lastAttached`/`created` in both payload builders via one helper; `_sessions_payload_key` |
| `muxplex/tests/test_sessions.py`, `test_poll_cycle_perf.py`, `test_api.py` | §3.1 tests; reset the new global in fixtures |
| `muxplex/frontend/app.js` | `sessionRecency`, `buildStripFolderGroups`, model rework, `layoutStrip`/`placeStripCandidates`, render, dataset lookup, menu tree, submenu, shared menu click handler, Escape precedence in `handleGlobalKeydown`, focus preservation |
| `muxplex/frontend/index.html` | Other Sessions host outside `#expanded-pills`; `#expanded-pill-submenu` |
| `muxplex/frontend/style.css` | Other Sessions outside the strip; folder-row (`›`, count, bell) and submenu styles |
| `muxplex/frontend/tests/test_app.mjs` | §4; migrate assertions on `otherViews` / `otherSessions` / positional keys (`:6329`, `:6400`, `:6547`, `:6640`), keeping their behavioural intent |
| `CHANGELOG.md`, `pyproject.toml` | version bump (cache-buster) and entry, including the Escape fix |
| `docs/plans/2026-06-04-expanded-header-session-pills-design.md` | pointer: other-view / Other Sessions behaviour superseded by this plan |
| `CLAUDE.md` | contracts: strip folder groups are strip-local and honour the auto-views toggle while `buildAutoViews` stays ≥2; recency comes from tmux attach time; the list-sessions parse splits before trimming and is never worse than today |

## 4. Tests

- **Backend:** §3.1.
- **Recency:** `sessionRecency` with both fields, one, none, and `created` newer than
  `lastAttached`.
- **Model:**
  - singletons become pills; recency order with name tie-break; R3 member order;
  - the current folder is never a candidate, **including when the current session is hidden**;
  - keyless sessions go to `ungrouped`, excluding current and home-group members;
  - hidden and status sessions are excluded; remote sessions are keyed by `sessionKey`;
  - missing fields (older peer) sort last; `__proto__` / `constructor` folder names are safe;
  - `autoViewsEnabled:false` gives the flat Other Sessions (the existing test is kept).
- **Contract #6 regression:** `buildAutoViews` still drops singletons on a live-shaped fixture
  (18 folders, 2 multi-session).
- **Layout** (`layoutStrip` with injected widths, so the whole calculation is tested, not just a
  helper):
  - Case A, everything fits, so no Other Sessions;
  - the self-induced overflow case (both candidates fit only without Other Sessions);
  - a siblings-only case; prefix semantics; unique-session overflow count;
  - the degenerate minimum layout.
- **Menus** (extend the DOM and event stubs so handlers really register and dispatch):
  - the tree equals the overflow plus `(no folder)`; aggregated bell; ✎ only on local sessions;
  - the shared click handler works from **both** roots;
  - mouse `pointerover` opens after the grace delay, and a touch pointer does not;
  - hover-open then click stays open; tap toggles;
  - **Escape: submenu → menu → terminal, with `closeSession` NOT called while a menu is open**;
  - click-outside; keys containing `"`, `\`, `:` and Unicode;
  - the menu survives a re-render that reorders folders, and focus is restored;
  - the submenu closes when its folder leaves the overflow, or on resize below 600 px.
- All four suites green (`CLAUDE.md` § Tests).

## 5. Exit criteria

- Every item in #24's acceptance list is demonstrably met.
- **Manual check on the live instance.** The user accepts disruption, so restart the server from
  the worktree (`uv sync --extra dev` once in the worktree, then `.venv/bin/muxplex serve`) after
  the backend phase.
  - Desktop Chrome at roughly 700 / 1200 / 1900 px: pills fill to Other Sessions, Other Sessions
    is always visible, and the order matches tmux attach times.
  - Switching sessions moves the folder just left into the first slot within a few seconds.
  - Escape on an open menu does **not** leave the terminal.
  - Touch device at ≥600 px: tap opens the submenu, and tapping a session switches to it.
  - Don't send hand-written partial `PATCH /api/settings` (memory: it wipes views).

## 6. Risks

- **Reordering under the cursor:** order changes only on the user's own access, and keys and focus
  are preserved. If it still feels jumpy, freeze the order while a menu is open.
- **Poll churn:** singleton folders come and go. The signature guard and name keys bound the cost;
  the width cache gains about one entry per folder (cap 500).
- **Narrow desktop windows (600–800 px):** most folders land in Other Sessions, which is intended.

## 7. Follow-ups (not in scope; file separately if wanted)

- Make the grid's Settings → Sort → **"Recent"** real using the same fields; it is a no-op today
  (§1.2.3).
- Full arrow-key navigation for the header menus. This is an accessibility enhancement and was not
  adopted as a bug fix.
- Views retirement (requirement 5).
