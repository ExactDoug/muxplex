# Claude Code + tmux + Mouse: A Technical Reference

*A consolidated, evidence-graded reference on the friction points users encounter with the mouse when running Claude Code inside tmux, with current configuration guidance and version history. Each section carries source-count, conflict, and verification annotations.*

*Annotation key: **Sources** = how many of the contributing knowledge sources attested the item (active pool of seven; one additional source carried zero weight throughout and is excluded from counts unless noted). **Conflict** = whether any source gave contradictory information. **Verification** = whether the item is settled or warrants an independent check before being treated as fact.*

---

## 1. Overview

People running Claude Code inside tmux most often complain that **the mouse "fights" between Claude Code and tmux**: scrolling lands on the wrong thing, text selection misbehaves, and the screen flickers or jumps. The root cause is structural — Claude Code's terminal UI captures mouse events for its own features (scrolling, clickable elements, expanding tool output, selection) while tmux simultaneously wants those events for pane management, copy-mode, and scrollback. Two layers contend for the same input.

> *Sources: 7 of 7 (root-cause framing was universal). Conflict: none. Verification: settled — this is the one claim every source agreed on from the first round.*

The modern picture is shaped by Claude Code's **fullscreen rendering** mode (also called "no flicker mode" in the community), which changes how scrolling and the mouse behave and introduces a small set of environment variables that resolve most of the friction — at the cost of needing to understand which renderer you're in.

---

## 2. Business-Actionable Recommendations

If you run Claude Code in tmux and want predictable mouse behavior, the following are the best-supported mitigations across all sources:

- **Decide which renderer you want.** Fullscreen (alternate-screen) rendering gives you in-app mouse scrolling and flicker-free output but takes scrollback away from tmux. Classic rendering keeps the conversation in tmux's native scrollback. You control this explicitly (Section 5).
- **In fullscreen, enable tmux mouse mode** (`set -g mouse on`) if you want the wheel to scroll Claude Code's transcript. Without it, the wheel goes to tmux instead.
- **If you prefer your terminal's native selection/copy**, keep fullscreen but disable Claude Code's mouse capture with `CLAUDE_CODE_DISABLE_MOUSE=1`.
- **If you want tmux to own scrollback entirely** (copy-mode, `Cmd+f`, search), force the classic renderer with `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`.
- **Do not use iTerm2's `tmux -CC` integration mode** with fullscreen rendering — it's officially documented as incompatible.
- **Keyboard scrolling (`PgUp`/`PgDn`) always works** regardless of renderer or mouse mode; it's the reliable fallback.

> *Sources: drawn from 6 of 7 active sources (every source except the one that delivered only a preamble in one round). Conflict: none on the recommendations themselves; one nuance disputed (whether `set -g mouse on` "fixes" scrolling — resolved in Section 6). Verification: settled; all anchored to official documentation.*

---

## 3. The Biggest Annoyances (Summary)

The recurring pain points, in rough order of how frequently they were raised:

**3.1 Mouse wheel scrolls the input box / input history instead of the conversation output.**
The single most-cited complaint. In tmux, the wheel cycles the prompt/input history (like pressing Up/Down) rather than scrolling the transcript.
> *Sources: 6 of 7. Conflict: none. Verification: settled; anchored to issues #9902 and #38810 (both confirmed real, Section 8).*

**3.2 Scrollback becomes unusable / the scrollbar disappears.**
Inside tmux the TUI scrollbar can vanish and the viewport gets stuck at the bottom; the conversation can't be scrolled at all.
> *Sources: 6 of 7. Conflict: none. Verification: settled; anchored to #38810.*

**3.3 Mouse capture breaks normal text selection — including in *other*, non-Claude panes.**
Starting Claude Code in one pane can make selection erratic across an entire split-window terminal, not just the Claude pane.
> *Sources: 5 of 7 (plus the zero-weight source). Conflict: none on existence; nuance on scope (Section 9). Verification: confirmed for Ghostty specifically; **not** established as universal across all terminals — treat the cross-pane effect as terminal-specific pending broader evidence.*

**3.4 Raw SGR mouse escape sequences leak into the input prompt.**
With tmux mouse mode on, scroll/click can insert literal escape codes (e.g., `\e[<0;45;12M`) as prompt text, which may be submitted if Enter is pressed.
> *Sources: 3 of 7 confirmed the underlying issue directly; anchored to #30644. Conflict: one (zero-weight) source claimed #30644 was instead a high-CPU bug — rejected (Section 8). Verification: settled as an SGR-leak issue.*

