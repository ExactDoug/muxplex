"""
Characterization ("pinning") tests for the cost of ONE ``_run_poll_cycle()``.

These tests exist because of plan item 0.3
(``docs/plans/2026-08-08-resource-efficiency-plan.md``): ``_run_poll_cycle()``
had **zero** non-integration coverage, and nothing anywhere counted tmux process
spawns — which makes the central thesis of the whole efficiency plan ("per-poll
work must stop scaling with N") unverifiable.

They assert on WORK DONE (subprocess spawns, sidecar writes), not on results.
They are written against the code **as it is today** and therefore all PASS
today.  When an optimization lands, the expected value changes and the test is
updated deliberately — that is the point: the cost becomes visible and a silent
regression becomes impossible.

Measured shape today (verified by the tests below):

    tmux spawns per poll cycle, N live sessions
    ------------------------------------------------------------------
    list-sessions                                1        O(1)
    list-panes -a         (BATCHED, item 1.2c)   1        O(1)
    capture-pane          (item 1.2c)            C        O(changed)
    list-windows -a       (BATCHED, item 1.1)    1        O(1)
    ------------------------------------------------------------------
    total                                      C + 3

where C is the number of sessions whose pane CHANGED since their cached
snapshot, NOT N.  C == N on the first cycle (cold cache) and on the every-15th
backstop cycle; C == 0 across an idle fleet.  On this machine's live 45-window
fleet, 2 of 45 windows changed over 6 s -- so C is ~0 in steady state and the
poll cycle is O(1) in N for the first time.

HISTORY — item 1.2c landed 2026-08-08.  ``capture-pane`` used to run
unconditionally for EVERY session every cycle (the last O(N) term).  It is now
gated on a composite change key --
``window_activity|pane_id|pane_width|pane_height`` -- carried for free on the
already-batched ``list-panes -a``.  Ordering in ``_run_poll_cycle`` is
load-bearing: ``list_session_paths`` (which publishes the keys) MUST precede
``snapshot_all`` (which consumes them), or the keys are a cycle stale.

HISTORY — item 1.1 landed 2026-08-08.  The bell path used to be N SEQUENTIAL
``tmux display-message`` spawns (one per session) executed while ``state_lock``
was held, making the cycle ``2N + 2`` and adding N x spawn-RTT of latency to
every state reader.  It is now ONE batched
``tmux list-windows -a -F '#{session_name}\\t#{window_bell_flag}'``.
Do NOT reintroduce a per-session bell query.

Seams used (all established elsewhere in this suite):
  * ``muxplex.sessions.run_tmux``  — covers list-sessions / capture-pane /
    list-panes, because ``enumerate_sessions``/``capture_pane``/
    ``list_session_paths`` resolve it from the ``muxplex.sessions`` globals.
  * ``muxplex.bells.run_tmux``     — SEPARATE mock: ``bells.py`` does
    ``from muxplex.sessions import run_tmux`` at import time, so it holds its
    own reference.  Patching only ``muxplex.sessions.run_tmux`` would NOT
    intercept the bell path (and would spawn real tmux processes in CI).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import muxplex.bells as bells_mod
import muxplex.main as main_mod
import muxplex.sessions as sessions_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_names(n: int) -> list[str]:
    return [f"perf-sess-{i}" for i in range(n)]


def _make_sessions_tmux_mock(
    names: list[str], pane_activity: dict[str, str] | None = None
) -> AsyncMock:
    """AsyncMock standing in for ``muxplex.sessions.run_tmux``.

    Answers the three commands the poll cycle issues through the sessions
    module, and raises on anything unexpected so a new spawn site can't sneak
    in unnoticed.

    *pane_activity* maps session name → ``#{window_activity}`` value; it is
    read at CALL time, so a test can mutate it between cycles to simulate one
    session producing output.  Defaults to a constant (an idle fleet).
    """
    if pane_activity is None:
        pane_activity = {}
    pane_activity = {n: pane_activity.get(n, "1000") for n in names}

    async def _side_effect(*args: str) -> str:
        cmd = args[0] if args else ""
        if cmd == "list-sessions":
            return "\n".join(names) + ("\n" if names else "")
        if cmd == "capture-pane":
            return "snapshot text\n"
        if cmd == "list-panes":
            # sessions._PANE_FORMAT — 8 TAB-separated fields, path LAST.
            # The four middle fields form the item-1.2c change key; holding
            # them constant across cycles is what makes the fleet "idle".
            return "".join(
                f"{n}\t1\t1\t{pane_activity[n]}\t%{i}\t80\t24\t/tmp/perf\n"
                for i, n in enumerate(names)
            )
        raise AssertionError(f"unexpected tmux command in poll cycle: {args!r}")

    mock = AsyncMock(side_effect=_side_effect)
    mock.pane_activity = pane_activity  # exposed so tests can mutate it
    return mock


def _make_bells_tmux_mock(names: list[str]) -> AsyncMock:
    """AsyncMock standing in for ``muxplex.bells.run_tmux`` (list-windows -a).

    Returns one TAB-delimited row per session with the bell flag clear.  Asserts
    the command is the BATCHED ``list-windows`` — a regression to per-session
    ``display-message`` fails loudly here rather than quietly costing N spawns.
    """

    async def _side_effect(*args: str) -> str:
        assert args[0] == "list-windows", f"unexpected bell tmux command: {args!r}"
        return "".join(f"{n}\t0\n" for n in names)

    return AsyncMock(side_effect=_side_effect)


@pytest.fixture(autouse=True)
def _isolate_poll_cycle_globals(monkeypatch):
    """Reset the process-global state ``_run_poll_cycle()`` reads/writes.

    ``bells._bell_seen`` and ``main._settings_sync_counter`` are module-level and
    leak across tests; ``_federation_client`` must stay None so the poll cycle
    performs no network I/O.
    """
    bells_mod._bell_seen.clear()
    monkeypatch.setattr(main_mod, "_settings_sync_counter", 0)
    monkeypatch.setattr(main_mod, "_federation_client", None)
    # Item 1.2c change detection is process-global too: cached snapshots, the
    # per-session change keys, and the backstop cycle counter all persist
    # across tests.  Without this reset, one test's cycles leak into the next
    # test's skip decisions (and into its backstop phase).
    sessions_mod.reset_snapshot_change_tracking()
    sessions_mod._snapshots = {}
    sessions_mod._session_list = []
    yield
    bells_mod._bell_seen.clear()
    sessions_mod.reset_snapshot_change_tracking()
    sessions_mod._snapshots = {}
    sessions_mod._session_list = []


# ---------------------------------------------------------------------------
# 1. Bell path — tmux spawns per cycle vs N
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 5, 20])
async def test_bell_path_tmux_spawns_is_o1_in_n(n, monkeypatch):
    """PINNED: the bell path spawns exactly ONE tmux process, regardless of N.

    Item 1.1 replaced N sequential ``tmux display-message`` calls (one per
    session, awaited one at a time while ``state_lock`` was held) with a single
    batched ``tmux list-windows -a``.  This test is the regression guard: if
    anyone reintroduces a per-session bell query, ``call_count`` becomes N and
    this fails immediately.

    The serialized-in-lock latency mattered more than the spawn count — at N=100
    the old shape held the lock for 200-500 ms per cycle, stalling every reader
    of ``/api/state`` and ``/api/heartbeat``.
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    assert bells_tmux.call_count == 1, (
        f"bell path made {bells_tmux.call_count} tmux spawns for {n} sessions; "
        f"the batched query must cost exactly 1 regardless of N. A count of "
        f"{n} means a per-session bell query was reintroduced (item 1.1)."
    )

    # The one call must be the batched, TAB-delimited list-windows form.
    args = bells_tmux.call_args_list[0].args
    assert args[0] == "list-windows"
    assert "-a" in args
    assert "#{session_name}\t#{window_bell_flag}" in args, (
        "bell format string must be TAB-delimited — session names may contain "
        "spaces, so a space-delimited format silently mis-parses them."
    )


