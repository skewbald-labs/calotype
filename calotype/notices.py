"""The optional email address on a key, and the quota notices sent to it (running plan H1).

A key needs no address. A holder may give one, when creating the key
(``POST /v1/keys`` with ``{"email": ...}``) or later (``POST /v1/keys/email``),
so that Calotype can say when the key reaches 80% and 100% of its monthly
quota. Nothing else is ever sent to it.

How a notice travels, so the public service never holds the mail key:

1. ``capture_for_key`` in ``app.py`` sees the key's count reach the 80% or the
   100% mark and queues a ``storage.Notice`` (key id, month, threshold,
   counts, the upgrade line). One per key, month and threshold, whatever
   happens later in the month.
2. The skewbald-checks job (free-reign ``ops/checks/main.py``, every 15
   minutes) reads pending notices, looks up the key's address at that moment,
   enforces a cap of ``PER_ADDRESS_MONTHLY_CAP`` notices per address per
   month across all keys, sends through AgentMail, and marks each one sent
   only after AgentMail answers 2xx.
3. Each notice ends with a link to ``GET /v1/keys/email/remove?token=...``.
   The token is an HMAC, under ``CALOTYPE_EMAIL_TOKEN_SECRET``, of the key id
   and the address. The service checks it against the address the key holds
   now, so a link stops working once the address changes or is removed.

Without ``CALOTYPE_EMAIL_TOKEN_SECRET`` (or with one shorter than
``MIN_SECRET_LEN``) the feature is off: no address is stored and nothing is
queued.

The token format is shared with ``ops/checks/main.py`` in the free-reign
repository. Change both together; each repository's tests check the same
fixed example (``tests/test_key_email.py``, ``ops/checks/test_main.py``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

EMAIL_MAX_LEN = 254
MIN_SECRET_LEN = 32
# Enforced by the sender job; here so the docs and the tests name one number.
PER_ADDRESS_MONTHLY_CAP = 4

_TOKEN_CONTEXT = "calotype-email-remove"
_TOKEN = re.compile(r"^([1-9][0-9]{0,17})\.([A-Za-z0-9_-]{43})$")
# One "@", no spaces or characters that make a header or a list of addresses.
_FORBIDDEN = re.compile(r"[\s<>,;:\"()\[\]\\]")


def normalize_email(raw: object) -> str | None:
    """The address as stored (domain lower-cased), or None if it is not plausible.

    A light check, as asked for: one ``@``, something before it, a domain
    with a dot that is not at either end, at most 254 characters, and no
    spaces or characters that could split a header. Whether it reaches
    anyone is not checked.
    """
    if not isinstance(raw, str):
        return None
    email = raw.strip()
    if not email or len(email) > EMAIL_MAX_LEN or email.count("@") != 1:
        return None
    if _FORBIDDEN.search(email) or any(ord(c) < 32 or ord(c) == 127 for c in email):
        return None
    local, domain = email.split("@")
    domain = domain.lower()
    if not local or "." not in domain or domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return None
    return f"{local}@{domain}"


def mask_email(email: str | None) -> str | None:
    """``someone@example.com`` -> ``s***@example.com``: enough to recognise, not to read back."""
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"


def notices_enabled(secret: str) -> bool:
    return len(secret or "") >= MIN_SECRET_LEN


def _mac(secret: str, key_id: int, email: str) -> str:
    message = f"{_TOKEN_CONTEXT}\n{int(key_id)}\n{email}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def email_token(secret: str, key_id: int, email: str) -> str:
    """The removal token for this key and address: ``<key id>.<HMAC-SHA256, base64url>``."""
    return f"{int(key_id)}.{_mac(secret, key_id, email)}"


def token_key_id(token: object) -> int | None:
    """The key id a token names, or None if it is not shaped like a token."""
    m = _TOKEN.match(token) if isinstance(token, str) else None
    return int(m.group(1)) if m else None


def token_matches(secret: str, token: str, key_id: int, email: str | None) -> bool:
    """True only for the token made for exactly this key and its current address."""
    if not email or not notices_enabled(secret) or token_key_id(token) != key_id:
        return False
    return hmac.compare_digest(token.encode("ascii"), email_token(secret, key_id, email).encode("ascii"))


def threshold_count(threshold: int, quota: int) -> int:
    """The count at which ``threshold`` percent of ``quota`` is first reached (rounded up)."""
    return -(-threshold * quota // 100)


def thresholds_at(used: int, quota: int) -> list[int]:
    """The thresholds this exact count reaches first: [80], [100], or [].

    ``try_consume`` hands every successful call its own count, so exactly one
    capture sees each mark. A refund can make a count repeat; the notice's id
    (key, month, threshold) keeps that from queueing twice. With a quota so
    small that 80% rounds up to all of it, only the 100% notice is sent.
    """
    if quota <= 0:
        return []
    hits = []
    eighty = threshold_count(80, quota)
    if used == eighty and eighty < quota:
        hits.append(80)
    if used == quota:
        hits.append(100)
    return hits
