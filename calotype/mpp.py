"""The wire format of the Machine Payments Protocol (MPP), server side.

MPP is the "Payment" HTTP authentication scheme: an unpaid request is answered
``402`` with a challenge in ``WWW-Authenticate``, the client retries with a
credential, and a paid response carries a receipt. This module only builds and
checks those three headers. It knows nothing about Stripe or about Calotype;
``credits.py`` does the charging.

Written from the published protocol, not invented here:

- https://mpp.dev/protocol/challenges , /credentials , /receipts , /http-402
- https://paymentauth.org (the IETF drafts), and for the Stripe method
  https://paymentauth.org/draft-stripe-charge-00
- the two official SDKs, read to settle what the prose leaves open: ``mppx``
  0.21.1 (TypeScript, the one Stripe's guide uses) and ``pympp`` 0.26.0
  (Python). ``tests/test_mpp.py`` checks this module against ``pympp`` when it
  is installed.

The format, in short:

``WWW-Authenticate: Payment id="..", realm="..", method="..", intent="..",
request="<b64>", expires="..", description="..", header="..", opaque="<b64>"``
    ``request`` and ``opaque`` are JSON objects in canonical form (RFC 8785),
    base64url without padding. ``id`` is HMAC-SHA256 over
    ``realm|method|intent|request|expires|digest|[header|]opaque`` (the wire
    strings; an absent ``expires``, ``digest`` or ``opaque`` is an empty
    string; ``header`` has no slot at all when absent), base64url without
    padding. Because the id is a keyed hash of the terms, the server needs no
    record of the challenges it has issued, and a client cannot change the
    amount and keep the id.
``Authorization: Payment <b64>`` (or ``Payment-Authorization: Payment <b64>``
when the challenge said ``header="Payment-Authorization"``)
    base64url JSON ``{"challenge": {...the challenge's parameters, echoed
    unchanged...}, "payload": {...}, "source": "..."}``.
``Payment-Receipt: <b64>``
    base64url JSON ``{"method", "reference", "status": "success", "timestamp"}``.

Errors are RFC 9457 problem details whose ``type`` is
``https://paymentauth.org/problems/<name>``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

PAYMENT_AUTHORIZATION = "Payment-Authorization"
PROBLEM_BASE = "https://paymentauth.org/problems"
PROBLEM_CONTENT_TYPE = "application/problem+json"

# Sizes the official SDKs enforce: a decoded request or opaque object, and a whole credential.
MAX_PARAM_SIZE = 16 * 1024
MAX_CREDENTIAL_SIZE = 64 * 1024
DEFAULT_EXPIRES_S = 5 * 60

_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_METHOD_ID = re.compile(r"^[a-z][a-z0-9:_-]*$")
_SCHEME = re.compile(r"^payment[ \t]+(.+)$", re.IGNORECASE | re.DOTALL)

PROBLEM_TITLES = {
    "payment-required": "Payment Required",
    "malformed-credential": "Malformed Credential",
    "invalid-challenge": "Invalid Challenge",
    "payment-expired": "Payment Expired",
    "verification-failed": "Verification Failed",
    "method-unsupported": "Method Unsupported",
}


class MalformedCredential(Exception):
    """The credential is not base64url JSON of the documented shape. The message is safe to show."""


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    if not _BASE64URL.match(text or ""):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical_json(value: Any) -> bytes:
    """``value`` as RFC 8785 canonical JSON.

    Only what a challenge needs is accepted: objects with string keys, arrays,
    strings, integers, booleans and null. For those, RFC 8785 is exactly
    "sorted keys, no spaces, UTF-8, minimal escapes", which is what
    ``json.dumps`` writes below. Floats are refused, because their canonical
    form is the part of RFC 8785 that ``json`` does not follow.
    """
    _check_canonical(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _check_canonical(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("integers beyond 2^53 have no exact canonical JSON form")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii():
                # RFC 8785 sorts keys by UTF-16 unit; for ASCII that is plain string order.
                raise ValueError("object keys must be ASCII strings")
            _check_canonical(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_canonical(item)
        return
    raise ValueError(f"{type(value).__name__} has no canonical JSON form here")


def encode_object(value: dict[str, Any]) -> str:
    """A JSON object as a ``request`` or ``opaque`` parameter."""
    return b64url(canonical_json(value))


def decode_object(text: str, max_size: int = MAX_PARAM_SIZE) -> dict[str, Any]:
    """A ``request``, ``opaque`` or credential string back to its JSON object. Raises ``ValueError``."""
    if len(text) > max_size:
        raise ValueError("too large")
    try:
        value = json.loads(b64url_decode(text))
    except (ValueError, binascii.Error, RecursionError):
        raise ValueError("not base64url JSON") from None
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def challenge_id(
    secret: str,
    *,
    realm: str,
    method: str,
    intent: str,
    request: str,
    expires: str = "",
    digest: str = "",
    header: str = "",
    opaque: str = "",
) -> str:
    """The id that binds a challenge's parameters. Every argument is the string that goes on the wire."""
    parts = [realm, method, intent, request, expires, digest]
    if header:
        parts.append(header)
    parts.append(opaque)
    mac = hmac.new(secret.encode("utf-8"), "|".join(parts).encode("utf-8"), hashlib.sha256).digest()
    return b64url(mac)


