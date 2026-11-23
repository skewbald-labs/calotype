"""The client: three calls over the Calotype HTTP API, with the standard library only."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

from ._version import __version__
from .errors import APIConnectionError, AuthenticationError, CalotypeError, error_for

DEFAULT_BASE_URL = "https://calotype.dev"
USER_AGENT = f"calotype-python/{__version__}"

Format = Literal["png", "jpeg", "webp"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]


@dataclass(frozen=True)
class Request:
    """One HTTP request, as handed to the transport."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout: float


@dataclass(frozen=True)
class Response:
    """One HTTP answer, as the transport returns it. Header names are lower case."""

    status: int
    headers: Mapping[str, str]
    body: bytes


#: Sends a request and returns the answer, whatever its status. Raises
#: ``APIConnectionError`` when there is no answer. Pass your own to
#: ``Calotype(transport=...)`` to use another HTTP library, or a fake in tests.
Transport = Callable[[Request], Response]


def urllib_transport(request: Request) -> Response:
    """The default transport: ``urllib.request`` from the standard library."""
    req = urllib.request.Request(request.url, data=request.body, method=request.method, headers=dict(request.headers))
    try:
        try:
            with urllib.request.urlopen(req, timeout=request.timeout) as resp:
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
        except urllib.error.HTTPError as e:  # an answer with an error status
            try:
                return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
            finally:
                e.close()
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise APIConnectionError(f"Could not reach the Calotype API ({type(reason).__name__}).") from e


@dataclass(frozen=True)
class Screenshot:
    """A captured page: the image and what the API said about it."""

    image: bytes = field(repr=False)
    content_type: str
    #: Screenshots the key has used this month, and its monthly limit.
    used: int | None
    limit: int | None
    #: The page's own HTTP status, when the API reported one.
    target_status: int | None
    #: True if the page passed the download limit and the image shows what had loaded by then.
    truncated: bool
    duration_ms: int | None
    #: The whole scrolling page's height in CSS pixels, when the page said.
    page_height: int | None = None
    #: For a one-screen capture: how far down the page the screen's top is (less than the
    #: ``scroll_y`` sent when that was past the last screen). None for a full page.
    scroll_y: int | None = None


@dataclass(frozen=True)
class NewKey:
    """A key from ``create_key``. ``key`` is shown this once; Calotype keeps only its hash."""

    key: str = field(repr=False)
    plan: str
    monthly_quota: int
    message: str = ""


