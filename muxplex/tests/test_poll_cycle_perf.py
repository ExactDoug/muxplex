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
    capture-pane          (concurrent)           N        O(N)
    list-panes -a                                1        O(1)
    display-message       (SEQUENTIAL, in-lock)  N        O(N)
    ------------------------------------------------------------------
    total                                     2N + 2

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


def _make_bells_tmux_mock() -> AsyncMock:
    """AsyncMock standing in for ``muxplex.bells.run_tmux`` (display-message)."""

    async def _side_effect(*args: str) -> str:
        assert args[0] == "display-message", f"unexpected bell tmux command: {args!r}"
        return "0\n"  # no bell

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
async def test_bell_path_tmux_spawns_is_currently_linear_in_n(n, monkeypatch):
    """PINNED: the bell path spawns exactly ONE tmux process PER SESSION.

    ``process_bell_flags`` loops over sessions and ``await``s
    ``poll_bell_flag`` one at a time — N ``tmux display-message`` spawns,
    SERIALIZED, and the whole loop runs while ``state_lock`` is held.  That is
    the single worst scaling property of the poll cycle (it adds latency to
    every state reader, not just CPU).

    >>> EXPECTED TO CHANGE <<<
    Plan item 1.1 collapses these into ONE batched
    ``tmux list-windows -a -F '#{session_name}\\t#{window_bell_flag}'`` call.
    When that lands, this assertion flips from ``== n`` to ``== 1`` (O(1) in N).
    Until then, asserting the CURRENT value is what keeps the cost visible.
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock()
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    # --- CURRENT BEHAVIOUR: O(N) ------------------------------------------
    assert bells_tmux.call_count == n, (
        f"bell path made {bells_tmux.call_count} tmux spawns for {n} sessions; "
        f"expected the current O(N) shape ({n}). If plan item 1.1 has landed, "
        f"update this to the batched O(1) expectation."
    )

    # Every bell spawn is a per-session display-message (the thing 1.1 removes).
    targets = [c.args[2] for c in bells_tmux.call_args_list]
    assert all(c.args[0] == "display-message" for c in bells_tmux.call_args_list)
    assert sorted(targets) == sorted(names)


# ---------------------------------------------------------------------------
# 2. Total tmux spawns per cycle — the 2N+2 shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected_sessions_spawns", "expected_bell_spawns", "expected_total"),
    [
        # n   sessions-module (1 list-sessions + N capture-pane + 1 list-panes)
        #                             bells (N display-message)      total 2N+2
        (1, 3, 1, 4),
        (5, 7, 5, 12),
        (20, 22, 20, 42),
    ],
)
async def test_total_tmux_spawns_per_cycle_is_currently_2n_plus_2(
    n, expected_sessions_spawns, expected_bell_spawns, expected_total, monkeypatch
):
    """PINNED: one poll cycle costs ``2N + 2`` tmux fork/exec pairs.

    At the plan's reference point of N=20 and a 2 s cadence that is 42 spawns
    every 2 s ≈ 21 process spawns/second, ~1.8 M/day.

    >>> EXPECTED TO CHANGE <<<
    Item 1.1 (batched bells) takes this to ``N + 3``; item 1.2 attacks the
    remaining N ``capture-pane`` spawns.  Update the table above when either
    lands — do not delete the test.
    """
    names = _session_names(n)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock()
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
    assert expected_total == 2 * n + 2


async def test_zero_sessions_still_costs_two_tmux_spawns(monkeypatch):
    """Baseline constant: with no sessions the cycle is list-sessions + list-panes.

    (``snapshot_all([])`` short-circuits without gathering, and the bell loop
    has nothing to iterate — so 2N+2 holds at N=0 too.)
    """
    sessions_tmux = _make_sessions_tmux_mock([])
    bells_tmux = _make_bells_tmux_mock()
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    await main_mod._run_poll_cycle()

    assert sessions_tmux.call_count == 2
    assert bells_tmux.call_count == 0


# ---------------------------------------------------------------------------
# 3. Pruning sidecar write frequency
# ---------------------------------------------------------------------------


async def test_pruning_state_is_written_every_cycle_even_when_nothing_changes(
    monkeypatch,
):
    """PINNED: ``save_pruning_state()`` is called UNCONDITIONALLY, once per cycle.

    ``_run_poll_cycle()`` calls it outside the ``if _prune_changed:`` guard, so
    two consecutive idle cycles perform two full ``pruning.json`` writes — 43k
    blocking writes/day at the 2 s cadence, all of them no-ops.

    >>> EXPECTED TO CHANGE <<<
    Plan item 2.1 makes the write conditional on the pruning bookkeeping
    actually differing.  When that lands, the expected count for two idle
    cycles drops from 2 to 0 (or to 1 if the first cycle seeds the sidecar).

    NOTE: this test deliberately asserts NOTHING about ``settings_updated_at``
    or about prune-triggered ``save_settings`` calls — item 0.2 is changing that
    behaviour concurrently.  Scope here is the sidecar write COUNT only.
    """
    names = _session_names(3)
    sessions_tmux = _make_sessions_tmux_mock(names)
    bells_tmux = _make_bells_tmux_mock()
    monkeypatch.setattr("muxplex.sessions.run_tmux", sessions_tmux)
    monkeypatch.setattr("muxplex.bells.run_tmux", bells_tmux)

    save_spy = MagicMock()
    monkeypatch.setattr(main_mod, "save_pruning_state", save_spy)

    await main_mod._run_poll_cycle()
    assert save_spy.call_count == 1, "first cycle writes the sidecar once"

    # Second, identical cycle — same sessions, nothing changed.
    await main_mod._run_poll_cycle()

    # --- CURRENT BEHAVIOUR: unconditional write every cycle ---------------
    assert save_spy.call_count == 2, (
        f"expected the current unconditional write (2 writes over 2 idle "
        f"cycles), saw {save_spy.call_count}. If plan item 2.1 has landed, this "
        f"should drop — update the expectation deliberately."
    )
