# Expanded-Header Session Pills — Design

**Date:** 2026-06-04
**Status:** Approved 2026-06-04 (all open questions resolved — see bottom)
**Extends:** `2026-04-15-views-design.md` (views model), CHANGELOG v0.6.8 (header view pills)

## Goal

The expanded (terminal) header is mostly empty space. Turn it into a session-level
navigation strip so the user can jump between related sessions in 1 click (same
view) or 2 clicks (any other view), without leaving the terminal or opening the
sidebar.

## Verified data-model facts (drives the design)

- **A session MAY belong to multiple views.** `_opAddMembership` (app.js) appends
  to any view's `sessions` list with no exclusivity check. Only hidden ⟷ views
  is mutually exclusive (`hideSessionOp` removes from all views).
- View membership keys are `session.sessionKey || session.name`; `filterVisible()`
  matches either. We reuse the same matching.
- Sessions list entries may be status sentinels (`s.status` set) — always excluded.
- Hidden sessions: **excluded from this feature entirely** (decided). No Hidden
  pill, hidden sessions never listed.

## Terminology

- **Current session** — the session open in the terminal (`_viewingSession` +
  `_viewingRemoteId`).
- **Home views** — user views whose membership includes the current session
  (0, 1, or many), in `settings.views` array order.
- **Other views** — user views that do NOT include the current session.
- **Other sessions** — live, non-hidden sessions that are members of **no** user
  view (and are not the current session).

## Header layout (left → right)

```
[←] [☰]  [⦿ current] [sib-a] [sib-b] │ [sib-x] [grp2 ▾]   [ViewC 4 ▾] [ViewD 2 ▾]   …spacer…  [Other Sessions 6 ▾]  [⚙]
         └─ home group 1 ──────────┘ └─ home group 2 ───┘ └─ other-view pills ────┘           └─ right-aligned ──┘
```

1. **Current-session pill** — always first in the strip, distinctly styled
   (filled accent, like `.view-pill--active` but stronger), click = no-op.
   Appears **once**, far left, even when the session is in multiple home views;
   it is NOT repeated inside the groups.
2. **Home groups** — one group per home view, in `settings.views` order,
   separated by a subtle vertical bar (`.expanded-pills__sep`). Each group
   contains the view's *other* member sessions as click-to-switch pills, sorted
   alphabetically (case-insensitive; tie-break device name). **Dedup:** a
   sibling belonging to several home views renders only in the *first* such
   group.
3. **Other-view pills** — one dropdown pill per other view (views order, same
   7-view cap as the main header): label = view name + member count + caret.
   Click opens a dropdown listing that view's sessions (alphabetical); clicking
   a session switches to it (2 clicks total). Empty views are skipped.
4. **Other Sessions pill** — right-aligned (margin-left:auto), same dropdown
   pattern, listing the view-less sessions. Hidden when empty.

The plain `#expanded-session-name` label is **replaced** by the strip whenever
the strip renders; it returns below 600 px (strip hidden — mobile keeps today's
header + bottom-sheet switcher) and as a fallback when the strip is empty.

### Gating / degraded modes

- **1 home view** — the headline case: one inline group, as specced.
- **0 home views** — no inline groups; all views render as other-view dropdown
  pills + Other Sessions. Navigation always available.
- **2+ home views** — grouped layout above (current pill once at far left,
  one group per home view, separators, sibling dedup).
- **No user views at all** — only the Other Sessions pill renders (open
  question Q1 below).

## Responsive behavior (the collapse/expand algorithm)

The strip must guarantee, at minimum, **one pill per home view** (a collapsed
`ViewName +N ▾` dropdown holding that group's sessions) and grow fairly as the
viewport widens until every sibling is inline.

### Group states

A home group with `n` sibling sessions is in exactly one of:

- **Fully expanded:** `n` inline session pills, no dropdown.
- **Partially collapsed:** `k` inline pills + one `ViewName +overflow ▾`
  dropdown pill, where `0 ≤ k ≤ n−2`. The dropdown holds the remaining
  (alphabetically-last) sessions.

`k = n−1` is deliberately impossible: a dropdown pill is about as wide as the
session pill it would replace, so collapsing exactly one session saves nothing.
The final expansion step swaps the dropdown for the **last two** sessions at once.

### Allocation (pure function, width-measured)

```
allocate(groups, fixedItems, widths, available) -> per-group inline count
```

1. Measure every candidate pill's width once per data change (offscreen
   measurement node; cache keyed by label text — font/padding are fixed).
2. Start from the minimum layout: current pill + every home group collapsed
   (`ViewName +N ▾` only) + separators + other-view pills + Other Sessions.
   (If even this overflows, the strip scrolls horizontally like `.view-pills` —
   scrollbar hidden, content reachable.)
3. **Round-robin expansion** over home groups in views order: each round, try
   to move one more session inline in each group:
   - normal step: cost = next session pill width + gap;
   - final step (k = n−2 → fully expanded): cost = (last two session pill
     widths + gap) − dropdown pill width.
   Commit the step if it fits the remaining space, else mark the group done.
   Stop when all groups are done.

