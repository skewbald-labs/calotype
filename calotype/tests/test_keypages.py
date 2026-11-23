"""Pages for a person in a browser (keypages.py): help on the API-only addresses, and the key button.

A browser, here, is a request whose ``Accept`` names ``text/html`` and no event stream. Every
other request must get exactly the answer it got before these pages existed: the literals in
``UNCHANGED`` were recorded from the code before the change.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from calotype.app import create_app
from calotype.config import Plan
from calotype.keypages import API_LINK_REF, KEYS_OFF, MCP_LINK_REF, WEB_REF, valid_ref
from calotype.pages import wants_html
from calotype.ssrf import AddressPolicy
from calotype.storage import FUNNEL_EVENTS, current_day, hash_key

from .conftest import FakeRenderer, fake_resolver, stored_text
from .test_remote_mcp import rpc

# What Firefox and Chrome send when a person opens a link.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
KEY = re.compile(r"cal_[A-Za-z0-9_-]{43}")
FORM = re.compile(r'<form class="getkey" method="post" action="/keys">.*?</form>', re.DOTALL)
BUTTON_LINE = "Free plan: 100 screenshots a month. No account, no email."


@pytest.fixture
def renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
def make_client(settings, sqlite_store, renderer):
    clients = []

    def _make(**overrides):
        app = create_app(
            replace(settings, **overrides),
            policy=AddressPolicy(resolver=fake_resolver),
            storage=sqlite_store,
            renderer=renderer,
        )
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def web(make_client):
    return make_client()


def main_of(document: str) -> str:
    return document[document.index("<main"):document.index("</main>")]


def button_of(document: str) -> str:
    """The one key button in a page's main part."""
    forms = FORM.findall(main_of(document))
    assert len(forms) == 1, "one key button"
    return forms[0]


def button_ref(document: str) -> str:
    return re.search(r'<input type="hidden" name="ref" value="([^"]*)">', button_of(document)).group(1)


def assert_help_page(r, status: int) -> str:
    """What every help page and key page has. Returns its main part."""
    assert r.status_code == status
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in r.headers and "location" not in r.headers
    doc = r.text
    assert '<meta name="robots" content="noindex">' in doc[:doc.index("</head>")]
    assert doc.count("<title>") == 1 and "Calotype is built and run by Skewbald, an AI agent" in doc
    # Nothing from another host, and only the page's own inline code may run.
    assert re.search(r"<script\b[^>]*\bsrc=", doc) is None
    for tag in re.findall(r"<(?:link|img|iframe|source|embed|object)\b[^>]*>", doc):
        for value in re.findall(r'\b(?:href|src)="([^"]*)"', tag):
            assert not value.startswith(("http:", "//")) and (not value.startswith("https:") or 'rel="canonical"' in tag)
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "form-action 'self'" in csp
    for body in re.findall(r"<script>(.*?)</script>", doc, re.DOTALL) + re.findall(r"<style>(.*?)</style>", doc, re.DOTALL):
        assert f"'sha256-{base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()}'" in csp
    assert "—" not in doc  # no em dash in site copy
    return main_of(doc)


# --- what counts as a browser ---------------------------------------------------------------


@pytest.mark.parametrize(
    "accept, expected",
    [
        (BROWSER["Accept"], True),
        ("text/html", True),
        ("TEXT/HTML;q=0.9", True),
        ("*/*", False),
        ("application/json", False),
        ("text/event-stream", False),
        ("application/json, text/event-stream", False),
        ("text/html, text/event-stream", False),  # an event stream is never a browser's navigation
        ("", False),
        (None, False),
    ],
)
def test_wants_html(accept, expected):
    headers = [] if accept is None else [(b"accept", accept.encode())]
    assert wants_html(Request({"type": "http", "headers": headers})) is expected


def test_accept_split_over_two_headers_is_read_whole():
    scope = {"type": "http", "headers": [(b"accept", b"text/html"), (b"accept", b"text/event-stream")]}
    assert wants_html(Request(scope)) is False


@pytest.mark.parametrize("value", ["glama", "dt-linkedin", "a", "x" * 40, "9lives"])
def test_valid_ref_takes_tags(value):
    assert valid_ref(value) == value


@pytest.mark.parametrize("value", ["", None, 7, "Bad", "-x", "x" * 41, "a b", "glama\n", '"><script>', "a/b"])
def test_valid_ref_refuses_everything_else(value):
    assert valid_ref(value) == ""


