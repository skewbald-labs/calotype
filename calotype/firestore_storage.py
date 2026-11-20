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
    pays; documents written before these fields existed simply lack them).
``{prefix}usage/{id}_{YYYY-MM}``
    ``key_id``, ``month``, ``count``: screenshots counted this month.
``{prefix}meta/key_ids``
    ``last_id``: the last key id handed out. Ids are small integers, as in
    SQLite, so the admin CLI and the logs stay the same.
``{prefix}stripe_events/{event id}``
    ``processed_at``: one document per Stripe webhook event already handled.
    ``create`` fails if the document exists, which makes recording atomic.
``{prefix}credits/{id}``
    ``key_id``, ``balance``: the key's prepaid screenshots not yet used.
``{prefix}credit_purchases/{PaymentIntent id}``
    One document per prepaid pack bought: ``id``, ``key_id``, ``pack``,
    ``screenshots``, ``amount_cents``, ``currency``, ``challenge_id``,
    ``method``, ``created_at``, ``refunded_at``, ``credits_removed``,
    ``refund_id``.

No email, name, IP address or URL is stored.

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

Prepaid credit: taking one credit (``consume_credit``) works like
``try_consume`` turned round: read the balance and refuse without a write if it
is zero, otherwise apply ``balance - 1`` and undo it if the result went below
zero. Recording a purchase is one atomic batch: ``create`` the purchase
document (which fails if it exists) and ``Increment`` the balance, together or
not at all, so a purchase is added once however often and however much in
parallel the call is repeated. Refunding one runs in a transaction that reads
the purchase first, so it too happens once; it is a rare operator action.