This "one more session per group per round" rule is the fair-share behavior:
small groups finish early and stop consuming rounds; big groups keep growing;
variable-length names are handled because every step is measured, not counted.

### Re-layout triggers

- Poll cycle (`pollSessions`) — rebuild model; **string-compare signature guard**
  (same convention as `renderViewPills`) so unchanged data + unchanged width is
  a no-op (no innerHTML churn, preserves open dropdown/hover state).
- `window.resize` — rAF-debounced re-allocation (widths cached; only the
  allocation + render re-run).
- `openSession()` / `closeSession()` — current session changed.
- View mutations already funnel through renders that run on the next poll tick
  (2 s); acceptable latency, no extra wiring.

An **open dropdown** whose pill survives re-render stays open (re-position);
if its pill disappears (view deleted / session gone), it closes — same spirit
as the flyout-close guard in `renderGrid`.

## Interactions

| Action | Result |
|---|---|
| Click sibling / dropdown session pill | `openSession(name, { remoteId })` — terminal switches; `_activeView` is NOT changed |
| Click current-session pill | no-op |
| Click view-dropdown pill | toggle its dropdown (only one open at a time) |
| Click outside / `Escape` | close any open dropdown |

Dropdown menus reuse the `.view-dropdown__menu` / `.view-dropdown__item` look,
but are **fixed-positioned** under the clicked pill (the strip is
`overflow-x: auto`, which would clip an absolutely-positioned child — same
trick as the sidebar view dropdown).

Session items/pills:

- Label = session name (ellipsis past `max-width`, full name in `title`).
- When `multi_device_enabled` and device badges are on, dropdown items append
  the `device-badge` (same as sidebar); inline pills keep name-only with the
  device in `title` (header space is precious).
- Identity carried as `data-session`, `data-remote-id`, `data-session-key`
  (same attributes as sidebar items).
- **Bell indicator (optional, Q3):** small amber dot on pills/items whose
  session has `bell.unseen_count > 0`, suppressed when activity indicator
  setting is `none`.

## Implementation map

| File | Change |
|---|---|
| `index.html` | Add `<nav id="expanded-pills" class="expanded-pills" hidden>` after `#expanded-session-name`; add shared `<div id="expanded-pill-menu" class="view-dropdown__menu hidden">` at body level (fixed positioning) |
| `app.js` | New pure functions: `buildExpandedPillsModel(sessions, settings, name, remoteId)` (groups/dedup/sorting/other-sessions) and `allocateExpandedPills(model, widths, available)` (round-robin fit). New render: `renderExpandedHeaderPills()` called from `pollSessions()`, `openSession()`, resize handler. Delegated attach-once listeners in `bindStaticEventListeners()` (contract #3 — never per-openSession). Export pure fns via the existing test-export block |
| `style.css` | `.expanded-pills` strip (flex, gap 6px, overflow-x auto, scrollbar hidden, `display:none` < 600px ⇒ name label returns), `.session-nav-pill` (reuses `.view-pill` metrics), `--current` variant (filled accent), `.expanded-pills__sep` (1px vertical bar), right-aligned other-sessions pill, bell dot |
| `pyproject.toml` | Version bump (cache-buster) before user-facing testing |
| `CHANGELOG.md` | Feature entry |

### Contracts honored

- Attach-once delegated listeners on static containers (frontend contract #3).
- String-compare render guard per poll cycle (contract #5 convention).
- No changes to terminal.js / key handling (contracts #1, #2, #4 untouched).

## Testing

`muxplex/frontend/tests/test_app.mjs` additions (pure-logic first):

1. **Model builder** — home-view detection (0/1/many), views-order grouping,
   sibling dedup across home groups, current session excluded from groups,
   alphabetical (case-insensitive) sorting, hidden + status exclusion,
   sessionKey vs name membership matching, other-sessions = member of no view,
   empty other views skipped, 7-view cap.
2. **Allocator** — minimum layout (all groups collapsed); fair round-robin
   (uneven group sizes); never k = n−1 (final step absorbs two); width-driven
   (long names consume budget); degenerate available-width ⇒ minimum layout.
3. **Render contracts** — current pill present once + distinct class; separator
   count = groups − 1; signature guard skips DOM write; strip replaces name
   label; clicking a session pill calls `openSession` with correct remoteId
   and does not change `_activeView`.

Manual checks: resize sweep 600→2200 px, multi-view membership, federation
session with duplicate name on two devices, dropdown open across poll ticks.

## Resolved questions (2026-06-04)

1. **No user views defined at all:** YES — still show the Other Sessions pill
   (= every other session) so the header is useful with the sidebar collapsed.
2. **Counts on dropdown pills** (`ViewC 4 ▾`, `Other Sessions 6 ▾`): YES —
   match the main-page pills.
3. **Bell dots on session pills/items:** YES — amber dot when
   `bell.unseen_count > 0`, suppressed when the activity-indicator setting is
   `none`.
