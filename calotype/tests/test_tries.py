"""The home page's free tries (tries.py): the form, the image page, the limits, the counts.

Every test that touches storage runs once per backend (the ``store`` fixture).
Nothing here starts a browser; ``test_browser.py`` has the end-to-end try.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.capture import CaptureError
from calotype.pages import PAGES
from calotype.ssrf import AddressPolicy
from calotype.storage import TryDay, current_day

from .conftest import FakeRenderer, fake_resolver, stored_text

KEY_COMMAND = "curl -X POST <span class=\"origin\">https://calotype.dev</span>/v1/keys -d '{\"ref\":\"try\"}'"
# The site's policy before the try box, with each hash list cut out. The box changed only hashes.
CSP_TEMPLATE = (
    "default-src 'none'; script-src HASHES; style-src HASHES; img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


@pytest.fixture
def make_client(settings, store):
    clients = []

    def _make(renderer=None, **overrides):
        app = create_app(
            replace(settings, **overrides),
            policy=AddressPolicy(resolver=fake_resolver),
            storage=store,
            renderer=renderer or FakeRenderer(),
        )
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
def tc(make_client, renderer):
    return make_client(renderer)


@pytest.fixture
def behind_proxy(make_client, renderer):
    """A client as on Cloud Run: one trusted proxy. Made before the test runs, so caplog sees its logs."""
    return make_client(renderer, trusted_proxy_hops=1)


def try_url(client, url, **headers):
    return client.post("/try", data={"url": url}, headers=headers)


def today(store) -> TryDay:
    return store.try_days([current_day()])[0]


def error_text(r) -> str:
    m = re.search(r'<p class="error" role="alert">(.*?)</p>', r.text, re.DOTALL)
    assert m, "the page shows an error"
    return html.unescape(m.group(1))


# --- the box and the page ---------------------------------------------------------------------


def test_home_has_the_box_near_the_top(tc):
    doc = tc.get("/").text
    form = re.search(r'<form class="try" id="try-form" method="post" action="/try">.*?</form>', doc, re.DOTALL)
    assert form, "a plain form that works with no script"
    assert 'name="url"' in form.group(0) and 'type="submit"' in form.group(0)
    assert doc.index('id="try-form"') < doc.index('id="agents"')
    assert "Free tries: 5 an hour, 300 a day for everyone, no key." in doc


def test_the_box_states_the_configured_limits_and_goes_when_tries_are_off(make_client):
    assert "Free tries: 2 an hour, 40 a day for everyone" in make_client(tries_per_ip_per_hour=2, tries_per_day=40).get("/").text
    off = make_client(tries_per_day=0)
    assert 'id="try-form"' not in off.get("/").text
    r = try_url(off, "https://example.com")
    assert r.status_code == 503 and "switched off" in r.text and KEY_COMMAND in r.text


def test_form_page_renders_and_a_link_only_fills_the_box(tc, renderer, store):
    r = tc.get("/try")
    assert r.status_code == 200 and 'id="try-form"' in r.text
    assert r.headers["cache-control"] == "no-store"
    r = tc.get("/try", params={"url": 'https://example.com/"><b>'})
    assert 'value="https://example.com/&quot;&gt;&lt;b&gt;"' in r.text
    assert renderer.calls == [] and today(store) == TryDay(current_day())


def test_a_try_returns_the_image_inline_and_the_next_steps(tc, renderer, store):
    r = try_url(tc, "https://example.com/some/page?q=1")
    assert r.status_code == 200
    m = re.search(r'<img src="data:image/jpeg;base64,([A-Za-z0-9+/=]+)" width="1280" height="800"', r.text)
    assert m, "the image is inline, as a data: URL"
    assert base64.b64decode(m.group(1)).startswith(b"\x89PNG")  # FakeRenderer's bytes, passed through untouched
    assert "<code>https://example.com</code>" in r.text and "/some/page" not in r.text
    assert "Calotype did not keep it" in r.text
    # Next step: a key in one line, tagged "try"; the MCP URL and the agents page, tagged too.
    assert KEY_COMMAND in r.text
    assert '<span class="origin">https://calotype.dev</span>/mcp?ref=try' in r.text
    assert 'href="/agents?ref=try"' in r.text
    assert "100 screenshots a month" in r.text
    # Sent once, kept by no cache, and the same headers as every site page.
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in r.headers
    assert today(store) == TryDay(current_day(), total=1, rendered=1, succeeded=1)


def test_settings_are_fixed_whatever_the_form_sends(tc, renderer):
    r = tc.post("/try", data={"url": "example.com", "width": "3840", "full_page": "true", "format": "png"})
    assert r.status_code == 200
    (p,) = renderer.calls
    assert p.url == "https://example.com"  # a bare host gets https://
    assert (p.width, p.height, p.full_page, p.format, p.effective_quality()) == (1280, 800, False, "jpeg", 70)
    assert p.block_ads and not p.dark_mode and p.delay_ms == 0 and p.device_scale_factor == 1


def test_try_page_csp_allows_its_own_inline_code_and_data_images(tc):
    r = try_url(tc, "https://example.com")
    csp = r.headers["content-security-policy"]
    for body in re.findall(r"<script>(.*?)</script>", r.text, re.DOTALL) + re.findall(r"<style>(.*?)</style>", r.text, re.DOTALL):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert re.search(r"<script\b[^>]*\bsrc=", r.text) is None


@pytest.mark.parametrize("path", [p.path for p in PAGES] + ["/try"])
def test_csp_is_unchanged_but_for_the_hashes(tc, path):
    csp = tc.get(path).headers["content-security-policy"]
    hashes = r"(?:'sha256-[A-Za-z0-9+/=]+'(?: 'sha256-[A-Za-z0-9+/=]+')*|'none')"
    assert re.sub(rf"(script-src|style-src) {hashes};", r"\1 HASHES;", csp) == CSP_TEMPLATE


def test_robots_keep_crawlers_off_try(tc):
    assert "Disallow: /try" in tc.get("/robots.txt").text


# --- limits -----------------------------------------------------------------------------------


def test_five_tries_an_hour_per_ip(make_client, renderer, store):
    c = make_client(renderer, trusted_proxy_hops=1)
    one = {"X-Forwarded-For": "198.51.100.7"}
    for _ in range(5):
        assert try_url(c, "https://example.com", **one).status_code == 200
    r = try_url(c, "https://example.com", **one)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert "5 free tries in the last hour" in error_text(r) and KEY_COMMAND in r.text
    assert len(renderer.calls) == 5
    # Another address has its own five; a forged left-hand entry does not help the first.
    assert try_url(c, "https://example.com", **{"X-Forwarded-For": "203.0.113.9"}).status_code == 200
    assert try_url(c, "https://example.com", **{"X-Forwarded-For": "1.2.3.4, 198.51.100.7"}).status_code == 429
    assert today(store) == TryDay(current_day(), total=8, rendered=6, succeeded=6, limited=2)


def test_try_limit_is_its_own_and_in_memory(make_client, store):
    c = make_client(tries_per_ip_per_hour=1)
    assert try_url(c, "https://example.com").status_code == 200
    assert try_url(c, "https://example.com").status_code == 429
    # Key creation keeps its own limit.
    assert c.post("/v1/keys").status_code == 201
    assert c.app.state.try_limiter is not c.app.state.key_limiter
    assert "testclient" not in stored_text(store)


def test_daily_cap_for_everyone(make_client, renderer, store):
    c = make_client(renderer, tries_per_day=2, trusted_proxy_hops=1)
    for ip in ("198.51.100.1", "198.51.100.2"):
        assert try_url(c, "https://example.com", **{"X-Forwarded-For": ip}).status_code == 200
    r = try_url(c, "https://example.com", **{"X-Forwarded-For": "198.51.100.3"})
    assert r.status_code == 429
    assert "Today's free tries are used up: 2 a day" in error_text(r)
    assert KEY_COMMAND in r.text and "<img" not in r.text
    assert len(renderer.calls) == 2
    assert today(store) == TryDay(current_day(), total=3, rendered=2, succeeded=2, capped=1)


def test_cap_is_counted_in_storage_so_instances_share_it(make_client, renderer, store):
    for _ in range(3):
        store.try_admit(current_day(), 3)  # three renders by other instances today
    r = try_url(make_client(renderer, tries_per_day=3), "https://example.com")
    assert r.status_code == 429 and renderer.calls == []


def test_failed_capture_still_uses_a_slot(make_client, store):
    c = make_client(FakeRenderer(fail_with=CaptureError(504, "timeout", "The page did not finish within 30 seconds.")))
    r = try_url(c, "https://example.com")
    assert r.status_code == 504 and error_text(r) == "The page did not finish within 30 seconds."
    assert today(store) == TryDay(current_day(), total=1, rendered=1, failed=1)


def test_busy_answers_retry_after(make_client):
    c = make_client(FakeRenderer(fail_with=CaptureError(503, "busy", "All renderers are busy. Retry shortly.", {"Retry-After": "5"})))
    r = try_url(c, "https://example.com")
    assert r.status_code == 503 and r.headers["retry-after"] == "5"


# --- the address policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/",
        "http://rebind.example/",
        "http://mixed.example/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "https://example.com:22/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "https://user:pass@example.com/",
        "https://exa mple.com/",
        "nxdomain.example",
    ],
)
def test_refused_address_gets_the_keyed_message(tc, renderer, store, free_key, url):
    r = try_url(tc, url)
    assert r.status_code == 400
    keyed = tc.get("/v1/screenshot", params={"url": url if "://" in url else "https://" + url},
                   headers={"Authorization": f"Bearer {free_key}"})
    assert keyed.status_code == 400
    assert error_text(r) == keyed.json()["error"]["message"]
    assert renderer.calls == []
    # Refused before the cap: counted, but no render slot used.
    assert today(store) == TryDay(current_day(), total=1, refused=1)


def test_url_too_long_is_refused(tc, renderer, store):
    r = try_url(tc, "https://example.com/" + "a" * 2100)
    assert r.status_code == 400 and "2048 characters" in error_text(r)
    assert renderer.calls == [] and today(store).refused == 1


def test_empty_or_cross_site_is_not_a_try(tc, renderer, store):
    assert try_url(tc, "   ").status_code == 400
    r = try_url(tc, "https://example.com", **{"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert try_url(tc, "https://example.com", **{"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert len(renderer.calls) == 1 and today(store).total == 1


# --- counts and logs --------------------------------------------------------------------------


def test_counts_hold_no_url_host_or_ip(make_client, store):
    c = make_client(trusted_proxy_hops=1, tries_per_ip_per_hour=2)
    ip = {"X-Forwarded-For": "198.51.100.77"}
    try_url(c, "https://public.example/secret-path?token=s3cret", **ip)
    try_url(c, "http://10.0.0.9/admin", **ip)
    try_url(c, "https://public.example/again", **ip)
    assert today(store) == TryDay(current_day(), total=3, rendered=1, succeeded=1, refused=1, limited=1)
    text = stored_text(store)
    for leak in ("public.example", "secret-path", "s3cret", "10.0.0.9", "198.51.100.77", "/admin"):
        assert leak not in text, leak


def test_one_log_line_per_try_with_scheme_and_host_only(behind_proxy, caplog):
    caplog.set_level(logging.INFO)
    c = behind_proxy
    # A named User-Agent, so "testclient" below can only be the test client's address.
    ip = {"X-Forwarded-For": "198.51.100.77", "User-Agent": "SomeBrowser/9 (probe-7c1d)"}
    try_url(c, "https://public.example/secret-path?token=s3cret", **ip)
    try_url(c, "http://127.0.0.1/admin", **ip)
    tries = [r for r in caplog.records if r.name == "calotype.try" and r.getMessage() == "try"]
    assert [(r.fields["outcome"], r.fields["target"]) for r in tries] == [
        ("succeeded", "https://public.example"),
        ("refused", None),
    ]
    assert tries[0].fields["format"] == "jpeg" and tries[0].fields["bytes"] > 0
    requests = [r.fields for r in caplog.records if r.getMessage() == "request" and r.fields["path"] == "/try"]
    assert len(requests) == 2 and requests[0]["key_id"] is None
    assert (requests[0]["agent"], requests[0]["agent_kind"]) == ("somebrowser", "tool")
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records if r.name.startswith("calotype"))
    for leak in ("secret-path", "s3cret", "/admin", "198.51.100.77", "testclient", "probe-7c1d", "SomeBrowser"):
        assert leak not in text, leak


def test_limiter_sweep_covers_tries(settings, sqlite_store, monkeypatch):
    import time

    import calotype.app as app_module

    monkeypatch.setattr(app_module, "SWEEP_INTERVAL_S", 0.01)
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=sqlite_store, renderer=FakeRenderer())
    calls = []
    monkeypatch.setattr(app.state.try_limiter, "sweep", lambda *a, **k: calls.append("try") or 0)
    with TestClient(app):
        for _ in range(200):
            if calls:
                break
            time.sleep(0.01)
    assert calls


# --- attribution --------------------------------------------------------------------


def test_the_key_command_on_the_page_tags_the_key_try(tc, store):
    # curl -d sends a form content type; the body is read as JSON all the same.
    r = tc.post("/v1/keys", content=b'{"ref":"try"}', headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    assert [c.label for c in store.list_keys()] == ["ref:try"]
