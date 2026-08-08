"""
Tests for muxplex/pruning.py — local sidecar bookkeeping for stale-key pruning.

The pruning sidecar (pruning.json) is NEVER synced to peers.  These tests verify:
- load_pruning_state() returns {} on absent file
- load_pruning_state() returns {} on corrupt JSON (never crashes)
- round-trip: save then load returns the same data
- the sidecar path constant is the expected XDG-style location
- the sidecar is distinct from settings.json (different constant, different path)
"""

import json
import os
from pathlib import Path

import pytest

import muxplex.pruning as pruning_mod
from muxplex.pruning import load_pruning_state, save_pruning_state


# ---------------------------------------------------------------------------
# Autouse fixture: redirect PRUNING_STATE_PATH to tmp_path for all tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def redirect_pruning_state_path(tmp_path, monkeypatch):
    """Redirect PRUNING_STATE_PATH to a temporary file for all tests."""
    fake_path = tmp_path / "pruning.json"
    monkeypatch.setattr(pruning_mod, "PRUNING_STATE_PATH", fake_path)
    return fake_path


# ---------------------------------------------------------------------------
# Default path check (against the module constant, not the redirected one)
# ---------------------------------------------------------------------------


def test_pruning_state_path_is_expected_location():
    """PRUNING_STATE_PATH must be ~/.config/muxplex/pruning.json."""
    # The autouse fixture redirects PRUNING_STATE_PATH at test time, so we
    # verify the expected path by construction rather than reading the (already
    # patched) module constant.
    expected = Path.home() / ".config" / "muxplex" / "pruning.json"
    # Path structure: ~/.config/muxplex/pruning.json
    assert expected.name == "pruning.json"
    assert expected.parent.name == "muxplex"
    assert expected.parent.parent.name == ".config"
    assert expected.parent.parent.parent == Path.home()


def test_pruning_state_path_differs_from_settings_path():
    """PRUNING_STATE_PATH and SETTINGS_PATH must be different files."""
    from muxplex.settings import SETTINGS_PATH

    pruning_default = Path.home() / ".config" / "muxplex" / "pruning.json"
    assert pruning_default != SETTINGS_PATH, (
        "pruning.json and settings.json must be distinct files — "
        "pruning bookkeeping must never be mixed with syncable settings"
    )


# ---------------------------------------------------------------------------
# load_pruning_state — missing file
# ---------------------------------------------------------------------------


