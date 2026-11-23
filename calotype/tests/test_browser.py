"""Tests that drive real Chromium against servers on 127.0.0.1.

The API is built with an exact exception for the test page's (ip, port), as
a deployment cannot do. Every other loopback port, name and address stays
blocked, and a second "canary" server records any request that reaches it.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from calotype.app import create_app
from calotype.capture import Renderer
from calotype.ssrf import AddressPolicy

from .conftest import LocalServer, fake_resolver


@pytest.fixture
def store(sqlite_store):
    """Browser tests check rendering and SSRF, not storage: SQLite only."""
    return sqlite_store


def html(body: str):
    return lambda _s: (200, {"Content-Type": "text/html; charset=utf-8"}, body.encode())


@pytest.fixture
def canary():
    with LocalServer() as s:
        yield s


@pytest.fixture
def site(canary):
    c = canary.port
    routes = {
        "/": html(
            "<!doctype html><html><head><style>html,body{margin:0;background:#ff0000}"
            "@media (prefers-color-scheme: dark){html,body{background:#0000ff}}</style></head>"
            "<body><h1>Calotype test page</h1></body></html>"
        ),
        "/tall": html("<!doctype html><body style='margin:0'><div style='height:30000px;background:#0f0'></div></body>"),
        # Sections that stay invisible until they scroll into view, as many marketing pages do.
        "/reveal": html(
            "<!doctype html><style>body{margin:0;background:#fff}"
            ".s{height:900px;opacity:0;transition:opacity 2s;background:#00f}.s.in{opacity:1}</style>"
            "<body><div style='height:900px'></div><div class='s'></div><div class='s'></div><div class='s'></div>"
            "<script>const o=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting)e.target.classList.add('in')}),"
            "{threshold:0.1});document.querySelectorAll('.s').forEach(e=>o.observe(e));</script></body>"
        ),
        # A page that grows every time it is scrolled: the scroll pass must not run on.
        "/endless": html(
            "<!doctype html><body style='margin:0;background:#fff'><div style='height:1200px'></div>"
            "<script>addEventListener('scroll',()=>{const d=document.createElement('div');"
            "d.style.height='1500px';document.body.appendChild(d)});</script></body>"
        ),
        # A page that tries to reach loopback every way a page can.
        "/ssrf": html(
            f"""<!doctype html><body style="background:#fff">
            <img src="http://127.0.0.1:{c}/img">
            <img src="http://localhost:{c}/img-localhost">
            <img src="http://[::1]:{c}/img-v6">
            <img src="http://2130706433:{c}/img-decimal">
            <img src="http://rebind.example:{c}/img-rebind">
            <iframe src="http://127.0.0.1:{c}/iframe"></iframe>
            <link rel="stylesheet" href="http://127.0.0.1:{c}/css">
            <script>
              fetch("http://127.0.0.1:{c}/fetch").catch(() => {{}});
              fetch("http://rebind.example:{c}/fetch-rebind").catch(() => {{}});
              try {{ new WebSocket("ws://127.0.0.1:{c}/ws"); }} catch (e) {{}}
              navigator.sendBeacon && navigator.sendBeacon("http://127.0.0.1:{c}/beacon", "x");
            </script></body>"""
        ),
        "/redirect": lambda _s: (302, {"Location": f"http://127.0.0.1:{c}/redirected"}, b""),
        "/redirect-meta": html(f"<meta http-equiv='refresh' content='0;url=http://127.0.0.1:{c}/meta'>"),
        "/redirect-file": lambda _s: (302, {"Location": "file:///etc/passwd"}, b""),
        # 3 MB of HTML: the main document itself crosses a 1 MB cap.
        "/big": html("<!doctype html><p>big</p><!--" + "x" * (3 * 1024 * 1024) + "-->"),
    }
    with LocalServer(routes) as s:
        yield s


async def resolver_with_rebind(host, port):
    return await fake_resolver(host, port)


@pytest.fixture
def make_client(settings, store, site):
    clients = []

    def _make(**renderer_kwargs):
        policy = AddressPolicy(resolver=resolver_with_rebind, exceptions=frozenset({("127.0.0.1", site.port)}))
        renderer = Renderer(policy, **renderer_kwargs)
        app = create_app(settings, policy=policy, storage=store, renderer=renderer)
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_end_to_end_screenshot(make_client, site, free_key):
    client = make_client()
    r = client.get("/v1/screenshot", params={"url": site.url("/"), "width": 400, "height": 300}, headers=auth(free_key))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-calotype-target-status"] == "200"
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (400, 300)
    assert img.convert("RGB").getpixel((200, 250)) == (255, 0, 0)

    # Same page: dark mode, JPEG, 2x scale.
    r = client.post(
        "/v1/screenshot",
        json={"url": site.url("/"), "width": 200, "height": 150, "dark_mode": True, "format": "jpeg",
              "device_scale_factor": 2},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert img.size == (400, 300)
    rr, gg, bb = img.getpixel((200, 280))
    assert bb > 200 and rr < 50

    # Full page of a 30,000px page is capped at 20,000px (PNG) and at WebP's 16,383px limit.
    r = client.get(
        "/v1/screenshot",
        params={"url": site.url("/tall"), "width": 320, "height": 200, "full_page": "true"},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (320, 20000)
    assert img.convert("RGB").getpixel((100, 19990)) == (0, 255, 0)
    assert r.headers["x-calotype-page-height"] == "30000"
    # A default capture is the first screen; the header says how tall the whole page is.
    first = client.get(
        "/v1/screenshot", params={"url": site.url("/tall"), "width": 320, "height": 200}, headers=auth(free_key)
    )
    assert first.status_code == 200 and Image.open(io.BytesIO(first.content)).size == (320, 200)
    assert first.headers["x-calotype-page-height"] == "30000"
    r = client.get(
        "/v1/screenshot",
        params={"url": site.url("/tall"), "width": 320, "height": 200, "full_page": "true", "format": "webp"},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(r.content)).size == (320, 16383)


def test_page_requests_to_loopback_are_blocked(make_client, site, canary, free_key):
    client = make_client()
    r = client.get(
        "/v1/screenshot",
        params={"url": site.url("/ssrf"), "wait_until": "networkidle", "delay_ms": 300},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert canary.hits == [], f"page reached the canary: {canary.hits}"


@pytest.mark.parametrize("path", ["/redirect", "/redirect-meta"])
def test_redirect_to_loopback_is_blocked(make_client, site, canary, free_key, path):
    client = make_client()
    r = client.get("/v1/screenshot", params={"url": site.url(path), "delay_ms": 300}, headers=auth(free_key))
    assert canary.hits == []
    if path == "/redirect":
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "blocked_target"


def test_redirect_to_file_is_blocked(make_client, site, free_key):
    client = make_client()
    r = client.get("/v1/screenshot", params={"url": site.url("/redirect-file")}, headers=auth(free_key))
    assert r.status_code == 422, r.text


def test_initial_loopback_url_refused(make_client, canary, free_key):
    client = make_client()
    r = client.get("/v1/screenshot", params={"url": canary.url("/")}, headers=auth(free_key))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "blocked_target"
    assert canary.hits == []


def test_page_byte_cap(make_client, site, free_key):
    """A page past the byte cap is cut off quickly, not left to time out."""
    client = make_client(max_page_bytes=1024 * 1024)
    r = client.get("/v1/screenshot", params={"url": site.url("/big")}, headers=auth(free_key))
    if r.status_code == 200:
        assert r.headers.get("x-calotype-truncated") == "1"
    else:
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "page_too_large"
    assert int(r.elapsed.total_seconds()) < 10


def test_control_ssrf_page_does_reach_canary_when_allowed(settings, store, site, canary, free_key):
    """Control for the test above: with the canary allowed, the same page reaches it.

    This shows the blocking test is not passing because the page never asked.
    The proxy, not the router, must be what stops rebind.example: the router
    lets that name through, and only the proxy resolves it to loopback.
    """
    policy = AddressPolicy(
        resolver=resolver_with_rebind,
        exceptions=frozenset({("127.0.0.1", site.port), ("127.0.0.1", canary.port)}),
    )
    app = create_app(settings, policy=policy, storage=store, renderer=Renderer(policy))
    with TestClient(app) as client:
        r = client.get(
            "/v1/screenshot",
            params={"url": site.url("/ssrf"), "wait_until": "networkidle", "delay_ms": 300},
            headers=auth(free_key),
        )
    assert r.status_code == 200, r.text
    paths = set(canary.hits)
    assert {"/img", "/fetch", "/iframe", "/img-rebind", "/fetch-rebind"} <= paths, paths


def test_end_to_end_try_returns_an_inline_jpeg_and_keeps_the_policy(make_client, site, canary):
    import base64
    import re

    client = make_client()
    r = client.post("/try", data={"url": site.url("/")})
    assert r.status_code == 200, r.text
    m = re.search(r'<img src="data:image/jpeg;base64,([A-Za-z0-9+/=]+)"', r.text)
    assert m
    img = Image.open(io.BytesIO(base64.b64decode(m.group(1))))
    assert img.format == "JPEG" and img.size == (1280, 800)
    rr, gg, bb = img.convert("RGB").getpixel((640, 700))
    assert rr > 200 and gg < 60 and bb < 60  # the test page's red, through JPEG

    # A page that tries every way to reach loopback reaches nothing, as with a key.
    r = client.post("/try", data={"url": site.url("/ssrf")})
    assert r.status_code == 200, r.text
    assert canary.hits == [], f"page reached the canary: {canary.hits}"
    # A redirect to loopback is refused in the browser, with the keyed call's error.
    r = client.post("/try", data={"url": site.url("/redirect")})
    assert r.status_code == 422 and "<img" not in r.text
    assert canary.hits == []


def test_a_full_page_capture_scrolls_through_first_so_revealed_sections_show(make_client, site, free_key):
    client = make_client()
    r = client.get(
        "/v1/screenshot",
        params={"url": site.url("/reveal"), "width": 320, "height": 200, "full_page": "true"},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    assert img.size == (320, 3600)
    # Every revealed section is drawn, down to the last one; unscrolled they would be white.
    for y in (1000, 2300, 3500):
        assert img.getpixel((160, y)) == (0, 0, 255), y
    assert img.getpixel((160, 100)) == (255, 255, 255)  # and the capture starts at the top again

    # A page that grows on every scroll still finishes, inside the height cap.
    r = client.get(
        "/v1/screenshot",
        params={"url": site.url("/endless"), "width": 320, "height": 200, "full_page": "true", "format": "jpeg"},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert Image.open(io.BytesIO(r.content)).size[1] <= 20000
    assert int(r.headers["x-calotype-duration-ms"]) < 20000
