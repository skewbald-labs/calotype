"""API keys and usage counts.

``Storage`` is the interface the app uses. ``SQLiteStorage`` implements it
for a single instance. ``FirestoreStorage`` (``firestore_storage.py``)
implements it for Cloud Run, where the disk is lost on restart and not shared
between instances. ``CALOTYPE_STORAGE`` picks one; see ``open_configured_storage``.

What is stored, and nothing else: the SHA-256 hash of each key (never the
key), its plan, its creation time, whether it is Calotype's own
(``internal``: made by an operator, or a test key an operator made through
the website and marked with ``set_internal``), an optional operator label, usage counts, a revocation time,
for paid keys the Stripe customer id and subscription id, and, only if the
holder gave one, an email address for quota notices (``email``). Separately,
the ids of Stripe webhook events already handled, so a repeated delivery is
not applied twice, and the quota notices queued for keys with an address
(``Notice``: key id, month, threshold, counts; never the address). For prepaid
credit (``credits.py``): each key's credit balance, and one record per
purchase with the Stripe PaymentIntent id (a card) or the Tempo transaction
hash and its PaymentIntent (a stablecoin), the pack, the amount and the times
it was made and refunded. For stablecoin payments also one payment claim per
transaction (``claim_payment``: the transaction hash and the challenge that
spent it, no key id), kept so that one transfer pays once. A transaction hash
is public on the chain and names the paying wallet there. No name, IP address
or other personal data.

The email address is used for one thing: telling the holder when the key
reaches 80% and 100% of its monthly quota (``notices.py``). The service only
queues a notice; a separate job sends it and marks it sent. Removing the
address (the holder's ``DELETE /v1/keys/email``, the link in each notice, or
``admin remove-email``), revoking the key, or deleting it clears the address.

``delete_key`` removes a key's record, its usage counts, its notices, its
credit balance and its purchase records for good, on the key holder's request
(see the privacy notice).

Prepaid credit is a second, smaller counter next to the monthly one. A
screenshot is counted against the month's plan quota first (``try_consume``);
only when that is used up does the app take one credit (``consume_credit``).
``add_credit_purchase`` records a purchase and adds its screenshots in one
atomic step, keyed by the PaymentIntent id (a card) or the transaction hash (a
stablecoin), so the same payment arriving twice (a retried request, a replayed
webhook) adds credit once.

Payment claims (``claim_payment``) are the replay record the Tempo method's
SDK keeps (``tempo.py``): a claim is taken atomically by one owner, the MPP
challenge being paid, and a second owner is refused. The same owner taking it
again succeeds, so a buyer resending the same credential after a failure is
checked again instead of being told the payment was used. Claims never expire.

The free tries on the home page (``tries.py``) are counted per UTC day
(``TryDay``): how many there were and how each ended. Never the URL, the host
or the IP address. ``try_admit`` also enforces the daily cap on renders, for
every instance at once.

The funnel (``funnel.py``) is counted the same way, per UTC day and per step
(``FunnelDay``): how often an agent looked at the MCP tool without a key,
called it without one, sent a wrong key, probed for OAuth, looked for a key
page that does not exist, was shown a help page in a browser, made a key
(by the API or the site's button), and took a key's first screenshot.
Counts only: never a URL, a key, a user agent or an IP address.
OAuth sign-in on ``/mcp`` (``oauth.py``) stores one short-lived record per
authorization code (``OAuthCode``): the SHA-256 hash of the code (never the
code), a hash of the client id, the redirect address, the PKCE challenge, the
resource, and the API key sealed with a pad derived from the code, so the
record alone gives no usable key. A code is taken once (``take_oauth_code``
deletes it) and lives a minute; expired records are deleted whenever a new
code is saved. Client registrations are not stored at all.

The "contact ordinal" for the demand test is derived, not stored: it is the
creation order of non-internal keys that have made at least one successful
screenshot (see ``list_contacts``).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import Settings

KEY_PREFIX = "cal_"


@dataclass(frozen=True)
class ApiKey:
    id: int
    plan: str
    label: str
    created_at: str
    revoked: bool
    internal: bool = False
    successful_screenshots: int = 0
    # Set by the Stripe webhook once a key has paid. Ids only; Stripe holds the rest.
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    # Optional, for quota notices only. Kept out of repr so it never lands in a log line.
    email: str | None = field(default=None, repr=False)


# A notice goes out at most once per key, month and threshold (percent of the quota).
NOTICE_THRESHOLDS = (80, 100)
NOTICE_PENDING = "pending"


@dataclass(frozen=True)
class Notice:
    """A quota notice queued by the service for the sender job (see ``notices.py``).

    It holds what the email says, never the address: the job reads the
    address from the key's record when it sends, so an address removed in
    between is never written to.
    """

    key_id: int
    month: str  # YYYY-MM, UTC
    threshold: int  # 80 or 100
    used: int
    quota: int
    plan: str
    key_ref: str
    resets_at: str
    upgrade: str  # the same line the MCP tool adds near the limit (app.upgrade_text)
    origin: str  # where the removal link points
    status: str = NOTICE_PENDING  # pending, then sent, dropped, capped or failed (set by the job)
    created_at: str = ""
    sent_at: str | None = None

    @property
    def id(self) -> str:
        return notice_id(self.key_id, self.month, self.threshold)


def notice_id(key_id: int, month: str, threshold: int) -> str:
    return f"{int(key_id)}_{month}_{int(threshold)}"


@dataclass(frozen=True)
class Contact:
    ordinal: int  # 1 = the earliest-created non-internal key with a successful screenshot
    key_id: int
    plan: str
    created_at: str
    successful_screenshots: int
    source: str = ""  # the ?ref= tag the key was made with, e.g. "dt-linkedin"; "" if none


@dataclass(frozen=True)
class CreditPurchase:
    """One prepaid pack bought with one payment."""

    # The payment's own id, so the same payment is never recorded twice: the Stripe
    # PaymentIntent id for a card, the Tempo transaction hash (0x...) for a stablecoin.
    id: str
    key_id: int
    pack: str
    screenshots: int
    amount_cents: int
    currency: str
    challenge_id: str  # the MPP challenge that was paid; "" if recorded from a webhook alone
    method: str  # the MPP payment method: "stripe" (a card) or "tempo" (USDC on Tempo)
    created_at: str
    refunded_at: str | None = None
    credits_removed: int = 0  # unused credit taken back when it was refunded
    refund_id: str | None = None  # the Stripe Refund id, once Stripe has made the refund
    # The PaymentIntent that records a Tempo payment in Stripe, once Stripe has one. None for a
    # card, whose id is its PaymentIntent already. ``stripe_payment_intent`` covers both.
    payment_intent: str | None = None
    # Where the payment is (PURCHASE_STATUSES). Only "credited" has added screenshots to the
    # balance. A card purchase is recorded once its payment has succeeded, so it is always
    # "credited"; a Tempo purchase goes pending -> paid -> credited (or failed, or unmatched).
    status: str = "credited"
    # A Tempo purchase's immutable terms, as JSON: the MPP request the transfer was checked
    # against (amount in base units, token, recipient, chain), the realm and the challenge id.
    tempo_terms: str | None = None
    # A pull-mode transfer's valid_before (Unix seconds): after it, a transfer that is not on the
    # chain can never be, which is what turns "pending" into "failed". None for push mode.
    valid_before: int | None = None
    # Recording in Stripe (Tempo): the idempotency key's attempt number, and, after an answer
    # that leaves the outcome unknown, the time from which Stripe is searched before trying again.
    stripe_attempt: int = 0
    stripe_retry_at: str | None = None
    # A refund: the amount fixed the first time it was asked for, and Stripe's last word on it.
    refund_amount_cents: int | None = None
    refund_status: str | None = None

    @property
    def stripe_payment_intent(self) -> str | None:
        """The PaymentIntent to refund, or None if Stripe holds none for this payment."""
        if self.payment_intent:
            return self.payment_intent
        return self.id if self.id.startswith("pi_") else None

    @property
    def credited(self) -> bool:
        return self.status == PURCHASE_CREDITED


# A purchase's ``status``. The balance holds a purchase's screenshots only once it is "credited".
PURCHASE_PENDING = "pending"  # Tempo: bound to its challenge; the chain has not confirmed it yet
PURCHASE_PAID = "paid"  # Tempo: on the chain; Stripe has not yet matched it to a settled payment
PURCHASE_CREDITED = "credited"  # the screenshots are in the balance
PURCHASE_FAILED = "failed"  # Tempo: reverted, or can no longer be included. Nothing moved
PURCHASE_UNMATCHED = "unmatched"  # Tempo: on the chain but not creditable (a second transfer for a challenge)
PURCHASE_STATUSES = (PURCHASE_PENDING, PURCHASE_PAID, PURCHASE_CREDITED, PURCHASE_FAILED, PURCHASE_UNMATCHED)

# The fields ``update_credit_purchase`` may change. Every other field is fixed when the purchase is made.
PURCHASE_MUTABLE_FIELDS = frozenset(
    {"status", "payment_intent", "stripe_attempt", "stripe_retry_at", "refund_id", "refund_status"}
)


def refund_amount(purchase: CreditPurchase, removed: int, *, prorated: bool) -> int:
    """The cents to refund for ``purchase`` when ``removed`` of its screenshots were taken back.

    All of it, unless ``prorated``: then the share of the price that was
    unused. A purchase never credited gave nothing to use, so all of it goes back.
    """
    if not purchase.credited or not prorated:
        return purchase.amount_cents
    return purchase.amount_cents * removed // purchase.screenshots


# The claim that binds a Tempo challenge to the one transaction that pays it (``bind_tempo_payment``).
TEMPO_CHALLENGE_CLAIM = "calotype:tempo-challenge:"

# What ``bind_tempo_payment`` found.
BOUND = "bound"  # this call bound the challenge and made the purchase
BOUND_ALREADY = "same"  # the challenge was already bound to this transaction
CHALLENGE_TAKEN = "challenge_taken"  # the challenge is bound to another transaction
TRANSACTION_TAKEN = "transaction_taken"  # the transaction is recorded for something else


# A key made from a link tagged ?ref=<tag> is labelled "ref:<tag>", so the
# demand test can tell which post or channel brought it. The tag is a short
# lower-case word chosen by us, never anything about the person.
REF_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
REF_LABEL_PREFIX = "ref:"


def source_from_label(label: str) -> str:
    return label[len(REF_LABEL_PREFIX):] if label.startswith(REF_LABEL_PREFIX) else ""


# How a free try ended. ``total`` counts every try; each try also adds one to
# exactly one of these. ``rendered`` (tries let through to the browser, the
# count the daily cap applies to) is separate: it is taken before the capture.
TRY_OUTCOMES = ("succeeded", "failed", "refused", "limited", "capped")


@dataclass(frozen=True)
class TryDay:
    """One UTC day's free tries: counts only, never a URL, host or IP address."""

    day: str  # YYYY-MM-DD, UTC
    total: int = 0
    rendered: int = 0  # let through to the browser, against the daily cap
    succeeded: int = 0  # an image was returned
    failed: int = 0  # the capture ran and failed (timeout, unreachable, busy...)
    refused: int = 0  # not a valid URL, or an address the policy refuses
    limited: int = 0  # over the per-IP limit
    capped: int = 0  # the day's cap was already reached


