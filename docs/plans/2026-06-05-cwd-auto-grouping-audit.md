# cwd Auto-Grouping — Code Audit

**Date:** 2026-06-05
**Status:** AUDIT — code surveyed against `2026-06-05-cwd-auto-grouping-requirements.md`
by five parallel read-only agents (view identity, pill collapse, grid grouping,
metadata coverage, search/exclusion surfaces). Open questions A8–A13, B2, B5 are
resolved with the user after this doc; decisions land in the implementation plan.

## Headline findings

1. **Metadata is production-ready (A-prereq confirmed).** `cwd`/`cwdLeaf`/`gitRepo`
   flow through `/api/sessions` and federation for all live local sessions; no new
   collection needed. Old remotes omit the keys (absent, not null) — frontend already
   tolerates via `(s.cwdLeaf || '')`.
2. **Architecture decision: auto-views must be synthesized as a SEPARATE frontend
   list, never merged into `_serverSettings.views`.** All 8 view-list consumers read
   `_serverSettings.views` directly (single source of truth, no copies). Keeping
   auto-views out of it makes every A5-excluded surface (bulk chips, bulk menu,
   new-session picker, search chips, keyboard digits) correct *by default* — zero
   per-surface filtering. Only the display surfaces (pills, two dropdowns,
   expanded-header) need additive code.
3. **Five guards are still required** even with the separate-list design (§3).
4. **A7 needs a new width-aware allocator pass on the main header** — current pills
   overflow is CSS-only (`overflow-x: auto` scroll). `allocateExpandedPills` is a
   pure, reusable algorithm precedent; its measurement infra (`_epMeasureWidth`,
   width cache) is shareable.
5. **Feature B is a small delta**: `renderGroupedGrid` (group-by-deviceName) is a
   direct template; backend already treats `gridViewMode` as an opaque synced string,
   so a third `'cwd'` value needs **no backend change**.

---

## 1. View-identity plumbing (audit a)

Views are opaque name strings everywhere; `'all'`/`'hidden'` reserved
(`views.py` `RESERVED_VIEW_NAMES`). A namespaced `dir:<leaf>` id flows cleanly
through `switchView` (app.js:1669), pill/dropdown click handlers (`.dataset.view`
extraction, app.js:5172+), and `filterVisible` (returns `[]` for unknown view —
benign, but auto-views need their own membership path).

**Active view persistence:** `_activeView` (app.js:222) is persisted to device-local
`state.json` via `PATCH /api/state` (app.js:1687 → main.py:568–588); NOT federation-
synced (`active_view` ∉ `SYNCABLE_KEYS`, settings.py:67). Restored on page load
(app.js:305) **without** running `_resolveActiveView()` — a pre-existing latent gap.

**Disappearing-view fallback today:** `_resolveActiveView(activeView, views)`
(app.js:845–852) returns `'all'` when the view isn't in `settings.views`. But it's
only invoked at state-restore/settings-patch points; a view deleted server-side can
leave a stale highlighted pill with an empty grid until the next resolve. For
auto-views (which vanish when their last session dies) we must resolve **every poll
cycle** against the synthesized list.

**Keyboard digits 2–8** index `settings.views` only (app.js:4600–4615) — auto-views
correctly unreachable by design (A5).

**Stale-key pruning** (`prune_stale_keys`, views.py:310) iterates persisted
membership only — no interaction with never-persisted auto-views. Confirmed clean.

## 2. Required guards (from audits a + e)

| # | Where | Guard |
|---|-------|-------|
| G1 | `views.py` `validate_view_name()` (~:275) | Reject names starting with the auto-view namespace prefix (reserve `dir:`), so a user can never create a colliding persisted view |
| G2 | `_resolveActiveView()` app.js:845 | A `dir:` id resolves against the *synthesized auto-view list*; falls back to `'all'` when the auto-view no longer exists. Run on every poll, and on state restore (app.js:305 currently skips it) |
| G3 | `openManageViewPanel()` app.js:2519–2525 | Extend the `'all'`/`'hidden'` rejection to `dir:` ids (A5: read-only) |
| G4 | `_createViewPicker()` pre-selection app.js:~4807 | Don't pre-select `_activeView` when it's a `dir:` id (else the namespaced id is sent to `createNewSession` → backend membership op on a nonexistent view) |
| G5 | Header dropdown "Manage \"<name>\"…" button app.js:1123–1126 | Suppress for `dir:` active views (would render `Manage "dir:qw-brid…"`) |

Surfaces clean by construction (separate-list design): bulk chips
(`_searchFooterHTML` app.js:3797), bulk menu (`_renderBulkViewMenu` app.js:4018),
new-session picker list (app.js:4790), search-tag chips, keyboard digits, federation
sync, stale pruning.

## 3. Display surfaces needing additive auto-view rendering (A3)

