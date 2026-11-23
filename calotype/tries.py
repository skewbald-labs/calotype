"""Free tries: paste a page address on the home page, see its screenshot, no key.

The home page's box (``site/try-form.html``) is a plain HTML form that posts
to ``/try``; it works with no JavaScript. ``/try`` renders the answer as a page
in the site's layout, with the image inline as a ``data:`` URL. The site's
Content-Security-Policy already allows ``img-src 'self' data:``, so nothing
was loosened. The image is sent in that one response (``Cache-Control:
no-store``) and kept nowhere.

The form posts rather than gets, so the page address never sits in the
request line (or a browser's history), and a link or a link preview cannot
start a capture.

Limits, all on the server:

* Fixed settings: 1280 x 800, not full page, JPEG at quality 70, ads blocked,
  the normal timeouts. The visitor chooses only the address.
* Per client IP: ``tries_per_ip_per_hour`` (5) in memory only, found and
  forgotten exactly as for key creation (``ratelimit.py``). Never stored or logged.
* For everyone: ``tries_per_day`` (300) renders per UTC day, counted in
  storage (``Storage.try_admit``), so every instance shares the cap. It is
  taken before the capture and not given back when the capture fails.
* The address policy (``ssrf.py``) runs exactly as for a keyed call, and the
  renderer applies it again in the page and at its proxy.
* A form from another site (``Sec-Fetch-Site: cross-site``) is refused, so no
  other page can spend its visitors' tries or the day's cap.

Each try is counted per UTC day by outcome (``storage.TryDay``): no URL,
host or IP address. Each logs one ``try`` line, like a screenshot's, with the
target as scheme and host only. ``python -m calotype.admin tries`` prints the
counts.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from .capture import CaptureError, CaptureResult
from .config import Settings
from .logs import log_event
from .pages import TryLimits, key_form, posted_from_another_site, render_document, try_form
from .params import ScreenshotParams
from .ratelimit import client_ip
from .ssrf import MAX_URL_LENGTH, AddressPolicy, BlockedTarget, ParsedTarget
from .storage import current_day

log = logging.getLogger("calotype.try")

WIDTH, HEIGHT = 1280, 800
FORMAT, QUALITY = "jpeg", 70
MAX_FORM_BYTES = 8 * 1024

# The source tag on a key made from a try: in the key command, the MCP URL and the key button.
TRY_REF = "try"

TITLE = "Try Calotype: a screenshot of any public page"
DESCRIPTION = "Paste the address of a public web page and see Calotype's screenshot of it, with no key."


def try_params(url: str) -> ScreenshotParams:
    """The fixed, cheap settings of a try. Only the address comes from the visitor."""
    return ScreenshotParams(
        url=url, width=WIDTH, height=HEIGHT, full_page=False, format=FORMAT, quality=QUALITY, block_ads=True
    )


def normalize_try_url(raw: str) -> str:
    """Accept ``example.com`` as well as ``https://example.com``."""
    url = raw.strip()
    return url if "://" in url else "https://" + url


@dataclass(frozen=True)
class Outcome:
    """What one try ends as: the count it adds to, the page's status and its text."""

    name: str  # one of storage.TRY_OUTCOMES
    status: int
    message: str = ""
    headers: dict | None = None


def key_steps(free_quota: int | None, button: str = "") -> str:
    """The next step after a try: a free key in one line or one click, the MCP URL, and the agents page.

    The key command and the MCP URL carry the tag ``try``: a key made with
    ``{"ref": "try"}`` is labelled with it (app.py), and an untagged key is
    labelled on its first call to ``/mcp?ref=try`` (remote_mcp.py). The
    ``/agents?ref=try`` page's key button sends the same tag, and so does
    ``button``, the "Get a free key" form (``pages.key_form``) when keys can be made.
    """
    quota = f" and comes with {free_quota:,} screenshots a month" if free_quota else ""
    click = f"  <p>Or with one click:</p>\n{button}" if button else ""
    return f"""  <p>Make a free key in one line. It asks for nothing{quota}:</p>
<pre><code>curl -X POST <span class="origin">https://calotype.dev</span>/v1/keys -d '{{"ref":"try"}}'</code></pre>
{click}  <p>Then add the MCP server <code><span class="origin">https://calotype.dev</span>/mcp?ref=try</code> to your agent, with the header <code>Authorization: Bearer cal_...</code>. Set-up for Claude Code, Cursor, VS Code and other clients, and a key button, are on <a href="/agents?ref=try">For agents</a>.</p>
  <p class="muted">The <code>try</code> tag is stored on the new key only to show that it came from a try. It says nothing about you.</p>
"""


