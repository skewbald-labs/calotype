"""Pages for a person in a browser: help on the API-only addresses, and the "Get a free key" button.

People, not programs, arrive at three addresses that only answer programs:
directories list ``/mcp`` as a link, examples show ``/v1/screenshot?url=...``,
and ``/v1/keys`` looks like a page. Each used to answer a browser with a bare
JSON error. Now a request whose ``Accept`` names ``text/html`` and no event
stream (``pages.wants_html``) gets a short page in the site's layout instead,
with the **same status and the same ``Allow`` or ``WWW-Authenticate`` header**
as the JSON answer. Every other request gets exactly the answer it got before.

``GET /mcp`` (405)
    Says the address is an MCP endpoint, shows it, links the set-up on
    ``/agents``, and offers the try box and the key button.
``GET /v1/screenshot`` with no key or a wrong key (401)
    Says the link needs a key. If its ``url`` is one the try box would take,
    links to ``/try?url=...``, which only fills the box in: nothing is captured
    by this request. Then the key button. Any other error stays JSON.
``GET /v1/keys`` (405)
    Says keys are made with a POST, shows the curl line and the key button.

The pages set no cookie, need no script and load nothing from other hosts.
They are ``no-store`` and ``noindex``. Nothing from the request is written
into a page except a valid ``ref`` tag and the ``url`` to try, both escaped;
an ``access_key`` in the address is never shown.

The key button (``site/key-form.html``, placed by ``pages.key_form``) is a
plain form that posts to ``POST /keys``. That route makes the key through the
app's one key-making function, the same one ``POST /v1/keys`` uses, so the
per-IP limit, the overall limit and the switch for self-serve keys are shared,
not copied. Like the try box it refuses a form posted from another site. The
answer is a page that shows the key once: ``no-store``, ``no-referrer``,
``noindex``. The key is in that page's body and nowhere else: no redirect, no
address, no cookie, no log line. A refusal is a page with the status the API
would give. ``GET /keys`` shows the button on a page of its own.

A key made with the button is labelled with the form's ``ref`` tag when it is
a valid one, else ``web``. The help pages send the tag of the link that was
opened (``/mcp?ref=<tag>``), or ``mcp-link`` and ``api-link``.

Funnel (``funnel.py``): each help page shown counts ``api_page``; each key the
button makes counts ``web_key`` and ``key_made``; a refusal counts
``key_refused``.
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from . import funnel
from .config import Settings
from .pages import TryLimits, key_form, posted_from_another_site, render_document, try_form, wants_html
from .ssrf import BlockedTarget, parse_target_url
from .storage import REF_TAG
from .tries import normalize_try_url, try_params

# The tag on a key made with the button when the form carries none.
WEB_REF = "web"
# The tags the help pages send when the link that was opened carried none.
MCP_LINK_REF = "mcp-link"
API_LINK_REF = "api-link"

MAX_FORM_BYTES = 8 * 1024

ORIGIN = '<span class="origin">https://calotype.dev</span>'
# The help pages share their address with a JSON answer; which one is sent depends on Accept.
VARY = {"Vary": "Accept"}
KEYS_OFF = "Self-serve keys are not available right now."
ALLOWED_TO_VIEW = (
    'Please capture only pages you are allowed to view. The <a href="/acceptable-use">acceptable use policy</a> '
    "has the rules."
)


def valid_ref(value: Any) -> str:
    """``value`` if it is a source tag under the rules for ``?ref=`` (``storage.REF_TAG``), else ``""``."""
    return value if isinstance(value, str) and REF_TAG.fullmatch(value) else ""


class BrowserPages:
    """Builds the help pages and the key pages for one app."""

    def __init__(self, *, site_dir: Path, settings: Settings, tries: TryLimits):
        self.site_dir = site_dir
        self.settings = settings
        self.tries = tries
        free = settings.plans.get("free")
        # None where a visitor cannot make a key: then no page shows the button.
        self.free_quota: int | None = free.monthly_quota if free and settings.self_serve_keys else None

    # --- shared pieces ----------------------------------------------------------------------

    def respond(
        self, path: str, title: str, description: str, content: str, status: int, headers: dict | None = None
    ) -> Response:
        doc = render_document(
            self.site_dir, path=path, title=title, description=description, content=content, noindex=True
        )
        return Response(
            doc.body,
            status_code=status,
            media_type="text/html; charset=utf-8",
            headers={
                # An answer to one request, sometimes with a key in it: kept by no cache.
                "Cache-Control": "no-store",
                "Content-Security-Policy": doc.csp,
                "X-Content-Type-Options": "nosniff",
                # The address may carry an access_key or a page address; a link from here sends neither on.
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex",
                **(headers or {}),
            },
        )

    def button(self, ref: str) -> str:
        """The key button with its tag, or one line where keys are switched off."""
        return key_form(self.site_dir, self.free_quota, ref) or f"  <p>{KEYS_OFF}</p>\n"

    def try_target(self, raw: str) -> str | None:
        """The page a link's ``url`` names, as the try box would take it; None if the box would refuse it.

        The same normalisation and the same checks the try box makes before any lookup:
        nothing is resolved and nothing is rendered here.
        """
        if not self.tries.on or not raw.strip() or "access_key" in raw.lower():
            # A target that itself carries an access_key is left out, so no key is ever echoed.
            return None
        try:
            url = try_params(normalize_try_url(raw)).url
            parse_target_url(url)
        except (ValidationError, BlockedTarget):
            return None
        return url

    # --- the three help pages ----------------------------------------------------------------

    def mcp(self, request: Request) -> Response | None:
        """For ``GET /mcp`` from a browser; None for anything else, which keeps its JSON 405."""
        if request.method != "GET" or not wants_html(request):
            return None
        carried = valid_ref(request.query_params.get("ref"))
        ref = carried or MCP_LINK_REF
        endpoint = f"{ORIGIN}/mcp" + (f"?ref={html.escape(carried)}" if carried else "")
        box = try_form(self.site_dir, self.tries)
        try_section = f"""
  <h2 id="try">See a screenshot first</h2>
  <p>Calotype gives an agent one tool, <code>screenshot</code>, which returns an image of a public web page. You can try it here with no key:</p>
{box}""" if box else ""
        content = f"""  <h1>This is Calotype's MCP endpoint</h1>
  <p class="lede">This address is for an MCP client, such as an AI agent or a code editor. It is not a page for a browser.</p>
  <p>To use it, give your MCP client this address, with a Calotype key in the header <code>Authorization: Bearer cal_...</code>:</p>