@dataclass(frozen=True)
class Usage:
    """A key's count for the current month."""

    plan: str
    month: str
    used: int
    limit: int
    resets_at: str
    #: Not secret: names this key to support without sending the key.
    key_ref: str
    #: The masked address quota notices go to, if the key has one.
    email: str | None = None
    notices: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class Calotype:
    """A client for the Calotype API.

    ``api_key`` defaults to the ``CALOTYPE_API_KEY`` environment variable.
    ``create_key`` needs no key. ``timeout`` is in seconds, per request.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 90.0,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("CALOTYPE_API_KEY", "")).strip()
        self.base_url = base_url.rstrip("/")
        if urlsplit(self.base_url).scheme not in ("http", "https"):
            raise ValueError("base_url must start with http:// or https://")
        self.timeout = timeout
        self._transport = transport or urllib_transport

    def __repr__(self) -> str:  # never the key
        return f"Calotype(base_url={self.base_url!r}, api_key={'set' if self.api_key else 'not set'})"

    # --- the three calls ------------------------------------------------------------------

    def screenshot(
        self,
        url: str,
        *,
        width: int | None = None,
        height: int | None = None,
        full_page: bool | None = None,
        format: Format | None = None,
        quality: int | None = None,
        device_scale_factor: float | None = None,
        delay_ms: int | None = None,
        wait_until: WaitUntil | None = None,
        block_ads: bool | None = None,
        dark_mode: bool | None = None,
        scroll_y: int | None = None,
    ) -> bytes:
        """Capture ``url`` and return the image bytes.

        Parameters left as ``None`` are not sent, so the API's defaults apply:
        a 1280 by 800 PNG of the viewport. ``scroll_y`` captures one screen further
        down the page instead of the first (not with ``full_page``); ``capture``
        reports how tall the page is. Reference: https://calotype.dev/docs
        """
        return self.capture(
            url,
            width=width,
            height=height,
            full_page=full_page,
            format=format,
            quality=quality,
            device_scale_factor=device_scale_factor,
            delay_ms=delay_ms,
            wait_until=wait_until,
            block_ads=block_ads,
            dark_mode=dark_mode,
            scroll_y=scroll_y,
        ).image

    def capture(
        self,
        url: str,
        *,
        width: int | None = None,
        height: int | None = None,
        full_page: bool | None = None,
        format: Format | None = None,
        quality: int | None = None,
        device_scale_factor: float | None = None,
        delay_ms: int | None = None,
        wait_until: WaitUntil | None = None,
        block_ads: bool | None = None,
        dark_mode: bool | None = None,
        scroll_y: int | None = None,
    ) -> Screenshot:
        """Like ``screenshot``, but also returns the usage count and the page's status."""
        params = {
            "url": url,
            "width": width,
            "height": height,
            "full_page": full_page,
            "format": format,
            "quality": quality,
            "device_scale_factor": device_scale_factor,
            "delay_ms": delay_ms,
            "wait_until": wait_until,
            "block_ads": block_ads,
            "dark_mode": dark_mode,
            "scroll_y": scroll_y,
        }
        resp = self._send("POST", "/v1/screenshot", {k: v for k, v in params.items() if v is not None}, auth=True)
        used, _, limit = resp.headers.get("x-calotype-usage", "").partition("/")
        return Screenshot(
            image=resp.body,
            content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
            used=_int(used),
            limit=_int(limit),
            target_status=_int(resp.headers.get("x-calotype-target-status")),
            truncated=bool(resp.headers.get("x-calotype-truncated")),
            duration_ms=_int(resp.headers.get("x-calotype-duration-ms")),
            page_height=_int(resp.headers.get("x-calotype-page-height")),
            scroll_y=_int(resp.headers.get("x-calotype-scroll-y")),
        )

    def create_key(self, ref: str | None = "pypi", *, email: str | None = None) -> NewKey:
        """Create a free key. Needs no key and no account.

        ``ref`` is a short tag that tells Calotype where the key came from;
        pass ``None`` to send none. ``email`` is optional and used only for
        quota notices. The new key is returned, not stored on this client:
        ``Calotype(new.key)`` makes a client that uses it.
        """
        body: dict[str, Any] = {}
        if ref:
            body["ref"] = ref
        if email:
            body["email"] = email
        resp = self._send("POST", "/v1/keys", body, auth=False)
        data = self._json(resp)
        if not isinstance(data.get("key"), str):
            raise CalotypeError("The API's answer held no key.", status=resp.status)
        return NewKey(
            key=data["key"],
            plan=data.get("plan", ""),
            monthly_quota=data.get("monthly_quota", 0),
            message=data.get("message", ""),
        )

    def usage(self) -> Usage:
        """This key's count for the current month."""
        data = self._json(self._send("GET", "/v1/usage", None, auth=True))
        return Usage(
            plan=data.get("plan", ""),
            month=data.get("month", ""),
            used=data.get("used", 0),
            limit=data.get("limit", 0),
            resets_at=data.get("resets_at", ""),
            key_ref=data.get("key_ref", ""),
            email=data.get("email"),
            notices=bool(data.get("notices", False)),
        )

    # --- HTTP -----------------------------------------------------------------------------

    def _send(self, method: str, path: str, body: Mapping[str, Any] | None, *, auth: bool) -> Response:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if auth:
            if not self.api_key:
                raise AuthenticationError(
                    "No API key. Pass one to Calotype(...) or set CALOTYPE_API_KEY. "
                    "Calotype().create_key() makes a free one.",
                    code="missing_key",
                )
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        resp = self._transport(Request(method, self.base_url + path, headers, data, self.timeout))
        if not 200 <= resp.status < 300:
            try:
                error = json.loads(resp.body).get("error")
            except Exception:
                error = None
            raise error_for(resp.status, error, resp.headers)
        return resp

    @staticmethod
    def _json(resp: Response) -> dict[str, Any]:
        try:
            data = json.loads(resp.body)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise CalotypeError("The API answered with something that is not a JSON object.", status=resp.status)
        return data
