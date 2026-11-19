"""API tests with a fake renderer: validation, auth, quota, logging."""

from __future__ import annotations

import logging

import pytest

from calotype.capture import CaptureError
from calotype.storage import current_month, hash_key

from .conftest import stored_text


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def test_health(client):
    for path in ("/health", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_landing_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Skewbald" in r.text
    assert "AI agent" in r.text


# --- auth -------------------------------------------------------------------


def test_missing_key(client):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_key"


def test_bad_key(client):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth("cal_nope"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_key"


def test_wrong_auth_scheme(client, free_key):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers={"Authorization": f"Basic {free_key}"})
    assert r.status_code == 401


def test_bearer_key(client, free_key, fake_renderer):
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["x-calotype-usage"] == "1/100"
    assert len(fake_renderer.calls) == 1


def test_query_key(client, free_key):
    r = client.get("/v1/screenshot", params={"url": "https://example.com", "access_key": free_key})
    assert r.status_code == 200


def test_post_json(client, free_key, fake_renderer):
    r = client.post(
        "/v1/screenshot",
        json={"url": "https://example.com", "format": "webp", "quality": 70, "full_page": True},
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"
    p = fake_renderer.calls[0]
    assert p.format == "webp" and p.quality == 70 and p.full_page is True


def test_revoked_key(client, store):
    raw, key = store.create_key("free", "x")
    store.revoke_key(key.id)
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 401


def test_keys_are_stored_hashed(store):
    raw, key = store.create_key("starter", "hash check")
    dump = stored_text(store)
    assert raw not in dump
    assert hash_key(raw) in dump
    assert store.get_key(raw).id == key.id


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"url": ""},
        {"url": "https://example.com", "width": 3841},
        {"url": "https://example.com", "width": 99},
        {"url": "https://example.com", "height": 2161},
        {"url": "https://example.com", "width": "wide"},
        {"url": "https://example.com", "format": "gif"},
        {"url": "https://example.com", "quality": 0},
        {"url": "https://example.com", "quality": 101},
        {"url": "https://example.com", "format": "png", "quality": 50},
        {"url": "https://example.com", "device_scale_factor": 4},
        {"url": "https://example.com", "device_scale_factor": 0.5},
        {"url": "https://example.com", "delay_ms": 10001},
        {"url": "https://example.com", "delay_ms": -1},
        {"url": "https://example.com", "wait_until": "forever"},
        {"url": "https://example.com", "full_page": "maybe"},
        {"url": "https://example.com", "unknown_option": "1"},
    ],
)
def test_invalid_parameters(client, free_key, params, fake_renderer):
    r = client.get("/v1/screenshot", params=params, headers=auth(free_key))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "invalid_parameters"
    assert fake_renderer.calls == []


@pytest.mark.parametrize(
    "url,code",
    [
        ("file:///etc/passwd", "invalid_scheme"),
        ("data:text/html,hi", "invalid_scheme"),
        ("chrome://version", "invalid_scheme"),
        ("http://127.0.0.1/", "blocked_target"),
        ("http://169.254.169.254/latest/meta-data/", "blocked_target"),
        ("http://[::1]/", "blocked_target"),
        ("http://localhost:8080/", "blocked_target"),
        ("http://rebind.example/", "blocked_target"),
        ("http://nxdomain.example/", "dns_error"),
    ],
)
def test_blocked_urls_rejected_without_quota(client, free_key, store, url, code, fake_renderer):
    r = client.get("/v1/screenshot", params={"url": url}, headers=auth(free_key))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == code
    assert fake_renderer.calls == []
    key = store.get_key(free_key)
    assert store.get_usage(key.id, current_month()) == 0


def test_full_page_height_cap():
    from calotype.params import ScreenshotParams

    assert ScreenshotParams(url="https://x.example", width=1280).full_page_height_cap() == 20000
    p = ScreenshotParams(url="https://x.example", width=3840, height=2160, device_scale_factor=3)
    assert p.check_combinations() is None
    assert 2160 <= p.full_page_height_cap() < 20000
    assert p.width * p.full_page_height_cap() * 9 <= 80_000_000


