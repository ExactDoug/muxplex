"""
Shared pytest fixtures for the muxplex Python suite.

WHY THIS FILE EXISTS
--------------------
Every muxplex module that persists anything does so through a *module-level path
constant* pointing at the user's REAL ``~/.config/muxplex`` directory:

    muxplex.state.STATE_PATH          state.json
    muxplex.settings.SETTINGS_PATH    settings.json   (views + hidden_sessions!)
    muxplex.pruning.PRUNING_STATE_PATH pruning.json
    muxplex.identity.IDENTITY_PATH    identity.json
    muxplex.ttyd.TTYD_PID_PATH        ttyd.pid

Historically each of the 22 test modules redirected these itself via an autouse
``monkeypatch`` fixture.  That meant a **new** test file got the real config by
default — a live footgun that has previously destroyed the user's saved views
(a partial settings write drops ``views``/``hidden_sessions``).

``redirect_muxplex_paths`` below makes the safe behaviour the *default* for
every test in the package, new files included.

INTERACTION WITH THE EXISTING PER-MODULE FIXTURES
-------------------------------------------------
This fixture does NOT replace them; it runs *in addition* to them.  Ordering is
well defined: same-scope autouse fixtures declared in a ``conftest.py`` are
instantiated **before** those declared in the test module, and ``monkeypatch``
undoes its patches in reverse order at teardown.  So a module-level (or
in-test) redirect is applied *on top of* this one and therefore **wins** —
existing modules keep their exact current behaviour, and this conftest is purely
a safe default for anything that does not redirect for itself.

Deliberately NOT redirected: ``muxplex.settings.FEDERATION_KEY_PATH``.
``test_settings.py`` asserts that constant still equals the real
``~/.config/muxplex/federation_key`` location; patching it here would turn that
into a false failure.  Tests that exercise the federation key must redirect it
themselves (``test_api.py`` already does).
"""

import pytest


@pytest.fixture(autouse=True)
def redirect_muxplex_paths(tmp_path, monkeypatch):
    """Autouse: point every muxplex state/config path at ``tmp_path``.

    Returns the tmp config dir so a test may ``request`` it explicitly if it
    wants to inspect what was written.
    """
    cfg_dir = tmp_path / "muxplex-config"

    # state.json
    monkeypatch.setattr("muxplex.state.STATE_DIR", cfg_dir)
    monkeypatch.setattr("muxplex.state.STATE_PATH", cfg_dir / "state.json")

    # settings.json
    monkeypatch.setattr("muxplex.settings.SETTINGS_PATH", cfg_dir / "settings.json")

    # pruning.json (local sidecar, never synced)
    monkeypatch.setattr(
        "muxplex.pruning.PRUNING_STATE_PATH", cfg_dir / "pruning.json"
    )

    # identity.json — load_device_id() WRITES a new uuid when the file is absent
    monkeypatch.setattr("muxplex.identity.IDENTITY_PATH", cfg_dir / "identity.json")

    # ttyd pid file
    pid_dir = tmp_path / "muxplex-ttyd"
    monkeypatch.setattr("muxplex.ttyd.TTYD_PID_DIR", pid_dir)
    monkeypatch.setattr("muxplex.ttyd.TTYD_PID_PATH", pid_dir / "ttyd.pid")

    return cfg_dir
