"""
Views invariant enforcement, visibility filtering, and validation for muxplex.

Schema v2 semantics (see docs/plans/2026-05-17-hidden-state-redesign-design.md):
- "hidden" is a property of a session, determined by membership in
  hidden_sessions. View membership and hidden state are orthogonal.
- A session key MAY appear in both hidden_sessions and one or more
  view.sessions. Lists are filtered at read time via `filter_visible`.
- The legacy mutual-exclusion invariant (`enforce_mutual_exclusion`) is
  retained as a backstop in v1 for mixed-version federation compatibility.
  It will be removed in Phase 3 once all peers report _schema_version >= 2.

Other invariants:
- View names are non-empty, max 30 chars, trimmed, unique, not reserved.
- Duplicate session keys within a view are deduplicated by
  `enforce_mutual_exclusion`.
"""

RESERVED_VIEW_NAMES = frozenset({"all", "hidden"})
MAX_VIEW_NAME_LENGTH = 30


# ---------------------------------------------------------------------------
# Schema v2: visibility filtering (read-time)
# ---------------------------------------------------------------------------


def _key_of(session: dict) -> str:
    """Canonical key for a session dict: prefer `sessionKey`, fall back to `name`."""
    return session.get("sessionKey") or session.get("name") or ""


def is_hidden(key: str, settings: dict) -> bool:
    """Return True if the given key is in settings['hidden_sessions']."""
    return key in (settings.get("hidden_sessions") or [])


def filter_visible(
    sessions: list[dict],
    settings: dict,
    view: str,
    *,
    include_hidden: bool = False,
) -> list[dict]:
    """Return the canonical visible session list for the given view.

    This is the single source of truth for "what is in this view right now."
    Every count display and every list render must go through this function
    (or the frontend equivalent) — never read raw lengths off stored arrays.

    Parameters:
        sessions: live session dicts (from sessions.list_sessions or similar).
            Each should have `sessionKey` and/or `name`; entries with a truthy
            `status` field are treated as non-session tiles and excluded.
        settings: dict containing `views` and `hidden_sessions`.
        view: "all", "hidden", or a user view name.
        include_hidden: when True, hidden sessions are NOT filtered out of
            "all" or user views. Ignored for "hidden" (which always shows
            only hidden sessions).

    Behavior:
        - Unknown view name → empty list (callers can detect missing views
          by comparing to the user's view list, not via this function).
        - "hidden" view → only sessions whose key (or bare name) appears in
          hidden_sessions. include_hidden is meaningless here.
        - "all" view → all live sessions; exclude hidden unless include_hidden.
        - User view → sessions whose key (or bare name) is in view.sessions;
          exclude hidden unless include_hidden.

    Dual-lookup against `sessionKey` and `name` handles legacy bare-name
    entries in stored data. Once `normalize_session_keys` has run on the
    install, all stored entries should be in `device_id:name` form and the
    fallback is harmless.
    """
    hidden = set(settings.get("hidden_sessions") or [])
    live = [s for s in (sessions or []) if not s.get("status")]

    def is_session_hidden(s: dict) -> bool:
        return _key_of(s) in hidden or s.get("name", "") in hidden

    if view == "hidden":
        return [s for s in live if is_session_hidden(s)]

    if view == "all":
        if include_hidden:
            return list(live)
        return [s for s in live if not is_session_hidden(s)]

    # User view
    user_view = next(
        (v for v in (settings.get("views") or []) if v.get("name") == view),
        None,
    )
    if user_view is None:
        return []
    members = set(user_view.get("sessions") or [])

    def in_view(s: dict) -> bool:
        return _key_of(s) in members or s.get("name", "") in members

    if include_hidden:
        return [s for s in live if in_view(s)]
    return [s for s in live if in_view(s) and not is_session_hidden(s)]


def visible_count(
    sessions: list[dict],
    settings: dict,
    view: str,
    *,
    include_hidden: bool = False,
) -> int:
    """Length of `filter_visible(...)`. Use this for every count display."""
    return len(filter_visible(sessions, settings, view, include_hidden=include_hidden))


# ---------------------------------------------------------------------------
# Key normalization (one-shot or idempotent, run after fetching live sessions)
# ---------------------------------------------------------------------------


