# muxplex — Plan & Status

**This is where project status lives.** `CLAUDE.md` is the durable brief (architecture,
contracts, gotchas) and carries no status. GitHub Issues own item-level state; they are
referenced here by number and never restated. Per-feature design and rationale live in
`docs/plans/`, linked below.

Last updated: 2026-11-10.

## Current state

- **Version 0.9.6.dev12 on `main`** (PR #40, merged 2026-11-10). **This fork has no git tags and no GitHub releases.**
  `CHANGELOG.md` version headings are dev builds, not tagged releases. The last non-dev
  heading is v0.9.5 (2026-06-19). Upstream `bkrabach/muxplex` tags a separate
  `v0.6x` line.
- **Last verified suites (2026-11-10, branch `fix/split-utf8-ws-frames`, v0.9.6.dev13):**
  pytest non-integration **1,477 passed**; frontend `test_app` **550**, `test_terminal` **83**,
  `test_mobile_keyboard` **16**, all with 0 failures. Commands are in `CLAUDE.md` § Tests.
  **The suites run locally only.** GitHub Actions has never run on this fork, so CI
  gates nothing (#38).
- **Start new work from `main` in a worktree.**

## Workstreams

| Workstream | Status | Shipped in | Design / record |
|---|---|---|---|
| Split UTF-8 characters across WebSocket messages showed `�` and stray border fragments (#41) | **IN REVIEW**, PR #42 | v0.9.6.dev13 (branch `fix/split-utf8-ws-frames`) | `CHANGELOG.md` v0.9.6.dev13; `CLAUDE.md` frontend contract #14 |
| Emoji cell widths: tables with emoji misaligned because xterm.js counted emoji as one column (tmux counts two) | **DONE** 2026-11-10 | PR #40, v0.9.6.dev12 | `CHANGELOG.md` v0.9.6.dev12; `CLAUDE.md` frontend contract #13 |
| OSC 52 UTF-8 clipboard + lease-bound local/federated navigation (#30, #31) | **DONE** 2026-11-03. Follow-up #36: stale pre-upgrade tabs should fail closed | PR #35, v0.9.6.dev11 | `CHANGELOG.md` v0.9.6.dev11; `CLAUDE.md` frontend contracts #8, #9 |
| ttyd port cleanup can SIGTERM the server itself (`lsof -ti :7682` has no LISTEN filter) | Issue #28; fix proposed in PR #29 | — | #28 |
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

- **Decide whether to sync with upstream.** Checked 2026-11-10: this fork is **508 commits
  behind and 146 ahead** of `bkrabach/muxplex` `main`, merge base `57a4c77` (2026-05-18).
  Upstream had already fixed two bugs this fork fixed again independently: OSC 52 UTF-8
  copy (upstream #9, 2026-07-10; ours #30) and Unicode 11 emoji widths (upstream #50,
  2026-08-18; ours PR #40, the same add-on and activation). The split-character decode
  (#41) is **not** fixed upstream; its `terminal.js` still decodes each message on its
  own, so that fix could go upstream. Before re-solving a terminal bug, check upstream's
  history first.
- **Make the grid's Settings → Sort → "Recent" real.** It is a no-op today. `/api/sessions`
  now carries `lastAttached` / `created`, which is what it needs.
- Full arrow-key navigation for the header menus (an accessibility enhancement).
- **Views retirement.** The user no longer uses Views; the #24 plan treats them as
  keep-working, no new investment.
- **Independent per-browser sessions.** Today one ttyd and one active session are shared by
  all browsers, and v0.9.1 made them converge; per-browser independence is listed as future
  work in `CHANGELOG.md` v0.9.1.