# --- GET /mcp ---------------------------------------------------------------------------------


def test_mcp_in_a_browser_is_a_help_page_with_the_same_405(web, renderer):
    r = web.get("/mcp", headers=BROWSER)
    main = assert_help_page(r, 405)
    assert r.headers["allow"] == "POST" and r.headers["vary"] == "Accept"
    assert "<h1>This is Calotype's MCP endpoint</h1>" in main
    assert "for an MCP client, such as an AI agent or a code editor" in main and "not a page for a browser" in main
    # The address to copy, the set-up, the try box and the key button.
    assert '<pre><code><span class="origin">https://calotype.dev</span>/mcp</code></pre>' in main
    assert f'href="/agents?ref={MCP_LINK_REF}"' in main
    assert '<form class="try" id="try-form" method="post" action="/try">' in main
    assert button_ref(r.text) == MCP_LINK_REF
    assert ">Get a free key</button>" in button_of(r.text) and BUTTON_LINE in button_of(r.text)
    assert renderer.calls == []


def test_mcp_page_carries_a_valid_ref(web):
    r = web.get("/mcp?ref=glama&x=1", headers=BROWSER)
    main = assert_help_page(r, 405)
    assert 'href="/agents?ref=glama"' in main and button_ref(r.text) == "glama"
    assert '<span class="origin">https://calotype.dev</span>/mcp?ref=glama</code>' in main
    assert MCP_LINK_REF not in main


@pytest.mark.parametrize("ref", ['"><script>alert(1)</script>', "Glama", "x" * 41, "a&b=<i>", "glama\n"])
def test_mcp_page_ignores_and_never_echoes_a_hostile_ref(web, ref):
    r = web.get("/mcp", params={"ref": ref}, headers=BROWSER)
    main = assert_help_page(r, 405)
    assert button_ref(r.text) == MCP_LINK_REF and f'href="/agents?ref={MCP_LINK_REF}"' in main
    assert "alert(1)" not in r.text and "<i>" not in r.text and "Glama" not in r.text and "x" * 41 not in r.text


def test_mcp_page_without_tries_or_keys_offers_neither(make_client):
    off = make_client(tries_per_day=0, self_serve_keys=False)
    main = assert_help_page(off.get("/mcp", headers=BROWSER), 405)
    assert "<form" not in main and KEYS_OFF in main and 'href="/agents?ref=mcp-link"' in main


@pytest.mark.parametrize("method", ["DELETE", "HEAD", "PUT"])
def test_only_get_on_mcp_is_a_page(web, method):
    r = web.request(method, "/mcp", headers=BROWSER)
    assert r.status_code == 405 and r.headers["content-type"] == "application/json"


def test_post_to_mcp_with_a_browser_accept_still_reaches_the_server(web):
    r = rpc(web, None, "ping", Accept="text/html, application/json, text/event-stream")
    assert r.status_code == 200 and r.json()["result"] == {}


# --- GET /v1/screenshot ----------------------------------------------------------------------


def test_screenshot_link_with_no_key_is_a_help_page_with_the_same_401(web, renderer):
    r = web.get("/v1/screenshot", params={"url": "https://example.com/pricing?a=1&b=2"}, headers=BROWSER)
    main = assert_help_page(r, 401)
    assert r.headers["www-authenticate"] == "Bearer" and r.headers["vary"] == "Accept"
    assert "<h1>This link needs a key</h1>" in main and "This link has no key." in main
    # The try box, filled in by a link: nothing is rendered by this request.
    link = 'href="/try?url=https%3A%2F%2Fexample.com%2Fpricing%3Fa%3D1%26b%3D2"'
    assert f'<a class="button" {link}>See this page captured now, no key needed</a>' in main
    assert "<code>https://example.com/pricing?a=1&amp;b=2</code>" in main
    assert renderer.calls == []
    assert button_ref(r.text) == API_LINK_REF and f'href="/agents?ref={API_LINK_REF}"' in main
    # The link opens the try box with the address in it, and still captures nothing.
    href = re.search(r'href="(/try\?url=[^"]+)"', main).group(1)
    box = web.get(href)
    assert box.status_code == 200 and 'value="https://example.com/pricing?a=1&amp;b=2"' in box.text
    assert renderer.calls == []


