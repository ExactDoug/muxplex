# muxplex resource-efficiency plan

**Branch:** `perf/resource-efficiency` · **Worktree:** `.worktrees/perf-resource-efficiency`
**Date:** 2026-08-08 · **Base:** `main` @ `84ac710` (v0.9.6.dev5)
**Status:** proposal — nothing implemented yet

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
3. **Scale-invariance is the point.** The prioritization below is driven by *how cost
   grows with N sessions*, not by absolute cost today. An O(N)-per-poll cost that is
   invisible at N=3 is the thing that makes N=100 unusable; an O(1) cost that is
   slightly wasteful stays slightly wasteful forever and is low priority.

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

**Conclusion that frames this whole document:** muxplex's *memory* is already fine and
is not the problem worth solving. There is no leak of consequence and no growth trend.
The real cost is **per-poll work that scales with N** — process spawns, blocking disk
I/O on the event loop, and full DOM rebuilds. That is what this plan targets. Memory
items are included for completeness but are deliberately ranked low.

---

## Cost model: what one poll cycle costs today

Constants: server `POLL_INTERVAL = 2.0s` (`muxplex/main.py:90`), client `POLL_MS = 2000`
(`app.js:115`), `HEARTBEAT_MS = 5000` (`app.js:116`), `SETTINGS_SYNC_INTERVAL = 15`
cycles ≈ 30 s (`main.py:92`). Let **N** = live sessions, **B** = open browser tabs.

| Work item | Where | Cost per cycle | Scales with |
|---|---|---|---|
| `list-sessions` | `main.py:187` | 1 tmux spawn | O(1) |
| `capture-pane` ×N (concurrent) | `main.py:191`, `sessions.py:308-329` | N tmux spawns | **O(N)** |
| `list-panes -a` | `main.py:196` | 1 tmux spawn | O(1) |
| `poll_bell_flag` ×N (**sequential**) | `main.py:225` → `bells.py:82-89` | N tmux spawns, **serialized, inside `state_lock`** | **O(N), latency too** |
| `load_settings()` ×2 + 2 throwaway `json.dumps` | `main.py:295-306` | 2 file reads, 2 deepcopies, 2 full serializations | O(1), large constant |
| `load_pruning_state()` + **unconditional** `save_pruning_state()` | `main.py:325`, `main.py:341` | 1 read + **1 write, every cycle** | O(1) |
| `load_settings()` (prune) | `main.py:324` | 1 more file read + deepcopy | O(1) |
| `load_state()` / `save_state()` | `main.py:203`, poll + heartbeat + every request | **blocking I/O on the event loop** | O(B) |

**At N=20: `2N+2` = 42 tmux `fork`/`exec` every 2 s ≈ 21 process spawns/second**, ~1.8 M/day.
At N=100 that is 202 spawns per cycle — ~101/second — and the bell half of it is
*serialized inside the lock*.

---

## Priority 1 — O(N) work that must become O(1)

These are the only items that change muxplex's scaling curve. Everything else is
constant-factor cleanup.

### 1.1 Collapse N sequential bell polls into one tmux call ⭐ highest leverage

**Today:** `bells.py:82-89` loops over sessions and does `flag_set = await
poll_bell_flag(name)` — one `tmux display-message` subprocess **per session, awaited one
at a time**, and the whole loop runs inside `state_lock` (held from `main.py:184`).

Two compounding problems:
- N subprocess spawns per cycle.
- **Serialized latency**: N × spawn-RTT (~2-5 ms each) = 40-100 ms at N=20,
  **200-500 ms at N=100** — all of it holding `state_lock`, which blocks `/api/state`,
  `/api/heartbeat`, and every other state reader. This is the single clearest path to
  user-visible sluggishness at high N.

**Fix:** replace the loop with **one** batched query:

```
tmux list-windows -a -F '#{session_name} #{window_bell_flag}'
```

Parse once, build a `{session: flag}` map, feed the existing transition logic unchanged.
**N spawns → 1. Serialized latency → single RTT.**

**Why this is safe:** `bells.py:59-63` documents that this path is only a *fallback* —
the tmux alert-bell hook (`POST /api/sessions/{name}/bell`) is the primary detection
mechanism, and `window_bell_flag` is only set when no tmux client is watching. The
0→1 / 1→0 transition semantics and `_bell_seen` bookkeeping are untouched; only the
data-acquisition method changes. Behavior is identical, detection is not delayed.

