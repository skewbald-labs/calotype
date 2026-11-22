"""Errors the client raises, one class per kind of API error code.

Every API error answers ``{"error": {"code": ..., "message": ...}}``. The code
is stable; ``error_for`` picks the class from it, then from the HTTP status.
"""

from __future__ import annotations

from typing import Any, Mapping


class CalotypeError(Exception):
    """Any error from the client. The base of every class below.

    ``code`` is the API's stable error code (``""`` when the answer had none),
    ``status`` the HTTP status (``None`` when there was no answer), and
    ``body`` the parsed ``error`` object with any extra fields.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status: int | None = None,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.body: Mapping[str, Any] = body or {}
        self.headers: Mapping[str, str] = headers or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.code else self.message

    @property
    def retry_after(self) -> float | None:
        """Seconds the server asked the caller to wait, if it said."""
        value = self.body.get("retry_after_s", self.headers.get("retry-after"))
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


class APIConnectionError(CalotypeError):
    """The API could not be reached, or did not answer in time. Nothing was counted."""


class AuthenticationError(CalotypeError):
    """No key, or a key that is wrong or revoked (``missing_key``, ``invalid_key``)."""


class PermissionDeniedError(CalotypeError):
    """The server refused the action (``plan_unavailable``, ``self_serve_disabled``)."""


class InvalidRequestError(CalotypeError):
    """The request was not valid (``invalid_parameters``, ``invalid_url``,
    ``invalid_scheme``, ``invalid_json``, ``invalid_email``)."""

    @property
    def details(self) -> list[dict[str, str]]:
        """For ``invalid_parameters``: one ``{"field": ..., "message": ...}`` per bad field."""
        details = self.body.get("details")
        return list(details) if isinstance(details, list) else []

    def __str__(self) -> str:
        text = super().__str__()
        if self.details:
            text += " " + "; ".join(f"{d.get('field')}: {d.get('message')}" for d in self.details)
        return text


class BlockedTargetError(CalotypeError):
    """The address is private, local or otherwise not allowed (``blocked_target``)."""


class CaptureFailedError(CalotypeError):
    """The page could not be captured (``dns_error``, ``target_unreachable``,
    ``page_too_large``, ``timeout``). It was not counted against the quota."""


class QuotaExceededError(CalotypeError):
    """The key has used its screenshots for the month (``quota_exceeded``)."""

    @property
    def plan(self) -> str | None:
        return self.body.get("plan")

    @property
    def limit(self) -> int | None:
        return self.body.get("limit")

    @property
    def used(self) -> int | None:
        return self.body.get("used")

    @property
    def resets_at(self) -> str | None:
        """When the count resets, as an ISO 8601 UTC time."""
        return self.body.get("resets_at")


class RateLimitError(CalotypeError):
    """Too many keys were created recently (``rate_limited``). See ``retry_after``."""


class ServerBusyError(CalotypeError):
    """All renderers are busy (``busy``). Retry after ``retry_after`` seconds."""


class ServerError(CalotypeError):
    """The service failed (``internal_error``, or any other 5xx answer)."""


_BY_CODE: dict[str, type[CalotypeError]] = {
    "missing_key": AuthenticationError,
    "invalid_key": AuthenticationError,
    "plan_unavailable": PermissionDeniedError,
    "self_serve_disabled": PermissionDeniedError,
    "invalid_parameters": InvalidRequestError,
    "invalid_url": InvalidRequestError,
    "invalid_scheme": InvalidRequestError,
    "invalid_json": InvalidRequestError,
    "invalid_email": InvalidRequestError,
    "blocked_target": BlockedTargetError,
    "dns_error": CaptureFailedError,
    "target_unreachable": CaptureFailedError,
    "page_too_large": CaptureFailedError,
    "timeout": CaptureFailedError,
    "quota_exceeded": QuotaExceededError,
    "rate_limited": RateLimitError,
    "busy": ServerBusyError,
    "internal_error": ServerError,
}

_BY_STATUS: dict[int, type[CalotypeError]] = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
}


def error_for(status: int, error: Mapping[str, Any] | None, headers: Mapping[str, str]) -> CalotypeError:
    """The exception for an answer that is not a success."""
    error = error if isinstance(error, Mapping) else {}
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    message = error.get("message") if isinstance(error.get("message"), str) else f"HTTP {status}"
    cls = _BY_CODE.get(code) or _BY_STATUS.get(status) or (ServerError if status >= 500 else CalotypeError)
    return cls(message, code=code, status=status, body=error, headers=headers)
