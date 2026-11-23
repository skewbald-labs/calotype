"""The public website: a few plain HTML pages, each at its own clean URL.

Each page's content is a fragment in ``site/pages/``, wrapped in the shared
``site/layout.html`` (header, navigation, footer). Pages are rendered once,
when the app is created, and served from memory.

The site loads nothing from other hosts: no scripts, fonts, images,
analytics or cookies. Images come from the site itself (``site/images/``) or ``data:`` URLs. Every inline ``<script>`` and ``<style>`` is hashed into
the page's Content-Security-Policy, so a browser runs those and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

SITE_ORIGIN = "https://calotype.dev"
CONTACT = "skewbald@agentmail.to"
DRAFT_BANNER = "Draft, not yet in force. Paid plans are not on sale until these terms are approved."


@dataclass(frozen=True)
class Page:
    path: str
    source: str  # file name in site/pages/
    title: str
    description: str
    nav_label: str = ""  # shown in the header navigation when set
    draft: bool = False


PAGES: tuple[Page, ...] = (
    Page(
        "/",
        "home.html",
        "Calotype: let your AI agent see any web page",
        "A screenshot tool for AI agents with no browser: one MCP tool call or HTTP request returns an image "
        "of any public page. Free key, no sign-up. Built and run by Skewbald, an AI agent.",
    ),
    Page(
        "/agents",
        "agents.html",
        "For AI agents: add the screenshot tool | Calotype",
        "Add Calotype's remote MCP server to Cursor or VS Code in one click, or to Claude Code, Windsurf, "
        "Codex or n8n in one step, and check it works in a minute.",
        nav_label="For agents",
    ),
    Page(
        "/docs",
        "docs.html",
        "API reference | Calotype",
        "The Calotype API: endpoints, parameters, response headers, errors, quotas, rate limits, "
        "the MCP server and billing.",
        nav_label="Docs",
    ),
    Page(
        "/pricing",
        "pricing.html",
        "Pricing | Calotype",
        "Calotype plans: Free, 100 screenshots a month; Starter, $19 for 5,000; Pro, $49 for 25,000. "
        "Only successful captures count.",
        nav_label="Pricing",
    ),
    Page(
        "/switch",
        "switch.html",
        "Switching to Calotype from another screenshot API | Calotype",
        "Parameter names mapped from ScreenshotOne, Urlbox, ApiFlash, ScreenshotAPI.net and Allscreenshots, "
        "prices per 1,000 as read on 21 November 2026, what other services do that Calotype does not, "
        "and when to use Playwright instead.",
    ),
    Page(
        "/switch/screenshotone",
        "switch/screenshotone.html",
        "Switching from ScreenshotOne to Calotype | Calotype",
        "ScreenshotOne and Calotype side by side: prices per 1,000 as read on 21 November 2026, parameter names "
        "mapped, one request before and after, and when to stay with ScreenshotOne.",
    ),
    Page(
        "/switch/urlbox",
        "switch/urlbox.html",
        "Switching from Urlbox to Calotype | Calotype",
        "Urlbox and Calotype side by side: Lo-Fi and Hi-Fi prices as read on 21 November 2026, render options "
        "mapped to Calotype's parameters, one request before and after, and when to stay with Urlbox.",
    ),
    Page(
        "/switch/screenshotapi-net",
        "switch/screenshotapi-net.html",
        "Switching from ScreenshotAPI.net to Calotype | Calotype",
        "ScreenshotAPI.net and Calotype side by side: prices at each plan as read on 21 November 2026, where "
        "ScreenshotAPI.net costs less, parameter names mapped, and one request before and after.",
    ),
    Page(
        "/switch/allscreenshots",
        "switch/allscreenshots.html",
        "Switching from Allscreenshots to Calotype | Calotype",
        "Allscreenshots and Calotype side by side: prices as read on 21 November 2026, camel-case options mapped "
        "to Calotype's parameters, defaults that differ, and when to stay with Allscreenshots.",
    ),
    Page(
        "/switch/apiflash",
        "switch/apiflash.html",
        "Switching from ApiFlash to Calotype | Calotype",
        "ApiFlash and Calotype side by side: prices as read on 21 November 2026, parameter names mapped, the "
        "network-wait default that differs, and when ApiFlash is the smaller bill.",
    ),
    Page(
        "/switch/playwright-mcp",
        "switch/playwright-mcp.html",
        "Playwright MCP or Calotype | Calotype",
        "Playwright MCP is free and runs a browser next to your agent; Calotype is a remote screenshot tool. "
        "Where each runs, what each installs, private pages, interaction, and when to keep Playwright MCP.",
    ),
    Page(
        "/compare/screenshot-mcp-servers",
        "compare/screenshot-mcp-servers.html",
        "Screenshot MCP servers compared | Calotype",
        "Playwright MCP, Chrome DevTools MCP, ScreenshotOne, Urlbox, Allscreenshots, ScreenshotAPI.net and "
        "Calotype: local or remote, sign-in, what the model gets and price. By the agent that runs Calotype.",
    ),
    Page(
        "/guides",
        "guides.html",
        "Guides: screenshots from code, agents and workflows | Calotype",
        "Four how-to guides: screenshots in Claude Code and Cursor, Playwright in Python and where it breaks, "
        "a screenshot step in n8n, and why a headless browser inside your network is an SSRF risk.",
    ),
    Page(
        "/guides/claude-code-cursor-screenshots",
        "guides/claude-code-cursor-screenshots.html",
        "Screenshots in Claude Code or Cursor without a local browser | Calotype",
        "Two ways to let a coding agent see a rendered page: Playwright MCP next to the agent, or a remote MCP "
        "server. When each is the better choice, with set-up for Claude Code and Cursor.",
    ),
    Page(
        "/guides/playwright-python-screenshots",
        "guides/playwright-python-screenshots.html",
        "Playwright screenshots in Python, and where they break | Calotype",
        "Fonts, lazy loading, cookie banners, full-page captures, containers and timeouts: what goes wrong "
        "with Playwright screenshots in Python, and tested code that fixes each one.",
    ),
    Page(
        "/guides/n8n-screenshot-workflow",
        "guides/n8n-screenshot-workflow.html",
        "Add a screenshot step to an n8n workflow | Calotype",
        "Take a web page screenshot in n8n with the HTTP Request node or the MCP Client Tool: settings, "
        "error handling, and how much quota a schedule uses.",
    ),
    Page(
        "/guides/headless-browser-ssrf",
        "guides/headless-browser-ssrf.html",
        "A headless browser inside your network is an SSRF risk | Calotype",
        "What a screenshot browser can reach from inside your network, why checking the address is not enough, "
        "how to defend it yourself, and what Calotype's code does.",
    ),
    Page(
        "/acceptable-use",
        "acceptable-use.html",
        "Acceptable use policy | Calotype",
        "What Calotype may not be used for, what happens to keys that break the rules, and how to report abuse.",
    ),
    Page(
        "/privacy",
        "privacy.html",
        "Privacy notice | Calotype",
        "What Calotype stores, what it logs, what it never keeps, and who to contact. No cookies, no trackers.",
    ),
    Page(
        "/terms",
        "terms.html",
        "Terms of service | Calotype",
        "Terms of service for Calotype, the website screenshot API sold by Dostal Technology.",
    ),
    Page(
        "/support",
        "support.html",
        "Support | Calotype",
        "How to reach Calotype's support: one inbox, answered by Skewbald, the AI agent that runs it. Billing, refunds, lost keys and abuse reports.",
    ),
    Page(
        "/refunds",
        "refunds.html",
        "Refund policy | Calotype",
        "Calotype's refund policy for paid plans: cancel any time, full refund within 7 days of a first payment.",
    ),
)

_INLINE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)


@dataclass(frozen=True)
class RenderedPage:
    body: bytes
    csp: str


def canonical_url(path: str) -> str:
    return SITE_ORIGIN + ("" if path == "/" else path)


def _nav(current: Page | None) -> str:
    links = []
    for page in PAGES:
        if page.nav_label:
            here = ' aria-current="page"' if page is current else ""
            links.append(f'<a href="{page.path}"{here}>{html.escape(page.nav_label)}</a>')
    links.append('<a href="/#key">Get a key</a>')
    return "\n    ".join(links)


def _csp(document: str) -> str:
    """A policy that allows exactly the page's own inline scripts and styles."""
    hashes: dict[str, list[str]] = {"script": [], "style": []}
    for kind, body in _INLINE.findall(document):
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        hashes[kind].append(f"'sha256-{digest}'")
    script_src = " ".join(hashes["script"]) or "'none'"
    style_src = " ".join(hashes["style"]) or "'none'"
    return (
        f"default-src 'none'; script-src {script_src}; style-src {style_src}; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )


# Text that only holds while paid plans are on sale sits between <!--sale--> and <!--/sale-->;
# its replacement for a server without card checkout, between <!--nosale--> and <!--/nosale-->.
_SALE = re.compile(r"<!--sale-->(.*?)<!--/sale-->", re.DOTALL)
_NOSALE = re.compile(r"<!--nosale-->(.*?)<!--/nosale-->", re.DOTALL)


def sale_text(content: str, paid_open: bool) -> str:
    keep, drop = (_SALE, _NOSALE) if paid_open else (_NOSALE, _SALE)
    return keep.sub(lambda m: m.group(1), drop.sub("", content))


# The same for OAuth sign-in on /mcp (oauth.py), which is on only where CALOTYPE_OAUTH_SECRET is set:
# <!--oauth-->...<!--/oauth--> with it, <!--nooauth-->...<!--/nooauth--> without.
_OAUTH = re.compile(r"<!--oauth-->(.*?)<!--/oauth-->", re.DOTALL)
_NOOAUTH = re.compile(r"<!--nooauth-->(.*?)<!--/nooauth-->", re.DOTALL)


def oauth_text(content: str, oauth_on: bool) -> str:
    keep, drop = (_OAUTH, _NOOAUTH) if oauth_on else (_NOOAUTH, _OAUTH)
    return keep.sub(lambda m: m.group(1), drop.sub("", content))


# The home page's try box (site/try-form.html) goes where a page has this marker.
TRY_FORM_MARK = "<!--try-form-->"


@dataclass(frozen=True)
class TryLimits:
    """The free-try limits the box states (from Settings); ``per_day`` 0 means tries are off."""

    per_ip_per_hour: int = 5
    per_day: int = 300


def try_form(site_dir: Path, limits: TryLimits, url: str = "") -> str:
    """The try box: a plain form that posts to /try, with the limits it states filled in."""
    if limits.per_day <= 0:
        return ""
    form = (site_dir / "try-form.html").read_text(encoding="utf-8")
    form = form.replace("TRIES_PER_IP", f"{limits.per_ip_per_hour:,}").replace("TRIES_PER_DAY", f"{limits.per_day:,}")
    return form.replace('value=""', f'value="{html.escape(url, quote=True)}"', 1)


# The "Get a free key" button (site/key-form.html) goes where a page has this marker.
KEY_FORM_MARK = "<!--key-form-->"

NOINDEX_META = '<meta name="robots" content="noindex">'


def key_form(site_dir: Path, free_quota: int | None, ref: str = "") -> str:
    """The key button: a plain form that posts to /keys (keypages.py), with the free plan's quota filled in.

    ``free_quota`` is None where a visitor cannot make a key (self-serve keys are off); then
    there is no button. ``ref`` is the source tag the form sends; on a page served from memory
    it is empty, and the layout's script fills it from the page's own ``?ref=``.
    """
    if not free_quota:
        return ""
    form = (site_dir / "key-form.html").read_text(encoding="utf-8").replace("FREE_QUOTA", f"{free_quota:,}")
    return form.replace('value=""', f'value="{html.escape(ref, quote=True)}"', 1)


def wants_html(request: Request) -> bool:
    """Whether a request comes from a browser showing a page, not from a program.

    A browser's navigation names ``text/html`` in ``Accept``. API clients send ``*/*`` or
    ``application/json``, and MCP clients ``text/event-stream``; a client that names an event
    stream is never taken for a browser, whatever else it lists.
    """
    accept = ",".join(request.headers.getlist("accept")).lower()
    return "text/html" in accept and "text/event-stream" not in accept


def posted_from_another_site(request: Request) -> bool:
    """Whether a form was posted from another site's page (``Sec-Fetch-Site: cross-site``).

    The site's own forms (the try box, the key button) refuse those, so no other page can
    spend its visitors' tries or key slots. A client that sends no such header is let through.
    """
    return request.headers.get("sec-fetch-site", "").lower() == "cross-site"


def render_document(
    site_dir: Path, *, path: str, title: str, description: str, content: str,
    current: Page | None = None, draft: bool = False, noindex: bool = False,
) -> RenderedPage:
    """Wrap ``content`` in the shared layout and compute its Content-Security-Policy.

    ``noindex`` adds a robots meta tag, for pages that are answers to one request."""
    layout = (site_dir / "layout.html").read_text(encoding="utf-8")
    banner = f'<p class="draft-banner" role="note">{html.escape(DRAFT_BANNER)}</p>' if draft else ""
    fields = {
        "title": html.escape(title),
        "description": html.escape(description),
        "canonical": html.escape(canonical_url(path)),
        "nav": _nav(current),
        "draft_banner": banner,
        "robots": NOINDEX_META if noindex else "",
    }
    document = layout
    for name, value in fields.items():
        document = document.replace("{{" + name + "}}", value)
    # Content last, so nothing in it is taken for a placeholder.
    document = document.replace("{{content}}", content)
    if "{{" in document.replace(content, ""):
        raise ValueError(f"unfilled placeholder in the layout for {path}")
    return RenderedPage(document.encode("utf-8"), _csp(document))


def render_page(
    page: Page, site_dir: Path, paid_open: bool = True, tries: TryLimits = TryLimits(), oauth_on: bool = False,
    free_quota: int | None = None,
) -> RenderedPage:
    content = sale_text((site_dir / "pages" / page.source).read_text(encoding="utf-8"), paid_open)
    content = oauth_text(content, oauth_on)
    content = content.replace(TRY_FORM_MARK, try_form(site_dir, tries))
    content = content.replace(KEY_FORM_MARK, key_form(site_dir, free_quota))
    return render_document(
        site_dir, path=page.path, title=page.title, description=page.description, content=content,
        current=page, draft=page.draft,
    )


def sitemap_xml() -> str:
    urls = "\n".join(f"  <url><loc>{html.escape(canonical_url(p.path))}</loc></url>" for p in PAGES)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def robots_txt() -> str:
    # /try renders a page on each visit; crawlers have no reason to.
    return f"User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /try\nDisallow: /oauth/\n\nSitemap: {SITE_ORIGIN}/sitemap.xml\n"


def llms_txt(site_dir: Path, paid_open: bool = True, oauth_on: bool = False) -> str:
    """``/llms.txt`` (https://llmstxt.org): what Calotype is, for a model, in plain Markdown.

    Not in the sitemap, which lists the HTML pages; like /robots.txt it sits at a fixed path."""
    return oauth_text(sale_text((site_dir / "llms.txt").read_text(encoding="utf-8"), paid_open), oauth_on)


def add_site_routes(
    app: FastAPI, site_dir: Path, paid_open: bool = True, tries: TryLimits = TryLimits(), oauth_on: bool = False,
    free_quota: int | None = None,
) -> None:
    """Serve every page in ``PAGES``, plus /robots.txt, /sitemap.xml and /llms.txt (GET and HEAD).

    ``paid_open`` is whether this server sells paid plans (Stripe is set up); it picks the
    sale or no-sale text in each page. ``tries`` are the limits the home page's try box states.
    ``oauth_on`` is whether OAuth sign-in on /mcp is on; it picks the oauth or no-oauth text.
    ``free_quota`` is the free plan's monthly quota where a visitor can make a key, for the
    "Get a free key" button (``key_form``); None leaves the button out."""
    rendered = {page.path: render_page(page, site_dir, paid_open, tries, oauth_on, free_quota) for page in PAGES}

    def page_endpoint(path: str):
        page = rendered[path]

        async def endpoint(_request: Request) -> Response:
            return Response(
                page.body,
                media_type="text/html; charset=utf-8",
                headers={
                    "Cache-Control": "public, max-age=300",
                    "Content-Security-Policy": page.csp,
                    "X-Content-Type-Options": "nosniff",
                },
            )

        return endpoint

    def text_endpoint(body: str, media_type: str):
        data = body.encode("utf-8")

        async def endpoint(_request: Request) -> Response:
            return Response(data, media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})

        return endpoint

    for path in rendered:
        app.add_route(path, page_endpoint(path), include_in_schema=False)
    app.add_route("/robots.txt", text_endpoint(robots_txt(), "text/plain; charset=utf-8"), include_in_schema=False)
    app.add_route("/sitemap.xml", text_endpoint(sitemap_xml(), "application/xml"), include_in_schema=False)
    if (site_dir / "llms.txt").is_file():
        app.add_route("/llms.txt", text_endpoint(llms_txt(site_dir, paid_open, oauth_on), "text/plain; charset=utf-8"),
                      include_in_schema=False)
    for path, (data, media_type) in {**brand_files(site_dir), **image_files(site_dir)}.items():
        app.add_route(path, file_endpoint(data, media_type), include_in_schema=False)


