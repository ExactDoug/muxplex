# muxplex — Plan & Status

**This is where project status lives.** `CLAUDE.md` is the durable brief (architecture,
contracts, gotchas) and carries no status. GitHub Issues own item-level state; they are
referenced here by number and never restated. Per-feature design and rationale live in
`docs/plans/`, linked below.

Last updated: 2026-10-06 (checkpoint after PR #25).

## Current state

- **`main` @ `4d39546`**, version **0.9.6.dev10** (`pyproject.toml`). This is a dev build;
  the last tagged release is **0.9.5**. The dev builds since then (dev2–dev10) have no
  release yet.
- **Test suites, last verified green 2026-10-06 at dev10:** Python 1446,
  `test_app.mjs` 543, `test_terminal.mjs` 66, `test_mobile_keyboard.mjs` 16. Commands are in
  `CLAUDE.md` § Tests.
- **Start new work from `main` in a worktree.** `.worktrees/mobile-keybar-fixes` (branch
  `fix/mobile-keybar-paste-and-geometry`, merged in PR #21) is stale and can be removed.

## Workstreams

| Workstream | Status | Shipped in | Design / record |
|---|---|---|---|
| Header strip: every project folder as a pill, MRU order, Other Sessions folder menu (#24) | **DONE** 2026-10-06 | PR #25, v0.9.6.dev10 | `docs/plans/2026-10-06-header-project-folders-plan.md` |
| Mobile keybar: terminal sizing for the iOS keyboard, Paste key | **DONE** 2026-09-16 | PR #21, v0.9.6.dev8–dev9 | `docs/plans/2026-07-27-mobile-terminal-keybar.md` |
| Mobile keybar follow-ups | **OPEN**: #14, #16, #17, #18, #19, #20, #22, #23 | — | issues |
| Terminal "Reconnecting…" loop | **DONE** 2026-09-11 | PR #13, v0.9.6.dev6–dev7 | `docs/plans/2026-08-09-terminal-reconnect-loop-investigation.md` |
| Resource efficiency (poll cycle O(1) in N) | **DONE** 2026-08-09 | PRs #11, #12 | `docs/plans/2026-08-08-resource-efficiency-plan.md` |
| Mobile terminal keybar | **DONE** 2026-07-28 | PR #9, v0.9.6.dev5 | `docs/plans/2026-07-27-mobile-terminal-keybar.md` |
| Mouse Lab harness (two mouse bugs) | **IN PROGRESS** since 2026-06-24, waiting on the user's over-time lever testing; no issue | v0.9.6.dev2–dev4 (harness only) | `docs/plans/2026-06-24-mouse-lab-harness.md` |
| v0.9 session UX (auto-open, rename, ⧉ glyph, convergence, worktree grouping, mouse fixes) | **DONE** | PR #8, v0.9.0–v0.9.5 | `docs/plans/2026-06-11-v0.9-session-ux-requirements.md`, `CHANGELOG.md` |

Earlier work (v0.6–v0.8: views, header pills, search, bulk select, cwd auto-grouping) is
complete; see `CHANGELOG.md` and the dated docs in `docs/plans/`.

## Open: Mouse Lab harness

The harness is live in `terminal.js` (`window.MouseLab`). Its defaults reproduce the shipped
v0.9.5 behaviour, so the frontend contracts hold. It is waiting on two answers from the user's
testing:

1. **Stale selection under tmux `mouse on`.** Hypothesis A is that the highlight is tmux
   copy-mode (server-side), so `_term.clearSelection()` is aimed at the wrong layer; levers 4
   and 5 test it. Hypothesis B is a tracking-mode desync. The decisive read: if
   `_term.hasSelection()` is false while a highlight is visible, it is A.
2. **Right-click double-paste with fullscreen Claude Code** (its mouse capture plus tmux
   `mouse on`). Lever 6 `rightClickPassThru` tests the fix. If ON turns double into single,
   the fix is confirmed. If ON turns double into zero, muxplex was double-sending, which needs
   a different fix.

When a winner is picked, bake it in and remove the harness, following that doc's § "Cleanup
when a winner is picked".

## Candidate follow-ups (no issue filed yet)

- **The production `kill_ttyd` port fallback may be able to SIGTERM the server itself.**
  It runs `lsof -ti :7682` with no LISTEN filter, which matches *either* end of a connection,
  and the muxplex server holds a client socket to ttyd while a terminal is open. During the
  test suite this killed the live ttyd *and* the live server (fixed for tests only, in
  `e0f6ea2`). What is **unverified** is whether any production path reaches that fallback
  while a terminal is open.
- **Make the grid's Settings → Sort → "Recent" real.** It is a no-op today. `/api/sessions`
  now carries `lastAttached` / `created`, which is what it needs.
- Full arrow-key navigation for the header menus (an accessibility enhancement).
- **Views retirement.** The user no longer uses Views; the #24 plan treats them as
  keep-working, no new investment.
- **Independent per-browser sessions.** Today one ttyd and one active session are shared by
  all browsers, and v0.9.1 made them converge; per-browser independence is listed as future
  work in `CHANGELOG.md` v0.9.1.
