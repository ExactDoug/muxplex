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

PATHS ARE NOT THE ONLY SHARED RESOURCE — SEE ``never_kill_a_real_server``
-------------------------------------------------------------------------
Redirecting file paths does not isolate a test from the *network*.  ``serve()``
kills whatever process is listening on its port before binding, which is a
machine-wide side effect no ``tmp_path`` can contain.  That fixture is the port
analogue of this one; read its docstring before adding tests that call
``serve()``.
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


@pytest.fixture(autouse=True)
def reset_session_times(monkeypatch):
    """Isolate the process-global list-sessions times map (issue #24).

    enumerate_sessions() publishes it as a side effect, so without this a test
    that enumerates leaks recency times into later tests' payloads.
    """
    monkeypatch.setattr("muxplex.sessions._session_times", {})


@pytest.fixture(autouse=True)
def never_kill_a_real_server(request, monkeypatch):
    """Autouse: stop ``serve()`` from SIGTERMing the developer's running muxplex.

    ``cli.serve()`` calls ``_kill_stale_port_holder(port)`` before binding, which
    runs ``lsof -ti :<port>`` and SIGTERMs every occupant.  That exists to break
    a systemd restart crash-loop and is correct in production — but in a test it
    reaches straight out of the sandbox and kills whatever muxplex is genuinely
    serving on that port.

    Patching ``uvicorn.run`` is NOT sufficient protection, which is the trap:
    it stops a server being *started* while leaving the kill fully live.  Eleven
    ``test_serve_*`` tests did exactly that.

    Found 2026-08-09 the hard way — a bare ``pytest muxplex/tests/test_cli.py``
    silently terminated the user's running server (137 tests passing, server
    gone).  Verified by noting the listener's pid, running the file, and finding
    the port unbound.

    The tests that exercise ``_kill_stale_port_holder`` *itself* are exempted by
    name; they supply their own ``subprocess``/``os.kill`` fakes and would
    otherwise be asserting against this stub instead of the real function.
    """
    if "kill_stale_port_holder" in request.node.name:
        return
    monkeypatch.setattr(
        "muxplex.cli._kill_stale_port_holder", lambda port: None, raising=False
    )
