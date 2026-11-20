"""API keys and usage counts in Google Cloud Firestore (Native mode).

``FirestoreStorage`` implements the ``Storage`` interface in ``storage.py``
with the same behaviour as ``SQLiteStorage``. It is meant for Cloud Run,
where the container disk is lost on restart and not shared between
instances. Credentials come from the environment's default Google
credentials (on Cloud Run, the service account); no key file is used.

Layout, under a configurable collection prefix (default ``calotype_``):

``{prefix}keys/{id}``
    One document per key, with the same fields as the SQLite ``api_keys``
    table: ``id`` (int), ``key_hash`` (SHA-256 hex, never the key), ``plan``,
    ``label``, ``created_at`` (ISO 8601, UTC), ``revoked_at`` (null or ISO
    8601), ``internal`` (bool), ``successful_screenshots`` (int),
    ``stripe_customer_id`` and ``stripe_subscription_id`` (null until the key
    pays; documents written before these fields existed simply lack them),
    and ``email`` (null unless the holder gave an address for quota notices).
``{prefix}usage/{id}_{YYYY-MM}``
    ``key_id``, ``month``, ``count``: screenshots counted this month.
``{prefix}meta/key_ids``
    ``last_id``: the last key id handed out. Ids are small integers, as in
    SQLite, so the admin CLI and the logs stay the same.
``{prefix}stripe_events/{event id}``
    ``processed_at``: one document per Stripe webhook event already handled.
    ``create`` fails if the document exists, which makes recording atomic.
``{prefix}notices/{id}_{YYYY-MM}_{threshold}``
    A quota notice (``storage.Notice``), queued by the service with
    ``create``, so a key gets at most one per month and threshold. The
    skewbald-checks job (free-reign ``ops/checks``) reads the pending ones,
    sends them, and sets ``status``, ``sent_at`` and ``to_hash`` (a keyed
    hash of the address, for its per-address monthly cap). Never the address.
``{prefix}tries/{YYYY-MM-DD}``
    The free tries of one UTC day (``storage.TryDay``): ``day``, ``total``,
    ``rendered`` and one count per outcome. Never a URL, host or IP address.
    ``rendered`` is the shared daily cap, taken by ``try_admit`` exactly as
    ``try_consume`` takes a key's quota (below).

No name, IP address or URL is stored; an email address only if the key's
holder gave one, on the key's own document.

Counters change through Firestore's atomic ``Increment`` transform, which
the server applies in order and which returns the value it produced. That
needs no lock and no retry, so parallel requests for one key never fail on
contention (a read-modify-write transaction on one document does: under a
burst its retries run out). ``try_consume`` works like this:

1. Read the month's count. If it is already at the limit, refuse. This costs
   one read and no write, so a client hammering a used-up key cannot burn the
   free tier's daily write quota.
2. Otherwise apply ``count + 1`` and look at the value it returned. Each
   caller gets a different value. If it is at most the limit, the call is
   allowed. If not, apply ``count - 1`` to undo it and refuse.

Allowed calls are exactly those whose increment landed at or under the
limit, so a key never gets more than its quota. Two requests that both pass
step 1 for the last slot both increment; one gets ``limit`` and runs, the
other gets ``limit + 1``, undoes it and is refused. The count can sit above
the limit for the instant between an over-limit increment and its undo, and
a request that lands in that instant may be refused although a refund freed
a slot; it never lets one through.

Key ids come from an ``Increment`` on ``{prefix}meta/key_ids`` too, so they
are unique and rise in creation order. Revoking, which needs "only if not
already revoked", runs in a transaction; it is a rare operator action.
Deleting a key (``delete_key``, on the key holder's request) removes its key
document and every usage document for it; the id is not reused.

Queries use only single-field filters and sorts, which Firestore indexes
automatically, so no composite index has to be deployed.
"""

from __future__ import annotations

import time
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import Aborted, AlreadyExists, NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from .storage import (
    KEY_REF,
    NOTICE_PENDING,
    REF_LABEL_PREFIX,
    REF_TAG,
    TRY_OUTCOMES,
    ApiKey,
    Contact,
    Notice,
    TryDay,
    generate_key,
    hash_key,
    source_from_label,
)

DEFAULT_COLLECTION_PREFIX = "calotype_"