**Also:** move this work *outside* `state_lock` — gather the flags first, then take the
lock only to mutate state. Same for the snapshot/path steps where possible.

**Risk:** low. **Effort:** small. **Tests:** existing bell tests should pass unchanged;
add one asserting a single tmux invocation regardless of N.

### 1.2 Bound and batch the snapshot path

**Today:** `snapshot_all` (`sessions.py:308-329`) spawns `capture-pane -e -p -S -30` per
session concurrently via `gather`. Concurrency saves latency but not spawn count, and an
unbounded `gather` at N=100 means 100 simultaneous forks — a thundering herd every 2 s.

**Fix, in order of preference:**
- **(a) Semaphore the gather** (e.g. 16-32 concurrent) so N=100 doesn't fork 100 processes
  at once. Pure win, no behavior change, trivial.
- **(b) Only snapshot what is actually rendered.** Snapshots exist to fill grid tiles.
  Sessions hidden by the active view, collapsed, or off-screen do not need a fresh
  30-line ANSI capture every 2 s. This requires the client to communicate its visible
  set (or the server to derive it from `hidden_sessions` + active view), so it is a
  larger change — but it is the one that makes N=100 genuinely cheap, because it turns
  the dominant O(N) term into O(visible), which is bounded by screen real estate.
- **(c) Snapshot-on-change.** `capture-pane` output for an idle pane is identical cycle
  to cycle. A cheap change-detector (`#{window_activity}` or pane content hash from a
  single batched `list-panes -a -F` call) lets us skip captures for unchanged panes.
  Combines well with (a).

**Note:** (b) and (c) must preserve the *appearance* of live tiles. A tile that stops
updating because it scrolled off-screen must refresh immediately when it comes back into
view — that is the acceptance criterion.

**Risk:** (a) none; (b)/(c) moderate — needs care that no tile ever shows stale content
while visible. **Effort:** (a) trivial, (b)/(c) medium.

### 1.3 Front-end: diff-guard the grid and sidebar renders

**Today:** `pollSessions` (`app.js:392-421`) unconditionally calls `renderGrid`,
`renderSidebar`, and friends every 2 s. Guard status:

| Surface | Line | Diff guard |
|---|---|---|
| `renderViewPills` | `app.js:1319-1323` (`pills._lastHtml`) | ✅ yes |
| `renderExpandedHeaderPills` | `app.js:3958-4007` (`nav._lastSig`) | ✅ yes |
| **`renderGrid`** | `app.js:2068` `grid.innerHTML = …` | ❌ **no** |
| **`renderSidebar`** | `app.js:1056` `list.innerHTML = html` | ❌ **no** |
| Filter bar | `app.js:2071` `filterBar.innerHTML = ''` | ❌ no |
| Search results | `app.js:4297` | ❌ no |

Every 2 s, at N sessions, the client runs `ansiToHtml()` per tile (`app.js:2154/2166`, a
char-by-char string-append parser), tears down and recreates N tile DOM subtrees, walks
`document.querySelectorAll('.session-tile')`, and re-binds handlers on all of them.

**Fix:** apply the same `_lastHtml` string-compare short-circuit that `renderViewPills`
already uses. This is a proven pattern in this codebase, not a new invention.

**This is a UX *improvement*, not just an efficiency one** — the current teardown
destroys `:hover`, `:active`, and in-tile text selection every 2 s. That is exactly the
class of problem the view-pill guard was originally added to fix.

**Follow-on:** delegate tile clicks to the static `#session-grid` container instead of
binding per-tile handlers (respects contract #3's attach-once philosophy and removes the
per-poll re-bind entirely).

**Risk:** low — the guard is a pure short-circuit; if the HTML differs, behavior is
byte-identical to today. **Effort:** small. **Tests:** `test_app.mjs` should gain a case
asserting no DOM write when the session list is unchanged.

---

## Priority 2 — constant-factor waste in the poll cycle

Doesn't change the scaling curve, but runs 43,200 times a day forever.

### 2.1 Stop rewriting `pruning.json` every 2 seconds

`main.py:341` calls `save_pruning_state(_prune_state)` **unconditionally**, while the
adjacent `save_settings` at `main.py:345` is correctly guarded by `_prune_changed`.
Result: a full JSON write to `~/.config/muxplex/pruning.json` every cycle —
**43,200 writes/day** — almost always writing identical bytes.

