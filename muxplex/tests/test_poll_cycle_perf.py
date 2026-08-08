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
    capture-pane          (concurrent)           N        O(N)  <- item 1.2
    list-panes -a                                1        O(1)
    list-windows -a       (BATCHED, item 1.1)    1        O(1)
    ------------------------------------------------------------------
    total                                      N + 3

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_names(n: int) -> list[str]:
    return [f"perf-sess-{i}" for i in range(n)]


def _make_sessions_tmux_mock(names: list[str]) -> AsyncMock:
    """AsyncMock standing in for ``muxplex.sessions.run_tmux``.

    Answers the three commands the poll cycle issues through the sessions
    module, and raises on anything unexpected so a new spawn site can't sneak
    in unnoticed.
    """

    async def _side_effect(*args: str) -> str:
        cmd = args[0] if args else ""
        if cmd == "list-sessions":
            return "\n".join(names) + ("\n" if names else "")
        if cmd == "capture-pane":
            return "snapshot text\n"
        if cmd == "list-panes":
            # '#{session_name}\t#{window_active}\t#{pane_active}\t#{pane_current_path}'
            return "".join(f"{n}\t1\t1\t/tmp/perf\n" for n in names)
        raise AssertionError(f"unexpected tmux command in poll cycle: {args!r}")

    return AsyncMock(side_effect=_side_effect)


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
    yield
    bells_mod._bell_seen.clear()


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
    """PINNED: one poll cycle costs ``N + 3`` tmux fork/exec pairs.

    Was ``2N + 2`` before item 1.1 batched the bell path.  At N=20 and a 2 s
    cadence that is 23 spawns per cycle instead of 42; at N=100, 103 instead
    of 202.

    >>> EXPECTED TO CHANGE <<<
    The remaining O(N) term is entirely ``capture-pane`` — item 1.2 attacks it
    (bound the concurrency, then snapshot only what is rendered / only what
    changed).  Update the table when that lands; do not delete the test.
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock(names)
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    cmds = [c.args[0] for c in sessions_tmux.call_args_list]
    assert cmds.count("list-sessions") == 1  # O(1)
    assert cmds.count("capture-pane") == n  # O(N) — item 1.2
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