<pre><code>{endpoint}</code></pre>
  <p>Set-up for Claude Code, Cursor, VS Code and other clients is on <a href="/agents?ref={html.escape(ref)}">For agents</a>.</p>

  <h2 id="key">Get a free key</h2>
{self.button(ref)}{try_section}"""
        funnel.mark(request, "api_page")
        return self.respond(
            "/mcp", "Calotype's MCP endpoint | Calotype",
            "This address is Calotype's MCP endpoint, for an MCP client and not for a browser.",
            content, 405, {"Allow": "POST", **VARY},
        )

    def screenshot(self, request: Request, code: str, headers: dict[str, str]) -> Response | None:
        """For ``GET /v1/screenshot`` from a browser with no key or a wrong one; None for anything else.

        ``code`` and ``headers`` are the JSON error's: the page keeps its 401 and its ``WWW-Authenticate``.
        """
        if request.method != "GET" or code not in ("missing_key", "invalid_key") or not wants_html(request):
            return None
        ref = valid_ref(request.query_params.get("ref")) or API_LINK_REF
        if code == "missing_key":
            heading, problem = "This link needs a key", "This link has no key."
        elif request.headers.get("authorization"):
            heading, problem = "The key is not valid", "The key sent with this request is not a valid key."
        else:
            heading = "The key in this link is not valid"
            problem = "The key in this link is not a valid key. It may be a placeholder from an example."
        target = self.try_target(request.query_params.get("url", ""))
        if target:
            offer = f"""  <p><a class="button" href="/try?url={html.escape(quote(target, safe=""))}">See this page captured now, no key needed</a></p>
  <p class="muted">That opens the try box with <code>{html.escape(target)}</code> filled in. Nothing is captured until you press its button.</p>
