"""
Local sidecar bookkeeping for stale-key pruning.

Stale-key pruning is a per-device concern: each device independently tracks
which session keys it has failed to observe, and prunes them from its own
settings once the grace period expires.  That bookkeeping must NEVER be synced
to peers — it is stored in a local sidecar file outside settings.json.

The prune ACTION (removing stale keys from view.sessions / hidden_sessions) IS
a normal settings write and DOES sync via the existing LWW mechanism.

See docs/plans/2026-05-17-hidden-state-redesign-design.md, Phase 4 and the
section "Stale key pruning (separate concern, local-only state)".
"""

import json
from pathlib import Path

PRUNING_STATE_PATH = Path.home() / ".config" / "muxplex" / "pruning.json"


def load_pruning_state() -> dict:
    """Load local pruning bookkeeping from the sidecar file.

    Returns an empty dict on absent file or corrupt JSON — never raises for
    either condition.  Unexpected errors (e.g. PermissionError) propagate.

    The returned dict has the shape::

        {
            "first_missed_at": {
                "dev1:dead-session": 1747512345.0,
                ...
            }
        }
    """
    try:
        text = PRUNING_STATE_PATH.read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def save_pruning_state(state: dict) -> None:
    """Write pruning bookkeeping to the sidecar file.

    Creates parent directories as needed.  Matches the direct-write style of
    save_settings (indent=2, trailing newline).
    """
    PRUNING_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRUNING_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
