# Hidden State Redesign: Filter-at-Read, Single Visibility Helper

**Date:** 2026-05-17
**Status:** Design — ready for implementation
**Author:** bkrabach

## Background

The current implementation tangles two concerns: *placement* (which view a session
belongs to) and *presentation* (whether a session is hidden). The result is a
load-bearing invariant — "a session key cannot exist in both `hidden_sessions`
and any `view.sessions`" — enforced operationally on every add/remove/sync
(`views.py:enforce_mutual_exclusion`).

Because this invariant is enforced at write time but the count display reads
raw lengths at render time, the displayed counts can disagree with the rendered
grid. There are five count computation sites; only one of them goes through the
canonical filter (`getVisibleSessions`). The others read `v.sessions.length` or
`hidden_sessions.length` directly. See `app.js:894, 913, 1010, 1266, 2640`.

Concrete failure modes today:

- A dead tmux session leaves its key in `view.sessions` forever. The dropdown
  shows `Work (3)` but the grid renders 2 tiles.
- `hidden_sessions` accumulates stale keys across the lifetime of the install.
  The Hidden view label shows `(5)` but renders 0 tiles.
- `getVisibleSessions` does not cross-check `hidden_sessions` for user views. If
  the mutual-exclusion invariant is ever transiently violated (e.g., federation
  sync race), a hidden session **would** render in a user view.

## Principles

1. **Hidden is a property, not a placement.** A session can be in zero or more
   views and is independently marked hidden or not. View membership and hidden
   state are orthogonal.
2. **One filter function.** Every list-producing path and every count goes
   through a single canonical helper. No raw `.length` on stored arrays.
3. **Default exclude hidden, opt-in to include.** Display paths use the default.
   Management paths pass `include_hidden=True` explicitly.
4. **Compose, don't fuse.** Low-level data operations stay pure
   (`addMembership`, `hide`, `unhide`). User-intent operations compose them
   explicitly (`addSessionToView` = `unhide` + `addMembership`). The auto-unhide
   behavior remains, but it lives in a named operation a reviewer can see.
5. **Operational enforcement is replaced by read-time filtering.** The
   mutual-exclusion invariant is removed.

## Design

### Data model (no schema changes)

Storage is unchanged. Only the contract changes:

```json
{
  "views": [
    { "name": "Work", "sessions": ["dev1:proj", "dev1:build"] }
  ],
  "hidden_sessions": ["dev1:build", "dev1:old"]
}
```

The same key may now appear in both `view.sessions` and `hidden_sessions`. That
combination means: "this session is a member of Work, but is currently hidden."
It will not appear in the Work view by default; it will appear in the Hidden
view; it will appear *dimmed* in the Manage View panel for Work.

### The visibility helper (frontend)

Single function. Replaces all five count sites and `getVisibleSessions`.

```js
// app.js
function isHidden(key, hiddenSet) {
  return hiddenSet.has(key);
}

// Returns the canonical list of sessions for the requested view.
// view: "all" | "hidden" | <user view name>
// includeHidden: when true, do not filter hidden sessions out of user views
//                or "all". Ignored for "hidden" (always shows hidden only).
function filterVisible(sessions, settings, view, { includeHidden = false } = {}) {
  const hiddenSet = new Set(settings.hidden_sessions || []);
  const keyOf = s => s.sessionKey || s.name;

  // status-tile entries are never counted as sessions
  const live = (sessions || []).filter(s => !s.status);

  if (view === "hidden") {
    return live.filter(s => isHidden(keyOf(s), hiddenSet) || hiddenSet.has(s.name));
  }

  const visibilityPass = includeHidden
    ? () => true
    : s => !isHidden(keyOf(s), hiddenSet) && !hiddenSet.has(s.name);

  if (view === "all") {
    return live.filter(visibilityPass);
  }

  const userView = (settings.views || []).find(v => v.name === view);
  if (!userView) return [];
  const memberSet = new Set(userView.sessions || []);
  return live.filter(s =>
    (memberSet.has(keyOf(s)) || memberSet.has(s.name)) && visibilityPass(s)
  );
}

function visibleCount(...args) {
  return filterVisible(...args).length;
}
```

