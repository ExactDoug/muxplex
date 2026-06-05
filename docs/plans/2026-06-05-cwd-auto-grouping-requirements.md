# cwd Auto-Grouping — Detailed User Requirements

**Date:** 2026-06-05
**Status:** REQUIREMENTS ONLY — approved by the user; NOT yet audited against the
code, NOT yet planned, NOT yet implemented. The next session will: (1) spawn
sub-agents to audit the code relative to these requirements, (2) produce an
implementation plan, (3) implement.
**Decided by:** user, 2026-06-05 ("Do A … And do B")
**Builds on:** `2026-06-04-universal-session-search-design.md` (cwd/cwdLeaf/gitRepo
session metadata — ALREADY SHIPPED in v0.7.2), `2026-06-04-expanded-header-session-pills-design.md`,
`2026-04-15-views-design.md`

## Background / motivation

Sessions are usually launched per-project (often via `c-tmux`), so the active
pane's cwd top-leaf directory IS the project identity. Verified live examples
that name-based grouping cannot handle:

- `hello-world-call-pop-65a882de` and `hello-world-call-pop-9d48ae4d` (hash
  suffixes) both run in `…/github/hello-world-call-pop` — cwd leaf normalizes
  them into one group.
- `qw-animas` runs in `…/github/qw-bridge` — by cwd it belongs with
  `qw-bridge` / `qw-bridge-2` despite its unrelated name.

The per-session metadata (`cwd`, `cwdLeaf`, `gitRepo`) already flows through
`/api/sessions` and federation payloads (v0.7.2). These features consume it;
no new data collection is expected (audit to confirm).

---

## Feature A — Auto-views from cwd leaf (virtual views)

### Core behaviour

- A1. For each distinct `cwdLeaf` among **live** sessions, synthesize a virtual
  "auto-view" named after the leaf. Membership = the live sessions whose
  `cwdLeaf` matches, at poll time.
- A2. Auto-views are **never persisted** — not written to `settings.json`, not
  synced to federation peers, never touched by stale-key pruning. They are
  derived state, recomputed from poll data.
- A3. Auto-views appear alongside user views in:
  - the main-page view pills row,
  - the header + sidebar view dropdowns,
  - the expanded-header other-view dropdown pills,
  and are **visually distinguished** from user views (e.g. folder glyph /
  distinct style — exact treatment decided at planning).
- A4. Activating an auto-view filters the grid/sidebar exactly like a user
  view. Auto-view identity must be **namespaced** (e.g. `dir:qw-bridge`) so a
  user view with the same name never collides; display shows the bare leaf.
- A5. Auto-views are read-only: no manage panel, no rename, no delete, no
  membership editing, not selectable targets in bulk add-to-view / new-session
  picker / search chips (those operate on persisted membership only).
- A6. Hidden sessions are excluded from auto-view membership and counts (same
  rule as user views' visible counts).

### Viewport collapse priority (explicit user requirement)

- A7. Horizontal pill real estate prioritizes **user-defined views over
  auto-views**. As the viewport narrows, auto-view pills collapse into the
  views dropdown FIRST; user-view pills remain visible longest, collapsing
  only when space runs out even without auto-views. (The existing <600px
  behaviour — everything collapses to the dropdown switcher — is the end
  state.) This implies width-aware progressive collapse on the main header
  pills row; audit must assess the current CSS-only overflow behaviour vs the
  expanded-header allocator pattern.

### Open questions (resolve during planning)

- A8. Minimum group size: synthesize an auto-view for a single-session
  directory, or require ≥2 sessions? (Leaning ≥2 to avoid noise.)
- A9. Should the current session's auto-view contribute a "same directory"
  sibling group to the expanded-header pill strip (like home views do)?
- A10. Should auto-view names participate in universal search "tag" matching?
- A11. Settings toggle to disable auto-views entirely (default on)?
- A12. Cap on number of auto-view pills (user views have a 7-view cap)?
- A13. `cwdLeaf` vs `gitRepo` as the grouping key (worktrees/subdirs differ);
  current requirement is **cwd top-leaf name** per the user's words.

---

## Feature B — "Group by directory" grid mode

- B1. A grid layout mode on the main page that clusters session tiles under
  directory headers (the `cwdLeaf`), analogous to the existing
  grouped-by-device federation mode.
- B2. Available regardless of `multi_device_enabled` (the existing
  flat/grouped mode picker lives under Multi-Device settings — audit must
  propose where the directory mode fits: third value of `gridViewMode`,
  separate toggle, etc.).
- B3. Sessions with no known cwd (metadata missing — e.g. old federation
  remotes, tmux race) cluster under a final "Other"/ungrouped bucket.
- B4. Group headers sorted alphabetically; sessions within a group follow the
  existing sort-order setting. Active view filtering and hidden-session rules
  apply unchanged (grouping is orthogonal to view filtering).
- B5. Federation: remote sessions group by THEIR `cwdLeaf` (payloads already
  carry it); device badges still distinguish same-named directories across
  devices. Same-leaf dirs on different devices share one group (audit to
  confirm desirability; flag if it looks wrong in practice).
- B6. Status tiles (unreachable/auth_failed) follow the same placement rules
  as the device-grouped mode.

---

## Other outstanding items (carried context — not part of A/B)

- C1. **Cache-Control headers for static assets** (`muxplex/main.py`) — none
  are sent today; with `?v=` busting in place a long max-age is safe. Old
  backlog item, still open.
- C2. **Upstream PR** offering the fork's improvements to `bkrabach/muxplex` —
  process item, awaiting user go-ahead.
- C3. **Snapshot/content search** — search what's displayed in terminals, not
  just metadata. Declared future work in the search design doc.
- C4. **User feedback pass** — the user has observed oddities in the v0.7.x
  features and has NOT yet given the detailed list. Expect bug reports /
  refinements against pills, search, bulk select, and the new-session picker.

## Current shipped state (for the auditing session)

- v0.7.3 on branch tip (see CHANGELOG v0.7.0–v0.7.3). Key docs:
  - `2026-06-04-expanded-header-session-pills-design.md`
  - `2026-06-04-universal-session-search-design.md`
  - `2026-06-04-bulk-multiselect-views-design.md`
- Test baselines: Python 1315 (+5 deselected integration), frontend app 448,
  terminal suite 21 pass / 27 known environmental failures.