BRAND_TYPES = {".svg": "image/svg+xml", ".png": "image/png"}
IMAGE_TYPES = {".webp": "image/webp", ".png": "image/png"}


def brand_files(site_dir: Path) -> dict[str, tuple[bytes, str]]:
    """The logo files in ``site/brand/``, keyed by the URL each is served at (``/icon.svg``).

    Directories and the MCP Registry link to these; the pages themselves use an inline copy,
    because their Content-Security-Policy allows only ``data:`` images."""
    brand_dir = site_dir / "brand"
    if not brand_dir.is_dir():
        return {}
    return {
        f"/{f.name}": (f.read_bytes(), BRAND_TYPES[f.suffix])
        for f in sorted(brand_dir.iterdir())
        if f.suffix in BRAND_TYPES
    }


def image_files(site_dir: Path) -> dict[str, tuple[bytes, str]]:
    """Images the pages and the README show, such as the demo screenshot, at ``/images/<name>``.

    A file's name is its URL, so replace an image under a new name rather than editing it:
    the README on GitHub links to these paths."""
    images_dir = site_dir / "images"
    if not images_dir.is_dir():
        return {}
    return {
        f"/images/{f.name}": (f.read_bytes(), IMAGE_TYPES[f.suffix])
        for f in sorted(images_dir.iterdir())
        if f.suffix in IMAGE_TYPES
    }


def file_endpoint(data: bytes, media_type: str):
    async def endpoint(_request: Request) -> Response:
        return Response(
            data,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return endpoint
