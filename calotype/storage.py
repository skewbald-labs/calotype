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
(``Notice``: key id, month, threshold, counts; never the address). No name, IP
address or other personal data.

The email address is used for one thing: telling the holder when the key
reaches 80% and 100% of its monthly quota (``notices.py``). The service only
queues a notice; a separate job sends it and marks it sent. Removing the
address (the holder's ``DELETE /v1/keys/email``, the link in each notice, or
``admin remove-email``), revoking the key, or deleting it clears the address.

``delete_key`` removes a key's record, its usage counts and its notices for
good, on the key holder's request (see the privacy notice).

The free tries on the home page (``tries.py``) are counted per UTC day
(``TryDay``): how many there were and how each ended. Never the URL, the host
or the IP address. ``try_admit`` also enforces the daily cap on renders, for
every instance at once.

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
from dataclasses import dataclass, field
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
    def set_email(self, key_id: int, email: str | None) -> bool: ...
    def keys_with_email(self, email: str) -> list[int]: ...
    def queue_notice(self, notice: Notice) -> bool: ...
    def list_notices(self, key_id: int) -> list[Notice]: ...
    def try_admit(self, day: str, limit: int) -> tuple[bool, int]: ...
    def record_try(self, day: str, outcome: str) -> None: ...
    def try_days(self, days: list[str]) -> list[TryDay]: ...


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
"""

# Columns added after the first release. ``init`` adds any that an older
# database lacks, so an existing file keeps working.
_ADDED_KEY_COLUMNS = {
    "stripe_customer_id": "TEXT",
    "stripe_subscription_id": "TEXT",
    "email": "TEXT",
}
_INDEXES = """
CREATE INDEX IF NOT EXISTS api_keys_subscription ON api_keys (stripe_subscription_id);
CREATE INDEX IF NOT EXISTS api_keys_email ON api_keys (email);
CREATE INDEX IF NOT EXISTS notices_key ON notices (key_id);
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
        """Delete the key's record (with its email address), its usage counts and its notices.

        False if there was no such key.

        Ids are never handed out again (``AUTOINCREMENT``), so a late Stripe
        event for a deleted key finds no key rather than another one.
        """
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM usage WHERE key_id = ?", (key_id,))
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

    def _tries_rendered_locked(self, day: str) -> int:
        row = self._db().execute("SELECT rendered FROM tries WHERE day = ?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def _usage_locked(self, key_id: int, month: str) -> int:
        row = self._db().execute("SELECT count FROM usage WHERE key_id = ? AND month = ?", (key_id, month)).fetchone()
        return int(row[0]) if row else 0


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
