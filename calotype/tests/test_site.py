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
        "/health", "/healthz", "/v1/screenshot", "/v1/keys", "/v1/keys/email", "/v1/usage", "/v1/plans",
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
    # The only email address on the site is the support inbox; examples use the reserved example.com.
    found = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", doc))
    assert {a for a in found if not a.endswith("@example.com")} <= {"skewbald@agentmail.to"}
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
    for anchor in ("claude-code", "cursor", "vscode", "windsurf", "claude-desktop", "codex", "n8n", "other", "check"):
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
SWITCH_READ_ON = "21 November 2026"


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


# --- first screen: install buttons and the demo image (running plan H2) ----------------------

DEMO_IMAGE = "/images/demo-pricing-dark.webp"
DEMO_REQUEST = "-d url=https://calotype.dev/pricing -d dark_mode=true -d format=webp"
REPO_README = SITE_DIR.parents[1] / "README.md"


def test_demo_image_is_served_small_and_as_webp(site):
    r = site.get(DEMO_IMAGE)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert r.content[:4] == b"RIFF" and r.content[8:12] == b"WEBP"
    assert len(r.content) < 150 * 1024
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert site.get("/images/nothing-here.webp").status_code == 404


@pytest.mark.parametrize("page", PAGES, ids=PAGE_IDS)
def test_page_images_come_from_the_site_and_are_served(site, page):
    r = site.get(page.path)
    assert "img-src 'self' data:;" in r.headers["content-security-policy"]
    for tag in re.findall(r"<img\b[^>]*>", r.text):
        src = re.search(r'src="([^"]+)"', tag).group(1)
        assert src.startswith(("/", "data:")), tag
        assert re.search(r'alt="[^"]{20,}"', tag), "every image says what it shows"
        if src.startswith("/"):
            assert site.get(src).status_code == 200, src


def test_agents_first_screen_has_buttons_then_the_request_and_its_image(site):
    from calotype.install_links import SITE

    doc = site.get("/agents").text
    first_screen = doc[doc.index("<h1>"):doc.index('<ul class="toc">')]
    hrefs = [html.unescape(h) for h in re.findall(r'<a class="button" href="([^"]+)"', first_screen)]
    assert hrefs == [SITE.cursor, SITE.vscode]
    assert "CALOTYPE_KEY" in first_screen and "secret storage" in first_screen
    request = html.unescape(re.sub(r"<[^>]+>", "", first_screen))
    assert DEMO_REQUEST in request and "/v1/screenshot" in request
    assert first_screen.index(DEMO_REQUEST) < first_screen.index(f'src="{DEMO_IMAGE}"')
    img = re.search(r"<img\b[^>]*>", first_screen).group(0)
    assert "20 November 2026" in img and 'width="1280" height="800"' in img
    # Windsurf and Claude Desktop, each with what its docs say it needs.
    assert '"serverUrl"' in doc and "${env:CALOTYPE_KEY}" in doc
    desktop = doc[doc.index('id="claude-desktop"'):doc.index('id="codex"')]
    assert "Request headers" in desktop and "beta" in desktop and "mcp-remote" in desktop
    assert "No sign-in" in desktop


def _vscode_payload(link: str) -> dict:
    """Undo the web redirect, then parse as VS Code does: the URI decodes its query once,
    and the handler runs decodeURIComponent on it again before JSON.parse."""
    import json
    from urllib.parse import unquote

    if link.startswith("https://vscode.dev/redirect?url="):
        link = unquote(link.removeprefix("https://vscode.dev/redirect?url="))
    assert link.startswith("vscode:mcp/install?")
    return json.loads(unquote(unquote(link.removeprefix("vscode:mcp/install?"))))


def _cursor_config(link: str) -> tuple[str, dict]:
    import json
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(link).query)
    return query["name"][0], json.loads(base64.b64decode(query["config"][0], validate=True))


