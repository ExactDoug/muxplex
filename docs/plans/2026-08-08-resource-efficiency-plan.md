# muxplex resource-efficiency plan

**Branch:** `perf/resource-efficiency` · **Worktree:** `.worktrees/perf-resource-efficiency`
**Date:** 2026-08-08 · **Base:** `main` @ `84ac710` (v0.9.6.dev5)
**Status:** proposal — nothing implemented yet
**Revision:** rev 2, after an adversarial verification pass. See
[Revision log](#revision-log) for what rev 1 got wrong — four risk ratings were
incorrect, two of them ("zero risk") would have shipped real bugs.

---

## Objective and constraints

Reduce muxplex's resource cost (CPU, process spawns, disk I/O, event-loop blocking,
memory) **without regressing perceived responsiveness or changing any user-visible
behavior**, and in a way that holds up equally at **1 session and at 100**.

Three hard constraints govern every item below:

1. **No UX regression.** Poll cadence stays at 2 s. Bells, snapshots, session
   appearance/disappearance, and federation must feel exactly as immediate as they do
   today. Nothing here may be implemented by "polling less often" — that is a UX
   downgrade wearing an efficiency costume.
2. **No performance regression.** Latency of `/api/sessions`, `/api/state`, and terminal
   attach must be equal or better after every change.
3. **Scale-invariance is the point.** Prioritization is driven by *how cost grows with N
   sessions*, not by absolute cost today. An O(N)-per-poll cost that is invisible at N=3
   is what makes N=100 unusable; an O(1) cost that is slightly wasteful stays slightly
   wasteful forever and is low priority.

### Measured baseline (2026-08-08, this machine)

| Component | Measurement |
|---|---|
| muxplex server RSS | **58.6 MB** (PSS 49.3 MB, RssAnon 41.3 MB), `VmHWM` == first sample |
| RSS trend, 2 min idle | **flat / −1.0 MB**, 0 swap, 6 threads pinned |
| FD count | oscillates 15↔105 per poll burst, **no monotonic floor rise** |
| `uv run` wrapper process | ~30 MB (pure launcher overhead) |
| ttyd | 1.8 MB, exactly one, port-mutexed |
| tmux server | 19.7 MB |
| tmux pane trees (45 panes) | **~9.7 GB — ~95% of used RAM** |

**Conclusion that frames this whole document:** muxplex's *memory* is already fine and is
not the problem worth solving. There is no leak of consequence and no growth trend. The
real cost is **per-poll work that scales with N** — process spawns, blocking disk I/O on
the event loop, and full DOM rebuilds. That is what this plan targets. Memory items are
included for completeness but ranked low.

---

## Cost model: what one poll cycle costs today

Constants: server `POLL_INTERVAL = 2.0s` (`muxplex/main.py:90`), client `POLL_MS = 2000`
(`app.js:115`), `HEARTBEAT_MS = 5000` (`app.js:116`), `SETTINGS_SYNC_INTERVAL = 15`
cycles ≈ 30 s (`main.py:92`). Let **N** = live sessions, **B** = open browser tabs.

| Work item | Where | Cost per cycle | Scales with |
|---|---|---|---|
| `list-sessions` | `main.py:187` | 1 tmux spawn | O(1) |
| `capture-pane` ×N (concurrent, unbounded) | `main.py:191`, `sessions.py:308-329` | N tmux spawns | **O(N)** |
| `list-panes -a` | `main.py:196` | 1 tmux spawn | O(1) |
| `poll_bell_flag` ×N (**sequential**) | `main.py:225` → `bells.py:82-89` | N tmux spawns, **serialized, inside `state_lock`** | **O(N), latency too** |
| `load_settings()` ×2 + 2 throwaway `json.dumps` | `main.py:295-306` | 2 file reads, 2 deepcopies, 2 full serializations | O(1), large constant |
| `load_pruning_state()` + **unconditional** `save_pruning_state()` | `main.py:325`, `main.py:341` | 1 read + **1 write, every cycle** | O(1) |
| `load_settings()` (prune) | `main.py:324` | 1 more file read + deepcopy | O(1) |
| `load_state()` / `save_state()` | `main.py:203`, poll + heartbeat + every request | **blocking I/O on the event loop** | O(B) |

**At N=20: `2N+2` = 42 tmux `fork`/`exec` every 2 s ≈ 21 process spawns/second**, ~1.8 M/day.
At N=100 that is 202 spawns per cycle — and the bell half is *serialized inside the lock*.

**Honest scope note:** fixing the bell half (item 1.1) takes this to `N+3`, not to O(1).
The N `capture-pane` spawns remain and are addressed separately by item 1.2. Bells are
first because they are the *serialized, lock-held* half — the part that produces
user-visible stalls — not because they are the larger spawn count.

---

## Priority 0 — prerequisites (NEW in rev 2)

These are not efficiency work. They are the safety net that everything else needs, plus
two genuine pre-existing bugs found during verification. **Do these first.**

### 0.1 Atomic writes for `settings.json` and `pruning.json` ⭐ do this before anything else

`settings.py:139` (`save_settings`) and `pruning.py:56` (`save_pruning_state`) use a bare
`write_text`. `state.py:166-175` correctly uses the tmp-file + `os.replace` pattern.
There is **no file locking anywhere** in the tree (`grep fcntl|flock` → nothing).

**The failure chain:** a concurrent write (see 2.3 for the list of out-of-process
writers) during a server read yields a truncated file → `load_settings` catches
`JSONDecodeError` (`settings.py:113`) and **silently returns `DEFAULT_SETTINGS`** — empty
`views`, empty `hidden_sessions`, `settings_updated_at = 0.0`. If a `PATCH
/api/settings` lands in that window, `patch_settings` merges onto defaults and
**permanently destroys every view**.

This is very likely the mechanism behind the previously-observed view-wipe incident.
Adopting `os.replace` in both writers is a one-line change per site and is a **hard
prerequisite for 2.3** (and arguably for anything that touches settings at all).

**Risk:** none. **Effort:** trivial. **Priority:** highest in the document.

### 0.2 Fix the prune write that can never win LWW

