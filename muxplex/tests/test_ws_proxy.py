"""
Comprehensive tests for the WebSocket proxy in muxplex/main.py.
"""

import asyncio
import inspect
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from muxplex.auth import create_session_cookie
from muxplex.main import app, terminal_ws_proxy


# ---------------------------------------------------------------------------
# Polling helper — deterministic alternative to time.sleep() for async relay
# ---------------------------------------------------------------------------


def _wait_for(condition, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll *condition()* until it returns True or *timeout* seconds elapses.

    Returns True if the condition was met, False on timeout.
    Using a polling loop instead of a fixed sleep makes relay tests deterministic:
    on fast machines the loop exits as soon as the relay completes; on slow machines
    it waits up to *timeout* seconds rather than racing against a fixed 200ms budget.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False  # pragma: no cover — timeout branch only on pathological machines


# ---------------------------------------------------------------------------
# autouse fixture — redirect state/PID files to tmp_path, mock startup side-effects
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def patch_startup_and_state(tmp_path, monkeypatch):
    """Redirect state/PID files to tmp_path, mock kill_orphan_ttyd, replace _poll_loop with no-op."""
    # Redirect state files
    tmp_state_dir = tmp_path / "state"
    tmp_state_path = tmp_state_dir / "state.json"
    monkeypatch.setattr("muxplex.state.STATE_DIR", tmp_state_dir)
    monkeypatch.setattr("muxplex.state.STATE_PATH", tmp_state_path)

    # Valid ttyd lease for all relay tests. Tests that exercise a mismatch
    # override the state; no unit test touches a real ttyd listener.
    from muxplex.state import empty_state, save_state
    state = empty_state()
    state["active_session"] = "test-session"
    state["active_connection_id"] = "test-lease"
    save_state(state)
    monkeypatch.setattr("muxplex.main._ttyd_transition_lock", asyncio.Lock())
    monkeypatch.setattr("muxplex.main._ttyd_is_listening", lambda: True)
    # WS validates tmux membership even if the ttyd listener is still alive.
    monkeypatch.setattr("muxplex.main.get_session_list", lambda: ["test-session"])

    # Redirect PID files
    tmp_pid_dir = tmp_path / "ttyd"
    tmp_pid_path = tmp_pid_dir / "ttyd.pid"
    monkeypatch.setattr("muxplex.ttyd.TTYD_PID_DIR", tmp_pid_dir)
    monkeypatch.setattr("muxplex.ttyd.TTYD_PID_PATH", tmp_pid_path)

    # Mock kill_orphan_ttyd so startup doesn't touch real processes (must be async)
    async def _mock_kill_orphan():
        return False

    monkeypatch.setattr("muxplex.main.kill_orphan_ttyd", _mock_kill_orphan)

    # Replace _poll_loop with a no-op so tests don't spin up real poll cycles
    async def noop_poll_loop() -> None:
        pass

    monkeypatch.setattr("muxplex.main._poll_loop", noop_poll_loop)


# ---------------------------------------------------------------------------
# Helper — create TestClient with valid session cookie
# ---------------------------------------------------------------------------


def _make_authed_client():
    """Creates TestClient with valid session cookie."""
    from muxplex.main import _auth_secret, _auth_ttl

    cookie = create_session_cookie(_auth_secret, _auth_ttl)
    client = TestClient(app)
    client.cookies.set("muxplex_session", cookie)
    return client


# ---------------------------------------------------------------------------
# FakeTtydWs — mock ttyd WebSocket for relay testing
# ---------------------------------------------------------------------------


class FakeTtydWs:
    """Mock ttyd WebSocket that stores sent messages and yields pre-loaded responses.

    Supports send(), close(), async iterator, and async context manager.
    """

    def __init__(self, responses=None):
        self.sent = []
        self._responses = list(responses or [])
        self._closed = False

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self._closed = True

    def __aiter__(self):
        return self._async_gen()

    async def _async_gen(self):
        for msg in self._responses:
            yield msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False



class HoldOpenTtydWs(FakeTtydWs):
    """FakeTtydWs whose upstream stream stays open until explicitly released.

    Needed for any test that drives the browser→upstream direction: the proxy
    now tears down as soon as EITHER direction ends (plan item 3.4), so a fake
    whose message stream exhausts immediately would close the relay before the
    browser ever sends.  A real ttyd keeps its stream open.
    """

    def __init__(self):
        super().__init__()
        self._release = threading.Event()

    async def _async_gen(self):
        while not self._release.is_set():
            await asyncio.sleep(0.01)
        return
        yield  # pragma: no cover — makes this an async generator

    def release(self):
        self._release.set()


# ---------------------------------------------------------------------------
# Test 1: regression — proxy source must use receive(), not receive_bytes()
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: ttyd liveness check before websocket.accept
# ---------------------------------------------------------------------------


def test_ttyd_is_listening_function_exists():
    """_ttyd_is_listening() must exist in main.py (TCP probe helper)."""
    # Import will fail if function doesn't exist — that IS the failing test
    from muxplex.main import _ttyd_is_listening  # noqa: F401

    assert callable(_ttyd_is_listening)


def test_ws_proxy_checks_ttyd_before_accepting():
    """terminal_ws_proxy must check _ttyd_is_listening BEFORE websocket.accept.

    Root cause of the reconnect loop: the proxy called websocket.accept() before
    checking if ttyd was alive. The browser's 'open' event fired immediately,
    resetting _reconnectAttempts to 0. The counter bounced 0→1→0→1 forever so
    the client-side /connect POST (at >= 2 attempts) never fired.

    Fix: check _ttyd_is_listening() first. If not listening, auto-spawn ttyd
    THEN accept — so the browser only gets 'open' when ttyd is actually ready.
    """
    source = inspect.getsource(terminal_ws_proxy)
    # Use "await websocket.accept" to avoid matching the docstring mention
    accept_idx = source.index("await websocket.accept")
    ttyd_check_idx = source.index("_ttyd_is_listening")
    assert ttyd_check_idx < accept_idx, (
        "_ttyd_is_listening() must be checked BEFORE await websocket.accept() — "
        "proxy must not accept the browser WS until ttyd is confirmed alive"
    )


def test_ws_proxy_auto_spawns_ttyd_when_dead(monkeypatch):
    """WS proxy must call spawn_ttyd when _ttyd_is_listening returns False."""
    import asyncio

    spawn_calls = []

    async def mock_spawn_ttyd(name: str):
        spawn_calls.append(name)

    async def mock_kill_ttyd():
        pass

    async def mock_sleep(_delay: float):
        pass  # no-op so tests don't actually wait

    # Patch _ttyd_is_listening to report ttyd as dead
    monkeypatch.setattr("muxplex.main._ttyd_is_listening", lambda: False)
    # Patch spawn_ttyd / kill_ttyd so tests don't touch real processes
    monkeypatch.setattr("muxplex.main.spawn_ttyd", mock_spawn_ttyd)
    monkeypatch.setattr("muxplex.main.kill_ttyd", mock_kill_ttyd)
    # asyncio.sleep is called after spawn — patch to be a no-op
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    # Provide a fake websockets.connect that immediately closes (no real ttyd)
    fake_ws = FakeTtydWs(responses=[])
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    # Patch load_state to return state with active_session
    monkeypatch.setattr(
        "muxplex.main.load_state",
        lambda: {"active_session": "test-session", "active_connection_id": "test-lease",
                 "sessions": {}, "session_order": []},
    )
    # The session must be KNOWN for the proxy to respawn for it.  Pinned
    # explicitly rather than relying on the module-global session cache, which
    # other tests mutate -- and which made this test's precondition invisible.
    monkeypatch.setattr("muxplex.main.get_session_list", lambda: ["test-session"])

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as _:
            pass

    assert spawn_calls == ["test-session"], (
        "spawn_ttyd must be called with active_session when ttyd is not listening"
    )


def test_ws_proxy_refuses_to_spawn_ttyd_for_vanished_session(monkeypatch):
    """The proxy must NOT resurrect ttyd for a session that no longer exists.

    ttyd is a server, not a wrapper: spawned without --once it keeps listening
    and re-forks `tmux attach` for every client.  Pointed at a destroyed
    session, each fork prints "can't find session" and dies -- so an unguarded
    respawn here makes every browser reconnect regenerate the doomed process,
    a loop that sustains itself with no frontend involvement at all.

    connect_session has always had this check; this path did not.
    """
    spawn_calls: list[str] = []

    async def mock_spawn_ttyd(session_name: str):
        spawn_calls.append(session_name)

    async def mock_kill_ttyd():
        pass

    async def mock_sleep(_delay: float):
        pass

    monkeypatch.setattr("muxplex.main._ttyd_is_listening", lambda: False)
    monkeypatch.setattr("muxplex.main.spawn_ttyd", mock_spawn_ttyd)
    monkeypatch.setattr("muxplex.main.kill_ttyd", mock_kill_ttyd)
    monkeypatch.setattr(asyncio, "sleep", mock_sleep)

    fake_ws = FakeTtydWs(responses=[])
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    # State still names the dead session; the live session list no longer has it.
    monkeypatch.setattr(
        "muxplex.main.load_state",
        lambda: {"active_session": "ghost", "active_connection_id": "test-lease",
                 "sessions": {}, "session_order": []},
    )
    monkeypatch.setattr(
        "muxplex.main.get_session_list", lambda: ["alive-1", "alive-2"]
    )

    async def missing_tmux(*args):
        raise RuntimeError("ghost does not exist")

    monkeypatch.setattr("muxplex.main.run_tmux", missing_tmux)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=ghost&connection_id=test-lease") as ws:
            assert ws.receive()["code"] == 4404

    assert spawn_calls == [], (
        "spawn_ttyd must NOT be called for a session missing from the live list"
    )


def test_ws_vanished_after_poll_cleared_active_lease_reports_4404(monkeypatch):
    """A dead tmux session is ended, not a stale tab, even after poll reaping."""
    from muxplex.state import empty_state, save_state
    save_state(empty_state())  # poll already cleared name and connection lease
    monkeypatch.setattr("muxplex.main.get_session_list", lambda: ["still-live"])

    async def missing_tmux(*args):
        raise RuntimeError("dead session does not exist")

    monkeypatch.setattr("muxplex.main.run_tmux", missing_tmux)
    attaches = []
    monkeypatch.setattr(
        "muxplex.main.websockets.connect",
        lambda *args, **kwargs: attaches.append(args[0]) or FakeTtydWs(responses=[]),
    )
    with _make_authed_client() as c:
        with c.websocket_connect(
            "/terminal/ws?expected_session=dead&connection_id=previous-lease"
        ) as ws:
            assert ws.receive()["code"] == 4404
    assert attaches == [], "a vanished session must not reach ttyd"


def test_ws_cache_miss_does_not_end_a_live_new_session(monkeypatch):
    """A live new session may not appear in the poll cache yet."""
    probes = []

    async def has_session(*args):
        probes.append(args)
        return ""  # tmux has-session exits successfully without output

    monkeypatch.setattr("muxplex.main.get_session_list", lambda: [])
    monkeypatch.setattr("muxplex.main.run_tmux", has_session)
    monkeypatch.setattr(
        "muxplex.main.websockets.connect",
        lambda *args, **kw: FakeTtydWs(responses=["live-output"]),
    )
    with _make_authed_client() as c:
        with c.websocket_connect(
            "/terminal/ws?expected_session=test-session&connection_id=test-lease"
        ) as ws:
            assert ws.receive_text() == "live-output"

    assert probes == [("has-session", "-t", "=test-session")]


def test_terminal_ws_proxy_does_not_use_receive_bytes():
    """Regression: receive_bytes() silently drops TEXT frames (like the ttyd auth token).

    terminal.js sends {"AuthToken": ""} as a TEXT WebSocket frame. The original
    proxy used receive_bytes() which fails on text frames, swallowed the exception,
    and exited — meaning ttyd never received the auth token, never started
    streaming, resulting in a permanent black screen and reconnect loop.

    The proxy MUST use receive() and dispatch on message type to handle both
    binary and text frames correctly.
    """
    source = inspect.getsource(terminal_ws_proxy)
    assert "receive_bytes" not in source, (
        "client_to_ttyd must not use receive_bytes() — silently drops text frames "
        'like the ttyd auth token {"AuthToken": ""}'
    )
    assert ".receive()" in source, (
        "client_to_ttyd must use receive() to handle both text and binary frames"
    )


# ---------------------------------------------------------------------------
# Tests 2–3: auth rejection
# ---------------------------------------------------------------------------


def test_ws_auth_rejection_no_cookie():
    """WebSocket from non-localhost without cookie is closed with code 4001."""
    # TestClient default host is "testclient" which is treated as non-localhost
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as _:
                pass
    assert exc_info.value.code == 4001


def test_ws_auth_rejection_invalid_cookie():
    """WebSocket from non-localhost with a tampered cookie is closed with code 4001."""
    with TestClient(app) as c:
        c.cookies.set("muxplex_session", "tampered.invalid.cookie.value")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as _:
                pass
    assert exc_info.value.code == 4001


# ---------------------------------------------------------------------------
# Test: Bearer token auth accepted
# ---------------------------------------------------------------------------


def test_ws_bearer_auth_accepted(monkeypatch):
    """WebSocket from non-localhost with valid Bearer federation key is NOT rejected with 4001.

    When a valid federation key is provided as 'Authorization: Bearer <key>',
    the WebSocket connection must be accepted (not closed with code 4001).
    """
    fed_key = "test-federation-secret-key"
    monkeypatch.setattr("muxplex.main._federation_key", fed_key)

    fake_ws = FakeTtydWs(responses=[])
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    # Connect without a session cookie but with a valid Bearer token.
    # Should NOT raise WebSocketDisconnect with code 4001.
    with TestClient(app) as c:
        # If Bearer auth is not implemented, this raises WebSocketDisconnect(code=4001)
        with c.websocket_connect(
            "/terminal/ws?expected_session=test-session&connection_id=test-lease",
            headers={"Authorization": f"Bearer {fed_key}"},
        ) as _ws:
            pass  # Successfully connected — auth was accepted


# ---------------------------------------------------------------------------
# Tests 4–5: browser → ttyd relay
# ---------------------------------------------------------------------------


def test_browser_text_relayed_to_ttyd(monkeypatch):
    """Text message from browser is forwarded to ttyd via FakeTtydWs.send()."""
    fake_ws = HoldOpenTtydWs()
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
            ws.send_text("hello from browser")
            _wait_for(lambda: "hello from browser" in fake_ws.sent)

    assert "hello from browser" in fake_ws.sent


def test_browser_bytes_relayed_to_ttyd(monkeypatch):
    """Binary message from browser is forwarded to ttyd via FakeTtydWs.send()."""
    fake_ws = HoldOpenTtydWs()
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
            ws.send_bytes(b"\x00\x01\x02 binary data")
            _wait_for(lambda: b"\x00\x01\x02 binary data" in fake_ws.sent)

    assert b"\x00\x01\x02 binary data" in fake_ws.sent


# ---------------------------------------------------------------------------
# Tests 6–7: ttyd → browser relay
# ---------------------------------------------------------------------------


def test_ttyd_text_relayed_to_browser(monkeypatch):
    """Text message from ttyd is forwarded to browser via websocket.send_text()."""
    fake_ws = FakeTtydWs(responses=["hello from ttyd"])
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
            msg = ws.receive_text()
    assert msg == "hello from ttyd"


def test_ttyd_bytes_relayed_to_browser(monkeypatch):
    """Binary message from ttyd is forwarded to browser via websocket.send_bytes()."""
    fake_ws = FakeTtydWs(responses=[b"\xde\xad\xbe\xef binary"])
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
            msg = ws.receive_bytes()
    assert msg == b"\xde\xad\xbe\xef binary"


# ---------------------------------------------------------------------------
# Test 8: ttyd close propagates to browser
# ---------------------------------------------------------------------------


def test_ttyd_close_propagates_to_browser(monkeypatch):
    """When ttyd exhausts its messages, the proxy cleans up and closes the browser WS."""
    fake_ws = FakeTtydWs(responses=[])  # no responses — exhausts immediately
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as _:
            # FakeTtydWs has no responses so ttyd_to_client exhausts immediately.
            # Exiting the context manager closes the browser WS, which causes
            # client_to_ttyd to complete, gather finishes, and the proxy
            # finally-block calls fake_ws.close().
            pass

    # fake_ws should have been closed when the async-with block exited
    assert fake_ws._closed


# ---------------------------------------------------------------------------
# Test 9: ttyd unreachable closes browser WS
# ---------------------------------------------------------------------------


def test_ttyd_unreachable_does_not_accept_browser_before_upstream(monkeypatch):
    """Do not report a successful socket until ttyd is actually attached."""

    def mock_connect_raises(*args, **kwargs):
        raise OSError("Connection refused — ttyd not running")

    monkeypatch.setattr("muxplex.main.websockets.connect", mock_connect_raises)

    with _make_authed_client() as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with c.websocket_connect(
                "/terminal/ws?expected_session=test-session&connection_id=test-lease"
            ):
                pass
    assert exc.value.code != 4001, "not an authentication rejection"


# ---------------------------------------------------------------------------
# Test 10: concurrent sessions don't interfere
# ---------------------------------------------------------------------------


def test_concurrent_ws_sessions(monkeypatch):
    """Two simultaneous proxy sessions relay to separate FakeTtydWs instances."""
    # Create two separate FakeTtydWs instances, one per connection
    ws_pool = [HoldOpenTtydWs(), HoldOpenTtydWs()]
    call_count = 0
    lock = threading.Lock()

    def mock_connect(*args, **kwargs):
        nonlocal call_count
        with lock:
            idx = call_count % len(ws_pool)
            call_count += 1
        return ws_pool[idx]

    monkeypatch.setattr("muxplex.main.websockets.connect", mock_connect)

    errors = []

    with _make_authed_client() as c:

        def send_msg(text):
            try:
                with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
                    ws.send_text(text)
                    _wait_for(lambda: text in ws_pool[0].sent + ws_pool[1].sent)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=send_msg, args=("session_one_msg",))
        t2 = threading.Thread(target=send_msg, args=("session_two_msg",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

    assert not errors, f"Concurrent sessions raised errors: {errors}"

    # Both messages must have been relayed (one to each fake_ws)
    all_sent = ws_pool[0].sent + ws_pool[1].sent
    assert "session_one_msg" in all_sent
    assert "session_two_msg" in all_sent


# ---------------------------------------------------------------------------
# Task-11: federation WebSocket proxy route
# ---------------------------------------------------------------------------


def test_federation_ws_proxy_route_exists():
    """App must have a WebSocket route at /federation/{device_id}/terminal/ws."""
    from starlette.routing import WebSocketRoute

    ws_routes = [r for r in app.routes if isinstance(r, WebSocketRoute)]
    paths = [r.path for r in ws_routes]
    assert "/federation/{device_id}/terminal/ws" in paths, (
        "App must have a WebSocket route at /federation/{device_id}/terminal/ws"
    )


def test_federation_ws_proxy_uses_ssl_context_for_wss():
    """Federation WS proxy must pass an SSL context when connecting via wss://.

    Self-signed certs on remote instances (cortex, spark-2, etc.) fail the
    default SSL verification in websockets.connect().  The proxy must build an
    SSLContext with CERT_NONE for wss:// URLs — the same fix already applied
    to the httpx client (verify=False) but for the websockets library.
    """
    from muxplex.main import federation_terminal_ws_proxy

    source = inspect.getsource(federation_terminal_ws_proxy)
    assert "ssl" in source and ("CERT_NONE" in source or "ssl_context" in source), (
        "Federation WS proxy must configure an SSL context (CERT_NONE / ssl_context) "
        "for self-signed cert support on wss:// connections"
    )


# ---------------------------------------------------------------------------
# Plan item 3.4 — FIRST_COMPLETED teardown
#
# The proxy used to `asyncio.gather(client_to_ttyd(), ttyd_to_client())`, which
# waits for BOTH.  `client_to_ttyd` blocks in `await websocket.receive()` until
# the browser acts, so if ttyd died while an idle tab was open the handler task,
# both coroutines, the accepted browser WS and the upstream connection all
# stayed resident — one stranded connection per idle tab per ttyd restart.
# ---------------------------------------------------------------------------


def _run_with_deadline(fn, timeout: float = 5.0):
    """Run *fn* in a daemon thread; return (finished, box) where box holds results."""
    box: dict = {}

    def _target():
        try:
            fn(box)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted on by caller
            box["exc"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    return (not thread.is_alive()), box


def test_upstream_end_tears_down_proxy_while_browser_is_idle(monkeypatch):
    """When ttyd ends first, the proxy must tear down without waiting on the browser.

    The browser never sends a frame, so `client_to_ttyd` is parked in
    `websocket.receive()`.  With `gather` the handler would hang forever and the
    browser would never see a close frame; with FIRST_COMPLETED it is cancelled
    and the handler returns.
    """
    fake_ws = FakeTtydWs(responses=[])  # upstream exhausts immediately
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    def body(box):
        with _make_authed_client() as c:
            with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
                # Deliberately send NOTHING — this is the idle-tab case.
                box["frame"] = ws.receive()

    finished, box = _run_with_deadline(body)

    assert finished, (
        "proxy did not tear down after the upstream ended — the browser-side "
        "receive() is still parked (regression: gather instead of FIRST_COMPLETED)"
    )
    frame = box.get("frame")
    assert frame is not None and frame.get("type") == "websocket.close", (
        f"expected a close frame from the proxy, got {frame!r} / {box.get('exc')!r}"
    )
    assert fake_ws._closed, "upstream connection must be closed on teardown"


def test_client_disconnect_still_closes_both_sides(monkeypatch):
    """Normal case unchanged: browser closes the tab → both sides are closed.

    Here the *upstream* is the survivor that must be cancelled.
    """
    fake_ws = HoldOpenTtydWs()
    monkeypatch.setattr("muxplex.main.websockets.connect", lambda *a, **kw: fake_ws)

    def body(box):
        with _make_authed_client() as c:
            with c.websocket_connect("/terminal/ws?expected_session=test-session&connection_id=test-lease") as ws:
                ws.send_text("ping")
                _wait_for(lambda: "ping" in fake_ws.sent)
            # Context exit disconnects the browser.
            box["sent"] = list(fake_ws.sent)
            box["closed"] = _wait_for(lambda: fake_ws._closed)

    finished, box = _run_with_deadline(body)

    assert finished, "client-disconnect teardown hung"
    assert box.get("exc") is None, f"unexpected error: {box.get('exc')!r}"
    assert "ping" in box.get("sent", []), "relay must still work in the normal case"
    assert box.get("closed"), (
        "upstream must be closed after the browser disconnects (the still-running "
        "upstream reader must be cancelled, not awaited forever)"
    )


def test_ws_proxies_use_first_completed_not_gather():
    """Both proxies must not fall back to gather()-ing the two relay directions."""
    from muxplex.main import _relay_until_either_ends, federation_terminal_ws_proxy

    for fn in (terminal_ws_proxy, federation_terminal_ws_proxy):
        source = inspect.getsource(fn)
        assert "asyncio.gather(" not in source, (
            f"{fn.__name__} must not gather() both relay directions — gather waits "
            "for BOTH and strands idle connections when the upstream dies"
        )
        assert "_relay_until_either_ends(" in source, (
            f"{fn.__name__} must use the FIRST_COMPLETED relay helper"
        )

    helper_src = inspect.getsource(_relay_until_either_ends)
    assert "FIRST_COMPLETED" in helper_src
    assert ".cancel()" in helper_src, "the survivor must be cancelled"
    assert "return_exceptions=True" in helper_src, (
        "the cancelled task must be awaited so no pending task is left behind"
    )

# ---------------------------------------------------------------------------
# P1: tab A's stale connection is refused after tab B changes the shared ttyd.
# ---------------------------------------------------------------------------


def test_stale_ws_cannot_attach_to_other_tabs_ttyd_after_connect_ack(monkeypatch):
    from muxplex.state import load_state

    events = []

    async def mock_kill():
        events.append("kill")

    async def mock_spawn(name):
        events.append("spawn:" + name)

    monkeypatch.setattr("muxplex.main.kill_ttyd", mock_kill)
    monkeypatch.setattr("muxplex.main.spawn_ttyd", mock_spawn)
    monkeypatch.setattr("muxplex.main.get_session_list", lambda: ["alpha", "beta"])

    attached = []
    fake_ws = FakeTtydWs(responses=["beta-output"])

    def fake_connect(*args, **kwargs):
        attached.append(args[0])
        return fake_ws

    monkeypatch.setattr("muxplex.main.websockets.connect", fake_connect)

    with _make_authed_client() as c:
        # A's initial /connect succeeds but it has NOT attached a WebSocket yet.
        first = c.post("/api/sessions/alpha/connect")
        assert first.status_code == 200
        lease_a = first.json()["connection_id"]
        # A second tab switches the single shared ttyd to beta first.
        second = c.post("/api/sessions/beta/connect")
        assert second.status_code == 200
        lease_b = second.json()["connection_id"]
        assert lease_a != lease_b

        with c.websocket_connect(
            f"/terminal/ws?expected_session=alpha&connection_id={lease_a}"
        ) as ws_a:
            assert ws_a.receive()["code"] == 4409
        assert attached == [], "A must never attach to beta's ttyd"

        # A valid B still attaches and sees B's upstream terminal data.
        with c.websocket_connect(
            f"/terminal/ws?expected_session=beta&connection_id={lease_b}"
        ) as ws_b:
            assert ws_b.receive_text() == "beta-output"
        assert len(attached) == 1
        state = load_state()
        assert (state["active_session"], state["active_connection_id"]) == (
            "beta", lease_b
        )


def test_ttyd_lease_prevents_aba_reuse_of_same_session_name(monkeypatch):
    from muxplex.state import load_state

    async def mock_kill():
        pass

    async def mock_spawn(name):
        pass

    monkeypatch.setattr("muxplex.main.kill_ttyd", mock_kill)
    monkeypatch.setattr("muxplex.main.spawn_ttyd", mock_spawn)
    monkeypatch.setattr("muxplex.main.get_session_list", lambda: ["alpha", "beta"])

    attached = []
    monkeypatch.setattr(
        "muxplex.main.websockets.connect",
        lambda *args, **kw: attached.append(args[0]) or FakeTtydWs(responses=[]),
    )

    with _make_authed_client() as c:
        first = c.post("/api/sessions/alpha/connect").json()["connection_id"]
        c.post("/api/sessions/beta/connect")
        second = c.post("/api/sessions/alpha/connect").json()["connection_id"]
        assert first != second
        assert load_state()["active_connection_id"] == second
        with c.websocket_connect(
            f"/terminal/ws?expected_session=alpha&connection_id={first}"
        ) as ws:
            assert ws.receive()["code"] == 4409
        assert attached == [], "stale A epoch must not attach to newer A process"


def test_federated_ws_validates_coordinator_lease_and_forwards_to_remote(monkeypatch):
    from muxplex.state import empty_state, save_state

    state = empty_state()
    state["active_session"] = "project x"
    state["active_remote_id"] = "dev-a"
    state["active_connection_id"] = "remote-lease"
    save_state(state)

    monkeypatch.setattr(
        "muxplex.main._lookup_remote_by_device_id",
        lambda device_id: {"url": "http://remote-host:8088", "key": "test-key"}
        if device_id == "dev-a" else None,
    )
    upstream_urls = []

    def fake_connect(url, **kwargs):
        upstream_urls.append(url)
        return FakeTtydWs(responses=["remote-output"])

    monkeypatch.setattr("muxplex.main.websockets.connect", fake_connect)

    with _make_authed_client() as c:
        with c.websocket_connect(
            "/federation/dev-a/terminal/ws?expected_session=project%20x&connection_id=wrong"
        ) as ws:
            assert ws.receive()["code"] == 4409
        assert upstream_urls == [], "wrong lease may not reach the remote"

        with c.websocket_connect(
            "/federation/dev-a/terminal/ws?expected_session=project%20x&connection_id=remote-lease"
        ) as ws:
            assert ws.receive_text() == "remote-output"
    assert len(upstream_urls) == 1
    assert "expected_session=project+x" in upstream_urls[0]
    assert "connection_id=remote-lease" in upstream_urls[0]


def test_missing_ttyd_lease_is_rejected_before_any_ttyd_connect(monkeypatch):
    attaches = []
    monkeypatch.setattr(
        "muxplex.main.websockets.connect",
        lambda *args, **kw: attaches.append(args) or FakeTtydWs(responses=[]),
    )
    with _make_authed_client() as c:
        with c.websocket_connect("/terminal/ws") as ws:
            assert ws.receive()["code"] == 4409
    assert attaches == []

@pytest.mark.parametrize("remote_code", [4409, 4404])
def test_federation_forwards_remote_terminal_close_code(monkeypatch, remote_code):
    """Forward stale-lease and ended-session close reasons from remote peers."""
    from muxplex.state import empty_state, save_state
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    state = empty_state()
    state["active_session"] = "alpha"
    state["active_remote_id"] = "node-b"
    state["active_connection_id"] = "lease-alpha"
    save_state(state)
    monkeypatch.setattr(
        "muxplex.main._lookup_remote_by_device_id",
        lambda rid: {"url": "http://node-b:8088", "key": "secret"}
        if rid == "node-b" else None,
    )

    class RemoteRejected(FakeTtydWs):
        async def _async_gen(self):
            # The remote accepted WS then rejected the lease before sending
            # ttyd output, as its local active session became beta.
            raise ConnectionClosedError(
                Close(remote_code, "Remote session unavailable"), None
            )
            yield  # mark as async generator

    monkeypatch.setattr(
        "muxplex.main.websockets.connect",
        lambda *args, **kwargs: RemoteRejected(),
    )
    with _make_authed_client() as c:
        with c.websocket_connect(
            "/federation/node-b/terminal/ws?expected_session=alpha&connection_id=lease-alpha"
        ) as ws:
            assert ws.receive()["code"] == remote_code