def expires_at(now: datetime | None = None, seconds: int = DEFAULT_EXPIRES_S) -> str:
    return ((now or datetime.now(UTC)) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> datetime | None:
    """An RFC 3339 time, or None if it is not one (or has no zone)."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class Challenge:
    """One challenge as issued. ``request`` and ``opaque`` are the encoded wire strings."""

    id: str
    realm: str
    method: str
    intent: str
    request: str
    expires: str = ""
    description: str = ""
    header: str = ""  # "" means the credential goes in Authorization
    opaque: str = ""

    def www_authenticate(self) -> str:
        """The ``WWW-Authenticate`` value, parameters in the order the SDKs write them."""
        parts = [
            f'id="{_quoted(self.id)}"',
            f'realm="{_quoted(self.realm)}"',
            f'method="{_quoted(self.method)}"',
            f'intent="{_quoted(self.intent)}"',
            f'request="{self.request}"',
        ]
        if self.expires:
            parts.append(f'expires="{_quoted(self.expires)}"')
        if self.description:
            parts.append(f'description="{_quoted(self.description)}"')
        if self.header:
            parts.append(f'header="{_quoted(self.header)}"')
        if self.opaque:
            parts.append(f'opaque="{self.opaque}"')
        return "Payment " + ", ".join(parts)


def issue_challenge(
    secret: str,
    *,
    realm: str,
    method: str,
    intent: str,
    request: dict[str, Any],
    opaque: dict[str, str] | None = None,
    description: str = "",
    header: str = "",
    expires: str | None = None,
) -> Challenge:
    """A new challenge for ``request``.

    ``opaque`` is the server's own data, echoed back by the client and bound
    into the id like everything else. A random ``nonce`` is added to it, so two
    challenges for the same terms never share an id (the SDKs do the same).
    ``header`` is ``""`` or ``Payment-Authorization``, the one alternative the
    protocol allows.
    """
    if header and header != PAYMENT_AUTHORIZATION:
        raise ValueError("a challenge may only select Payment-Authorization")
    if not _METHOD_ID.match(method):
        raise ValueError("not a payment method id")
    request_b64 = encode_object(request)
    opaque_b64 = encode_object({**(opaque or {}), "nonce": secrets.token_hex(16)})
    expires = expires if expires is not None else expires_at()
    return Challenge(
        id=challenge_id(
            secret,
            realm=realm,
            method=method,
            intent=intent,
            request=request_b64,
            expires=expires,
            header=header,
            opaque=opaque_b64,
        ),
        realm=realm,
        method=method,
        intent=intent,
        request=request_b64,
        expires=expires,
        description=description,
        header=header,
        opaque=opaque_b64,
    )


@dataclass(frozen=True)
class Credential:
    """A parsed credential: the echoed challenge (wire strings) and the method's payload."""

    id: str
    realm: str
    method: str
    intent: str
    request: str
    expires: str
    digest: str
    header: str
    opaque: str
    payload: dict[str, Any]
    source: str = ""

    def issued_with(self, secret: str) -> bool:
        """True if this server issued the echoed challenge and no parameter was changed since."""
        expected = challenge_id(
            secret,
            realm=self.realm,
            method=self.method,
            intent=self.intent,
            request=self.request,
            expires=self.expires,
            digest=self.digest,
            header=self.header,
            opaque=self.opaque,
        )
        return hmac.compare_digest(expected.encode("utf-8"), self.id.encode("utf-8"))


def payment_credential(header_value: str | None) -> str | None:
    """The part after ``Payment`` in a header value, or None if the header is not a Payment credential."""
    if not header_value:
        return None
    match = _SCHEME.match(header_value.strip())
    return match.group(1).strip() if match else None


def parse_credential(header_value: str) -> Credential:
    """Parse ``Payment <base64url JSON>``. Raises ``MalformedCredential``."""
    if len(header_value) > MAX_CREDENTIAL_SIZE:
        raise MalformedCredential("The credential is too large.")
    token = payment_credential(header_value)
    if token is None:
        raise MalformedCredential("Expected 'Payment <credential>'.")
    try:
        data = decode_object(token, MAX_CREDENTIAL_SIZE)
    except ValueError:
        raise MalformedCredential("The credential is not base64url-encoded JSON.") from None
    echo, payload = data.get("challenge"), data.get("payload")
    if not isinstance(echo, dict) or not isinstance(payload, dict):
        raise MalformedCredential("The credential needs a 'challenge' object and a 'payload' object.")

    def text(name: str, *, required: bool = False) -> str:
        value = echo.get(name)
        if value is None and not required:
            return ""
        if not isinstance(value, str) or (required and not value):
            raise MalformedCredential(f"The credential's challenge.{name} must be a string.")
        return value

    header = text("header")
    if "header" in echo and header.lower() != PAYMENT_AUTHORIZATION.lower():
        raise MalformedCredential("The credential's challenge.header may only be Payment-Authorization.")
    source = data.get("source")
    return Credential(
        id=text("id", required=True),
        realm=text("realm", required=True),
        method=text("method", required=True),
        intent=text("intent", required=True),
        request=text("request", required=True),
        expires=text("expires"),
        digest=text("digest"),
        header=header,
        opaque=text("opaque"),
        payload=payload,
        source=source if isinstance(source, str) else "",
    )


def receipt(method: str, reference: str, *, timestamp: datetime | None = None, external_id: str = "") -> str:
    """A ``Payment-Receipt`` header value for a payment that succeeded."""
    body: dict[str, Any] = {
        "method": method,
        "reference": reference,
        "status": "success",
        "timestamp": (timestamp or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if external_id:
        body["externalId"] = external_id
    return encode_object(body)


def problem(name: str, detail: str, status: int = 402, **extra: Any) -> dict[str, Any]:
    """An RFC 9457 problem details body of one of the protocol's types."""
    return {
        "type": f"{PROBLEM_BASE}/{name}",
        "title": PROBLEM_TITLES.get(name, "Payment Error"),
        "status": status,
        "detail": detail,
        **extra,
    }


def _quoted(text: str) -> str:
    """``text`` for the inside of a quoted-string. Only printable ASCII is ever written here."""
    if not text.isascii() or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ValueError("header parameters must be printable ASCII")
    return text.replace("\\", "\\\\").replace('"', '\\"')