`main.py:344-346`'s comment claims the prune deletion "triggers LWW sync on next cycle".
**It does not.** `save_settings` (`settings.py:127-140`) never touches
`settings_updated_at` — only `patch_settings` (`settings.py:196-197`) does. So the prune
write lands on disk with an **unchanged** timestamp, never wins last-write-wins, and is
never pushed to peers. Worse: any peer with a higher `settings_updated_at` pushes the
dead key **straight back** (`main.py:143-145` `apply_synced_settings`) — a resurrected
pruned key, ping-ponging indefinitely.

**Fix:** bump the timestamp on the prune write (or route it through `patch_settings`).
The same applies to the normalize write at `main.py:304`, though there it may be
intentional — decide explicitly and comment the decision.

**Risk:** low, but it touches federation semantics — needs a test. **Effort:** small.

### 0.3 Test-suite prerequisites

Three gaps make the rest of this plan unverifiable:

- **No shared `conftest.py`.** Every test module defines its own autouse fixture
  redirecting `STATE_PATH` / `SETTINGS_PATH` / `PRUNING_STATE_PATH` / `TTYD_PID_PATH` /
  `IDENTITY_PATH` to `tmp_path`. A **new** test file therefore gets the **real**
  `~/.config/muxplex` by default. Given 0.1's failure mode, that is a live footgun.
  Add a `conftest.py` with the redirect fixtures (copy the block at `test_api.py:16-42`
  plus `test_settings_sync_poll.py:30-35` and `test_pruning.py:30`).
- **`_run_poll_cycle` has zero non-integration coverage.** Its only tests are in
  `test_integration.py` (`-m integration`, deselected by the default `addopts`). Every
  Priority 1-2 change lives in this function.
- **No test counts tmux spawns.** The central thesis of 1.1 (N → 1) is unverifiable
  today. No new infrastructure is needed: `patch("muxplex.bells.run_tmux",
  AsyncMock(...))` gives `call_count` and `call_args_list` for free, and `call_count`
  assertions already have precedent in `test_api.py`, `test_ttyd.py`, `test_ws_proxy.py`.

**Deliverable:** `conftest.py` + a `test_poll_cycle_perf.py` driving `_run_poll_cycle()`
directly with N synthetic sessions, asserting (a) tmux spawn count is O(1) in N for the
bell path, and (b) a second consecutive cycle performs zero pruning writes.

### 0.4 Restore the dev environment

The worktree venv lacks the `dev` extra, so **there is currently no valid Python
baseline**: `bs4` is missing (`test_frontend_html.py`, `test_main.py` fail to collect)
and `pytest-asyncio` is missing (all 50 "failures" are `async def functions are not
natively supported` — environment artifacts, not real failures). Run `uv sync --extra
dev` in the worktree and re-baseline before touching code.

---

## Priority 1 — O(N) work that must become O(1)

### 1.1 Collapse N sequential bell polls into one tmux call ⭐ highest leverage

**Today** (`bells.py:43-47`, called in a loop at `bells.py:82-89`):

```
tmux display-message -t <session_name> -p '#{window_bell_flag}'
```

one subprocess **per session, awaited one at a time**, with the whole loop inside
`state_lock` (held from `main.py:184`).

Two compounding problems:
- N subprocess spawns per cycle.
- **Serialized latency**: N × spawn-RTT (~2-5 ms) = 40-100 ms at N=20, **200-500 ms at
  N=100** — all holding `state_lock`, blocking `/api/state`, `/api/heartbeat`, and every
  other state reader. This is the clearest path to user-visible stalls at high N.

**Fix:** one batched query, then feed the existing transition logic unchanged.

#### Verified requirements (rev 2 — the rev 1 formulation was subtly wrong)

**(a) Semantics differ — this is NOT a pure refactor.** `-t <session>` resolves to that
session's **current window only** (`display-message` completes an incomplete target to
the session's current window). `window_bell_flag` is a *window* variable (man tmux 3.4:
`window_bell_flag  1 if window has bell`); **there is no session-level bell format
variable.** So today = "does the session's *current* window have a bell"; batched-with-OR
= "does *any* window". For a 3-window session where a background window bells, today says
False and OR says True.

Choose explicitly:
- **OR across windows** — `any(flag == "1")`. **Recommended**: a bell in a background
  window is precisely the notification worth surfacing. But it **is a behavior change**
  and must be stated in the CHANGELOG. Rev 1's "no behavior change, no UX risk" claim
  was **false**.
- **Exact parity** — add the server-side filter `-f '#{window_active}'` (man tmux
  1399-1405), yielding one row per session and zero client-side filtering.

**(b) Delimiter must be TAB, not space.** `validate_session_name` (`sessions.py:79-97`)
rejects only `.`, `:`, control chars, and `dir:` — **spaces are allowed**, and sessions
created outside muxplex are enumerated too. There is an established convention to reuse:
`list_session_paths` (`sessions.py:184-190`) uses
`'#{session_name}\t#{window_active}\t…'` with `split("\t", 3)`. Use
`'#{session_name}\t#{window_bell_flag}'` parsed with `rsplit("\t", 1)` (safest — the flag
is always the last field and always `0`/`1`).

**(c) Missing sessions default to `False`.** Preserves today's `except RuntimeError:
return False` semantics for grouped sessions (the one case not verifiable read-only),
zero-window sessions (impossible in tmux, but free to handle), and the
enumerate→query race.

**(d) Widen the exception catch.** `poll_bell_flag` catches only `RuntimeError`;
`enumerate_sessions`/`list_session_paths` also catch `FileNotFoundError`. With one call
there is one total failure point, so the broader catch matters more.

**(e) Keep `poll_bell_flag` as a thin single-session helper** and add a new
`poll_all_bell_flags()`. This is the cheap path: 3 unit tests + 1 integration test keep
working unchanged. Deleting it means rewriting 9 tests.

**Live verification (45 sessions, tmux 3.4):** `list-windows -a` returned exactly one row
per session, agreeing with the per-session calls, covering all detached sessions, no
coverage loss. **Caveat: all 45 sessions have exactly one window, so the live test
structurally cannot detect the semantic divergence in (a).** Treat it as a smoke test,
not a validation.

**Rejected alternative:** `#{session_alerts}` (man tmux 2956). Two disqualifiers — it
conflates bell + activity + silence, and it is *client-visible alert* state cleared by
client attention, which defeats the documented premise of this fallback path
(`bells.py:59-63`: it exists for when **no client is watching**).

