"""API tests with a fake renderer: validation, auth, quota, logging."""

from __future__ import annotations

import logging

import pytest

from calotype.capture import CaptureError
from calotype.storage import current_month, hash_key

from .conftest import stored_text


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_health(client):
    for path in ("/health", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_landing_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Skewbald" in r.text
    assert "AI agent" in r.text


# --- auth -------------------------------------------------------------------


def test_missing_key(client):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_key"


def test_bad_key(client):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth("cal_nope"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_key"


def test_wrong_auth_scheme(client, free_key):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers={"Authorization": f"Basic {free_key}"})
    assert r.status_code == 401


def test_bearer_key(client, free_key, fake_renderer):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["x-calotype-usage"] == "1/100"
    assert len(fake_renderer.calls) == 1


def test_query_key(client, free_key):
    r = client.get("/v1/screenshot", params={"url": "https://example.com", "access_key": free_key})
    assert r.status_code == 200


def test_post_json(client, free_key, fake_renderer):
    r = client.post(
        "/v1/screenshot",
        json={"url": "https://example.com", "format": "webp", "quality": 70, "full_page": True},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    p = fake_renderer.calls[0]
    assert p.format == "webp" and p.quality == 70 and p.full_page is True


def test_revoked_key(client, store):
    raw, key = store.create_key("free", "x")
    store.revoke_key(key.id)
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 401


def test_keys_are_stored_hashed(store):
    raw, key = store.create_key("starter", "hash check")
    dump = stored_text(store)
    assert raw not in dump
    assert hash_key(raw) in dump
    assert store.get_key(raw).id == key.id


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"url": ""},
        {"url": "https://example.com", "width": 3841},
        {"url": "https://example.com", "width": 99},
        {"url": "https://example.com", "height": 2161},
        {"url": "https://example.com", "width": "wide"},
        {"url": "https://example.com", "format": "gif"},
        {"url": "https://example.com", "quality": 0},
        {"url": "https://example.com", "quality": 101},
        {"url": "https://example.com", "format": "png", "quality": 50},
        {"url": "https://example.com", "device_scale_factor": 4},
        {"url": "https://example.com", "device_scale_factor": 0.5},
        {"url": "https://example.com", "delay_ms": 10001},
        {"url": "https://example.com", "delay_ms": -1},
        {"url": "https://example.com", "wait_until": "forever"},
        {"url": "https://example.com", "full_page": "maybe"},
        {"url": "https://example.com", "unknown_option": "1"},
        {"url": "https://example.com", "scroll_y": -1},
        {"url": "https://example.com", "scroll_y": 20001},
        {"url": "https://example.com", "scroll_y": "down"},
        {"url": "https://example.com", "scroll_y": 800, "full_page": "true"},
    ],
)
def test_invalid_parameters(client, free_key, params, fake_renderer):
    r = client.get("/v1/screenshot", params=params, headers=auth(free_key))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "invalid_parameters"
    assert fake_renderer.calls == []


@pytest.mark.parametrize(
    "url,code",
    [
        ("file:///etc/passwd", "invalid_scheme"),
        ("data:text/html,hi", "invalid_scheme"),
        ("chrome://version", "invalid_scheme"),
        ("http://127.0.0.1/", "blocked_target"),
        ("http://169.254.169.254/latest/meta-data/", "blocked_target"),
        ("http://[::1]/", "blocked_target"),
        ("http://localhost:8080/", "blocked_target"),
        ("http://rebind.example/", "blocked_target"),
        ("http://nxdomain.example/", "dns_error"),
    ],
)
def test_blocked_urls_rejected_without_quota(client, free_key, store, url, code, fake_renderer):
    r = client.get("/v1/screenshot", params={"url": url}, headers=auth(free_key))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == code
    assert fake_renderer.calls == []
    key = store.get_key(free_key)
    assert store.get_usage(key.id, current_month()) == 0


def test_full_page_height_cap():
    from calotype.params import ScreenshotParams

    assert ScreenshotParams(url="https://x.example", width=1280).full_page_height_cap() == 20000
    p = ScreenshotParams(url="https://x.example", width=3840, height=2160, device_scale_factor=3)
    assert p.check_combinations() is None
    assert 2160 <= p.full_page_height_cap() < 20000
    assert p.width * p.full_page_height_cap() * 9 <= 80_000_000