@dataclass(frozen=True)
class FunnelDay:
    """One UTC day's funnel counts (``funnel.py``): counts only, never a URL, key, user agent or IP."""

    day: str  # YYYY-MM-DD, UTC
    looked: int = 0  # initialize or tools/list on /mcp with no key
    tool_no_key: int = 0  # tools/call on /mcp with no key: told how to get one
    bad_key: int = 0  # 401 on /v1/screenshot or /mcp for a key that was sent but is wrong or revoked
    oauth_probe: int = 0  # a request for OAuth metadata or an OAuth step, whatever the answer
    key_miss: int = 0  # a 404 or 405 on a path where a key or a sign-up might be (funnel.KEY_MISS_PATHS)
    api_page: int = 0  # a browser on GET /mcp, /v1/screenshot or /v1/keys was shown a help page (keypages.py)
    key_refused: int = 0  # POST /v1/keys or the site's key button refused: rate limited, bad email, switched off
    key_made: int = 0  # POST /v1/keys or the site's key button made a key
    web_key: int = 0  # the site's "Get a free key" button made a key (also counted in key_made)
    first_capture: int = 0  # a non-internal key's first successful screenshot


# The funnel's steps, in order. Each is a FunnelDay field and the name stored for it.
FUNNEL_EVENTS = tuple(f.name for f in fields(FunnelDay) if f.name != "day")

