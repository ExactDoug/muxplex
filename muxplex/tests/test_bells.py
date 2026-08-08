"""
Tests for coordinator/bells.py — bell flag polling and unseen_count tracking.
All 17 acceptance-criteria tests are defined here.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest

from muxplex.bells import (
    _bell_seen,
    apply_bell_clear_rule,
    poll_all_bell_flags,
    poll_bell_flag,
    process_bell_flags,
    should_clear_bell,
)
from muxplex.state import empty_bell, empty_state


# ---------------------------------------------------------------------------
# autouse fixture — clear _bell_seen before/after each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_bell_seen():
    """Clear _bell_seen before and after each test for isolation."""
    _bell_seen.clear()
    yield
    _bell_seen.clear()


# ---------------------------------------------------------------------------
# poll_bell_flag tests
# ---------------------------------------------------------------------------


async def test_poll_bell_flag_returns_true_when_flag_is_1():
    """poll_bell_flag returns True when tmux reports window_bell_flag=1."""
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value="1\n")):
        result = await poll_bell_flag("my-session")
    assert result is True


async def test_poll_bell_flag_returns_false_when_flag_is_0():
    """poll_bell_flag returns False when tmux reports window_bell_flag=0."""
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value="0\n")):
        result = await poll_bell_flag("my-session")
    assert result is False


async def test_poll_bell_flag_returns_false_on_error():
    """poll_bell_flag returns False when run_tmux raises RuntimeError."""
    with patch(
        "muxplex.bells.run_tmux",
        new=AsyncMock(side_effect=RuntimeError("session not found")),
    ):
        result = await poll_bell_flag("my-session")
    assert result is False


# ---------------------------------------------------------------------------
# process_bell_flags tests
# ---------------------------------------------------------------------------


async def test_process_bell_flags_increments_unseen_count_on_new_bell():
    """process_bell_flags increments unseen_count on a 0→1 transition."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-a": True}),
    ):
        changed = await process_bell_flags(["session-a"], state)

    assert changed is True
    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 1
    assert state["sessions"]["session-a"]["bell"]["last_fired_at"] is not None


async def test_process_bell_flags_does_not_double_count_persistent_flag():
    """process_bell_flags does not increment unseen_count if flag stays at 1."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-a": True}),
    ):
        # First poll — 0→1 transition
        await process_bell_flags(["session-a"], state)
        # Second poll — 1→1 (persistent), should NOT increment again
        changed = await process_bell_flags(["session-a"], state)

    assert changed is False
    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 1


async def test_process_bell_flags_resets_tracking_when_flag_clears():
    """1→0→1 sequence counts as two separate bells."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}

    # side_effect drives three sequential calls: 0→1, 1→0, 0→1
    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(
            side_effect=[
                {"session-a": True},
                {"session-a": False},
                {"session-a": True},
            ]
        ),
    ):
        for _ in range(3):
            await process_bell_flags(["session-a"], state)

    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 2


async def test_process_bell_flags_no_change_returns_false():
    """process_bell_flags returns False when no bell state changed."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-a": False}),
    ):
        changed = await process_bell_flags(["session-a"], state)

    assert changed is False
    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 0


async def test_process_bell_flags_creates_bell_entry_if_missing():
    """process_bell_flags creates the bell sub-dict if session has no bell key."""
    state = empty_state()
    state["sessions"]["session-a"] = {}  # no 'bell' key

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-a": False}),
    ):
        await process_bell_flags(["session-a"], state)

    assert "bell" in state["sessions"]["session-a"]
    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 0


# ---------------------------------------------------------------------------
# should_clear_bell tests
# ---------------------------------------------------------------------------


def test_should_clear_bell_returns_true_for_fullscreen_recent_interaction():
    """should_clear_bell returns True when a device is fullscreen and interacted recently."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,  # 10 seconds ago
        "last_heartbeat_at": time.time(),
    }

    assert should_clear_bell("session-a", state) is True


def test_should_clear_bell_returns_false_for_grid_mode():
    """should_clear_bell returns False when device is in grid mode."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "grid",
        "last_interaction_at": time.time() - 10.0,  # recent interaction
        "last_heartbeat_at": time.time(),
    }

    assert should_clear_bell("session-a", state) is False


def test_should_clear_bell_returns_false_when_interaction_too_old():
    """should_clear_bell returns False when last interaction was more than 60s ago."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 90.0,  # 90 seconds ago (> 60s window)
        "last_heartbeat_at": time.time(),
    }

    assert should_clear_bell("session-a", state) is False


