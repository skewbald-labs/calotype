"""The funnel (funnel.py): which requests count as which step, and that counting stays out of the way."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from calotype import funnel
from calotype.app import create_app
from calotype.funnel import FunnelCounter, classify
from calotype.ssrf import AddressPolicy
from calotype.storage import FUNNEL_EVENTS, FunnelDay, current_day

from .conftest import FakeRenderer, fake_resolver, stored_text
from .test_remote_mcp import call, rpc


def today(client: TestClient, store) -> FunnelDay:
    client.app.state.funnel.wait()
    return store.funnel_days([current_day()])[0]


def counted(client: TestClient, store) -> dict[str, int]:
    """Today's non-zero counts."""
    day = today(client, store)
    return {e: getattr(day, e) for e in FUNNEL_EVENTS if getattr(day, e)}


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# --- classify: the rules on their own ------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, status, key_sent, methods, expected",
    [
        ("POST", "/mcp", 200, False, {"initialize"}, ["looked"]),
        ("POST", "/mcp", 200, False, {"tools/list"}, ["looked"]),
        ("POST", "/mcp", 200, False, {"tools/call"}, ["tool_no_key"]),
        ("POST", "/mcp", 401, False, {"tools/call"}, ["tool_no_key"]),  # OAuth on: keyless call is a 401
        ("POST", "/mcp", 401, False, {"tools/list"}, []),
        ("POST", "/mcp", 200, False, {"initialize", "tools/call"}, ["looked", "tool_no_key"]),
        ("POST", "/mcp", 202, False, {"notifications/initialized"}, []),
        ("POST", "/mcp", 200, False, {"ping"}, []),
        ("POST", "/mcp", 200, True, {"initialize", "tools/call"}, []),  # with a key: a user, not a step
        ("POST", "/mcp", 403, False, {"initialize"}, []),  # refused before the SDK
        ("POST", "/mcp", 401, True, set(), ["bad_key"]),
        ("GET", "/v1/screenshot", 401, True, set(), ["bad_key"]),
        ("POST", "/v1/screenshot", 401, True, set(), ["bad_key"]),
        ("GET", "/v1/screenshot", 401, False, set(), []),  # no key at all
        ("GET", "/v1/usage", 401, True, set(), []),
        # OAuth by path family, whatever the answer: 404 today, 200 once oauth.py lands.
        ("GET", "/.well-known/oauth-protected-resource", 404, False, set(), ["oauth_probe"]),
        ("GET", "/.well-known/oauth-protected-resource", 200, False, set(), ["oauth_probe"]),
        ("GET", "/.well-known/oauth-protected-resource/mcp", 404, False, set(), ["oauth_probe"]),
        ("GET", "/.well-known/oauth-authorization-server", 200, False, set(), ["oauth_probe"]),
        ("GET", "/.well-known/openid-configuration/mcp", 404, False, set(), ["oauth_probe"]),
        ("GET", "/mcp/.well-known/openid-configuration", 404, False, set(), ["oauth_probe"]),
        ("GET", "/authorize", 302, False, set(), ["oauth_probe"]),
        ("POST", "/token", 400, False, set(), ["oauth_probe"]),
        ("POST", "/oauth/token", 200, False, set(), ["oauth_probe"]),
        ("POST", "/register", 201, False, set(), ["oauth_probe"]),  # dynamic client registration
        ("GET", "/register", 404, False, set(), ["key_miss"]),  # a sign-up page
        ("GET", "/v1/keys", 405, False, set(), ["key_miss"]),
        ("HEAD", "/signup", 404, False, set(), ["key_miss"]),
        ("GET", "/API/Keys/", 404, False, set(), ["key_miss"]),
        ("GET", "/login", 200, False, set(), []),  # a path that exists is not a miss
        ("GET", "/no-such-page", 404, False, set(), []),
        ("POST", "/v1/keys", 201, False, set(), ["key_made"]),
        ("POST", "/v1/keys", 429, False, set(), ["key_refused"]),
        ("POST", "/v1/keys", 400, False, set(), ["key_refused"]),
        ("GET", "/", 200, False, set(), []),
        ("GET", "/v1/screenshot", 200, True, set(), []),
        ("POST", "/mcp", 0, False, {"initialize"}, []),  # the app raised before answering
    ],
)
def test_classify(method, path, status, key_sent, methods, expected):
    assert classify(method, path, status, key_sent=key_sent, rpc_methods=methods) == expected