# ---------------------------------------------------------------------------
# 2. Total tmux spawns per cycle — the 2N+2 shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected_sessions_spawns", "expected_bell_spawns", "expected_total"),
    [
        # n   sessions-module (1 list-sessions + N capture-pane + 1 list-panes)
        #                            bells (1 batched list-windows)   total N+3
        (1, 3, 1, 4),
        (5, 7, 1, 8),
        (20, 22, 1, 23),
    ],
)
async def test_total_tmux_spawns_per_cycle_is_n_plus_3(
    n, expected_sessions_spawns, expected_bell_spawns, expected_total, monkeypatch
):
    """PINNED: the FIRST poll cycle costs ``N + 3`` tmux fork/exec pairs.

    Was ``2N + 2`` before item 1.1 batched the bell path.

    This is the COLD-CACHE cost and it is still O(N) by design: with no cached
    snapshots, every session must be captured.  It is no longer the STEADY-STATE
    cost — see ``test_second_idle_cycle_issues_zero_capture_pane_spawns``, where
    an unchanged second cycle costs a flat 3 regardless of N (item 1.2c).
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    cmds = [c.args[0] for c in sessions_tmux.call_args_list]
    assert cmds.count("list-sessions") == 1  # O(1)
    # O(N) on a COLD cache only — every session lacks a cached snapshot, so
    # every session is captured.  Item 1.2c makes the SECOND cycle 0.
    assert cmds.count("capture-pane") == n
    assert cmds.count("list-panes") == 1  # O(1)

    assert sessions_tmux.call_count == expected_sessions_spawns
    assert bells_tmux.call_count == expected_bell_spawns
    assert sessions_tmux.call_count + bells_tmux.call_count == expected_total
    assert expected_total == n + 3


async def test_zero_sessions_still_costs_two_tmux_spawns(monkeypatch):
    """Baseline constant: with no sessions the cycle is list-sessions + list-panes.

    (``snapshot_all([])`` short-circuits without gathering, and
    ``process_bell_flags`` skips the batched query entirely when there are no
    sessions — so N=0 costs 2, not 3.  That guard is deliberate: without it the
    batched call would make the empty case MORE expensive than before item 1.1.)
    """
    sessions_tmux = _make_sessions_tmux_mock([])
    bells_tmux = _make_bells_tmux_mock([])
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    assert sessions_tmux.call_count == 2
    assert bells_tmux.call_count == 0


# ---------------------------------------------------------------------------
# 3. Pruning sidecar write frequency
# ---------------------------------------------------------------------------


async def test_pruning_state_is_not_written_when_nothing_changes(monkeypatch):
    """PINNED: ``save_pruning_state()`` is NOT called when bookkeeping is stable.

    Item 2.1 made the write conditional on ``first_missed_at`` actually
    differing.  Idle cycles now perform ZERO ``pruning.json`` writes, down from
    one per cycle (~43k blocking writes/day at the 2 s cadence, all no-ops).

    CRITICAL — do NOT "simplify" this guard to ``if _prune_changed:``.  That
    flag is True only when a key was actually REMOVED from settings, while the
    grace clock is started by a *bookkeeping-only* mutation that leaves it
    False.  Since the pruning state is re-read from disk every cycle, guarding
    on ``_prune_changed`` means the clock is never persisted, resets every
    cycle, and stale-key pruning silently never fires.  The dedicated
    regression guard for that failure lives in
    ``test_pruning.py::test_grace_clock_survives_consecutive_cycles_and_eventually_prunes``.

    NOTE: asserts nothing about ``settings_updated_at`` — that is item 0.2's
    territory, covered in ``test_settings_sync_poll.py``.
    """
    names = _session_names(3)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    save_spy = MagicMock()
    monkeypatch.setattr(main_mod, "save_pruning_state", save_spy)

    await main_mod._run_poll_cycle()
    assert save_spy.call_count == 0, (
        "a cycle with no stale keys must not write the sidecar at all "
        f"(saw {save_spy.call_count})"
    )

    # Second, identical cycle — same sessions, nothing changed.
    await main_mod._run_poll_cycle()

    assert save_spy.call_count == 0, (
        f"two idle cycles must perform ZERO pruning.json writes, saw "
        f"{save_spy.call_count}. A count of 2 means the unconditional write "
        f"came back (item 2.1)."
    )


# ---------------------------------------------------------------------------
# 4. Change-detected snapshots — the steady-state cost (item 1.2c)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 5, 20])
async def test_second_idle_cycle_issues_zero_capture_pane_spawns(n, monkeypatch):
    """PINNED: an UNCHANGED second cycle spawns NO ``capture-pane`` at all.

    This is item 1.2c and it is the whole point of the plan's Priority 1: with
    the bell path batched (1.1) and captures gated on change (1.2c), the steady
    -state poll cycle is finally O(1) in N — a flat 3 spawns
    (list-sessions + list-panes + list-windows) for an idle fleet of any size.

    ``capture-pane`` is skipped only when the composite change key
    ``window_activity|pane_id|pane_width|pane_height`` is byte-identical to the
    key recorded when the cached snapshot was taken.  Everything ambiguous
    captures: new session, unknown key, failed capture, backstop cycle.
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()  # cycle 1 — cold cache
    first = [c.args[0] for c in sessions_tmux.call_args_list]
    assert first.count("capture-pane") == n

    sessions_tmux.reset_mock()
    await main_mod._run_poll_cycle()  # cycle 2 — nothing changed
    second = [c.args[0] for c in sessions_tmux.call_args_list]

    assert second.count("capture-pane") == 0, (
        f"an unchanged second cycle spawned {second.count('capture-pane')} "
        f"capture-pane processes for {n} idle sessions; item 1.2c requires 0"
    )
    assert second.count("list-sessions") == 1
    assert second.count("list-panes") == 1
    assert sessions_tmux.call_count + bells_tmux.call_count - 1 == 3, (
        "steady-state cycle must be a flat 3 tmux spawns, independent of N"
    )

    # And the snapshots are PRESERVED, not blanked — skipping a capture must
    # reuse the cached pane text, or every tile goes empty.
    assert set(sessions_mod.get_snapshots()) == set(names)
    assert all(v == "snapshot text\n" for v in sessions_mod.get_snapshots().values())