**3.5 Flicker / jitter / cursor-jumping during streaming output.**
The TUI re-renders frequently while streaming; without synchronized output the screen flickers and the cursor visibly jumps to the top of the render area and back.
> *Sources: 5 of 7. Conflict: none on the symptom; the specific cause framing (missing DECSET 2026) is single-issue-sourced (Section 8, #37283). Verification: symptom settled; the DECSET-2026 causal framing is one detailed user report, corroborated by direct page read but not by Anthropic.*

**3.6 Jump-to-top of history when Claude adds text.**
On new output the view can snap to the top of history, breaking the user's reading position.
> *Sources: 4 of 7. Conflict: none. Verification: settled; anchored to #826.*

**3.7 Two overlapping scroll/search systems create cognitive friction.**
Users must juggle tmux copy-mode (`Ctrl-b [`) and Claude Code's own navigation, remembering which layer is "in charge."
> *Sources: 4 of 7. Conflict: none. Verification: settled as a UX-friction observation (interpretive rather than a discrete bug).*

**3.8 Loss of familiar copy/paste (middle-click / copy-on-select).**
Once Claude Code captures the mouse, terminal-native copy-on-select and middle-click paste stop working; users fall back to Shift-modified actions or tmux copy-mode.
> *Sources: 4 of 7 (plus zero-weight). Conflict: none. Verification: settled; the Linux PRIMARY-selection variant is anchored to #66957 (Section 8).*

---

## 4. "No Flicker Mode" / Fullscreen Rendering

**What it is.** A smoother, flicker-free rendering mode with mouse support and stable (flat) memory usage in long conversations. Its **official name is "fullscreen rendering"**; "no flicker mode" is community/marketing shorthand for the same thing.
> *Sources: 6 of 7. Conflict: none. Verification: settled; official docs.*

**How it's enabled.** Either with the in-app command `/tui fullscreen`, or by setting `CLAUDE_CODE_NO_FLICKER=1` before launch. The docs state the `tui` setting and the environment variable are equivalent. On versions before v2.1.110 the environment variable was the way to opt in; the `/tui` command and `tui` setting arrived later.
> *Sources: 6 of 7. Conflict: none. Verification: settled.*

**Is it the default?** No. It is described as an **opt-in research preview** requiring **Claude Code v2.1.89 or later**. No source found any statement that it became the universal default.
> *Sources: 6 of 7 (every source that addressed it agreed it is opt-in, not default). Conflict: none. Verification: settled, with one open caveat — "opt-in today" is a present-tense fact about a research preview that could change; re-verify against current docs if precision matters.*

---

## 5. Environment Variables

All three variables below are **real and officially documented**. This was the single biggest factual clash in early rounds (sources initially disagreed on names and even existence) and is now fully resolved against the official environment-variable reference and fullscreen docs.

| Variable | Effect |
|---|---|
| `CLAUDE_CODE_NO_FLICKER=1` | Enables fullscreen / flicker-free rendering. Equivalent to `/tui fullscreen`. |
| `CLAUDE_CODE_DISABLE_MOUSE=1` | Disables Claude Code's mouse capture **while keeping** fullscreen rendering and flat memory. Restores terminal-native selection. Keyboard scrolling still works. |
| `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` | Forces the classic main-screen renderer regardless of the saved `tui` setting; keeps the conversation in the terminal's native scrollback so `Cmd+f` and tmux copy-mode work. **Takes precedence over** `CLAUDE_CODE_NO_FLICKER` and the `tui` setting. |

> *Sources: 6 of 7 confirmed all three against official docs. Conflict: resolved — an early stray claim of a different variable (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` was at one point thought unique to a single source, and one source had earlier offered no alternative names) collapsed once sources read the env-var reference directly. Verification: settled; names and behaviors are primary-confirmed.*

**Combined usage example (fullscreen rendering, but native selection preserved):**
```
CLAUDE_CODE_NO_FLICKER=1 CLAUDE_CODE_DISABLE_MOUSE=1 claude
```
> *Sources: 3 of 7 quoted this exact example from the docs. Conflict: none. Verification: settled.*

**Introduction version for `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN`:** added in **v2.1.132** (dated early May 2026 — one source read May 6 from the changelog directly; a mirror listed May 8; the one-to-two-day spread is immaterial).
> *Sources: 3 of 7 confirmed v2.1.132 (one from the official changelog directly, two via release-notes/mirrors and issue #56881). Conflict: 3 of 7 said "cannot confirm the exact version" — but those were retrieval failures (truncated changelog views), not contradictions; none asserted a different version. Verification: settled on v2.1.132; the exact day (May 6 vs 8) is the only soft spot and doesn't matter.*

---

## 6. Does `set -g mouse on` Fix Claude Code Scrolling?

**The answer is renderer-dependent**, and this resolves an apparent early contradiction.

- **In fullscreen rendering:** `set -g mouse on` **is required** for the mouse wheel to scroll Claude Code's transcript. Without it, wheel events go to tmux instead of Claude Code. Keyboard scrolling (`PgUp`/`PgDn`) works either way. Claude Code prints a one-time startup hint if it detects tmux with mouse mode off.
- **In classic rendering:** the docs do **not** claim `set -g mouse on` changes Claude Code's own scroll behavior; there it mainly governs how tmux routes wheel events and copy-mode.

> *Sources: 6 of 7. Conflict: **yes — one source originally claimed `set -g mouse on` "has no effect"** on Claude Code scrolling. This is now reconciled: that claim described the pre-fullscreen / buggy state (and issue #38810, where a reporter said mouse on/off "doesn't help in either mode," predates the fullscreen-transition guidance). It was a context mismatch, not a true contradiction. Verification: settled; the fullscreen-requires-mouse-mode behavior is primary-confirmed in the official docs.*

---

## 7. iTerm2 `tmux -CC` Incompatibility

Fullscreen rendering is **officially documented as incompatible with iTerm2's tmux integration ("control") mode**, the mode entered with `tmux -CC`. In that mode iTerm2 renders each tmux pane as a native split rather than letting tmux draw the terminal; the alternate screen buffer and mouse tracking do not work correctly there. Specifically: **the mouse wheel does nothing, and double-click can corrupt the terminal state.** Guidance: don't enable fullscreen in `tmux -CC` sessions; regular tmux inside iTerm2 (without `-CC`) works fine.

> *Sources: 5 of 7 confirmed the official documentation text; community corroboration (hboon.com) adds a sixth secondary voice. Conflict: 1 of 7 dissented, calling the incompatibility "not officially documented / practical only" — but that source explicitly admitted it could not fetch the docs page and was inferring. That is a fetch failure, not a genuine counter-finding. Verification: settled; primary-confirmed in the fullscreen docs.*

---

## 8. GitHub Issue Catalog

These are the user-reported issues that document the friction points above. Existence and details were heavily contested across rounds — but the contest split cleanly along *retrieval capability*: sources that could load GitHub directly confirmed each issue with specific detail (titles, dates, statuses, version strings); sources reduced to search snippets returned "cannot confirm." **No source ever produced contradictory content** for these issues — only absence. Where a snippet-limited source did manage to reconstruct an issue, its details matched the direct-load details. They are therefore treated as real, with confidence proportional to how many sources reached them.

**All issues are in the `anthropics/claude-code` repository.**

| Issue | Title (verbatim where directly read) | Opened | Status | What it documents |
|---|---|---|---|---|
| **#9902** | Mouse scroll in tmux scrolls input box instead of output | Oct 19, 2025 | Closed (duplicate) | Wheel scrolls input box, not conversation. |
| **#38810** | [Bug] Claude Code captures mouse events in tmux, making scrollback completely unusable | Mar 25, 2026 | Closed (duplicate) | Scrollbar disappears; wheel cycles input history; reporter says `set -g mouse on/off` didn't help. |
| **#30644** | Mouse escape sequences leak into input prompt in tmux with SGR mouse mode | Mar 4, 2026 | Closed (duplicate) | Raw SGR escape sequences appear as prompt text. **(Not a high-CPU bug.)** |
| **#58364** | [BUG] iTerm2 + tmux: mouse wheel hijacked to input history, scrollback renders broken past viewport (2.1.138 + 2.1.139) | May 12, 2026 | Open | iTerm2+tmux regression; title names 2.1.138/2.1.139; body says 2.1.123 was clean. |
| **#37283** | [BUG] TUI flickers/cursor jumps in tmux during streaming output (missing DECSET 2026 synchronized output) | Mar 22, 2026 | Closed (not planned) | Flicker/cursor-jump during streaming; proposes DECSET 2026 synchronized output. Environment: CC 2.1.81, tmux 3.4, WSL2. |
| **#826** | [BUG] Console scrolling top of history when claude add text to the console | Apr 19, 2025 | Open (duplicate label) | View jumps to top of history on new output (notably in Cursor host). |
| **#9935** | Excessive scroll events causing UI jitter in terminal multiplexers (4,000–6,700 scrolls/second) | Oct 20, 2025 | Open | Streaming generates thousands of scroll events/sec in tmux/smux → jitter/flicker/tearing. |
| **#15780** | [FEATURE] support mouse scrolling in tmux | Dec 30, 2025 | Open (stale) | Feature request: can't scroll up/down in tmux while using Claude. Labels: area:tui, enhancement, platform:linux. |
| **#66957** | Mouse capture in TUI breaks Linux PRIMARY-selection middle-click paste (no opt-out) | Jun 10, 2026 | Open | Mouse capture breaks Linux PRIMARY middle-click paste; reproduces with and without tmux. |
| **#63545** | Claude Code inside tmux: scroll hits input not scrollback, no rich mode, transcript not saved | ~May 28–29, 2026 | Open | Detached/wrapper-launched tmux: scroll hits input, rich mode never engages, transcript not saved. |
| **#62890** | Tmux scrollback broken after May 26 update (v2.1.152) | May 27, 2026 | Open | Separate regression: tmux scrollback stopped working after v2.1.152; other panes scroll normally. |
| **#56881** | [DOCS] Missing `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` env var documentation | ~May 6–7, 2026 | Closed | Docs-gap issue noting v2.1.132 added the env var before docs mentioned it. |
| **#69619** | [DOCS] [Fullscreen] docs omit `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT` troubleshooting (Windows Terminal nested-subagent) | Jun 19, 2026 | Open | Primarily about `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT` (a *different* env var); mentions `DISABLE_ALTERNATE_SCREEN` only as fallback. |

**Per-issue evidence grading:**

- **#38810** — *Sources: 7 of 7. Conflict: none. Verification: settled (universal).*
- **#826** — *Sources: 5 of 7. Conflict: none. Verification: settled.*
- **#63545** — *Sources: 5 of 7. Conflict: none. Verification: settled (new this study; uncontradicted).*
- **#9902, #15780, #58364, #56881, #69619** — *Sources: ~4 of 7 each. Conflict: none (the non-confirmers said "cannot confirm," never "wrong"). Verification: settled by direct page reads; the cannot-confirms were retrieval failures. #15780's open date was the lone micro-dispute (Dec 28/29/30) — resolved to **Dec 30, 2025** from the raw issue page; Dec 29 was the label-add event, Dec 28 a stale metadata artifact.*
- **#30644** — *Sources: 3 of 7 confirmed (SGR-leak). Conflict: 1 (zero-weight) source claimed high-CPU — rejected as that source's documented failure pattern. Verification: settled as SGR-leak.*
- **#37283** — *Sources: 3 of 7 (2 direct reads + raw page). Conflict: 1 source questioned whether "DECSET 2026" was in the title vs. a third-party gloss. Resolved from the raw page: **the parenthetical is part of the actual title.** Verification: settled. Caveat below.*
- **#9935, #66957** — *Sources: 2 of 7 direct, plus independent reconstruction by an otherwise-stingy source for #9935. Conflict: none (only absence). Verification: settled — independent corroboration from a source that refused to confirm most other issues is strong signal.*
- **#62890** — *Sources: 3 of 7 direct/verbatim + cross-references from a 4th. Conflict: 2 of 7 said "cannot confirm." Verification: settled as real (v2.1.152 regression); non-finders were retrieval failures.*

**Caveat on #37283 (DECSET 2026):** the raw page shows a commenter correcting the issue's own premise — at the time, no *released* tmux version shipped the synchronized-output passthrough the reporter assumed; it existed only as an unreleased trunk/PR patch. So the symptom (tmux flicker/cursor-jump) is well-attested, but the proposed DECSET-2026 fix depended on an unreleased tmux feature.
> *Verification: the flicker symptom is corroborated by multiple commenters on the issue; the fix-path premise is flagged as conditional and would warrant checking current tmux release status before relying on it.*

**A note on the zero-weight source:** across rounds this source variously claimed `#30644` was a high-CPU bug and, in a later round, that the entire `anthropics/claude-code` issue set was fabricated and the repo had no public issues — while simultaneously "confirming" an env var from that same repository's changelog. These self-refuting claims are why it carried zero weight; none of its unique claims were used to establish or contest any fact in this reference.

---

## 9. Cross-Pane Selection Degradation (Ghostty)

Running Claude Code in **one** pane of a split-window terminal can make mouse text selection erratic in **all** panes, not just the Claude pane. This is concretely documented for **Ghostty** (discussion #10974). A maintainer marked it fixed for Ghostty 1.3.1; later comments report recurrence on 1.3.1, and at least one commenter reported similar behavior **without** Claude Code running, which weakens a Claude-only causal claim.

> *Sources: 5 of 7 (plus zero-weight). Conflict: none on existence. Verification: **confirmed for Ghostty specifically; NOT established as universal.** Treat as terminal-specific. The "fixed in 1.3.1 → recurred on 1.3.1" history and the "seen without Claude" comment both warrant caution before attributing this broadly or treating it as resolved.*

---

## 10. Version History & Regressions

This is where the cleanest distinction must be drawn: **user-reported regressions in GitHub issues** versus **officially documented changelog entries**. They are different evidentiary tiers and are kept separate below.

### 10.1 User-reported regressions (in issues, NOT in the official changelog)

These are **two separate** user reports at different versions and dates — not two descriptions of one event:

- **Regression A — v2.1.138/2.1.139** (issue #58364, iTerm2 + tmux): mouse wheel hijacked to input history, scrollback broken past viewport. Body states 2.1.123 was clean.
- **Regression B — v2.1.152** (issue #62890, tmux scrollback): scrollback stopped working after the May 26 update; other panes unaffected.

> *Sources: the two-regressions framing was confirmed by the sources that could read both issues (notably 1 fully direct, with corroboration from 2–3 others on at least one of the two). Conflict: some sources could confirm only one of the two (retrieval limits); none claimed they were the same event or contradicted the version numbers. Verification: both are **confirmed as user reports**. Critically — **neither is documented as a tmux regression in the official changelog.** Multiple sources searched the changelog for 2.1.138 / 2.1.139 / 2.1.152 in a tmux context and found nothing. Treat these as user-reported, not Anthropic-acknowledged.*

### 10.2 Officially documented (changelog) mouse/terminal entries

These ARE in the official changelog and are version-pinned by Anthropic:

- **WSL2 mouse-wheel regression: introduced in v2.1.172, fixed in v2.1.179** ("Fixed mouse-wheel scrolling in WSL2 under Windows Terminal and VS Code (regression in 2.1.172)"). **This is WSL2 / Windows Terminal / VS Code-specific — NOT tmux.** It is important not to conflate it with the tmux user-reports above.
  > *Sources: 5 of 7 confirmed from the changelog. Conflict: none. Verification: settled; primary-confirmed and explicitly non-tmux.*

- **v2.1.176: tmux clipboard fix** ("Fixed `/copy` and mouse-selection copy not reaching the system clipboard inside tmux over SSH, and tmux paste buffer not loading on versions older than 3.2").
  > *Sources: 4 of 7 confirmed verbatim. Conflict: 1 source said it couldn't see this entry (retrieval limit). Verification: settled; primary-confirmed. Note this is a clipboard fix, distinct from the scrolling issues.*

- **`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` added in v2.1.132** (see Section 5).

### 10.3 Renderer timeline

- **v2.0.10 (Oct 8, 2025): "Rewrote terminal renderer for buttery smooth UI"** — confirmed directly from the official changelog.
  > *Sources: 1 of 7 read this directly from the changelog; others couldn't reach that far back in the truncated changelog view. Conflict: none. Verification: single-source-but-primary. Uncontradicted, but because only one source surfaced the exact entry, treat the precise wording/date as **lightly held** — worth a direct changelog check if it matters.*

- **"Differential renderer rolled out to all users, late January 2026"** — appears in a third-party blog (Angular Schule) only.
  > *Sources: 2 of 7 cited the blog. Conflict: the official changelog was explicitly searched for the phrase "differential renderer" and returned **no match.** Verification: **community-reported, NOT in the official changelog.** Do not treat as Anthropic-confirmed; the term does not appear in the official record.*

---

## 11. Practical Configuration Patterns

Consolidated from the sources, the working mental model is:

```
Fullscreen renderer  (/tui fullscreen  OR  CLAUDE_CODE_NO_FLICKER=1)
   → alternate-screen, flicker-free, mouse support inside Claude Code
   → in tmux, wheel scrolling REQUIRES  set -g mouse on
   → disable just the mouse capture with  CLAUDE_CODE_DISABLE_MOUSE=1
     (keeps flicker-free rendering + native terminal selection)

Classic renderer  (CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1)
   → conversation stays in tmux native scrollback
   → tmux copy-mode, Cmd+f, search all work normally
   → overrides NO_FLICKER / tui setting
```

Additional tmux settings several sources recommend for cleaner key/mouse passthrough (community-sourced, not from official docs unless noted): `set -g allow-passthrough on`, `set -s extended-keys on`, terminal-feature hints, and passthrough key bindings (e.g., `bind o send-keys C-o`). For long-output review when the in-app UI is unreliable, sources recommend leaning on tmux copy-mode (`Ctrl-b [`) and `capture-pane`.

> *Sources: the renderer model itself is 6 of 7 (official docs). The supplementary tmux passthrough settings are 3 of 7 (community guides, chiefly hboon.com and a couple of dev blogs). Conflict: none. Verification: renderer model settled and primary-confirmed; the supplementary tmux tweaks are community best-practice and reasonable but not from official Claude Code docs — apply and test rather than treat as guaranteed.*

---

## 12. Conclusion / Summary

The dominant, universally-agreed reality is that **Claude Code's mouse capture and tmux's mouse handling contend for the same events**, producing wrong-target scrolling, vanished scrollback, broken selection, flicker, and copy/paste regressions. The modern resolution mechanism — **fullscreen rendering plus three environment variables** (`CLAUDE_CODE_NO_FLICKER`, `CLAUDE_CODE_DISABLE_MOUSE`, `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN`) — is fully settled and primary-confirmed, as is the renderer-dependent behavior of `set -g mouse on` and the official `tmux -CC` incompatibility.

What is **well-attested but should be held as user-reported rather than Anthropic-confirmed**: the tmux scrollback/mouse regressions at 2.1.138/2.1.139 and 2.1.152 (two separate reports, neither in the changelog). What is **terminal-specific and not universal**: the cross-pane selection breakage (documented for Ghostty). What is **community-only and not in the official record**: the "differential renderer / late-January-2026 rollout" narrative. And one structural caveat worth carrying: the proposed DECSET-2026 flicker fix (#37283) depended on a tmux feature that was unreleased at the time of reporting.

The one officially version-pinned mouse regression — **WSL2 wheel scrolling, 2.1.172 → fixed 2.1.179** — is **not** a tmux issue and should not be conflated with the tmux user-reports, a distinction that resolves a fair amount of the confusion circulating in secondary summaries.

---

## 13. Bibliography

*Format: name; URL; date; what it verifies; brief snippet. Items not loaded directly by every source are still listed where at least one source verified them.*

1. **Claude Code Docs — Fullscreen rendering**
   https://code.claude.com/docs/en/fullscreen
   Date: live (fetched mid-2026; references v2.1.89+).
   Verifies: fullscreen as opt-in flicker-free mode (v2.1.89+); all three env vars and their behavior; `set -g mouse on` requirement in fullscreen; iTerm2 `tmux -CC` incompatibility.
   Snippet: "Fullscreen rendering is an opt-in research preview and requires Claude Code v2.1.89 or later… Mouse wheel scrolling requires tmux's mouse mode… the mouse wheel does nothing, and double-click can corrupt the terminal state."

2. **Claude Code Docs — Configure your terminal for Claude Code**
   https://code.claude.com/docs/en/terminal-config
   Date: live (references v2.1.118).
   Verifies: tmux default-breakage guidance; fullscreen as the fix for flicker/scrollback jumps; `CLAUDE_CODE_NO_FLICKER` as the default-start option.
   Snippet: "When Claude Code runs inside tmux, two things break by default…"

3. **Claude Code Docs — Environment variables**
   https://code.claude.com/docs/en/env-vars
   Date: live.
   Verifies: authoritative existence/behavior of `CLAUDE_CODE_DISABLE_MOUSE`, `CLAUDE_CODE_NO_FLICKER`, `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` and the precedence of the latter.
   Snippet: "Set to `1` to disable fullscreen rendering and use the classic main-screen renderer… Takes precedence over `CLAUDE_CODE_NO_FLICKER`."

4. **Claude Code Docs — Changelog**
   https://code.claude.com/docs/en/changelog
   Date: continuously updated.
   Verifies: `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` added in v2.1.132; v2.0.10 renderer rewrite (Oct 8, 2025); WSL2 wheel regression 2.1.172→fixed 2.1.179; 2.1.176 tmux-over-SSH clipboard fix; absence of "differential renderer"; absence of any tmux-regression entry at 2.1.138/2.1.139/2.1.152.
   Snippet: "Rewrote terminal renderer for buttery smooth UI." / "Fixed mouse-wheel scrolling in WSL2 under Windows Terminal and VS Code (regression in 2.1.172)."

5. **GitHub #9902 — Mouse scroll in tmux scrolls input box instead of output**
   https://github.com/anthropics/claude-code/issues/9902
   Date: Oct 19, 2025.
   Verifies: wheel scrolls input box, not output.
   Snippet: "using the mouse wheel to scroll scrolls the input box instead of the output/conversation area."

6. **GitHub #38810 — Claude Code captures mouse events in tmux, making scrollback completely unusable**
   https://github.com/anthropics/claude-code/issues/38810
   Date: Mar 25, 2026.
   Verifies: scrollbar disappears; wheel cycles input history; `set -g mouse on/off` didn't help in reporter's setup.
   Snippet: "The scrollbar disappears entirely… `tmux set -g mouse on/off` doesn't help in either mode."

7. **GitHub #30644 — Mouse escape sequences leak into input prompt in tmux with SGR mouse mode**
   https://github.com/anthropics/claude-code/issues/30644
   Date: Mar 4, 2026.
   Verifies: raw SGR sequences inserted as prompt text (SGR-leak, not high-CPU).
   Snippet: "Raw SGR mouse escape sequences appear as text in the input prompt."

8. **GitHub #58364 — iTerm2 + tmux: mouse wheel hijacked… (2.1.138 + 2.1.139)**
   https://github.com/anthropics/claude-code/issues/58364
   Date: May 12, 2026.
   Verifies: title contains 2.1.138/2.1.139; body states 2.1.123 clean.
   Snippet: "Scrollback regression appeared on 2.1.138 and persists on 2.1.139… 2.1.123 (cached locally) does not exhibit this."

9. **GitHub #37283 — TUI flickers/cursor jumps in tmux during streaming output (missing DECSET 2026 synchronized output)**
   https://github.com/anthropics/claude-code/issues/37283
   Date: Mar 22, 2026; closed as not planned May 28, 2026.
   Verifies: flicker/cursor-jump during streaming; DECSET 2026 in title and body; environment CC 2.1.81 / tmux 3.4 / WSL2; commenter notes tmux sync-output passthrough was unreleased.
   Snippet: "The TUI should use synchronized output (DECSET 2026: `\e[?2026h` … `\e[?2026l`)."

10. **GitHub #826 — Console scrolling top of history when claude add text**
    https://github.com/anthropics/claude-code/issues/826
    Date: Apr 19, 2025.
    Verifies: view jumps to top of history on new output.
    Snippet: "the terminal scroll back and forth to the top and back to current level."

11. **GitHub #9935 — Excessive scroll events causing UI jitter in terminal multiplexers**
    https://github.com/anthropics/claude-code/issues/9935
    Date: Oct 20, 2025.
    Verifies: 4,000–6,700 scroll events/sec in tmux/smux causing jitter.
    Snippet: "4,000-6,700 scroll events per second when running inside terminal multiplexers."

12. **GitHub #15780 — [FEATURE] support mouse scrolling in tmux**
    https://github.com/anthropics/claude-code/issues/15780
    Date: opened Dec 30, 2025 (open, stale).
    Verifies: can't scroll up/down in tmux while using Claude; community workaround `set -g mouse on`.
    Snippet: "Currently, it's not possible to scroll up or down within a tmux session when using Claude."

13. **GitHub #66957 — Mouse capture in TUI breaks Linux PRIMARY-selection middle-click paste**
    https://github.com/anthropics/claude-code/issues/66957
    Date: Jun 10, 2026.
    Verifies: mouse capture breaks Linux PRIMARY middle-click paste; reproduces with and without tmux.
    Snippet: "The middle-click is consumed by Claude Code; the PRIMARY selection is never pasted."

14. **GitHub #63545 — Claude Code inside tmux: scroll hits input not scrollback**
    https://github.com/anthropics/claude-code/issues/63545
    Date: ~May 28–29, 2026.
    Verifies: detached/wrapper tmux — scroll hits input, rich mode never engages, transcript not saved.
    Snippet: "Scroll goes to the input field, not the conversation… Rich render mode never engages."

15. **GitHub #62890 — Tmux scrollback broken after May 26 update (v2.1.152)**
    https://github.com/anthropics/claude-code/issues/62890
    Date: May 27, 2026.
    Verifies: separate user-reported tmux scrollback regression at v2.1.152.
    Snippet: "After the update to v2.1.152 (installed May 26, 2026), tmux scrollback no longer works."

16. **GitHub #56881 — [DOCS] Missing CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN documentation**
    https://github.com/anthropics/claude-code/issues/56881
    Date: ~May 6–7, 2026.
    Verifies: v2.1.132 added the env var ahead of docs.
    Snippet: "The changelog for v2.1.132 added `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`."

17. **GitHub #69619 — [DOCS] Fullscreen docs omit CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT troubleshooting**
    https://github.com/anthropics/claude-code/issues/69619
    Date: Jun 19, 2026.
    Verifies: primarily about a *different* env var (`ALT_SCREEN_FULL_REPAINT`); `DISABLE_ALTERNATE_SCREEN` only mentioned as fallback.
    Snippet: "Fullscreen docs omit `CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT` troubleshooting…"

18. **GitHub (Ghostty) Discussion #10974 — Mouse text selection becomes erratic in all panes**
    https://github.com/ghostty-org/ghostty/discussions/10974
    Date: asked Feb 23–24, 2026; maintainer answer Mar 10, 2026; later recurrence comments.
    Verifies: Ghostty-specific cross-pane selection degradation; "fixed in 1.3.1" then recurrence reported.
    Snippet: "mouse text selection becomes erratic and unreliable in all other panes… This is fixed, will be in 1.3.1."

19. **Hwee-Boon Yar — Using tmux with Claude Code**
    https://hboon.com/using-tmux-with-claude-code/
    Date: Nov 27, 2025.
    Verifies (secondary): avoid iTerm2 `tmux -CC`; enable `set -g mouse on` for fullscreen wheel scrolling; tmux passthrough settings.
    Snippet: "Don't use iTerm2's `tmux -CC` integration mode with Claude Code fullscreen rendering."

20. **Reddit r/ClaudeCode — Scrolling inside tmux broken recently?**
    https://www.reddit.com/r/ClaudeCode/comments/1sxmg52/scrolling_inside_tmux_broken_recently/
    Date: ~Apr 27, 2026.
    Verifies (community): post-update tmux scrollback breakage; workarounds via `/tui fullscreen` and `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN`.
    Snippet: "seen scrolling up get broken suddenly over the last week or two?"

21. **Angular Schule — Claude Code: How to Actually Fix the Endless Scrolling**
    https://angular.schule/blog/2026-02-claude-code-scrolling/
    Date: Feb 16–17, 2026.
    Verifies (community-only): v2.0.10 renderer rewrite claim and "differential renderer rolled out late January 2026" — the latter NOT corroborated by the official changelog.
    Snippet: "In version 2.0.10 (October 2025), they completely rewrote the renderer, and in late January 2026, the new 'differential renderer' was rolled out to all users."

22. **Hacker News — "Claude Chill" / TUI rendering thread (#46699072)**
    https://news.ycombinator.com/item?id=46699072
    Date: ~Jan 20, 2026.
    Verifies (community): Anthropic TUI dev acknowledging flicker as a long-standing frustration, supporting the renderer-work narrative.
    Snippet: "I work on TUI rendering for Claude Code. I know this has been a long-standing frustration."

---

*End of technical reference.*

**A brief methodological note for the reader:** confidence in this document tracks evidentiary tier, not vote count. Several items confirmed by only two or three sources (e.g., #9935, #62890, the v2.0.10 entry) are held with high confidence because they were verified against primary sources and never contradicted — only unreached by sources with weaker retrieval. Conversely, items where many sources echoed the same secondary blog (the "differential renderer" claim) are explicitly flagged as unverified against the primary record. Where this document says "settled," it means primary-source-confirmed with no genuine contradiction; where it says "user-reported," "terminal-specific," "community-only," or "lightly held," an independent check is advised before treating the item as authoritative.