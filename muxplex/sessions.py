"""
tmux session enumeration and snapshot helpers for the tmux-web muxplex.

In-memory cache:
    _session_list  — most-recently-enumerated list of session names.
    _snapshots     — most-recently-captured pane text, keyed by session name.
    _session_paths — active-pane cwd per session, keyed by session name.
    _session_times — (last_attached, created) epoch seconds per session name.

Public API:
    get_session_list()                    → list[str]
    get_snapshots()                       → dict[str, str]
    get_session_paths()                   → dict[str, str]
    get_session_times()                   → dict[str, tuple[int | None, int | None]]
    update_session_cache(names, snapshots) → None
    update_session_paths(paths)           → None
    run_tmux(*args)                       → str   (raises RuntimeError on nonzero exit)
    enumerate_sessions()                  → list[str]
    capture_pane(name, lines)             → str
    snapshot_all(names, change_keys)      → dict[str, str]
    list_session_panes()                  → (dict[str, str], dict[str, str])
    list_session_paths()                  → dict[str, str]
    resolve_git_repo(cwd)                 → str | None
"""

import asyncio
import logging
import os

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_session_list: list[str] = []
_snapshots: dict[str, str] = {}
_session_paths: dict[str, str] = {}
# name -> (last_attached, created), epoch seconds or None when unknown/never.
# Published by enumerate_sessions(); REBOUND wholesale, never mutated (see
# get_session_times).
_session_times: dict[str, tuple[int | None, int | None]] = {}


def get_session_list() -> list[str]:
    """Return a copy of the cached session name list."""
    return list(_session_list)


def get_snapshots() -> dict[str, str]:
    """Return a SHALLOW copy of the cached pane-snapshot dict.

    Deliberately shallow (and cheap): the values are immutable ``str`` pane
    text, so only the outer dict needs protecting from caller mutation. This
    copies N pointers, NOT the N × 30 lines of ANSI text — do not "optimize" it
    into a bare ``return _snapshots``, which would hand every HTTP handler a
    live reference to the module global.

    Callers may safely hold the returned dict across a poll cycle:
    ``update_session_cache`` REBINDS ``_snapshots`` rather than mutating it, so
    a held reference stays a consistent view of its own cycle.
    """
    return dict(_snapshots)


def update_session_cache(names: list[str], snapshots: dict[str, str]) -> None:
    """Replace the in-memory caches with fresh data.

    Sets _session_list to *names* and _snapshots to the provided *snapshots* dict.
    Callers must pass the return value of snapshot_all() as *snapshots*.
    """
    global _session_list, _snapshots
    _session_list = list(names)
    _snapshots = snapshots


def get_session_paths() -> dict[str, str]:
    """Return a copy of the cached session→cwd dict."""
    return dict(_session_paths)


def get_session_times() -> dict[str, tuple[int | None, int | None]]:
    """Return the session→(last_attached, created) map from the last enumeration.

    Returns the live reference, NOT a copy: callers read it once per request
    and must treat it as read-only.  That is safe because enumerate_sessions()
    REBINDS ``_session_times`` with a complete new dict on every successful
    enumeration and never mutates the published one, so a held reference stays
    a consistent snapshot of its own enumeration.
    """
    return _session_times


def update_session_paths(paths: dict[str, str]) -> None:
    """Replace the cached session→cwd dict with fresh data.

    Callers must pass the return value of list_session_paths().
    """
    global _session_paths
    _session_paths = dict(paths)


# ---------------------------------------------------------------------------
# Session-name validation
# ---------------------------------------------------------------------------

# tmux uses '.' and ':' as separators in target specs (session:window.pane), so
# a session name containing either can't be reliably addressed. 'dir:' would be
# caught by the ':' rule, but we reject it explicitly for a clearer message
# (it is the reserved auto-view namespace — see views.AUTO_VIEW_PREFIX).
_AUTO_VIEW_PREFIX = "dir:"


