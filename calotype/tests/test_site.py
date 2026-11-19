"""The public website: routes, titles, shared footer, draft banners, SEO files, no third parties."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.config import SITE_DIR
from calotype.pages import DRAFT_BANNER, PAGES, SITE_ORIGIN, canonical_url, render_page
from calotype.ratelimit import SlidingWindowLimiter
from calotype.ssrf import AddressPolicy

from .conftest import FakeRenderer, fake_resolver

DISCLOSURE = "Calotype is built and run by Skewbald, an AI agent"
SELLER = "Dostal Technology, a Texas company"
CONTACT = "mailto:skewbald@agentmail.to"
PAGE_IDS = [p.path for p in PAGES]


@pytest.fixture
def site(settings, sqlite_store):
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=sqlite_store, renderer=FakeRenderer())
    with TestClient(app) as c:
        yield c


def footer_of(document: str) -> str:
    found = re.findall(r"<footer\b.*?</footer>", document, re.DOTALL)
    assert len(found) == 1, "each page has exactly one footer"
    return found[0]


def meta(document: str, attr: str, name: str) -> str:
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)">', document)
    assert m, f"no meta {attr}={name}"
    return html.unescape(m.group(1))


# --- routes and titles ---------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_page_returns_200_with_its_title(site, page):
    r = site.get(page.path)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert f"<title>{html.escape(page.title)}</title>" in r.text
    assert r.text.count("<title>") == 1
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_page_answers_head(site, page):
    assert site.head(page.path).status_code == 200


def test_every_page_has_a_distinct_title_and_description():
    assert len({p.title for p in PAGES}) == len(PAGES)
    assert len({p.description for p in PAGES}) == len(PAGES)
    assert len({p.path for p in PAGES}) == len(PAGES)


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_seo_tags(site, page):
    doc = site.get(page.path).text
    assert meta(doc, "name", "description") == page.description
    assert meta(doc, "property", "og:title") == page.title
    assert meta(doc, "property", "og:description") == page.description
    assert meta(doc, "property", "og:url") == canonical_url(page.path)
    assert f'<link rel="canonical" href="{canonical_url(page.path)}">' in doc
    assert canonical_url(page.path).startswith("https://calotype.dev")


def test_canonical_urls_are_clean():
    assert canonical_url("/") == "https://calotype.dev"
    assert canonical_url("/docs") == "https://calotype.dev/docs"
    for page in PAGES:
        assert page.path == "/" or (not page.path.endswith("/") and "." not in page.path)


def test_site_can_be_switched_off(settings, sqlite_store):
    app = create_app(
        replace(settings, serve_site=False),
        policy=AddressPolicy(resolver=fake_resolver),
        storage=sqlite_store,
        renderer=FakeRenderer(),
    )
    with TestClient(app) as c:
        for page in PAGES:
            assert c.get(page.path).status_code == 404
        assert c.get("/health").status_code == 200


def test_docs_is_the_site_page_not_swagger(site):
    doc = site.get("/docs").text
    assert "swagger" not in doc.lower()
    assert "/v1/screenshot" in doc and "quota_exceeded" in doc and "mcpServers" in doc
    assert site.get("/openapi.json").status_code == 200
    # Site routes stay out of the API schema.
    assert set(site.get("/openapi.json").json()["paths"]) == {
        "/health", "/healthz", "/v1/screenshot", "/v1/keys", "/v1/usage", "/v1/plans",
        "/v1/billing/checkout", "/v1/billing/portal", "/v1/billing/webhook",
    }


def page_text(path: str, paid_open: bool) -> str:
    page = next(p for p in PAGES if p.path == path)
    return render_page(page, SITE_DIR, paid_open).body.decode("utf-8")


def test_paid_plans_are_on_sale_and_tax_is_disclosed(site):
    doc = site.get("/docs").text
    billing = doc[doc.index('id="billing"'):]
    assert "Coming soon" not in billing[:200]
    pricing = page_text("/pricing", paid_open=True)
    assert "Paid plans are on sale." in pricing and "do not include sales tax" in pricing
    assert 'id="upgrade-form"' in pricing and "Paid plans open soon" not in pricing
    assert "Paid plans are on sale" in page_text("/", paid_open=True)
    assert "do not include sales tax" in site.get("/terms").text
    for path in ("/", "/pricing", "/privacy", "/terms", "/refunds", "/docs"):
        assert "not on sale" not in page_text(path, paid_open=True).replace("not on sale.", ""), path


def test_plans_are_provisional_only_without_billing(site):
    assert site.get("/v1/plans").json()["provisional"] is True  # this test app has no Stripe


# --- shared layout -------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_footer_disclosure_on_every_page(site, page):
    footer = footer_of(site.get(page.path).text)
    assert DISCLOSURE in footer
    assert SELLER in footer
    assert CONTACT in footer
    for path in ("/terms", "/privacy", "/acceptable-use", "/refunds"):
        assert f'href="{path}"' in footer


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_header_navigation_on_every_page(site, page):
    doc = site.get(page.path).text
    header = re.search(r"<header\b.*?</header>", doc, re.DOTALL).group(0)
    assert 'href="/docs"' in header and 'href="/pricing"' in header and 'href="/"' in header
    if page.nav_label:
        assert f'<a href="{page.path}" aria-current="page">' in header


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_draft_banner_only_on_drafts(site, page):
    doc = site.get(page.path).text
    if page.draft:
        assert DRAFT_BANNER == "Draft, not yet in force. Paid plans are not on sale until these terms are approved."
        assert DRAFT_BANNER in doc
        # Near the top: before the page's own heading.
        assert doc.index(DRAFT_BANNER) < doc.index("<h1>")
    else:
        assert DRAFT_BANNER not in doc


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_light_and_dark_and_phone_width(site, page):
    doc = site.get(page.path).text
    assert "@media (prefers-color-scheme: dark)" in doc
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in doc


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_no_personal_data_and_no_human_team(site, page):
    doc = site.get(page.path).text
    # The only email address on the site is the support inbox.
    assert set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", doc)) <= {"skewbald@agentmail.to"}
    assert "our team" not in doc.lower() and "our staff" not in doc.lower()
    assert "reviewed by a lawyer" not in doc.lower()


# --- nothing from other hosts ---------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_no_external_scripts_fonts_or_styles(site, page):
    r = site.get(page.path)
    doc = r.text
    assert re.search(r"<script\b[^>]*\bsrc=", doc) is None, "scripts are inline only"
    assert "@import" not in doc and "@font-face" not in doc
    assert re.search(r"url\(\s*['\"]?(https?:)?//", doc) is None
    for tag in re.findall(r"<(?:link|img|iframe|source|video|audio|embed|object)\b[^>]*>", doc):
        for value in re.findall(r'\b(?:href|src|srcset|data)="([^"]*)"', tag):
            external = value.startswith(("http:", "https:", "//"))
            if external:
                assert 'rel="canonical"' in tag and value.startswith(SITE_ORIGIN), tag
    for host in ("googleapis", "gstatic", "jsdelivr", "unpkg", "cdnjs", "typekit", "fontawesome",
                 "googletagmanager", "google-analytics", "plausible", "segment", "posthog", "hotjar"):
        assert host not in doc.lower(), host


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_csp_allows_only_the_pages_own_inline_code(site, page):
    r = site.get(page.path)
    csp = r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "http:" not in csp and "https:" not in csp and "*" not in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    scripts = re.findall(r"<script>(.*?)</script>", r.text, re.DOTALL)
    assert scripts, "the layout's own script is always there"
    for body in scripts + re.findall(r"<style>(.*?)</style>", r.text, re.DOTALL):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_internal_links_resolve(site, page):
    doc = site.get(page.path).text
    for href in set(re.findall(r'href="(/[^"]*)"', doc)):
        path, _, anchor = href.partition("#")
        r = site.get(path.split("?", 1)[0] or "/")
        assert r.status_code == 200, href
        if anchor:
            assert f'id="{anchor}"' in r.text, href
    for anchor in set(re.findall(r'href="#([^"]+)"', doc)):
        assert f'id="{anchor}"' in doc, anchor


# --- robots and sitemap ---------------------------------------------------------------------


def test_sitemap_lists_every_page(site):
    r = site.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.fromstring(r.content).findall("s:url/s:loc", ns)]
    assert sorted(locs) == sorted(canonical_url(p.path) for p in PAGES)
    assert len(locs) == len(set(locs))


def test_robots_points_at_the_sitemap(site):
    r = site.get("/robots.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in r.text
    assert "Disallow: /v1/" in r.text
    assert f"Sitemap: {SITE_ORIGIN}/sitemap.xml" in r.text


def test_logo_files_are_served_for_directories(site):
    """The MCP Registry entry and directories link to /icon.svg and /icon.png."""
    svg = site.get("/icon.svg")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert b"<script" not in svg.content and b"<title>Calotype</title>" in svg.content
    assert "sandbox" in svg.headers["content-security-policy"]
    png = site.get("/icon.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(png.content[16:20], "big") == 512  # width, as server.json's sizes says


def test_registry_icons_point_at_served_files(site):
    import json
    from pathlib import Path

    entry = json.loads((Path(__file__).parents[1] / "deploy" / "mcp-registry" / "server.json").read_text())
    for icon in entry["icons"]:
        assert icon["src"].startswith(SITE_ORIGIN + "/")
        assert site.get(icon["src"].removeprefix(SITE_ORIGIN)).status_code == 200


# --- facts the pages state, checked against the code -----------------------------------------


def test_pricing_matches_plans_json(site, settings):
    doc = site.get("/pricing").text
    for plan in settings.plans.values():
        assert f"<h3>{plan.title}</h3>" in doc
        assert f"${plan.price_usd_cents // 100} <small>" in doc
        assert f"{plan.monthly_quota:,} screenshots a month" in doc


def test_docs_parameter_limits_match_the_code(site):
    from calotype import params

    doc = site.get("/docs").text
    assert f"100 to {params.MAX_WIDTH}" in doc
    assert f"100 to {params.MAX_HEIGHT}" in doc
    assert f"{params.MAX_DELAY_MS:,} ms" in doc
    assert f"{params.MAX_FULL_PAGE_HEIGHT:,} CSS pixels" in doc
    assert f"{params.MAX_OUTPUT_PIXELS:,} pixels" in doc
    assert f"{params.WEBP_MAX_SIDE:,}" in doc
    for name in params.ScreenshotParams.model_fields:
        assert f"<td><code>{name}</code></td>" in doc, name


def test_docs_list_every_error_code_the_code_raises(site):
    from pathlib import Path

    import calotype

    doc = site.get("/docs").text
    source = "".join(
        (Path(calotype.__file__).parent / f).read_text() for f in ("app.py", "capture.py", "ssrf.py")
    )
    codes = set(re.findall(r'ApiError\(\s*\d{3},\s*"([a-z_]+)"', source))
    codes |= set(re.findall(r'CaptureError\(\s*\d{3},\s*"([a-z_]+)"', source))
    codes |= set(re.findall(r'BlockedTarget\([^)]*?,\s*"([a-z_]+)"\)', source)) | {"blocked_target"}
    codes -= {"invalid_webhook", "webhook_failed"}  # Stripe's own endpoint
    assert codes, "the scan found the codes"
    for code in sorted(codes):
        assert f"<code>{code}</code>" in doc, code


def test_key_creation_limits_in_docs_match_defaults(site, settings):
    doc = site.get("/docs").text
    assert f"At most {settings.keys_per_ip_per_hour} keys an hour from one IP address" in doc
    assert f"at most {settings.keys_global_per_hour} an hour across the whole service" in doc
    assert f"{settings.capture_timeout_s} seconds per capture" in doc


def test_stripe_returns_buyers_to_the_page_with_the_form():
    doc = page_text("/pricing", paid_open=True)
    assert 'id="upgrade-form"' in doc and 'id="upgrade"' in doc
    # The page reads ?checkout=success or ?checkout=cancelled, as app.py sends them.
    assert 'params.get("checkout") === "success"' in doc and '=== "cancelled"' in doc


# --- the IP address limit forgets addresses within the hour --------------------------------


def test_limiter_sweep_forgets_addresses_within_the_window():
    now = [0.0]
    lim = SlidingWindowLimiter(3, window_s=3600, clock=lambda: now[0])
    lim.check("198.51.100.7")
    now[0] = 1800.0
    lim.check("203.0.113.9")
    now[0] = 3600.0 - 60.0
    assert lim.sweep(margin_s=60) == 1
    assert lim.tracked() == 1
    # Swept every 60 s, no address outlives one window after its last attempt.
    now[0] = 1800.0 + 3600.0 - 60.0
    assert lim.sweep(margin_s=60) == 1
    assert lim.tracked() == 0


def test_limiter_sweep_keeps_recent_addresses():
    now = [0.0]
    lim = SlidingWindowLimiter(1, window_s=3600, clock=lambda: now[0])
    assert lim.check("a")[0]
    now[0] = 100.0
    assert lim.sweep() == 0
    assert lim.check("a")[0] is False


def test_app_sweeps_its_limiters(settings, sqlite_store, monkeypatch):
    import time

    import calotype.app as app_module

    monkeypatch.setattr(app_module, "SWEEP_INTERVAL_S", 0.01)
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=sqlite_store, renderer=FakeRenderer())
    calls = []
    monkeypatch.setattr(app.state.key_limiter, "sweep", lambda *a, **k: calls.append("ip") or 0)
    with TestClient(app):
        for _ in range(200):
            if calls:
                break
            time.sleep(0.01)
    assert calls



# --- the agent positioning ------------------------------------------------------------------


def test_home_leads_with_the_agent_job(site):
    doc = site.get("/").text
    assert doc.index("<h1>Let your AI agent see any public web page.</h1>") < doc.index('id="api"')
    assert "claude mcp add --transport http calotype" in doc
    assert "Public pages only." in doc and "Playwright MCP" in doc
    assert "protects your network, not your model" in doc
    assert 'href="/agents"' in doc


def test_agents_page_has_a_block_per_client_and_a_key_button(site):
    doc = site.get("/agents").text
    for anchor in ("claude-code", "cursor", "vscode", "codex", "n8n", "other", "check"):
        assert f'id="{anchor}"' in doc, anchor
    assert '"servers"' in doc and '"mcpServers"' in doc and "bearer_token_env_var" in doc
    assert "HTTP Streamable" in doc and "1.104.0" in doc  # n8n needs the streamable transport
    assert 'id="create-key"' in doc and "POST" in doc and "/v1/keys" in doc
    # Examples read the key from the environment or a prompt; no real key is ever on the page.
    assert re.search(r"cal_[A-Za-z0-9]{8,}", doc) is None


@pytest.mark.parametrize("path", ["/", "/agents"])
def test_key_button_script_is_on_pages_with_the_button(site, path):
    doc = site.get(path).text
    assert 'id="create-key"' in doc and 'fetch("/v1/keys"' in doc


def test_support_page_names_the_inbox_and_never_asks_for_the_key(site):
    doc = site.get("/support").text
    assert "skewbald@agentmail.to" in doc and "AI agent" in doc and "Dostal Technology" in doc
    assert "Never send your API key" in doc
    assert 'href="/support"' in site.get("/").text  # linked from every page's footer


def test_without_card_checkout_the_site_says_paid_plans_open_soon(site):
    # This test app has no Stripe, so it serves the no-sale text.
    for path in ("/", "/pricing"):
        doc = site.get(path).text
        assert "Paid plans open soon" in doc, path
        assert "Paid plans are on sale" not in doc, path
        assert "<!--sale-->" not in doc and "<!--nosale-->" not in doc, path
    pricing = site.get("/pricing").text
    assert 'id="upgrade-form"' not in pricing and "Continue to checkout" not in pricing
    assert "100 screenshots a month" in pricing


def test_sale_markers_never_leak_into_a_rendered_page():
    for page in PAGES:
        for paid_open in (True, False):
            text = page_text(page.path, paid_open)
            assert "<!--sale-->" not in text and "<!--/nosale-->" not in text, (page.path, paid_open)


# --- switching from other screenshot APIs ----------------------------------------------------

# Each competitor fact on /switch was read from these pages on the date the page states.
SWITCH_SOURCES = (
    "https://screenshotone.com/pricing/",
    "https://urlbox.com/pricing",
    "https://apiflash.com/",
    "https://screenshotapi.net/pricing",
    "https://allscreenshots.com/pricing",
    "https://screenshotone.com/docs/options/",
    "https://urlbox.com/docs/options",
    "https://apiflash.com/documentation",
    "https://docs.allscreenshots.com/api-reference/screenshots",
    "https://github.com/microsoft/playwright-mcp",
)
SWITCH_READ_ON = "19 November 2026"


def test_switch_page_maps_every_parameter_the_code_takes(site):
    from calotype import params

    doc = site.get("/switch").text
    table = doc[doc.index('id="params"'):doc.index('id="example"')]
    mapped = re.findall(r"<tr><td><code>([a-z_]+)</code></td>", table)
    assert sorted(mapped) == sorted(params.ScreenshotParams.model_fields)
    # The converted example uses only parameters Calotype accepts.
    example = doc[doc.index('id="example"'):doc.index('id="differences"')]
    query = re.search(r"/v1/screenshot\?([^\"]+)\"", example).group(1)
    names = {pair.split("=", 1)[0] for pair in html.unescape(query).split("&")}
    assert names <= set(params.ScreenshotParams.model_fields), names
    assert f"at most {params.MAX_DELAY_MS:,}" in doc
    assert f"widths up to {params.MAX_WIDTH:,}" in doc


def test_switch_page_prices_match_plans_json_and_are_dated(site, settings):
    doc = site.get("/switch").text
    starter, pro = settings.plans["starter"], settings.plans["pro"]
    assert f"${starter.price_usd_cents // 100} a month, {starter.monthly_quota:,}" in doc
    assert f"${starter.price_usd_cents / starter.monthly_quota * 10:.2f}" in doc  # per 1,000
    assert f"${pro.price_usd_cents // 100} for {pro.monthly_quota:,}" in doc
    assert f"${pro.price_usd_cents / pro.monthly_quota * 10:.2f} per 1,000" in doc
    assert f"{settings.plans['free'].monthly_quota} a month, no account" in doc
    # Every competitor figure is dated and linked to the seller's own page.
    assert doc.count(SWITCH_READ_ON) >= 3
    for url in SWITCH_SOURCES:
        assert f'href="{url}"' in doc, url


def test_switch_page_says_where_others_do_more_and_stays_plain(site):
    doc = site.get("/switch").text
    assert 'id="more"' in doc and "Calotype is not the cheapest" in doc
    assert "OAuth" in doc and "PDF" in doc
    text = re.sub(r"<[^>]+>", " ", doc[doc.index("<main"):doc.index("</main>")]).lower()
    for word in ("best", "fastest", "leading", "world-class", "unbeatable", "blazing", "worse", "terrible"):
        assert re.search(rf"\b{word}\b", text) is None, word


def test_switch_page_offers_a_month_for_a_written_review(site):
    # The key is named by its key_ref from /v1/usage (running plan G1b), never sent.
    doc = site.get("/switch").text
    offer = doc[doc.index('id="feedback"'):]
    assert "one month on Starter" in offer and "key_ref" in offer
    assert 'href="mailto:skewbald@agentmail.to"' in offer
    assert "Never send your key" in offer


def test_agents_page_says_when_to_use_playwright_instead(site):
    doc = site.get("/agents").text
    block = doc[doc.index('id="playwright"'):]
    assert "Node.js 20" in block and "npx @playwright/mcp@latest" in block
    assert "localhost" in block and "no browser to install" in block
    assert 'href="/switch"' in block
    assert 'href="#playwright"' in doc  # listed in the page's contents
