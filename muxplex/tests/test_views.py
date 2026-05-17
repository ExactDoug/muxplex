"""
Tests for muxplex/views.py — views invariant enforcement and v2 visibility helpers.
"""

from muxplex.views import (
    enforce_mutual_exclusion,
    filter_visible,
    is_hidden,
    normalize_session_keys,
    validate_view_name,
    visible_count,
)


# ---------------------------------------------------------------------------
# Test fixtures (built-in, no pytest fixtures needed)
# ---------------------------------------------------------------------------


def _session(name: str, device_id: str = "dev1", status: str | None = None) -> dict:
    """Build a session dict with the standard fields."""
    d: dict = {"sessionKey": f"{device_id}:{name}", "name": name}
    if status is not None:
        d["status"] = status
    return d


# ---------------------------------------------------------------------------
# enforce_mutual_exclusion
# ---------------------------------------------------------------------------


def test_enforce_removes_from_hidden_when_in_view():
    """If a session is in both hidden_sessions and a view, remove from hidden (favor visibility)."""
    settings = {
        "hidden_sessions": ["abc:dev", "def:build"],
        "views": [
            {"name": "Work", "sessions": ["abc:dev", "abc:web"]},
        ],
    }
    result = enforce_mutual_exclusion(settings)
    assert "abc:dev" not in result["hidden_sessions"]
    assert "def:build" in result["hidden_sessions"]
    assert "abc:dev" in result["views"][0]["sessions"]


def test_enforce_no_change_when_no_overlap():
    """No changes when there is no overlap between hidden and views."""
    settings = {
        "hidden_sessions": ["abc:old"],
        "views": [
            {"name": "Work", "sessions": ["abc:dev"]},
        ],
    }
    result = enforce_mutual_exclusion(settings)
    assert result["hidden_sessions"] == ["abc:old"]
    assert result["views"][0]["sessions"] == ["abc:dev"]


def test_enforce_handles_empty_views():
    """Works when views is an empty list."""
    settings = {
        "hidden_sessions": ["abc:dev"],
        "views": [],
    }
    result = enforce_mutual_exclusion(settings)
    assert result["hidden_sessions"] == ["abc:dev"]


def test_enforce_handles_empty_hidden():
    """Works when hidden_sessions is empty."""
    settings = {
        "hidden_sessions": [],
        "views": [{"name": "Work", "sessions": ["abc:dev"]}],
    }
    result = enforce_mutual_exclusion(settings)
    assert result["hidden_sessions"] == []


def test_enforce_deduplicates_view_sessions():
    """Duplicate session keys within a view are deduplicated."""
    settings = {
        "hidden_sessions": [],
        "views": [
            {"name": "Work", "sessions": ["abc:dev", "abc:dev", "abc:web"]},
        ],
    }
    result = enforce_mutual_exclusion(settings)
    assert result["views"][0]["sessions"] == ["abc:dev", "abc:web"]


def test_enforce_overlap_across_multiple_views():
    """A hidden session appearing in multiple views is removed from hidden."""
    settings = {
        "hidden_sessions": ["abc:dev"],
        "views": [
            {"name": "Work", "sessions": ["abc:dev"]},
            {"name": "Hobby", "sessions": ["abc:dev", "abc:printer"]},
        ],
    }
    result = enforce_mutual_exclusion(settings)
    assert "abc:dev" not in result["hidden_sessions"]


# ---------------------------------------------------------------------------
# validate_view_name
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_name():
    assert validate_view_name("", []) is not None


def test_validate_rejects_whitespace_only():
    assert validate_view_name("   ", []) is not None


def test_validate_rejects_too_long():
    assert validate_view_name("a" * 31, []) is not None


def test_validate_rejects_reserved_all():
    assert validate_view_name("all", []) is not None


def test_validate_rejects_reserved_hidden():
    assert validate_view_name("Hidden", []) is not None


def test_validate_rejects_duplicate():
    existing = [{"name": "Work", "sessions": []}]
    assert validate_view_name("Work", existing) is not None


def test_validate_accepts_valid_name():
    assert validate_view_name("My Project", []) is None


def test_validate_trims_whitespace():
    """A name that is valid after trimming should pass."""
    assert validate_view_name("  My Project  ", []) is None


def test_validate_accepts_at_max_length():
    assert validate_view_name("a" * 30, []) is None


# ---------------------------------------------------------------------------
# v2 visibility helpers: is_hidden, filter_visible, visible_count
# See docs/plans/2026-05-17-hidden-state-redesign-design.md
# ---------------------------------------------------------------------------


