"""
State schema and factory functions for muxplex.

State schema (all values are plain JSON-serialisable dicts):

    {
        "active_session": str | None,
        "active_remote_id": str | None,
        "active_view": str,  # 'all' | 'hidden' | view name
        "session_order": list[str],
        "sessions": {
            "<name>": {
                "bell": {
                    "last_fired_at": float | None,
                    "seen_at": float | None,
                    "unseen_count": int,
                }
            }
        },
        "devices": {
            "<device_id>": {
                "label": str,
                "viewing_session": str | None,
                "view_mode": "fullscreen" | "grid",
                "last_interaction_at": float,
                "last_heartbeat_at": float,
            }
        },
    }
"""

import asyncio
import json
import os
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_default_state_dir = Path.home() / ".local" / "share" / "muxplex"
STATE_DIR: Path = Path(
    os.environ.get(
        "MUXPLEX_STATE_DIR", os.environ.get("TMUX_WEB_STATE_DIR", _default_state_dir)
    )
)
STATE_PATH: Path = STATE_DIR / "state.json"

# ---------------------------------------------------------------------------
# Global asyncio lock — must be acquired before reading or writing state.
# ---------------------------------------------------------------------------

state_lock: asyncio.Lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def empty_state() -> dict:
    """Return a fresh, empty top-level state dict.

    Every call returns a fully independent object — no shared mutables.
    """
    return {
        "active_session": None,
        "active_remote_id": None,
        "active_view": "all",
        "session_order": [],
        "sessions": {},
        "devices": {},
    }


def empty_bell() -> dict:
    """Return a fresh bell sub-dict with all fields reset."""
    return {
        "last_fired_at": None,
        "seen_at": None,
        "unseen_count": 0,
    }


def empty_device(device_id: str, label: str) -> dict:  # noqa: ARG001
    """Return a fresh device sub-dict.

    Args:
        device_id: Identifier for the device (unused in the dict itself,
                   kept as a parameter for call-site clarity).
        label:     Human-readable name for the device.
    """
    now = time.time()
    return {
        "label": label,
        "viewing_session": None,
        "view_mode": "grid",
        "last_interaction_at": now,
        "last_heartbeat_at": now,
    }


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------


def register_device(
    state: dict,
    device_id: str,
    label: str,
    viewing_session: str | None,
    view_mode: str,
    last_interaction_at: float,
) -> None:
    """Create or update a device entry in state['devices'].

    For new devices, seeds the entry via empty_device().
    Always refreshes last_heartbeat_at to time.time().
    Updates label, viewing_session, view_mode, last_interaction_at.
    """
    if device_id not in state["devices"]:
        state["devices"][device_id] = empty_device(device_id, label)

    device = state["devices"][device_id]
    device["label"] = label
    device["viewing_session"] = viewing_session
    device["view_mode"] = view_mode
    device["last_interaction_at"] = last_interaction_at
    device["last_heartbeat_at"] = time.time()


def prune_devices(state: dict, ttl_seconds: float = 300.0) -> list[str]:
    """Remove devices whose last_heartbeat_at is older than ttl_seconds.

    Returns the list of removed device IDs.
    """
    cutoff = time.time() - ttl_seconds
    stale = [
        device_id
        for device_id, device in state["devices"].items()
        if device["last_heartbeat_at"] < cutoff
    ]
    for device_id in stale:
        del state["devices"][device_id]
    return stale


# ---------------------------------------------------------------------------
# Sync I/O helpers (no lock — callers must hold state_lock when appropriate)
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """Read and return state from STATE_PATH.

    Returns empty_state() if the file does not exist or contains invalid JSON.
    """
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return empty_state()


def save_state(state: dict) -> None:
    """Atomically write *state* to STATE_PATH.

    Uses the write-to-tmp-then-os.replace pattern so readers never see a
    partial file.  Creates STATE_DIR (and parents) if it does not exist.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(STATE_PATH) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Async wrappers — acquire state_lock before touching the file
# ---------------------------------------------------------------------------
#
# These deliberately call the SYNCHRONOUS primitives on the event-loop thread.
# Plan item 2.4 (docs/plans/2026-08-08-resource-efficiency-plan.md) proposed
# wrapping them in ``asyncio.to_thread``; that item was MEASURED after phases
# 0-3 landed and DELIBERATELY NOT IMPLEMENTED.  Do not "fix" this.
#
# Measured 2026-08-08 on this machine (native ext4, WSL2, CPython 3.11):
#
#   operation                          mean      p95      p99
#   load_state()  N=20 sessions        25 us     30 us     65 us
#   save_state()  N=20 sessions       870 us   1535 us   2282 us
#   load_settings()                   147 us
#   load_pruning_state()               22 us
#
#   blocking per event-loop second, 20 sessions / 4 browser tabs:
#     poll cycle       0.5/s x 1217 us  =  609 us/s
#     /api/sessions    2.0/s x   57 us  =  113 us/s
#     heartbeat        0.8/s x  809 us  =  647 us/s
#     TOTAL            ~1.37 ms/s  =  0.14% of wall clock
#
# Two facts make the change actively counterproductive rather than merely
# unnecessary:
#
#  1. ``save_state`` cost is FIXED, not O(N): 898 us at N=1, 870 us at N=20,
#     1306 us at N=100.  It is the atomic tmp-write + ``os.replace`` metadata
#     path, not serialization.  By this plan's own scale-invariance criterion
#     that makes it the lowest-priority kind of cost.
#  2. An ``asyncio.to_thread`` round trip on this machine costs ~860 us mean /
#     2093 us p95 (warm pool; 447 us/hop under 4-way concurrency) -- i.e. AS
#     MUCH AS the entire ``save_state`` it would offload, and ~30x the cost of
#     a ``load_state``.  Offloading would not shorten the operation; it would
#     roughly double the wall-clock time ``state_lock`` is held (the lock must
#     stay held across the hop to preserve write ordering), making writers
#     serialize worse than they do today.
#
# What would change the answer: state.json growing by an order of magnitude
# (so save cost is dominated by serialization and scales with N), STATE_DIR
# living on a network/fuse mount where a write is tens of ms, or a profile
# showing these calls as a measurable share of request latency.  Re-measure
# before acting; do not port this from a machine with different storage.




async def read_state() -> dict:
    """Async read: acquires state_lock, then delegates to load_state()."""
    async with state_lock:
        return load_state()


async def write_state(state: dict) -> None:
    """Async write: acquires state_lock, then delegates to save_state()."""
    async with state_lock:
        save_state(state)
