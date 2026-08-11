# "Reconnecting…" loop when a session's process exits — investigation brief

**Status:** ✅ **RESOLVED (2026-08-10, v0.9.6.dev7).** The first attempt
(v0.9.6.dev6) **did not actually fix it** — see §"Round 2" at the end, which is the
authoritative account. The original hypothesis was directionally right but named the
wrong driver: the loop is sustained by **ttyd itself**, which is a server that re-forks
`tmux attach` per client, not a wrapper that dies with its child.
**Reported:** 2026-08-09 by the user.
**Pre-existing:** confirmed NOT introduced by the resource-efficiency work (PRs #11/#12).
**Branch:** `investigate/terminal-reconnect-loop`

---

## Symptom

Kill / exit / end the process that a tmux session was invoked to run (e.g. quitting the
shell or the Claude Code process in the pane) while that session's terminal is open in
muxplex. The terminal shows **"Reconnecting…" and never stops** — apparently forever.

The user reports this is long-standing, and it reproduces on `main`.

---

## Hypothesis (NOT yet verified — verify before fixing)

The reconnect logic has no notion of "the session no longer exists", so it retries a
connection that can never succeed, forever.

Proposed chain:

1. The pane's process exits → its window closes → with tmux's default `exit-empty on`
   the **tmux session is destroyed**. (`destroy-unattached` is `off` on this machine and
   is not the relevant option.)
2. ttyd was spawned as `ttyd -W -m 3 -p 7682 tmux attach -t <session>`
   (`muxplex/ttyd.py:255`). Its `tmux attach` client dies with the session, so **ttyd
   exits**.
3. The browser WebSocket closes. In the `close` handler
   (`muxplex/frontend/terminal.js:256-265`) the only bail-outs are
   `ws !== _ws` (stale socket) and `!_currentSession` (intentional close).
   **`_currentSession` is still set** — the frontend has no idea the session died — so it
   shows the overlay, increments `_reconnectAttempts`, and schedules a retry.
4. Backoff is `min(1000 * 2^(n-1), 15000)` + jitter (`terminal.js:262`). It **caps at 15 s
   and never gives up** — there is no maximum attempt count.
5. From attempt 2 onward, `connect()` (`terminal.js:281-299`) POSTs
   `/api/sessions/<name>/connect` to respawn ttyd. The server spawns
   `tmux attach -t <dead-session>`, which **fails immediately**, so ttyd exits again.
6. → back to step 3. Loop, every ~15 s, indefinitely.

**Nothing in this path ever asks whether the session still exists.** The poll loop
(`pollSessions` in `app.js`) *does* know — the session disappears from `/api/sessions` —
but the terminal's reconnect logic is completely independent of it.

### Decisive test for the hypothesis

Open a session, `exit` its shell, then watch:
- `tmux has-session -t <name>` → should fail (session gone), and
- the browser devtools Network tab → a repeating `POST /connect` + failed WS upgrade
  every ~15 s, and
- `pgrep -af ttyd` → ttyd repeatedly appearing and dying.

If all three hold, the chain above is confirmed. If ttyd stays *alive* while the WS still
fails, the cause is elsewhere and this brief is wrong — say so and re-investigate.

---

## Things to check that the hypothesis does not cover

- **What does `POST /api/sessions/{name}/connect` return for a dead session?** If it
  already returns a 4xx, the frontend is ignoring it (`.catch(() => null)` at
  `terminal.js:294` swallows everything, and the `.then()` proceeds regardless). That
  swallow may be the single smallest fix point.
- **Remote/federated sessions** take a different connect path
  (`/api/federation/{id}/connect/{name}`, `terminal.js:285`). Confirm whether the same
  loop occurs for a remote session whose process exits, and whether the peer reports the
  death differently.
- **Does the grid/sidebar update correctly** while the terminal is stuck? If the session
  vanishes from the grid but the fullscreen terminal keeps retrying, that confirms the
  two code paths disagree — and points at the fix.
- **`reconcileViewingSession`** (v0.9.1 cross-browser convergence) may interact: with the
  session gone, what does it converge *to*? Check it does not fight the fix.

---

## Fix directions (do not implement before verifying the cause)

Roughly in order of increasing invasiveness:

1. **Honour the connect response.** Stop swallowing errors at `terminal.js:294`; on a
   4xx/`session not found`, stop retrying and surface "session ended".
2. **Bound the retries.** Give the backoff a maximum attempt count, after which the
   terminal shows a terminal-state message with a "Back to sessions" action, instead of
   an infinite overlay. This is a good safety net regardless of root cause.
3. **Teach the reconnect path about session liveness.** The poll already knows. Have the
   terminal close itself (or show "session ended") when its session disappears from the
   poll payload.

Whichever is chosen, the UX requirement is that a session ending is presented as a
**normal, explained outcome** — not an indefinite spinner.

### Constraint carried over from the efficiency work

Do not "fix" this by having the server keep a dead session's ttyd alive, and do not widen
any ttyd kill to a process group. `kill_ttyd` is deliberately single-PID
(SIGTERM → SIGKILL, `muxplex/ttyd.py`); a group or cgroup-wide kill destroys live tmux
sessions and everything running inside them (upstream issue #7).

---

## Relevant files

| File | Why |
|---|---|
| `muxplex/frontend/terminal.js:198-303` | `_connectWebSocket`, the `close` handler, backoff, and the `/connect` respawn path — the whole loop lives here |
| `muxplex/frontend/terminal.js:442-447, 638-659` | where `_reconnectAttempts` / `_reconnectTimer` are reset (session open / intentional close) |
| `muxplex/main.py` `connect_session` | what `/api/sessions/{name}/connect` does for a session that no longer exists |
| `muxplex/ttyd.py:251-290` | `spawn_ttyd` — `tmux attach -t <name>`; behaviour when the target is gone |
| `muxplex/frontend/app.js` `pollSessions` | already learns the session vanished; currently does not inform the terminal |

## Known-adjacent, already-fixed (do not re-report)

`terminal.js:297`'s 800 ms `setTimeout(_connectWebSocket, 800)` was previously flagged
for not tracking its timer id, letting a late callback reattach to a stale session. That
was reviewed during the efficiency work; check whether it is still untracked, because it
sits directly in this code path and could confuse the investigation.

---

## Verification and outcome (2026-08-09, v0.9.6.dev6)

### The hypothesis held

Each link of the proposed chain was confirmed:

| Link | Evidence |
|---|---|
| Pane process exits → tmux destroys the session | `tmux show-options -g exit-empty` → `exit-empty on` (and `destroy-unattached off`, as the brief said — not the relevant option) |
| ttyd's `tmux attach` then fails, so ttyd exits | `tmux attach -t <missing>` → `can't find session`, **exit 1**. `spawn_ttyd` (`ttyd.py:250`) execs exactly that. |
| The close handler has no liveness notion | `terminal.js` close handler bails only on `ws !== _ws` and `!_currentSession`; neither is true here |
| Backoff never gives up | `min(1000·2^(n-1), 15000)` with **no maximum attempt count** |
| `/connect` respawns a doomed ttyd every cycle | `connect()` takes the `/connect` branch from attempt 2 onward, then opens a WS regardless of the answer |

The decisive live test (kill a pane's process, watch `tmux has-session` / Network tab /
`pgrep -af ttyd`) was **not** run against the user's machine, because it would have
required `POST /connect` on the live server — which kills the shared ttyd and repoints
every connected browser mid-session. The chain was instead confirmed link-by-link from
tmux's own configuration, `tmux attach`'s exit status, and the code, which pins the same
conclusion without disturbing 45 live sessions.

### What the brief got wrong

It named `terminal.js`'s `.catch(() => null)` as the thing swallowing the error. That
catch was misordered (it preceded `.then()`, so the success path ran unconditionally) —
but it was **not** what hid the 404. `fetch()` only rejects on *network* failure; an HTTP
404 is a perfectly **resolved** promise. The actual defect was that `res.ok` / `res.status`
were never inspected at all. Both were fixed, but the distinction matters: removing the
catch alone would have changed nothing.

### The fix (fix directions 1 + 2, both)

1. **Honour the connect response.** `connect_session` (`main.py:963`) already raises
   404 `Session 'x' not found` once the ~2 s poll cycle drops the dead name from the
   cache — so the definitive signal existed all along and was simply discarded.
   Federated sessions surface it as a **502** whose detail reads `Remote returned 404`
   (`federation_connect` translates every non-2xx from the peer into 502), so a bare 502
   is deliberately *not* enough — the proxied detail is parsed. 503 (peer unreachable),
   500 and network errors stay retryable.
2. **Bounded retries.** `MAX_RECONNECT_ATTEMPTS = 8` (~75 s under the existing backoff)
   as a root-cause-independent backstop.
3. **A real end state.** `#session-ended-overlay` replaces the endless spinner with
   "Session ended." / "Lost connection to this session." plus a **Back to sessions**
   button that delegates to the existing `#back-btn`, keeping one implementation of
   "return to the grid" in `app.js`. `endTerminalSession()` nulls `_currentSession`, the
   latch every reconnect path already checks, which halts the close handler *and* any
   in-flight `/connect` continuation.

No backend change was needed — the backend was already reporting the truth.

### Adjacent item from the brief, now closed

The untracked `setTimeout(_connectWebSocket, 800)` (brief's "known-adjacent") **was**
still untracked. It is now assigned to `_reconnectTimer`, so `openTerminal` /
`closeTerminal` cancel it instead of letting a late callback reattach to a stale session.

### `reconcileViewingSession` does not fight the fix

With the session gone, the server's `active_session` still names it, so the
`serverName === _viewingSession` early return holds and no reconcile fires. After **Back
to sessions**, `closeSession()` PATCHes `active_session: null` and the `!serverName`
early return holds. Checked both directions; no interaction.

### Tests

Five cases appended to `muxplex/frontend/tests/test_terminal.mjs` — local 404, the retry
cap, a federated peer 404-as-502, a federated 503 that must **keep** retrying (negative
control), and overlay clearing on the next `openTerminal`. **Four of the five fail
before the fix**, verified by reverting `terminal.js` and re-running.

Their harness gives the `#terminal-container` stub an `addEventListener`, which is why
they run at all: that missing method is the single root cause of this file's 27
pre-existing failures. Those 27 are **unchanged** — verified by diffing failing test
*names* against the pre-change baseline. Fixing the older harnesses was left out of
scope deliberately; it is a worthwhile separate cleanup.

---

# Round 2 (2026-08-10, v0.9.6.dev7) — what dev6 missed

The dev6 fix shipped and the loop **still happened**, reported from an iPhone. It did
eventually terminate, but only after ~5 attempts / ~15 s, with `can't find session: test`
printed on every attempt, and it left the doomed ttyd running. A six-agent read-only
investigation found four defects, three of them server-side and none addressed by dev6.

## The actual root cause: ttyd is a server, not a wrapper

This is the fact everything else follows from, and dev6 never established it.

`spawn_ttyd` runs `ttyd -W -m 3 -p 7682 tmux attach -t <name>` — **without `--once`**.
Empirically verified on an isolated port against a nonexistent session:

- 3 sequential WebSocket connections → **3 distinct child PIDs**, with the ttyd process
  itself alive and `LISTEN`ing throughout. ttyd binds the port once and **forks the
  command per client**; a child exiting closes only that one WebSocket.
- Each connection delivers a real **`0x30` OUTPUT frame** containing
  `can't find session: <name>\r\n`, then a bare **1006 close with no reason**. There is no
  application-level "the child died" signal at all.
- Child spawn → exit is **~11 ms** — an order of magnitude faster than the frontend's
  800 ms settle delay, so no race hypothesis is needed anywhere.

So every reconnect got a *successful* WebSocket, *real* terminal data, and *then* a close.
And `_ttyd_is_listening()` is a bare TCP probe, so that orphan read as perfectly healthy
forever. Nothing on the server ever reaped it: the poll cycle cleared `active_session`
(main.py step 7) but left the **process** running.

## The four defects

1. **Nothing reaped the orphan ttyd.** Clearing the name while leaving the process alive
   is what made the loop self-sustaining, with *zero* frontend involvement. Fixed: the
   poll cycle now kills it in the same cycle, outside `state_lock`, exactly once.
2. **`terminal_ws_proxy` respawned blind.** It auto-spawned from `active_session` with no
   existence check — the check `connect_session` has always had. Fixed.
3. **The reconnect counter was reset by the failure itself.** `_reconnectAttempts` was
   reset on any inbound frame, on the theory that data proves health. Here the data *is*
   the error message. ttyd also sends title/preferences frames on every connect, and the
   reset ran *before* the frame-type dispatch, so even a silent connection reset it.
   Consequence: the counter oscillated ~0↔1, escalation to `/connect` took ~5 cycles
   instead of 2, and the retry cap was nearly unreachable. Fixed by judging health on
   **connection survival** (`HEALTHY_CONNECTION_MS = 5000`) instead.
4. **A late `/connect` response could resurrect an ended terminal.** `endTerminalSession`
   nulls `_currentSession`, but the fetch continuation never read that latch and its
   settle timer reopened a socket anyway. Contract #8 asserted this was safe. It was not.

Plus, separately: `GET /` sent **no `Cache-Control` and no validator**, making
`index.html` heuristically cacheable — which would make any future version bump a silent
no-op, since the `?v=` URLs live inside that HTML. Not the cause here (the device was
confirmed running dev6 via the "Session ended." overlay, which exists only in dev6+), but
the cache-buster was resting on nothing.

## Why the dev6 tests passed while the fix did nothing

**The harness had no way to deliver an inbound message.** There was no `fireMessage`
helper at all, so every test modelled a socket that opened and closed **silently** — which
is precisely the one variant in which the dev6 fix worked, and is not the production
scenario. The tests validated the complement of the bug.

"Verified to fail before the fix" was worthless as evidence: it proved the tests were
sensitive to the code that had just been written, not that they modelled the reported
failure. Four tests failing pre-fix is entirely consistent with a fix that is correct for
a scenario that never occurs.

A second, subtler harness flaw made some assertions **unfalsifiable**: promise
continuations run outside the synchronous `setTimeout`-mock window, so a timer scheduled
by the `/connect` continuation escaped onto the real `setTimeout` and was invisible to the
test. `flush()` now keeps the mock installed across the await — which is what made defect 4
detectable at all.

**Generalised lesson:** the harness simulated the transport's *control plane* (open, close,
timers) and omitted its *data plane*, then drew a conclusion about a bug whose entire
mechanism lives in the data plane. Tests now assert on **bounded total work** ("after N
data-bearing cycles the terminal has ended and opened ≤4 sockets") rather than on any
particular mechanism firing — an assertion that is oblivious to *which* stop fires and
would have failed loudly.

## Corrections to Round 1's account

- **"The backend was already reporting the truth."** Only partly. `connect_session`'s 404
  was correct, but `terminal_ws_proxy` was respawning doomed processes and nothing reaped
  ttyd — the backend was an active participant in the loop.
- **"`MAX_RECONNECT_ATTEMPTS` is a cause-independent backstop."** False. It depended on the
  counter climbing, which the failure suppressed.
- **"Nulling `_currentSession` stops any in-flight `/connect` continuation."** False as
  written; no code on that path read the latch.
- **A claim made mid-investigation that `[exited]` cannot come from muxplex** was wrong —
  a screenshot showed it plainly. It is relayed ttyd/PTY output. The sequence is
  `[exited]` **once** (the real session's process ending), then one
  `can't find session: <name>` line appended per retry — not an alternation.
- **Caching was wrongly nominated as the most likely explanation** for the iPhone report.
  The device was running dev6; the "Session ended." overlay proved it.

## Diagnostic notes worth keeping

- `~/.local/state/muxplex/serve.log` distinguishes eras by `Started server process [pid]`.
  Counting `terminal/ws` lines per era is the fastest way to see whether a loop is live.
- A repeating `POST /api/sessions/<name>/connect` in the log is the frontend escalation
  path; its **absence** during a loop means the loop is being driven server-side.
- `connect_session`'s guard is `if known and name not in known` — when the session cache is
  **empty** (startup before the first poll, or a poll failure), the 404 is skipped and every
  name is accepted. Left as-is deliberately (404-ing everything during startup would be
  worse), but it is a real hole in that signal and worth remembering.