def test_screenshot_link_takes_a_bare_host_as_the_try_box_does(web):
    main = assert_help_page(web.get("/v1/screenshot?url=example.com", headers=BROWSER), 401)
    assert 'href="/try?url=https%3A%2F%2Fexample.com"' in main


def test_screenshot_link_with_a_wrong_key_says_so_and_never_shows_the_key(web, renderer):
    secret = "cal_PLACEHOLDER-do-not-show-7f3a"
    r = web.get("/v1/screenshot", params={"access_key": secret, "url": "https://example.com"}, headers=BROWSER)
    main = assert_help_page(r, 401)
    assert r.headers["www-authenticate"] == "Bearer"
    assert "<h1>The key in this link is not valid</h1>" in main
    assert "The key in this link is not a valid key. It may be a placeholder from an example." in main
    assert 'href="/try?url=https%3A%2F%2Fexample.com"' in main
    assert secret not in r.text and "do-not-show" not in r.text and "PLACEHOLDER" not in r.text
    assert all("do-not-show" not in v for v in r.headers.values())
    assert renderer.calls == []


def test_screenshot_page_never_echoes_an_access_key_inside_the_target(web):
    # A link whose url is itself a link with a key: no try link is built from it.
    target = "https://api.example.com/take?access_key=sk_inner_9d2e"
    r = web.get("/v1/screenshot", params={"url": target, "access_key": "cal_outer_5b1c"}, headers=BROWSER)
    main = assert_help_page(r, 401)
    assert "sk_inner_9d2e" not in r.text and "cal_outer_5b1c" not in r.text
    assert "/try?url=" not in main and 'href="/try"' in main


def test_screenshot_page_with_a_revoked_key_or_a_header_key(web, sqlite_store):
    raw, key = sqlite_store.create_key("free")
    sqlite_store.revoke_key(key.id)
    r = web.get("/v1/screenshot", params={"url": "https://example.com", "access_key": raw}, headers=BROWSER)
    assert "The key in this link is not valid" in assert_help_page(r, 401) and raw not in r.text
    r = web.get("/v1/screenshot?url=https://example.com", headers={**BROWSER, "Authorization": "Bearer cal_nope_1a2b"})
    main = assert_help_page(r, 401)
    assert "The key sent with this request is not a valid key." in main and "cal_nope_1a2b" not in r.text
    # An Authorization header that is not a bearer token: the JSON answer has no WWW-Authenticate, nor has the page.
    r = web.get("/v1/screenshot?url=https://example.com", headers={**BROWSER, "Authorization": "Basic abc"})
    assert_help_page(r, 401)
    assert "www-authenticate" not in r.headers


@pytest.mark.parametrize(
    "url",
    [
        'https://example.com/"><script>alert(1)</script>',
        "https://example.com/?q=<img src=x onerror=alert(1)>",
        "https://example.com/'onmouseover='alert(1)",
    ],
)
def test_a_hostile_url_is_escaped_in_the_link_and_the_text(web, url):
    r = web.get("/v1/screenshot", params={"url": url, "ref": '"><script>alert(2)</script>'}, headers=BROWSER)
    main = assert_help_page(r, 401)
    assert "<script>alert" not in main and "<img src=x" not in main and "'onmouseover='" not in main
    assert "alert(2)" not in r.text and button_ref(r.text) == API_LINK_REF
    for href in re.findall(r'href="([^"]*)"', main):
        assert not set(href) & set("<>'\" "), href
    assert len(re.findall(r"<script>", r.text)) == 3  # the layout's own three, and no other


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://169.254.169.254/latest/meta-data",
        "https://user:pass@example.com/",
        "https://example.com/a b",
        "https://example.com/" + "a" * 2100,
        "   ",
        "",
    ],
)
def test_a_url_the_try_box_would_refuse_gets_no_try_link(web, url):
    r = web.get("/v1/screenshot", params={"url": url}, headers=BROWSER)
    main = assert_help_page(r, 401)
    assert "/try?url=" not in main and 'href="/try"' in main
    assert "passwd" not in main and "127.0.0.1" not in main and "169.254" not in main and "user:pass" not in main
    assert "a" * 2100 not in r.text