def test_should_clear_bell_returns_false_when_device_viewing_different_session():
    """should_clear_bell returns False when device is viewing a different session."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-b",  # different session
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }

    assert should_clear_bell("session-a", state) is False


def test_should_clear_bell_returns_false_when_no_devices():
    """should_clear_bell returns False when there are no connected devices."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    # No devices in state["devices"]

    assert should_clear_bell("session-a", state) is False


# ---------------------------------------------------------------------------
# apply_bell_clear_rule tests
# ---------------------------------------------------------------------------


def test_apply_bell_clear_rule_clears_matching_sessions():
    """apply_bell_clear_rule resets unseen_count to 0 and sets seen_at for qualifying sessions."""
    state = empty_state()
    state["sessions"]["session-a"] = {
        "bell": {
            "unseen_count": 3,
            "last_fired_at": time.time() - 30.0,
            "seen_at": None,
        }
    }
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }

    before = time.time()
    apply_bell_clear_rule(state)
    after = time.time()

    bell = state["sessions"]["session-a"]["bell"]
    assert bell["unseen_count"] == 0
    assert bell["seen_at"] is not None
    assert before <= bell["seen_at"] <= after


def test_apply_bell_clear_rule_skips_sessions_with_zero_unseen():
    """apply_bell_clear_rule does not modify sessions that already have unseen_count == 0."""
    state = empty_state()
    state["sessions"]["session-a"] = {
        "bell": {
            "unseen_count": 0,
            "last_fired_at": None,
            "seen_at": None,
        }
    }
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }

    result = apply_bell_clear_rule(state)

    assert result == []
    assert state["sessions"]["session-a"]["bell"]["seen_at"] is None


def test_apply_bell_clear_rule_returns_list_of_cleared_session_names():
    """apply_bell_clear_rule returns the names of sessions that were cleared."""
    state = empty_state()
    state["sessions"]["session-a"] = {
        "bell": {"unseen_count": 2, "last_fired_at": time.time() - 5.0, "seen_at": None}
    }
    state["sessions"]["session-b"] = {
        "bell": {"unseen_count": 1, "last_fired_at": time.time() - 5.0, "seen_at": None}
    }
    state["sessions"]["session-c"] = {
        "bell": {"unseen_count": 0, "last_fired_at": None, "seen_at": None}
    }
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }
    state["devices"]["device-2"] = {
        "label": "Device 2",
        "viewing_session": "session-b",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }

    result = apply_bell_clear_rule(state)

    assert sorted(result) == ["session-a", "session-b"]


def test_apply_bell_clear_rule_resets_bell_seen_tracking():
    """apply_bell_clear_rule resets _bell_seen[name] = False for cleared sessions."""
    state = empty_state()
    state["sessions"]["session-a"] = {
        "bell": {"unseen_count": 1, "last_fired_at": time.time() - 5.0, "seen_at": None}
    }
    state["devices"]["device-1"] = {
        "label": "Device 1",
        "viewing_session": "session-a",
        "view_mode": "fullscreen",
        "last_interaction_at": time.time() - 10.0,
        "last_heartbeat_at": time.time(),
    }

    # Pre-seed _bell_seen as if the bell was previously seen
    _bell_seen["session-a"] = True

    apply_bell_clear_rule(state)

    assert _bell_seen.get("session-a") is False


# ---------------------------------------------------------------------------
# poll_all_bell_flags tests (batched bell poll — plan item 1.1)
# ---------------------------------------------------------------------------


async def test_poll_all_bell_flags_ors_across_windows():
    """A bell in a NON-active window still marks the session as belling (OR)."""
    output = "session-a\t0\nsession-a\t1\nsession-b\t0\n"
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value=output)) as rt:
        flags = await poll_all_bell_flags()

    assert flags == {"session-a": True, "session-b": False}
    # No -f '#{window_active}' parity filter: every window is considered.
    assert "-f" not in rt.call_args.args


async def test_poll_all_bell_flags_uses_tab_delimiter_and_one_call():
    """Format string is tab-delimited and exactly one tmux call is made."""
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value="")) as rt:
        await poll_all_bell_flags()

    assert rt.await_count == 1
    assert rt.call_args.args == (
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_bell_flag}",
    )


async def test_poll_all_bell_flags_parses_session_names_containing_spaces():
    """Session names may contain spaces — tab-delimited parsing must survive them."""
    output = "my session name\t1\nother session\t0\n"
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value=output)):
        flags = await poll_all_bell_flags()

    assert flags == {"my session name": True, "other session": False}