"""
        elif self.tries.on:
            offer = '  <p>To see a screenshot with no key, use the <a href="/try">try box</a>.</p>\n'
        else:
            offer = ""
        content = f"""  <h1>{heading}</h1>
  <p class="lede">This address is Calotype's screenshot API. It returns an image to a program that sends an API key. {problem}</p>
{offer}
  <h2 id="key">Get a free key</h2>
{self.button(ref)}  <p>Then send the key in the header <code>Authorization: Bearer cal_...</code>, or add it to the link as <code>access_key</code>. Every parameter is in the <a href="/docs#screenshot">API reference</a>. To give an AI agent the same tool, see <a href="/agents?ref={html.escape(ref)}">For agents</a>.</p>
"""
        funnel.mark(request, "api_page")
        return self.respond(
            "/v1/screenshot", f"{heading} | Calotype",
            "This address is Calotype's screenshot API. It needs an API key.",
            content, 401, {**headers, **VARY},
        )

    def keys(self, request: Request) -> Response | None:
        """For ``GET /v1/keys`` from a browser; None for anything else, which keeps its JSON 405."""
        if request.method != "GET" or not wants_html(request):
            return None
        ref = valid_ref(request.query_params.get("ref")) or API_LINK_REF
        content = f"""  <h1>Keys are made with a POST</h1>
  <p class="lede">This address makes a free Calotype key when a program sends it a POST request. A browser sends a GET, which makes nothing.</p>
  <p>From a terminal:</p>
<pre><code>curl -X POST {ORIGIN}/v1/keys</code></pre>
  <p>Or use the button. It makes the same kind of key:</p>
{self.button(ref)}  <p>How to add the key to an agent is on <a href="/agents?ref={html.escape(ref)}">For agents</a>. The request and its answer are in the <a href="/docs#keys">API reference</a>.</p>
"""
        funnel.mark(request, "api_page")
        return self.respond(
            "/v1/keys", "Keys are made with a POST | Calotype",
            "This address makes a free Calotype key when a program sends it a POST request.",
            content, 405, {"Allow": "POST", **VARY},
        )

    # --- the key button's own pages ------------------------------------------------------------

    def key_button_page(self, ref: str = "", problem: str = "", status: int = 200, headers: dict | None = None) -> Response:
        """``GET /keys``: the button on a page of its own. Also the answer to a form that was refused unread."""
        alert = f'  <p class="error" role="alert">{html.escape(problem)}</p>\n' if problem else ""
        content = f"""  <h1>Get a free key</h1>
  <p class="lede">A Calotype key lets an AI agent or a program take screenshots of public web pages. The button makes one on the free plan.</p>
{alert}{self.button(ref)}  <p>A program or an agent can make its own key with one request:</p>
<pre><code>curl -X POST {ORIGIN}/v1/keys</code></pre>
  <p class="muted">For a key made here, Calotype's database keeps a hash of the key, its plan, when it was made, a short tag for the page the button was on, and how many screenshots it has taken. Not your name, an email address or your IP address. The <a href="/privacy">privacy notice</a> has the details.</p>
"""
        return self.respond(
            "/keys", "Get a free key | Calotype",
            "Make a free Calotype key with one button: no account and no email.",
            content, status, headers,
        )

    def key_made(self, raw: str) -> Response:
        """The page that shows a new key, once."""
        key = html.escape(raw)
        plan = self.settings.plans["free"]
        content = f"""  <h1>Your free key</h1>
  <div class="keybox shown">
    <p><strong>This is the only time the key is shown.</strong> Calotype stores only its hash and cannot recover it, so copy it now.</p>
    <div class="keyrow"><code class="newkey">{key}</code></div>
  </div>

  <h2 id="first">Take a first screenshot</h2>
