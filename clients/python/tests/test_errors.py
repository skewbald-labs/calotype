from __future__ import annotations

import pytest

from calotype import (
    APIConnectionError,
    AuthenticationError,
    BlockedTargetError,
    Calotype,
    CalotypeError,
    CaptureFailedError,
    InvalidRequestError,
    PermissionDeniedError,
    QuotaExceededError,
    RateLimitError,
    Response,
    ServerBusyError,
    ServerError,
)

from fakes import FakeTransport, error_response, json_response


def raised(response: Response | Exception) -> CalotypeError:
    client = Calotype("cal_test", transport=FakeTransport(response))
    with pytest.raises(CalotypeError) as caught:
        client.screenshot("https://example.com")
    return caught.value


# Every code the API can answer on these endpoints, with the status it uses (calotype/app.py).
@pytest.mark.parametrize(
    "status, code, cls",
    [
        (401, "missing_key", AuthenticationError),
        (401, "invalid_key", AuthenticationError),
        (403, "plan_unavailable", PermissionDeniedError),
        (403, "self_serve_disabled", PermissionDeniedError),
        (400, "invalid_parameters", InvalidRequestError),
        (400, "invalid_url", InvalidRequestError),
        (400, "invalid_scheme", InvalidRequestError),
        (400, "invalid_json", InvalidRequestError),
        (400, "invalid_email", InvalidRequestError),
        (400, "blocked_target", BlockedTargetError),
        (422, "blocked_target", BlockedTargetError),
        (400, "dns_error", CaptureFailedError),
        (422, "target_unreachable", CaptureFailedError),
        (422, "page_too_large", CaptureFailedError),
        (504, "timeout", CaptureFailedError),
        (429, "quota_exceeded", QuotaExceededError),
        (429, "rate_limited", RateLimitError),
        (503, "busy", ServerBusyError),
        (500, "internal_error", ServerError),
    ],
)
def test_each_error_code_has_its_class(status, code, cls):
    error = raised(error_response(status, code, "What went wrong."))
    assert type(error) is cls
    assert error.code == code
    assert error.status == status
    assert error.message == "What went wrong."
    assert str(error) == f"{code}: What went wrong."


def test_an_unknown_code_falls_back_on_the_status():
    assert type(raised(error_response(400, "brand_new_code", "x"))) is InvalidRequestError
    assert type(raised(error_response(401, "brand_new_code", "x"))) is AuthenticationError
    assert type(raised(error_response(403, "brand_new_code", "x"))) is PermissionDeniedError
    assert type(raised(error_response(418, "brand_new_code", "x"))) is CalotypeError
    assert type(raised(error_response(503, "billing_unavailable", "x"))) is ServerError


def test_an_answer_that_is_not_the_api_error_shape():
    for body in (b"<html>bad gateway</html>", b"[1, 2]", b'{"error": "nope"}', b""):
        error = raised(Response(502, {"content-type": "text/html"}, body))
        assert type(error) is ServerError
        assert (error.code, error.status, error.message) == ("", 502, "HTTP 502")
        assert str(error) == "HTTP 502"


def test_invalid_parameters_lists_the_fields():
    error = raised(
        error_response(
            400,
            "invalid_parameters",
            "Some parameters are not valid.",
            details=[{"field": "width", "message": "Input should be less than or equal to 3840"}],
        )
    )
    assert isinstance(error, InvalidRequestError)
    assert error.details == [{"field": "width", "message": "Input should be less than or equal to 3840"}]
    assert str(error) == (
        "invalid_parameters: Some parameters are not valid. width: Input should be less than or equal to 3840"
    )
    assert raised(error_response(400, "invalid_url", "The URL is not valid.")).details == []


def test_quota_exceeded_carries_the_counts():
    error = raised(
        error_response(
            429, "quota_exceeded", "This key has used its 100 screenshots.",
            plan="free", limit=100, used=100, resets_at="2026-12-01T00:00:00Z",
        )
    )
    assert isinstance(error, QuotaExceededError)
    assert (error.plan, error.limit, error.used, error.resets_at) == ("free", 100, 100, "2026-12-01T00:00:00Z")


def test_retry_after_from_the_body_or_the_header():
    limited = raised(error_response(429, "rate_limited", "Too many keys.", {"retry-after": "30"}, retry_after_s=1800))
    assert limited.retry_after == 1800
    busy = raised(error_response(503, "busy", "All renderers are busy. Retry shortly.", {"retry-after": "5"}))
    assert busy.retry_after == 5
    assert raised(error_response(500, "internal_error", "x")).retry_after is None


def test_no_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("CALOTYPE_API_KEY", raising=False)
    transport = FakeTransport()
    client = Calotype(transport=transport)
    for call in (lambda: client.screenshot("https://example.com"), client.usage):
        with pytest.raises(AuthenticationError) as caught:
            call()
        assert caught.value.code == "missing_key"
        assert caught.value.status is None
        assert "create_key" in caught.value.message
    assert transport.requests == []


def test_a_connection_failure_passes_through_as_api_connection_error():
    error = raised(APIConnectionError("Could not reach the Calotype API (TimeoutError)."))
    assert type(error) is APIConnectionError
    assert (error.code, error.status) == ("", None)


def test_a_success_that_is_not_json_is_an_error_not_a_crash():
    client = Calotype("cal_test", transport=FakeTransport(Response(200, {}, b"<html>"), json_response(201, {"plan": "free"})))
    with pytest.raises(CalotypeError, match="not a JSON object"):
        client.usage()
    with pytest.raises(CalotypeError, match="held no key"):
        client.create_key()
