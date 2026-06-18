"""
tmux session enumeration and snapshot helpers for the tmux-web muxplex.

In-memory cache:
    _session_list  — most-recently-enumerated list of session names.
    _snapshots     — most-recently-captured pane text, keyed by session name.
    _session_paths — active-pane cwd per session, keyed by session name.

Public API:
    get_session_list()                    → list[str]
    get_snapshots()                       → dict[str, str]
    get_session_paths()                   → dict[str, str]
    update_session_cache(names, snapshots) → None
    update_session_paths(paths)           → None
    run_tmux(*args)                       → str   (raises RuntimeError on nonzero exit)
    enumerate_sessions()                  → list[str]
    capture_pane(name, lines)             → str
    snapshot_all(names)                   → dict[str, str]
    list_session_paths()                  → dict[str, str]
    resolve_git_repo(cwd)                 → str | None
"""

import asyncio
import os

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_session_list: list[str] = []
_snapshots: dict[str, str] = {}
_session_paths: dict[str, str] = {}


def get_session_list() -> list[str]:
    """Return a copy of the cached session name list."""
    return list(_session_list)


def get_snapshots() -> dict[str, str]:
    """Return a copy of the cached pane-snapshot dict."""
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


async def enumerate_sessions() -> list[str]:
    """Return the list of currently running tmux session names.

    Calls ``tmux list-sessions -F #{session_name}``, splits on newlines,
    and strips whitespace from each entry.

    Returns [] if tmux is not running (RuntimeError from run_tmux).
    """
    try:
        output = await run_tmux("list-sessions", "-F", "#{session_name}")
    except (RuntimeError, FileNotFoundError):
        return []

    names = [line.strip() for line in output.splitlines() if line.strip()]
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


async def list_session_paths() -> dict[str, str]:
    """Return {session_name: active-pane cwd} for all sessions.

    ONE subprocess per call:
        tmux list-panes -a -F '#{session_name}\\t#{window_active}\\t#{pane_active}\\t#{pane_current_path}'
    keeping only rows where both the window and the pane are active (the
    session's "current" pane). Sessions whose row can't be parsed are simply
    omitted. Returns {} when tmux is unavailable.

    Note: the cwd is split off with maxsplit on the FIRST three tabs, so paths
    containing tabs survive; session names containing tabs do not (tmux itself
    barely tolerates those).
    """
    try:
        output = await run_tmux(
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_active}\t#{pane_active}\t#{pane_current_path}",
        )
    except (RuntimeError, FileNotFoundError):
        return {}

    paths: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        name, window_active, pane_active, path = parts
        if window_active != "1" or pane_active != "1":
            continue
        if name and path:
            paths[name] = path
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


async def snapshot_all(names: list[str]) -> dict[str, str]:
    """Capture all sessions concurrently and return a name→text mapping.

    Uses asyncio.gather with return_exceptions=True so that individual
    failures do not abort the whole batch.  Failed sessions map to ''.

    Note: this function does not mutate module state — it does not update the module cache.
    Callers are responsible for passing the result to update_session_cache.
    """
    if not names:
        return {}
    results = await asyncio.gather(
        *[capture_pane(name) for name in names],
        return_exceptions=True,
    )
    snapshots: dict[str, str] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            snapshots[name] = ""
        else:
            snapshots[name] = result
    return snapshots