**Fix:** guard the save on an actual change to the pruning state (compare, or have
`prune_stale_keys` return a dirty flag for the pruning state as it already does for
settings). **Risk:** none. **Effort:** trivial. This is the single cheapest win in the
document.

### 2.2 Kill the double-`json.dumps` change detector

`main.py:299-301` serializes the entire settings dict **twice per cycle** purely to
compare before/after normalization, then discards both strings:

```python
_norm_before = json.dumps(_norm_settings, sort_keys=True)
normalize_session_keys(_norm_settings, _sessions_for_normalize)
_norm_after = json.dumps(_norm_settings, sort_keys=True)
if _norm_before != _norm_after:
```

**Fix:** have `normalize_session_keys` return a boolean indicating whether it mutated
anything — it already knows. Two full serializations per cycle → zero. **Risk:** none
(pure refactor with identical semantics). **Effort:** trivial.

### 2.3 Hold an in-memory authoritative settings copy

`load_settings()` is called from **11 sites** (`main.py:281, 295, 324, 669, 806, 884,
1004, 1056, 1082, 1234, 1454`), including the federation hot path — **three times per
poll cycle alone**. Each call does `copy.deepcopy(DEFAULT_SETTINGS)` (`settings.py:109`)
plus a full file read and `json.loads`. `save_settings` (`settings.py:134-140`) does
another deepcopy plus a full re-serialize — so every small mutation (each view-membership
checkbox toggle at `app.js:2634`/`app.js:3053`) rewrites the entire settings file.

**Fix:** a module-level cached settings object invalidated on write, with the on-disk file
remaining the durable record. Must preserve the existing last-write-wins federation
semantics and the external-edit story (settings.json is user-editable — invalidate on
mtime change so hand-edits are still picked up).

**Risk:** medium — this is the item most able to introduce a subtle correctness bug
(stale settings, lost federation write). Needs careful test coverage; consider doing it
*after* the trivial wins land. **Effort:** medium.

### 2.4 Move state/settings disk I/O off the event loop

`state.py:154-175`, `settings.py:111/140`, `pruning.py:38/56` all use blocking
`open`/`write_text`/`os.replace`. `read_state`/`write_state` (`state.py:183-192`) wrap
them in an asyncio lock but **not** a thread — so every poll cycle, every heartbeat
(every 5 s per tab), and every `/api/sessions` and `/api/state` request performs blocking
file I/O **on the event-loop thread**. With 4 tabs open that is roughly **5 full
`state.json` read/write round-trips per second on the loop**.

**Fix:** `asyncio.to_thread` for the read/write primitives (or accept it as fine once
2.1-2.3 have cut the call volume — measure first). On a slow or contended disk this is
the difference between smooth and stuttering; on a fast SSD it is currently invisible.

**Risk:** low-medium (ordering guarantees must be preserved via the existing lock).
**Effort:** small. **Sequencing note:** do this *after* 2.1-2.3, then re-measure — the
volume reduction may make it unnecessary.

### 2.5 `/api/sessions`: stop re-serializing identical payloads

`main.py:617-647` per request: `get_session_list()` copies the list (`sessions.py:37`),
`get_snapshots()` **deep-copies the whole snapshot dict** (`sessions.py:42` — N × 30
lines of ANSI text), builds a fresh dict per session, then FastAPI JSON-serializes all of
it. No ETag, no `If-None-Match`, no diffing, no `Cache-Control`. At N=20 with ~2 KB/pane
that is ~40 KB × B clients every 2 s ≈ **20·B KB/s of re-serialized, mostly-unchanged
bytes**. `/api/federation/sessions` (`main.py:1462-1482`) does it again, plus a full
rebuild per peer via `{**s, …}` (`main.py:1519-1527`).

**Fix options (compose well):**
- Compute a cheap payload hash per cycle; serve `304 Not Modified` on a matching
  `If-None-Match`. Client already polls on a timer — a 304 is a no-op for it.
- Cache the serialized body per poll cycle so B clients share **one** serialization
  instead of B.
- Avoid the snapshot deep-copy where the data is read-only downstream.

**Risk:** low, provided the ETag is invalidated by *everything* that can change the
payload (snapshots, paths, bell state, order). Get that wrong and tiles go stale — so the
etag input must be derived mechanically from the payload, not hand-maintained.
**Effort:** small-medium.