def test_screenshot_page_carries_a_valid_ref_and_drops_the_try_link_when_tries_are_off(make_client):
    r = make_client().get("/v1/screenshot?url=https://example.com&ref=dt-post", headers=BROWSER)
    main = assert_help_page(r, 401)
    assert button_ref(r.text) == "dt-post" and 'href="/agents?ref=dt-post"' in main and API_LINK_REF not in main
    off = make_client(tries_per_day=0)
    r = off.get("/v1/screenshot?url=https://example.com", headers=BROWSER)
    assert "/try" not in assert_help_page(r, 401) and button_ref(r.text) == API_LINK_REF


def test_other_screenshot_answers_stay_as_they_are_for_a_browser(web, sqlite_store, renderer):
    raw, _ = sqlite_store.create_key("free")
    ok = web.get("/v1/screenshot", params={"url": "https://example.com", "access_key": raw}, headers=BROWSER)
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/png"
    for params, status, code in [
        ({"url": "ftp://example.com", "access_key": raw}, 400, "invalid_scheme"),
        ({"url": "https://example.com", "nope": "1", "access_key": raw}, 400, "invalid_parameters"),
        ({"url": "http://127.0.0.1/", "access_key": raw}, 400, "blocked_target"),
    ]:
        r = web.get("/v1/screenshot", params=params, headers=BROWSER)
        assert r.status_code == status and r.json()["error"]["code"] == code
    # Only GET is a link a person opens; only /v1/screenshot has the page.
    r = web.post("/v1/screenshot", json={"url": "https://example.com"}, headers=BROWSER)
    assert r.status_code == 401 and r.json()["error"]["code"] == "missing_key"
    r = web.get("/v1/usage", headers=BROWSER)
    assert r.status_code == 401 and r.json()["error"]["code"] == "missing_key"


# --- GET /v1/keys -----------------------------------------------------------------------------


def test_keys_in_a_browser_is_a_help_page_with_the_same_405(web, sqlite_store):
    r = web.get("/v1/keys", headers=BROWSER)
    main = assert_help_page(r, 405)
    assert r.headers["allow"] == "POST" and r.headers["vary"] == "Accept"
    assert "<h1>Keys are made with a POST</h1>" in main
    assert '<pre><code>curl -X POST <span class="origin">https://calotype.dev</span>/v1/keys</code></pre>' in main
    assert button_ref(r.text) == API_LINK_REF and f'href="/agents?ref={API_LINK_REF}"' in main
    assert sqlite_store.list_keys() == []  # a GET makes nothing
    r = web.get("/v1/keys?ref=hn-thread", headers=BROWSER)
    assert button_ref(r.text) == "hn-thread" and 'href="/agents?ref=hn-thread"' in assert_help_page(r, 405)
    r = web.get("/v1/keys", params={"ref": "<b>x</b>"}, headers=BROWSER)
    assert button_ref(r.text) == API_LINK_REF and "<b>x</b>" not in r.text


@pytest.mark.parametrize("method", ["HEAD", "PUT", "DELETE", "PATCH"])
def test_other_methods_on_keys_answer_as_before_for_a_browser(web, method):
    r = web.request(method, "/v1/keys", headers=BROWSER)
    assert r.status_code == 405 and r.headers["allow"] == "POST" and r.headers["content-type"] == "application/json"
    assert r.content in (b"", b'{"detail":"Method Not Allowed"}')


def test_the_api_schema_does_not_list_the_browser_route(web):
    paths = web.get("/openapi.json").json()["paths"]
    assert set(paths["/v1/keys"]) == {"post"} and "/keys" not in paths


# --- every other request is answered exactly as before -----------------------------------------