@dataclass(frozen=True)
class OAuthCode:
    """One OAuth authorization code, waiting to be exchanged for its token (``oauth.py``).

    Single use and short-lived. Holds no usable secret: ``id`` is the SHA-256
    of the code, and ``sealed_key`` is the API key XORed with a pad that only
    the code itself yields.
    """

    id: str  # SHA-256 hex of the code
    client_hash: str  # SHA-256 hex of the client id it was issued to
    redirect_uri: str
    code_challenge: str  # PKCE, S256
    resource: str  # the RFC 8707 resource asked for, "" if none
    sealed_key: str
    expires_at: int  # Unix seconds
    created_at: int = 0


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# A key's reference: the first 12 hex characters of its stored hash. It is shown to the
# key's holder by /v1/usage so they can name their key to support without sending it.
# It reveals nothing usable about the key, and support finds the key by it (admin find-ref).
KEY_REF_LEN = 12
KEY_REF = re.compile(r"^[0-9a-f]{12}$")


def key_ref(raw_key: str) -> str:
    return hash_key(raw_key)[:KEY_REF_LEN]


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def current_month(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


def current_day(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m-%d")


class Storage(Protocol):
    def init(self) -> None: ...
    def create_key(
        self, plan: str, label: str = "", *, internal: bool = False, email: str | None = None
    ) -> tuple[str, ApiKey]: ...
    def get_key(self, raw_key: str) -> ApiKey | None: ...
    def list_keys(self) -> list[ApiKey]: ...
    def revoke_key(self, key_id: int) -> bool: ...
    def delete_key(self, key_id: int) -> bool: ...
    def set_plan(self, key_id: int, plan: str) -> bool: ...
    def set_internal(self, key_id: int, internal: bool = True) -> bool: ...
    def tag_source(self, key_id: int, tag: str) -> bool: ...
    def get_key_by_id(self, key_id: int) -> ApiKey | None: ...
    def find_key_by_subscription(self, subscription_id: str) -> ApiKey | None: ...
    def find_key_by_ref(self, ref: str) -> ApiKey | None: ...
    def set_billing(self, key_id: int, plan: str, *, customer_id: str, subscription_id: str) -> bool: ...
    def event_processed(self, event_id: str) -> bool: ...
    def mark_event_processed(self, event_id: str) -> bool: ...
    def try_consume(self, key_id: int, month: str, limit: int) -> tuple[bool, int]: ...
    def refund(self, key_id: int, month: str) -> None: ...
    def get_usage(self, key_id: int, month: str) -> int: ...
    def record_success(self, key_id: int) -> int: ...
    def list_contacts(self) -> list[Contact]: ...
    def contact_ordinal(self, key_id: int) -> int | None: ...
    def get_credit_balance(self, key_id: int) -> int: ...
    def consume_credit(self, key_id: int) -> tuple[bool, int]: ...
    def return_credit(self, key_id: int) -> None: ...
    def add_credit_purchase(
        self,
        purchase_id: str,
        key_id: int,
        *,
        pack: str,
        screenshots: int,
        amount_cents: int,
        currency: str,
        challenge_id: str = "",
        method: str = "stripe",
        payment_intent: str | None = None,
        status: str = PURCHASE_CREDITED,
        tempo_terms: str | None = None,
    ) -> tuple[bool, int]: ...
    def bind_tempo_payment(
        self,
        purchase_id: str,
        key_id: int,
        *,
        pack: str,
        screenshots: int,
        amount_cents: int,
        currency: str,
        challenge_id: str,
        status: str,
        tempo_terms: str,
        valid_before: int | None = None,
    ) -> tuple[str, CreditPurchase | None]: ...
    def update_credit_purchase(
        self, purchase_id: str, changes: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> CreditPurchase | None: ...
    def credit_tempo_purchase(self, purchase_id: str, payment_intent: str) -> tuple[bool, int]: ...
    def get_credit_purchase(self, purchase_id: str) -> CreditPurchase | None: ...
    def find_credit_purchase_by_challenge(self, challenge_id: str) -> CreditPurchase | None: ...
    def list_credit_purchases(self, key_id: int) -> list[CreditPurchase]: ...
    def refund_credit_purchase(
        self, purchase_id: str, *, prorated: bool = False
    ) -> tuple[CreditPurchase, bool] | None: ...
    def set_credit_refund_id(self, purchase_id: str, refund_id: str) -> bool: ...
    def set_credit_payment_intent(
        self, purchase_id: str, payment_intent: str, *, replacing: str | None = None
    ) -> bool: ...
    def claim_payment(self, claim: str, owner: str, value: str) -> bool: ...
    def get_payment_claim(self, claim: str) -> tuple[str, str] | None: ...
    def release_payment_claim(self, claim: str, owner: str) -> None: ...
    def set_email(self, key_id: int, email: str | None) -> bool: ...
    def keys_with_email(self, email: str) -> list[int]: ...
    def queue_notice(self, notice: Notice) -> bool: ...
    def list_notices(self, key_id: int) -> list[Notice]: ...
    def try_admit(self, day: str, limit: int) -> tuple[bool, int]: ...
    def record_try(self, day: str, outcome: str) -> None: ...
    def try_days(self, days: list[str]) -> list[TryDay]: ...
    def record_funnel(self, day: str, event: str) -> None: ...
    def funnel_days(self, days: list[str]) -> list[FunnelDay]: ...
    def save_oauth_code(self, code: OAuthCode) -> None: ...
    def take_oauth_code(self, code_id: str, now: int | None = None) -> OAuthCode | None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    internal INTEGER NOT NULL DEFAULT 0,
    successful_screenshots INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    email TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    month TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, month)
);
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credits (
    key_id INTEGER PRIMARY KEY REFERENCES api_keys(id),
    balance INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS credit_purchases (
    id TEXT PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    pack TEXT NOT NULL,
    screenshots INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    challenge_id TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL DEFAULT 'stripe',
    created_at TEXT NOT NULL,
    refunded_at TEXT,
    credits_removed INTEGER NOT NULL DEFAULT 0,
    refund_id TEXT,
    payment_intent TEXT,
    status TEXT NOT NULL DEFAULT 'credited',
    tempo_terms TEXT,
    valid_before INTEGER,
    stripe_attempt INTEGER NOT NULL DEFAULT 0,
    stripe_retry_at TEXT,
    refund_amount_cents INTEGER,
    refund_status TEXT
);
CREATE TABLE IF NOT EXISTS payment_claims (
    claim TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id TEXT PRIMARY KEY,
    key_id INTEGER NOT NULL REFERENCES api_keys(id),
    month TEXT NOT NULL,
    threshold INTEGER NOT NULL,
    used INTEGER NOT NULL,
    quota INTEGER NOT NULL,
    plan TEXT NOT NULL,
    key_ref TEXT NOT NULL,
    resets_at TEXT NOT NULL,
    upgrade TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    to_hash TEXT
);
CREATE TABLE IF NOT EXISTS tries (
    day TEXT PRIMARY KEY,
    total INTEGER NOT NULL DEFAULT 0,
    rendered INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    refused INTEGER NOT NULL DEFAULT 0,
    limited INTEGER NOT NULL DEFAULT 0,
    capped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS funnel (
    day TEXT NOT NULL,
    event TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, event)
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    id TEXT PRIMARY KEY,
    client_hash TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    resource TEXT NOT NULL,
    sealed_key TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# Columns added after the first release. ``init`` adds any that an older
# database lacks, so an existing file keeps working.
_ADDED_KEY_COLUMNS = {
    "stripe_customer_id": "TEXT",
    "stripe_subscription_id": "TEXT",
    "email": "TEXT",
}
_ADDED_PURCHASE_COLUMNS = {
    "payment_intent": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'credited'",
    "tempo_terms": "TEXT",
    "valid_before": "INTEGER",
    "stripe_attempt": "INTEGER NOT NULL DEFAULT 0",
    "stripe_retry_at": "TEXT",
    "refund_amount_cents": "INTEGER",
    "refund_status": "TEXT",
}
_INDEXES = """
CREATE INDEX IF NOT EXISTS api_keys_subscription ON api_keys (stripe_subscription_id);
CREATE INDEX IF NOT EXISTS credit_purchases_key ON credit_purchases (key_id);
CREATE INDEX IF NOT EXISTS credit_purchases_challenge ON credit_purchases (challenge_id);
CREATE INDEX IF NOT EXISTS api_keys_email ON api_keys (email);
CREATE INDEX IF NOT EXISTS notices_key ON notices (key_id);
CREATE INDEX IF NOT EXISTS oauth_codes_expiry ON oauth_codes (expires_at);
"""


class SQLiteStorage:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._conn = conn
        return self._conn

    def init(self) -> None:
        with self._lock:
            db = self._db()
            db.executescript(SCHEMA)
            have = {row[1] for row in db.execute("PRAGMA table_info(api_keys)")}
            for column, kind in _ADDED_KEY_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {kind}")
            have = {row[1] for row in db.execute("PRAGMA table_info(credit_purchases)")}
            for column, kind in _ADDED_PURCHASE_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE credit_purchases ADD COLUMN {column} {kind}")
            db.executescript(_INDEXES)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def create_key(
        self, plan: str, label: str = "", *, internal: bool = False, email: str | None = None
    ) -> tuple[str, ApiKey]:
        raw = generate_key()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._db().execute(
                "INSERT INTO api_keys (key_hash, plan, label, created_at, internal, email) VALUES (?, ?, ?, ?, ?, ?)",
                (hash_key(raw), plan, label, now, 1 if internal else 0, email),
            )
            key_id = int(cur.lastrowid)
        return raw, ApiKey(key_id, plan, label, now, False, internal, 0, email=email)

    def get_key(self, raw_key: str) -> ApiKey | None:
        with self._lock:
            row = self._db().execute(
                f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE key_hash = ?",
                (hash_key(raw_key),),
            ).fetchone()
        return _row_to_key(row) if row else None

    def list_keys(self) -> list[ApiKey]:
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_KEY_COLUMNS} FROM api_keys ORDER BY id"
            ).fetchall()
        return [_row_to_key(r) for r in rows]

    def revoke_key(self, key_id: int) -> bool:
        """Revoke the key and forget its email address. False if it was already revoked."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._db().execute(
                "UPDATE api_keys SET revoked_at = ?, email = NULL WHERE id = ? AND revoked_at IS NULL", (now, key_id)
            )
        return cur.rowcount == 1

    def delete_key(self, key_id: int) -> bool:
        """Delete the key's record (with its email address), its usage counts, its notices,
        its credit balance and its purchase records.

        False if there was no such key.

        Ids are never handed out again (``AUTOINCREMENT``), so a late Stripe
        event for a deleted key finds no key rather than another one.
        """
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM usage WHERE key_id = ?", (key_id,))
                db.execute("DELETE FROM credits WHERE key_id = ?", (key_id,))
                db.execute("DELETE FROM credit_purchases WHERE key_id = ?", (key_id,))
                db.execute("DELETE FROM notices WHERE key_id = ?", (key_id,))
                cur = db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return cur.rowcount == 1

    def set_plan(self, key_id: int, plan: str) -> bool:
        with self._lock:
            cur = self._db().execute("UPDATE api_keys SET plan = ? WHERE id = ?", (plan, key_id))
        return cur.rowcount == 1

    def set_internal(self, key_id: int, internal: bool = True) -> bool:
        """Mark a key as Calotype's own (or not). Internal keys are never demand-test contacts."""
        with self._lock:
            cur = self._db().execute("UPDATE api_keys SET internal = ? WHERE id = ?", (1 if internal else 0, key_id))
        return cur.rowcount == 1

    def tag_source(self, key_id: int, tag: str) -> bool:
        """Record the ``?ref=`` tag a key was first used with, if it has no label yet.

        Only an outside key with an empty label is tagged, so a tag is never
        overwritten and an internal key never gets one. False if nothing changed.
        """
        if not REF_TAG.match(tag):
            return False
        with self._lock:
            cur = self._db().execute(
                "UPDATE api_keys SET label = ? WHERE id = ? AND label = '' AND internal = 0",
                (REF_LABEL_PREFIX + tag, key_id),
            )
        return cur.rowcount == 1

    def get_key_by_id(self, key_id: int) -> ApiKey | None:
        with self._lock:
            row = self._db().execute(f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return _row_to_key(row) if row else None

    def find_key_by_subscription(self, subscription_id: str) -> ApiKey | None:
        with self._lock:
            row = self._db().execute(
                f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE stripe_subscription_id = ? ORDER BY id LIMIT 1",
                (subscription_id,),
            ).fetchone()
        return _row_to_key(row) if row else None

    def find_key_by_ref(self, ref: str) -> ApiKey | None:
        """The one key whose reference is ``ref``; None if none, or (never in practice) more than one."""
        if not KEY_REF.match(ref or ""):
            return None
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_KEY_COLUMNS} FROM api_keys WHERE substr(key_hash, 1, ?) = ? LIMIT 2",
                (KEY_REF_LEN, ref),
            ).fetchall()
        return _row_to_key(rows[0]) if len(rows) == 1 else None

    def set_billing(self, key_id: int, plan: str, *, customer_id: str, subscription_id: str) -> bool:
        """Set the plan and the Stripe ids together, as one write."""
        with self._lock:
            cur = self._db().execute(
                "UPDATE api_keys SET plan = ?, stripe_customer_id = ?, stripe_subscription_id = ? WHERE id = ?",
                (plan, customer_id, subscription_id, key_id),
            )
        return cur.rowcount == 1

    def event_processed(self, event_id: str) -> bool:
        with self._lock:
            row = self._db().execute("SELECT 1 FROM stripe_events WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None

    def mark_event_processed(self, event_id: str) -> bool:
        """Record a handled webhook event. False if it was already recorded."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._db().execute(
                "INSERT INTO stripe_events (event_id, processed_at) VALUES (?, ?) ON CONFLICT (event_id) DO NOTHING",
                (event_id, now),
            )
        return cur.rowcount == 1

    def try_consume(self, key_id: int, month: str, limit: int) -> tuple[bool, int]:
        """Atomically add one to this month's count if it is under ``limit``.

        Returns (allowed, count after the call).
        """
        with self._lock:
            if limit <= 0:
                return False, self._usage_locked(key_id, month)
            row = self._db().execute(
                "INSERT INTO usage (key_id, month, count) VALUES (?, ?, 1) "
                "ON CONFLICT (key_id, month) DO UPDATE SET count = count + 1 WHERE usage.count < ? "
                "RETURNING count",
                (key_id, month, limit),
            ).fetchone()
            if row is None:
                return False, self._usage_locked(key_id, month)
            return True, int(row[0])

    def refund(self, key_id: int, month: str) -> None:
        with self._lock:
            self._db().execute(
                "UPDATE usage SET count = count - 1 WHERE key_id = ? AND month = ? AND count > 0", (key_id, month)
            )

    def get_usage(self, key_id: int, month: str) -> int:
        with self._lock:
            return self._usage_locked(key_id, month)

    def record_success(self, key_id: int) -> int:
        """Count one successful screenshot for the key's lifetime. Returns the new total."""
        with self._lock:
            row = self._db().execute(
                "UPDATE api_keys SET successful_screenshots = successful_screenshots + 1 WHERE id = ? "
                "RETURNING successful_screenshots",
                (key_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def list_contacts(self) -> list[Contact]:
        """Non-internal keys with at least one successful screenshot, in creation order.

        The demand test counts the first 20 of these. Keys made with the admin
        CLI are internal and never appear, whatever they do.
        """
        with self._lock:
            rows = self._db().execute(
                "SELECT id, plan, created_at, successful_screenshots, label FROM api_keys "
                "WHERE internal = 0 AND successful_screenshots > 0 ORDER BY id"
            ).fetchall()
        return [
            Contact(i + 1, int(r[0]), r[1], r[2], int(r[3]), source_from_label(r[4] or ""))
            for i, r in enumerate(rows)
        ]

    def contact_ordinal(self, key_id: int) -> int | None:
        for c in self.list_contacts():
            if c.key_id == key_id:
                return c.ordinal
        return None

    # --- prepaid credit ------------------------------------------------------------------

    def get_credit_balance(self, key_id: int) -> int:
        with self._lock:
            return self._credit_locked(key_id)

    def consume_credit(self, key_id: int) -> tuple[bool, int]:
        """Atomically take one credit if the key has any. Returns (taken, balance after the call)."""
        with self._lock:
            row = self._db().execute(
                "UPDATE credits SET balance = balance - 1 WHERE key_id = ? AND balance > 0 RETURNING balance",
                (key_id,),
            ).fetchone()
            if row is None:
                return False, self._credit_locked(key_id)
            return True, int(row[0])

    def return_credit(self, key_id: int) -> None:
        """Give back the credit taken for a screenshot that then failed."""
        with self._lock:
            self._db().execute("UPDATE credits SET balance = balance + 1 WHERE key_id = ?", (key_id,))

    def add_credit_purchase(
        self,
        purchase_id: str,
        key_id: int,
        *,
        pack: str,
        screenshots: int,
        amount_cents: int,
        currency: str,
        challenge_id: str = "",
        method: str = "stripe",
        payment_intent: str | None = None,
        status: str = PURCHASE_CREDITED,
        tempo_terms: str | None = None,
    ) -> tuple[bool, int]:
        """Record a paid pack and add its screenshots, once per ``purchase_id``.

        Returns (added, balance after the call). ``added`` is False, and
        nothing changes, if this purchase was recorded before or the key does
        not exist. A purchase recorded with another ``status`` than
        "credited" adds nothing to the balance.
        """
        if screenshots <= 0:
            raise ValueError("a purchase adds at least one screenshot")
        if status not in PURCHASE_STATUSES:
            raise ValueError(f"unknown purchase status {status!r}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                added = False
                if db.execute("SELECT 1 FROM api_keys WHERE id = ?", (key_id,)).fetchone() is not None:
                    added = self._insert_purchase_locked(
                        purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method, now,
                        payment_intent, status, tempo_terms, None,
                    )
                    if added and status == PURCHASE_CREDITED:
                        self._add_credit_locked(key_id, screenshots)
                balance = self._credit_locked(key_id)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return added, balance

    def bind_tempo_payment(
        self,
        purchase_id: str,
        key_id: int,
        *,
        pack: str,
        screenshots: int,
        amount_cents: int,
        currency: str,
        challenge_id: str,
        status: str,
        tempo_terms: str,
        valid_before: int | None = None,
    ) -> tuple[str, CreditPurchase | None]:
        """Bind a Tempo challenge to the one transaction that pays it, and record that purchase, as one step.

        Nothing is credited: the purchase starts ``status`` ("pending" or
        "paid"). Returns what was found: ``BOUND`` (done now), ``BOUND_ALREADY``
        (the same pair, from an earlier call), ``CHALLENGE_TAKEN`` (the
        challenge is bound to another transaction, whose purchase is returned)
        or ``TRANSACTION_TAKEN`` (the transaction is recorded for something
        else, returned). ``(TRANSACTION_TAKEN, None)`` if the key does not exist.
        """
        if screenshots <= 0 or not challenge_id:
            raise ValueError("a Tempo purchase needs screenshots and a challenge")
        if status not in (PURCHASE_PENDING, PURCHASE_PAID):
            raise ValueError("a bound purchase starts pending or paid")
        claim, now = TEMPO_CHALLENGE_CLAIM + challenge_id, datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                found = db.execute("SELECT owner FROM payment_claims WHERE claim = ?", (claim,)).fetchone()
                existing = db.execute(
                    f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)
                ).fetchone()
                if db.execute("SELECT 1 FROM api_keys WHERE id = ?", (key_id,)).fetchone() is None:
                    outcome, row = TRANSACTION_TAKEN, None
                elif found is not None and found[0] != purchase_id:
                    outcome = CHALLENGE_TAKEN
                    row = db.execute(
                        f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (found[0],)
                    ).fetchone()
                elif existing is not None:
                    same = found is not None and existing[6] == challenge_id and int(existing[1]) == key_id
                    outcome, row = (BOUND_ALREADY if same else TRANSACTION_TAKEN), existing
                else:
                    if found is None:
                        db.execute(
                            "INSERT INTO payment_claims (claim, owner, value, created_at) VALUES (?, ?, ?, ?)",
                            (claim, purchase_id, purchase_id, now),
                        )
                    self._insert_purchase_locked(
                        purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, "tempo", now,
                        None, status, tempo_terms, valid_before,
                    )
                    outcome = BOUND
                    row = db.execute(
                        f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)
                    ).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return outcome, (_row_to_purchase(row) if row else None)

    def update_credit_purchase(
        self, purchase_id: str, changes: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> CreditPurchase | None:
        """Change a purchase's mutable fields (``PURCHASE_MUTABLE_FIELDS``), only while its status is one of
        ``only_if_status`` if given. Returns the purchase as it is now, or None if nothing was changed."""
        _check_purchase_changes(changes)
        if not changes:
            raise ValueError("nothing to change")
        sets = ", ".join(f"{name} = ?" for name in changes)
        where, args = "id = ?", [purchase_id]
        if only_if_status is not None:
            where += f" AND status IN ({', '.join('?' for _ in only_if_status)})"
            args.extend(only_if_status)
        with self._lock:
            db = self._db()
            cur = db.execute(f"UPDATE credit_purchases SET {sets} WHERE {where}", [*changes.values(), *args])
            if cur.rowcount != 1:
                return None
            row = db.execute(f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)).fetchone()
        return _row_to_purchase(row)

    def credit_tempo_purchase(self, purchase_id: str, payment_intent: str) -> tuple[bool, int]:
        """Release a paid Tempo purchase: link its settled PaymentIntent and add its screenshots, as one step.

        Only from "paid", never after a refund, and never with another
        PaymentIntent than one already linked. Returns (credited by this call,
        the key's balance after it); a second call changes nothing.
        """
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT key_id, screenshots FROM credit_purchases WHERE id = ? AND status = ? "
                    "AND refunded_at IS NULL AND (payment_intent IS NULL OR payment_intent = ?)",
                    (purchase_id, PURCHASE_PAID, payment_intent),
                ).fetchone()
                if row is not None:
                    db.execute(
                        "UPDATE credit_purchases SET status = ?, payment_intent = ? WHERE id = ?",
                        (PURCHASE_CREDITED, payment_intent, purchase_id),
                    )
                    self._add_credit_locked(int(row[0]), int(row[1]))
                    key_id = int(row[0])
                else:
                    found = db.execute("SELECT key_id FROM credit_purchases WHERE id = ?", (purchase_id,)).fetchone()
                    key_id = int(found[0]) if found else 0
                balance = self._credit_locked(key_id)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return row is not None, balance

    def _insert_purchase_locked(
        self, purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method, now,
        payment_intent, status, tempo_terms, valid_before,
    ) -> bool:
        cur = self._db().execute(
            "INSERT INTO credit_purchases (id, key_id, pack, screenshots, amount_cents, currency, challenge_id, "
            "method, created_at, payment_intent, status, tempo_terms, valid_before) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (
                purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method, now,
                payment_intent, status, tempo_terms, valid_before,
            ),
        )
        return cur.rowcount == 1

    def _add_credit_locked(self, key_id: int, screenshots: int) -> None:
        self._db().execute(
            "INSERT INTO credits (key_id, balance) VALUES (?, ?) "
            "ON CONFLICT (key_id) DO UPDATE SET balance = balance + excluded.balance",
            (key_id, screenshots),
        )

    def get_credit_purchase(self, purchase_id: str) -> CreditPurchase | None:
        with self._lock:
            row = self._db().execute(
                f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)
            ).fetchone()
        return _row_to_purchase(row) if row else None

    def find_credit_purchase_by_challenge(self, challenge_id: str) -> CreditPurchase | None:
        if not challenge_id:
            return None
        with self._lock:
            row = self._db().execute(
                f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE challenge_id = ? LIMIT 1", (challenge_id,)
            ).fetchone()
        return _row_to_purchase(row) if row else None

    def list_credit_purchases(self, key_id: int) -> list[CreditPurchase]:
        """A key's purchases, oldest first."""
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE key_id = ? ORDER BY created_at, id",
                (key_id,),
            ).fetchall()
        return [_row_to_purchase(r) for r in rows]

    def refund_credit_purchase(
        self, purchase_id: str, *, prorated: bool = False
    ) -> tuple[CreditPurchase, bool] | None:
        """Mark a purchase refunded, take back what is left of it and fix the amount to refund, as one step.

        The credit taken back is the purchase's size or the key's whole
        balance, whichever is smaller: credit already spent stays spent. The
        amount to refund (``refund_amount_cents``, see ``refund_amount``) is
        fixed in the same step, so a later call, whatever ``prorated`` says
        then, refunds the same amount. Returns (the purchase as it is now,
        whether this call took the credit back), or None if there is no such
        purchase. A second call changes nothing, except to fix the amount of a
        purchase refunded before it was stored with the refund.
        """
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)
                ).fetchone()
                changed = False
                purchase = _row_to_purchase(row) if row is not None else None
                if purchase is not None and purchase.refunded_at is None:
                    # A purchase never credited (a Tempo payment not yet matched, or unmatched) gave nothing.
                    held = self._credit_locked(purchase.key_id) if purchase.credited else 0
                    removed = min(purchase.screenshots, held)
                    db.execute(
                        "UPDATE credits SET balance = balance - ? WHERE key_id = ?", (removed, purchase.key_id)
                    )
                    db.execute(
                        "UPDATE credit_purchases SET refunded_at = ?, credits_removed = ?, refund_amount_cents = ? "
                        "WHERE id = ?",
                        (now, removed, refund_amount(purchase, removed, prorated=prorated), purchase_id),
                    )
                    changed = True
                elif purchase is not None and purchase.refund_amount_cents is None:
                    db.execute(
                        "UPDATE credit_purchases SET refund_amount_cents = ? WHERE id = ?",
                        (refund_amount(purchase, purchase.credits_removed, prorated=prorated), purchase_id),
                    )
                if purchase is not None:
                    row = db.execute(
                        f"SELECT {_PURCHASE_COLUMNS} FROM credit_purchases WHERE id = ?", (purchase_id,)
                    ).fetchone()
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return (_row_to_purchase(row), changed) if row else None

    def set_credit_refund_id(self, purchase_id: str, refund_id: str) -> bool:
        """Record the Stripe Refund made for a refunded purchase."""
        with self._lock:
            cur = self._db().execute(
                "UPDATE credit_purchases SET refund_id = ? WHERE id = ? AND refunded_at IS NOT NULL",
                (refund_id, purchase_id),
            )
        return cur.rowcount == 1

    def set_credit_payment_intent(
        self, purchase_id: str, payment_intent: str, *, replacing: str | None = None
    ) -> bool:
        """Link a purchase to the PaymentIntent that records it in Stripe. True if it is linked to it now.

        Never replaces another one, except ``replacing``, and that only while
        the purchase is not credited: the caller has found that this
        PaymentIntent, not that one, is Stripe's settlement of the transfer.
        """
        with self._lock:
            cur = self._db().execute(
                "UPDATE credit_purchases SET payment_intent = ? WHERE id = ? "
                "AND (payment_intent IS NULL OR payment_intent = ? OR (payment_intent = ? AND status != ?))",
                (payment_intent, purchase_id, payment_intent, replacing, PURCHASE_CREDITED),
            )
        return cur.rowcount == 1

    # --- payment claims: the replay record of on-chain payments (tempo.py) -------------------

    def claim_payment(self, claim: str, owner: str, value: str) -> bool:
        """Claim ``claim`` for ``owner``, atomically. True if it was free or is already ``owner``'s."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            db = self._db()
            cur = db.execute(
                "INSERT INTO payment_claims (claim, owner, value, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (claim) DO NOTHING",
                (claim, owner, value, now),
            )
            if cur.rowcount == 1:
                return True
            row = db.execute("SELECT owner FROM payment_claims WHERE claim = ?", (claim,)).fetchone()
        return row is not None and row[0] == owner

    def get_payment_claim(self, claim: str) -> tuple[str, str] | None:
        """(owner, value) of a claim, or None."""
        with self._lock:
            row = self._db().execute("SELECT owner, value FROM payment_claims WHERE claim = ?", (claim,)).fetchone()
        return (row[0], row[1]) if row else None

    def release_payment_claim(self, claim: str, owner: str) -> None:
        """Give up a claim, only if ``owner`` holds it."""
        with self._lock:
            self._db().execute("DELETE FROM payment_claims WHERE claim = ? AND owner = ?", (claim, owner))

    def _credit_locked(self, key_id: int) -> int:
        row = self._db().execute("SELECT balance FROM credits WHERE key_id = ?", (key_id,)).fetchone()
        return int(row[0]) if row else 0

    # --- the optional email address and quota notices ----------------------------------

    def set_email(self, key_id: int, email: str | None) -> bool:
        """Set, replace or (with None) remove the key's address. False if there is no such key."""
        with self._lock:
            cur = self._db().execute("UPDATE api_keys SET email = ? WHERE id = ?", (email, key_id))
        return cur.rowcount == 1

    def keys_with_email(self, email: str) -> list[int]:
        """Ids of the keys that hold this exact address (for a "stop" reply)."""
        with self._lock:
            rows = self._db().execute("SELECT id FROM api_keys WHERE email = ? ORDER BY id", (email,)).fetchall()
        return [int(r[0]) for r in rows]

    def queue_notice(self, notice: Notice) -> bool:
        """Record a pending notice. False if this key, month and threshold already has one."""
        now = notice.created_at or datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._db().execute(
                "INSERT INTO notices (id, key_id, month, threshold, used, quota, plan, key_ref, resets_at, upgrade, "
                "origin, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO NOTHING",
                (notice.id, notice.key_id, notice.month, notice.threshold, notice.used, notice.quota, notice.plan,
                 notice.key_ref, notice.resets_at, notice.upgrade, notice.origin, NOTICE_PENDING, now),
            )
        return cur.rowcount == 1

    def list_notices(self, key_id: int) -> list[Notice]:
        with self._lock:
            rows = self._db().execute(
                "SELECT key_id, month, threshold, used, quota, plan, key_ref, resets_at, upgrade, origin, status, "
                "created_at, sent_at FROM notices WHERE key_id = ? ORDER BY month, threshold",
                (key_id,),
            ).fetchall()
        return [Notice(int(r[0]), r[1], int(r[2]), int(r[3]), int(r[4]), *r[5:]) for r in rows]

    # --- free tries (tries.py) -----------------------------------------------------------

    def try_admit(self, day: str, limit: int) -> tuple[bool, int]:
        """Atomically add one to the day's ``rendered`` count if it is under ``limit``.

        Returns (allowed, count after the call). Not refunded when the capture
        fails: the cap limits rendering, and a failed render costs as much.
        """
        with self._lock:
            if limit <= 0:
                return False, self._tries_rendered_locked(day)
            row = self._db().execute(
                "INSERT INTO tries (day, rendered) VALUES (?, 1) "
                "ON CONFLICT (day) DO UPDATE SET rendered = rendered + 1 WHERE tries.rendered < ? "
                "RETURNING rendered",
                (day, limit),
            ).fetchone()
            if row is None:
                return False, self._tries_rendered_locked(day)
            return True, int(row[0])

    def record_try(self, day: str, outcome: str) -> None:
        """Count one try and how it ended (one of ``TRY_OUTCOMES``)."""
        if outcome not in TRY_OUTCOMES:
            raise ValueError(f"unknown try outcome {outcome!r}")
        with self._lock:
            # outcome is one of the fixed column names above, never input.
            self._db().execute(
                f"INSERT INTO tries (day, total, {outcome}) VALUES (?, 1, 1) "
                f"ON CONFLICT (day) DO UPDATE SET total = total + 1, {outcome} = {outcome} + 1",
                (day,),
            )

    def try_days(self, days: list[str]) -> list[TryDay]:
        """The counts for each of ``days``, in that order; zeros for a day with no tries."""
        with self._lock:
            rows = self._db().execute(
                f"SELECT {_TRY_COLUMNS} FROM tries WHERE day IN ({', '.join('?' * len(days))})", days
            ).fetchall() if days else []
        found = {r[0]: TryDay(r[0], *(int(v) for v in r[1:])) for r in rows}
        return [found.get(d, TryDay(d)) for d in days]

    # --- funnel counts (funnel.py) --------------------------------------------------------

    def record_funnel(self, day: str, event: str) -> None:
        """Add one to the day's count for ``event`` (one of ``FUNNEL_EVENTS``)."""
        if event not in FUNNEL_EVENTS:
            raise ValueError(f"unknown funnel event {event!r}")
        with self._lock:
            self._db().execute(
                "INSERT INTO funnel (day, event, count) VALUES (?, ?, 1) "
                "ON CONFLICT (day, event) DO UPDATE SET count = count + 1",
                (day, event),
            )

    def funnel_days(self, days: list[str]) -> list[FunnelDay]:
        """The counts for each of ``days``, in that order; zeros for a day with none."""
        with self._lock:
            rows = self._db().execute(
                f"SELECT day, event, count FROM funnel WHERE day IN ({', '.join('?' * len(days))})", days
            ).fetchall() if days else []
        found: dict[str, dict[str, int]] = {}
        for day, event, count in rows:
            if event in FUNNEL_EVENTS:  # a step since renamed or dropped is not shown
                found.setdefault(day, {})[event] = int(count)
        return [FunnelDay(d, **found.get(d, {})) for d in days]
    # --- OAuth authorization codes (oauth.py) ---------------------------------------

    def save_oauth_code(self, code: OAuthCode) -> None:
        """Store a new code, and delete every code that has expired."""
        now = int(time.time())
        with self._lock:
            db = self._db()
            db.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            db.execute(
                f"INSERT INTO oauth_codes ({_CODE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (code.id, code.client_hash, code.redirect_uri, code.code_challenge, code.resource,
                 code.sealed_key, int(code.expires_at), int(code.created_at or now)),
            )

    def take_oauth_code(self, code_id: str, now: int | None = None) -> OAuthCode | None:
        """Delete the code and return it, once. None if unknown, already taken or expired."""
        now = int(time.time()) if now is None else now
        with self._lock:
            row = self._db().execute(
                f"DELETE FROM oauth_codes WHERE id = ? RETURNING {_CODE_COLUMNS}", (code_id,)
            ).fetchone()
        if row is None:
            return None
        code = OAuthCode(*row[:6], int(row[6]), int(row[7]))
        return code if code.expires_at >= now else None

    def _tries_rendered_locked(self, day: str) -> int:
        row = self._db().execute("SELECT rendered FROM tries WHERE day = ?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def _usage_locked(self, key_id: int, month: str) -> int:
        row = self._db().execute("SELECT count FROM usage WHERE key_id = ? AND month = ?", (key_id, month)).fetchone()
        return int(row[0]) if row else 0


_CODE_COLUMNS = "id, client_hash, redirect_uri, code_challenge, resource, sealed_key, expires_at, created_at"

_TRY_COLUMNS = "day, total, rendered, succeeded, failed, refused, limited, capped"

_KEY_COLUMNS = (
    "id, plan, label, created_at, revoked_at, internal, successful_screenshots, "
    "stripe_customer_id, stripe_subscription_id, email"
)


def _row_to_key(row) -> ApiKey:
    return ApiKey(
        id=int(row[0]),
        plan=row[1],
        label=row[2],
        created_at=row[3],
        revoked=row[4] is not None,
        internal=bool(row[5]),
        successful_screenshots=int(row[6]),
        stripe_customer_id=row[7],
        stripe_subscription_id=row[8],
        email=row[9],
    )


_PURCHASE_COLUMNS = (
    "id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method, created_at, "
    "refunded_at, credits_removed, refund_id, payment_intent, status, tempo_terms, valid_before, "
    "stripe_attempt, stripe_retry_at, refund_amount_cents, refund_status"
)


def _check_purchase_changes(changes: dict) -> None:
    unknown = set(changes) - PURCHASE_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"not a field that can change: {sorted(unknown)}")
    if "status" in changes and changes["status"] not in PURCHASE_STATUSES:
        raise ValueError(f"unknown purchase status {changes['status']!r}")