### 2.6 Fold `/api/state` into the poll response

`app.js:421` → `reconcileViewingSession` (`app.js:369-373`) issues a **second** request
every 2 s per fullscreen tab, purely to read one field — doubling request rate and adding
a blocking `load_state()` server-side. **Fix:** ride that field along on the
`/api/sessions` response. **Risk:** low. **Effort:** small.

---

## Priority 3 — process and lifecycle hygiene

Small absolute cost, but these are real bugs that leak OS resources.

### 3.1 Kill ttyd on server shutdown

`ttyd.py:218` spawns with `start_new_session=True` ("so ttyd survives independently"), and
the lifespan shutdown path (`main.py:401-419`) closes the httpx client and cancels the
poll task but **never calls `kill_ttyd()`**. Because ttyd is in its own process group it
does not receive the SIGINT/SIGTERM that stops `muxplex serve` — so **Ctrl-C leaves ttyd
running forever**, holding port 7682, a PTY, and a `tmux attach` client. Recovery is
purely retroactive via `kill_orphan_ttyd()` at `main.py:378` on the *next* startup.

Under systemd this is masked (`service.py:31` `KillMode=mixed` kills the cgroup, which
`start_new_session` cannot escape). It bites in the `uv run muxplex serve` dev path — the
common one.

**Fix:** `await kill_ttyd()` after the `yield` in `lifespan`. **Risk:** none.
**Effort:** trivial.

### 3.2 Escalate `kill_ttyd` to SIGKILL on timeout

`ttyd.py:134-143`: after 2 s of SIGTERM polling, `kill_ttyd` unlinks the PID file and
returns `killed=True` **regardless of whether the process died**. A wedged ttyd then
loses its PID-file handle entirely and is only reachable via the `lsof` port fallback —
which silently no-ops if `lsof` isn't installed (`ttyd.py:71-74`).

**Fix:** escalate to SIGKILL on timeout before unlinking; only report success on
confirmed exit. **Risk:** none. **Effort:** trivial.

### 3.3 Guard the PID-file write

`ttyd.py:213` `TTYD_PID_PATH.write_text(str(proc.pid))` is unguarded. If it fails
(read-only dir, disk full) the ttyd is **live with no PID file and no `_active_process`
assignment** — trackable only via the port fallback. **Fix:** try/except that kills the
just-spawned process on write failure. **Risk:** none. **Effort:** trivial.

### 3.4 `FIRST_COMPLETED` instead of `gather` in both WS proxies

`main.py:1206` and `main.py:1332`:

```python
await asyncio.gather(client_to_ttyd(), ttyd_to_client())
```

`gather` waits for **both**. `ttyd_to_client` ends when ttyd closes, but `client_to_ttyd`
blocks in `await websocket.receive()` (`main.py:1188`) until the *browser* sends a frame
or disconnects. So if ttyd dies while an idle tab is open, the handler task, both
coroutines, the accepted WebSocket, and the upstream connection all stay resident
indefinitely. One stranded connection per idle tab per ttyd restart.

**Fix:** `asyncio.wait(..., return_when=FIRST_COMPLETED)` then cancel the survivor.
**Risk:** low — must ensure the cancel path still runs the existing `finally` close
logic (`main.py:1209-1213`, `1335-1339`). **Effort:** small.

### 3.5 Bound WebSocket write buffers

`websockets.connect` (`main.py:1181`, `main.py:1301`) is called with no `max_queue` /
`write_limit` override, and Starlette's `send_bytes` goes through uvicorn's transport
queue. A slow reader (VPN, mobile) on a firehose-producing pane is a plausible unbounded
server-side buffer path. **Not confirmed** — needs a deliberate slow-client load test
before acting. **Action: verify first, then decide.** Do not "fix" this speculatively.

---

## Priority 4 — memory items (deliberately last)

Included for completeness. Measured evidence says none of these are hurting anything.

### 4.1 Prune `_bell_seen`

`bells.py:27` `_bell_seen: dict[str, bool]` — inserted at `bells.py:97, 102, 174`, and
there is **no `del` or `pop` anywhere** in the module (only `.get`, assignment, and a
`.clear()` in tests). The poll cycle deletes dead sessions from persisted state at
`main.py:218-220` but never mirrors that here; `rename_session` (`main.py:905`) moves the
state entry and leaves `_bell_seen[old_name]` behind.