**Test impact:** five `process_bell_flags` tests `patch("muxplex.bells.poll_bell_flag",
AsyncMock(...))` **by name**. If the loop stops calling it, those patches become no-ops
and the tests either fail or silently hit real tmux. They must be re-pointed at the new
function even if `poll_bell_flag` is retained per (e).

**Also:** gather the flags *outside* `state_lock`, then take the lock only to mutate state.

**Risk:** low with (a)-(e) applied. **Effort:** small + test rework.

### 1.2 Bound and batch the snapshot path

`snapshot_all` (`sessions.py:308-329`) spawns `capture-pane -e -p -S -30` per session
concurrently via an **unbounded** `gather`. Concurrency saves latency but not spawn
count; at N=100 it forks 100 processes simultaneously every 2 s.

- **(a) Semaphore the gather** (16-32 concurrent). Pure win, no behavior change, trivial.
  **Do this in Phase 3.**
- **(b) Only snapshot what is rendered.** Snapshots fill grid tiles; sessions hidden by
  the active view or off-screen don't need a fresh 30-line capture every 2 s. Turns the
  dominant O(N) term into O(visible), bounded by screen real estate. Requires a new
  client→server contract (see Open Questions).
- **(c) Snapshot-on-change.** Detect unchanged panes cheaply (`#{window_activity}` from
  the batched call) and skip their captures. Composes with (a).

**Acceptance criterion for (b)/(c):** a tile that stops updating because it scrolled
off-screen must refresh **immediately** on return to view.

**Risk:** (a) none; (b)/(c) moderate. **Effort:** (a) trivial, (b)/(c) medium.

### 1.3 Front-end render guards — REDESIGNED in rev 2

Rev 1 proposed a whole-grid `_lastHtml` string compare mirroring `renderViewPills`.
**Verification showed that formulation is a no-op in practice and, implemented literally,
a memory leak.** Three findings:

**(i) A whole-grid compare is an AND across all N tiles.** One Claude session rendering a
spinner (or `top`, `tail -f`, a progress bar) invalidates the entire grid every poll. Hit
rate ≈100% when the whole fleet is idle, ≈0% during working hours — **the exact inverse
of scale-invariance**, since bigger N means more likely that *something* is animating.

**(ii) An HTML compare cannot recover the cost this item exists to remove.**
`ansiToHtml` (`app.js:467`) runs *inside* `buildTileHTML:608` — i.e. **before** any HTML
string exists to compare. Only a signature over **raw inputs** short-circuits ahead of
the parse.

**(iii) A guard around the `innerHTML =` line is an unbounded listener leak.** `on()`
(`app.js:262-264`) is a bare `addEventListener` with a **fresh arrow closure per call** and
no dedup. Today this is safe *only* because `innerHTML =` destroys the old nodes. Guard
just the assignment and the bind loop at `app.js:2074-2096` keeps stacking handlers on
surviving tiles — after an hour, ~1800 click listeners per tile, and one click fires
`openSession` 1800 times. Same defect in `renderSidebar` (`app.js:1058-1067`).

#### Revised design — three sequenced steps