def test_valid_options_reach_renderer(client, free_key, fake_renderer):
    r = client.get(
        "/v1/screenshot",
        params={
            "url": "https://example.com/a?b=c",
            "width": 3840,
            "height": 2160,
            "format": "jpg",
            "quality": "55",
            "device_scale_factor": "2",
            "delay_ms": "250",
            "wait_until": "networkidle",
            "block_ads": "true",
            "dark_mode": "1",
            "full_page": "false",
        },
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    p = fake_renderer.calls[0]
    assert (p.width, p.height, p.format, p.quality, p.device_scale_factor) == (3840, 2160, "jpeg", 55, 2.0)
    assert p.block_ads and p.dark_mode and not p.full_page and p.wait_until == "networkidle"


def test_scroll_y_reaches_the_renderer_and_its_header_says_where_the_screen_is(settings, store, free_key):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from calotype.app import create_app
    from calotype.ssrf import AddressPolicy

    from .conftest import FakeRenderer, fake_resolver

    class Scrolled(FakeRenderer):
        async def capture(self, params):
            result = await super().capture(params)
            # As the real renderer does: a scroll_y past the last screen is moved up to it.
            return replace(result, meta={"page_height": 3000, "scroll_y": min(params.scroll_y, 3000 - params.height)})

    renderer = Scrolled()
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=renderer)
    with TestClient(app) as c:
        r = c.get("/v1/screenshot", params={"url": "https://example.com", "scroll_y": "800"}, headers=auth(free_key))
        assert r.status_code == 200, r.text
        assert renderer.calls[-1].scroll_y == 800 and not renderer.calls[-1].full_page
        assert (r.headers["x-calotype-scroll-y"], r.headers["x-calotype-page-height"]) == ("800", "3000")
        r = c.post("/v1/screenshot", json={"url": "https://example.com", "scroll_y": 9000}, headers=auth(free_key))
        assert r.status_code == 200 and r.headers["x-calotype-scroll-y"] == "2200"
        # scroll_y=0 with full_page is the default, not a conflict.
        r = c.post("/v1/screenshot", json={"url": "https://example.com", "scroll_y": 0, "full_page": True},
                   headers=auth(free_key))
        assert r.status_code == 200, r.text


def test_scroll_y_with_full_page_is_refused_in_plain_words(client, free_key, fake_renderer, store):
    r = client.post(
        "/v1/screenshot", json={"url": "https://example.com", "scroll_y": 800, "full_page": True}, headers=auth(free_key)
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_parameters"
    assert "scroll_y" in error["message"] and "full_page=true" in error["message"]
    assert fake_renderer.calls == []
    assert store.get_usage(store.get_key(free_key).id, current_month()) == 0


def test_scroll_y_params():
    from calotype.params import MAX_FULL_PAGE_HEIGHT, ScreenshotParams

    assert ScreenshotParams(url="https://x.example").scroll_y == 0
    assert ScreenshotParams(url="https://x.example", scroll_y=MAX_FULL_PAGE_HEIGHT).check_combinations() is None
    assert ScreenshotParams(url="https://x.example", scroll_y=0, full_page=True).check_combinations() is None
    assert "full_page" in ScreenshotParams(url="https://x.example", scroll_y=1, full_page=True).check_combinations()


# --- quota ------------------------------------------------------------------


def test_quota_exceeded(client, store, settings):
    raw, key = store.create_key("free", "quota")
    month = current_month()
    limit = settings.plans["free"].monthly_quota
    for _ in range(limit - 1):
        assert store.try_consume(key.id, month, limit)[0]
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 200
    assert r.headers["x-calotype-usage"] == f"{limit}/{limit}"
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "quota_exceeded"
    assert err["limit"] == limit and err["used"] == limit and err["plan"] == "free"
    assert err["resets_at"].endswith("T00:00:00Z")


def test_quota_is_per_key_and_per_month(store):
    a, ka = store.create_key("free", "a")
    b, kb = store.create_key("free", "b")
    assert store.try_consume(ka.id, "2026-11", 1) == (True, 1)
    assert store.try_consume(ka.id, "2026-11", 1) == (False, 1)
    assert store.try_consume(kb.id, "2026-11", 1) == (True, 1)
    assert store.try_consume(ka.id, "2026-12", 1) == (True, 1)


def test_failed_capture_is_refunded(client, free_key, store, fake_renderer):
    fake_renderer.fail_with = CaptureError(422, "target_unreachable", "nope")
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "target_unreachable"
    assert store.get_usage(store.get_key(free_key).id, current_month()) == 0


def test_busy_returns_503(client, free_key, fake_renderer):
    fake_renderer.fail_with = CaptureError(503, "busy", "busy", {"Retry-After": "5"})
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"


def test_usage_endpoint(client, free_key):
    client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    r = client.get("/v1/usage", headers=auth(free_key))
    assert r.json()["used"] == 1 and r.json()["limit"] == 100


def test_usage_names_the_key_by_reference_not_by_value(client, free_key):
    import hashlib

    body = client.get("/v1/usage", headers=auth(free_key)).json()
    assert body["key_ref"] == hashlib.sha256(free_key.encode()).hexdigest()[:12]
    assert free_key not in str(body)


# --- logging ----------------------------------------------------------------


def test_logs_have_no_key_or_query(client, free_key, caplog):
    caplog.set_level(logging.INFO)
    secret_path = "/private/report?token=s3cret-token"
    client.get("/v1/screenshot", params={"url": "https://example.com" + secret_path, "access_key": free_key})
    client.post("/v1/screenshot", json={"url": "https://example.com" + secret_path}, headers=auth(free_key))
    records = [r for r in caplog.records if r.name.startswith("calotype")]
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in records)
    assert "request" in text
    assert free_key not in text
    assert "s3cret-token" not in text
    assert "/private/report" not in text
    assert "https://example.com" in text


CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
CLAUDE_USER = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"
)


@pytest.mark.parametrize(
    ("header", "agent", "kind"),
    [
        # No header, or nothing in it.
        (None, None, "none"),
        ("", None, "none"),
        ("  \t ", None, "none"),
        # Browsers.
        (CHROME, "mozilla", "browser"),
        ("mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0", "mozilla", "browser"),
        # Tools: HTTP libraries, command lines, MCP clients.
        ("curl/8.5.0", "curl", "tool"),
        ("python-httpx/0.28.1", "python-httpx", "tool"),
        ("node", "node", "tool"),
        ("Claude-User/1.0", "claude-user", "tool"),
        ("node-fetch/1.0 (+https://github.com/bitinn/node-fetch)", "node-fetch", "tool"),
        ("Go-http-client/2.0", "go-http-client", "tool"),
        ("n8n", "n8n", "tool"),
        ("  Wget/1.21", "wget", "tool"),
        ("PostmanRuntime/7.43.0", "postmanruntime", "tool"),
        ("Mozilla", "mozilla", "tool"),  # not "Mozilla/": no browser sends this
        # Bots: a marker anywhere in the header, in any case.
        (GOOGLEBOT, "mozilla", "bot"),
        (CLAUDE_USER, "mozilla", "bot"),
        (CHROME.replace("Chrome/", "HeadlessChrome/"), "mozilla", "bot"),
        ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", "slackbot-linkexpanding", "bot"),
        ("facebookexternalhit/1.1", "facebookexternalhit", "bot"),
        ("UptimeRobot/2.0", "uptimerobot", "bot"),
        ("Example-CRAWLER/3", "example-crawler", "bot"),
        ("Site Monitor 2", "site", "bot"),
        ("FeedFetcher-Google; (+http://www.google.com/feedfetcher.html)", "feedfetcher-google", "bot"),
        ("Nmap Scripting Engine; scanner", "nmap", "bot"),
        ("link-preview/1", "link-preview", "bot"),
        # Hostile input: control characters, JSON-breaking characters, non-ASCII, very long values, a key.
        ('cu"rl\\{}\x00\x1b[31m/1 "}],\n{"severity":"ERROR"', "curl31m", "tool"),
        ('"},{"agent":"x"}', "agentx", "tool"),
        ("\x00\x01\x02", None, "tool"),
        ("/1.0", None, "tool"),
        ("---/1.0", None, "tool"),
        ("Čtečka-Ünï/2 ☃", "teka-n", "tool"),
        ("日本語ブラウザ/1.0", None, "tool"),
        ("A" * 100_000, "a" * 32, "tool"),
        ("x" * 100_000 + " bot", "x" * 32, "bot"),
        ("<script>alert(1)</script>", "scriptalert1", "tool"),
        ("cal_abcdefghijklmnop/1", None, "tool"),
        ("../../etc/passwd", None, "tool"),
        ("..%2f..%2fetc%2fpasswd", "..2f..2fetc2fpasswd", "tool"),
    ],
)
def test_client_software_names_the_sender_and_nothing_else(header, agent, kind):
    import json
    import re

    from calotype.logs import AGENT_MAX, client_software

    got = client_software(header)
    assert got == (agent, kind)
    assert got[0] is None or (re.fullmatch(r"[a-z0-9._-]+", got[0]) and len(got[0]) <= AGENT_MAX)
    # Nothing in the value needs escaping, so it cannot break out of its JSON field.
    assert json.dumps(got[0]) in ("null", f'"{got[0]}"')