Queries use only single-field filters and sorts, which Firestore indexes
automatically, so no composite index has to be deployed.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import Aborted, AlreadyExists, NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from .storage import (
    KEY_REF,
    REF_LABEL_PREFIX,
    REF_TAG,
    ApiKey,
    Contact,
    CreditPurchase,
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
        self._credits = self._client.collection(f"{collection_prefix}credits")
        self._purchases = self._client.collection(f"{collection_prefix}credit_purchases")

    # --- plumbing -----------------------------------------------------------------

    def init(self) -> None:
        """Nothing to create: Firestore collections exist once written to."""

    def close(self) -> None:
        """The client holds no state that needs closing between requests."""

    def collections(self) -> list[Any]:
        """The collections this store writes to (used by tests and cleanup)."""
        return [self._keys, self._usage, self._counter.parent, self._events, self._credits, self._purchases]

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

    def create_key(self, plan: str, label: str = "", *, internal: bool = False) -> tuple[str, ApiKey]:
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
            }
        )
        return raw, ApiKey(key_id, plan, label, now, False, internal, 0)

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
            t.update(ref, {"revoked_at": now})
            return True

        return revoke(self._client.transaction())

    def delete_key(self, key_id: int) -> bool:
        """Delete the key's document, usage documents, credit and purchase records. False if there was no such key.

        The other documents go first, so a run cut short leaves the key document
        to find and delete again. The counter in ``meta/key_ids`` is not
        lowered, so the id is never handed out again.
        """
        ref = self._key_ref(key_id)
        exists = ref.get().exists
        usage = self._usage.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        purchases = self._purchases.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        batch = self._client.batch()
        batch.delete(self._credit_ref(key_id))
        pending = 1
        for doc in [*usage, *purchases]:
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

    # --- prepaid credit -------------------------------------------------------------------

    def _credit_ref(self, key_id: int):
        return self._credits.document(str(int(key_id)))

    def _purchase_ref(self, purchase_id: str):
        return self._purchases.document(_purchase_doc_id(purchase_id))

    def get_credit_balance(self, key_id: int) -> int:
        snap = self._credit_ref(key_id).get()
        return max(int(snap.get("balance")), 0) if snap.exists else 0

    def consume_credit(self, key_id: int) -> tuple[bool, int]:
        """Atomically take one credit if the key has any. Returns (taken, balance after the call)."""
        if self.get_credit_balance(key_id) <= 0:  # one read and no write for a key without credit
            return False, 0
        ref = self._credit_ref(key_id)
        balance = self._increment(ref, "balance", -1)
        if balance >= 0:
            return True, balance
        # Another request took the last one first: undo, as try_consume does.
        return False, max(self._increment(ref, "balance", 1), 0)

    def return_credit(self, key_id: int) -> None:
        """Give back the credit taken for a screenshot that then failed."""
        try:
            self._increment(self._credit_ref(key_id), "balance", 1)
        except NotFound:
            return

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
    ) -> tuple[bool, int]:
        """Record a paid pack and add its screenshots, once per ``purchase_id``.

        Returns (added, balance after the call). ``added`` is False, and
        nothing changes, if this purchase was recorded before or the key does
        not exist.
        """
        if screenshots <= 0:
            raise ValueError("a purchase adds at least one screenshot")
        purchase_ref, credit_ref = self._purchase_ref(purchase_id), self._credit_ref(key_id)
        if not self._key_ref(key_id).get().exists:
            return False, 0
        now = datetime.now(UTC).isoformat(timespec="seconds")
        record = {
            "id": purchase_id,
            "key_id": int(key_id),
            "pack": pack,
            "screenshots": int(screenshots),
            "amount_cents": int(amount_cents),
            "currency": currency,
            "challenge_id": challenge_id,
            "method": method,
            "created_at": now,
            "refunded_at": None,
            "credits_removed": 0,
            "refund_id": None,
        }
        # One atomic batch: the purchase document is created only if it does not
        # exist, and the balance grows by an Increment in the same commit. Either
        # both happen or neither. No read is involved, so parallel calls for one
        # payment do not contend: one commit wins and the others fail whole.
        for attempt in range(6):
            batch = self._client.batch()
            batch.create(purchase_ref, record)
            batch.set(credit_ref, {"key_id": int(key_id), "balance": firestore.Increment(int(screenshots))}, merge=True)
            try:
                results = batch.commit()
            except AlreadyExists:
                return False, self.get_credit_balance(key_id)
            except Aborted:
                # Contention, not a verdict: nothing was written by this commit. If a
                # parallel call's commit won, the purchase now exists; otherwise try again.
                if purchase_ref.get().exists:
                    return False, self.get_credit_balance(key_id)
                time.sleep(0.05 * (2**attempt))
                continue
            return True, int(results[1].transform_results[0].integer_value)
        raise Aborted(f"could not record purchase {purchase_id} after retries")

    def get_credit_purchase(self, purchase_id: str) -> CreditPurchase | None:
        snap = self._purchase_ref(purchase_id).get()
        return _doc_to_purchase(snap.to_dict()) if snap.exists else None

    def find_credit_purchase_by_challenge(self, challenge_id: str) -> CreditPurchase | None:
        if not challenge_id:
            return None
        query = self._purchases.where(filter=FieldFilter("challenge_id", "==", challenge_id)).limit(1)
        docs = [d.to_dict() for d in query.stream()]
        return _doc_to_purchase(docs[0]) if docs else None

    def list_credit_purchases(self, key_id: int) -> list[CreditPurchase]:
        """A key's purchases, oldest first (sorted here, so no composite index is needed)."""
        query = self._purchases.where(filter=FieldFilter("key_id", "==", int(key_id)))
        purchases = [_doc_to_purchase(d.to_dict()) for d in query.stream()]
        return sorted(purchases, key=lambda p: (p.created_at, p.id))

    def refund_credit_purchase(self, purchase_id: str) -> tuple[CreditPurchase, bool] | None:
        """Mark a purchase refunded and take back what is left of it, as one step (see storage.py)."""
        purchase_ref = self._purchase_ref(purchase_id)
        now = datetime.now(UTC).isoformat(timespec="seconds")

        @firestore.transactional
        def refund(t) -> tuple[CreditPurchase, bool] | None:
            snap = purchase_ref.get(transaction=t)
            if not snap.exists:
                return None
            doc = snap.to_dict()
            if doc.get("refunded_at") is not None:
                return _doc_to_purchase(doc), False
            credit_ref = self._credit_ref(doc["key_id"])
            credit = credit_ref.get(transaction=t)
            # The stored value, which a refused consume_credit may hold below zero
            # for an instant: subtract from it as it is, so that undo still lands right.
            stored = int(credit.get("balance")) if credit.exists else 0
            removed = min(int(doc["screenshots"]), max(stored, 0))
            if credit.exists:
                t.update(credit_ref, {"balance": stored - removed})
            changes = {"refunded_at": now, "credits_removed": removed}
            t.update(purchase_ref, changes)
            return _doc_to_purchase({**doc, **changes}), True

        return refund(self._client.transaction())

    def set_credit_refund_id(self, purchase_id: str, refund_id: str) -> bool:
        """Record the Stripe Refund made for a refunded purchase."""
        ref = self._purchase_ref(purchase_id)

        @firestore.transactional
        def record(t) -> bool:
            snap = ref.get(transaction=t)
            if not snap.exists or snap.get("refunded_at") is None:
                return False
            t.update(ref, {"refund_id": refund_id})
            return True

        return record(self._client.transaction())

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


def _purchase_doc_id(purchase_id: str) -> str:
    # Stripe ids are letters, digits and underscores. Anything else could name
    # another path, so it is refused rather than escaped.
    if not purchase_id or "/" in purchase_id or purchase_id in (".", "..") or purchase_id.startswith("__"):
        raise ValueError("not a usable purchase id")
    return purchase_id


def _doc_to_purchase(d: dict) -> CreditPurchase:
    return CreditPurchase(
        id=d["id"],
        key_id=int(d["key_id"]),
        pack=d["pack"],
        screenshots=int(d["screenshots"]),
        amount_cents=int(d["amount_cents"]),
        currency=d["currency"],
        challenge_id=d.get("challenge_id") or "",
        method=d.get("method") or "stripe",
        created_at=d["created_at"],
        refunded_at=d.get("refunded_at"),
        credits_removed=int(d.get("credits_removed") or 0),
        refund_id=d.get("refund_id"),
    )


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
    )
