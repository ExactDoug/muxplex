# Bulk Multi-Select → Add to Views — Design

**Date:** 2026-06-04
**Status:** Approved (user chose ALL THREE surfaces)
**Related:** `2026-06-04-universal-session-search-design.md` (Surface 3 lives in its
results dropdown), `2026-05-17-hidden-state-redesign-design.md` (op layer)

## Goal

Assign many sessions to views/tags quickly, from three surfaces:

1. **Grid select mode** (overview) — a Select toggle makes tiles
   checkbox-selectable; a floating action bar applies `Add to View ▾` / `Hide`
   to the whole selection.
2. **Manage View panel upgrade** — batched Apply (one PATCH for any number of
   checkbox changes) and openable for ANY view without switching the active
   view first.
3. **Search-results multi-select** — checkboxes on result rows + per-view
   "+ ViewName" chips in a footer; search for a pattern, check matches, one
   click adds them all.

## Shared core (pure, tested)

- `bulkAddToViewsOp(settings, viewNames, keys)` — for each key: unhide (the
  established add-implies-unhide invariant) + add membership to each view.
  Returns `{hidden_sessions, views}` patch. One PATCH regardless of counts.
- `bulkHideOp(settings, keys)` — for each key: hide + remove from all views
  (mirrors `hideSessionOp`). Returns the same patch shape.

Both compose the existing `_op*` primitives on a `_cloneOpState` working copy.

## Surface 1 — Grid select mode

- `☑` toggle button in the overview header-actions (`#select-mode-btn`).
- In select mode, tile click/Enter/Space TOGGLES selection (highlight ring via
  `.session-tile--selected`) instead of opening; the ⋮ options button still
  works. Selection keyed by `data-session-key`.
- Floating bottom action bar (`#bulk-action-bar`): `N selected · Add to View ▾
  · Hide · Done`. Add to View opens a dropdown of user views; choosing one
  applies the bulk add and KEEPS the selection (so the same set can be added
  to a second view). Hide applies `bulkHideOp` and clears the selection (the
  tiles leave the grid). Done or Escape exits, clearing selection.
- Selection highlight + bar survive the 2s poll re-render (classes reapplied
  in `renderGrid`'s bind pass).

## Surface 2 — Manage View panel

- `openManageViewPanel(viewName?)` — manages `viewName`, defaulting to the
  previously-set target or the active view. `_manageViewTarget` is module
  state, cleared on close; `renderManageViewList()` resolves
  `_manageViewTarget || _activeView` (keeps direct-call tests/back-compat).
- The Views settings tab's per-view **Manage** button now opens the panel for
  that view directly — it NO LONGER calls `switchView()` first.
- **Batched commits:** checkbox toggles accumulate in `_managePending`
  (toggling back to the original state removes the entry); a footer
  `Apply (N)` button commits everything in ONE PATCH. Close discards pending
  changes. Rename/delete inside the panel operate on the managed view and
  only touch `_activeView` when the managed view IS the active one.

## Surface 3 — Search results

- Each result row gains a leading checkbox (row container becomes a
  `div[role=option]` — buttons cannot legally nest inputs). Checkbox click
  toggles selection; clicking anywhere else on the row still opens the
  session.
- With ≥1 selected, a footer renders: `N selected` + one `+ ViewName` chip per
  user view (7-cap). Clicking a chip bulk-adds the selection to that view
  (selection then clears; dropdown stays open and live).
- `closeSearch()` clears the selection. Search slim sessions now carry `key`
  (`sessionKey || name`) for bulk identity.

## Testing

- Pure ops: multi-key/multi-view membership, unhide-on-add, hide-removes-from-
  views, dedupe (existing member not duplicated), one patch shape.
- Manage panel: pending accumulates/cancels, apply composes one PATCH (source
  contract), target-view resolution.
- Grid/search: source + render contracts via the established mock-DOM and
  source-window patterns.