Counts that displayed `(N)` previously now call `visibleCount(_currentSessions,
settings, viewName)`. Manage View counts call
`visibleCount(_currentSessions, settings, viewName, { includeHidden: true })`
and additionally report the hidden-of-total breakdown (see UI section).

### Backend helper

If the server ever computes session lists (e.g., for sync or for a future API),
mirror the helper in Python:

```python
# views.py (replacing enforce_mutual_exclusion)

def is_hidden(key: str, settings: dict) -> bool:
    return key in (settings.get("hidden_sessions") or [])

def filter_visible(
    sessions: list[dict],
    settings: dict,
    view: str,
    *,
    include_hidden: bool = False,
) -> list[dict]:
    hidden = set(settings.get("hidden_sessions") or [])

    def key_of(s): return s.get("sessionKey") or s.get("name")
    def visibility_pass(s):
        if include_hidden:
            return True
        k = key_of(s)
        return k not in hidden and s.get("name") not in hidden

    live = [s for s in sessions if not s.get("status")]
    if view == "hidden":
        return [s for s in live if key_of(s) in hidden or s.get("name") in hidden]
    if view == "all":
        return [s for s in live if visibility_pass(s)]

    user_view = next((v for v in (settings.get("views") or []) if v["name"] == view), None)
    if not user_view:
        return []
    members = set(user_view.get("sessions") or [])
    return [
        s for s in live
        if (key_of(s) in members or s.get("name") in members) and visibility_pass(s)
    ]
```

### Operations: pure vs user-intent

Two layers. The UI calls user-intent operations. Tests verify both layers.

**Pure data operations** (low-level, no side effects beyond their name):

| Operation              | Effect                                                       |
|------------------------|--------------------------------------------------------------|
| `addMembership(k, v)`  | Append `k` to `settings.views[v].sessions` if absent         |
| `removeMembership(k, v)` | Remove `k` from `settings.views[v].sessions`               |
| `hide(k)`              | Append `k` to `settings.hidden_sessions` if absent           |
| `unhide(k)`            | Remove `k` from `settings.hidden_sessions`                   |

**User-intent operations** (high-level, used by UI):

| Operation                          | Composition                                                 |
|------------------------------------|-------------------------------------------------------------|
| `addSessionToView(k, v)`           | `unhide(k); addMembership(k, v)` — **auto-unhide here**     |
| `removeSessionFromView(k, v)`      | `removeMembership(k, v)` — does *not* hide                  |
| `hideSession(k)`                   | `hide(k)` — does *not* touch view membership                |
| `unhideSession(k)`                 | `unhide(k)` — does *not* touch view membership              |

The auto-unhide on "Add to View" is preserved as a deliberate user-intent
composition, **not** as an invariant enforced everywhere. Reviewers see
exactly one call to `unhide()` adjacent to `addMembership()` in
`addSessionToView`. Future operations that want to add membership without
unhiding (e.g., a bulk migration tool) can call `addMembership` directly and
the existence of that uncomposed path is obvious in code review.

### Federation sync

**Reality check first.** The current sync model (`settings.py:164-184`) is
**whole-list last-write-wins**: `apply_synced_settings()` replaces each
syncable key wholesale from the incoming payload, then runs
`enforce_mutual_exclusion` as a sanitization pass. Conflict resolution is via
a single `settings_updated_at` timestamp on the entire settings blob. There is
no per-key merge, no CRDT, no add-wins/remove-wins set semantics.

This refactor does **not** change that. We accept whole-list LWW for v1 and
document the consequences:

