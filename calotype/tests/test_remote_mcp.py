"""The remote MCP server at /mcp: streamable HTTP, the caller's key, one tool.

Requests are raw JSON-RPC over HTTP, as a client sends them, so these tests
check the wire format rather than the SDK's own client.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.capture import CaptureError
from calotype.ssrf import AddressPolicy
from calotype.storage import current_month

from .conftest import PNG_1X1, FakeRenderer, fake_resolver

ACCEPT = "application/json, text/event-stream"
MODERN = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
}


def headers(key: str | None, version: str | None = "2025-11-25", **extra) -> dict:
    h = {"Accept": ACCEPT, "Content-Type": "application/json", **extra}
    if key is not None:
        h["Authorization"] = f"Bearer {key}"
    if version is not None:
        h["MCP-Protocol-Version"] = version
    return h


def rpc(client: TestClient, key: str | None, method: str, params: dict | None = None, *, version="2025-11-25", **extra):
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers(key, version, **extra))


def call(client: TestClient, key: str | None, arguments: dict):
    return rpc(client, key, "tools/call", {"name": "screenshot", "arguments": arguments})


def tool_result(r) -> dict:
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 7 and "error" not in body, body
    return body["result"]


def used(client: TestClient, key: str) -> int:
    return client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"}).json()["used"]


# --- the handshake and the tool list ---------------------------------------------------


def test_initialize(client, free_key):
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
    r = rpc(client, free_key, "initialize", params, version=None)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    result = tool_result(r)
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "calotype" and result["serverInfo"]["websiteUrl"] == "https://calotype.dev"
    assert "tools" in result["capabilities"]
    assert "public web page" in result["instructions"]
    # Stateless: no session id to carry, so any instance can serve the next request.
    assert "mcp-session-id" not in r.headers
    # The initialized notification is accepted.
    note = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers(free_key)
    )
    assert note.status_code == 202


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_initialize_negotiates_older_versions(client, free_key, version):
    params = {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}
    assert tool_result(rpc(client, free_key, "initialize", params, version=None))["protocolVersion"] == version
    tools = tool_result(rpc(client, free_key, "tools/list", {}, version=version))["tools"]
    assert [t["name"] for t in tools] == ["screenshot"]


def test_the_docs_and_home_page_point_at_the_remote_endpoint(client):
    docs = client.get("/docs").text
    assert "/mcp" in docs and "--transport http" in docs and '"headers": {"Authorization": "Bearer cal_..."}' in docs
    assert "calotype.mcp_server" not in docs  # the stdio package is not published
    assert "/mcp" in client.get("/").text
    assert "AI agent" in client.get("/").text  # the disclosure stays


def test_ping(client, free_key):
    assert tool_result(rpc(client, free_key, "ping")) == {}


def test_registry_entry_matches_the_server():
    from pathlib import Path

    from calotype import __version__

    entry = json.loads((Path(__file__).parents[1] / "deploy" / "mcp-registry" / "server.json").read_text())
    assert entry["name"] == "dev.calotype/screenshot" and entry["version"] == __version__
    assert len(entry["description"]) <= 100
    (remote,) = entry["remotes"]
    # The registry's copy carries ?ref=mcp-registry so keys made through it are counted by source.
    assert remote == {**remote, "type": "streamable-http", "url": "https://calotype.dev/mcp?ref=mcp-registry"}
    (header,) = remote["headers"]
    assert header["name"] == "Authorization" and header["isRequired"] and header["isSecret"]
    assert header["value"] == "Bearer {api_key}" and "api_key" in header["variables"]


def test_tools_list(client, free_key):
    tools = tool_result(rpc(client, free_key, "tools/list", {}))["tools"]
    assert [t["name"] for t in tools] == ["screenshot"]
    tool = tools[0]
    assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["openWorldHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    schema = tool["inputSchema"]
    assert schema["required"] == ["url"]
    props = schema["properties"]
    assert set(props) == {"url", "full_page", "width", "height", "format", "dark_mode", "block_ads"}
    assert (props["width"]["minimum"], props["width"]["maximum"]) == (100, 3840)
    assert (props["height"]["minimum"], props["height"]["maximum"]) == (100, 2160)
    assert props["url"]["maxLength"] == 2048
    assert props["format"]["enum"] == ["png", "jpeg", "webp"]
    assert "ctx" not in props


# --- tools/call -------------------------------------------------------------------------


def test_tools_call_returns_an_image_and_counts(client, free_key, store, fake_renderer):
    result = tool_result(call(client, free_key, {"url": "https://example.com", "width": 1024}))
    assert result["isError"] is False
    image, text = result["content"]
    assert image["type"] == "image" and image["mimeType"] == "image/jpeg"
    assert base64.b64decode(image["data"]) == PNG_1X1
    assert text["type"] == "text" and "https://example.com" in text["text"] and "1x1 px" in text["text"]
    assert "1 of 100" in text["text"]
    # The same capture path: the parameters reached the renderer, with the tool's defaults.
    (params,) = fake_renderer.calls
    assert (params.url, params.width, params.format, params.block_ads, params.full_page) == (
        "https://example.com", 1024, "jpeg", True, False
    )
    # It counts toward the month's quota and the key's lifetime successes.
    assert used(client, free_key) == 1
    assert store.get_key(free_key).successful_screenshots == 1


def test_tools_call_passes_every_parameter(client, free_key, fake_renderer):
    args = {"url": "https://example.com", "full_page": True, "width": 800, "height": 600, "format": "webp",
            "dark_mode": True, "block_ads": False}
    result = tool_result(call(client, free_key, args))
    assert result["content"][0]["mimeType"] == "image/webp"
    p = fake_renderer.calls[0]
    assert (p.full_page, p.width, p.height, p.format, p.dark_mode, p.block_ads) == (True, 800, 600, "webp", True, False)


def test_tools_call_reports_the_final_url_and_page_status(settings, store, free_key):
    class Redirected(FakeRenderer):
        async def capture(self, params):
            result = await super().capture(params)
            return replace(result, final_url="https://example.com/landing", target_status=404)

    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=Redirected())
    with TestClient(app) as c:
        text = tool_result(call(c, free_key, {"url": "https://example.com"}))["content"][1]["text"]
    assert "Screenshot of https://example.com/landing" in text and "HTTP 404" in text


def test_tools_call_refuses_private_addresses(client, free_key, fake_renderer):
    for url in ("http://127.0.0.1/", "http://10.0.0.1/", "http://metadata.example/", "http://localhost/", "file:///etc/passwd"):
        result = tool_result(call(client, free_key, {"url": url}))
        assert result["isError"] is True, url
        assert result["content"][0]["text"].split(": ", 1)[1].startswith(("blocked_target", "invalid_scheme")), url
    assert fake_renderer.calls == []
    assert used(client, free_key) == 0


def test_tools_call_quota_exceeded(client, free_key, store, fake_renderer):
    key = store.get_key(free_key)
    for _ in range(100):
        store.try_consume(key.id, current_month(), 100)
    result = tool_result(call(client, free_key, {"url": "https://example.com"}))
    assert result["isError"] is True
    assert "quota_exceeded" in result["content"][0]["text"] and "100 screenshots" in result["content"][0]["text"]
    assert fake_renderer.calls == []
    assert used(client, free_key) == 100


def test_a_failed_capture_is_refunded(client, free_key, fake_renderer):
    fake_renderer.fail_with = CaptureError(504, "timeout", "The page did not load in time.")
    result = tool_result(call(client, free_key, {"url": "https://example.com"}))
    assert result["isError"] is True and "timeout" in result["content"][0]["text"]
    assert used(client, free_key) == 0


def test_an_unexpected_failure_is_refunded_too(client, free_key, fake_renderer):
    fake_renderer.fail_with = RuntimeError("boom")
    result = tool_result(call(client, free_key, {"url": "https://example.com"}))
    assert result["isError"] is True and "internal_error" in result["content"][0]["text"]
    assert "boom" not in result["content"][0]["text"]
    assert used(client, free_key) == 0


@pytest.mark.parametrize(
    "arguments",
    [{}, {"url": "https://example.com", "width": 5}, {"url": "https://example.com", "format": "gif"},
     {"url": "https://example.com", "height": 9999}, {"url": "x" * 2049}],
)
def test_invalid_arguments_are_a_tool_error(client, free_key, fake_renderer, arguments):
    result = tool_result(call(client, free_key, arguments))
    assert result["isError"] is True
    assert fake_renderer.calls == []
    assert used(client, free_key) == 0


def test_the_2026_envelope_works_with_the_key(client, free_key):
    params = {"name": "screenshot", "arguments": {"url": "https://example.com"}, "_meta": MODERN_META}
    r = rpc(client, free_key, "tools/call", params, version=MODERN, **{"Mcp-Method": "tools/call", "Mcp-Name": "screenshot"})
    result = tool_result(r)
    assert result["isError"] is False and result["content"][0]["type"] == "image"
    assert used(client, free_key) == 1


# --- auth, origin and methods -------------------------------------------------------------


def test_without_a_key_the_tool_is_listed_and_a_call_says_how_to_get_one(client, fake_renderer):
    # Directories and inspectors connect with no key, so the handshake and the list work.
    init = rpc(client, None, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "t", "version": "1"}}, version=None)
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "calotype"
    listed = rpc(client, None, "tools/list")
    assert [t["name"] for t in listed.json()["result"]["tools"]] == ["screenshot"]
    assert rpc(client, None, "ping").status_code == 200
    # A call is a tool error with the way to a free key, and nothing is captured.
    result = tool_result(call(client, None, {"url": "https://example.com"}))
    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "Authorization: Bearer" in message and "https://calotype.dev/#key" in message and "/v1/keys" in message
    assert fake_renderer.calls == []


def test_invalid_or_revoked_key_is_401(client, store, fake_renderer):
    r = call(client, "cal_not_a_real_key", {"url": "https://example.com"})
    assert r.status_code == 401 and r.json()["error"]["data"]["code"] == "invalid_key"
    assert "/#key" in r.json()["error"]["message"]
    raw, key = store.create_key("free")
    store.revoke_key(key.id)
    assert call(client, raw, {"url": "https://example.com"}).status_code == 401
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={**headers(None), "Authorization": "Basic abc"})
    assert r.status_code == 401
    assert fake_renderer.calls == []


def test_key_in_the_query_string_is_not_accepted(client, free_key, fake_renderer):
    params = {"name": "screenshot", "arguments": {"url": "https://example.com"}}
    r = client.post(f"/mcp?access_key={free_key}", json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                          "params": params}, headers=headers(None))
    # Treated as no key at all: the call says how to send one, and nothing is captured or counted.
    assert tool_result(r)["isError"] is True and "Authorization: Bearer" in tool_result(r)["content"][0]["text"]
    assert fake_renderer.calls == [] and used(client, free_key) == 0


def test_get_and_delete_are_405(client, free_key):
    for method in ("GET", "DELETE"):
        r = client.request(method, "/mcp", headers=headers(free_key))
        assert r.status_code == 405 and r.headers["allow"] == "POST", method


def test_foreign_origin_is_refused(client, free_key):
    r = rpc(client, free_key, "ping", Origin="https://evil.example")
    assert r.status_code == 403
    assert tool_result(rpc(client, free_key, "ping", Origin="https://calotype.dev")) == {}


@pytest.mark.parametrize("origin", ["https://claude.ai", "https://claude.com", "https://chatgpt.com", "https://chat.openai.com/"])
def test_the_web_apps_of_known_mcp_clients_may_name_themselves_in_origin(client, free_key, origin):
    # Claude and ChatGPT connect from their own servers and may send their web app's Origin.
    assert tool_result(rpc(client, free_key, "ping", Origin=origin)) == {}


@pytest.mark.parametrize("origin", ["https://claude.ai.evil.example", "http://claude.ai", "https://evil.example/https://claude.ai", "null"])
def test_look_alike_origins_are_still_refused(client, free_key, origin):
    assert rpc(client, free_key, "ping", Origin=origin).status_code == 403


def test_logs_name_the_key_by_id_only(client, free_key, store, caplog):
    caplog.set_level("INFO")
    tool_result(call(client, free_key, {"url": "https://example.com/secret/path?q=1"}))
    lines = [{"message": r.getMessage(), **getattr(r, "fields", {})} for r in caplog.records]
    key_id = store.get_key(free_key).id
    access = [l for l in lines if l["message"] == "request" and l.get("path") == "/mcp"]
    assert access and access[-1]["key_id"] == key_id and access[-1]["target"] == "https://example.com"
    shot = [l for l in lines if l["message"] == "screenshot"]
    assert shot and shot[-1]["key_id"] == key_id
    text = json.dumps(lines, default=str)
    assert free_key not in text and "secret/path" not in text


def mcp_lines(caplog) -> list[dict]:
    return [r.fields for r in caplog.records if r.getMessage() == "request" and r.fields["path"] == "/mcp"]


def initialize(client, key, client_info, **extra):
    params = {"protocolVersion": "2025-11-25", "capabilities": {}}
    if client_info is not None:
        params["clientInfo"] = client_info
    return rpc(client, key, "initialize", params, version=None, **extra)


def test_an_initialize_logs_the_clients_own_name_and_version(client, free_key, caplog):
    caplog.set_level("INFO")
    r = initialize(client, None, {"name": "Claude Code", "version": "2.1.0 (beta)"}, **{"User-Agent": "claude-code/2.1.0 (cli)"})
    assert tool_result(r)["serverInfo"]["name"] == "calotype"
    line = mcp_lines(caplog)[-1]
    assert (line["mcp_client"], line["mcp_client_version"]) == ("claude-code", "2.1.0-beta")
    assert (line["agent"], line["agent_kind"], line["status"], line["key_id"]) == ("claude-code", "tool", 200, None)
    # The same with a key: the body goes to the SDK by another path.
    tool_result(initialize(client, free_key, {"name": "cursor-vscode", "version": "1.0.0"}))
    line = mcp_lines(caplog)[-1]
    assert (line["mcp_client"], line["mcp_client_version"]) == ("cursor-vscode", "1.0.0") and line["key_id"] is not None


def test_no_other_mcp_request_carries_a_client_name(client, free_key, caplog):
    caplog.set_level("INFO")
    assert rpc(client, None, "tools/list").status_code == 200
    assert rpc(client, free_key, "ping").status_code == 200
    tool_result(call(client, free_key, {"url": "https://example.com"}))
    note = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers(free_key))
    assert note.status_code == 202
    # The 2026 envelope has no initialize; its per-request clientInfo is not logged.
    meta = {**MODERN_META, "io.modelcontextprotocol/clientInfo": {"name": "modern-client", "version": "9"}}
    assert rpc(client, free_key, "tools/list", {"_meta": meta}, version=MODERN, **{"Mcp-Method": "tools/list"}).status_code == 200
    assert client.get("/mcp", headers=headers(free_key)).status_code == 405
    lines = mcp_lines(caplog)
    assert len(lines) == 6
    for line in lines:
        assert "mcp_client" not in line and "mcp_client_version" not in line
        assert line["agent"] == "testclient" and line["agent_kind"] == "tool"
    assert "modern-client" not in str(lines)


@pytest.mark.parametrize(
    ("client_info", "expected"),
    [
        (None, (None, None)),
        ("claude", (None, None)),
        ([], (None, None)),
        ({}, (None, None)),
        ({"name": 7, "version": 1.5}, (None, None)),
        ({"name": None, "version": ["1"]}, (None, None)),
        ({"name": "mcp-inspector"}, ("mcp-inspector", None)),
        ({"version": "0.1"}, (None, "0.1")),
        ({"name": "N" * 500, "version": "9" * 500}, ("n" * 40, "9" * 20)),
        ({"name": 'x"}\n{"severity":"CRITICAL"', "version": "1\x00\x1b[0m;rm -rf /"}, ("x-severitycritical", "10mrm-rf-")),
        ({"name": "Čtečka ☃", "version": "١٢٣"}, ("teka-", None)),
        ({"name": "cal_abcdefghijklmnop", "version": "1"}, (None, "1")),
    ],
)
def test_client_names_are_cleaned_and_odd_ones_are_null(client, caplog, client_info, expected):
    caplog.set_level("INFO")
    # Whatever the SDK makes of the handshake (it refuses some of these), the line is written and holds only clean values.
    r = initialize(client, None, client_info)
    assert r.status_code in (200, 400), r.text
    line = mcp_lines(caplog)[-1]
    assert (line["mcp_client"], line["mcp_client_version"]) == expected
    assert line["status"] == r.status_code


def test_client_info_is_read_from_an_initialize_only():
    from calotype.remote_mcp import _client_info

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "A B", "version": "1"}}}
    assert _client_info(json.dumps(init).encode()) == ("a-b", "1")
    assert _client_info(json.dumps([{"jsonrpc": "2.0", "method": "ping", "id": 0}, init]).encode()) == ("a-b", "1")
    assert _client_info(json.dumps({**init, "params": None}).encode()) == (None, None)
    assert _client_info(json.dumps({**init, "params": [1]}).encode()) == (None, None)
    for body in (
        b"",
        b"not json",
        b"\xff\xfe",
        b"null",
        b"7",
        b'"initialize"',
        b"[]",
        b"[1, null]",
        b"[" * 50_000,
        json.dumps({**init, "method": "tools/list"}).encode(),
        json.dumps({**init, "method": ["initialize"]}).encode(),
        json.dumps({"result": {"clientInfo": {"name": "x"}}}).encode(),
    ):
        assert _client_info(body) is None, body[:40]


def test_a_body_too_big_to_peek_at_is_passed_on_unread(client, caplog):
    caplog.set_level("INFO")
    r = initialize(client, None, {"name": "big-client", "version": "1", "padding": "p" * 70_000})
    assert tool_result(r)["serverInfo"]["name"] == "calotype"  # the SDK still answers
    line = mcp_lines(caplog)[-1]
    assert "mcp_client" not in line and line["status"] == 200


@pytest.fixture
def oauth_client(settings, store, fake_renderer):
    """A client with OAuth on. Made before the test runs, so caplog sees its logs."""
    app = create_app(
        replace(settings, oauth_secret="s" * 40),
        policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer,
    )
    with TestClient(app) as c:
        yield c


def test_with_oauth_on_a_keyless_initialize_still_names_its_client(oauth_client, caplog):
    # With OAuth on, this module reads a keyless body itself (to answer a tools/call with 401) and replays it.
    caplog.set_level("INFO")
    r = initialize(oauth_client, None, {"name": "ChatGPT", "version": "1"}, **{"User-Agent": "openai-mcp/1.0.0"})
    assert tool_result(r)["serverInfo"]["name"] == "calotype"
    line = mcp_lines(caplog)[-1]
    assert (line["mcp_client"], line["mcp_client_version"], line["agent"]) == ("chatgpt", "1", "openai-mcp")
    assert call(oauth_client, None, {"url": "https://example.com"}).status_code == 401
    line = mcp_lines(caplog)[-1]
    assert "mcp_client" not in line and line["status"] == 401


def test_a_refused_request_is_logged_without_reading_its_body(client, caplog):
    caplog.set_level("INFO")
    r = initialize(client, "cal_wrong_9d2e", {"name": "refused-client", "version": "1"})
    assert r.status_code == 401
    line = mcp_lines(caplog)[-1]
    assert "mcp_client" not in line and line["status"] == 401 and line["agent"] == "testclient"


# --- the ?ref= source tag on the endpoint URL -----------------------------------------------


def rpc_at(client: TestClient, path: str, key: str | None, method: str):
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    return client.post(path, json=body, headers=headers(key))


def test_ref_on_the_url_tags_an_untagged_key_the_first_time(client, store):
    raw, key = store.create_key("free")
    assert tool_result(rpc_at(client, "/mcp?ref=glama", raw, "ping")) == {}
    assert store.get_key(raw).label == "ref:glama"
    # A later listing's tag does not replace the first.
    assert tool_result(rpc_at(client, "/mcp?ref=smithery", raw, "ping")) == {}
    assert store.get_key(raw).label == "ref:glama"


def test_ref_never_touches_labelled_internal_or_absent_keys(client, store):
    raw_site, _ = store.create_key("free", "ref:dt-linkedin")
    raw_internal, _ = store.create_key("free", internal=True)
    for raw in (raw_site, raw_internal):
        assert tool_result(rpc_at(client, "/mcp?ref=glama", raw, "ping")) == {}
    assert store.get_key(raw_site).label == "ref:dt-linkedin"
    assert store.get_key(raw_internal).label == ""
    # No key: the tool list still works, and nothing is stored.
    assert tool_result(rpc_at(client, "/mcp?ref=glama", None, "ping")) == {}


def test_a_bad_ref_is_ignored_and_never_fails_the_request(client, store):
    raw, _ = store.create_key("free")
    assert tool_result(rpc_at(client, "/mcp?ref=%3Cscript%3E", raw, "ping")) == {}
    assert store.get_key(raw).label == ""


# --- A11: the quota as an upgrade prompt inside the tool ----------------------------------


def test_no_upgrade_line_below_80_percent(client, free_key, store, fake_renderer):
    key = store.get_key(free_key)
    for _ in range(78):
        store.try_consume(key.id, current_month(), 100)
    result = tool_result(call(client, free_key, {"url": "https://example.com"}))
    text = next(c["text"] for c in result["content"] if c["type"] == "text")
    assert "79 of 100" in text and "near its monthly limit" not in text


def test_upgrade_line_from_80_of_100(client, free_key, store, fake_renderer):
    key = store.get_key(free_key)
    for _ in range(79):
        store.try_consume(key.id, current_month(), 100)
    result = tool_result(call(client, free_key, {"url": "https://example.com"}))
    text = next(c["text"] for c in result["content"] if c["type"] == "text")
    assert "80 of 100" in text and "near its monthly limit" in text and "/pricing" in text


def test_quota_exceeded_names_where_to_get_more(client, free_key, store, fake_renderer):
    key = store.get_key(free_key)
    for _ in range(100):
        store.try_consume(key.id, current_month(), 100)
    text = tool_result(call(client, free_key, {"url": "https://example.com"}))["content"][0]["text"]
    assert "quota_exceeded" in text and "/pricing" in text


def test_upgrade_text_names_the_next_plan_and_price():
    from calotype.app import upgrade_text
    from calotype.config import Plan

    plans = {
        "free": Plan("free", 100, 0, "Free"),
        "starter": Plan("starter", 5000, 1900, "Starter"),
        "pro": Plan("pro", 25000, 4900, "Pro"),
    }
    origin = "https://calotype.dev"
    assert upgrade_text(plans, plans["free"], origin, True) == (
        "Starter is $19 a month for 5,000 screenshots: https://calotype.dev/pricing.")
    assert upgrade_text(plans, plans["starter"], origin, True).startswith("Pro is $49 a month for 25,000")
    assert "write to skewbald@agentmail.to" in upgrade_text(plans, plans["pro"], origin, True)
    assert upgrade_text(plans, plans["free"], origin, False) == (
        "Paid plans with more screenshots open soon: https://calotype.dev/pricing.")