def validate_session_name(name: str, existing: list[str] | None = None) -> str | None:
    """Validate a tmux session name. Return an error message, or None if valid.

    Rules: non-empty after trimming; no '.' or ':' (tmux target separators); no
    control characters; not the reserved 'dir:' auto-view prefix; and unique
    among *existing* session names when provided.
    """
    stripped = (name or "").strip()
    if not stripped:
        return "Session name cannot be empty"
    if stripped.lower().startswith(_AUTO_VIEW_PREFIX):
        return f"Names starting with '{_AUTO_VIEW_PREFIX}' are reserved"
    if "." in stripped or ":" in stripped:
        return "Session name cannot contain '.' or ':'"
    if any(ord(c) < 0x20 for c in stripped):
        return "Session name cannot contain control characters"
    if existing and stripped in set(existing):
        return f"A session named '{stripped}' already exists"
    return None


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


async def run_tmux(*args: str) -> str:
    """Run `tmux <args>` in a subprocess and return stdout as a string.

    Raises:
        RuntimeError: If the process exits with a nonzero return code.
                      The error message contains the decoded stderr output.
    """
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr_bytes.decode("utf-8", errors="replace"))
    return stdout_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Session enumeration
# ---------------------------------------------------------------------------


# `tmux list-sessions` format: per-session recency (header strip MRU ordering,
# issue #24) carried on the SAME single call that enumerates names, so the poll
# cycle stays O(1) in tmux spawns.
#
# - The name is LAST and split off with maxsplit, so a name containing a TAB
#   survives intact.
# - Both time fields go through a tmux conditional so they are NEVER empty: a
#   session that was never attached prints an EMPTY #{session_last_attached}.
#   An empty leading field is how a line like "<TAB>1790…<TAB>name" used to be one
#   .strip() away from collapsing into a single bogus "name" — and a name that
#   goes missing from enumeration gets its ttyd reaped by the poll cycle.
_LIST_SESSIONS_FORMAT = (
    "#{?session_last_attached,#{session_last_attached},0}\t"
    "#{?session_created,#{session_created},0}\t"
    "#{session_name}"
)


def _epoch_or_none(field: str) -> int | None:
    """Parse a tmux epoch-seconds field; 0, empty or non-numeric → None."""
    field = field.strip()
    if not field.isdigit():
        return None
    value = int(field)
    return value or None


def _parse_session_line(line: str) -> tuple[str, int | None, int | None] | None:
    """Parse one `list-sessions` line into (name, last_attached, created).

    SPLIT FIRST, TRIM AFTER — never strip the raw line, or an empty leading
    field is eaten and the remaining fields shift into the name.

    - two or more TABs → ``last TAB created TAB name`` (name is everything after
      the second TAB); a non-numeric time becomes None, the name is still kept;
    - no TAB → a bare name with unknown times (the pre-#24 shape, and what the
      test suite's subprocess mocks emit);
    - exactly one TAB → unreachable with ``_LIST_SESSIONS_FORMAT``; logged and
      kept whole as the name — exactly what the pre-#24 parser did with every
      line, so this is never WORSE than before.

    Returns None for a blank line.
    """
    raw = line.rstrip("\r")
    tabs = raw.count("\t")
    if tabs >= 2:
        last, created, name = raw.split("\t", 2)
        name = name.strip()
        if not name:
            return None
        return name, _epoch_or_none(last), _epoch_or_none(created)
    if tabs == 1:
        _log.warning("unexpected list-sessions line shape (1 TAB): %r", raw)
    name = raw.strip()
    if not name:
        return None
    return name, None, None


async def enumerate_sessions() -> list[str]:
    """Return the list of currently running tmux session names.

    ONE ``tmux list-sessions`` call.  Besides the names it also publishes each
    session's (last_attached, created) times for ``get_session_times()`` — the
    map is REBOUND wholesale on every successful call, so a deleted or
    recreated name can never keep stale times.

    Returns [] if tmux is not running (RuntimeError from run_tmux); the
    published times are left untouched in that case (every name is gone
    anyway).
    """
    global _session_times
    try:
        output = await run_tmux("list-sessions", "-F", _LIST_SESSIONS_FORMAT)
    except (RuntimeError, FileNotFoundError):
        return []

    names: list[str] = []
    times: dict[str, tuple[int | None, int | None]] = {}
    for line in output.splitlines():
        parsed = _parse_session_line(line)
        if parsed is None:
            continue
        name, last_attached, created = parsed
        names.append(name)
        times[name] = (last_attached, created)
    _session_times = times
    return names