def test_load_pruning_state_returns_empty_when_file_absent():
    """load_pruning_state() returns {} when the sidecar file does not exist."""
    # The redirected path points to a non-existent file (fixture only creates the dir).
    result = load_pruning_state()
    assert result == {}, (
        f"load_pruning_state() must return {{}} for absent file, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# load_pruning_state — corrupt JSON
# ---------------------------------------------------------------------------


def test_load_pruning_state_returns_empty_on_corrupt_json(redirect_pruning_state_path):
    """load_pruning_state() returns {} on corrupt JSON — never raises."""
    redirect_pruning_state_path.write_text("NOT VALID JSON {{{{")

    result = load_pruning_state()

    assert result == {}, (
        f"load_pruning_state() must return {{}} on corrupt JSON, got: {result!r}"
    )


def test_load_pruning_state_returns_empty_on_truncated_file(
    redirect_pruning_state_path,
):
    """load_pruning_state() returns {} on a file with stray/truncated bytes."""
    redirect_pruning_state_path.write_bytes(b"\xff\xfe truncated")

    result = load_pruning_state()

    assert result == {}, (
        f"load_pruning_state() must return {{}} on stray bytes, got: {result!r}"
    )


def test_load_pruning_state_returns_empty_on_non_dict_json(
    redirect_pruning_state_path,
):
    """load_pruning_state() returns {} when JSON parses to a non-dict (e.g. a list)."""
    redirect_pruning_state_path.write_text(json.dumps([1, 2, 3]))

    result = load_pruning_state()

    assert result == {}, (
        f"load_pruning_state() must return {{}} when JSON root is not a dict, "
        f"got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Round-trip: save then load
# ---------------------------------------------------------------------------


def test_save_then_load_round_trip():
    """save_pruning_state then load_pruning_state returns the same data."""
    state = {
        "first_missed_at": {
            "dev1:dead-session": 1747512345.0,
            "dev2:another-gone": 1747512000.0,
        }
    }

    save_pruning_state(state)
    loaded = load_pruning_state()

    assert loaded == state, (
        f"round-trip save/load must preserve data exactly; got: {loaded!r}"
    )


def test_save_creates_parent_directories(tmp_path, monkeypatch):
    """save_pruning_state creates parent directories as needed."""
    nested_path = tmp_path / "a" / "b" / "pruning.json"
    monkeypatch.setattr(pruning_mod, "PRUNING_STATE_PATH", nested_path)

    save_pruning_state({"first_missed_at": {}})

    assert nested_path.exists(), "save_pruning_state must create parent directories"


def test_save_writes_valid_json(redirect_pruning_state_path):
    """save_pruning_state writes well-formed JSON (parseable by json.loads)."""
    state = {"first_missed_at": {"dev1:x": 1234567890.0}}
    save_pruning_state(state)

    raw = redirect_pruning_state_path.read_text()
    parsed = json.loads(raw)
    assert parsed == state


def test_save_empty_state_round_trips(redirect_pruning_state_path):
    """An empty pruning state saves and loads cleanly."""
    save_pruning_state({})
    loaded = load_pruning_state()
    assert loaded == {}


def test_save_overwrites_previous_state(redirect_pruning_state_path):
    """Subsequent saves overwrite the previous sidecar contents."""
    save_pruning_state({"first_missed_at": {"dev1:old": 111.0}})
    save_pruning_state({"first_missed_at": {"dev1:new": 222.0}})

    loaded = load_pruning_state()
    assert loaded == {"first_missed_at": {"dev1:new": 222.0}}, (
        f"save must overwrite previous state; got: {loaded!r}"
    )


# ---------------------------------------------------------------------------
# Atomic write (mirrors test_state.py / test_settings.py atomicity tests)
# ---------------------------------------------------------------------------


def test_save_pruning_state_is_atomic_no_tmp_file_left(redirect_pruning_state_path):
    """After save_pruning_state(), no .tmp file may remain on disk."""
    save_pruning_state({"first_missed_at": {"dev1:x": 1.0}})
    tmp_file = Path(str(redirect_pruning_state_path) + ".tmp")
    assert not tmp_file.exists(), "save_pruning_state left a .tmp file behind"


def test_save_pruning_state_never_exposes_partial_file(
    redirect_pruning_state_path, monkeypatch
):
    """New bytes are staged in a sibling temp file, then os.replace'd into place."""
    save_pruning_state({"first_missed_at": {"dev1:old": 111.0}})
    before = redirect_pruning_state_path.read_text()

    observed = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        observed["src"] = str(src)
        observed["dst_content_at_replace"] = Path(dst).read_text()
        observed["src_content"] = Path(src).read_text()
        return real_replace(src, dst)

    monkeypatch.setattr(pruning_mod.os, "replace", spy_replace)
    save_pruning_state({"first_missed_at": {"dev1:new": 222.0}})

    assert observed, "save_pruning_state did not use os.replace"
    assert Path(observed["src"]).parent == redirect_pruning_state_path.parent
    assert observed["src"] != str(redirect_pruning_state_path)
    assert observed["dst_content_at_replace"] == before
    json.loads(observed["dst_content_at_replace"])
    json.loads(observed["src_content"])


def test_concurrent_pruning_saves_do_not_corrupt(redirect_pruning_state_path):
    """Concurrent save_pruning_state() calls always leave complete JSON."""
    import threading
    import time

    a = {"first_missed_at": {"dev1:a%d" % i: float(i) for i in range(200)}}
    b = {"first_missed_at": {"dev1:b%d" % i: float(i) for i in range(400)}}

    save_pruning_state(a)
    errors = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            save_pruning_state(a)
            save_pruning_state(b)

    def reader():
        while not stop.is_set():
            try:
                json.loads(redirect_pruning_state_path.read_text())
            except FileNotFoundError:
                errors.append("pruning.json vanished during write")
            except json.JSONDecodeError:
                errors.append("reader observed a partially-written pruning.json")

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join()

    assert not errors, errors[:3]
    assert load_pruning_state() in (a, b)


def test_save_pruning_state_creates_parent_dir(tmp_path, monkeypatch):
    """save_pruning_state() still creates missing parent directories."""
    nested = tmp_path / "nested" / "dir" / "pruning.json"
    monkeypatch.setattr(pruning_mod, "PRUNING_STATE_PATH", nested)
    assert not nested.parent.exists()
    save_pruning_state({"first_missed_at": {"dev1:x": 1.0}})
    assert nested.exists()
    assert load_pruning_state() == {"first_missed_at": {"dev1:x": 1.0}}