def result_content(
    shot: CaptureResult, target: ParsedTarget, form: str, free_quota: int | None, button: str = ""
) -> str:
    where = html.escape(target.display)
    data = base64.b64encode(shot.image).decode("ascii")
    notes = [f"{WIDTH} × {HEIGHT}", "JPEG", "ads blocked", f"taken in {shot.duration_ms / 1000:.1f} s"]
    if shot.target_status is not None:
        notes.append(f"the page answered HTTP {shot.target_status}")
    if shot.truncated:
        notes.append("the page hit the download limit, so parts may be missing")
    return f"""  <h1>Calotype's screenshot</h1>
  <p class="lede">This is <code>{where}</code> as Calotype's browser rendered it just now.</p>
  <figure class="shot">
    <img src="data:{shot.content_type};base64,{data}" width="{WIDTH}" height="{HEIGHT}" alt="Screenshot of {where}">
    <figcaption>{html.escape(", ".join(notes))}. The image is only in this page: Calotype did not keep it.</figcaption>
  </figure>

  <h2 id="next">Give your agent the same view</h2>
{key_steps(free_quota, button)}
  <h2 id="again">Try another page</h2>
{form}"""


def message_content(
    outcome: Outcome, form: str, free_quota: int | None, with_key_steps: bool, button: str = ""
) -> str:
    text = f'  <p class="error" role="alert">{html.escape(outcome.message)}</p>\n' if outcome.message else ""
    steps = f'  <h2 id="next">A free key instead</h2>\n{key_steps(free_quota, button)}' if with_key_steps else ""
    return f"""  <h1>Try Calotype</h1>
  <p class="lede">Paste the address of a public web page to see Calotype's screenshot of it.</p>
{text}{form}{steps}"""


