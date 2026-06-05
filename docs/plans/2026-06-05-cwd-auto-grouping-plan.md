# cwd Auto-Grouping — Implementation Plan

**Date:** 2026-06-05
**Status:** PLAN — pending user approval
**Inputs:** `2026-06-05-cwd-auto-grouping-requirements.md` (A1–A13, B1–B6),
`2026-06-05-cwd-auto-grouping-audit.md` (file:line anchors below come from it)
**Target version:** 0.8.0, branch `feat/cwd-auto-grouping` (plumbing pattern on
`feat-ehsp-wt2` until the wsl --shutdown lands)

## Resolved decisions (user, 2026-06-05)

| Q | Decision |
|---|----------|
| A8 | Min group size **≥2** live (non-hidden) sessions |
| A9 | **Yes** — expanded header gets a "same directory" sibling group |
| A10 | **Yes** — auto-view names participate in search tag matching |
| A11 | **Yes** — settings toggle, default ON |
| A12 | **No hard cap** — pills self-limit via width allocator; dropdown lists all |
| A13 | Group key = **`gitRepo`, fallback `cwdLeaf`** (worktrees identical either way; subdir shells fold into their repo group) |
| B2 | Relocate the View Mode select out of the multi-device fieldset to general display settings, 3 options; user can revert from the UI at any time (Flat restores today's exact behavior) |
| B5 | **One shared group** cross-device; tile-level device badges distinguish |

## Core design

- **Group key:** `sessionGroupKey(s) = s.gitRepo || s.cwdLeaf || null` (null ⇒ no
  auto-view membership; "Other" bucket in grid mode).
- **Auto-view identity:** `dir:<key>` (namespaced; display shows bare key + folder
  glyph). `dir:` becomes a reserved name prefix.
- **Auto-views are a separate synthesized list** — never merged into
  `_serverSettings.views`, never persisted/synced/pruned (A2). All A5-excluded
  surfaces (bulk chips/menu, new-session picker list, search chips, keyboard
  digits) stay correct by construction.
- **Membership is computed, not stored:** `sessionGroupKey(s) === key`, hidden and
  status-sentinel sessions excluded (A6).

---

## Phase 1 — Backend guard + synthesizer (pure logic)

1. **G1 — reserve the namespace** (`muxplex/views.py` `validate_view_name()` ~:275):
   reject names matching `^dir:` (case-insensitive), message
   `"names starting with 'dir:' are reserved for auto-views"`.
   Tests: `test_views.py` new cases.
2. **`buildAutoViews(sessions, settings)`** (new pure fn in `app.js`, exported for
   tests): pool = live sessions minus status sentinels minus hidden (precedent:
   pool filter app.js:3300); group by `sessionGroupKey`; keep groups with **≥2**
   members; return sorted alphabetically (case-insensitive):
   `[{ id: 'dir:'+key, name: key, sessions: [sessionKey…], count }]`.
   Respects the A11 toggle (returns `[]` when disabled).
   Cached per poll in module state `_autoViews` (recomputed in `pollSessions`).
3. **`sessionGroupKey(s)`** helper + export.

Tests (test_app.mjs, pure-fn style): grouping, ≥2 rule, hidden/status exclusion,
gitRepo-over-cwdLeaf precedence, missing-metadata sessions ignored, sort order,
toggle-off ⇒ empty.

## Phase 2 — View-identity guards (G2–G5)

1. **G2 — `_resolveActiveView()`** (app.js:845): add `autoViews` param; a `dir:` id
   resolves iff present in the synthesized list, else `'all'`. Call it
   (a) on state restore (app.js:305 — currently skipped, latent gap), and
   (b) each poll after `_autoViews` recompute, so a vanished auto-view falls back
   cleanly.
2. **`filterVisible()`** (app.js:646): `dir:` branch — filter pool (non-hidden,
   non-status) by `sessionGroupKey(s) === key`. Backend `filter_visible` untouched
   (auto-view ids never reach it).
3. **G3 — `openManageViewPanel()`** (app.js:2519–2525): extend reject guard to
   `dir:` ids.
4. **G4 — `_createViewPicker()`** (app.js:~4807): don't pre-select `_activeView`
   when it's a `dir:` id.
5. **G5 — header dropdown Manage button** (app.js:1123–1126): suppress for `dir:`
   active views.

Tests: guard behaviors; fallback-on-vanish; filterVisible dir-branch incl. hidden
exclusion. Watch `test_ux_fixes.py` signature regexes (bodies change, signatures
shouldn't — `_resolveActiveView` gains a param; verify no regex pins it).

## Phase 3 — Display surfaces (A3/A4) + A7 width-aware collapse

1. **`renderViewPills()`** (app.js:1053): after user pills (and before the Hidden
   pill logic stays as-is), append auto-view pills — class
   `view-pill view-pill--auto`, folder glyph (`📁` or CSS pseudo-element), label =
   bare key, count, `data-view="dir:<key>"`. Active state same as user pills.
2. **A7 allocator — `allocateViewPills(autoWidths, available)`** (new pure fn):
   user pills keep today's behavior (they collapse only via the existing <600px
   end state); the allocator decides **how many auto pills fit** in the width left
   after the fixed items (All + user pills + Hidden + dropdown trigger). Overflowed
   auto-views appear only in the dropdown. Reuse `_epMeasureWidth` (app.js:3465,
   width cache shared) and the rAF-debounced resize pattern (app.js:5670). Update
   the `_lastHtml` re-render guard to a signature including container width.
   `<600px`: unchanged (row hidden, dropdown switcher).
3. **Header + sidebar dropdowns** (`renderViewDropdown` app.js:1091,
   `renderSidebarViewsMenu` app.js:1184): auto-view section between user views and
   "Hidden", styled distinct, all auto-views listed (A12: no cap).
4. **Expanded header** (`buildExpandedPillsModel` app.js:3292):
   - A3: include auto-views in `otherViews` (visually distinguished pills).
   - A9: if the current session has a group key shared with ≥1 sibling, emit a
     directory home group (after view home groups; dedup via existing `seen` map;
     label = key + glyph).
5. **CSS:** `.view-pill--auto`, dropdown auto section, expanded-header auto pill
   styles — all inserted **before** the final `@media (max-width: 959px)` block
   (style.css ~:2566, enforced by `test_frontend_css.py:471`).

Tests: pure-fn allocator suite (precedent test_app.mjs:6010); mock-DOM render
tests for pills/dropdowns with auto-views (precedent :6073); model tests for the
A9 dir group (dedup, hidden exclusion, no group when solo).

## Phase 4 — Search integration (A10)

`searchSessions()` (app.js:3675): concat `buildAutoViews(...)` results into the
tag-matching list only (NOT `_searchFooterHTML` chips). `inView()` gains an
auto-branch (`sessionGroupKey(s) === key`, non-hidden — A6 applies to tag
expansion even though hidden sessions still appear with their own matches).
Rank: reuse `tag` rank 4. `_searchItemHTML` (app.js:3772): auto tags render bare
key with `search-tag--auto` styling.

Tests: auto-tag expansion, hidden excluded from auto-tag expansion but still
flagged on direct matches, chips footer free of auto-views.

## Phase 5 — A11 toggle + B2 settings relocation

1. **`autoViewsEnabled`** (default `true`): add to `DEFAULT_SETTINGS` +
   `SYNCABLE_KEYS` (settings.py:56/78 — same pattern as `gridViewMode`); checkbox
   in the display settings section; consumed only by `buildAutoViews`.
2. **B2:** move the View Mode field out of `#multi-device-fields`
   (index.html:276–282) into the general display settings; options:
   `flat` "Flat" / `grouped` "Group by device" / `cwd` "Group by directory".
   Disable the `grouped` option when multi-device is off (if currently selected
   and multi-device turns off, behave as flat — render branch already requires
   exact `'grouped'`). `gridViewMode` key, save/load plumbing (app.js:4311/4323)
   unchanged. Remove the field from `_updateMultiDeviceFieldsState()` scope
   (app.js:4177).

## Phase 6 — Feature B: directory-grouped grid

1. **`renderCwdGroupedGrid(ordered, mobile)`** (new, modeled on
   `renderGroupedGrid` app.js:1003): group by `sessionGroupKey`; **alpha-sorted
   keys** (device mode is insertion-order; directory mode sorts), `Other` bucket
   last for null keys (B3); within-group order = incoming sort (B4); header
   `<h3 class="device-group-header dir-group-header">📁 <key></h3>` (reuse
   `grid-column: 1 / -1` style.css:1852). Grouping happens after
   filterVisible/sort — view filtering stays orthogonal (B4). No min-size rule
   here (grid mode shows every group, including singletons — the ≥2 rule is
   auto-views-only).
2. **`renderGrid()` branch** (app.js:1740): `else if (_gridViewMode === 'cwd')`.
3. Status tiles: untouched (appended after grid HTML, app.js:1750 — B6).
   `status=empty` stays silently dropped (test_app.mjs:4833). Device badges
   untouched (tile-level — B5). Select mode: no special casing (keys by
   sessionKey/name).

Tests: mock-DOM grouped render (precedent :2698), Other bucket, alpha order,
hidden filtering before grouping (:5509 precedent), status tiles after groups,
federation sessions with absent metadata land in Other (old remotes, B3).

## Phase 7 — Ship

1. Full suites: `uv run pytest -q -m "not integration"` (baseline 1315 + new),
   `node muxplex/frontend/tests/test_app.mjs` (baseline 448 + new),
   `node .../test_terminal.mjs` (27 env failures = baseline, only NEW names count).
2. Docs: CHANGELOG 0.8.0, README dashboard section, CLAUDE.md doc map + contracts
   if any new invariant deserves pinning; bump `version` in pyproject.toml
   (rotates `?v=` cache-buster).
3. Commits (conventional, plumbing pattern):
   `git add -A && TREE=$(git write-tree) && NEW=$(git commit-tree "$TREE" -p "$(git rev-parse refs/heads/feat-ehsp-wt2)" -m msg) && git update-ref refs/heads/feat-ehsp-wt2 "$NEW"`
   then `git push origin <sha>:refs/heads/feat/cwd-auto-grouping`; PR → main.
4. User verification on their live instance (backend changes ⇒ their server needs
   a restart; hard-refresh or rely on the version bump).

## Known edge cases handled

- Active auto-view vanishes (last sibling dies / gets hidden) → resolve-on-poll
  falls back to `'all'` (G2).
- New session race (≤1 poll cycle without metadata) → briefly ungrouped, then
  regroups; Other bucket in grid mode.
- Old federation remotes (absent cwd/gitRepo keys) → no auto-view membership,
  Other bucket; never crash (B3).
- User view named like a directory → no collision (namespace) and new names with
  `dir:` prefix rejected (G1). Existing persisted views can't have the prefix
  (validation predates any release with auto-views).
- `gridViewMode` is federation-synced (pre-existing) → switching to `cwd` on one
  device changes peers, same as flat/grouped today.

## Test-impact watchlist (from audit §8)

- Fixed-window source tests around `createNewSession` (test_app.mjs:6257) — widen
  windows if Phase 2 G4 grows that region.
- `test_ux_fixes.py` signature regexes — signatures of pinned functions unchanged.
- CSS end-block test — all new CSS inserted before the 959px block.
