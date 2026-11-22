"""The optional email address on a key, and quota notices (running plan H1).

The sender is the skewbald-checks job in the free-reign repository; its tests
cover the per-address cap, the email text and marking notices sent. Here: the
address in the API and both backends, queueing, and the removal link.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from calotype import admin
from calotype.app import NEAR_LIMIT, create_app, upgrade_text
from calotype.config import Plan, Settings
from calotype.notices import (
    email_token,
    mask_email,
    normalize_email,
    thresholds_at,
    token_key_id,
    token_matches,
)
from calotype.pages import SITE_ORIGIN
from calotype.ssrf import AddressPolicy
from calotype.storage import Notice, SQLiteStorage, current_month, key_ref

from .conftest import FakeRenderer, fake_resolver, stored_text

SECRET = "test-secret-" + "x" * 32
ADDRESS = "Someone@Example.COM"
STORED = "Someone@example.com"  # the domain is lower-cased, the local part kept


def make_client(settings, store, renderer=None, **overrides):
    overrides.setdefault("email_token_secret", SECRET)
    app = create_app(
        replace(settings, **overrides),
        policy=AddressPolicy(resolver=fake_resolver),
        storage=store,
        renderer=renderer or FakeRenderer(),
    )
    return TestClient(app)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def shoot(client, key):
    return client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(key))


# A five-screenshot free plan: 80% is the 4th screenshot, 100% the 5th.
SMALL_PLANS = {
    "free": Plan("free", 5, 0, "Free"),
    "starter": Plan("starter", 5000, 1900, "Starter"),
}


# --- the address itself -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("someone@example.com", "someone@example.com"),
        ("  Some.One+tag@Mail.Example.COM ", "Some.One+tag@mail.example.com"),
        ("x@a.io", "x@a.io"),
    ],
)
def test_normalize_email_accepts_and_lowercases_the_domain(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "plain", "a@b", "a@@b.com", "a@b@c.com", "@example.com", "x@.com", "x@com.", "x@a..com",
        "a b@c.com", "a@c.com\nBcc: z@z.com", "<a@b.com>", "a@b.com, c@d.com", "a;b@c.com",
        "x" * 243 + "@example.com", 7, None, ["a@b.com"],
    ],
)
def test_normalize_email_rejects(raw):
    assert normalize_email(raw) is None


def test_254_characters_is_the_limit():
    ok = "x" * 242 + "@example.com"
    assert len(ok) == 254 and normalize_email(ok) == ok
    assert normalize_email("x" + ok) is None


def test_mask():
    assert mask_email("someone@example.com") == "s***@example.com"
    assert mask_email(None) is None


def test_the_address_is_never_in_a_repr(store):
    raw, key = store.create_key("free", email=STORED)
    assert key.email == STORED and STORED not in repr(key) and STORED not in repr(store.get_key(raw))
    assert SECRET not in repr(Settings(email_token_secret=SECRET))


# --- creating a key ------------------------------------------------------------------------


def test_create_key_with_email(settings, store):
    with make_client(settings, store) as c:
        r = c.post("/v1/keys", json={"email": ADDRESS, "ref": "dt-linkedin"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["email"] == "S***@example.com" and body["notices"] is True
        assert "omeone" not in r.text
        key = store.get_key(body["key"])
        assert key.email == STORED and key.label == "ref:dt-linkedin"
        assert STORED in stored_text(store)


def test_create_key_without_email_is_unchanged(settings, store):
    with make_client(settings, store, keys_per_ip_per_hour=10) as c:
        for body in (None, {}, {"email": None}, {"email": ""}):
            r = c.post("/v1/keys", json=body) if body is not None else c.post("/v1/keys")
            assert r.status_code == 201
            assert "email" not in r.json() and "notices" not in r.json()
            assert store.get_key(r.json()["key"]).email is None


@pytest.mark.parametrize("bad", ["not-an-address", "a@b", "a b@c.com", 42, "x" * 300 + "@e.com"])
def test_create_key_with_a_bad_email_is_refused_and_makes_no_key(settings, store, bad):
    with make_client(settings, store) as c:
        r = c.post("/v1/keys", json={"email": bad})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_email"
        assert store.list_keys() == []
        # A refused address does not use up one of the IP's three keys an hour.
        assert all(c.post("/v1/keys").status_code == 201 for _ in range(3))


# --- set, replace, remove ----------------------------------------------------------------------


def test_set_replace_and_remove(settings, store):
    raw, key = store.create_key("free")
    with make_client(settings, store) as c:
        assert c.get("/v1/usage", headers=auth(raw)).json()["email"] is None

        r = c.post("/v1/keys/email", json={"email": "first@example.com"}, headers=auth(raw))
        assert r.status_code == 200 and r.json() == {"email": "f***@example.com", "notices": True}
        assert store.get_key_by_id(key.id).email == "first@example.com"

        r = c.post("/v1/keys/email", json={"email": "Second@EXAMPLE.org"}, headers=auth(raw))
        assert r.json()["email"] == "S***@example.org"
        assert store.get_key_by_id(key.id).email == "Second@example.org"
        assert "first@example.com" not in stored_text(store)

        r = c.request("DELETE", "/v1/keys/email", headers=auth(raw))
        assert r.status_code == 200 and r.json() == {"email": None, "notices": False}
        assert store.get_key_by_id(key.id).email is None
        assert "Second@" not in stored_text(store)
        # Removing again is fine.
        assert c.request("DELETE", "/v1/keys/email", headers=auth(raw)).status_code == 200


def test_set_email_errors(settings, store):
    raw, _ = store.create_key("free")
    with make_client(settings, store) as c:
        assert c.post("/v1/keys/email", json={"email": "x@example.com"}).status_code == 401
        r = c.post("/v1/keys/email", json={"email": "nope"}, headers=auth(raw))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_email"
        r = c.post("/v1/keys/email", content=b"not json", headers=auth(raw))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"
    # With the feature off, an address cannot be set (but one can always be removed).
    with make_client(settings, store, email_token_secret="") as c:
        r = c.post("/v1/keys/email", json={"email": "x@example.com"}, headers=auth(raw))
        assert r.status_code == 503 and r.json()["error"]["code"] == "notices_unavailable"
        assert c.request("DELETE", "/v1/keys/email", headers=auth(raw)).status_code == 200


def test_usage_shows_only_the_masked_address(settings, store):
    raw, key = store.create_key("free", email="someone@example.com")
    with make_client(settings, store) as c:
        r = c.get("/v1/usage", headers=auth(raw))
        assert r.json()["email"] == "s***@example.com" and r.json()["notices"] is True
        assert "someone@example.com" not in r.text
    with make_client(settings, store, email_token_secret="short") as c:
        # A secret under 32 characters leaves the feature off.
        assert c.get("/v1/usage", headers=auth(raw)).json()["notices"] is False


# --- revoking and deleting ------------------------------------------------------------------


def test_delete_and_revoke_remove_the_address(store):
    _, a = store.create_key("free", email="a@example.com")
    _, b = store.create_key("free", email="b@example.com")
    store.queue_notice(_notice(a.id))
    assert store.delete_key(a.id)
    assert store.list_notices(a.id) == []
    assert store.revoke_key(b.id)
    assert store.get_key_by_id(b.id).email is None
    dump = stored_text(store)
    assert "a@example.com" not in dump and "b@example.com" not in dump


def test_admin_delete_key_removes_the_address_and_notices(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "c.db"))
    store = SQLiteStorage(tmp_path / "c.db")
    store.init()
    _, key = store.create_key("free", email="gone@example.com")
    store.queue_notice(_notice(key.id))
    assert admin.main(["delete-key", str(key.id)]) == 0
    err = capsys.readouterr().err
    assert "email address" in err and "gone@example.com" not in err
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 0
    dump = stored_text(store)
    assert "gone@example.com" not in dump
    assert store.list_notices(key.id) == []


def test_admin_remove_email_by_address_and_by_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "c.db"))
    store = SQLiteStorage(tmp_path / "c.db")
    store.init()
    _, a = store.create_key("free", email="stop@example.com")
    _, b = store.create_key("free", email="stop@example.com")
    _, c = store.create_key("free", email="other@example.com")
    # The domain's case does not matter; the local part is matched as stored.
    assert admin.main(["remove-email", "stop@EXAMPLE.com"]) == 0
    assert "stop@example.com" not in capsys.readouterr().err
    assert store.get_key_by_id(a.id).email is None and store.get_key_by_id(b.id).email is None
    assert admin.main(["remove-email", str(c.id)]) == 0
    assert store.get_key_by_id(c.id).email is None
    assert admin.main(["remove-email", str(c.id)]) == 1


def test_keys_with_email(store):
    _, a = store.create_key("free", email="same@example.com")
    _, b = store.create_key("free")
    store.set_email(b.id, "same@example.com")
    assert store.keys_with_email("same@example.com") == [a.id, b.id]
    assert store.set_email(999999, "x@example.com") is False


# --- queueing notices ------------------------------------------------------------------------


def _notice(key_id, month="2026-11", threshold=80):
    return Notice(key_id, month, threshold, 80, 100, "free", "abc123abc123", "2026-12-01T00:00:00Z", "up", "https://x")


def test_thresholds():
    assert [t for n in range(1, 101) for t in thresholds_at(n, 100)] == [80, 100]
    assert thresholds_at(80, 100) == [80] and thresholds_at(100, 100) == [100]
    assert thresholds_at(4, 5) == [80] and thresholds_at(5, 5) == [100]
    assert thresholds_at(1, 1) == [100]  # 80% of 1 rounds up to all of it: one notice
    assert thresholds_at(0, 0) == []


def test_notice_queued_once_at_80_and_once_at_100(settings, store):
    raw, key = store.create_key("free", email="someone@example.com")
    with make_client(settings, store, plans=SMALL_PLANS) as c:
        for n in range(1, 4):
            assert shoot(c, raw).status_code == 200
        assert store.list_notices(key.id) == []
        assert shoot(c, raw).status_code == 200  # 4 of 5: 80%
        [eighty] = store.list_notices(key.id)
        assert shoot(c, raw).status_code == 200  # 5 of 5: 100%
        assert shoot(c, raw).status_code == 429  # over: nothing more
        assert shoot(c, raw).status_code == 429
    notices = store.list_notices(key.id)
    assert [(n.threshold, n.used, n.quota, n.status) for n in notices] == [(80, 4, 5, "pending"), (100, 5, 5, "pending")]
    month = current_month()
    assert all(n.month == month and n.key_ref == key_ref(raw) and n.plan == "free" for n in notices)
    assert eighty.resets_at.endswith("-01T00:00:00Z")
    # The notice carries the same upgrade line the MCP tool gives near the limit.
    assert notices[0].origin == SITE_ORIGIN
    assert notices[0].upgrade == upgrade_text(SMALL_PLANS, SMALL_PLANS["free"], SITE_ORIGIN, False)
    assert notices[0].upgrade.startswith("Paid plans") and 4 >= NEAR_LIMIT * 5  # the tool says it from 4 of 5 too
    # The notice never holds the address, nor the key.
    for n in notices:
        assert "someone" not in repr(n) and raw not in repr(n)
    # A second queue for the same key, month and threshold is refused; another month is not.
    assert store.queue_notice(notices[0]) is False
    assert store.queue_notice(replace(notices[0], month="2099-01")) is True


def test_refunded_capture_does_not_queue_twice(settings, store):
    from calotype.capture import CaptureError

    raw, key = store.create_key("free", email="someone@example.com")
    renderer = FakeRenderer()
    with make_client(settings, store, renderer, plans=SMALL_PLANS) as c:
        for _ in range(4):
            assert shoot(c, raw).status_code == 200
        renderer.fail_with = CaptureError(422, "target_unreachable", "nope")
        assert shoot(c, raw).status_code == 422  # counted to 5, refunded to 4: no 100% notice
        renderer.fail_with = None
        assert [n.threshold for n in store.list_notices(key.id)] == [80]
        assert shoot(c, raw).status_code == 200
    assert [n.threshold for n in store.list_notices(key.id)] == [80, 100]


def test_nothing_queued_without_an_address(settings, store):
    raw, key = store.create_key("free")
    with make_client(settings, store, plans=SMALL_PLANS) as c:
        for _ in range(6):
            shoot(c, raw)
    assert store.list_notices(key.id) == []


def test_nothing_queued_without_the_secret(settings, store):
    raw, key = store.create_key("free", email="someone@example.com")
    with make_client(settings, store, plans=SMALL_PLANS, email_token_secret="") as c:
        for _ in range(6):
            shoot(c, raw)
    assert store.list_notices(key.id) == []


# --- the removal link ---------------------------------------------------------------------------


def test_token_matches_the_job():
    # The same example is in free-reign ops/checks/test_main.py; both sides must agree.
    assert email_token("s" * 32, 7, "someone@example.com") == "7.YMYkKMWeY5ZCGtdalBzjFOIyM6F5I-_WXDmZXH98jPs"
    assert token_key_id("7.YMYkKMWeY5ZCGtdalBzjFOIyM6F5I-_WXDmZXH98jPs") == 7
    assert token_key_id("07.x") is None and token_key_id(None) is None


def test_token_checks():
    t = email_token(SECRET, 3, "a@example.com")
    assert token_matches(SECRET, t, 3, "a@example.com")
    assert not token_matches(SECRET, t, 3, "b@example.com")  # the address changed
    assert not token_matches(SECRET, t, 4, "a@example.com")  # another key
    assert not token_matches(SECRET + "y", t, 3, "a@example.com")  # another secret
    assert not token_matches(SECRET, t, 3, None)
    assert not token_matches("", email_token("", 3, "a@example.com"), 3, "a@example.com")


def test_removal_link_removes_the_address(settings, store):
    _, key = store.create_key("free", email="someone@example.com")
    token = email_token(SECRET, key.id, "someone@example.com")
    with make_client(settings, store) as c:
        r = c.get("/v1/keys/email/remove", params={"token": token})
        assert r.status_code == 200 and "Address removed" in r.text
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
        assert store.get_key_by_id(key.id).email is None
        # Used again: the address is gone, so the link no longer matches anything.
        assert c.get("/v1/keys/email/remove", params={"token": token}).status_code == 400


def test_one_click_post_removes_the_address(settings, store):
    _, key = store.create_key("free", email="someone@example.com")
    token = email_token(SECRET, key.id, "someone@example.com")
    with make_client(settings, store) as c:
        r = c.post(f"/v1/keys/email/remove?token={token}", data={"List-Unsubscribe": "One-Click"})
        assert r.status_code == 200
    assert store.get_key_by_id(key.id).email is None


def test_removal_link_refuses_tampered_and_wrong_key_tokens(settings, store):
    _, a = store.create_key("free", email="a@example.com")
    _, b = store.create_key("free", email="b@example.com")
    good = email_token(SECRET, a.id, "a@example.com")
    mac = good.split(".", 1)[1]
    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
    wrong_key = f"{b.id}.{mac}"  # a's signature on b's id
    other_secret = email_token("z" * 40, a.id, "a@example.com")
    with make_client(settings, store) as c:
        for token in (tampered, wrong_key, other_secret, "", "garbage", f"{a.id}.", "99999." + mac):
            r = c.get("/v1/keys/email/remove", params={"token": token})
            assert r.status_code == 400 and "not valid" in r.text, token
    assert store.get_key_by_id(a.id).email == "a@example.com"
    assert store.get_key_by_id(b.id).email == "b@example.com"


def test_removal_link_for_a_replaced_address_does_nothing(settings, store):
    _, key = store.create_key("free", email="old@example.com")
    old = email_token(SECRET, key.id, "old@example.com")
    store.set_email(key.id, "new@example.com")
    with make_client(settings, store) as c:
        assert c.get("/v1/keys/email/remove", params={"token": old}).status_code == 400
    assert store.get_key_by_id(key.id).email == "new@example.com"
