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
        "Add Calotype's remote MCP server to Claude Code, Cursor, VS Code or Codex in one step, "
        "and check it works in a minute.",
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
        "prices per 1,000 as read on 19 November 2026, what other services do that Calotype does not, "
        "and when to use Playwright instead.",
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


def _nav(current: Page) -> str:
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


def render_page(page: Page, site_dir: Path, paid_open: bool = True) -> RenderedPage:
    layout = (site_dir / "layout.html").read_text(encoding="utf-8")
    content = sale_text((site_dir / "pages" / page.source).read_text(encoding="utf-8"), paid_open)
    banner = f'<p class="draft-banner" role="note">{html.escape(DRAFT_BANNER)}</p>' if page.draft else ""
    fields = {
        "title": html.escape(page.title),
        "description": html.escape(page.description),
        "canonical": html.escape(canonical_url(page.path)),
        "nav": _nav(page),
        "draft_banner": banner,
    }
    document = layout
    for name, value in fields.items():
        document = document.replace("{{" + name + "}}", value)
    # Content last, so nothing in it is taken for a placeholder.
    document = document.replace("{{content}}", content)
    if "{{" in document.replace(content, ""):
        raise ValueError(f"unfilled placeholder in the layout for {page.path}")
    return RenderedPage(document.encode("utf-8"), _csp(document))


def sitemap_xml() -> str:
    urls = "\n".join(f"  <url><loc>{html.escape(canonical_url(p.path))}</loc></url>" for p in PAGES)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )


def robots_txt() -> str:
    return f"User-agent: *\nAllow: /\nDisallow: /v1/\n\nSitemap: {SITE_ORIGIN}/sitemap.xml\n"


def add_site_routes(app: FastAPI, site_dir: Path, paid_open: bool = True) -> None:
    """Serve every page in ``PAGES``, plus /robots.txt and /sitemap.xml (GET and HEAD).

    ``paid_open`` is whether this server sells paid plans (Stripe is set up); it picks the
    sale or no-sale text in each page."""
    rendered = {page.path: render_page(page, site_dir, paid_open) for page in PAGES}

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