- If device A hides session X while device B adds X to a view, whichever
  device last wrote settings wins for *both* keys (`hidden_sessions` and
  `views`). The user may see a brief flicker between sync rounds. This is the
  same trade-off every other syncable setting already makes.
- Removing `enforce_mutual_exclusion` from `apply_synced_settings` is **not**
  safe in mixed-version federation (see Mixed-version safety below). It stays
  in place for v1 as a backstop. The read-time filter does not depend on the
  invariant for correctness; it just needs to handle overlap when overlap
  exists. Letting `enforce_mutual_exclusion` continue to strip overlap on
  incoming sync is fine — new code tolerates either presence or absence of
  overlap.

This means **Phase 3 (delete `enforce_mutual_exclusion`) is deferred** out of
the v1 plan. It becomes safe to remove only after all federated devices
publish a `_schema_version >= 2`. See Phase 3 in the implementation phases
section.

### Mixed-version safety (the blocking concern)

Without a schema version field, mixed-version federation will silently revert
user actions:

1. User on new device A hides session X (X is in view Work).
2. A's settings now have X in both `hidden_sessions` and `Work.sessions`.
3. Sync to old device B. B applies the new settings, then runs
   `enforce_mutual_exclusion` — silently strips X from `hidden_sessions`.
4. B saves with new `settings_updated_at`. Sync back to A. X is now unhidden.
5. User re-hides. Oscillation.

The mitigation: add a `_schema_version: int` field in **Phase 0** (a
prerequisite to all other phases). New clients write `_schema_version: 2`
into settings whenever they save. New clients detect old peers during
federation handshake (old peers send no `_schema_version` or `< 2`) and
behave conservatively:

- When sending to a legacy peer: do not produce settings where the same key
  appears in both `hidden_sessions` and any `view.sessions`. Pre-flatten by
  calling `enforce_mutual_exclusion` on the outgoing payload.
- When receiving from a legacy peer: accept the incoming state as-is (it
  already respects the old invariant).

This keeps mixed-version operation safe. Once all peers report
`_schema_version >= 2`, the pre-flatten step is unnecessary. Detection of
"all peers uniform" is a manual operator action for v1 (a log message
suffices); we do not need to automate it before deleting
`enforce_mutual_exclusion`.

### Stale key pruning (separate concern, local-only state)

Filtering at read time means stale keys no longer corrupt counts. But
`hidden_sessions` and `view.sessions` still accumulate dead keys forever and
sync them across the federation. Add a periodic pruning pass.

**Critical: pruning bookkeeping does NOT sync.** First-missed-at timestamps
are a per-device concern. Each device tracks which keys it has personally
failed to see, and prunes from its own settings when its grace period expires.
The actual prune (removing the key from `view.sessions` or `hidden_sessions`)
is a normal settings write that *does* sync via the existing LWW mechanism.
This avoids inventing new federation merge semantics.

Bookkeeping lives in a local sidecar file, not in `settings.json`:

```
~/.config/muxplex/pruning.json
{
  "first_missed_at": {
    "dev1:dead-session": 1747512345.0
  }
}
```

`pruning.json` is **not** in `SYNCABLE_KEYS` and is never sent to peers.

```python
def prune_stale_keys(settings: dict, live_keys: set[str], grace_seconds: float) -> dict:
    # 1. Update local first-missed-at bookkeeping based on live_keys.
    # 2. For each key in hidden_sessions or any view.sessions:
    #    - If key in live_keys: remove from first-missed-at (if present).
    #    - If key not in live_keys: ensure first-missed-at has this key.
    #      If first_missed_at[key] + grace_seconds < now: drop the key.
    # 3. Return modified settings (caller decides whether to save).
```

Runs on `_run_poll_cycle()` after the live session list is gathered
(`main.py:173`).