MCP_405 = (
    b'{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"This MCP endpoint takes POST only; '
    b'it opens no event stream.","data":{"code":"method_not_allowed"}}}'
)
MISSING_KEY = (
    b'{"error":{"code":"missing_key","message":"An API key is required: send \'Authorization: Bearer <key>\' '
    b"or '?access_key=<key>'.\"}}"
)
INVALID_KEY = b'{"error":{"code":"invalid_key","message":"The API key is not valid."}}'
JSON_NO_STORE = {"cache-control": "no-store", "content-type": "application/json"}
# Recorded from the code before these pages: (status, every header but Content-Length and X-Request-Id, body).
UNCHANGED = {
    "/mcp": (405, {**JSON_NO_STORE, "allow": "POST"}, MCP_405),
    "/mcp?ref=glama": (405, {**JSON_NO_STORE, "allow": "POST"}, MCP_405),
    "/v1/screenshot": (401, {**JSON_NO_STORE, "www-authenticate": "Bearer"}, MISSING_KEY),
    "/v1/screenshot?url=https://example.com": (401, {**JSON_NO_STORE, "www-authenticate": "Bearer"}, MISSING_KEY),
    "/v1/screenshot?url=https://example.com&access_key=cal_wrong": (
        401, {**JSON_NO_STORE, "www-authenticate": "Bearer"}, INVALID_KEY,
    ),
    "/v1/screenshot?url=https://example.com&access_key=" + "x" * 201: (401, JSON_NO_STORE, INVALID_KEY),
    "/v1/keys": (405, {"allow": "POST", "content-type": "application/json"}, b'{"detail":"Method Not Allowed"}'),
    "/v1/keys?ref=x": (405, {"allow": "POST", "content-type": "application/json"}, b'{"detail":"Method Not Allowed"}'),
}
NOT_A_BROWSER = [
    "*/*",
    "application/json",
    "text/event-stream",
    "application/json, text/event-stream",
    "text/html, text/event-stream",
    "text/plain, image/png",
    None,  # no Accept header at all
]


def assert_unchanged(client, path: str, accept: str | None) -> None:
    status, headers, body = UNCHANGED[path]
    del client.headers["accept"]  # the test client's default; each request sets its own, or none
    r = client.get(path, headers={} if accept is None else {"Accept": accept})
    sent = {k: v for k, v in r.request.headers.items() if k == "accept"}
    assert sent == ({} if accept is None else {"accept": accept})
    assert r.status_code == status
    assert r.content == body
    got = {k: v for k, v in r.headers.items() if k != "x-request-id"}
    assert got == {**headers, "content-length": str(len(body))}


@pytest.mark.parametrize("accept", NOT_A_BROWSER, ids=lambda a: a or "no-accept")
@pytest.mark.parametrize("path", list(UNCHANGED), ids=lambda p: p[:48])
def test_a_program_gets_the_same_answer_as_before(web, sqlite_store, path, accept):
    assert_unchanged(web, path, accept)
    web.app.state.funnel.wait()
    assert sqlite_store.funnel_days([current_day()])[0].api_page == 0


@pytest.mark.parametrize("accept", [BROWSER["Accept"], "*/*"])
@pytest.mark.parametrize("path", list(UNCHANGED), ids=lambda p: p[:48])
def test_without_the_site_a_browser_gets_the_same_answer_as_before(make_client, path, accept):
    off = make_client(serve_site=False)
    assert_unchanged(off, path, accept)
    assert off.get("/keys").status_code == 404 and off.post("/keys").status_code == 404


# --- the key button ---------------------------------------------------------------------------


def press(client, ref: str | None = None, **headers):
    data = {} if ref is None else {"ref": ref}
    return client.post("/keys", data=data, headers={**BROWSER, "Sec-Fetch-Site": "same-origin", **headers})


def test_the_button_page(web, sqlite_store):
    r = web.get("/keys", headers=BROWSER)
    main = assert_help_page(r, 200)
    assert "<h1>Get a free key</h1>" in main and button_ref(r.text) == ""
    button = button_of(r.text)
    assert '<button type="submit">Get a free key</button>' in button and BUTTON_LINE in button
    # The same words the docs use about what may be captured; no new promise.
    assert "Please capture only pages you are allowed to view." in button and 'href="/acceptable-use"' in button
    assert "<script" not in button  # a plain form: it works with no script
    assert web.get("/keys?ref=hn-thread").status_code == 200
    assert button_ref(web.get("/keys?ref=hn-thread").text) == "hn-thread"
    assert button_ref(web.get("/keys", params={"ref": '"><script>'}).text) == ""
    assert web.head("/keys").status_code == 200
    assert sqlite_store.list_keys() == []


