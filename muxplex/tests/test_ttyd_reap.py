"""Reaping the ttyd left attached to a session that no longer exists.

ttyd is a SERVER, not a wrapper.  muxplex spawns it without ``--once``, so it
binds its port once and re-forks ``tmux attach -t <name>`` for every client that
connects; it does not exit when a child exits.  Pointed at a destroyed session,
every fork prints ``can't find session: <name>`` and dies in ~11ms.

The poll cycle already cleared ``active_session`` when its session vanished, but
left that process running.  Because ``_ttyd_is_listening()`` is a bare TCP probe,
the orphan reads as perfectly healthy forever, and every browser reconnect gets
a fresh doomed attach — the "Reconnecting…" loop, sustained entirely
server-side.  These tests pin the reap that closes it.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import muxplex.main as main_mod


@pytest.fixture
def poll_env(monkeypatch):
    """Stub every side effect of the poll cycle except the ttyd reap."""
    state_stub = {
        "session_order": [],
        "sessions": {},
        "active_session": None,
        "devices": {},
    }

    monkeypatch.setattr(main_mod, "snapshot_all", AsyncMock(return_value={}))
    monkeypatch.setattr(main_mod, "update_session_cache", MagicMock())
    monkeypatch.setattr(main_mod, "list_session_paths", AsyncMock(return_value={}))
    monkeypatch.setattr(main_mod, "update_session_paths", MagicMock())
    monkeypatch.setattr(main_mod, "load_state", MagicMock(return_value=state_stub))
    monkeypatch.setattr(main_mod, "save_state", MagicMock())
    monkeypatch.setattr(main_mod, "process_bell_flags", AsyncMock())
    monkeypatch.setattr(main_mod, "apply_bell_clear_rule", MagicMock())
    monkeypatch.setattr(main_mod, "prune_devices", MagicMock())
    monkeypatch.setattr(main_mod, "load_device_id", MagicMock(return_value="devA"))
    monkeypatch.setattr(main_mod, "_federation_client", None)

    kills = {"count": 0}

    async def counting_kill():
        kills["count"] += 1
        return True

    monkeypatch.setattr(main_mod, "kill_ttyd", counting_kill)

    async def run_cycle(live_names):
        monkeypatch.setattr(
            main_mod, "enumerate_sessions", AsyncMock(return_value=list(live_names))
        )
        await main_mod._run_poll_cycle()

    return state_stub, kills, run_cycle


async def test_ttyd_reaped_when_active_session_vanishes(poll_env):
    """The orphan ttyd is killed in the same cycle that clears active_session."""
    state, kills, run_cycle = poll_env
    state["active_session"] = "doomed"

    await run_cycle(["other-1", "other-2"])

    assert state["active_session"] is None, "active_session must be cleared"
    assert kills["count"] == 1, (
        "the ttyd attached to the vanished session must be reaped -- leaving it "
        "listening is what sustained the reconnect loop"
    )


async def test_ttyd_not_reaped_while_its_session_lives(poll_env):
    """A live active_session must never have its ttyd killed."""
    state, kills, run_cycle = poll_env
    state["active_session"] = "alive"

    await run_cycle(["alive", "other"])

    assert state["active_session"] == "alive"
    assert kills["count"] == 0, "killing ttyd for a LIVE session would drop the user"


async def test_no_reap_when_nothing_was_active(poll_env):
    """A null active_session must not trigger a pointless kill every 2 seconds."""
    state, kills, run_cycle = poll_env
    state["active_session"] = None

    await run_cycle(["a", "b"])

    assert kills["count"] == 0, "must not kill ttyd on every cycle when idle"


async def test_reap_is_not_repeated_on_subsequent_cycles(poll_env):
    """Once cleared, later cycles are quiet -- the kill fires once, not forever."""
    state, kills, run_cycle = poll_env
    state["active_session"] = "doomed"

    await run_cycle(["other"])
    await run_cycle(["other"])
    await run_cycle(["other"])

    assert kills["count"] == 1, (
        f"reap must fire once, not once per cycle (fired {kills['count']}x)"
    )


async def test_reap_failure_does_not_break_the_poll_cycle(poll_env, monkeypatch):
    """A kill_ttyd error must not abort the cycle -- state was already saved."""
    state, _kills, run_cycle = poll_env
    state["active_session"] = "doomed"

    async def exploding_kill():
        raise OSError("no such process")

    monkeypatch.setattr(main_mod, "kill_ttyd", exploding_kill)

    await run_cycle(["other"])  # must not raise

    assert state["active_session"] is None