<pre><code>curl -H "Authorization: Bearer {key}" \\
  "{ORIGIN}/v1/screenshot?url=https://example.com" \\
  -o example.png</code></pre>

  <h2 id="agent">Add it to your agent</h2>
  <p>In Claude Code:</p>
<pre><code>claude mcp add --transport http calotype {ORIGIN}/mcp \\
  --header "Authorization: Bearer {key}"</code></pre>
  <p>Cursor, VS Code, Codex and other clients are on <a href="/agents">For agents</a>.</p>

  <h2 id="limits">Limits</h2>
  <ul>
    <li>{html.escape(plan.title or "Free")} plan: {plan.monthly_quota:,} screenshots a month, counted per calendar month in UTC.</li>
    <li>Only a capture that returns an image counts.</li>
    <li>{self.settings.capture_timeout_s} seconds per capture.</li>
  </ul>
  <p>Every parameter, error and limit is in the <a href="/docs">API reference</a>. {ALLOWED_TO_VIEW}</p>
"""
        return self.respond(
            "/keys", "Your free key | Calotype", "A new free Calotype key, shown once.", content, 200
        )

    def key_refused(self, code: str, message: str, status: int, headers: dict | None, retry_after_s: int = 0) -> Response:
        """The page for a key the shared path refused, with the status the API gives."""
        if code == "rate_limited":
            minutes = max(1, -(-int(retry_after_s) // 60))
            s = self.settings
            message = (
                f"Too many keys were made recently, so this one was not made. The limit is "
                f"{s.keys_per_ip_per_hour:,} key{'s' if s.keys_per_ip_per_hour != 1 else ''} an hour from one "
                f"address and {s.keys_global_per_hour:,} an hour "
                f"for everyone. Try again in about {minutes:,} minute{'s' if minutes != 1 else ''}."
            )
        content = f"""  <h1>No key was made</h1>
  <p class="error" role="alert">{html.escape(message)}</p>
  <p>A key you already have still works. How to use one is on <a href="/agents">For agents</a> and in the <a href="/docs">API reference</a>.</p>
"""
        return self.respond(
            "/keys", "No key was made | Calotype", "The key could not be made.", content, status, headers
        )


def add_key_routes(
    app: FastAPI,
    pages: BrowserPages,
    *,
    issue_key: Callable[..., Awaitable[tuple]],
    api_error: type[Exception],
) -> None:
    """``GET /keys`` shows the key button; ``POST /keys`` makes a key through ``issue_key`` and shows it once.

    ``issue_key(request, ref=..., default_ref=...)`` is the app's one key-making function, shared
    with ``POST /v1/keys``: it applies every limit and raises ``api_error`` for a refusal.
    """

    async def key_page(request: Request) -> Response:
        # A link like /keys?ref=<tag> carries its tag into the form.
        return pages.key_button_page(valid_ref(request.query_params.get("ref")))

    async def make_key(request: Request) -> Response:
        if posted_from_another_site(request):
            return pages.key_button_page("", "The key button works from Calotype's own site. Press it here.", 403)
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return pages.key_button_page("", "The form could not be read. Press the button again.", 413)
        form = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
        ref = valid_ref((form.get("ref") or [""])[0])
        try:
            raw, *_ = await issue_key(request, ref=ref, default_ref=WEB_REF)
        except api_error as e:
            funnel.mark(request, "key_refused")
            extra = getattr(e, "extra", None) or {}
            return pages.key_refused(
                getattr(e, "code", ""), getattr(e, "message", str(e)), getattr(e, "status", 400),
                getattr(e, "headers", None), extra.get("retry_after_s", 0),
            )
        funnel.mark(request, "key_made")
        funnel.mark(request, "web_key")
        return pages.key_made(raw)

    app.add_route("/keys", key_page, methods=["GET"], include_in_schema=False)
    app.add_route("/keys", make_key, methods=["POST"], include_in_schema=False)


__all__ = ["API_LINK_REF", "MCP_LINK_REF", "WEB_REF", "BrowserPages", "add_key_routes", "valid_ref"]