This is **the only genuine insertion-without-eviction path in the package**. Cost is one
short string + one bool per session name ever seen — measured in kilobytes over months.

**Fix:** mirror the step-6 deletion — prune `_bell_seen` against `name_set` in the same
place. **Risk:** none. **Effort:** trivial. Worth doing purely for correctness/tidiness.

### 4.2 Cap `_pillWidthCache`

`app.js:3649`, populated at `app.js:3912-3928`, keyed on **complete pill HTML strings**
that embed session name, bell glyph, active class, **and count** (`app.js:3926-3932`).
Because counts change constantly, the key space is (views × names × bell states × counts),
not (pills). No eviction, no cap — the one structure in `app.js` that only ever grows.

**Fix:** either clear above ~500 keys, or key on a normalized `label + count-digit-width`
signature instead of raw HTML (the latter is strictly better — the *measurement* only
depends on text length, not on the count's exact value).

### 4.3 Evict `_federation_cache` on peer removal

`main.py:1441`, keyed by `remote_device_id`, only evicted on 401/403 (`main.py:1504`).
Removing a remote from settings strands its cached session list — including full pane
snapshots — for the process lifetime. Bounded by peers-ever-configured. Trivial in
practice; fix while touching federation code, not on its own.

### 4.4 Not-bugs (documented so nobody "fixes" them)

Verified clean; do not churn these:
- **Contract #3 is upheld** — every container-level listener is a module-level
  attach-once IIFE. The `openTerminal()` search-bar listeners are safe because each
  element is `cloneNode`/`replaceChild`ed first (drops all prior handlers).
- **xterm lifecycle is correct** — `createTerminal()` disposes the prior `_term` before
  constructing; `closeTerminal()` disposes term/fit/search and `disconnect()`s the
  ResizeObserver. **No WebGL/canvas addon is vendored** (`vendor/` has only fit, search,
  web-links, image), so the classic undisposed-renderer leak cannot occur here.
- **`resolve_git_repo` is already memoized** (`sessions.py:270-305`, cache at
  `sessions.py:211`, capped at 512) — **0 stat calls per poll** in steady state. Do not
  "optimize" it.
- **Snapshot caches rebind rather than mutate** (`sessions.py:51-53, 66-67`), so dead
  sessions drop out automatically. Snapshots are capped at 30 lines (`sessions.py:153`).
- **One `httpx.AsyncClient`**, created once (`main.py:393`), reused everywhere, properly
  `aclose()`d. No per-request client churn.
- **One background task** (`main.py:379`), cancelled and awaited in lifespan.
- `run_tmux` (`sessions.py:114-123`) always `communicate()`s — no pipe leak.

---

## Priority 5 — invocation and environment

Outside the codebase, but directly in scope for "invocation methods".

### 5.1 Drop the `uv run` wrapper for long-lived serving

`uv run muxplex serve` leaves a **~30 MB supervisor process** resident for the lifetime of
the server — more than half the server's own 58 MB footprint, for zero runtime benefit
once the environment is resolved. Invoking `.venv/bin/muxplex serve` directly (or via the
systemd unit) removes it entirely.

**Action:** document this in the "Running locally" section of `CLAUDE.md`/README, and make
sure the systemd unit (`service.py`) uses the direct path. `uv run` remains the right
call for one-shot commands and tests.

### 5.2 Prefer the systemd unit over foreground `serve` for long sessions

It fixes 3.1 for free (`KillMode=mixed` reaps ttyd via the cgroup) and eliminates the
duplicate-`serve` failure mode that `_kill_stale_port_holder` (`cli.py:317-351`, commit
`000c71c`, "2075+ restarts before manual intervention") exists to paper over.

### 5.3 Consider passing ttyd client options

`ttyd.py:204-217` spawns `ttyd -W -m 3 -p 7682 tmux attach -t <name>` — **no `-t` client
option at all**. ttyd's default xterm.js `scrollback` is 1000 lines *per browser client*.
This is browser-side memory, not server-side, and muxplex sets its own scrollback in
`terminal.js:360` (`mobile ? 500 : 5000`), so this is likely moot — **verify which value
actually wins** before changing anything. `-m 3` already bounds client count.

### 5.4 Out of scope but worth stating: tmux `history-limit`

Global `history-limit` is **50000** — 50k lines retained *per pane*, across 45 panes, in
the tmux server process. muxplex never reads or sets this and should not start; it is a
user/environment setting. Noted because it dwarfs everything in this document, and
because the ~9.7 GB of pane memory is the actual resource story on this machine. Not a
muxplex change.

---

## Suggested sequencing

**Phase 1 — trivial, zero-risk, immediate (one PR)**
1.1 batched bell poll · 2.1 guard `pruning.json` write · 2.2 drop double-`dumps` ·
3.1 kill ttyd on shutdown · 3.2 SIGKILL escalation · 3.3 guard PID write ·
4.1 prune `_bell_seen` · 5.1 document direct invocation

> Cuts per-cycle tmux spawns from `2N+2` to `N+3`, removes the serialized in-lock
> latency entirely, eliminates 43,200 disk writes/day and two full serializations per
> cycle, and closes the ttyd orphan hole. No behavior change, no UX risk.

**Phase 2 — front-end responsiveness (one PR)**
1.3 diff-guard grid/sidebar + delegate tile clicks · 4.2 cap `_pillWidthCache`

> Removes N ANSI parses + N DOM subtree rebuilds per 2 s, and **fixes** the
> hover/selection-loss side effect. Net UX improvement.

**Phase 3 — server-side payload and I/O (one PR, measure before and after)**
2.5 ETag/304 + shared serialization · 2.6 fold `/api/state` in · 1.2(a) semaphore the
snapshot gather

**Phase 4 — the careful one (separate PR, heavy tests)**
2.3 in-memory settings cache · 2.4 I/O off the event loop · 3.4 `FIRST_COMPLETED`

**Phase 5 — investigate before acting**
1.2(b)/(c) visible-set or change-detected snapshots · 3.5 WS backpressure load test ·
5.3 ttyd `-t` scrollback verification

---

## Verification plan

There is currently **no memory or performance testing anywhere** in the repo — a grep for
`tracemalloc|memory_profiler|getrusage|RSS` across all `.py` files returns nothing, in a
~1320-test suite. Before changing anything, we need a way to prove these changes help and
don't regress.

**Add before Phase 1:**
- A **synthetic-load harness**: spin up K throwaway tmux sessions (K ∈ {1, 10, 50, 100})
  against an isolated `MUXPLEX_STATE_DIR`, run the server, and record per-poll-cycle
  wall time, tmux spawn count, disk write count, and RSS.
- A **spawn-count assertion test**: with N sessions, one poll cycle must issue a bounded
  number of tmux invocations. This is the regression guard that keeps the O(N)→O(1) wins
  from silently reverting.
- An **RSS soak check** (long-running, marked `integration`): RSS flat over ≥1 h idle.

**Acceptance criteria for every change here:**
- `/api/sessions` p95 latency ≤ baseline at N ∈ {1, 10, 50, 100}
- Poll-cycle wall time ≤ baseline, and its **growth with N** strictly better
- Zero change in bell detection latency (hook path is primary and untouched)
- Full Python suite green; `test_app.mjs` green; `test_terminal.mjs` no *new* failures
  beyond the 27 known-environmental ones

**Testing hygiene reminder:** never smoke-test against the live `~/.config/muxplex` — a
partial `PATCH /api/settings` wipes views/hidden state. Always use a throwaway
`MUXPLEX_STATE_DIR`.

---

## Open questions

1. **Snapshot scope (1.2b)** — is the client willing to tell the server its visible set?
   This is the difference between "N=100 works" and "N=100 works *cheaply*", but it adds
   a client→server contract that doesn't exist today.
2. **Settings cache (2.3)** — is settings.json hand-editing a supported workflow that
   must keep working live? Answer determines whether mtime-invalidation is required or
   merely nice.
3. **ETag scope (2.5)** — should federation responses be ETagged too, or is per-device
   caching enough?
4. **Crash history** — a prior claude-mem note attributes past muxplex "crashes" to a
   duplicate-`serve` SIGTERM, while observation #54299 attributes them to OOM. Live
   `dmesg`/`journalctl` show no OOM evidence, and the measured 58 MB flat footprint makes
   OOM-*of-muxplex* implausible — but OOM pressure from the 8.8 GB of Claude processes
   could plausibly have taken it out as collateral. Worth reconciling, though it does not
   block any work here.