| Surface | Function | Change |
|---------|----------|--------|
| Main-header pills | `renderViewPills()` app.js:1053–1085 | Append auto-view pills after user views; `data-view="dir:<leaf>"`, display bare leaf + folder glyph/class; subject to A7 collapse |
| Header dropdown | `renderViewDropdown()` app.js:1091–1147 | Auto-view section between user views and "Hidden" |
| Sidebar dropdown | `renderSidebarViewsMenu()` app.js:1184–1220 | Same |
| Expanded-header other-view pills | `buildExpandedPillsModel()` app.js:3292–3357 | Include auto-views in `otherViews` (and `homeGroups` if A9 = yes), visually distinguished |

Plus a new pure synthesizer, e.g. `buildAutoViews(sessions, settings)`:
group live, non-status, non-hidden sessions by `cwdLeaf` (A6 — hidden excluded;
precedent: pool filter at app.js:3300), key `dir:<leaf>`, display = leaf.
Membership check is `session.cwdLeaf === leaf` at poll time, not a stored list.

## 4. Main-header pill collapse — A7 (audit b)

**Today:** `.view-pills` (style.css:1625) is `flex; overflow-x: auto` with hidden
scrollbar — pills scroll, never collapse. Hard 7-view render cap in JS. `<600px`:
whole row `display:none` (style.css:1695–1698) and the dropdown trigger swaps to the
dynamic active-view label (≥600px shows static "Views", style.css:1690). No resize
handling on this surface.

**Precedent:** the expanded header does true width-aware allocation:
- `allocateExpandedPills(groups, fixedWidth, available, gap)` app.js:3379–3419 —
  **pure**, deterministic fair-share round-robin: start all-collapsed, expand one
  item per group per round while width allows; final step absorbs the last two
  (never leaves a 1-item dropdown).
- `_epMeasureWidth(html)` app.js:3465–3484 — offscreen measure div, cached by HTML
  string, returns 0 in test envs.
- Re-render: string-compare signature (model+width) guard app.js:3512; rAF-debounced
  `resize` listener app.js:5670–5683.

