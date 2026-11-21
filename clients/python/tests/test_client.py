from __future__ import annotations

import pytest

import calotype
from calotype import Calotype, NewKey, Response, Screenshot, Usage

from fakes import FakeTransport, json_response

PNG = b"\x89PNG\r\n\x1a\n fake image bytes"


def image_response(**headers: str) -> Response:
    return Response(200, {"content-type": "image/png", **headers}, PNG)


def test_screenshot_returns_the_image_bytes_and_sends_only_what_was_asked():
    transport = FakeTransport(image_response())
    client = Calotype("cal_test", transport=transport)

    assert client.screenshot("https://example.com") == PNG

    request = transport.last
    assert request.method == "POST"
    assert request.url == "https://calotype.dev/v1/screenshot"
    assert request.headers["Authorization"] == "Bearer cal_test"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"] == f"calotype-python/{calotype.__version__}"
    assert request.timeout == 90.0
    # Nothing but the URL: the API's own defaults apply.
    assert transport.last_json() == {"url": "https://example.com"}


def test_screenshot_passes_every_parameter_through():
    transport = FakeTransport(image_response())
    Calotype("cal_test", transport=transport).screenshot(
        "https://example.com",
        width=390,
        height=844,
        full_page=True,
        format="webp",
        quality=70,
        device_scale_factor=2.0,
        delay_ms=500,
        wait_until="networkidle",
        block_ads=True,
        dark_mode=False,
    )
    assert transport.last_json() == {
        "url": "https://example.com",
        "width": 390,
        "height": 844,
        "full_page": True,
        "format": "webp",
        "quality": 70,
        "device_scale_factor": 2.0,
        "delay_ms": 500,
        "wait_until": "networkidle",
        "block_ads": True,
        "dark_mode": False,  # False is a value, not "unset"
    }


def test_capture_reads_the_usage_and_page_headers():
    transport = FakeTransport(
        image_response(
            **{
                "content-type": "image/webp; charset=binary",
                "x-calotype-usage": "42/100",
                "x-calotype-duration-ms": "1830",
                "x-calotype-target-status": "404",
                "x-calotype-truncated": "1",
            }
        ),
        image_response(),
    )
    client = Calotype("cal_test", transport=transport)

    shot = client.capture("https://example.com/missing", format="webp")
    assert shot == Screenshot(
        image=PNG, content_type="image/webp", used=42, limit=100, target_status=404, truncated=True, duration_ms=1830
    )

    bare = client.capture("https://example.com")
    assert (bare.used, bare.limit, bare.target_status, bare.truncated, bare.duration_ms) == (None, None, None, False, None)
    assert "fake image" not in repr(bare)


def test_create_key_needs_no_key_and_tags_the_source():
    answer = {"key": "cal_new", "plan": "free", "monthly_quota": 100, "message": "This is the only time the key is shown."}
    transport = FakeTransport(json_response(201, answer), json_response(201, answer), json_response(201, answer))
    client = Calotype(api_key="", transport=transport)

    new = client.create_key()
    assert new == NewKey(key="cal_new", plan="free", monthly_quota=100, message="This is the only time the key is shown.")
    assert transport.last.method == "POST"
    assert transport.last.url == "https://calotype.dev/v1/keys"
    assert "Authorization" not in transport.last.headers
    assert transport.last_json() == {"ref": "pypi"}
    assert "cal_new" not in repr(new)
    assert client.api_key == ""  # the new key is returned, not adopted

    client.create_key(ref="my-tool", email="someone@example.com")
    assert transport.last_json() == {"ref": "my-tool", "email": "someone@example.com"}

    client.create_key(ref=None)
    assert transport.last_json() == {}


def test_usage():
    transport = FakeTransport(
        json_response(
            200,
            {
                "plan": "free",
                "month": "2026-11",
                "used": 7,
                "limit": 100,
                "resets_at": "2026-12-01T00:00:00Z",
                "key_ref": "cal_ab12",
                "email": "s***@example.com",
                "notices": True,
            },
        )
    )
    usage = Calotype("cal_test", transport=transport).usage()
    assert usage == Usage(
        plan="free",
        month="2026-11",
        used=7,
        limit=100,
        resets_at="2026-12-01T00:00:00Z",
        key_ref="cal_ab12",
        email="s***@example.com",
        notices=True,
    )
    assert usage.remaining == 93
    assert transport.last.method == "GET"
    assert transport.last.url == "https://calotype.dev/v1/usage"
    assert transport.last.body is None
    assert transport.last.headers["Authorization"] == "Bearer cal_test"


def test_key_comes_from_the_environment_and_never_shows_in_repr(monkeypatch):
    monkeypatch.setenv("CALOTYPE_API_KEY", " cal_env ")
    transport = FakeTransport(image_response())
    client = Calotype(transport=transport)
    client.screenshot("https://example.com")
    assert transport.last.headers["Authorization"] == "Bearer cal_env"
    assert "cal_env" not in repr(client)
    assert repr(client) == "Calotype(base_url='https://calotype.dev', api_key=set)"


def test_base_url_and_timeout_can_be_changed():
    transport = FakeTransport(image_response())
    Calotype("cal_test", base_url="http://127.0.0.1:8080/", timeout=5, transport=transport).screenshot("https://example.com")
    assert transport.last.url == "http://127.0.0.1:8080/v1/screenshot"
    assert transport.last.timeout == 5
    with pytest.raises(ValueError):
        Calotype("cal_test", base_url="file:///etc")


def test_the_package_is_typed_and_exports_what_it_documents():
    from importlib.resources import files

    assert files("calotype").joinpath("py.typed").is_file()
    for name in calotype.__all__:
        assert hasattr(calotype, name), name
    assert calotype.__version__ == "0.1.0"