async def test_poll_all_bell_flags_returns_none_on_runtime_error():
    """A tmux failure yields None — "unknown", NOT "no bells".

    Returning {} here would be indistinguishable from tmux answering with no
    bells set, which drives a spurious 1->0 transition. See
    test_process_bell_flags_query_failure_preserves_latch.
    """
    with patch(
        "muxplex.bells.run_tmux", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        assert await poll_all_bell_flags() is None


async def test_poll_all_bell_flags_returns_none_on_file_not_found():
    """tmux missing from PATH yields None, not an exception and not {}."""
    with patch(
        "muxplex.bells.run_tmux", new=AsyncMock(side_effect=FileNotFoundError())
    ):
        assert await poll_all_bell_flags() is None


async def test_process_bell_flags_query_failure_preserves_latch():
    """A failed bell query must not reset the seen-latch or over-count.

    RFR round 2 finding. With the flag genuinely still set, a failed poll that
    reported "no bells" would clear the latch, so the next SUCCESSFUL poll would
    read the still-set flag as a fresh 0->1 transition and increment
    unseen_count a second time for one real bell.
    """
    state = {"sessions": {}}

    # Cycle 1: a real bell fires. unseen_count -> 1, latch set.
    with patch(
        "muxplex.bells.poll_all_bell_flags", new=AsyncMock(return_value={"s": True})
    ):
        await process_bell_flags(["s"], state)
    assert state["sessions"]["s"]["bell"]["unseen_count"] == 1

    # Cycle 2: tmux is briefly unavailable — the query FAILS.
    with patch("muxplex.bells.poll_all_bell_flags", new=AsyncMock(return_value=None)):
        changed = await process_bell_flags(["s"], state)
    assert changed is False, "a failed query is not a state change"
    assert state["sessions"]["s"]["bell"]["unseen_count"] == 1, (
        "nothing is lost on failure — unseen_count is never decremented"
    )

    # Cycle 3: tmux recovers, flag STILL set (nobody looked at the window).
    with patch(
        "muxplex.bells.poll_all_bell_flags", new=AsyncMock(return_value={"s": True})
    ):
        await process_bell_flags(["s"], state)
    assert state["sessions"]["s"]["bell"]["unseen_count"] == 1, (
        "one real bell must count once — a failed poll in between must not make "
        "the still-set flag look like a second 0->1 transition"
    )


async def test_process_bell_flags_query_failure_still_creates_bell_entries():
    """Even when the query fails, session bell scaffolding is created."""
    state = {"sessions": {}}
    with patch("muxplex.bells.poll_all_bell_flags", new=AsyncMock(return_value=None)):
        await process_bell_flags(["fresh"], state)
    assert state["sessions"]["fresh"]["bell"] == empty_bell()


async def test_poll_all_bell_flags_skips_malformed_rows():
    """Rows without a tab are ignored rather than corrupting the map."""
    output = "no-tab-row\nsession-a\t1\n\n"
    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value=output)):
        flags = await poll_all_bell_flags()

    assert flags == {"session-a": True}


# ---------------------------------------------------------------------------
# process_bell_flags — batched-path behaviour (plan items 1.1 / 4.1)
# ---------------------------------------------------------------------------


async def test_process_bell_flags_session_absent_from_output_defaults_false():
    """A session missing from the batched result is treated as no-bell."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    state["sessions"]["session-b"] = {"bell": empty_bell()}

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-b": True}),
    ):
        changed = await process_bell_flags(["session-a", "session-b"], state)

    assert changed is True
    assert state["sessions"]["session-a"]["bell"]["unseen_count"] == 0
    assert state["sessions"]["session-b"]["bell"]["unseen_count"] == 1


async def test_process_bell_flags_makes_exactly_one_tmux_call_for_many_sessions():
    """The bell path is O(1) tmux spawns regardless of session count."""
    names = [f"session-{i}" for i in range(50)]
    state = empty_state()
    output = "".join(f"{n}\t0\n" for n in names)

    with patch("muxplex.bells.run_tmux", new=AsyncMock(return_value=output)) as rt:
        await process_bell_flags(names, state)

    assert rt.await_count == 1


async def test_process_bell_flags_evicts_bell_seen_for_dead_sessions():
    """_bell_seen is pruned down to the live session set."""
    state = empty_state()
    state["sessions"]["session-a"] = {"bell": empty_bell()}
    _bell_seen["gone-session"] = True
    _bell_seen["renamed-away"] = False

    with patch(
        "muxplex.bells.poll_all_bell_flags",
        new=AsyncMock(return_value={"session-a": True}),
    ):
        await process_bell_flags(["session-a"], state)

    assert "gone-session" not in _bell_seen
    assert "renamed-away" not in _bell_seen
    assert _bell_seen["session-a"] is True