**A7 shape:** simpler than the expanded header — a priority-drop, not group
expansion: measure each pill; render user-view pills first (drop into dropdown only
if even alone they overflow), then append auto-view pills while width remains;
overflowed auto-views live only in the dropdown. Reuse `_epMeasureWidth` + the rAF
resize pattern; a new small pure fn (e.g. `allocateViewPills(widths, available)`)
is cleaner than generalizing `allocateExpandedPills` (whose group/sessionWidths
model doesn't match). `<600px` end state unchanged.

**Tests:** allocator precedents are pure-fn tests (test_app.mjs:6010–6071) +
mock-DOM render-contract tests with width introspection (:6073+). Follow the same
pattern for the new allocator.

## 5. Grid grouping — Feature B (audit c)

**Mode selection today:** `renderGrid()` branches at app.js:1740 on
`_gridViewMode === 'grouped'` → `renderGroupedGrid(ordered, mobile)`
(app.js:1003–1028): Map keyed by `session.deviceName`, insertion order, emits
`<h3 class="device-group-header">` (CSS `grid-column: 1 / -1`, style.css:1852),
skips empty groups, `buildTileHTML` per tile. Status tiles (auth_failed/unreachable)
are ALWAYS appended after the grid HTML regardless of mode (app.js:1750–1753);
`status=empty` devices silently dropped (test_app.mjs:4833). Filtering
(`filterVisible`) and sort run BEFORE the mode branch — grouping already orthogonal
to view filtering (B4 satisfied structurally).

**Setting plumbing:** `gridViewMode` lives in `DEFAULT_SETTINGS` (settings.py:56),
is in `SYNCABLE_KEYS` (settings.py:78), patched opaquely — **no backend change for a
third value**. The `<select id="setting-view-mode">` (index.html:278–282) sits inside
`<div id="multi-device-fields">`, which `_updateMultiDeviceFieldsState()`
(app.js:4177) disables wholesale when multi-device is off — **this is the B2
conflict**: a `'cwd'` option there would be unreachable for single-device users.
Options for B2 (decide with user): move the View Mode field out of
`multi-device-fields` (relabel options "Flat / Group by device / Group by
directory", disable only the device option when multi-device off) vs a separate
toggle elsewhere.

**Delta for `renderCwdGroupedGrid`:** group key `s.cwdLeaf || <Other bucket>` (B3);
**alpha-sort group keys** (device mode uses insertion order — directory mode must
sort, "Other" last); within-group order = existing sort; device badges unchanged
(tile-level, gated `multi_device_enabled`, app.js:520–521) so same-leaf cross-device
groups stay distinguishable (B5); status tiles unchanged (B6); select mode keys by
sessionKey/name, works in grouped mode today — no special casing.

**Note for B5:** `gridViewMode` is federation-synced — switching to `'cwd'` on one
device changes all peers. Pre-existing behavior for flat/grouped; flag to user only
if it surprises.

## 6. Metadata coverage (audit d)

- `list_session_paths()` sessions.py:140–173 — one `tmux list-panes -a` subprocess
  per poll (~2s interval); active-window+active-pane only; tab-safe (`maxsplit=3`),
  malformed lines skipped, `{}` on tmux failure. Refreshed each poll under
  `state_lock` (main.py:193–197), exception-isolated.
- `_session_path_fields()` main.py:591–601 — missing cwd ⇒ all three fields `null`
  (keys present, null-valued) for **local** sessions; `cwdLeaf = basename(rstrip('/'))
  or cwd`.
- **Old federation remotes** (<v0.7.2): remote dicts are spread as-is
  (main.py:1420) ⇒ keys **absent** entirely. Frontend must (and does) treat
  absent/null alike. B3's Other bucket: key on `s.cwdLeaf || null`.
- `resolve_git_repo()` sessions.py:183–211 — pure walk-up, memoized (512-entry
  cache, cleared when full). **Worktree note for A13:** `.git`-file worktrees
  resolve to the *worktree directory* name, not the main repo — so for worktrees
  cwdLeaf ≡ gitRepo; they diverge only for subdirectory shells (cwdLeaf="src",
  gitRepo="myrepo").
- **Race window:** a session launched between polls appears without metadata for
  ≤1 poll cycle (~2s) — lands in Other/ungrouped briefly, then regroups. Acceptable;
  no crash paths.
- **Hidden orthogonal:** hidden sessions carry full metadata through the API;
  hiding is applied frontend-side after metadata attach. A6 exclusion happens in
  the synthesizer.
- Cost: ~10–50 ms/poll, steady-state cache-hit. No concerns.

## 7. Search & A10 (audit e)

`searchSessions()` app.js:3675–3755: rank order
`namePrefix(0) < name(1) < dir(2) < repo(3) < tag(4)` (app.js:3662). Tag matching:
substring-match the query against `settings.views[].name` (app.js:3691), then
expand to member sessions via `inView()` (persisted-membership lookup,
app.js:3685). Hidden sessions included-with-badge (`hidden` flag app.js:3742,
badge app.js:3775); status sentinels excluded (app.js:3696).

If A10 = yes: concat synthesized auto-views into the *matching* list only (not the
chips footer); `inView` needs an auto-branch (`s.cwdLeaf === leaf && !hidden` —
A6 applies inside search tag expansion too, even though search rows themselves show
hidden sessions). Tag badges (app.js:3772) should render the bare leaf with the
auto-view visual treatment. Ranking: reuse rank 4 (sessions matching by their own
cwdLeaf already match at rank 2 `dir`, so auto-view tags mostly add *sibling*
sessions — same-rank is fine).

Note: a query matching a leaf will surface that directory's sessions via the `dir`
field match (rank 2) regardless of A10 — A10's only marginal value is the tag badge
and any future leaf≠cwdLeaf cases. Low-stakes decision.

## 8. Test-impact inventory

- `test_views.py` — new cases: `validate_view_name` rejects `dir:` prefix (G1).
- `test_app.mjs` — new pure-fn suites: `buildAutoViews`, `allocateViewPills`,
  `renderCwdGroupedGrid` (mock-DOM, follow grouped-grid tests at :2698, :4833,
  :5509); guard tests G2–G5; A10 search tests if accepted.
- **Fixed-window source tests** (slice-based) exist around `createNewSession`
  (test_app.mjs:6257–6268 asserts signature + `Array.isArray(viewNames)` within a
  3000-char window) — touching that region may require widening windows (precedent
  exists). `renderViewPills`/`filterVisible` tests are data-driven, lower risk.
- `test_ux_fixes.py` regex-pins `openManageViewPanel`'s signature — G3 touches the
  body, not the signature; should be safe, verify.
- CSS: insert all new blocks BEFORE the final `@media (max-width: 959px)`
  (style.css:~2566; enforced by `test_frontend_css.py:471`).

## 9. Open questions → user (A8–A13, B2, B5)

Audit-informed leanings:

- **A8 min group size:** ≥2 (single-session auto-views ≈ noise; matches doc lean).
- **A9 expanded-header sibling group:** natural fit — `buildExpandedPillsModel`
  homeGroups extend cleanly; adds clutter for users with many dirs.
- **A10 search tags:** cheap; marginal value since `dir` field matching already
  covers most cases (see §7).
- **A11 settings toggle:** trivially cheap (frontend-only check around the
  synthesizer); doc default on.
- **A12 pill cap:** A7's width allocator IS a dynamic cap; a numeric cap on the
  *dropdown* list may still be wanted if leaf count explodes.
- **A13 grouping key:** cwdLeaf per user's words; worktrees identical either way;
  divergence only for subdir shells (audit d evidence).
- **B2 setting placement:** recommend relocating View Mode out of
  `multi-device-fields` as a 3-option select; device option disabled when
  multi-device off.
- **B5 cross-device same-leaf merge:** one group + device badges reads well given
  badges are tile-level; alternative is per-device subgroups (more complex).