def test_valid_options_reach_renderer(client, free_key, fake_renderer):
    r = client.get(
        "/v1/screenshot",
        params={
            "url": "https://example.com/a?b=c",
            "width": 3840,
            "height": 2160,
            "format": "jpg",
            "quality": "55",
            "device_scale_factor": "2",
            "delay_ms": "250",
            "wait_until": "networkidle",
            "block_ads": "true",
            "dark_mode": "1",
            "full_page": "false",
        },
        headers=auth(free_key),
    )
    assert r.status_code == 200, r.text
    p = fake_renderer.calls[0]
    assert (p.width, p.height, p.format, p.quality, p.device_scale_factor) == (3840, 2160, "jpeg", 55, 2.0)
    assert p.block_ads and p.dark_mode and not p.full_page and p.wait_until == "networkidle"


# --- quota ------------------------------------------------------------------


def test_quota_exceeded(client, store, settings):
    raw, key = store.create_key("free", "quota")
    month = current_month()
    limit = settings.plans["free"].monthly_quota
    for _ in range(limit - 1):
        assert store.try_consume(key.id, month, limit)[0]
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 200
    assert r.headers["x-calotype-usage"] == f"{limit}/{limit}"
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(raw))
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["code"] == "quota_exceeded"
    assert err["limit"] == limit and err["used"] == limit and err["plan"] == "free"
    assert err["resets_at"].endswith("T00:00:00Z")


def test_quota_is_per_key_and_per_month(store):
    a, ka = store.create_key("free", "a")
    b, kb = store.create_key("free", "b")
    assert store.try_consume(ka.id, "2026-11", 1) == (True, 1)
    assert store.try_consume(ka.id, "2026-11", 1) == (False, 1)
    assert store.try_consume(kb.id, "2026-11", 1) == (True, 1)
    assert store.try_consume(ka.id, "2026-12", 1) == (True, 1)


def test_failed_capture_is_refunded(client, free_key, store, fake_renderer):
    fake_renderer.fail_with = CaptureError(422, "target_unreachable", "nope")
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "target_unreachable"
    assert store.get_usage(store.get_key(free_key).id, current_month()) == 0


def test_busy_returns_503(client, free_key, fake_renderer):
    fake_renderer.fail_with = CaptureError(503, "busy", "busy", {"Retry-After": "5"})
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"


def test_usage_endpoint(client, free_key):
    client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    r = client.get("/v1/usage", headers=auth(free_key))
    assert r.json()["used"] == 1 and r.json()["limit"] == 100


def test_usage_names_the_key_by_reference_not_by_value(client, free_key):
    import hashlib

    body = client.get("/v1/usage", headers=auth(free_key)).json()
    assert body["key_ref"] == hashlib.sha256(free_key.encode()).hexdigest()[:12]
    assert free_key not in str(body)


# --- logging ----------------------------------------------------------------


def test_logs_have_no_key_or_query(client, free_key, caplog):
    caplog.set_level(logging.INFO)
    secret_path = "/private/report?token=s3cret-token"
    client.get("/v1/screenshot", params={"url": "https://example.com" + secret_path, "access_key": free_key})
    client.post("/v1/screenshot", json={"url": "https://example.com" + secret_path}, headers=auth(free_key))
    records = [r for r in caplog.records if r.name.startswith("calotype")]
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in records)
    assert "request" in text
    assert free_key not in text
    assert "s3cret-token" not in text
    assert "/private/report" not in text
    assert "https://example.com" in text


def test_log_redaction():
    from calotype.logs import redact

    line = redact(
        'Page.goto: net::ERR_FAILED at https://example.com/a/b?token=abc&x=1 '
        'GET /v1/screenshot?access_key=cal_abcdefghijklmnop Authorization: Bearer cal_zzzzzzzzzzzz'
    )
    assert "token=abc" not in line and "/a/b" not in line
    assert "abcdefghijklmnop" not in line and "zzzzzzzzzzzz" not in line
    assert "https://example.com" in line