def normalize_session_keys(settings: dict, sessions: list[dict]) -> dict:
    """Upgrade bare-name entries in stored keys to `device_id:name` form.

    Pre-v2 stored entries used bare `name` strings. v2 stores
    `device_id:name`. This function walks `hidden_sessions` and each
    `view.sessions`, and for any bare-name entry that has a matching live
    session with a `sessionKey`, replaces the entry in place with the
    canonical form.

    Idempotent: entries already in canonical form are left untouched.
    Entries that have no matching live session are also left untouched —
    they may match in the future, or they may be pruned by
    `prune_stale_keys` (Phase 4).

    Mutates and returns *settings*.
    """
    # Build a name → sessionKey map from live sessions. Only sessions that
    # actually have a sessionKey contribute; bare-name live sessions are
    # never the target of an upgrade.
    name_to_key: dict[str, str] = {}
    for s in sessions or []:
        name = s.get("name")
        key = s.get("sessionKey")
        if name and key and name != key:
            # Prefer the first sessionKey we see for a given name. If two
            # live sessions share a name across devices, we cannot pick a
            # single canonical form anyway; leave the bare-name entry alone.
            name_to_key.setdefault(name, key)

    def upgrade(entries: list[str]) -> list[str]:
        result: list[str] = []
        for entry in entries:
            if entry in name_to_key:
                result.append(name_to_key[entry])
            else:
                result.append(entry)
        return result

    if isinstance(settings.get("hidden_sessions"), list):
        settings["hidden_sessions"] = upgrade(settings["hidden_sessions"])

    for view in settings.get("views") or []:
        if isinstance(view.get("sessions"), list):
            view["sessions"] = upgrade(view["sessions"])

    return settings


def enforce_mutual_exclusion(settings: dict) -> dict:
    """Enforce that hidden_sessions and view sessions are disjoint.

    If a session key appears in both hidden_sessions and any view,
    it is removed from hidden_sessions (favor visibility over hiding).

    Also deduplicates session keys within each view.

    Mutates and returns the settings dict.
    """
    views = settings.get("views", [])
    hidden = settings.get("hidden_sessions", [])

    # Collect all session keys across all views
    all_view_sessions: set[str] = set()
    for view in views:
        all_view_sessions.update(view.get("sessions", []))

    # Remove overlap from hidden (favor visibility)
    if all_view_sessions and hidden:
        settings["hidden_sessions"] = [s for s in hidden if s not in all_view_sessions]

    # Deduplicate session keys within each view (preserve order)
    for view in views:
        sessions = view.get("sessions", [])
        seen: set[str] = set()
        deduped: list[str] = []
        for s in sessions:
            if s not in seen:
                seen.add(s)
                deduped.append(s)
        view["sessions"] = deduped

    return settings


# ---------------------------------------------------------------------------
# Pure data ops (Phase 2)
#
# Pure data ops. Composable. No tangling of concerns. User-intent ops live on
# the frontend (where the PATCH boundary is) and call these to build the final
# state.
#
# Each mutates the settings dict in place and returns it. No side effects
# beyond the named operation.
# ---------------------------------------------------------------------------


def add_membership(settings: dict, view_name: str, key: str) -> dict:
    """Add `key` to view's session list if absent. No-op if view doesn't exist."""
    for view in settings.get("views") or []:
        if view.get("name") == view_name:
            sessions = view.setdefault("sessions", [])
            if key not in sessions:
                sessions.append(key)
            break
    return settings


def remove_membership(settings: dict, view_name: str, key: str) -> dict:
    """Remove `key` from view's session list. No-op if view or key absent."""
    for view in settings.get("views") or []:
        if view.get("name") == view_name:
            sessions = view.get("sessions") or []
            if key in sessions:
                sessions.remove(key)
            break
    return settings


def remove_from_all_views(settings: dict, key: str) -> dict:
    """Remove `key` from every view's session list."""
    for view in settings.get("views") or []:
        sessions = view.get("sessions") or []
        if key in sessions:
            sessions.remove(key)
    return settings


def hide(settings: dict, key: str) -> dict:
    """Append `key` to hidden_sessions if absent."""
    hidden = settings.setdefault("hidden_sessions", [])
    if key not in hidden:
        hidden.append(key)
    return settings


def unhide(settings: dict, key: str) -> dict:
    """Remove `key` from hidden_sessions. No-op if absent."""
    hidden = settings.get("hidden_sessions") or []
    if key in hidden:
        hidden.remove(key)
    return settings


def validate_view_name(name: str, existing_views: list[dict]) -> str | None:
    """Validate a view name. Returns an error message string, or None if valid.

    Rules:
    - Non-empty after trimming
    - Max 30 characters after trimming
    - Not a reserved name ("all", "hidden") case-insensitive
    - Unique among existing views (case-sensitive match)
    """
    trimmed = name.strip()
    if not trimmed:
        return "View name cannot be empty"
    if len(trimmed) > MAX_VIEW_NAME_LENGTH:
        return f"View name must be {MAX_VIEW_NAME_LENGTH} characters or fewer"
    if trimmed.lower() in RESERVED_VIEW_NAMES:
        return f"'{trimmed}' is a reserved name"
    existing_names = {v.get("name", "") for v in existing_views}
    if trimmed in existing_names:
        return f"A view named '{trimmed}' already exists"
    return None