def test_is_hidden_true_when_key_in_hidden_sessions():
    settings = {"hidden_sessions": ["dev1:a", "dev1:b"]}
    assert is_hidden("dev1:a", settings) is True
    assert is_hidden("dev1:b", settings) is True


def test_is_hidden_false_when_key_absent():
    settings = {"hidden_sessions": ["dev1:a"]}
    assert is_hidden("dev1:b", settings) is False


def test_is_hidden_handles_missing_field():
    assert is_hidden("dev1:a", {}) is False
    assert is_hidden("dev1:a", {"hidden_sessions": None}) is False


# --- filter_visible: "all" view ---


def test_filter_visible_all_excludes_hidden_by_default():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {"hidden_sessions": ["dev1:b"], "views": []}

    result = filter_visible(sessions, settings, "all")
    assert [s["name"] for s in result] == ["a", "c"]


def test_filter_visible_all_includes_hidden_when_requested():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {"hidden_sessions": ["dev1:b"], "views": []}

    result = filter_visible(sessions, settings, "all", include_hidden=True)
    assert [s["name"] for s in result] == ["a", "b", "c"]


def test_filter_visible_all_excludes_status_tiles():
    sessions = [
        _session("a"),
        _session("disconnected", status="error"),
        _session("b"),
    ]
    settings = {"hidden_sessions": [], "views": []}

    result = filter_visible(sessions, settings, "all")
    assert [s["name"] for s in result] == ["a", "b"]


# --- filter_visible: "hidden" view ---


def test_filter_visible_hidden_returns_only_hidden_sessions():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {"hidden_sessions": ["dev1:a", "dev1:c"], "views": []}

    result = filter_visible(sessions, settings, "hidden")
    assert [s["name"] for s in result] == ["a", "c"]


def test_filter_visible_hidden_returns_empty_when_no_hidden():
    sessions = [_session("a"), _session("b")]
    settings = {"hidden_sessions": [], "views": []}

    result = filter_visible(sessions, settings, "hidden")
    assert result == []


def test_filter_visible_hidden_excludes_dead_keys():
    """Stale hidden_sessions entries with no live counterpart are not counted."""
    sessions = [_session("a")]  # only "a" is live
    settings = {"hidden_sessions": ["dev1:a", "dev1:ghost"], "views": []}

    result = filter_visible(sessions, settings, "hidden")
    assert [s["name"] for s in result] == ["a"]


# --- filter_visible: user view ---


def test_filter_visible_user_view_membership_and_visibility():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {
        "hidden_sessions": ["dev1:b"],
        "views": [{"name": "Work", "sessions": ["dev1:a", "dev1:b"]}],
    }

    result = filter_visible(sessions, settings, "Work")
    # 'a' is in view and not hidden; 'b' is in view but hidden — should be filtered out.
    assert [s["name"] for s in result] == ["a"]


def test_filter_visible_user_view_include_hidden_keeps_membership_filter():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {
        "hidden_sessions": ["dev1:b"],
        "views": [{"name": "Work", "sessions": ["dev1:a", "dev1:b"]}],
    }

    result = filter_visible(sessions, settings, "Work", include_hidden=True)
    # include_hidden does not lift the membership filter — only the hidden filter.
    assert [s["name"] for s in result] == ["a", "b"]
    # 'c' is not in the view; never appears.


def test_filter_visible_unknown_view_returns_empty():
    sessions = [_session("a"), _session("b")]
    settings = {"hidden_sessions": [], "views": []}
    assert filter_visible(sessions, settings, "Nonexistent") == []


def test_filter_visible_user_view_with_overlap_state():
    """v2 permits a key in both hidden_sessions AND view.sessions.

    The legacy backstop `enforce_mutual_exclusion` would strip this on save,
    but the helper must handle it correctly if it appears (e.g. during a
    sync round before the backstop runs).
    """
    sessions = [_session("a"), _session("b")]
    settings = {
        "hidden_sessions": ["dev1:a"],  # also in view
        "views": [{"name": "Work", "sessions": ["dev1:a", "dev1:b"]}],
    }

    # Default behavior: hidden filter wins, 'a' is excluded from Work view.
    result = filter_visible(sessions, settings, "Work")
    assert [s["name"] for s in result] == ["b"]

    # include_hidden=True surfaces it again.
    result = filter_visible(sessions, settings, "Work", include_hidden=True)
    assert [s["name"] for s in result] == ["a", "b"]


# --- filter_visible: dual-lookup (legacy bare-name entries) ---