# ---------------------------------------------------------------------------
# Pane capture
# ---------------------------------------------------------------------------


async def capture_pane(session_name: str, lines: int = 30) -> str:
    """Capture the last *lines* lines of output from *session_name*.

    Returns the captured text, or '' on any error.
    """
    try:
        return await run_tmux(
            "capture-pane",
            "-e",  # preserve ANSI escape sequences for color rendering
            "-p",
            "-t",
            session_name,
            "-S",
            f"-{lines}",
        )
    except RuntimeError:
        return ""


# Format string for the ONE batched `list-panes -a` call.
#
# CRITICAL ORDERING CONSTRAINT: `#{pane_current_path}` MUST stay LAST, and
# every field added here must go BEFORE it.  The parser splits with a maxsplit
# so that the path — the only field that can legitimately contain a TAB — is
# the whole remainder of the line.  Adding a field after the path, or forgetting
# to bump `_PANE_FIELD_MAXSPLIT`, silently corrupts cwd parsing for tabbed
# paths (and therefore auto-view grouping and universal search).
_PANE_FORMAT = (
    "#{session_name}\t"
    "#{window_active}\t"
    "#{pane_active}\t"
    "#{window_activity}\t"
    "#{pane_id}\t"
    "#{pane_width}\t"
    "#{pane_height}\t"
    "#{pane_current_path}"
)

# Number of TAB-separated fields in _PANE_FORMAT.
_PANE_FIELD_COUNT = 8
# maxsplit for `str.split("\t", n)` — one less than the field count, so the
# trailing path keeps any tabs it contains.
_PANE_FIELD_MAXSPLIT = _PANE_FIELD_COUNT - 1


async def list_session_panes() -> tuple[dict[str, str], dict[str, str]]:
    """Return ({session: cwd}, {session: change-key}) for all sessions.

    ONE subprocess per call — the same single `tmux list-panes -a` that has
    always fed the cwd map, now also carrying the fields the snapshot path
    needs to tell "this pane provably has not changed" from "capture it".
    Adding them costs ZERO extra spawns.

    Only rows where both the window and the pane are active are kept (the
    session's "current" pane — the same pane `capture_pane` targets).

    The CHANGE KEY is composite, not just a timestamp::

        window_activity | pane_id | pane_width | pane_height

    * ``window_activity`` — time of last activity in the active window; this
      is the actual "new output" signal.  (NOT ``window_activity_flag``, which
      is the alert flag and is gated on ``monitor-activity``; not
      ``session_activity``, which tmux also bumps on client attach.)
    * ``pane_id`` — `capture_pane` targets ``-t <session>``, which tmux
      resolves to the session's CURRENT window's ACTIVE pane.  Switching the
      active window or pane changes what a capture returns with no new output
      at all, and does not move ``window_activity``.
    * ``pane_width`` / ``pane_height`` — a resize reflows the visible content
      with no new output.

    A session whose row is missing or unparseable is simply absent from the
    change-key map, which the snapshot path treats as UNKNOWN and therefore
    captures (fail toward capturing, never toward staleness).

    Returns ({}, {}) when tmux is unavailable — again forcing a full capture.

    SIDE EFFECT: publishes the change-key map for `snapshot_all` to consume
    (see `_publish_pane_change_keys`).  That handoff is a one-shot freshness
    handshake, not a plain cache — `snapshot_all` uses the keys only if this
    call produced them since the last snapshot.
    """
    try:
        output = await run_tmux("list-panes", "-a", "-F", _PANE_FORMAT)
    except (RuntimeError, FileNotFoundError):
        _publish_pane_change_keys({})
        return {}, {}

    paths: dict[str, str] = {}
    change_keys: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t", _PANE_FIELD_MAXSPLIT)
        if len(parts) != _PANE_FIELD_COUNT:
            continue
        (
            name,
            window_active,
            pane_active,
            window_activity,
            pane_id,
            pane_width,
            pane_height,
            path,
        ) = parts
        if window_active != "1" or pane_active != "1":
            continue
        if not name:
            continue
        if path:
            paths[name] = path
        # Only record a key when every component is present.  A partially
        # empty key would compare equal across a real change.
        if window_activity and pane_id and pane_width and pane_height:
            change_keys[name] = "|".join(
                (window_activity, pane_id, pane_width, pane_height)
            )
    _publish_pane_change_keys(change_keys)
    return paths, change_keys