**Step 1 (prerequisite, not a follow-on): delegate tile clicks.** Replace the
`app.js:2074-2096` bind loop with a single delegated `click`/`keydown` listener on
`#session-grid` in `bindStaticEventListeners` (`app.js:5869`), mirroring the existing
`.tile-options-btn` delegation (`app.js:5869-5875`, whose comment already says "tiles are
re-rendered each poll"). Same for `renderSidebar` on `#sidebar-list`. **Without this, the
guard is a leak rather than an optimization.**

**Step 2: hoist poll-invariant side effects above the guard** — the stale-flyout check
(`app.js:2007-2015`) and `updatePillBell()` (`app.js:2099-2101`). The latter reads
`_currentSessions[].bell.unseen_count` including **hidden** sessions that never appear in
the HTML, so an early return would leave the fullscreen pill badge stale.

**Step 3: per-tile signature guard.** Key each tile by `sessionKey`; per tile compute

```
sig = name | sessionKey | remoteId | deviceName | priority | snapshotRaw | settingsEpoch
```

where `settingsEpoch` folds `isMobile()`, `ds.viewMode`, `ds.activityIndicator`,
`ds.showDeviceBadges`, `multi_device_enabled` — and, **for the sidebar**,
`currentSession`/`currentRemoteId` (the sidebar's click handlers close over those, so
omitting them binds stale closures). Skip `buildTileHTML` — and therefore `ansiToHtml` —
when a tile's sig matches its node's stored `_sig`; replace only changed tiles.

`renderExpandedHeaderPills`' `_lastSig` (`app.js:3957-3959`) is the correct precedent —
it is an early return over a derived signature, computed before the expensive measuring
pass. `renderViewPills`' `_lastHtml` is safe *there* only because pill HTML has zero
volatile content **and** pill clicks are already delegated; neither property transfers.

**`snapshotRaw` must stay in the signature** — it *is* the tile's visible content. It
arrives as a fresh string per poll so identity comparison won't work, but a raw string
compare is still far cheaper than the char-by-char `ansiToHtml` parse it avoids.

**Deliberately exclude `last_activity_at`.** `formatTimestamp` (`app.js:7-14`) returns
`"Ns ago"` — changing every second — but the server never emits the field
(`main.py:641-642`, `main.py:1470-1475`), so it currently renders empty. **The day anyone
wires it up, any timestamp-inclusive guard silently dies.** If it is ever populated,
refresh `.tile-meta` via a separate cheap `textContent` pass.

**Grouped modes** (`renderGroupedGrid` `app.js:1164`, `renderCwdGroupedGrid`
`app.js:1207`) interleave `<h3>` headers with tiles in one flat string; per-tile
reconciliation there needs keyed headers plus insert/remove/reorder handling. Acceptable
interim: apply reconciliation to **flat mode only**, keep whole-grid rebuild in
grouped/cwd modes.

**Bonus, not a regression:** select-mode highlighting *improves*.
`session-tile--selected` is applied via `classList` (`app.js:2076-2078`) precisely
because the DOM is destroyed every 2 s; skipping the rebuild lets it survive naturally,
and the reapply loop becomes vestigial. Confirmed non-issues: there is no tile
drag-and-drop, and `filterBar.innerHTML = ''` (`app.js:2071`) is dead code.

**Also note** `renderSidebar` early-returns unless `_viewMode === 'fullscreen'`
(`app.js:1024`), so it is already free in grid mode — its share of the win is smaller
than rev 1 implied. Its unguarded empty-state write (`app.js:1031`) should be guarded too.

**Test impact:** `test_frontend_js.py`'s `renderGrid`/`renderSidebar` tests regex the
function body with a pattern terminating at the first line starting with
`function`/`//`/`window.` — **an early-return guard with a `//` comment at column 0
truncates the captured body and fails ~10 tests.** Write the guard without a col-0
comment, or update the regex. `test_app.mjs:3918` asserts `reconcileViewingSession`'s
*source text* contains `/api/state` — relevant to item 2.6, not this one, but the same
class of coupling.

**Risk:** medium (was "low"). **Effort:** medium (was "small").

---

## Priority 2 — constant-factor waste in the poll cycle

### 2.1 Stop rewriting `pruning.json` every 2 seconds — CORRECTED in rev 2

`main.py:341` calls `save_pruning_state(_prune_state)` **unconditionally** (~43,200
writes/day), while the adjacent `save_settings` at `main.py:345` is correctly guarded by
`_prune_changed`.

> ⚠️ **Rev 1 said "guard it on `_prune_changed`, risk: none". That is WRONG and would
> silently disable stale-key pruning entirely.**

`prune_stale_keys` (`views.py:355-462`) returns `(settings, pruning_state,
settings_changed)`, and its docstring (`views.py:373-375`) is explicit that
`settings_changed` is True **iff a key was actually removed**. Pruning state mutates in
four places and **three leave `settings_changed == False`**:

| # | views.py | mutation | `settings_changed`? |
|---|---|---|---|
| a | `:424` | `first_missed.pop(key)` — key came back alive | **False** |
| b | `:429` | `first_missed[key] = now` — **starts the grace clock** | **False** |
| c | `:433-442` | grace expired → prune + delete bookkeeping | True |
| d | `:459-461` | GC of bookkeeping for unreferenced keys | **False** |

Case (b) is fatal. `_prune_state` is re-read from disk every cycle
(`main.py:325 load_pruning_state()`), so it is not carried in memory:

1. Cycle 1 — key goes missing, `first_missed[key] = t1`, `settings_changed=False`, **not saved**.
2. Cycle 2 — `load_pruning_state()` returns a file without the entry → set to `t2`. Not saved.
3. …forever. `now - first_missed[key]` is always ~0, the grace period **never** elapses,
   stale-key pruning is permanently and silently dead.

Every existing `prune_stale_keys` unit test would still pass, because they test the
function, not the persistence loop.

**Correct fix:** guard on a **pruning-state** dirty flag, true for all of (a)-(d). Either
return a fourth bool from `prune_stale_keys`, or snapshot
`dict(pruning_state.get("first_missed_at", {}))` **before** the call and compare after —
a shallow copy suffices (`first_missed` is a flat `dict[str, float]`), and it must be
taken before the call because `prune_stale_keys` mutates in place and injects
`"first_missed_at"` if absent (`views.py:404-405`).

**Test impact:** the 4-tuple option breaks 5 tests in `test_views.py` that unpack exactly
3 values. The snapshot-compare option breaks none — **prefer it**.

**Risk:** medium if done naively, low if done as specified. **Effort:** small.

### 2.2 Kill the double-`json.dumps` change detector — SAFE with one condition

`main.py:299-301` serializes the whole settings dict **twice per cycle** purely to
compare before/after normalization, then discards both strings.

Verified: `normalize_session_keys` (`views.py:126-178`) mutates exactly two things —
`settings["hidden_sessions"]` (`:171-172`) and each `view["sessions"]` (`:174-176`), both
assigned a **new list** from `upgrade()`. Nothing else is touched, and both sides of the
compare are post-`load_settings()` dicts, so the compare cannot be catching schema or
default-backfill drift either. **The `json.dumps` compare is not load-bearing.**

**The condition:** the `mutated` flag must be computed as `upgraded_list !=
original_list` (list inequality), **not** "did any `name_to_key` lookup hit".
`upgrade()` also performs **dedup** (`views.py:161-168`) — collapsing pre-existing
duplicates is a real mutation with zero key upgrades, and a naive flag would lose that
write. Accumulate across `hidden_sessions` **and every view** (`mutated = a or b or …`,
no short-circuit that skips evaluation).

**Test impact:** 6 tests in `test_views.py` call `result = normalize_session_keys(...)`
then subscript `result["hidden_sessions"]`. Changing the return type to `bool` or a tuple
breaks all 6 — keep returning the dict and expose dirtiness via a second channel.

**Risk:** none with the condition applied. **Effort:** trivial.

### 2.3 In-memory settings cache — DEFERRED in rev 2 (was "medium risk")

`load_settings()` is called from **11 sites** (`main.py:281, 295, 324, 669, 806, 884,
1004, 1056, 1082, 1234, 1454`), three per poll cycle, each doing
`copy.deepcopy(DEFAULT_SETTINGS)` (`settings.py:109`) + a file read + `json.loads`.

> ⚠️ **Verification answered the decisive question and the answer disqualifies the simple
> version: other processes DO write `settings.json` while the server is running.**

- `cli.py:978` — `config set` → `patch_settings`
- `cli.py:1001`/`:1004` — `config reset` → `patch_settings` / full-file overwrite
- `cli.py:1141` — `setup-tls` → `patch_settings`
- `service.py:142` — `service install` → `patch_settings`

None check whether the server is running, and `README.md:185` documents hand-editing the
config. **The failure is not a lost local edit — it is stale data laundered into a newer
timestamp, which LWW then makes authoritative fleet-wide:**

1. Server caches settings (`settings_updated_at = 100`).
2. User runs `muxplex config set hidden_sessions '["dev1:foo"]'` → disk has hidden=[foo], ts=200.
3. Server cache still says hidden=[], ts=100.
4. Any server-side write calls `save_settings(cached)` — which writes the **whole merged
   dict** — restoring hidden=[] and (via `patch_settings`) bumping ts=300.
5. 30 s later `_sync_settings_with_remotes` sees local ts=300 > every peer and **pushes
   hidden=[] to every peer**. Irreversible under LWW.

**If it is ever done, these are mandatory:** invalidate on `st_mtime_ns` (**not**
`st_mtime` — 1 s float resolution is coarser than the 2 s poll and misses same-second CLI
writes) plus `st_size`; put invalidation **inside `settings.py`**, not at the 11 call
sites; never TTL the stat; and hand out a `deepcopy` since every caller mutates the
result — **which is most of the cost the item was trying to remove.**

Residual TOCTOU remains regardless: server stats at t, CLI writes at t+ε, server writes
at t+2ε. Today's code has the same race with a ~microsecond window; **the cache widens it
by roughly five orders of magnitude.**

**Verdict: defer.** Poor cost/benefit. If revisited, 0.1 (atomic writes) is a hard
prerequisite. Additional hazard: `test_settings.py` and `test_api.py` monkeypatch
`SETTINGS_PATH` **per test**, so a module-level cache not keyed on the current path
object leaks state across tests and produces order-dependent failures.

### 2.4 Move state/settings disk I/O off the event loop

`state.py:154-175`, `settings.py:111/140`, `pruning.py:38/56` use blocking
`open`/`write_text`/`os.replace`. `read_state`/`write_state` (`state.py:183-192`) wrap
them in an asyncio lock but **not** a thread — so every poll, every heartbeat (5 s per
tab), and every `/api/sessions` and `/api/state` request does blocking file I/O **on the
event-loop thread**: roughly **5 full `state.json` round-trips per second** at 4 tabs.

**Fix:** `asyncio.to_thread` for the read/write primitives — **but measure after 0.1,
2.1, and 2.2 land**, since those materially cut the call volume and may make this
unnecessary. Preserve ordering via the existing lock.
`test_state.py::test_state_lock_is_asyncio_lock` breaks only if the lock type changes.

**Risk:** low-medium. **Effort:** small.

### 2.5 `/api/sessions` serialization — SPLIT in rev 2

`main.py:617-647` per request: `get_session_list()` copies the list (`sessions.py:37`),
`get_snapshots()` **deep-copies the whole snapshot dict** (`sessions.py:42` — N × 30
lines of ANSI text), rebuilds a dict per session, then FastAPI serializes it all. At
N=20, ~40 KB × B clients every 2 s.

**2.5a — shared per-cycle serialization + drop the deep copy. SAFE, do it.**
Serialize once per poll cycle and hand the same body to all B clients; avoid the
`get_snapshots()` deep copy where the data is read-only downstream. No protocol change,
no frontend change, no ETag. This is the real win in the item.

**2.5b — ETag / 304. DROP or defer.**

> ⚠️ **Rev 1 claimed "a 304 is a no-op for [the client]". That is factually wrong.**

`app.js:277-284` is the single `fetch` call site and throws on any non-2xx
(`Response.ok` is 200-299). A 304 lands in `pollSessions`' catch (`app.js:424-427`) →
`_pollFailCount++` → **permanent connection-error UI, grid never re-renders**. Even after
fixing `api()` to pass 304 through, `await res.json()` (`app.js:398`) on a bodyless 304
throws `SyntaxError`. There is no `If-None-Match` and no `cache:` option anywhere in
`app.js`; the client would need to cache the last payload and reuse it on 304.

Two further disqualifiers: when `multi_device_enabled` the client polls
`/api/federation/sessions`, **not** `/api/sessions` (`app.js:394-396`), so the ETag helps
single-device users only — and the hit rate is ~0 for exactly the sessions users are
watching, since any active pane's snapshot changes every cycle.

If ever revisited: derive the ETag mechanically as `blake2b(body)` computed once per
cycle — which sidesteps enumerating inputs, but requires serializing anyway, further
shrinking the win. Note also that **2.6 couples to this**: folding `active_session` in
adds an input mutated *outside* the poll cycle, so a cycle-counter ETag would be
insufficient. Bell state has the same property (`POST /api/sessions/{name}/bell` is the
*primary* bell path).

**Zero ETag/304/`If-None-Match` tests exist anywhere today.**

### 2.6 Fold `/api/state` into the poll response

`app.js:421` → `reconcileViewingSession` (`app.js:369-373`) issues a **second** request
every 2 s per fullscreen tab to read one field, doubling request rate and adding a
blocking `load_state()` server-side. **Fix:** carry the field on `/api/sessions`.

**Test impact:** `test_app.mjs:3918` asserts the *source text* of
`reconcileViewingSession` contains `/api/state` and `reconcileOnly` — removing the fetch
fails it directly. Keep `/api/state` as an endpoint (10 `test_api.py` tests depend on it).

**Risk:** low. **Effort:** small.

---

## Priority 3 — process and lifecycle hygiene

### 3.1 Kill ttyd on server shutdown — VERIFIED SAFE (rev 1 over-flagged this)

`ttyd.py:218` spawns with `start_new_session=True`; the lifespan shutdown
(`main.py:401-419`) closes the httpx client and cancels the poll task but **never calls
`kill_ttyd()`**. Ctrl-C therefore leaves ttyd running indefinitely, holding port 7682, a
PTY, and a `tmux attach` client; recovery is retroactive via `kill_orphan_ttyd()`
(`main.py:378`) on next startup.

**Why this is safe — three independent confirmations:**

1. **The kill is single-PID SIGTERM, never a group kill.** `kill_ttyd` does
   `os.kill(pid, SIGTERM)` from the PID file; `_kill_pids_on_port` does `lsof -ti :7682`
   → per-PID kill. No `os.killpg`, no negative PID. The tmux server listens on a **unix
   socket**, so it can never appear in `lsof -ti :7682`. No `destroy-unattached`,
   `exit-empty`, `remain-on-exit`, or `kill-server` anywhere in the source.
2. **Upstream issue #7 is a different mechanism.** Its destruction comes from systemd's
   **cgroup-wide** kill (`KillMode=mixed` → SIGKILL to all remaining cgroup processes)
   hitting the *tmux server and the shells inside sessions*. Killing one ttyd PID shares
   neither mechanism nor victims. Issue #7 even lists orphaned ttyd clients as a
   *concern of its own fix* — upstream wants clean ttyd shutdown.
3. **The `start_new_session` comment doesn't mean what it appears to.** Commit `65b5c3a`
   ("fix: ttyd dies immediately"): without it, ttyd was cleaned up "when the asyncio
   transport is garbage collected after the HTTP handler completes." "Survives
   independently" = survives past the HTTP handler, **not** past server shutdown. There
   is no session-loss bug in this history.

Killing ttyd is already routine: `main.py:761` (connect), `:780` (user disconnect),
`:1171` (reattach), `:378` (startup sweep). Users' ttyd is SIGTERM'd on every session
switch today. Data loss on kill: none — the tmux client dies, the session detaches, panes
keep running.

**Conditions (all required):**
1. Patch `muxplex.main.kill_ttyd` in the lifespan fixtures (`test_api.py:18`,
   `test_main.py:37`) alongside `kill_orphan_ttyd` — otherwise the suite signals a
   **developer's real running ttyd** when run on the dev box.
2. Wrap in `try/except` + log, consistent with the other shutdown cleanups.
3. **SIGTERM-only, single-PID.** No `killpg`, no SIGKILL on the shutdown path, never the
   tmux server. Reaching for a group kill is exactly how this becomes issue #7.
4. **Do not touch `KillMode=mixed`** as part of this item — that is issue #7's separate
   fix (`KillMode=process` + re-attach). Conflating them turns a safe change into a
   destructive one.

**Risk:** low with conditions. **Effort:** trivial.

### 3.2 Escalate `kill_ttyd` to SIGKILL on timeout — has a test contradiction

`ttyd.py:134-143`: after 2 s of SIGTERM polling, `kill_ttyd` unlinks the PID file and
returns `killed=True` **regardless of whether the process died** — a wedged ttyd loses
its PID-file handle and is reachable only via the `lsof` fallback, which silently no-ops
when `lsof` is absent (`ttyd.py:71-74`).

**Blocker to resolve first:** `test_ttyd.py::test_kill_ttyd_removes_pid_file` explicitly
asserts the PID file is removed *"regardless of whether process was alive"* —
**directly contradicting** the proposed "only unlink on confirmed exit". That test must
be rewritten as part of this item, which is a deliberate behavior decision, not an
oversight to patch around. `test_kill_ttyd_reads_pid_file_and_sends_sigterm` also
inspects the `os.kill` call log by signal and is sensitive to probe ordering.

**Risk:** low. **Effort:** small + test rewrite.

### 3.3 Guard the PID-file write

`ttyd.py:213` `TTYD_PID_PATH.write_text(str(proc.pid))` is unguarded; on failure the ttyd
is **live with no PID file and no `_active_process`**, trackable only via the port
fallback. **Fix:** try/except that kills the just-spawned process on write failure.
Note `test_spawn_ttyd_uses_correct_command` asserts the argv list **exactly** — safe for
a try/except, breaks on any spawn-flag change (relevant to 5.3).

**Risk:** none. **Effort:** trivial.

### 3.4 `FIRST_COMPLETED` instead of `gather` in both WS proxies

`main.py:1206` / `main.py:1332`: `gather` waits for **both** coroutines, but
`client_to_ttyd` blocks in `await websocket.receive()` (`main.py:1188`) until the browser
acts. If ttyd dies while an idle tab is open, the handler task, both coroutines, the
accepted WebSocket, and the upstream connection stay resident — one stranded connection
per idle tab per ttyd restart.

**Fix:** `asyncio.wait(..., return_when=FIRST_COMPLETED)` then cancel the survivor,
ensuring the cancel path still runs the existing `finally` close logic
(`main.py:1209-1213`, `1335-1339`). `test_ws_proxy.py` is the only suite with
call-count discipline — review it before touching.

**Risk:** low. **Effort:** small.

### 3.5 Bound WebSocket write buffers — investigate, do not fix speculatively

`websockets.connect` (`main.py:1181`, `:1301`) is called with no `max_queue` /
`write_limit` override, and Starlette's `send_bytes` goes through uvicorn's transport
queue. A slow reader on a firehose pane is a plausible unbounded-buffer path — **not
confirmed**. Needs a deliberate slow-client load test first.

---

## Priority 4 — memory items (deliberately last)

Measured evidence says none of these are hurting anything.

### 4.1 Prune `_bell_seen`
`bells.py:27` — inserted at `:97, :102, :174`, **no `del`/`pop` anywhere** (only `.get`,
assignment, and a `.clear()` in tests). `main.py:218-220` deletes dead sessions from
persisted state but never mirrors it; `rename_session` (`main.py:905`) leaves the old key.
The only genuine insertion-without-eviction path in the package. Cost: one short string +
a bool per name ever seen. **Fix:** prune against `name_set` alongside step 6. Trivial;
worth doing for tidiness. The `test_bells.py` autouse `reset_bell_seen` fixture already
exists; no test asserts eviction.

### 4.2 Cap `_pillWidthCache`
`app.js:3649`, populated at `:3912-3928`, keyed on **complete pill HTML** embedding the
session **count** (`:3926-3932`) — so the key space is (views × names × bell states ×
counts), not (pills). **Fix:** clear above ~500 keys, or key on a normalized `label +
count-digit-width` signature (strictly better — the measurement depends only on text
length, not the count's value).

### 4.3 Evict `_federation_cache` on peer removal
`main.py:1441`, evicted only on 401/403 (`:1504`). Removing a remote from settings strands
its cached session list, including full pane snapshots, for the process lifetime. Fix
opportunistically while touching federation code.

### 4.4 Not-bugs — verified clean, do not churn

- **Contract #3 is upheld** — every container-level listener is a module-level
  attach-once IIFE; `openTerminal()`'s search-bar listeners are safe because each element
  is `cloneNode`/`replaceChild`ed first.
- **xterm lifecycle is correct** — `createTerminal()` disposes the prior `_term`;
  `closeTerminal()` disposes term/fit/search and disconnects the ResizeObserver.
  **No WebGL/canvas addon is vendored**, so the classic renderer leak cannot occur.
- **`resolve_git_repo` is memoized** (`sessions.py:270-305`, cache at `:211`, capped at
  512) — **0 stat calls per poll** in steady state. Do not "optimize" it.
- **Snapshot caches rebind rather than mutate** (`sessions.py:51-53, 66-67`); snapshots
  capped at 30 lines (`:153`).
- **One `httpx.AsyncClient`** (`main.py:393`), reused, properly `aclose()`d.
- **One background task** (`main.py:379`), cancelled and awaited in lifespan.
- `run_tmux` (`sessions.py:114-123`) always `communicate()`s — no pipe leak.
- **No tile drag-and-drop exists**; `filterBar.innerHTML = ''` (`app.js:2071`) is dead code.

---

## Priority 5 — invocation and environment

### 5.1 Drop the `uv run` wrapper for long-lived serving
`uv run muxplex serve` leaves a **~30 MB supervisor** resident for the server's lifetime —
more than half the server's own footprint, for zero runtime benefit. `.venv/bin/muxplex
serve` (or the systemd unit) removes it. Document in `CLAUDE.md`/README; keep `uv run`
for one-shot commands and tests.

### 5.2 Prefer the systemd unit for long sessions
Eliminates the duplicate-`serve` failure mode that `_kill_stale_port_holder`
(`cli.py:317-351`, commit `000c71c`, "2075+ restarts before manual intervention") exists
to paper over. **Caveat added in rev 2:** it does **not** make 3.1 unnecessary, and
`KillMode=mixed` carries upstream issue #7's session-destruction exposure — see 3.1
condition 4. Verify this fork's `service.py` against #7 separately.

### 5.3 ttyd client options — verify before changing
`ttyd.py:204-217` passes no `-t` client option; ttyd's default xterm.js `scrollback` is
1000 lines per browser client. muxplex sets its own in `terminal.js:360` (`mobile ? 500 :
5000`), so this is likely moot — **verify which value wins** before touching. `-m 3`
already bounds client count. Note `test_spawn_ttyd_uses_correct_command` asserts argv
exactly and will break on any flag change.

### 5.4 Out of scope: tmux `history-limit`
Global `history-limit` is **50000** — per pane, across 45 panes. muxplex never reads or
sets it and should not start. Noted only because the ~9.7 GB of pane memory dwarfs
everything here. Not a muxplex change.

---

## Upstream context (NEW in rev 2)

**Divergence is severe and accelerating.** No `upstream` remote is configured. Upstream
`bkrabach/muxplex` is at v0.44.0 (2026-08-02) with a different version lineage entirely:

| File | This fork | Upstream | Δ |
|---|---|---|---|
| `main.py` | 1,784 | 5,288 | +196% |
| `settings.py` | 267 | 1,095 | +310% |
| `ttyd.py` | 224 | 865 | +286% |
| `state.py` | 192 | 481 | +150% |
| `bells.py` | 177 | 238 | +34% |
| `frontend/app.js` | 6,658 | 8,089 | +21% |

Architecturally incompatible: upstream moved to **per-session ttyd over UNIX domain
sockets** (`431edcec`) and is mid-extraction of tmux logic into a separate `tmuxkit`
workspace package (`00cc8f06`, `d5a6bbe4`) — `poll_bell_flag` is now imported from
`tmuxkit.bell` there. **Evaluate every item here purely on its merits for this fork;
this is no longer a fork that rebases onto upstream.**

**Upstream has not fixed** bell batching (still `for name: await poll_bell_flag(name)`),
settings caching, or render guards. It **has** shipped two conceptually relevant merged
PRs worth studying (the diffs won't port): **#17** federation circuit breaker (dead remote
made polls take ~5 s → ~5 ms; same class as our bell-latency-under-lock problem) and
**#16** session switch 8.8 s → 0.7 s, partly by short-circuiting an unconditional ttyd
kill+respawn when the session is already active.

**Contribution posture:** one upstream PR ever (#6, 2026-06-04), **closed same day,
unmerged, no maintainer comment**; 10 self-merged PRs on this fork since.
**Recommendation: keep this work fork-local.**

**Cautionary precedent — upstream issue #27** ("restore dying silently"): a fix for one
reliability issue silently removed per-step state persistence, turning partial failure
into total data loss. That is precisely the shape of the 2.1 trap above. Reducing write
frequency for efficiency must never remove a durability guarantee.

**External validation:** batched `tmux list-* -a -F` is the idiomatic way to query the
tmux server at scale (per-item `tmux` invocations are a recognized anti-pattern in plugin
development); ETag/304 is boilerplate HTTP practice this codebase already uses for static
assets upstream. The one genuinely bespoke piece is the bell-detection batching design —
upstream's own docstring calls its bell approach "based on spike findings", i.e. original
engineering that needs its own test coverage, not a drop-in recipe.

---

## Sequencing

**Phase 0 — prerequisites (do first)**
0.1 atomic writes ⭐ · 0.2 prune-write LWW fix · 0.3 `conftest.py` + poll-cycle perf test ·
0.4 `uv sync --extra dev` + re-baseline

**Phase 1 — trivial, low-risk wins**
1.1 batched bell poll (tab-delimited, aggregation decided, `poll_bell_flag` retained) ·
2.1 pruning-state dirty flag (snapshot-compare form) · 2.2 list-inequality mutated flag ·
3.1 ttyd shutdown kill (4 conditions) · 3.3 PID-write guard · 4.1 prune `_bell_seen` ·
5.1 document direct invocation

> Cuts per-cycle tmux spawns from `2N+2` to `N+3`, removes the serialized in-lock
> latency, eliminates 43,200 disk writes/day and two full serializations per cycle, and
> closes the ttyd orphan hole.

**Phase 2 — front-end (steps in order; step 1 is a prerequisite, not optional)**
1.3 step 1 delegate clicks → step 2 hoist side effects → step 3 per-tile signature guard ·
4.2 cap `_pillWidthCache`

**Phase 3 — server-side payload and I/O (measure before and after)**
2.5a shared serialization + drop deep copy · 2.6 fold `/api/state` in ·
1.2(a) semaphore the snapshot gather

**Phase 4 — measure first, then decide**
2.4 I/O off the event loop (only if still warranted after Phase 1) · 3.2 SIGKILL
escalation (+ test rewrite) · 3.4 `FIRST_COMPLETED`

**Phase 5 — investigate before acting**
1.2(b)/(c) visible-set or change-detected snapshots · 3.5 WS backpressure load test ·
5.3 ttyd `-t` verification

**Deferred / dropped:** 2.3 settings cache (deferred — poor cost/benefit, widens an
existing race); 2.5b ETag (dropped — breaks the client, helps only single-device users,
~0 hit rate on active sessions).

---

## Verification plan

### Current baselines (2026-08-08)

| Suite | Result |
|---|---|
| `uv run pytest -q -m "not integration"` | ❌ **no valid baseline** — venv lacks `dev` extra (`bs4` missing → 2 modules won't collect; `pytest-asyncio` missing → all 50 "failures" are `async def functions are not natively supported`). Fix via 0.4. |
| `node muxplex/frontend/tests/test_app.mjs` | ✅ **497 / 497 pass** |
| `node muxplex/frontend/tests/test_terminal.mjs` | ⚠️ **26 pass / 27 fail** — confirms the documented 27; all from one root cause: `container.addEventListener is not a function` at `terminal.js:733` during module require (harness DOM-stub gap, not product code) |
| `muxplex/frontend/tests/test_mobile_keyboard.mjs` | present, not previously listed in `CLAUDE.md` |

### Tests to add (Phase 0)
- `conftest.py` with path-redirect fixtures (**mandatory** — no shared conftest exists
  today, so a new test file writes to the real `~/.config/muxplex`)
- Poll-cycle perf test: `run_tmux` spy asserting bell-path spawn count is O(1) in N;
  second consecutive cycle performs zero pruning writes
- Stale-key prune persistence test — a key missing across ≥2 cycles must retain its
  `first_missed_at` (the regression guard for the 2.1 trap)
- Lifespan-shutdown test asserting `kill_ttyd` is called (none exists)
- Grid guard tests: no DOM write when nothing changed, **and** a changed snapshot on one
  session out of many still rewrites that tile

### Acceptance criteria for every change
- `/api/sessions` p95 latency ≤ baseline at N ∈ {1, 10, 50, 100}
- Poll-cycle wall time ≤ baseline, and its **growth with N** strictly better
- Zero change in bell detection latency (the hook path is primary and untouched)
- Full Python suite green; `test_app.mjs` 497/497; `test_terminal.mjs` no *new* failures
  beyond the 27 known-environmental ones

**Testing hygiene:** never smoke-test against the live `~/.config/muxplex` — a partial
`PATCH /api/settings` wipes views/hidden state (and see 0.1 for how that can happen
without any smoke test at all). Always use a throwaway `MUXPLEX_STATE_DIR`.

---

## Open questions

1. **Bell aggregation (1.1)** — OR-across-windows (recommended; surfaces background-window
   bells) or exact parity via `-f '#{window_active}'`? OR is a documented behavior change.
2. **Snapshot scope (1.2b)** — is the client willing to tell the server its visible set?
   The difference between "N=100 works" and "N=100 works cheaply", at the cost of a new
   client→server contract.
3. **Grouped-mode reconciliation (1.3)** — accept flat-mode-only per-tile guarding as the
   interim, or do keyed group headers up front?
4. **2.3** — confirm it stays deferred, or is there a use case that justifies it?
5. **Crash history** — a prior claude-mem note attributes past "crashes" to a
   duplicate-`serve` SIGTERM; observation #54299 attributes them to OOM. Live
   `dmesg`/`journalctl` show no OOM evidence and the measured 58 MB flat footprint makes
   OOM-*of-muxplex* implausible — though OOM pressure from ~8.8 GB of Claude processes
   could have taken it as collateral. Does not block any work here.

---

## Revision log

**rev 2 (2026-08-08)** — adversarial verification pass across six angles (batched-bell
feasibility incl. live tmux comparison, frontend guard feasibility, ttyd shutdown risk vs
upstream issue #7, settings/ETag correctness, test blast radius, upstream divergence).
Corrections to rev 1:

| Item | rev 1 said | rev 2 verified |
|---|---|---|
| 2.1 pruning guard | "Risk: none. Trivial." | **WRONG** — guarding on `settings_changed` permanently disables stale-key pruning (3 of 4 pruning-state mutations leave it False). Needs a pruning-state dirty flag. |
| 2.5 ETag | "a 304 is a no-op for [the client]" | **WRONG** — the client throws on any non-2xx; every 304 would paint a permanent connection error. Item split; ETag half dropped. |
| 1.3 render guard | "`_lastHtml` compare, risk low, effort small" | **No-op in practice** (AND across N tiles) and a **listener leak** as literally described. Redesigned as 3 sequenced steps; risk medium, effort medium. |
| 1.1 bell batching | "No behavior change, no UX risk" | **False for multi-window sessions** — today's query is current-window-only. Also needs tab delimiter and a missing-session default. |
| 3.1 ttyd kill | flagged as needing careful risk review | **Verified SAFE** — single-PID SIGTERM, unrelated to issue #7's cgroup mechanism; `start_new_session` was a GC-lifetime fix, not session protection. |
| 2.3 settings cache | "medium risk" | **Worse** — widens an existing TOCTOU race by ~5 orders of magnitude; deepcopy eats most of the win. Deferred. |
| — | not mentioned | **Two pre-existing bugs found**: non-atomic settings/pruning writes (silent total view loss) and a prune write that can never win LWW. Both promoted to Phase 0. |

**rev 1 (2026-08-08)** — initial proposal from a six-angle memory/efficiency
investigation.