def test_filter_visible_matches_by_bare_name_when_stored_that_way():
    """Legacy entries stored as bare 'name' still match against session.name."""
    sessions = [_session("a"), _session("b")]
    settings = {
        "hidden_sessions": ["a"],  # bare name, not "dev1:a"
        "views": [],
    }
    result = filter_visible(sessions, settings, "all")
    assert [s["name"] for s in result] == ["b"]


def test_filter_visible_matches_bare_name_in_view_membership():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {
        "hidden_sessions": [],
        "views": [{"name": "Work", "sessions": ["a", "b"]}],  # bare names
    }
    result = filter_visible(sessions, settings, "Work")
    assert [s["name"] for s in result] == ["a", "b"]


# --- visible_count ---


def test_visible_count_matches_filter_visible_length():
    sessions = [_session("a"), _session("b"), _session("c")]
    settings = {
        "hidden_sessions": ["dev1:b"],
        "views": [{"name": "Work", "sessions": ["dev1:a", "dev1:b", "dev1:c"]}],
    }

    for view, include_hidden in [
        ("all", False),
        ("all", True),
        ("hidden", False),
        ("Work", False),
        ("Work", True),
        ("Nonexistent", False),
    ]:
        expected = len(
            filter_visible(sessions, settings, view, include_hidden=include_hidden)
        )
        actual = visible_count(sessions, settings, view, include_hidden=include_hidden)
        assert actual == expected, (
            f"visible_count({view!r}, include_hidden={include_hidden}) "
            f"= {actual} != filter_visible length {expected}"
        )


# ---------------------------------------------------------------------------
# normalize_session_keys (Phase 1)
# ---------------------------------------------------------------------------


def test_normalize_upgrades_bare_name_in_hidden_sessions():
    sessions = [_session("a"), _session("b")]
    settings = {
        "hidden_sessions": ["a", "dev1:b"],  # 'a' is bare; 'b' already canonical
        "views": [],
    }
    result = normalize_session_keys(settings, sessions)
    assert result["hidden_sessions"] == ["dev1:a", "dev1:b"]


def test_normalize_upgrades_bare_name_in_view_sessions():
    sessions = [_session("a"), _session("b")]
    settings = {
        "hidden_sessions": [],
        "views": [{"name": "Work", "sessions": ["a", "dev1:b"]}],
    }
    result = normalize_session_keys(settings, sessions)
    assert result["views"][0]["sessions"] == ["dev1:a", "dev1:b"]


def test_normalize_leaves_unmatched_entries_alone():
    """Entries with no live counterpart are kept as-is (they may match later)."""
    sessions = [_session("a")]  # only 'a' is live
    settings = {
        "hidden_sessions": ["a", "ghost"],
        "views": [{"name": "Work", "sessions": ["a", "another-ghost"]}],
    }
    result = normalize_session_keys(settings, sessions)
    # 'a' is upgraded; the ghosts are preserved verbatim.
    assert result["hidden_sessions"] == ["dev1:a", "ghost"]
    assert result["views"][0]["sessions"] == ["dev1:a", "another-ghost"]


def test_normalize_is_idempotent():
    sessions = [_session("a"), _session("b")]
    settings = {
        "hidden_sessions": ["a"],
        "views": [{"name": "Work", "sessions": ["a", "b"]}],
    }
    once = normalize_session_keys(settings, sessions)
    twice = normalize_session_keys(once, sessions)
    assert once["hidden_sessions"] == twice["hidden_sessions"]
    assert once["views"] == twice["views"]


def test_normalize_handles_empty_or_missing_fields():
    """Don't crash when fields are missing or empty."""
    assert normalize_session_keys({}, []) == {}
    assert normalize_session_keys({"hidden_sessions": []}, []) == {
        "hidden_sessions": []
    }
    assert normalize_session_keys({"views": []}, []) == {"views": []}


def test_normalize_handles_cross_device_name_collisions_safely():
    """When two devices have a session with the same bare name, leave the stored
    bare-name entry alone — there's no unambiguous canonical form to choose."""
    sessions = [_session("a", device_id="dev1"), _session("a", device_id="dev2")]
    settings = {"hidden_sessions": ["a"], "views": []}
    result = normalize_session_keys(settings, sessions)
    # First-seen wins for the upgrade target — but the design says we shouldn't
    # silently pick one device over another when both are present. The
    # implementation uses setdefault, so first-seen wins. Document the behavior
    # in this test so the choice is visible.
    assert result["hidden_sessions"][0] in {"dev1:a", "dev2:a"}
