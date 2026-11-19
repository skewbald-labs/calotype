"""Self-serve keys, the in-memory rate limit, and the contact ordinal."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from calotype import admin
from calotype.app import create_app
from calotype.capture import CaptureError
from calotype.ratelimit import SlidingWindowLimiter, client_ip
from calotype.ssrf import AddressPolicy
from calotype.storage import hash_key

from .conftest import FakeRenderer, fake_resolver, stored_key_fields, stored_text


def make_client(settings, store, renderer=None, **overrides):
    app = create_app(
        replace(settings, **overrides),
        policy=AddressPolicy(resolver=fake_resolver),
        storage=store,
        renderer=renderer or FakeRenderer(),
    )
    return TestClient(app)


def shoot(client, key, url="https://example.com"):
    return client.get("/v1/screenshot", params={"url": url}, headers={"Authorization": f"Bearer {key}"})


# --- creation -----------------------------------------------------------------


def test_create_free_key(client, store):
    r = client.post("/v1/keys")
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    raw = body["key"]
    assert raw.startswith("cal_") and body["plan"] == "free" and body["monthly_quota"] == 100
    assert "only time" in body["message"]

    key = store.get_key(raw)
    assert key.plan == "free" and key.internal is False and key.label == ""
    assert shoot(client, raw).status_code == 200

    # The database holds the hash, never the key, and no client address.
    dump = stored_text(store)
    assert raw not in dump and hash_key(raw) in dump
    assert "testclient" not in dump and "127.0.0.1" not in dump
    assert stored_key_fields(store) == {
        "id", "key_hash", "plan", "label", "created_at", "revoked_at", "internal", "successful_screenshots",
        "stripe_customer_id", "stripe_subscription_id",
    }
    assert (key.stripe_customer_id, key.stripe_subscription_id) == (None, None)


def test_create_key_needs_no_body_or_auth(client):
    r = client.post("/v1/keys", json={"email": "someone@example.com"})
    assert r.status_code == 201
    assert "email" not in r.json()


def test_self_serve_can_be_disabled(settings, store):
    with make_client(settings, store, self_serve_keys=False) as c:
        assert c.post("/v1/keys").status_code == 403


# --- rate limit -----------------------------------------------------------------


def test_rate_limit_per_ip(settings, store):
    with make_client(settings, store) as c:
        for _ in range(3):
            assert c.post("/v1/keys").status_code == 201
        r = c.post("/v1/keys")
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "rate_limited"
        assert 1 <= int(r.headers["retry-after"]) <= 3600
    assert len(store.list_keys()) == 3


def test_rate_limit_uses_trusted_forwarded_hop(settings, store):
    with make_client(settings, store, trusted_proxy_hops=1) as c:
        for _ in range(3):
            assert c.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 201
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 429
        # A forged left-hand entry does not give the same client a new bucket.
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}).status_code == 429
        # A different client is not affected.
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 201


def test_rate_limit_ignores_forwarded_header_by_default(settings, store):
    with make_client(settings, store) as c:
        for i in range(3):
            assert c.post("/v1/keys", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code == 201
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "10.0.0.99"}).status_code == 429


def test_global_key_limit(settings, store):
    with make_client(settings, store, keys_global_per_hour=2, trusted_proxy_hops=1) as c:
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 201
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 201
        assert c.post("/v1/keys", headers={"X-Forwarded-For": "198.51.100.3"}).status_code == 429


def test_limiter_window_expires():
    now = [1000.0]
    lim = SlidingWindowLimiter(3, window_s=3600, clock=lambda: now[0])
    assert [lim.check("a")[0] for _ in range(4)] == [True, True, True, False]
    allowed, retry = lim.check("a")
    assert not allowed and 3500 < retry <= 3601
    assert lim.check("b")[0]
    now[0] += 3600
    assert lim.check("a")[0]


def test_limiter_memory_is_bounded():
    now = [0.0]
    lim = SlidingWindowLimiter(1, window_s=60, max_tracked=100, clock=lambda: now[0])
    for i in range(1000):
        now[0] += 0.001
        lim.check(f"ip{i}")
    assert len(lim._hits) <= 100


@pytest.mark.parametrize(
    "peer,xff,hops,expected",
    [
        ("10.1.1.1", None, 0, "10.1.1.1"),
        ("10.1.1.1", "203.0.113.5", 0, "10.1.1.1"),
        ("10.1.1.1", "203.0.113.5", 1, "203.0.113.5"),
        ("10.1.1.1", "6.6.6.6, 203.0.113.5", 1, "203.0.113.5"),
        ("10.1.1.1", "6.6.6.6, 203.0.113.5, 35.0.0.1", 2, "203.0.113.5"),
        ("10.1.1.1", "", 1, "10.1.1.1"),
        (None, None, 0, "unknown"),
    ],
)
def test_client_ip(peer, xff, hops, expected):
    assert client_ip(peer, xff, hops) == expected


# --- contact ordinal ----------------------------------------------------------------


def test_contact_ordinal_is_creation_order_of_keys_with_a_success(settings, store):
    internal_raw, internal_key = store.create_key("free", "operator", internal=True)
    renderer = FakeRenderer()
    with make_client(settings, store, renderer, trusted_proxy_hops=1) as c:
        keys = [c.post("/v1/keys", headers={"X-Forwarded-For": f"198.51.100.{i}"}).json()["key"] for i in range(4)]
        a, b, never, failed = keys

        # Internal keys never count, however much they are used.
        assert shoot(c, internal_raw).status_code == 200
        assert store.list_contacts() == []

        # B succeeds before A, but the ordinal follows creation order.
        assert shoot(c, b).status_code == 200
        assert [x.key_id for x in store.list_contacts()] == [store.get_key(b).id]
        assert shoot(c, a).status_code == 200
        assert shoot(c, a).status_code == 200

        # A key whose only capture failed is not a contact.
        renderer.fail_with = CaptureError(422, "target_unreachable", "nope")
        assert shoot(c, failed).status_code == 422
        renderer.fail_with = None
        # Nor is a key that was refused before rendering.
        assert shoot(c, failed, "http://127.0.0.1/").status_code == 400

    contacts = store.list_contacts()
    assert [(x.ordinal, x.key_id) for x in contacts] == [(1, store.get_key(a).id), (2, store.get_key(b).id)]
    assert contacts[0].successful_screenshots == 2
    assert store.contact_ordinal(store.get_key(b).id) == 2
    assert store.contact_ordinal(store.get_key(never).id) is None
    assert store.contact_ordinal(internal_key.id) is None
    assert store.get_key(internal_raw).successful_screenshots == 1


def test_admin_keys_are_internal_and_contacts_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "c.db"))
    assert admin.main(["create-key", "--plan", "pro", "--label", "ops"]) == 0
    raw = capsys.readouterr().out.strip()
    from calotype.storage import SQLiteStorage

    store = SQLiteStorage(tmp_path / "c.db")
    key = store.get_key(raw)
    assert key.internal is True
    store.record_success(key.id)
    _, ext = store.create_key("free")
    store.record_success(ext.id)
    assert admin.main(["contacts"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("ordinal")
    assert lines[1].split("\t")[:2] == ["1", str(ext.id)]
    assert len(lines) == 2


def test_landing_page_has_key_button_and_upgrade(client):
    html = client.get("/").text
    assert 'id="create-key"' in html and "/v1/keys" in html
    assert "<!-- Prices: source of truth is calotype/plans.json" in html
    # The upgrade form lives on /pricing, where Stripe sends buyers back to. It is there
    # only on a server that sells paid plans; this test app has no Stripe.
    from calotype.config import SITE_DIR
    from calotype.pages import PAGES, render_page

    pricing = next(p for p in PAGES if p.path == "/pricing")
    html = render_page(pricing, SITE_DIR, paid_open=True).body.decode("utf-8")
    assert 'id="upgrade"' in html and "skewbald@agentmail.to" in html
    # A working checkout flow: asks for the existing key, then calls checkout.
    assert 'id="upgrade-form"' in html and 'id="upgrade-key"' in html and 'type="password"' in html
    assert "/v1/billing/checkout" in html and "/v1/billing/portal" in html
    assert 'value="starter"' in html and 'value="pro"' in html
    assert "checkout is coming" not in html.lower() and "is coming" not in html
    assert "<!-- Prices: source of truth is calotype/plans.json" in html


# --- ?ref= tags on new keys ---------------------------------------------------


def test_key_created_with_ref_tag_is_labelled_and_reported(client, store):
    r = client.post("/v1/keys", json={"ref": "dt-linkedin"})
    assert r.status_code == 201
    keys = store.list_keys()
    assert keys[-1].label == "ref:dt-linkedin"
    from calotype.storage import source_from_label

    assert source_from_label(keys[-1].label) == "dt-linkedin"


@pytest.mark.parametrize("body", [None, {}, {"ref": "Bad Tag!"}, {"ref": "x" * 41}, {"ref": 7}, ["dt"]])
def test_key_without_a_valid_ref_tag_gets_no_label(client, store, body):
    r = client.post("/v1/keys", json=body) if body is not None else client.post("/v1/keys")
    assert r.status_code == 201
    assert store.list_keys()[-1].label == ""


def test_key_button_sends_the_ref_tag():
    # The key button's script lives in the shared layout, so /, /agents and any page with the button send it.
    page = (Path(__file__).resolve().parents[1] / "site" / "layout.html").read_text()
    assert "URLSearchParams(location.search).get(\"ref\")" in page


def test_privacy_mentions_ref_tags():
    assert "?ref=" in (Path(__file__).resolve().parents[1] / "site" / "pages" / "privacy.html").read_text()