def _row_to_purchase(row) -> CreditPurchase:
    return CreditPurchase(
        id=row[0],
        key_id=int(row[1]),
        pack=row[2],
        screenshots=int(row[3]),
        amount_cents=int(row[4]),
        currency=row[5],
        challenge_id=row[6] or "",
        method=row[7],
        created_at=row[8],
        refunded_at=row[9],
        credits_removed=int(row[10] or 0),
        refund_id=row[11],
        payment_intent=row[12],
        status=row[13] or PURCHASE_CREDITED,
        tempo_terms=row[14],
        valid_before=None if row[15] is None else int(row[15]),
        stripe_attempt=int(row[16] or 0),
        stripe_retry_at=row[17],
        refund_amount_cents=None if row[18] is None else int(row[18]),
        refund_status=row[19],
    )


def open_configured_storage(settings: "Settings") -> Storage:
    """Open the backend named by ``settings.storage_backend`` (``CALOTYPE_STORAGE``)."""
    if settings.storage_backend == "firestore":
        # Imported here so the SQLite path does not load the Google client.
        from .firestore_storage import FirestoreStorage

        store: Storage = FirestoreStorage(
            project=settings.firestore_project or None,
            collection_prefix=settings.firestore_collection_prefix,
        )
        store.init()
        return store
    if settings.storage_backend == "sqlite":
        return open_storage(settings.database_url)
    raise ValueError(f"unknown storage backend {settings.storage_backend!r}")


def open_storage(database_url: str) -> Storage:
    """Open storage from a URL. Only ``sqlite:///path`` exists today."""
    if database_url.startswith("sqlite:///"):
        store = SQLiteStorage(database_url[len("sqlite:///") :])
    elif database_url == "sqlite://:memory:":
        store = SQLiteStorage(":memory:")
    else:
        raise ValueError("Unsupported CALOTYPE_DATABASE_URL; only sqlite:///path is implemented")
    store.init()
    return store