def add_try_routes(app: FastAPI, *, settings: Settings, policy: AddressPolicy, renderer, site_dir: Path) -> None:
    """``GET /try`` shows the form; ``POST /try`` takes one try and shows the image."""
    limits = TryLimits(settings.tries_per_ip_per_hour, settings.tries_per_day)
    free_quota = settings.plans["free"].monthly_quota if "free" in settings.plans else None
    # The "Get a free key" button under the key command, tagged like the command; none with keys off.
    button = key_form(site_dir, free_quota if settings.self_serve_keys else None, TRY_REF)

    def page(content: str, status: int = 200, headers: dict | None = None) -> Response:
        doc = render_document(site_dir, path="/try", title=TITLE, description=DESCRIPTION, content=content)
        return Response(
            doc.body,
            status_code=status,
            media_type="text/html; charset=utf-8",
            headers={
                # The page holds the visitor's image: sent once, kept by no cache.
                "Cache-Control": "no-store",
                "Content-Security-Policy": doc.csp,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex",
                **(headers or {}),
            },
        )

    def form_page(url: str, outcome: Outcome | None = None, with_key_steps: bool = False) -> Response:
        outcome = outcome or Outcome("", 200)
        content = message_content(outcome, try_form(site_dir, limits, url), free_quota, with_key_steps, button)
        return page(content, outcome.status, outcome.headers)

    def count(day: str, outcome: str) -> None:
        try:
            app.state.storage.record_try(day, outcome)
        except Exception:
            log.warning("could not count a try", exc_info=True)

    async def finish(request: Request, day: str, outcome: Outcome, target: ParsedTarget | None, **fields) -> None:
        await asyncio.to_thread(count, day, outcome.name)
        log_event(
            log,
            "try",
            request_id=request.state.request_id,
            outcome=outcome.name,
            target=target.display if target else None,
            **fields,
        )

    @app.get("/try", include_in_schema=False)
    async def try_form_page(request: Request):
        # A link to /try?url=... fills the box in; only the button starts a capture.
        url = request.query_params.get("url", "")[:MAX_URL_LENGTH]
        if not limits.on:
            return form_page("", Outcome("", 200, "Free tries are switched off right now."), with_key_steps=True)
        return form_page(url)

    @app.post("/try", include_in_schema=False)
    async def take_try(request: Request):
        if not limits.on:
            return form_page("", Outcome("", 503, "Free tries are switched off right now."), with_key_steps=True)
        if posted_from_another_site(request):
            return form_page("", Outcome("", 403, "Tries start from the form on Calotype's own site."))
        body = await request.body()
        if len(body) > MAX_FORM_BYTES:
            return form_page("", Outcome("", 413, f"The address is longer than {MAX_URL_LENGTH:,} characters."))
        raw = (parse_qs(body.decode("utf-8", "replace"), max_num_fields=10).get("url") or [""])[0].strip()
        if not raw:
            # Nothing was tried, so nothing is counted.
            return form_page("", Outcome("", 400, "Paste the address of a page first."))

        day = current_day()
        ip = client_ip(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            settings.trusted_proxy_hops,
        )
        ok, retry = app.state.try_limiter.check(ip)
        del ip  # not kept beyond the in-memory limiter
        if not ok:
            minutes = max(1, -(-retry // 60))
            outcome = Outcome(
                "limited", 429,
                f"That is {limits.per_ip_per_hour} free tries in the last hour from your address. "
                f"The next one opens in about {minutes} minute{'s' if minutes != 1 else ''}, "
                "or make a free key below and carry on.",
                {"Retry-After": str(retry)},
            )
            await finish(request, day, outcome, None)
            return form_page(raw, outcome, with_key_steps=True)

        url = normalize_try_url(raw)
        try:
            params = try_params(url)
            target = await policy.check_url(params.url)
        except ValidationError:
            outcome = Outcome("refused", 400, f"The url is longer than {MAX_URL_LENGTH} characters.")
            await finish(request, day, outcome, None, code="invalid_url")
            return form_page(raw, outcome)
        except BlockedTarget as e:
            # The same message a keyed call gets for the same address.
            outcome = Outcome("refused", 400, e.reason)
            await finish(request, day, outcome, None, code=e.code)
            return form_page(raw, outcome)
        request.state.target = target.display

        try:
            allowed, _ = await asyncio.to_thread(app.state.storage.try_admit, day, limits.per_day)
        except Exception:
            log.exception("could not check the daily try cap", extra={"fields": {"request_id": request.state.request_id}})
            return form_page(raw, Outcome("", 503, "Free tries are not available right now. Try again shortly."),
                             with_key_steps=True)
        if not allowed:
            outcome = Outcome(
                "capped", 429,
                f"Today's free tries are used up: {limits.per_day:,} a day, shared by everyone. "
                "They start again at midnight UTC. A free key, below, works now.",
            )
            await finish(request, day, outcome, target)
            return form_page(raw, outcome, with_key_steps=True)

        try:
            shot = await renderer.capture(params)
        except CaptureError as e:
            outcome = Outcome("failed", e.status, e.message, e.headers)
            await finish(request, day, outcome, target, code=e.code)
            return form_page(raw, outcome)
        except Exception:
            log.exception("try failed", extra={"fields": {"request_id": request.state.request_id}})
            outcome = Outcome("failed", 500, "The screenshot failed. Try again, or try another page.")
            await finish(request, day, outcome, target, code="internal_error")
            return form_page(raw, outcome)

        outcome = Outcome("succeeded", 200)
        await finish(
            request, day, outcome, target,
            format=params.format,
            full_page=params.full_page,
            bytes=len(shot.image),
            duration_ms=shot.duration_ms,
            target_status=shot.target_status,
            blocked_requests=shot.blocked_requests,
            truncated=shot.truncated,
        )
        return page(result_content(shot, target, try_form(site_dir, limits), free_quota, button))
