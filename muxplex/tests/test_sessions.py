"""
Tests for coordinator/sessions.py — tmux session enumeration and helpers.
All 6 acceptance-criteria tests are defined here.
"""

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import muxplex.sessions as sessions_mod
from muxplex.sessions import (
    capture_pane,
    enumerate_sessions,
    get_snapshots,
    get_session_list,
    run_tmux,
    snapshot_all,
    update_session_cache,
    validate_session_name,
)


# ---------------------------------------------------------------------------
# Helpers for mocking asyncio.create_subprocess_exec
# ---------------------------------------------------------------------------


def _make_mock_process(stdout: str, stderr: str = "", returncode: int = 0):
    """Return a mock process whose communicate() returns encoded strings."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


@pytest.fixture
def mock_subprocess():
    """Fixture factory: returns a context-manager patch for asyncio.create_subprocess_exec.

    Usage::

        with mock_subprocess(stdout="...") as mock_create:
            await some_function()
    """

    def _factory(stdout: str = "", stderr: str = "", returncode: int = 0):
        proc = _make_mock_process(stdout, stderr, returncode)
        return patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc))

    return _factory


# ---------------------------------------------------------------------------
# run_tmux tests
# ---------------------------------------------------------------------------


async def test_run_tmux_calls_correct_command(mock_subprocess):
    """run_tmux('list-sessions', '-F', '#{session_name}') must call tmux
    with exactly those positional arguments via asyncio.create_subprocess_exec."""
    with mock_subprocess("session1\nsession2\n") as mock_create:
        await run_tmux("list-sessions", "-F", "#{session_name}")

    # First positional arg must be 'tmux'; rest must be the args we passed.
    call_args = mock_create.call_args[0]
    assert call_args[0] == "tmux"
    assert call_args[1] == "list-sessions"
    assert call_args[2] == "-F"
    assert call_args[3] == "#{session_name}"


async def test_run_tmux_raises_on_nonzero_exit(mock_subprocess):
    """run_tmux() must raise RuntimeError when the subprocess exits non-zero."""
    with mock_subprocess(
        stdout="", stderr="no server running on /tmp/tmux-1000/default", returncode=1
    ):
        with pytest.raises(RuntimeError, match="no server running"):
            await run_tmux("list-sessions", "-F", "#{session_name}")


# ---------------------------------------------------------------------------
# enumerate_sessions tests
# ---------------------------------------------------------------------------


async def test_enumerate_sessions_parses_newline_output(mock_subprocess):
    """enumerate_sessions() splits newline-separated output into a list of names."""
    with mock_subprocess("alpha\nbeta\ngamma\n"):
        result = await enumerate_sessions()

    assert result == ["alpha", "beta", "gamma"]


async def test_enumerate_sessions_returns_empty_list_when_no_sessions(mock_subprocess):
    """enumerate_sessions() returns [] when tmux output is empty."""
    with mock_subprocess(""):
        result = await enumerate_sessions()

    assert result == []


async def test_enumerate_sessions_strips_whitespace(mock_subprocess):
    """enumerate_sessions() strips leading/trailing whitespace from each name."""
    with mock_subprocess("  session1  \n  session2  \n"):
        result = await enumerate_sessions()

    assert result == ["session1", "session2"]


async def test_enumerate_sessions_handles_tmux_error(mock_subprocess):
    """enumerate_sessions() returns [] when run_tmux raises RuntimeError
    (e.g. tmux server not running)."""
    with mock_subprocess(stdout="", stderr="no server running", returncode=1):
        result = await enumerate_sessions()

    assert result == []


# ---------------------------------------------------------------------------
# capture_pane tests
# ---------------------------------------------------------------------------


async def test_capture_pane_returns_output(mock_subprocess):
    """capture_pane() returns the text output from tmux capture-pane."""
    with mock_subprocess("line1\nline2\nline3\n"):
        result = await capture_pane("my-session")

    assert result == "line1\nline2\nline3\n"


async def test_capture_pane_returns_empty_string_on_error(mock_subprocess):
    """capture_pane() returns '' when tmux exits with an error."""
    with mock_subprocess(
        stdout="", stderr="can't find session my-session", returncode=1
    ):
        result = await capture_pane("my-session")

    assert result == ""


async def test_capture_pane_calls_correct_tmux_args(mock_subprocess):
    """capture_pane() calls tmux with: capture-pane -e -p -t <name> -S -<lines>.

    Uses -e to preserve ANSI escape sequences for color rendering.
    Uses -S -N (start N lines from bottom) to limit output.
    Does NOT pass -l (invalid in tmux 3.4).
    """
    with mock_subprocess("output text\n") as mock_create:
        await capture_pane("target-session", lines=50)

    call_args = mock_create.call_args[0]
    assert call_args[0] == "tmux"
    assert call_args[1] == "capture-pane"
    assert call_args[2] == "-e"
    assert call_args[3] == "-p"
    assert call_args[4] == "-t"
    assert call_args[5] == "target-session"
    assert call_args[6] == "-S"
    assert call_args[7] == "-50"
    assert len(call_args) == 8, "-e must be present; no other extra args"


# ---------------------------------------------------------------------------
# snapshot_all tests
# ---------------------------------------------------------------------------


async def test_snapshot_all_returns_dict_keyed_by_name():
    """snapshot_all() returns a dict mapping each session name to its pane output."""

    async def mock_capture(name, lines=30):
        return f"output-for-{name}"

    with patch("muxplex.sessions.capture_pane", side_effect=mock_capture):
        result = await snapshot_all(["alpha", "beta", "gamma"])

    assert result == {
        "alpha": "output-for-alpha",
        "beta": "output-for-beta",
        "gamma": "output-for-gamma",
    }


async def test_snapshot_all_returns_empty_dict_for_empty_input():
    """snapshot_all([]) returns an empty dict without calling capture_pane."""
    with patch("muxplex.sessions.capture_pane", new=AsyncMock()) as mock_capture:
        result = await snapshot_all([])

    assert result == {}
    mock_capture.assert_not_called()


async def test_snapshot_all_returns_empty_string_on_individual_failure():
    """snapshot_all() maps '' for a failing session while others still succeed."""

    async def mock_capture(name, lines=30):
        if name == "bad-session":
            raise RuntimeError("pane not found")
        return f"output-for-{name}"

    with patch("muxplex.sessions.capture_pane", side_effect=mock_capture):
        result = await snapshot_all(["session-a", "bad-session", "session-b"])

    assert result == {
        "session-a": "output-for-session-a",
        "bad-session": "",
        "session-b": "output-for-session-b",
    }


# ---------------------------------------------------------------------------
# update_session_cache tests
# ---------------------------------------------------------------------------


def test_capture_pane_uses_escape_flag():
    """capture-pane must include -e for ANSI color preservation."""
    import inspect
    from muxplex.sessions import capture_pane

    source = inspect.getsource(capture_pane)
    assert '"-e"' in source, "capture_pane must pass -e flag to preserve ANSI escapes"


def test_update_session_cache_populates_snapshots():
    """update_session_cache(names, snapshots) must replace _snapshots with provided dict.

    This is the RED test for Critical Issue #1: previously, update_session_cache
    only accepted names and never received the snapshots dict, so _snapshots
    stayed empty forever.
    """
    # Reset module state to simulate a fresh start
    sessions_mod._snapshots = {}
    sessions_mod._session_list = []

    update_session_cache(
        ["sess1", "sess2"], {"sess1": "line1\nline2", "sess2": "hello"}
    )

    result = get_snapshots()
    assert result == {"sess1": "line1\nline2", "sess2": "hello"}


def test_update_session_cache_updates_session_list():
    """update_session_cache() must also replace _session_list with the given names."""
    sessions_mod._snapshots = {}
    sessions_mod._session_list = ["old-session"]

    update_session_cache(["alpha", "beta"], {"alpha": "a", "beta": "b"})

    assert get_session_list() == ["alpha", "beta"]


def test_update_session_cache_empty_names_clears_caches():
    """update_session_cache([], {}) clears both caches."""
    sessions_mod._snapshots = {"stale": "text"}
    sessions_mod._session_list = ["stale"]

    update_session_cache([], {})

    assert get_session_list() == []
    assert get_snapshots() == {}


# ---------------------------------------------------------------------------
# list_session_paths (universal search metadata)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_session_paths_keeps_only_active_window_and_pane(mock_subprocess):
    out = (
        "work\t1\t1\t/home/u/projects/work\n"
        "work\t1\t0\t/home/u/elsewhere\n"      # inactive pane
        "work\t0\t1\t/home/u/other-window\n"   # inactive window
        "play\t1\t1\t/srv/play\n"
    )
    with mock_subprocess(stdout=out):
        paths = await sessions_mod.list_session_paths()
    assert paths == {"work": "/home/u/projects/work", "play": "/srv/play"}


@pytest.mark.asyncio
async def test_list_session_paths_survives_tabs_in_path(mock_subprocess):
    out = "odd\t1\t1\t/home/u/dir\twith\ttabs\n"
    with mock_subprocess(stdout=out):
        paths = await sessions_mod.list_session_paths()
    assert paths == {"odd": "/home/u/dir\twith\ttabs"}


@pytest.mark.asyncio
async def test_list_session_paths_returns_empty_when_tmux_unavailable(mock_subprocess):
    with mock_subprocess(stdout="", stderr="no server running", returncode=1):
        paths = await sessions_mod.list_session_paths()
    assert paths == {}


@pytest.mark.asyncio
async def test_list_session_paths_skips_malformed_lines(mock_subprocess):
    out = "broken-line-without-tabs\nok\t1\t1\t/srv/ok\n"
    with mock_subprocess(stdout=out):
        paths = await sessions_mod.list_session_paths()
    assert paths == {"ok": "/srv/ok"}


# ---------------------------------------------------------------------------
# resolve_git_repo (universal search metadata)
# ---------------------------------------------------------------------------


def test_resolve_git_repo_finds_repo_root(tmp_path):
    sessions_mod._git_repo_cache.clear()
    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert sessions_mod.resolve_git_repo(str(nested)) == "myrepo"
    assert sessions_mod.resolve_git_repo(str(repo)) == "myrepo"


def test_resolve_git_repo_worktree_resolves_to_main_repo(tmp_path):
    """A linked worktree groups under the *main* repo, not its own dir name."""
    sessions_mod._git_repo_cache.clear()
    repo = tmp_path / "myrepo"
    main_gitdir = repo / ".git"
    wt_gitdir = main_gitdir / "worktrees" / "wt1"
    wt_gitdir.mkdir(parents=True)
    # git writes a commondir pointer relative to the worktree's gitdir
    (wt_gitdir / "commondir").write_text("../..\n")
    # worktrees live inside the repo at ./.worktrees/<name>
    wt = repo / ".worktrees" / "wt1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {wt_gitdir}\n")
    assert sessions_mod.resolve_git_repo(str(wt)) == "myrepo"
    # a nested cwd inside the worktree resolves the same way
    nested = wt / "src" / "deep"
    nested.mkdir(parents=True)
    assert sessions_mod.resolve_git_repo(str(nested)) == "myrepo"


def test_resolve_git_repo_worktree_without_commondir(tmp_path):
    """Falls back to stripping 'worktrees/<name>' when no commondir file."""
    sessions_mod._git_repo_cache.clear()
    wt = tmp_path / "myrepo-wt1"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /home/u/myrepo/.git/worktrees/wt1\n")
    assert sessions_mod.resolve_git_repo(str(wt)) == "myrepo"


def test_resolve_git_repo_worktree_unparseable_falls_back(tmp_path):
    """An unreadable/empty .git file falls back to the worktree dir name."""
    sessions_mod._git_repo_cache.clear()
    wt = tmp_path / "lonely-wt"
    wt.mkdir()
    (wt / ".git").write_text("not a gitdir pointer\n")
    assert sessions_mod.resolve_git_repo(str(wt)) == "lonely-wt"


def test_resolve_git_repo_returns_none_outside_repo(tmp_path):
    sessions_mod._git_repo_cache.clear()
    plain = tmp_path / "no-repo-here"
    plain.mkdir()
    assert sessions_mod.resolve_git_repo(str(plain)) is None
    assert sessions_mod.resolve_git_repo("") is None


def test_resolve_git_repo_memoizes(tmp_path):
    sessions_mod._git_repo_cache.clear()
    repo = tmp_path / "cached"
    (repo / ".git").mkdir(parents=True)
    assert sessions_mod.resolve_git_repo(str(repo)) == "cached"
    # Remove .git — the memoized answer must persist (no re-walk)
    (repo / ".git").rmdir()
    assert sessions_mod.resolve_git_repo(str(repo)) == "cached"


def test_session_paths_cache_roundtrip():
    sessions_mod.update_session_paths({"a": "/x"})
    assert sessions_mod.get_session_paths() == {"a": "/x"}
    got = sessions_mod.get_session_paths()
    got["b"] = "/y"  # mutating the copy must not affect the cache
    assert sessions_mod.get_session_paths() == {"a": "/x"}
    sessions_mod.update_session_paths({})


# ---------------------------------------------------------------------------
# validate_session_name (v0.9 rename)
# ---------------------------------------------------------------------------


def test_validate_session_name_accepts_plain_name():
    assert validate_session_name("my-project") is None
    assert validate_session_name("  trimmed  ") is None


def test_validate_session_name_rejects_empty_and_whitespace():
    assert validate_session_name("") is not None
    assert validate_session_name("   ") is not None


def test_validate_session_name_rejects_tmux_separators():
    assert validate_session_name("a.b") is not None
    assert validate_session_name("a:b") is not None


def test_validate_session_name_rejects_dir_prefix():
    assert validate_session_name("dir:foo") is not None
    assert validate_session_name("DIR:foo") is not None


def test_validate_session_name_rejects_control_chars():
    assert validate_session_name("a\tb") is not None
    assert validate_session_name("a\nb") is not None


def test_validate_session_name_rejects_duplicate():
    assert validate_session_name("taken", existing=["taken", "other"]) is not None
    assert validate_session_name("fresh", existing=["taken", "other"]) is None


# ---------------------------------------------------------------------------
# snapshot_all bounded concurrency (plan item 1.2a)
# ---------------------------------------------------------------------------


class _ConcurrencyProbe:
    """capture_pane stand-in that records concurrent in-flight calls."""

    def __init__(self, hold: bool = True):
        self.current = 0
        self.max_seen = 0
        self.calls: list[str] = []
        self._hold = hold

    async def __call__(self, name, lines=30):
        self.calls.append(name)
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)
        try:
            if self._hold:
                # Yield enough times that every task that *can* start does so
                # before any finishes — otherwise a serial-looking schedule
                # would masquerade as bounded concurrency.
                for _ in range(5):
                    await asyncio.sleep(0)
            return f"output-for-{name}"
        finally:
            self.current -= 1


async def test_snapshot_all_bounds_concurrency_at_large_n():
    """N >> limit: exactly N captures, never more than the limit in flight."""
    limit = sessions_mod._SNAPSHOT_CONCURRENCY
    names = [f"s{i}" for i in range(limit * 4)]
    probe = _ConcurrencyProbe()

    with patch("muxplex.sessions.capture_pane", new=probe):
        result = await snapshot_all(names)

    assert len(result) == len(names)
    assert result["s0"] == "output-for-s0"
    # Spawn COUNT is unchanged — one capture per session, no retries, no skips.
    assert len(probe.calls) == len(names)
    assert sorted(probe.calls) == sorted(names)
    assert probe.max_seen <= limit, f"observed {probe.max_seen} in flight"
    # And it really is concurrent up to the bound, not serialized.
    assert probe.max_seen == limit


async def test_snapshot_all_small_n_is_fully_concurrent():
    """N below the limit behaves exactly as the old unbounded gather did."""
    limit = sessions_mod._SNAPSHOT_CONCURRENCY
    names = [f"s{i}" for i in range(limit - 1)]
    probe = _ConcurrencyProbe()

    with patch("muxplex.sessions.capture_pane", new=probe):
        result = await snapshot_all(names)

    assert len(result) == len(names)
    assert len(probe.calls) == len(names)
    assert probe.max_seen == len(names), "small N must all be in flight at once"


async def test_snapshot_all_failure_isolated_under_bounded_concurrency():
    """One failing capture still maps to '' and does not abort the batch."""
    limit = sessions_mod._SNAPSHOT_CONCURRENCY
    names = [f"s{i}" for i in range(limit * 2)]
    bad = names[limit + 1]
    seen: list[str] = []

    async def mock_capture(name, lines=30):
        seen.append(name)
        await asyncio.sleep(0)
        if name == bad:
            raise RuntimeError("pane not found")
        return f"output-for-{name}"

    with patch("muxplex.sessions.capture_pane", side_effect=mock_capture):
        result = await snapshot_all(names)

    assert result[bad] == ""
    assert all(result[n] == f"output-for-{n}" for n in names if n != bad)
    assert len(seen) == len(names)


def test_snapshot_all_semaphore_is_not_bound_to_one_event_loop():
    """The limiter must be per-call — module-level would bind to one loop."""
    probe = _ConcurrencyProbe(hold=False)

    async def run():
        with patch("muxplex.sessions.capture_pane", new=probe):
            return await snapshot_all(["a", "b"])

    # Two *separate* event loops, as the wider test suite creates.
    first = asyncio.run(run())
    second = asyncio.run(run())
    assert first == second == {"a": "output-for-a", "b": "output-for-b"}


# ---------------------------------------------------------------------------
# Cache accessor copy semantics (plan item 2.5a)
# ---------------------------------------------------------------------------


def test_get_snapshots_returns_a_copy_not_the_module_global():
    """Mutating the returned dict must not corrupt the module cache."""
    sessions_mod._snapshots = {"a": "text-a"}

    got = get_snapshots()
    assert got is not sessions_mod._snapshots
    got["b"] = "injected"
    del got["a"]

    assert sessions_mod._snapshots == {"a": "text-a"}


def test_get_snapshots_copy_is_shallow():
    """The copy shares its (immutable str) values — no deep copy of pane text."""
    text = "a" * 64
    sessions_mod._snapshots = {"a": text}

    got = get_snapshots()

    assert got["a"] is text, "pane text must not be duplicated"


def test_update_session_cache_rebinds_rather_than_mutating():
    """A held reference stays a consistent view of its own poll cycle."""
    sessions_mod._snapshots = {}
    update_session_cache(["a"], {"a": "cycle-1"})
    held = get_snapshots()
    before = sessions_mod._snapshots

    update_session_cache(["a"], {"a": "cycle-2"})

    assert held == {"a": "cycle-1"}, "old reference must not see the new cycle"
    assert before == {"a": "cycle-1"}, "update must rebind, not mutate in place"
    assert get_snapshots() == {"a": "cycle-2"}


def test_get_session_list_and_paths_return_copies():
    """The other two accessors copy for the same reason (documented, unchanged)."""
    sessions_mod._session_list = ["a"]
    sessions_mod._session_paths = {"a": "/x"}

    names = get_session_list()
    paths = sessions_mod.get_session_paths()
    names.append("injected")
    paths["b"] = "/y"

    assert sessions_mod._session_list == ["a"]
    assert sessions_mod._session_paths == {"a": "/x"}
