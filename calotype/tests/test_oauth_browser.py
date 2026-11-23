"""The consent page in real Chromium: after Approve, the browser must get back to the app.

``test_oauth.py`` reads the 303 itself, so it cannot see what a browser does with
it. Chromium applies a page's ``form-action`` to every hop of the redirect that
answers a form. The apps' callbacks redirect on (``https://vscode.dev/redirect``
answers 302 to ``vscode://...``, ``https://www.cursor.com/agents/mcp/oauth/callback``
answers 308 to ``cursor.com``), and with ``form-action 'self' <redirect origin>``
on the consent page the browser stopped there: a key was made and the person
stayed on the page.

The app listens on a real port on 127.0.0.1. A second local server stands in for
an app's callback; the browser resolves no other name, so nothing leaves the machine.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from calotype.app import create_app
from calotype.ssrf import AddressPolicy

from .conftest import LocalServer, fake_resolver
from .test_oauth import SECRET, pkce

LOOPBACK_ONLY = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"


@pytest.fixture
def store(sqlite_store):
    """These tests are about the browser, not storage: SQLite only."""
    return sqlite_store


@pytest.fixture
def served(settings, store, fake_renderer):
    """The app on a real port, its own origin as the OAuth issuer. Yields that origin."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = create_app(
        replace(settings, oauth_secret=SECRET, public_url=origin),
        policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture
def chromium():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[LOOPBACK_ONLY])
        try:
            yield browser
        finally:
            browser.close()


def authorize_url(origin: str, redirect_uri: str, name: str) -> str:
    r = httpx.post(f"{origin}/oauth/register", json={"client_name": name, "redirect_uris": [redirect_uri]})
    assert r.status_code == 201, r.text
    _, challenge = pkce()
    return f"{origin}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": r.json()["client_id"], "redirect_uri": redirect_uri, "state": "st-123",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{origin}/mcp",
    })


def approve(chromium, url: str):
    """Open the consent page, press Approve; return (the console's lines, the URLs the browser asked for)."""
    page = chromium.new_page()
    console: list[str] = []
    asked: list[str] = []
    page.on("console", lambda m: console.append(m.text))
    page.on("request", lambda rq: asked.append(rq.url))
    assert page.goto(url).status == 200
    page.click('button[value="approve"]')
    return page, console, asked


def wait_for(condition, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_the_browser_follows_a_callback_that_redirects_to_another_origin(served, chromium):
    # Like www.cursor.com -> cursor.com: 127.0.0.1 and localhost are two origins.
    with LocalServer() as app_site:
        app_site.routes["/callback"] = lambda s: (
            302, {"Location": f"http://localhost:{s.port}/landed?" + s.hits[-1].partition("?")[2]}, b"")
        page, console, _ = approve(chromium, authorize_url(served, app_site.url("/callback"), "Hop to another host"))
        assert wait_for(lambda: any(h.startswith("/landed?") for h in app_site.hits)), (console, app_site.hits)
        landed = parse_qs(urlsplit(next(h for h in app_site.hits if h.startswith("/landed?"))).query)
    assert landed["state"] == ["st-123"] and landed["iss"] == [served] and landed["code"][0]
    assert not [line for line in console if "Content Security Policy" in line]


def test_the_browser_follows_a_callback_that_redirects_to_an_apps_scheme(served, chromium):
    # Like https://vscode.dev/redirect, which answers 302 to vscode://...
    with LocalServer() as app_site:
        app_site.routes["/redirect"] = lambda s: (
            302, {"Location": "vscode://dynamicauthprovider/calotype/authorize?" + s.hits[-1].partition("?")[2]}, b"")
        page, console, asked = approve(chromium, authorize_url(served, app_site.url("/redirect"), "Hop to a scheme"))
        assert wait_for(lambda: any(u.startswith("vscode://dynamicauthprovider/") for u in asked)), (console, asked)
    assert "code=" in next(u for u in asked if u.startswith("vscode://"))
    assert not [line for line in console if "Content Security Policy" in line]


def test_the_browser_follows_a_redirect_uri_with_an_editors_scheme(served, chromium):
    page, console, asked = approve(
        chromium, authorize_url(served, "cursor://anysphere.cursor-mcp/oauth/callback", "Cursor"))
    assert wait_for(lambda: any(u.startswith("cursor://anysphere.cursor-mcp/oauth/callback?") for u in asked)), (
        console, asked)
    assert not [line for line in console if "Content Security Policy" in line]