**Grace period rationale.** A tmux session disappears briefly during restart
cycles — minutes, occasionally hours. A user's intent to keep a session in a
view, however, can span days (a vacation, a long context-switch). The tension
is between cleaning up genuinely dead keys quickly and not surprising users.
Default: 24 hours, configurable via a new `stale_key_grace_hours` setting.
This is well above the worst-case restart duration and well below "I forgot
this view existed" timescales. Document the choice; revise based on use.

In a federated install, the grace period applies *per device*. A key not
seen on device A for 24h is dropped on A; B may still be tracking it.
B's eventual sync receives the deletion from A. This is the correct
behavior — A speaks for itself.

This phase is independent of the visibility refactor and can ship in its
own commit.

## UI changes

### Manage View panel

- Loads with `include_hidden=True`.
- Hidden sessions render dimmed (CSS class `session-hidden-dim`, ~50%
  opacity).
- Checkbox state for hidden sessions reflects membership (in view or not),
  independent of hidden state.
- Checking a hidden session that is not yet in the view calls
  `addSessionToView` — which unhides AND adds. The dim styling disappears.
- Unchecking a session calls `removeSessionFromView` (does not hide).
- A small "(hidden)" badge confirms hidden state for clarity.

### Settings panel view list

Show both numbers when relevant:

```
Work: 5 sessions (2 hidden)
Personal: 3 sessions
```

The bare "N sessions" form is used when no sessions in the view are hidden.
The "(M hidden)" suffix appears only when M > 0.

### Header & sidebar dropdowns

All counts go through `visibleCount(..., view, { includeHidden: false })`.
The bare numeric badge (e.g., `Work (3)`) reflects the count of sessions
actually rendered in the grid when that view is active. No exceptions.

### "Hidden" view label

The "Hidden" view count uses `visibleCount(..., "hidden")` — only counts
hidden sessions that are *live*. A stale entry in `hidden_sessions` for a
dead tmux session does not inflate this count. (The stale entry will be
removed by `prune_stale_keys` after the grace period.)

## Migration & backward compatibility

- **Settings files:** schema-compatible. Old files have no overlap between
  `hidden_sessions` and `view.sessions`; the new code handles that correctly.
  Until Phase 3 (deferred), new code never *writes* overlap either — it
  keeps `enforce_mutual_exclusion` as a backstop, so the on-disk schema does
  not change.
- **Schema version:** add `_schema_version: 2` to settings in Phase 0. Old
  clients ignore unknown keys (`settings.py:load_settings` only copies keys
  present in `DEFAULT_SETTINGS`), so this is safe to write. The version
  exists primarily as a marker for future Phase 3 — when all peers report
  `>= 2`, it is safe to delete `enforce_mutual_exclusion`.
- **Old clients reading new state:** identical to today (no overlap exists
  to drop, because we still call `enforce_mutual_exclusion` on save).
- **Key format normalization:** Phase 1 includes a one-time pass that walks
  `views[].sessions` and `hidden_sessions` on first load by the new code,
  upgrading bare-name entries to the canonical `device_id:name` form where a
  match exists in the current live session list. Entries that cannot be
  matched are preserved (they may match later). This pays the migration cost
  once and removes the need for dual-lookup at every read site.

## Implementation phases

Each phase is independently shippable. Order matters: Phase 0 is a
prerequisite to all others.

### Phase 0 — Schema version field

Tiny but load-bearing. Future-proofs the federation upgrade path.

- Add `_schema_version` to `DEFAULT_SETTINGS` (`settings.py:17-47`) with
  value `2`. Add to `SYNCABLE_KEYS` so peers see it.