class FirestoreStorage:
    def __init__(
        self,
        *,
        project: str | None = None,
        collection_prefix: str = DEFAULT_COLLECTION_PREFIX,
        client: firestore.Client | None = None,
    ):
        if "/" in collection_prefix:
            raise ValueError("CALOTYPE_FIRESTORE_COLLECTION_PREFIX may not contain '/'")
        self.prefix = collection_prefix
        # Project None: taken from the default credentials (on Cloud Run, the
        # service's project) or from FIRESTORE_EMULATOR_HOST's settings.
        self._client = client or firestore.Client(project=project or None)
        self._keys = self._client.collection(f"{collection_prefix}keys")
        self._usage = self._client.collection(f"{collection_prefix}usage")
        self._counter = self._client.collection(f"{collection_prefix}meta").document("key_ids")
        self._events = self._client.collection(f"{collection_prefix}stripe_events")
        self._notices = self._client.collection(f"{collection_prefix}notices")
        self._tries = self._client.collection(f"{collection_prefix}tries")

    # --- plumbing -----------------------------------------------------------------

    def init(self) -> None:
        """Nothing to create: Firestore collections exist once written to."""

    def close(self) -> None:
        """The client holds no state that needs closing between requests."""

    def collections(self) -> list[Any]:
        """The collections this store writes to (used by tests and cleanup)."""
        return [self._keys, self._usage, self._counter.parent, self._events, self._notices, self._tries]

    @staticmethod
    def _increment(ref, field: str, by: int, *, create: dict | None = None) -> int:
        """Apply ``field += by`` atomically and return the value it produced.

        With ``create``, a missing document is created from those fields
        first (the field starts at 0); without it, a missing document raises
        ``NotFound``.
        """
        if create is not None:
            result = ref.set({**create, field: firestore.Increment(by)}, merge=True)
        else:
            result = ref.update({field: firestore.Increment(by)})
        return int(result.transform_results[0].integer_value)

    def _key_ref(self, key_id: int):
        return self._keys.document(str(int(key_id)))

    def _usage_ref(self, key_id: int, month: str):
        return self._usage.document(f"{int(key_id)}_{month}")

    # --- keys -------------------------------------------------------------------------

    def create_key(
        self, plan: str, label: str = "", *, internal: bool = False, email: str | None = None
    ) -> tuple[str, ApiKey]:
        raw = generate_key()
        key_hash = hash_key(raw)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        key_id = self._increment(self._counter, "last_id", 1, create={})
        self._key_ref(key_id).create(
            {
                "id": key_id,
                "key_hash": key_hash,
                "plan": plan,
                "label": label,
                "created_at": now,
                "revoked_at": None,
                "internal": bool(internal),
                "successful_screenshots": 0,
                "stripe_customer_id": None,
                "stripe_subscription_id": None,
                "email": email,
            }
        )
        return raw, ApiKey(key_id, plan, label, now, False, internal, 0, email=email)

    def get_key(self, raw_key: str) -> ApiKey | None:
        docs = list(self._keys.where(filter=FieldFilter("key_hash", "==", hash_key(raw_key))).limit(1).stream())
        return _doc_to_key(docs[0].to_dict()) if docs else None

    def list_keys(self) -> list[ApiKey]:
        return [_doc_to_key(d.to_dict()) for d in self._keys.order_by("id").stream()]

    def revoke_key(self, key_id: int) -> bool:
        ref = self._key_ref(key_id)
        now = datetime.now(UTC).isoformat(timespec="seconds")

        @firestore.transactional
        def revoke(t) -> bool:
            snap = ref.get(transaction=t)
            if not snap.exists or snap.get("revoked_at") is not None:
                return False
            t.update(ref, {"revoked_at": now, "email": None})  # a revoked key keeps no address
            return True

        return revoke(self._client.transaction())

    def delete_key(self, key_id: int) -> bool:
        """Delete the key's document (with its email address), its usage and its notice documents.

        False if there was no such key. Usage and notice documents go first, so a run cut short leaves the key document
        to find and delete again. The counter in ``meta/key_ids`` is not
        lowered, so the id is never handed out again.
        """
        ref = self._key_ref(key_id)
        exists = ref.get().exists
        usage = self._usage.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        notices = self._notices.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        batch = self._client.batch()
        pending = 0
        for doc in [*usage, *notices]:
            batch.delete(doc.reference)
            pending += 1
            if pending == 400:  # a batch takes at most 500 writes
                batch.commit()
                batch, pending = self._client.batch(), 0
        if pending:
            batch.commit()
        if exists:
            ref.delete()
        return exists

    def set_plan(self, key_id: int, plan: str) -> bool:
        try:
            self._key_ref(key_id).update({"plan": plan})
        except NotFound:
            return False
        return True

    def set_internal(self, key_id: int, internal: bool = True) -> bool:
        """Mark a key as Calotype's own (or not). Internal keys are never demand-test contacts."""
        try:
            self._key_ref(key_id).update({"internal": bool(internal)})
        except NotFound:
            return False
        return True

    def tag_source(self, key_id: int, tag: str) -> bool:
        """Record the ``?ref=`` tag a key was first used with, if it has no label yet (see storage.py)."""
        if not REF_TAG.match(tag):
            return False
        ref = self._key_ref(key_id)

        @firestore.transactional
        def tag_it(t) -> bool:
            snap = ref.get(transaction=t)
            if not snap.exists:
                return False
            doc = snap.to_dict()
            if doc.get("label") or doc.get("internal"):
                return False
            t.update(ref, {"label": REF_LABEL_PREFIX + tag})
            return True

        return tag_it(self._client.transaction())

    def get_key_by_id(self, key_id: int) -> ApiKey | None:
        snap = self._key_ref(key_id).get()
        return _doc_to_key(snap.to_dict()) if snap.exists else None

    def find_key_by_ref(self, ref: str) -> ApiKey | None:
        """The one key whose hash starts with ``ref``: a range query on ``key_hash`` (hex sorts below 'g')."""
        if not KEY_REF.match(ref or ""):
            return None
        query = (self._keys.where(filter=FieldFilter("key_hash", ">=", ref))
                 .where(filter=FieldFilter("key_hash", "<", ref + "g")).limit(2))
        docs = [d.to_dict() for d in query.stream()]
        return _doc_to_key(docs[0]) if len(docs) == 1 else None

    def find_key_by_subscription(self, subscription_id: str) -> ApiKey | None:
        query = self._keys.where(filter=FieldFilter("stripe_subscription_id", "==", subscription_id))
        docs = sorted((d.to_dict() for d in query.stream()), key=lambda d: int(d["id"]))
        return _doc_to_key(docs[0]) if docs else None

    def set_billing(self, key_id: int, plan: str, *, customer_id: str, subscription_id: str) -> bool:
        """Set the plan and the Stripe ids together, as one write."""
        try:
            self._key_ref(key_id).update(
                {"plan": plan, "stripe_customer_id": customer_id, "stripe_subscription_id": subscription_id}
            )
        except NotFound:
            return False
        return True

    # --- Stripe webhook events -----------------------------------------------------------

    def event_processed(self, event_id: str) -> bool:
        return self._events.document(_event_doc_id(event_id)).get().exists

    def mark_event_processed(self, event_id: str) -> bool:
        """Record a handled webhook event. False if it was already recorded."""
        ref = self._events.document(_event_doc_id(event_id))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for attempt in range(6):
            try:
                ref.create({"processed_at": now})
                return True
            except AlreadyExists:
                return False
            except Aborted:
                # Parallel creates of one document contend for its lock. If
                # another caller's create won, the document now exists.
                if ref.get().exists:
                    return False
                time.sleep(0.05 * (2**attempt))
        raise Aborted(f"could not record event {event_id} after retries")

    # --- usage ------------------------------------------------------------------------

    def try_consume(self, key_id: int, month: str, limit: int) -> tuple[bool, int]:
        """Atomically add one to this month's count if it is under ``limit``.

        Returns (allowed, count after the call). See the module docstring for
        why parallel calls cannot take the key past ``limit``.
        """
        used = self.get_usage(key_id, month)
        if used >= limit:  # also covers limit <= 0
            return False, used
        ref = self._usage_ref(key_id, month)
        count = self._increment(ref, "count", 1, create={"key_id": int(key_id), "month": month})
        if count <= limit:
            return True, count
        # Over: undo. Other refused calls may not have undone theirs yet, so
        # report at most the limit, which is what this key has really used.
        return False, min(self._increment(ref, "count", -1), limit)

    def refund(self, key_id: int, month: str) -> None:
        ref = self._usage_ref(key_id, month)
        try:
            count = self._increment(ref, "count", -1)
        except NotFound:
            return
        if count < 0:  # nothing was counted; put it back to 0, as SQLite's "count > 0" guard does
            self._increment(ref, "count", 1)

    def get_usage(self, key_id: int, month: str) -> int:
        snap = self._usage_ref(key_id, month).get()
        return int(snap.get("count")) if snap.exists else 0

    def record_success(self, key_id: int) -> int:
        """Count one successful screenshot for the key's lifetime. Returns the new total."""
        try:
            return self._increment(self._key_ref(key_id), "successful_screenshots", 1)
        except NotFound:
            return 0

    # --- the optional email address and quota notices ----------------------------------

    def set_email(self, key_id: int, email: str | None) -> bool:
        """Set, replace or (with None) remove the key's address. False if there is no such key."""
        try:
            self._key_ref(key_id).update({"email": email})
        except NotFound:
            return False
        return True

    def keys_with_email(self, email: str) -> list[int]:
        docs = self._keys.where(filter=FieldFilter("email", "==", email)).stream()
        return sorted(int(d.get("id")) for d in docs)

    def queue_notice(self, notice: Notice) -> bool:
        """Record a pending notice. False if this key, month and threshold already has one."""
        data = asdict(notice)
        data["status"] = NOTICE_PENDING
        data["created_at"] = notice.created_at or datetime.now(UTC).isoformat(timespec="seconds")
        try:
            self._notices.document(notice.id).create(data)
        except AlreadyExists:
            return False
        return True

    def list_notices(self, key_id: int) -> list[Notice]:
        docs = [d.to_dict() for d in self._notices.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()]
        names = {f.name for f in fields(Notice)}
        notices = [Notice(**{k: v for k, v in d.items() if k in names}) for d in docs]
        return sorted(notices, key=lambda n: (n.month, n.threshold))

    # --- free tries (tries.py) -----------------------------------------------------------

    def try_admit(self, day: str, limit: int) -> tuple[bool, int]:
        """Atomically add one to the day's ``rendered`` count if it is under ``limit``.

        The same read, increment and undo as ``try_consume``, so instances
        sharing the database never let more than ``limit`` through in a day.
        """
        ref = self._tries.document(day)
        snap = ref.get()
        rendered = int(snap.get("rendered") or 0) if snap.exists else 0
        if rendered >= limit:  # also covers limit <= 0; no write
            return False, rendered
        count = self._increment(ref, "rendered", 1, create={"day": day})
        if count <= limit:
            return True, count
        return False, min(self._increment(ref, "rendered", -1), limit)

    def record_try(self, day: str, outcome: str) -> None:
        """Count one try and how it ended (one of ``TRY_OUTCOMES``), in one write."""
        if outcome not in TRY_OUTCOMES:
            raise ValueError(f"unknown try outcome {outcome!r}")
        self._tries.document(day).set(
            {"day": day, "total": firestore.Increment(1), outcome: firestore.Increment(1)}, merge=True
        )

    def try_days(self, days: list[str]) -> list[TryDay]:
        """The counts for each of ``days``, in that order; zeros for a day with no tries."""
        names = [f.name for f in fields(TryDay) if f.name != "day"]
        found = {}
        for snap in self._client.get_all([self._tries.document(d) for d in days]):
            if snap.exists:
                doc = snap.to_dict()
                found[snap.id] = TryDay(snap.id, **{n: int(doc.get(n) or 0) for n in names})
        return [found.get(d, TryDay(d)) for d in days]

    # --- contacts -----------------------------------------------------------------------

    def list_contacts(self) -> list[Contact]:
        """Non-internal keys with at least one successful screenshot, in creation order.

        Ids are handed out in creation order, so ordering by id is ordering by
        creation. The filter on ``internal`` and the sort run here rather than
        in the query, so Firestore needs no composite index.
        """
        docs = [
            d.to_dict()
            for d in self._keys.where(filter=FieldFilter("successful_screenshots", ">", 0)).stream()
        ]
        rows = sorted((d for d in docs if not d.get("internal")), key=lambda d: int(d["id"]))
        return [
            Contact(
                i + 1,
                int(d["id"]),
                d["plan"],
                d["created_at"],
                int(d["successful_screenshots"]),
                source_from_label(d.get("label") or ""),
            )
            for i, d in enumerate(rows)
        ]

    def contact_ordinal(self, key_id: int) -> int | None:
        for c in self.list_contacts():
            if c.key_id == key_id:
                return c.ordinal
        return None


def _event_doc_id(event_id: str) -> str:
    # Stripe event ids are "evt_" plus letters and digits. Anything else could
    # name another path, so it is refused rather than escaped.
    if not event_id or "/" in event_id or event_id in (".", "..") or event_id.startswith("__"):
        raise ValueError("not a usable event id")
    return event_id


def _doc_to_key(d: dict) -> ApiKey:
    return ApiKey(
        id=int(d["id"]),
        plan=d["plan"],
        label=d.get("label", ""),
        created_at=d["created_at"],
        revoked=d.get("revoked_at") is not None,
        internal=bool(d.get("internal", False)),
        successful_screenshots=int(d.get("successful_screenshots", 0)),
        stripe_customer_id=d.get("stripe_customer_id"),
        stripe_subscription_id=d.get("stripe_subscription_id"),
        email=d.get("email"),
    )