async def test_second_cycle_captures_only_the_session_that_changed(monkeypatch):
    """PINNED: work is proportional to CHANGE, not to fleet size."""
    names = _session_names(10)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    # One session produces output: its window_activity moves.
    sessions_tmux.pane_activity[names[4]] = "2000"

    sessions_tmux.reset_mock()
    await main_mod._run_poll_cycle()

    capture_targets = [
        c.args for c in sessions_tmux.call_args_list if c.args[0] == "capture-pane"
    ]
    assert len(capture_targets) == 1, (
        f"exactly one session changed; saw {len(capture_targets)} captures"
    )
    assert names[4] in capture_targets[0], (
        f"the wrong session was captured: {capture_targets[0]!r}"
    )


async def test_backstop_forces_a_full_capture_sweep_every_kth_cycle(monkeypatch):
    """PINNED: the every-Kth-cycle full sweep (K = ``_SNAPSHOT_FULL_EVERY``).

    CORRECTNESS BACKSTOP, not an optimization.  It caps any undiscovered way a
    pane's visible content can change without moving the composite key at ONE
    refresh interval (15 x 2 s = 30 s), while still removing ~93% of the
    spawns.  Removing it turns change detection from "safe" into "a bet".
    """
    k = sessions_mod._SNAPSHOT_FULL_EVERY
    n = 4
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    per_cycle = []
    for _ in range(k):
        sessions_tmux.reset_mock()
        await main_mod._run_poll_cycle()
        cmds = [c.args[0] for c in sessions_tmux.call_args_list]
        per_cycle.append(cmds.count("capture-pane"))

    assert per_cycle[0] == n, "cycle 1 is a cold cache"
    assert per_cycle[1:-1] == [0] * (k - 2), "idle cycles must capture nothing"
    assert per_cycle[-1] == n, f"cycle {k} must be the forced full sweep"


async def test_unparseable_pane_rows_force_a_full_capture(monkeypatch):
    """PINNED FAIL-SAFE: no usable change key -> capture, never reuse.

    A stale tile is a user-visible bug; an extra fork is a wasted fork.  Every
    ambiguous case must resolve toward capturing.
    """
    names = _session_names(3)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    # tmux now answers list-panes with garbage (no parseable rows at all).
    async def _broken(*args: str) -> str:
        if args[0] == "list-panes":
            return "garbage-without-tabs\n"
        return await sessions_tmux.side_effect(*args)

    broken = AsyncMock(side_effect=_broken)
    monkeypatch.setattr("muxplex.sessions.run_tmux", broken)
    await main_mod._run_poll_cycle()

    cmds = [c.args[0] for c in broken.call_args_list]
    assert cmds.count("capture-pane") == 3, (
        "unknown change keys must force a full capture, not a silent reuse"
    )