- On every `save_settings`/`patch_settings` write, ensure
  `_schema_version` is set to `2` regardless of incoming patch (clients
  don't get to write older versions).
- Add a helper `peer_supports_v2(peer_settings)` that returns
  `peer_settings.get("_schema_version", 0) >= 2`.
- No behavioral change yet. This is just the marker.

### Phase 1 — Visibility helper + audit + key normalization

Single most important change. Source of truth for visibility lives here.

- Add `filterVisible()` and `visibleCount()` to `app.js`.
- Add `filter_visible()` to `views.py` (for backend symmetry, even if no
  current Python caller needs it).
- Replace every count site:
  - `app.js:894` (Hidden count) → `visibleCount(..., "hidden")`
  - `app.js:900-903` (All Sessions header) → `visibleCount(..., "all")`
  - `app.js:913` (User view header dropdown) → `visibleCount(..., v.name)`
  - `app.js:997-1000` (All Sessions sidebar) → same
  - `app.js:1010` (User view sidebar) → same
  - `app.js:1266` (Settings panel) → same, with `(M hidden)` suffix
  - `app.js:2640` (Manage View "in this view") →
    `visibleCount(..., view, { includeHidden: true })`
- Replace `getVisibleSessions()` body with a call to `filterVisible()`.
- Grep audit: search for `.sessions.length`, `hidden_sessions.length`,
  `hidden.length`. Each remaining occurrence must be justified in a comment
  or replaced.
- **One-time key normalization**: on first load by the new code (detected via
  absence of `_schema_version` in the on-disk settings, or via a new marker),
  walk `views[].sessions` and `hidden_sessions`. For each entry that is a
  bare `name`, check if any live session has a matching `name`; if so,
  upgrade the stored entry to `device_id:name` form. Entries that cannot be
  matched (no live session with that name) are left as-is. Run once, set
  `_schema_version: 2`, save. Subsequent loads skip the pass.
- After normalization lands, the `memberSet.has(keyOf(s)) || memberSet.has(s.name)`
  dual-lookup can be simplified to a single lookup. Keep the dual lookup
  initially — remove it in a follow-up commit once normalization has run on
  all production installs.

### Phase 2 — Operation layer

- Refactor the existing hide/unhide/add-to-view code paths into the four pure
  + four user-intent operations described above.
- Update existing call sites in `app.js` (`hideSession`, `unhideSession`,
  add-to-view flyout, Manage View checkbox handler) to call the user-intent
  operations.
- Document the layering in a header comment.

### Phase 3 — Remove `enforce_mutual_exclusion` *(deferred from v1)*

**Not part of v1.** Listed here for completeness; do not ship until all
federated peers report `_schema_version >= 2` and the operator confirms it
is safe to drop the legacy backstop.

When the time comes:
- Delete `views.py:enforce_mutual_exclusion` and its call site in
  `settings.py:apply_synced_settings()`.
- Delete the corresponding test cases in `tests/test_views.py`.
- Remove the outgoing-payload pre-flatten step added in Phase 0.
- Bump `_schema_version` to 3 if any further on-disk format change accompanies
  the removal.

Until this phase ships, the new code coexists with the old invariant. Counts
and rendering are correct; storage continues to enforce mutual exclusion. The
only user-visible cost is that the auto-unhide-on-add behavior cannot be
broken into "add without unhiding" without `enforce_mutual_exclusion`
reverting it on the next save. This is acceptable for v1 — that capability
is not exposed to users.

### Phase 4 — Stale key pruning (local-only bookkeeping)

- Add `prune_stale_keys()` in `views.py`. Reads & writes a local sidecar
  file `~/.config/muxplex/pruning.json` (see the design section above).
- The sidecar is **not** in `SYNCABLE_KEYS` and is never sent to peers.
- Wire into `_run_poll_cycle()` after the live session collection step
  (`main.py:173`).
- Add `stale_key_grace_hours` (default 24) to `DEFAULT_SETTINGS`. Syncable;
  affects every device that consumes it. Each device still tracks its own
  first-missed-at timestamps locally.

### Phase 5 — UI polish

- Manage View dim styling.
- Settings panel "(M hidden)" suffix.
- Any other discoverability improvements.

Phases 0–2 are the actual semantic change. Phase 4 is hygiene. Phase 5 is
presentation. Phase 3 is deferred. Don't combine them.

## Test plan

### New tests (frontend, `frontend/tests/test_app.mjs`)

- `filterVisible` matrix — every combination of:
  - view ∈ {"all", "hidden", `<user view>`, `<nonexistent view>`}
  - `includeHidden` ∈ {true, false}
  - session in / absent from `view.sessions`
  - key in / absent from `hidden_sessions`
  - key in both (the new permitted state; verify v1 storage never produces it,
    but the helper handles it correctly if it appears)
  - session-key format: `device_id:name`, bare `name`, both
- `visibleCount` matches `filterVisible().length` for the same inputs.
- Key normalization: bare-name entries upgrade to `device_id:name` when a
  match exists; remain bare when no match exists; idempotent across reloads.

### New tests (backend, `tests/test_views.py`, `tests/test_settings.py`)

- `filter_visible` mirroring the frontend matrix.
- `addSessionToView` unhides and adds membership.
- `addMembership` adds membership but does **not** unhide.
- `removeSessionFromView` removes membership but does **not** hide.
- `hide` and `unhide` do not touch view membership.
- Schema version: `save_settings` always writes `_schema_version: 2`.
- `peer_supports_v2`: returns true for `_schema_version >= 2`, false for
  missing or `< 2`.

### Federation tests (`tests/test_settings.py` or new `test_federation.py`)

- Round-trip: state with overlap, save, reload, assert content unchanged
  (with `enforce_mutual_exclusion` still active, overlap should be stripped
  on save — verify this is the v1 invariant).
- Mixed-version send: when serializing settings for a peer reporting
  `_schema_version < 2`, the outgoing payload has no overlap. Verify by
  constructing settings with overlap (force it past the backstop in a test
  helper) and asserting the outgoing form is flattened.
- Mixed-version receive: applying settings from a legacy peer is a no-op
  beyond the existing whole-list replacement. Verify `_schema_version` is
  not downgraded by an incoming payload that lacks it.

### Stale-key pruning tests (`tests/test_views.py`)

- Live key: pruning bookkeeping is cleared.
- Newly missing key: bookkeeping records `first_missed_at`, settings unchanged.
- Missing past grace: key removed from `view.sessions` and `hidden_sessions`,
  bookkeeping entry removed.
- Re-appeared after partial absence (less than grace): bookkeeping cleared,
  settings unchanged.
- Sidecar file: never written to `settings.json`, never sent over the wire.

### Updated tests

- Adapt existing view tests that asserted the old mutual-exclusion invariant.
  Several tests assume `add_to_view` removes from `hidden_sessions` as a side
  effect; rewrite them to call the user-intent operation explicitly and
  assert on both effects. (Keep the existing `enforce_mutual_exclusion`
  tests — that function stays in v1.)

### Removed tests

- None in v1. `enforce_mutual_exclusion` tests stay until Phase 3 ships.

## Out of scope

- API redesign for `/api/views` or `/api/sessions`. The wire format is
  unchanged.
- Conflict UI for federation merges. Last-write-wins on the hidden flag is
  acceptable for v1.
- Bulk operations (hide all in view, etc.). The current granularity is
  sufficient.
- Tooltip / hover state showing why a session is dimmed. The "(hidden)" badge
  is enough.

## Open questions

None that block implementation. Worth deciding before Phase 5:

- Should the Manage View panel default to `include_hidden=True`, or require
  an explicit "Show hidden" toggle? Recommendation: default to true. The
  whole point of the Manage View is management; hiding hidden items from a
  management UI defeats the purpose.

## References

- COE design review conversation, 2026-05-17 (workspace session
  e74700a2-5340-46be-8fae-df40a19204f3).
- Current implementation: `views.py:14-68`, `settings.py:24-65, 164-184`,
  `state.py:9`, `app.js:628-672, 894-1010, 1266, 2173-2236, 2640`.