async def list_session_paths() -> dict[str, str]:
    """Return {session_name: active-pane cwd} for all sessions.

    Thin wrapper over `list_session_panes()` (which also returns snapshot
    change keys) for callers that only want the cwd map.

    Note: the cwd is split off with maxsplit on the LAST tab boundary before
    it, so paths containing tabs survive; session names containing tabs do not
    (tmux itself barely tolerates those).
    """
    paths, _ = await list_session_panes()
    return paths


# Memoized cwd → git repo name (or None). Bounded: cleared when it grows past
# _GIT_REPO_CACHE_MAX distinct directories (sessions revisit the same dirs, so
# in practice this never cycles).
_git_repo_cache: dict[str, str | None] = {}
_GIT_REPO_CACHE_MAX = 512


def _main_repo_name_from_worktree(git_file: str) -> str | None:
    """Resolve the *main* repo name for a linked worktree's `.git` file.

    A linked worktree (`git worktree add`) places a `.git` *file* — not a
    directory — at the worktree root, reading::

        gitdir: <main>/.git/worktrees/<wt-name>

    We follow that to the worktree's gitdir, then to the shared common dir
    (canonically via its `commondir` file, falling back to stripping the
    trailing `worktrees/<wt-name>`), and return the basename of the common
    dir's parent — i.e. the main repo directory name. This makes worktree
    sessions group with their parent repo instead of forming a lone
    `dir:<wt-name>` auto-view. Returns None if the file can't be parsed (the
    caller then falls back to the worktree directory's own name).
    """
    try:
        text = ""
        with open(git_file, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("gitdir:"):
                    text = line[len("gitdir:"):].strip()
                    break
        if not text:
            return None
        base = os.path.dirname(git_file)  # worktree root
        wt_gitdir = os.path.normpath(os.path.join(base, text))

        # Prefer the canonical `commondir` pointer; fall back to stripping
        # the conventional ".../worktrees/<name>" suffix.
        common: str | None = None
        commondir_file = os.path.join(wt_gitdir, "commondir")
        try:
            with open(commondir_file, encoding="utf-8") as fh:
                rel = fh.read().strip()
            if rel:
                common = os.path.normpath(os.path.join(wt_gitdir, rel))
        except OSError:
            common = None
        if common is None:
            # wt_gitdir == <main-git-dir>/worktrees/<name>
            common = os.path.dirname(os.path.dirname(wt_gitdir))

        # common is the shared git dir (typically "<repo>/.git"); the repo
        # root is its parent when it is named ".git", else common itself.
        repo_root = (
            os.path.dirname(common)
            if os.path.basename(common) == ".git"
            else common
        )
        return os.path.basename(repo_root) or None
    except OSError:
        return None


def resolve_git_repo(cwd: str) -> str | None:
    """Return the git repo name for *cwd*, or None when not inside a repo.

    Pure-Python walk-up: the repo root is the first ancestor of *cwd*
    (inclusive) containing a `.git` entry. For normal clones `.git` is a
    directory and the name is that directory's basename. For linked worktrees
    `.git` is a file pointing back at the main repo — we resolve it to the
    *main* repo's name (see `_main_repo_name_from_worktree`) so worktree
    sessions group with their parent repo. No `git` subprocess. Memoized per
    directory.
    """
    if not cwd:
        return None
    if cwd in _git_repo_cache:
        return _git_repo_cache[cwd]

    if len(_git_repo_cache) >= _GIT_REPO_CACHE_MAX:
        _git_repo_cache.clear()

    repo: str | None = None
    path = os.path.abspath(cwd)
    while True:
        dot_git = os.path.join(path, ".git")
        if os.path.isfile(dot_git):  # linked worktree
            repo = _main_repo_name_from_worktree(dot_git) or os.path.basename(path) or None
            break
        if os.path.exists(dot_git):  # normal clone (.git directory)
            repo = os.path.basename(path) or None
            break
        parent = os.path.dirname(path)
        if parent == path:  # filesystem root
            break
        path = parent

    _git_repo_cache[cwd] = repo
    return repo


# Maximum number of `tmux capture-pane` subprocesses in flight at once.
#
# The total number of spawns is unchanged (still exactly one per session) —
# only how many run *simultaneously*. Without a bound, a 100-session fleet
# forks 100 processes at once every poll cycle (~2 s): a thundering herd of
# fork/exec, page-table and fd churn, all contending on the single tmux server
# that services them serially anyway.
#
# 32 is chosen so that today's real-world fleets (tens of sessions) are
# completely unaffected — below the limit the gather is exactly as concurrent
# as before, so there is no latency regression at small N — while a large
# fleet is capped at a fixed, modest process footprint.
_SNAPSHOT_CONCURRENCY = 32


# --- Change-detected snapshots (plan item 1.2c) ----------------------------
#
# Force a full capture sweep every Kth cycle regardless of change keys.  This
# is a CORRECTNESS BACKSTOP, not an optimization: it caps any undiscovered way
# a pane's visible content can change without moving the composite key at ONE
# refresh interval (K x 2 s = 30 s), while still removing ~93% of the spawns.
# Do not raise it casually and do not remove it.
_SNAPSHOT_FULL_EVERY = 15

# Change key of the pane as it was when its cached snapshot was captured.
# Keyed by session name; only ever holds live sessions.
_snapshot_keys: dict[str, str] = {}
# Cycle counter driving the _SNAPSHOT_FULL_EVERY backstop.
_snapshot_cycle: int = 0

# --- Pane-key handoff (one-shot freshness handshake) -----------------------
#
# `list_session_panes` produces the change keys; `snapshot_all` consumes them.
# They are passed through module state rather than an argument so that the
# poll cycle's call shape (`snapshot_all(names)`) is unchanged.
#
# The FRESH flag is the safety property, and it is why this is a handshake and
# not a cache: keys are usable ONLY if they were produced since the last
# snapshot.  Consuming them clears the flag, so a `snapshot_all` that is not
# immediately preceded by a `list_session_panes` in the same cycle sees no keys
# and captures everything.  That makes call-ordering mistakes fail toward
# capturing (a wasted fork) instead of toward a stale tile.
_pane_change_keys: dict[str, str] = {}
_pane_change_keys_fresh: bool = False


def _publish_pane_change_keys(change_keys: dict[str, str]) -> None:
    """Hand a freshly-measured change-key map to the next `snapshot_all`."""
    global _pane_change_keys, _pane_change_keys_fresh
    _pane_change_keys = change_keys
    _pane_change_keys_fresh = True


def _take_pane_change_keys() -> dict[str, str] | None:
    """Consume the published keys; None when none were published since last use."""
    global _pane_change_keys_fresh
    if not _pane_change_keys_fresh:
        return None
    _pane_change_keys_fresh = False
    return _pane_change_keys


def reset_snapshot_change_tracking() -> None:
    """Clear the change-detection bookkeeping (cached keys + cycle counter).

    Exists for tests and for any caller that needs the next `snapshot_all` to
    behave as a cold start.  Does not touch the snapshot cache itself.
    """
    global _snapshot_keys, _snapshot_cycle, _pane_change_keys, _pane_change_keys_fresh
    _snapshot_keys = {}
    _snapshot_cycle = 0
    _pane_change_keys = {}
    _pane_change_keys_fresh = False


# Sentinel distinguishing "caller said nothing" (use the published keys, if
# fresh) from an explicit `change_keys=None` (capture everything).
_USE_PUBLISHED_KEYS: dict[str, str] = {}


async def snapshot_all(
    names: list[str],
    change_keys: dict[str, str] | None = _USE_PUBLISHED_KEYS,
) -> dict[str, str]:
    """Capture sessions concurrently and return a name→text mapping.

    CHANGE-DETECTED CAPTURE (item 1.2c).  *change_keys* normally comes from
    the immediately preceding `list_session_panes()` (which costs no extra
    spawn) via the freshness handshake — callers pass nothing.  When keys are
    available, a session is SKIPPED —
    its previous snapshot reused verbatim from the module cache — only when all
    of these hold:

      * it has a cached snapshot from a previous cycle (a NEW session always
        captures), and
      * its change key is KNOWN (an absent/unparseable key always captures),
        and
      * that key is byte-identical to the key recorded when the cached snapshot
        was taken (any change in output time, active pane, or pane size
        captures), and
      * this is not the every-`_SNAPSHOT_FULL_EVERY`th backstop cycle.

    Every ambiguous case resolves toward capturing: a missed capture is a
    user-visible stale tile, an extra capture is a wasted fork.  When no keys
    are available — none published, stale handshake, tmux query failed, or an
    explicit ``change_keys=None`` — EVERY session is captured, exactly as
    before this item.

    Concurrency is bounded to _SNAPSHOT_CONCURRENCY simultaneous
    `capture-pane` subprocesses.  When the number of sessions actually being
    captured is <= _SNAPSHOT_CONCURRENCY the behavior is identical to an
    unbounded gather.

    Uses asyncio.gather with return_exceptions=True so that individual
    failures do not abort the whole batch.  Failed sessions map to ''.

    Note: this function does not update the snapshot cache — callers pass the
    result to `update_session_cache`.  It DOES maintain its own change-key
    bookkeeping (`_snapshot_keys`), pruned to *names* on every call.
    """
    global _snapshot_cycle, _snapshot_keys

    if change_keys is _USE_PUBLISHED_KEYS:
        change_keys = _take_pane_change_keys()
    else:
        # An explicit argument still consumes the handshake, so a later
        # keyless call can't pick up keys measured before this one.
        _take_pane_change_keys()

    if not names:
        _snapshot_keys = {}
        return {}

    _snapshot_cycle += 1
    force_all = change_keys is None or (_snapshot_cycle % _SNAPSHOT_FULL_EVERY == 0)

    cached = _snapshots
    reused: dict[str, str] = {}
    to_capture: list[str] = []
    for name in names:
        key = None if change_keys is None else change_keys.get(name)
        # Every clause below is a REUSE precondition; failing any one of them
        # falls through to the else branch and captures.  Read them as "reuse
        # only if ...", not as descriptions of when we capture.
        if (
            not force_all  # reuse only if this is not a forced-sweep cycle
            and key  # ... and the key is known and parseable
            and name in cached  # ... and we actually have a cached snapshot
            and _snapshot_keys.get(name) == key  # ... and the pane has not moved
        ):
            reused[name] = cached[name]
        else:
            to_capture.append(name)

    # Carry forward the keys of everything we are reusing; captured sessions
    # get their (new) key recorded below, but only if the capture succeeded.
    next_keys: dict[str, str] = {n: _snapshot_keys[n] for n in reused}

    if not to_capture:
        _snapshot_keys = next_keys
        return reused

    names = to_capture

    # Created per call, not at module import: an asyncio.Semaphore binds to the
    # event loop that first awaits it, and the test suite runs many separate
    # loops. A per-call instance is a trivially cheap object and is correct on
    # every loop.
    limiter = asyncio.Semaphore(_SNAPSHOT_CONCURRENCY)

    async def _limited(name: str) -> str:
        async with limiter:
            return await capture_pane(name)

    results = await asyncio.gather(
        *[_limited(name) for name in names],
        return_exceptions=True,
    )
    snapshots: dict[str, str] = dict(reused)
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            snapshots[name] = ""
            # Deliberately do NOT record a key for a failed capture: leaving it
            # absent makes the next cycle retry instead of caching the '' until
            # the pane happens to change.
            continue
        snapshots[name] = result
        key = None if change_keys is None else change_keys.get(name)
        # An empty capture is also treated as "don't trust it": `capture_pane`
        # swallows RuntimeError and returns '' , so '' is indistinguishable
        # from a failed spawn.  Not recording the key costs one recapture per
        # cycle for a genuinely blank pane and removes a staleness hole.
        if key and result:
            next_keys[name] = key
    _snapshot_keys = next_keys
    return snapshots
