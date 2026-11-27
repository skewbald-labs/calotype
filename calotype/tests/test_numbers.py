"""The weekly numbers page (/numbers) and its data file, site/numbers.json."""

from __future__ import annotations

import json
import re
import shutil
from datetime import date

import pytest
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.config import SITE_DIR
from calotype.pages import NUMBERS_FIELDS, PAGES, canonical_url, load_numbers, render_page
from calotype.ssrf import AddressPolicy

from .conftest import FakeRenderer, fake_resolver

NUMBERS_PAGE = next(p for p in PAGES if p.path == "/numbers")


@pytest.fixture
def site(settings, sqlite_store):
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=sqlite_store, renderer=FakeRenderer())
    with TestClient(app) as c:
        yield c


def test_numbers_page_shows_week_one(site):
    r = site.get("/numbers")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    for text in ("5,303", "$12.98", "n8n published my page-watch template", "Week 1"):
        assert text in r.text
    assert "https://bsky.app/profile/skewbald.bsky.social" in r.text


def test_numbers_is_in_the_sitemap_and_the_footer(site):
    assert canonical_url("/numbers") in site.get("/sitemap.xml").text
    assert 'href="/numbers"' in re.search(r"<footer\b.*?</footer>", site.get("/").text, re.DOTALL).group(0)
    assert "/numbers" in site.get("/llms.txt").text


def test_numbers_page_loads_nothing_external(site):
    doc = site.get("/numbers").text
    assert re.search(r"<script\b[^>]*\bsrc=", doc) is None
    assert re.search(r"<link\b[^>]*\bhref=\"(?!/)(?!data:)(?!https://calotype.dev)", doc) is None


def test_weeks_are_listed_newest_first(tmp_path):
    site_dir = tmp_path / "site"
    shutil.copytree(SITE_DIR, site_dir)
    weeks = load_numbers(SITE_DIR)
    newer = {**weeks[0], "week": 2, "as_of": "2026-12-04", "looked": 7777, "note": "A second week."}
    (site_dir / "numbers.json").write_text(json.dumps([newer, *weeks]), encoding="utf-8")
    doc = render_page(NUMBERS_PAGE, site_dir).body.decode()
    assert "Week 2, as of 2026-12-04" in doc
    rows = re.findall(r"<tr><td>(\d+)</td>", doc)
    assert rows == ["2", "1"]
    assert doc.index("7,777") < doc.index("5,303")


def test_numbers_file_schema():
    weeks = load_numbers(SITE_DIR)
    assert weeks, "at least week 1"
    for entry in weeks:
        assert set(entry) == set(NUMBERS_FIELDS)
        for name, types in NUMBERS_FIELDS.items():
            assert isinstance(entry[name], types) and not isinstance(entry[name], bool), name
        date.fromisoformat(entry["as_of"])
        assert entry["week"] >= 1 and entry["note"].strip()
    numbers = [e["week"] for e in weeks]
    assert len(numbers) == len(set(numbers))
    dates = [e["as_of"] for e in weeks]
    assert dates == sorted(dates, reverse=True) and len(dates) == len(set(dates))
    assert numbers == sorted(numbers, reverse=True)
