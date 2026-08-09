# "Reconnecting…" loop when a session's process exits — investigation brief

**Status:** OPEN — not yet investigated. This is a briefing for the next work session.
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