def test_install_links_decode_to_a_keyless_config_with_their_tag():
    from calotype import install_links as links
    from calotype.storage import REF_TAG

    for buttons, suffix in ((links.README, ""), (links.SITE, "-site")):
        name, cursor = _cursor_config(buttons.cursor)
        assert name == "calotype"
        assert cursor == {
            "url": f"https://calotype.dev/mcp?ref=btn-cursor{suffix}",
            "headers": {"Authorization": "Bearer ${env:CALOTYPE_KEY}"},
        }
        vscode = _vscode_payload(buttons.vscode)
        assert vscode["name"] == "calotype" and vscode["type"] == "http"
        assert vscode["url"] == f"https://calotype.dev/mcp?ref=btn-vscode{suffix}"
        assert vscode["headers"] == {"Authorization": "Bearer ${input:calotype-key}"}
        assert vscode["inputs"] == [
            {"type": "promptString", "id": "calotype-key", "description": "Calotype API key, starts cal_", "password": True}
        ]
        for tag in (f"btn-cursor{suffix}", f"btn-vscode{suffix}"):
            assert REF_TAG.match(tag)
    assert links.README.cursor.startswith("https://cursor.com/en/install-mcp?name=calotype&config=")
    assert links.README.vscode.startswith("https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F")
    assert links.SITE.cursor.startswith("cursor://anysphere.cursor-deeplink/mcp/install?name=calotype&config=")


def test_readme_first_screen_matches_the_generated_links_and_the_served_image():
    from calotype.install_links import readme_markdown

    readme = REPO_README.read_text(encoding="utf-8")
    top = readme[:readme.index("## Client setup")]
    assert readme_markdown() in top
    assert "Skewbald, an AI agent" in top
    assert "https://calotype.dev/mcp?ref=gh-readme" in top
    assert top.index("https://calotype.dev/mcp?ref=gh-readme") < top.index(DEMO_REQUEST)
    image = re.search(r"!\[([^\]]+)\]\((https://calotype\.dev/images/[^)]+)\)", top)
    assert image and "20 November 2026" in image.group(1)
    assert top.index(DEMO_REQUEST) < image.start()
    assert (SITE_DIR / "images" / image.group(2).rsplit("/", 1)[1]).is_file()
    for client in ("### Claude Code", "### Cursor", "### VS Code", "### Windsurf", "### Claude Desktop", "### Codex"):
        assert client in readme, client
    assert "calotype.dev/agents#n8n" in readme
    assert re.search(r"cal_[A-Za-z0-9]{8,}", readme) is None


# --- free tries ------------------------------------------------------------------------------


def test_privacy_says_what_the_logs_hold_about_the_calling_software(site):
    privacy = site.get("/privacy").text
    assert "the first word of its <code>User-Agent</code> header" in privacy
    assert "a coarse kind (browser, bot or tool)" in privacy
    assert "the name and version the client reports about itself" in privacy
    assert "The rest of the <code>User-Agent</code> header is never logged or kept." in privacy
    assert "or your request headers, apart from the one word" in privacy
    assert privacy.count("23 November 2026") == 1 and "the name of the calling software in the logs" in privacy


def test_free_tries_in_docs_and_policies_match_the_code(site, settings):
    from calotype import tries

    docs = site.get("/docs").text
    assert f"at most {settings.tries_per_ip_per_hour} tries an hour from one IP address" in docs
    assert f"at most {settings.tries_per_day} renders a day across the whole service" in docs
    assert f"{tries.WIDTH} × {tries.HEIGHT}, not full page, JPEG at quality {tries.QUALITY} with ads blocked" in docs
    privacy = site.get("/privacy").text
    assert 'id="tries"' in privacy
    assert "only to limit key creation and free tries" in privacy
    assert "keeps neither the address nor the image" in privacy
    assert "applies to the free tries on the home page" in site.get("/acceptable-use").text


# --- guides (running plan H4) -----------------------------------------------------------------

GUIDES = [p for p in PAGES if p.path.startswith("/guides/")]
GUIDE_IDS = [p.path.rsplit("/", 1)[1] for p in GUIDES]
# Where each guide is linked from, besides the /guides index.
GUIDE_LINKED_FROM = {
    "claude-code-cursor-screenshots": "/agents",
    "playwright-python-screenshots": "/switch",
    "n8n-screenshot-workflow": "/agents",
    "headless-browser-ssrf": "/docs",
}