def test_every_step_classify_returns_is_a_stored_step():
    assert set(FUNNEL_EVENTS) >= {"looked", "tool_no_key", "bad_key", "oauth_probe", "key_miss", "key_made",
                                  "key_refused", "first_capture"}


# --- through the app ------------------------------------------------------------------------


def test_keyless_mcp_look_and_call(client, store, fake_renderer):
    assert rpc(client, None, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "crawler", "version": "1"},
    }).status_code == 200
    assert rpc(client, None, "tools/list").status_code == 200
    assert rpc(client, None, "ping").status_code == 200
    assert counted(client, store) == {"looked": 2}
    r = call(client, None, {"url": "https://example.com"})
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    assert counted(client, store) == {"looked": 2, "tool_no_key": 1}
    assert fake_renderer.calls == []


def test_mcp_with_a_key_is_not_a_funnel_step(client, store, free_key):
    rpc(client, free_key, "initialize", {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"},
    })
    rpc(client, free_key, "tools/list")
    assert counted(client, store) == {}


def test_bad_keys_on_the_api_and_mcp(client, store, free_key):
    url = "/v1/screenshot?url=https://example.com"
    assert client.get(url, headers=bearer("cal_wrong")).status_code == 401
    assert client.get(url + "&access_key=cal_wrong").status_code == 401
    assert client.post("/v1/screenshot", json={"url": "https://example.com"},
                       headers={"Authorization": "Basic abc"}).status_code == 401
    assert rpc(client, "cal_wrong", "tools/list").status_code == 401
    _, key = store.create_key("free")
    raw_revoked, revoked = store.create_key("free")
    store.revoke_key(revoked.id)
    assert client.get(url, headers=bearer(raw_revoked)).status_code == 401
    assert counted(client, store) == {"bad_key": 5}
    # No key at all is not a bad key, and other endpoints do not count.
    assert client.get(url).status_code == 401
    assert client.get("/v1/usage", headers=bearer("cal_wrong")).status_code == 401
    assert counted(client, store) == {"bad_key": 5}


def test_oauth_probes_count_by_path_whatever_the_answer(client, store):
    for method, path in [
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/openid-configuration"),
        ("GET", "/authorize?client_id=x&redirect_uri=http://localhost"),
        ("POST", "/token"),
        ("POST", "/register"),
    ]:
        client.request(method, path)
    assert counted(client, store) == {"oauth_probe": 7}


def test_key_misses(client, store):
    assert client.get("/v1/keys").status_code == 405
    for path in ("/signup", "/register", "/login", "/api/keys"):
        assert client.get(path).status_code == 404
    assert client.get("/nothing-here").status_code == 404
    assert client.get("/pricing").status_code == 200
    assert counted(client, store) == {"key_miss": 5}


def test_keys_made_and_refused(client, store):
    assert client.post("/v1/keys").status_code == 201
    assert client.post("/v1/keys", json={"ref": "try"}).status_code == 201
    assert client.post("/v1/keys", json={"email": "not an address"}).status_code == 400
    assert client.post("/v1/keys").status_code == 201
    assert client.post("/v1/keys").status_code == 429  # 3 an hour from one address
    assert counted(client, store) == {"key_made": 3, "key_refused": 2}


def test_first_capture_counts_once_per_key(client, store, free_key):
    url = "/v1/screenshot?url=https://example.com"
    assert client.get(url, headers=bearer(free_key)).status_code == 200
    assert client.get(url, headers=bearer(free_key)).status_code == 200
    assert counted(client, store) == {"first_capture": 1}
    raw, _ = store.create_key("free")
    assert client.post("/v1/screenshot", json={"url": "https://example.com"}, headers=bearer(raw)).status_code == 200
    assert counted(client, store) == {"first_capture": 2}