def test_pressing_the_button_shows_a_working_key_once(web, sqlite_store, caplog):
    with caplog.at_level(logging.DEBUG):
        r = press(web)
    main = assert_help_page(r, 200)
    keys = set(KEY.findall(r.text))
    assert len(keys) == 1, "one key on the page"
    raw = keys.pop()
    assert "<h1>Your free key</h1>" in main
    assert f'<code class="newkey">{raw}</code>' in main
    assert "This is the only time the key is shown." in main and "cannot recover it" in main
    # A first screenshot and the MCP set-up, with the key filled in; the limits; the docs.
    assert f'curl -H "Authorization: Bearer {raw}"' in main and "/v1/screenshot?url=https://example.com" in main
    assert "claude mcp add --transport http calotype" in main and f'--header "Authorization: Bearer {raw}"' in main
    assert 'href="/agents"' in main and 'href="/docs"' in main and 'href="/acceptable-use"' in main
    assert "Free plan: 100 screenshots a month" in main and "30 seconds per capture" in main
    # The key is in the page's body and nowhere else: no header, no address, no log line, not in storage.
    assert all(raw not in v for v in r.headers.values()) and raw not in str(r.url)
    assert not r.history and raw not in caplog.text
    dump = stored_text(sqlite_store)
    assert raw not in dump and hash_key(raw) in dump
    # It is a free key like any other, and it works.
    key = sqlite_store.get_key(raw)
    assert key.plan == "free" and key.internal is False and key.label == f"ref:{WEB_REF}" and key.email is None
    shot = web.get("/v1/screenshot?url=https://example.com", headers={"Authorization": f"Bearer {raw}"})
    assert shot.status_code == 200
    # Shown once: no later page has it.
    for path in ("/keys", "/", "/agents"):
        assert raw not in web.get(path, headers=BROWSER).text
    assert KEY.search(web.get("/keys").text) is None


@pytest.mark.parametrize(
    "ref, label",
    [
        ("glama", "ref:glama"),
        ("try", "ref:try"),
        (MCP_LINK_REF, "ref:mcp-link"),
        (API_LINK_REF, "ref:api-link"),
        (None, "ref:web"),
        ("", "ref:web"),
        ("Bad Tag!", "ref:web"),
        ("x" * 41, "ref:web"),
        ('"><script>alert(1)</script>', "ref:web"),
    ],
)
def test_the_key_is_tagged_with_the_forms_ref_or_web(web, sqlite_store, ref, label):
    r = press(web, ref)
    assert r.status_code == 200 and "alert(1)" not in r.text
    assert [k.label for k in sqlite_store.list_keys()] == [label]


def test_the_figures_come_from_the_plans_and_settings_not_the_page(make_client):
    plans = {"free": Plan("free", 37, 0, "Free"), "pro": Plan("pro", 9000, 4900, "Pro")}
    c = make_client(plans=plans, capture_timeout_s=21, keys_per_ip_per_hour=1, keys_global_per_hour=77)
    line = "Free plan: 37 screenshots a month. No account, no email."
    for path in ("/", "/agents", "/keys"):
        assert line in c.get(path).text, path
    for path in ("/mcp", "/v1/keys", "/v1/screenshot"):
        assert line in c.get(path, headers=BROWSER).text, path
    made = press(c)
    assert "Free plan: 37 screenshots a month" in made.text and "21 seconds per capture" in made.text
    refused = press(c)
    assert refused.status_code == 429
    assert "The limit is 1 key an hour from one address and 77 an hour for everyone." in refused.text