def main_of(document: str) -> str:
    return document[document.index("<main"):document.index("</main>")]


def code_blocks(document: str, language: str) -> list[str]:
    found = re.findall(rf'<pre><code class="language-{language}">(.*?)</code></pre>', document, re.DOTALL)
    return [html.unescape(block) for block in found]


def test_there_are_four_guides_and_an_index(site):
    assert sorted(GUIDE_IDS) == sorted(GUIDE_LINKED_FROM)
    index = site.get("/guides")
    assert index.status_code == 200
    assert index.text.count("<h1>") == 1
    assert "Skewbald, an AI agent" in main_of(index.text)
    for page in GUIDES:
        assert f'<a href="{page.path}">' in main_of(index.text), page.path
    assert 'href="/guides"' in footer_of(site.get("/").text)


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_serves_with_title_description_and_one_h1(site, page):
    r = site.get(page.path)
    assert r.status_code == 200
    doc = r.text
    assert f"<title>{html.escape(page.title)}</title>" in doc
    assert page.title.endswith(" | Calotype")
    assert 50 <= len(meta(doc, "name", "description")) <= 200
    assert len(re.findall(r"<h1\b", doc)) == 1
    heading = html.unescape(re.search(r"<h1>(.*?)</h1>", doc).group(1))
    assert heading == page.title.removesuffix(" | Calotype")
    assert f"<loc>{canonical_url(page.path)}</loc>" in site.get("/sitemap.xml").text


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_says_near_the_top_that_an_ai_agent_wrote_it(site, page):
    main = main_of(site.get(page.path).text)
    top = main[:main.index("<h2")]
    assert "Written by <strong>Skewbald, an AI agent</strong>, not a person." in top
    assert "builds and runs Calotype" in top
    assert "21 November 2026" in top  # the day its facts were checked
    assert 'id="sources"' in main and 'href="/guides"' in top


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_is_linked_from_an_existing_page_and_tags_its_links(site, page):
    from calotype.storage import REF_TAG

    slug = page.path.rsplit("/", 1)[1]
    assert f'href="{page.path}"' in main_of(site.get(GUIDE_LINKED_FROM[slug]).text)
    tag = f"guide-{slug}"
    assert REF_TAG.match(tag)
    doc = site.get(page.path).text
    # The link or address that leads to a key carries the guide's tag, and no other guide's.
    assert f"?ref={tag}" in doc
    assert set(re.findall(r"\?ref=([a-z0-9-]+)", main_of(doc))) == {tag}


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_length_and_plain_words(site, page):
    main = main_of(site.get(page.path).text)
    prose = re.sub(r"<pre>.*?</pre>", " ", main, flags=re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", " ", prose))
    assert 900 <= len(text.split()) <= 1800, len(text.split())
    for word in ("best", "fastest", "leading", "world-class", "unbeatable", "blazing", "seamless", "effortless",
                 "revolutionary", "powerful"):
        assert re.search(rf"\b{word}\b", text.lower()) is None, word


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_prices_and_parameters_match_the_code(site, page, settings):
    from calotype import params

    doc = site.get(page.path).text
    text = html.unescape(re.sub(r"<[^>]+>", " ", main_of(doc)))
    plans = settings.plans
    prices = {p.price_usd_cents // 100 for p in plans.values() if p.price_usd_cents}
    assert {int(n) for n in re.findall(r"\$(\d+)", text)} <= prices
    for name in ("starter", "pro"):
        plan = plans[name]
        for quota in re.findall(rf"\${plan.price_usd_cents // 100} (?:a month )?for ([\d,]+)", text):
            assert quota == f"{plan.monthly_quota:,}"
    if "screenshots a month" in text:
        assert f"{plans['free'].monthly_quota} screenshots a month" in text
    # Calotype requests in the examples use only parameters the API takes, and no real key.
    for block in re.findall(r"<pre><code>(.*?)</code></pre>", doc, re.DOTALL):
        if "/v1/screenshot" in block:
            names = set(re.findall(r"-d ([a-z_]+)=", block))
            assert names and names <= set(params.ScreenshotParams.model_fields), names
    assert re.search(r"cal_[A-Za-z0-9]{8,}", doc) is None


@pytest.mark.parametrize("page", GUIDES, ids=GUIDE_IDS)
def test_guide_code_samples_parse(site, page):
    import json

    doc = site.get(page.path).text
    for source in code_blocks(doc, "python"):
        compile(source, page.path, "exec")
    for source in code_blocks(doc, "json"):
        assert "mcpServers" in json.loads(source)
    # Every code block is one of the kinds checked above or a shell or prompt example.
    assert set(re.findall(r'<code class="([^"]+)">', doc)) <= {"language-python", "language-json"}


def test_guides_state_what_the_code_does():
    """Facts the guides give about Calotype, checked against the modules they describe."""
    from calotype import capture, egress, params, ssrf

    ssrf_guide = page_text("/guides/headless-browser-ssrf", paid_open=True)
    assert f"at most {ssrf.MAX_URL_LENGTH:,} characters" in ssrf_guide
    assert sorted(ssrf.ALLOWED_LOW_PORTS) == [80, 443] and "Ports 80, 443, and 1024 and up" in ssrf_guide
    for name in ("localhost", "metadata.google.internal"):
        assert name in ssrf.BLOCKED_HOSTNAMES and f"<code>{name}</code>" in ssrf_guide
    for suffix in (".internal", ".local", ".lan", ".corp"):
        assert suffix in ssrf.BLOCKED_HOST_SUFFIXES and f"<code>{suffix}</code>" in ssrf_guide
    assert "--disable-quic" in capture.CHROMIUM_ARGS and "--dns-prefetch-disable" in capture.CHROMIUM_ARGS
    assert "<-loopback>" in open(capture.__file__).read() and "&lt;-loopback&gt;" in ssrf_guide
    assert "Connection: close" in open(egress.__file__).read() and "Connection: close" in ssrf_guide
    assert "At most 50 MB downloaded and 30 seconds in total" in ssrf_guide
    playwright_guide = page_text("/guides/playwright-python-screenshots", paid_open=True)
    assert f"<code>delay_ms</code> (up to {params.MAX_DELAY_MS:,})" in playwright_guide
    assert f"cut off at {params.MAX_FULL_PAGE_HEIGHT:,} CSS pixels" in playwright_guide
    # The Dockerfile example names the same Playwright version as Calotype's own image.
    dockerfile = (SITE_DIR.parent / "Dockerfile").read_text()
    tag = re.search(r"playwright/python:(v[\d.]+-noble)", dockerfile).group(1)
    assert f"FROM mcr.microsoft.com/playwright/python:{tag}" in playwright_guide


# --- one page per alternative, and the MCP comparison (running plan H8) -----------------------

SWITCH_PAGES = [p for p in PAGES if p.path.startswith("/switch/")]
SWITCH_IDS = [p.path.rsplit("/", 1)[1] for p in SWITCH_PAGES]
API_SWITCH_PAGES = [p for p in SWITCH_PAGES if not p.path.endswith("/playwright-mcp")]
API_SWITCH_IDS = [p.path.rsplit("/", 1)[1] for p in API_SWITCH_PAGES]
COMPARE_PATH = "/compare/screenshot-mcp-servers"
H8_PAGES = SWITCH_PAGES + [p for p in PAGES if p.path == COMPARE_PATH]
H8_IDS = [p.path for p in H8_PAGES]
H8_READ_ON = "21 November 2026"
# The pages each one's facts about the other side were read from, on the date the page states.
H8_SOURCES = {
    "/switch/screenshotone": (
        "https://screenshotone.com/pricing/",
        "https://screenshotone.com/docs/options/",
        "https://screenshotone.com/docs/mcp/",
    ),
    "/switch/urlbox": ("https://urlbox.com/pricing", "https://urlbox.com/docs/options", "https://urlbox.com/docs/agents"),
    "/switch/screenshotapi-net": (
        "https://screenshotapi.net/pricing",
        "https://screenshotapi.net/docs/getStarted",
        "https://screenshotapi.net/integrations/mcp-server",
    ),
    "/switch/allscreenshots": (
        "https://allscreenshots.com/pricing",
        "https://docs.allscreenshots.com/api-reference/screenshots",
        "https://docs.allscreenshots.com/tools/mcp",
    ),
    "/switch/apiflash": ("https://apiflash.com/", "https://apiflash.com/documentation"),
    "/switch/playwright-mcp": ("https://github.com/microsoft/playwright-mcp",),
    COMPARE_PATH: (
        "https://github.com/microsoft/playwright-mcp",
        "https://github.com/ChromeDevTools/chrome-devtools-mcp",
        "https://github.com/just-every/mcp-screenshot-website-fast",
        "https://screenshotone.com/docs/mcp/",
        "https://github.com/screenshotone/mcp",
        "https://urlbox.com/docs/agents",
        "https://docs.allscreenshots.com/tools/mcp",
        "https://screenshotapi.net/integrations/mcp-server",
    ),
}
# The arguments of the hosted MCP server's one tool (remote_mcp.py), a subset of the API's parameters.
MCP_TOOL_ARGUMENTS = {"url", "full_page", "width", "height", "format", "dark_mode", "block_ads"}


def h8_ref_tag(path: str) -> str:
    return "compare-mcp" if path == COMPARE_PATH else "switch-" + path.rsplit("/", 1)[1]


def section(document: str, start: str, end: str) -> str:
    return document[document.index(f'id="{start}"'):document.index(f'id="{end}"')]


def calotype_request(example: str) -> dict:
    """The parameters of the Calotype request in a page's before-and-after example."""
    import json
    from urllib.parse import parse_qsl

    blocks = [html.unescape(re.sub(r"<[^>]+>", "", b)) for b in re.findall(r"<pre><code>(.*?)</code></pre>", example, re.DOTALL)]
    (block,) = [b for b in blocks if "calotype.dev/v1/screenshot" in b]
    assert "Bearer cal_your_key" in block
    query = re.search(r"/v1/screenshot\?([^\"]+)\"", block)
    if query:
        return dict(parse_qsl(query.group(1), strict_parsing=True))
    return json.loads(re.search(r"-d '(\{.*?\})'", block).group(1))


def test_there_is_a_page_per_alternative_and_the_overview_links_each(site):
    assert sorted(SWITCH_IDS) == [
        "allscreenshots", "apiflash", "playwright-mcp", "screenshotapi-net", "screenshotone", "urlbox",
    ]
    overview = main_of(site.get("/switch").text)
    for page in H8_PAGES:
        assert f'href="{page.path}"' in overview, page.path
    # The anchors other pages and outside links point at are still on the overview.
    for anchor in ("fit", "prices", "params", "example", "differences", "more", "playwright", "feedback"):
        assert f'id="{anchor}"' in overview, anchor
    # The comparison is linked from the guides index and the agents page.
    for path in ("/guides", "/agents"):
        assert f'href="{COMPARE_PATH}"' in main_of(site.get(path).text), path


@pytest.mark.parametrize("page", H8_PAGES, ids=H8_IDS)
def test_h8_page_serves_with_title_description_and_one_h1(site, page):
    r = site.get(page.path)
    assert r.status_code == 200
    doc = r.text
    assert f"<title>{html.escape(page.title)}</title>" in doc
    assert page.title.endswith(" | Calotype")
    assert 50 <= len(meta(doc, "name", "description")) <= 200
    assert len(re.findall(r"<h1\b", doc)) == 1
    heading = html.unescape(re.search(r"<h1>(.*?)</h1>", doc).group(1))
    assert heading == page.title.removesuffix(" | Calotype")
    assert f"<loc>{canonical_url(page.path)}</loc>" in site.get("/sitemap.xml").text
    assert (SITE_DIR / "pages" / page.source).parent.name == page.path.split("/")[1]


@pytest.mark.parametrize("page", H8_PAGES, ids=H8_IDS)
def test_h8_page_discloses_its_author_and_dates_its_sources(site, page):
    main = main_of(site.get(page.path).text)
    top = main[:main.index("<h2")]
    assert "Written by <strong>Skewbald, an AI agent</strong>, not a person." in top
    assert "builds and runs Calotype" in top and "weigh the Calotype parts" in top
    assert H8_READ_ON in top
    if page.path == COMPARE_PATH:
        assert "<strong>The author runs one of the entries.</strong>" in top
        assert 'href="/guides"' in top
    else:
        assert 'href="/switch"' in top
    sources = main[main.index('id="sources"'):]
    assert f"Read on {H8_READ_ON}:" in sources
    for url in H8_SOURCES[page.path]:
        assert f'href="{url}"' in sources, url
    # No date but the day the pages were read.
    assert set(re.findall(r"\d{1,2} November 2026", main)) == {H8_READ_ON}


@pytest.mark.parametrize("page", H8_PAGES, ids=H8_IDS)
def test_h8_page_tags_its_one_link_to_a_key(site, page):
    from calotype.storage import REF_TAG

    tag = h8_ref_tag(page.path)
    assert REF_TAG.match(tag)
    main = main_of(site.get(page.path).text)
    # One tagged link, to a page with the key button.
    assert re.findall(r'href="(/[^"]*)\?ref=([a-z0-9-]+)(#key)?"', main) in ([("/", tag, "#key")], [("/agents", tag, "")])
    assert main.count("?ref=") == 1


@pytest.mark.parametrize("page", H8_PAGES, ids=H8_IDS)
def test_h8_page_states_calotypes_prices_from_plans_json(site, page, settings):
    text = html.unescape(re.sub(r"<[^>]+>", " ", main_of(site.get(page.path).text)))
    free, starter, pro = (settings.plans[name] for name in ("free", "starter", "pro"))
    starter_usd, pro_usd = starter.price_usd_cents // 100, pro.price_usd_cents // 100
    if page.path == COMPARE_PATH:
        assert f"{free.monthly_quota} screenshots a month free, then ${starter_usd} a month for {starter.monthly_quota:,}" in text
        assert f"${pro_usd} for {pro.monthly_quota:,}" in text
        return
    assert f"${starter_usd} a month, {starter.monthly_quota:,} screenshots" in text
    assert f"${starter.price_usd_cents / starter.monthly_quota * 10:.2f}" in text  # per 1,000
    assert f"${pro_usd}" in text and f"{pro.monthly_quota:,}" in text
    assert f"${pro.price_usd_cents / pro.monthly_quota * 10:.2f}" in text
    assert f"{free.monthly_quota} a month, no account" in text
    assert "quota_exceeded" in text or page.path.endswith("/playwright-mcp")
    # "$19 for" and "$49 for" are only ever said of Calotype's plans on these pages, with the plan's quota.
    for plan in (starter, pro):
        for quota in re.findall(rf"\${plan.price_usd_cents // 100} (?:a month )?for ([\d,]*\d)", text):
            assert quota == f"{plan.monthly_quota:,}", quota


@pytest.mark.parametrize("page", API_SWITCH_PAGES, ids=API_SWITCH_IDS)
def test_switch_page_maps_and_converts_only_what_the_code_takes(site, page):
    from calotype import params

    doc = site.get(page.path).text
    for anchor in ("prices", "params", "example", "they-do-more", "calotype-does", "stay", "start", "sources"):
        assert f'id="{anchor}"' in doc and f'href="#{anchor}"' in doc, anchor
    fields = set(params.ScreenshotParams.model_fields)
    # The mapping table's Calotype column names every parameter, and nothing the API refuses.
    table = section(doc, "params", "example")
    calotype_cells = re.findall(r"<tr><td>.*?</td><td>(.*?)</td>", table)
    named = {name for cell in calotype_cells for name in re.findall(r"<code>([a-z_]+)</code>", cell)}
    assert fields <= named, fields - named
    assert named - fields <= {"access_key", "png", "jpeg", "webp", "jpg", "load", "domcontentloaded", "networkidle", "commit", "cal_"}
    assert f"at most {params.MAX_DELAY_MS:,}" in table
    assert f"{params.MIN_SIDE} to {params.MAX_WIDTH:,}" in table and f"{params.MIN_SIDE} to {params.MAX_HEIGHT:,}" in table
    assert f"cuts a full page off at {params.MAX_FULL_PAGE_HEIGHT:,} CSS pixels" in table
    # The converted request is one the API accepts.
    request = calotype_request(section(doc, "example", "they-do-more"))
    parsed = params.ScreenshotParams.model_validate(request)
    assert parsed.check_combinations() is None
    assert parsed.url.startswith("https://example.com")
    assert len(request) >= 6, "the example converts more than the address"
    assert re.search(r"cal_[A-Za-z0-9]{8,}", doc) is None
    assert 'href="/switch#feedback"' in doc


def test_playwright_page_covers_where_it_runs_and_uses_the_tools_own_arguments(site):
    import json

    from calotype import params

    doc = site.get("/switch/playwright-mcp").text
    for anchor in ("prices", "where", "private", "interaction", "params", "example", "they-do-more", "calotype-does",
                   "stay", "sources"):
        assert f'id="{anchor}"' in doc and f'href="#{anchor}"' in doc, anchor
    assert "Node.js 20 or newer" in doc and "npx @playwright/mcp@latest" in doc
    assert "localhost" in section(doc, "private", "interaction")
    assert "usually the better tool" in main_of(doc)[:main_of(doc).index("<h2")]
    assert "Apache-2.0" in doc and "Free" in section(doc, "prices", "where")
    (call,) = code_blocks(doc, "json")
    arguments = json.loads(call)
    assert set(arguments) <= MCP_TOOL_ARGUMENTS
    assert params.ScreenshotParams.model_validate(arguments).check_combinations() is None
    table = section(doc, "params", "example")
    calotype_cells = re.findall(r"<tr><td>.*?</td><td>(.*?)</td>", table)
    named = {name for cell in calotype_cells for name in re.findall(r"<code>([a-z_]+)</code>", cell)}
    assert MCP_TOOL_ARGUMENTS <= named | {"block_ads"} and named <= set(params.ScreenshotParams.model_fields) | {
        "screenshot", "jpeg", "png", "webp"}
    assert f"One tool with {['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven'][len(MCP_TOOL_ARGUMENTS)]} arguments" in doc
    assert re.search(r"cal_[A-Za-z0-9]{8,}", doc) is None and "cal_your_key" in doc


def test_mcp_tool_arguments_in_the_tests_match_the_server():
    import inspect

    from calotype.remote_mcp import RemoteMCP

    signature = inspect.signature(RemoteMCP.screenshot)
    assert set(signature.parameters) - {"self", "ctx"} == MCP_TOOL_ARGUMENTS


def test_comparison_names_every_server_and_says_where_others_are_ahead(site):
    doc = site.get(COMPARE_PATH).text
    for anchor in ("short", "where", "returns", "install", "notes", "ahead", "calotype", "method", "sources"):
        assert f'id="{anchor}"' in doc and f'href="#{anchor}"' in doc, anchor
    for table in (section(doc, "where", "returns"), section(doc, "returns", "install")):
        for name in ("Playwright MCP", "Chrome DevTools MCP", "@just-every/mcp-screenshot-website-fast", "ScreenshotOne",
                     "Urlbox", "Allscreenshots", "ScreenshotAPI.net", "Calotype"):
            assert name in table, name
        assert table.count("<tr><td>") >= 8
    where = section(doc, "where", "returns")
    assert "local package" in where and "hosted" in where  # both of ScreenshotOne's servers
    for column in ("Local or remote", "Sign-in", "Price and free use", "Licence"):
        assert f"<th>{column}</th>" in where, column
    for column in ("What comes back to the model", "Localhost and private pages", "Interaction"):
        assert f"<th>{column}</th>" in doc, column
    install = section(doc, "install", "notes")
    for line in ("npx @playwright/mcp@latest", "npx -y chrome-devtools-mcp@latest", "@screenshotone/mcp",
                 "https://mcp.screenshotone.com", "https://api.urlbox.com/mcp", "https://mcp.allscreenshots.com/mcp/v1",
                 "npx -y @screenshotapinet/mcp", "npx -y @just-every/mcp-screenshot-website-fast",
                 "https://calotype.dev/mcp"):
        assert line in install, line
    # It says where the others do more, and does not claim the three hosted OAuth servers are missing.
    ahead = section(doc, "ahead", "calotype")
    for word in ("localhost", "OAuth", "free", "PDF", "Markdown"):
        assert word in ahead, word
    assert "No OAuth" in where and "Nothing was installed or run" in doc
    assert re.search(r"cal_[A-Za-z0-9]{8,}", doc) is None


@pytest.mark.parametrize("page", H8_PAGES, ids=H8_IDS)
def test_h8_page_stays_plain_and_claims_no_first_place(site, page):
    main = main_of(site.get(page.path).text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", main)).lower()
    for word in ("best", "fastest", "leading", "world-class", "unbeatable", "blazing", "seamless", "effortless",
                 "revolutionary", "powerful", "worse", "terrible", "cheapest", "lowest price", "no remote mcp"):
        assert re.search(rf"\b{word}\b", text) is None, word
    # What the other side does better comes before what Calotype does.
    if page.path != COMPARE_PATH:
        assert main.index('id="they-do-more"') < main.index('id="calotype-does"') < main.index('id="stay"')
        more = section(main, "they-do-more", "calotype-does")
        does = section(main, "calotype-does", "stay")
        assert more.count("<li>") > does.count("<li>")
    # No images, forms or scripts of its own: nothing of the other sites is shown.
    assert "<img" not in main and "<form" not in main and "<script" not in main



# --- /llms.txt ---------------------------------------------------------------------------------


def test_llms_txt_is_served_as_plain_text(site):
    r = site.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.text.startswith("# Calotype\n\n> Calotype is built and run by Skewbald, an AI agent")
    assert site.head("/llms.txt").status_code == 200
    assert "<!--" not in r.text  # the sale markers are resolved


def test_llms_txt_links_only_to_pages_that_exist(site):
    text = site.get("/llms.txt").text
    links = re.findall(r"\]\((https://calotype\.dev[^)\s]*)\)", text)
    assert len(links) >= 10
    paths = {p.path for p in PAGES}
    for link in links:
        path, _, anchor = link[len(SITE_ORIGIN):].partition("#")
        if path == "/openapi.json":
            assert site.get(path).status_code == 200
            continue
        assert path in paths, link
        if anchor:
            assert f'id="{anchor}"' in site.get(path).text, link


def test_llms_txt_facts_match_the_code(site, settings):
    from calotype.params import MAX_DELAY_MS, MAX_FULL_PAGE_HEIGHT, MAX_HEIGHT, MAX_WIDTH
    from calotype.ssrf import MAX_URL_LENGTH

    text = site.get("/llms.txt").text
    plans = settings.plans
    assert f"Free, {plans['free'].monthly_quota} screenshots" in text
    for name in ("starter", "pro"):
        plan = plans[name]
        assert f"{plan.title}, ${plan.price_usd_cents // 100} a month for {plan.monthly_quota:,}" in text
    pack = settings.with_plans().credit_packs["starter"]
    assert '{"pack": "starter"}' in text
    assert f"buys {pack.screenshots:,} screenshots for ${pack.price_usd_cents // 100}, paid once" in text
    assert f"At most {settings.keys_per_ip_per_hour} keys an hour from one IP address" in text
    assert f"up to {MAX_URL_LENGTH:,} characters" in text
    assert f"`width` 100 to {MAX_WIDTH}" in text and f"`height` 100 to {MAX_HEIGHT}" in text
    assert f"up to {MAX_FULL_PAGE_HEIGHT:,} px tall" in text and f"`delay_ms` 0 to {MAX_DELAY_MS:,}" in text
    assert f"{settings.capture_timeout_s} seconds per capture" in text
    for path in ("https://calotype.dev/mcp", "POST /v1/keys", "/v1/screenshot", "POST /v1/credits"):
        assert path in text


def test_llms_txt_without_checkout_does_not_offer_paid_plans(settings, sqlite_store):
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=sqlite_store,
                     renderer=FakeRenderer())
    with TestClient(app) as c:
        text = c.get("/llms.txt").text  # the test app has no Stripe settings
    assert "Paid plans are not on sale on this server." in text
    assert "/v1/billing/checkout" not in text


def test_llms_txt_is_not_in_the_sitemap_and_not_disallowed(site):
    assert "llms.txt" not in site.get("/sitemap.xml").text
    assert "Disallow: /llms" not in site.get("/robots.txt").text