def test_first_capture_through_the_mcp_tool(client, store):
    raw, _ = store.create_key("free")
    r = call(client, raw, {"url": "https://example.com"})
    assert r.status_code == 200 and not r.json()["result"].get("isError"), r.text
    call(client, raw, {"url": "https://example.com"})
    assert counted(client, store) == {"first_capture": 1}


def test_internal_keys_and_failed_captures_are_not_first_captures(client, store, fake_renderer):
    from calotype.capture import CaptureError

    url = "/v1/screenshot?url=https://example.com"
    raw_internal, _ = store.create_key("free", "operator", internal=True)
    assert client.get(url, headers=bearer(raw_internal)).status_code == 200
    raw, _ = store.create_key("free")
    fake_renderer.fail_with = CaptureError(422, "target_unreachable", "no")
    assert client.get(url, headers=bearer(raw)).status_code == 422
    assert counted(client, store) == {}


def test_counts_hold_no_url_key_user_agent_or_ip(client, store):
    raw, _ = store.create_key("free")
    secret = "https://example.com/private-path-9f3a"
    agent = "SomeAgent/9.9 (probe-7c1d)"
    headers = {"User-Agent": agent, "X-Forwarded-For": "198.51.100.77"}
    client.get(f"/v1/screenshot?url={secret}", headers={**headers, **bearer(raw)})
    client.get(f"/v1/screenshot?url={secret}", headers={**headers, **bearer("cal_wrong_9d2e")})
    client.get("/.well-known/oauth-protected-resource/private-path-9f3a", headers=headers)
    client.get("/signup?email=someone@example.org", headers=headers)
    call(client, None, {"url": secret})
    assert counted(client, store) == {"first_capture": 1, "bad_key": 1, "oauth_probe": 1, "key_miss": 1,
                                      "tool_no_key": 1}
    text = stored_text(store)
    for value in ("private-path-9f3a", "probe-7c1d", "198.51.100.77", "cal_wrong_9d2e", raw, "someone@example.org"):
        assert value not in text


# --- counting never breaks or slows a request -----------------------------------------------


class BrokenFunnelStore:
    """Wraps a store; funnel writes raise or block, everything else works."""

    def __init__(self, inner, *, block: threading.Event | None = None):
        self._inner = inner
        self._block = block
        self.calls = 0

    def record_funnel(self, day, event):
        self.calls += 1
        if self._block is not None:
            self._block.wait(10)
            return
        raise RuntimeError("storage is down")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def make_client(settings, store):
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=FakeRenderer())
    return TestClient(app)


def test_a_failing_count_never_fails_the_request(settings, sqlite_store, capsys):
    broken = BrokenFunnelStore(sqlite_store)
    with make_client(settings, broken) as c:
        assert c.post("/v1/keys").status_code == 201
        assert c.get("/signup").status_code == 404
        assert rpc(c, None, "tools/list").status_code == 200
        c.app.state.funnel.wait()
    assert broken.calls == 3
    assert "could not count a funnel step" in capsys.readouterr().out


def test_a_slow_store_never_delays_the_response(settings, sqlite_store):
    gate = threading.Event()
    slow = BrokenFunnelStore(sqlite_store, block=gate)
    try:
        with make_client(settings, slow) as c:
            started = time.monotonic()
            for _ in range(5):
                assert c.get("/v1/keys").status_code == 405
            assert time.monotonic() - started < 3  # each write would block for 10 s
            gate.set()
            c.app.state.funnel.wait()
    finally:
        gate.set()
    assert slow.calls == 5


def test_counter_drops_counts_rather_than_queue_without_end(caplog):
    gate = threading.Event()
    written: list[str] = []

    class Store:
        def record_funnel(self, day, event):
            gate.wait(10)
            written.append(event)

    counter = FunnelCounter(lambda: Store(), max_workers=1, max_pending=3)
    try:
        for _ in range(10):
            counter.count(["looked"])
        assert "funnel counts dropped" in caplog.text
    finally:
        gate.set()
        counter.wait()
        counter.close()
    assert written == ["looked"] * 3


def test_counter_without_storage_does_nothing():
    counter = FunnelCounter(lambda: None)
    counter.count(["looked"])
    counter.count([])
    counter.wait()
    counter.close()


def test_mark_never_raises():
    funnel.mark(object(), "first_capture")  # no .state: logged, not raised