@pytest.mark.parametrize(
    ("value", "limit", "cleaned"),
    [
        ("claude-ai", 40, "claude-ai"),
        ("Claude Code", 40, "claude-code"),
        ("Visual Studio Code", 40, "visual-studio-code"),
        ("  Cursor  (1.2) ", 40, "cursor-1.2"),
        ("mcp-remote - fallback", 40, "mcp-remote-fallback"),
        ("my_client.v2", 40, "my_client.v2"),
        ("1.0.0", 20, "1.0.0"),
        ("0.3.1 (beta) build 2026-11-23T10:00:00Z", 20, "0.3.1-beta-build-202"),
        ("n" * 500, 40, "n" * 40),
        ("Ünï cödé ☃ client", 40, "n-cd--client"),
        ('x"}\n{"severity":"CRITICAL","message":"owned', 40, "x-severitycriticalmessageowned"),
        ("a\x00b\r\nc\x1b[0m", 40, "ab-c0m"),
        ("", 40, None),
        ("   ", 40, None),
        ("--..__", 40, None),
        ("☃☃☃", 40, None),
        ("cal_abcdefghijklmnop", 40, None),
        ("agent for cal_abcdefghijklmnop", 40, None),
        (None, 40, None),
        (7, 40, None),
        (1.5, 20, None),
        (True, 20, None),
        (["claude"], 40, None),
        ({"name": "claude"}, 40, None),
    ],
)
def test_clean_name(value, limit, cleaned):
    from calotype.logs import clean_name

    assert clean_name(value, limit) == cleaned


def request_lines(caplog, path: str) -> list[dict]:
    return [r.fields for r in caplog.records if r.getMessage() == "request" and r.fields["path"] == path]


def test_request_lines_name_the_software_and_never_the_user_agent(client, free_key, caplog):
    caplog.set_level(logging.INFO)
    agents = [CHROME, "curl/8.5.0", GOOGLEBOT, 'Evil"\x1b}/1 probe-7c1d (secret-detail; jane@example.org)']
    for ua in agents:
        assert client.get("/health", headers={"User-Agent": ua}).status_code == 200
    client.get("/v1/screenshot", params={"url": "https://example.com"}, headers={**auth(free_key), "User-Agent": "curl/8.5.0"})
    lines = request_lines(caplog, "/health")
    assert [(l["agent"], l["agent_kind"]) for l in lines] == [
        ("mozilla", "browser"),
        ("curl", "tool"),
        ("mozilla", "bot"),
        ("evil", "tool"),
    ]
    # The fields of a request line: the old seven and the two new ones, on every request.
    assert set(lines[0]) == {
        "request_id", "method", "path", "status", "duration_ms", "key_id", "target", "agent", "agent_kind",
    }
    shot = request_lines(caplog, "/v1/screenshot")[-1]
    assert (shot["agent"], shot["agent_kind"], shot["status"]) == ("curl", "tool", 200) and shot["key_id"] is not None
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    for leak in ("AppleWebKit", "Chrome/141", "8.5.0", "Googlebot", "google.com", "probe-7c1d", "secret-detail", "jane@"):
        assert leak not in text, leak


def test_a_request_with_no_user_agent_is_logged_as_none(client, caplog):
    caplog.set_level(logging.INFO)
    # httpx sends its own User-Agent unless the header is removed from the client.
    del client.headers["user-agent"]
    assert client.get("/health").status_code == 200
    line = request_lines(caplog, "/health")[-1]
    assert line["agent"] is None and line["agent_kind"] == "none"


def test_the_json_log_line_carries_the_new_fields(client, capsys):
    import json

    from calotype.logs import JsonFormatter

    handler = logging.StreamHandler()  # writes to the stderr that capsys holds
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("calotype")
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        client.get("/health", headers={"User-Agent": 'cu"rl\\/8 "}{'})
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    line = [l for l in lines if l["message"] == "request" and l["path"] == "/health"][-1]
    assert line["agent"] == "curl" and line["agent_kind"] == "tool" and line["status"] == 200


def test_log_redaction():
    from calotype.logs import redact

    line = redact(
        'Page.goto: net::ERR_FAILED at https://example.com/a/b?token=abc&x=1 '
        'GET /v1/screenshot?access_key=cal_abcdefghijklmnop Authorization: Bearer cal_zzzzzzzzzzzz'
    )
    assert "token=abc" not in line and "/a/b" not in line
    assert "abcdefghijklmnop" not in line and "zzzzzzzzzzzz" not in line
    assert "https://example.com" in line