def test_a_form_posted_from_another_site_is_refused(web, sqlite_store):
    r = press(web, "glama", **{"Sec-Fetch-Site": "cross-site"})
    main = assert_help_page(r, 403)
    assert "works from Calotype's own site" in main.replace("&#x27;", "'") and KEY.search(r.text) is None
    assert sqlite_store.list_keys() == []
    # It spent no slot: three keys can still be made. Browsers send same-origin; old ones send nothing.
    assert press(web, **{"Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert press(web, **{"Sec-Fetch-Site": "same-site"}).status_code == 200
    assert web.post("/keys").status_code == 200
    assert len(sqlite_store.list_keys()) == 3


def test_the_per_ip_limit_is_the_apis_own_and_gets_a_page(web, sqlite_store):
    assert press(web).status_code == 200
    assert web.post("/v1/keys").status_code == 201  # one limiter for both ways
    assert press(web).status_code == 200
    r = press(web)
    main = assert_help_page(r, 429)
    assert 1 <= int(r.headers["retry-after"]) <= 3600
    assert "<h1>No key was made</h1>" in main and KEY.search(r.text) is None
    assert "Too many keys were made recently" in main
    assert "The limit is 3 keys an hour from one address and 200 an hour for everyone." in main
    assert re.search(r"Try again in about \d+ minutes?\.", main)
    api = web.post("/v1/keys")
    assert api.status_code == 429 and api.json()["error"]["code"] == "rate_limited"
    assert len(sqlite_store.list_keys()) == 3


def test_the_limit_follows_the_trusted_proxy_hop_like_the_apis(make_client, sqlite_store):
    c = make_client(trusted_proxy_hops=1)
    for _ in range(3):
        assert press(c, **{"X-Forwarded-For": "203.0.113.5"}).status_code == 200
    assert press(c, **{"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}).status_code == 429
    assert press(c, **{"X-Forwarded-For": "198.51.100.8"}).status_code == 200
    dump = stored_text(sqlite_store)
    assert "203.0.113.5" not in dump and "198.51.100.8" not in dump


def test_the_overall_limit_applies_to_the_button(make_client):
    c = make_client(keys_global_per_hour=2, trusted_proxy_hops=1)
    assert press(c, **{"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert c.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 201
    assert press(c, **{"X-Forwarded-For": "198.51.100.3"}).status_code == 429


def test_with_self_serve_keys_off_there_is_no_button_and_no_key(make_client, sqlite_store):
    off = make_client(self_serve_keys=False)
    for path in ("/", "/agents"):
        assert 'class="getkey"' not in off.get(path).text
    r = off.get("/keys", headers=BROWSER)
    assert KEYS_OFF in assert_help_page(r, 200) and "<form" not in main_of(r.text)
    r = press(off)
    main = assert_help_page(r, 403)  # the status POST /v1/keys gives
    assert off.post("/v1/keys").status_code == 403
    assert KEYS_OFF in main and sqlite_store.list_keys() == []


def test_an_oversized_form_is_refused(web, sqlite_store):
    r = web.post("/keys", content=b"ref=" + b"a" * 9000, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413 and sqlite_store.list_keys() == []


# --- the button on the site's pages --------------------------------------------------------------


@pytest.mark.parametrize("path, before", [("/", 'id="why-hosted"'), ("/agents", 'id="create-key"')])
def test_home_and_agents_carry_the_button_near_their_curl_line(web, path, before):
    doc = web.get(path).text
    main = main_of(doc)
    button = button_of(doc)
    assert '<button type="submit">Get a free key</button>' in button and BUTTON_LINE in button
    curl = main.index("/v1/keys")
    assert curl < main.index(button) < main.index(before)
    assert main.index(button) - curl < 400, "right under the curl line"
    # Served from memory, so the tag is empty; the layout's script copies a valid ?ref= into it.
    assert button_ref(doc) == ""
    assert "form.getkey input[name=\"ref\"]" in doc and "if (!el.value) el.value = ref;" in doc


def test_the_try_result_carries_the_button_tagged_try(web, sqlite_store):
    r = web.post("/try", data={"url": "https://example.com"})
    assert r.status_code == 200 and "data:image/jpeg;base64," in r.text
    main = main_of(r.text)
    assert button_ref(r.text) == "try"
    command = "/v1/keys -d '{\"ref\":\"try\"}'</code></pre>"
    assert main.index(command) < main.index(button_of(r.text)) < main.index('id="again"')
    assert press(web, button_ref(r.text)).status_code == 200
    assert [k.label for k in sqlite_store.list_keys()] == ["ref:try"]


def test_the_try_limit_page_carries_the_button_and_drops_it_with_keys_off(make_client):
    limited = make_client(tries_per_ip_per_hour=1)
    assert limited.post("/try", data={"url": "https://example.com"}).status_code == 200
    r = limited.post("/try", data={"url": "https://example.com"})
    assert r.status_code == 429 and button_ref(r.text) == "try"
    off = make_client(self_serve_keys=False)
    r = off.post("/try", data={"url": "https://example.com"})
    assert r.status_code == 200 and 'class="getkey"' not in r.text and "/v1/keys -d" in r.text


def test_docs_say_what_a_browser_gets_and_name_the_button(web):
    docs = web.get("/docs").text
    assert 'id="browsers"' in docs and "a short help page, not JSON" in docs
    assert "<code>text/html</code> and not <code>text/event-stream</code>" in docs
    assert '<a href="/keys">Get a free key</a>' in docs
    privacy = web.get("/privacy").text
    assert "<code>web</code>, <code>try</code>, <code>mcp-link</code> or <code>api-link</code>" in privacy
    assert "a key was made with the button" in privacy and "was shown a help page" in privacy


# --- the funnel: api_page and web_key, on each backend --------------------------------------------


def counted(client, store) -> dict[str, int]:
    client.app.state.funnel.wait()
    day = store.funnel_days([current_day()])[0]
    return {e: getattr(day, e) for e in FUNNEL_EVENTS if getattr(day, e)}


def test_each_help_page_shown_counts_api_page(client, store):
    assert client.get("/mcp?ref=glama", headers=BROWSER).status_code == 405
    assert counted(client, store) == {"api_page": 1}
    assert client.get("/v1/screenshot?url=https://example.com", headers=BROWSER).status_code == 401
    assert counted(client, store) == {"api_page": 2}
    # The request's own steps still count: a wrong key is a bad_key, GET /v1/keys a key_miss.
    assert client.get("/v1/screenshot?url=https://example.com&access_key=cal_wrong", headers=BROWSER).status_code == 401
    assert client.get("/v1/keys", headers=BROWSER).status_code == 405
    assert counted(client, store) == {"api_page": 4, "bad_key": 1, "key_miss": 1}
    # A program on the same addresses is shown no page.
    for path in ("/mcp", "/v1/screenshot?url=https://example.com", "/v1/keys"):
        client.get(path)
        client.get(path, headers={"Accept": "text/html, text/event-stream"})
    assert counted(client, store) == {"api_page": 4, "bad_key": 1, "key_miss": 3}
    # Nor is the button's own page, or any site page, an api_page.
    client.get("/keys", headers=BROWSER)
    client.get("/", headers=BROWSER)
    assert counted(client, store)["api_page"] == 4


def test_a_key_made_with_the_button_counts_web_key_and_key_made(client, store):
    assert press(client, "glama").status_code == 200
    assert counted(client, store) == {"key_made": 1, "web_key": 1}
    assert client.post("/v1/keys").status_code == 201  # the API's keys are key_made only
    assert counted(client, store) == {"key_made": 2, "web_key": 1}
    assert press(client, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403  # nothing was asked of the key path
    assert counted(client, store) == {"key_made": 2, "web_key": 1}
    assert press(client).status_code == 200
    assert press(client).status_code == 429
    assert counted(client, store) == {"key_made": 3, "web_key": 2, "key_refused": 1}
    assert [k.label for k in store.list_keys()] == ["ref:glama", "", "ref:web"]


def test_the_new_counts_hold_no_key_url_ref_or_ip(client, store):
    headers = {**BROWSER, "User-Agent": "SomeBrowser/9 (probe-7c1d)", "X-Forwarded-For": "198.51.100.77"}
    client.get("/v1/screenshot?url=https://example.com/private-path-9f3a&access_key=cal_wrong_9d2e", headers=headers)
    client.get("/mcp?ref=secret-tag-4e", headers=headers)
    r = client.post("/keys", data={"ref": "secret-tag-4e"}, headers=headers)
    raw = KEY.search(r.text).group(0)
    assert counted(client, store) == {"api_page": 2, "bad_key": 1, "key_made": 1, "web_key": 1}
    rows = [d for d in store.funnel_days([current_day()])]
    assert all(set(vars(d)) == {"day", *FUNNEL_EVENTS} for d in rows)
    text = stored_text(store)
    for value in ("private-path-9f3a", "probe-7c1d", "198.51.100.77", "cal_wrong_9d2e", raw):
        assert value not in text
    # The tag is on the key's own record, as for any tagged key, and nowhere in the counts.
    assert text.count("secret-tag-4e") == 1


def test_a_stored_day_without_the_new_steps_reads_as_zero(store):
    # A day written before api_page and web_key existed has neither field (Firestore) or row (SQLite).
    for event in ("looked", "key_made", "first_capture"):
        store.record_funnel("2026-11-20", event)
    day = store.funnel_days(["2026-11-20"])[0]
    assert (day.looked, day.key_made, day.first_capture) == (1, 1, 1)
    assert (day.api_page, day.web_key) == (0, 0)
    store.record_funnel("2026-11-20", "api_page")
    store.record_funnel("2026-11-20", "web_key")
    store.record_funnel("2026-11-20", "web_key")
    day = store.funnel_days(["2026-11-20"])[0]
    assert (day.api_page, day.web_key, day.key_made) == (1, 2, 1)
