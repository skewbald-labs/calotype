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
``{prefix}credits/{id}``
    ``key_id``, ``balance``: the key's prepaid screenshots not yet used.
``{prefix}credit_purchases/{PaymentIntent id or transaction hash}``
    One document per prepaid pack bought: ``id``, ``key_id``, ``pack``,
    ``screenshots``, ``amount_cents``, ``currency``, ``challenge_id``,
    ``method``, ``created_at``, ``refunded_at``, ``credits_removed``,
    ``refund_id``, ``payment_intent`` (a Tempo payment's PaymentIntent), and
    the fields added with the Tempo state machine (``status``, ``tempo_terms``,
    ``valid_before``, ``stripe_attempt``, ``stripe_retry_at``,
    ``refund_amount_cents``, ``refund_status``). Documents written before a
    field existed simply lack it; a missing ``status`` reads as "credited".
``{prefix}payment_claims/{SHA-256 of the claim}``
    The replay record of on-chain payments (``storage.claim_payment``):
    ``claim``, ``owner`` (one settlement attempt for an MPP challenge), ``value``,
    ``created_at``. ``create`` fails if the document exists, which makes a claim
    atomic. ``bind_tempo_payment`` keeps the binding of a Tempo challenge to the
    transaction that pays it here too (claim ``calotype:tempo-challenge:<id>``,
    owner the transaction hash), created in the same batch as the purchase.
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
``{prefix}funnel/{YYYY-MM-DD}``
    The funnel counts of one UTC day (``storage.FunnelDay``, ``funnel.py``):
    ``day`` and one count per step that happened that day. Each step is one
    ``Increment`` write. Never a URL, key, user agent or IP address.
``{prefix}oauth_codes/{SHA-256 of the code}``
    One short-lived OAuth authorization code (``storage.OAuthCode``): the
    client id's hash, redirect address, PKCE challenge, resource, the sealed
    key and ``expires_at`` (Unix seconds). Taken once, in a transaction that
    reads and deletes it; codes that expired unused are deleted when a new
    one is saved (a single-field range query, so no index to deploy).

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

Prepaid credit: taking one credit (``consume_credit``) works like
``try_consume`` turned round: read the balance and refuse without a write if it
is zero, otherwise apply ``balance - 1`` and undo it if the result went below
zero. Recording a purchase is one atomic batch: ``create`` the purchase
document (which fails if it exists) and ``Increment`` the balance, together or
not at all, so a purchase is added once however often and however much in
parallel the call is repeated. Refunding one runs in a transaction that reads
the purchase first, so it too happens once; it is a rare operator action.
A Tempo purchase is bound to its challenge in one batch (``create`` the purchase
and ``create`` the challenge's claim) and credited later in a transaction that
reads the purchase, moves it from "paid" to "credited" and increments the
balance; both happen at most once per transaction hash.

Queries use only single-field filters and sorts, which Firestore indexes
automatically, so no composite index has to be deployed.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import Aborted, AlreadyExists, FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from .storage import (
    BOUND,
    BOUND_ALREADY,
    CHALLENGE_TAKEN,
    PURCHASE_CREDITED,
    PURCHASE_PAID,
    PURCHASE_PENDING,
    PURCHASE_STATUSES,
    TEMPO_CHALLENGE_CLAIM,
    TRANSACTION_TAKEN,
    _check_purchase_changes,
    KEY_REF,
    NOTICE_PENDING,
    REF_LABEL_PREFIX,
    REF_TAG,
    FUNNEL_EVENTS,
    TRY_OUTCOMES,
    ApiKey,
    Contact,
    CreditPurchase,
    FunnelDay,
    Notice,
    OAuthCode,
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
        self._credits = self._client.collection(f"{collection_prefix}credits")
        self._purchases = self._client.collection(f"{collection_prefix}credit_purchases")
        self._notices = self._client.collection(f"{collection_prefix}notices")
        self._tries = self._client.collection(f"{collection_prefix}tries")
        self._funnel = self._client.collection(f"{collection_prefix}funnel")
        self._codes = self._client.collection(f"{collection_prefix}oauth_codes")
        self._claims = self._client.collection(f"{collection_prefix}payment_claims")

    # --- plumbing -----------------------------------------------------------------

    def init(self) -> None:
        """Nothing to create: Firestore collections exist once written to."""

    def close(self) -> None:
        """The client holds no state that needs closing between requests."""

    def collections(self) -> list[Any]:
        """The collections this store writes to (used by tests and cleanup)."""
        return [
            self._keys, self._usage, self._counter.parent, self._events,
            self._credits, self._purchases, self._notices, self._tries, self._funnel, self._codes,
            self._claims,
        ]

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

    def _write_if_unchanged(self, ref, decide, *, attempts: int = 10):
        """Read ``ref``, let ``decide(doc)`` say what to write, and write it only if the document is unchanged.

        ``decide`` returns None (write nothing; the result is ``(None, doc)``)
        or ``(result, changes, extra)``: ``changes`` for ``ref`` and ``extra``
        a list of ``(ref, fields)`` merged in the same atomic commit. The write
        carries the snapshot's update time as a precondition, so a parallel
        change makes it fail whole; it is then read and decided again. No lock
        is held, so parallel callers never wait on one another (a transaction's
        lock can time out under a burst). Returns ``(result, doc)``, or
        ``(None, None)`` if the document does not exist.
        """
        for attempt in range(attempts):
            snap = ref.get()
            if not snap.exists:
                return None, None
            doc = snap.to_dict()
            decided = decide(doc)
            if decided is None:
                return None, doc
            result, changes, extra = decided
            batch = self._client.batch()
            batch.update(ref, changes, option=self._client.write_option(last_update_time=snap.update_time))
            for other, fields in extra:
                batch.set(other, fields, merge=True)
            try:
                batch.commit()
            except (FailedPrecondition, Aborted):
                time.sleep(0.02 * (2**min(attempt, 5)))
                continue
            return result, {**doc, **changes}
        raise Aborted(f"could not update {ref.id} after retries")

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
        """Delete the key's document (with its email address), its usage and notice documents,
        and its credit and purchase records.

        False if there was no such key. The other documents go first, so a run cut short leaves the key document
        to find and delete again. The counter in ``meta/key_ids`` is not
        lowered, so the id is never handed out again.
        """
        ref = self._key_ref(key_id)
        exists = ref.get().exists
        usage = self._usage.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        purchases = self._purchases.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        notices = self._notices.where(filter=FieldFilter("key_id", "==", int(key_id))).stream()
        batch = self._client.batch()
        batch.delete(self._credit_ref(key_id))
        pending = 1
        for doc in [*usage, *purchases, *notices]:
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
        purchase_ref, credit_ref = self._purchase_ref(purchase_id), self._credit_ref(key_id)
        if not self._key_ref(key_id).get().exists:
            return False, 0
        record = _purchase_record(
            purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method,
            payment_intent=payment_intent, status=status, tempo_terms=tempo_terms,
        )
        if status != PURCHASE_CREDITED:
            try:
                purchase_ref.create(record)
            except AlreadyExists:
                return False, self.get_credit_balance(key_id)
            return True, self.get_credit_balance(key_id)
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
        """Bind a Tempo challenge to one transaction and record its purchase, as one batch (see storage.py)."""
        if screenshots <= 0 or not challenge_id:
            raise ValueError("a Tempo purchase needs screenshots and a challenge")
        if status not in (PURCHASE_PENDING, PURCHASE_PAID):
            raise ValueError("a bound purchase starts pending or paid")
        if not self._key_ref(key_id).get().exists:
            return TRANSACTION_TAKEN, None
        purchase_ref = self._purchase_ref(purchase_id)
        claim = TEMPO_CHALLENGE_CLAIM + challenge_id
        claim_ref = self._claim_ref(claim)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        record = _purchase_record(
            purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, "tempo",
            status=status, tempo_terms=tempo_terms, valid_before=valid_before,
        )
        for attempt in range(6):
            # Both documents are created together or neither is: no read, so parallel calls do not contend.
            batch = self._client.batch()
            batch.create(purchase_ref, record)
            batch.create(claim_ref, {"claim": claim, "owner": purchase_id, "value": purchase_id, "created_at": now})
            try:
                batch.commit()
                return BOUND, _doc_to_purchase(record)
            except AlreadyExists:
                pass
            except Aborted:
                time.sleep(0.05 * (2**attempt))
            claim_snap, purchase_snap = claim_ref.get(), purchase_ref.get()
            if claim_snap.exists and claim_snap.get("owner") != purchase_id:
                other = self._purchase_ref(claim_snap.get("owner")).get()
                return CHALLENGE_TAKEN, _doc_to_purchase(other.to_dict()) if other.exists else None
            if purchase_snap.exists:
                found = _doc_to_purchase(purchase_snap.to_dict())
                same = claim_snap.exists and found.challenge_id == challenge_id and found.key_id == key_id
                return (BOUND_ALREADY if same else TRANSACTION_TAKEN), found
            if claim_snap.exists:
                # The claim without its purchase cannot come from this method; make the purchase alone.
                try:
                    purchase_ref.create(record)
                    return BOUND, _doc_to_purchase(record)
                except AlreadyExists:
                    continue
        raise Aborted(f"could not bind purchase {purchase_id} after retries")

    def update_credit_purchase(
        self, purchase_id: str, changes: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> CreditPurchase | None:
        """Change a purchase's mutable fields, only while its status is one of ``only_if_status`` if given."""
        _check_purchase_changes(changes)
        if not changes:
            raise ValueError("nothing to change")

        def decide(doc):
            if only_if_status is not None and (doc.get("status") or PURCHASE_CREDITED) not in only_if_status:
                return None
            return True, dict(changes), []

        done, doc = self._write_if_unchanged(self._purchase_ref(purchase_id), decide)
        return _doc_to_purchase(doc) if done else None

    def credit_tempo_purchase(self, purchase_id: str, payment_intent: str) -> tuple[bool, int]:
        """Release a paid Tempo purchase: link its PaymentIntent and add its screenshots, as one step."""

        def decide(doc):
            if (
                doc.get("status") != PURCHASE_PAID
                or doc.get("refunded_at") is not None
                or doc.get("payment_intent") not in (None, payment_intent)
            ):
                return None
            credit = {"key_id": int(doc["key_id"]), "balance": firestore.Increment(int(doc["screenshots"]))}
            return (
                True,
                {"status": PURCHASE_CREDITED, "payment_intent": payment_intent},
                [(self._credit_ref(doc["key_id"]), credit)],
            )

        changed, doc = self._write_if_unchanged(self._purchase_ref(purchase_id), decide)
        if doc is None:
            return False, 0
        return bool(changed), self.get_credit_balance(int(doc["key_id"]))

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
            # A purchase never credited (a Tempo payment not yet matched, or unmatched) gave nothing.
            credited = (doc.get("status") or PURCHASE_CREDITED) == PURCHASE_CREDITED
            removed = min(int(doc["screenshots"]), max(stored, 0)) if credited else 0
            if credit.exists and removed:
                t.update(credit_ref, {"balance": stored - removed})
            changes = {"refunded_at": now, "credits_removed": removed}
            t.update(purchase_ref, changes)
            return _doc_to_purchase({**doc, **changes}), True

        return refund(self._client.transaction())

    def set_credit_refund_amount(self, purchase_id: str, amount_cents: int) -> int | None:
        """Fix the amount to refund for a refunded purchase, the first time only (see storage.py)."""
        ref = self._purchase_ref(purchase_id)

        @firestore.transactional
        def fix(t) -> int | None:
            snap = ref.get(transaction=t)
            if not snap.exists or snap.get("refunded_at") is None:
                return None
            fixed = snap.to_dict().get("refund_amount_cents")
            if fixed is not None:
                return int(fixed)
            t.update(ref, {"refund_amount_cents": int(amount_cents)})
            return int(amount_cents)

        return fix(self._client.transaction())

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

    def set_credit_payment_intent(self, purchase_id: str, payment_intent: str) -> bool:
        """Link a purchase to the PaymentIntent that records it in Stripe. Never replaces another one."""

        def link(doc):
            if doc.get("payment_intent") is not None:
                return None  # linked already: to this one (True below) or to another (False)
            return True, {"payment_intent": payment_intent}, []

        done, doc = self._write_if_unchanged(self._purchase_ref(purchase_id), link)
        return bool(done) or (doc is not None and doc.get("payment_intent") == payment_intent)

    # --- payment claims: the replay record of on-chain payments (tempo.py) -------------------

    def _claim_ref(self, claim: str):
        return self._claims.document(hashlib.sha256(claim.encode("utf-8")).hexdigest())

    def claim_payment(self, claim: str, owner: str, value: str) -> bool:
        """Claim ``claim`` for ``owner``, atomically. True if it was free or is already ``owner``'s."""
        ref = self._claim_ref(claim)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for attempt in range(6):
            try:
                ref.create({"claim": claim, "owner": owner, "value": value, "created_at": now})
                return True
            except AlreadyExists:
                snap = ref.get()
                return snap.exists and snap.get("owner") == owner
            except Aborted:
                # Parallel creates of one document contend for its lock (as in
                # mark_event_processed). If another caller's create won, it owns the claim.
                snap = ref.get()
                if snap.exists:
                    return snap.get("owner") == owner
                time.sleep(0.05 * (2**attempt))
        raise Aborted(f"could not claim {claim} after retries")

    def get_payment_claim(self, claim: str) -> tuple[str, str] | None:
        """(owner, value) of a claim, or None."""
        snap = self._claim_ref(claim).get()
        return (snap.get("owner"), snap.get("value")) if snap.exists else None

    def release_payment_claim(self, claim: str, owner: str) -> None:
        """Give up a claim, only if ``owner`` holds it."""
        ref = self._claim_ref(claim)

        @firestore.transactional
        def release(t) -> None:
            snap = ref.get(transaction=t)
            if snap.exists and snap.get("owner") == owner:
                t.delete(ref)

        release(self._client.transaction())

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

    # --- funnel counts (funnel.py) --------------------------------------------------------

    def record_funnel(self, day: str, event: str) -> None:
        """Add one to the day's count for ``event`` (one of ``FUNNEL_EVENTS``), in one write."""
        if event not in FUNNEL_EVENTS:
            raise ValueError(f"unknown funnel event {event!r}")
        self._funnel.document(day).set({"day": day, event: firestore.Increment(1)}, merge=True)

    def funnel_days(self, days: list[str]) -> list[FunnelDay]:
        """The counts for each of ``days``, in that order; zeros for a day with none."""
        found = {}
        for snap in self._client.get_all([self._funnel.document(d) for d in days]):
            if snap.exists:
                doc = snap.to_dict()
                found[snap.id] = FunnelDay(snap.id, **{e: int(doc.get(e) or 0) for e in FUNNEL_EVENTS})
        return [found.get(d, FunnelDay(d)) for d in days]
    # --- OAuth authorization codes (oauth.py) ---------------------------------------

    def save_oauth_code(self, code: OAuthCode) -> None:
        """Store a new code (``create``: a code id is never reused), and delete expired ones."""
        now = int(time.time())
        expired = self._codes.where(filter=FieldFilter("expires_at", "<", now)).limit(50).stream()
        batch, pending = self._client.batch(), 0
        for doc in expired:
            batch.delete(doc.reference)
            pending += 1
        if pending:
            batch.commit()
        data = asdict(code)
        data["created_at"] = int(code.created_at or now)
        self._codes.document(_code_doc_id(code.id)).create(data)

    def take_oauth_code(self, code_id: str, now: int | None = None) -> OAuthCode | None:
        """Delete the code and return it, once. None if unknown, already taken or expired.

        The read and the delete run in one transaction, so of two instances
        taking the same code at once, only one gets it.
        """
        now = int(time.time()) if now is None else now
        try:
            ref = self._codes.document(_code_doc_id(code_id))
        except ValueError:
            return None

        @firestore.transactional
        def take(t) -> dict | None:
            snap = ref.get(transaction=t)
            if not snap.exists:
                return None
            t.delete(ref)
            return snap.to_dict()

        doc = take(self._client.transaction())
        if doc is None:
            return None
        names = {f.name for f in fields(OAuthCode)}
        code = OAuthCode(**{k: v for k, v in doc.items() if k in names})
        return code if int(code.expires_at) >= now else None

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


def _code_doc_id(code_id: str) -> str:
    # Always a SHA-256 hex digest; anything else is refused rather than used as a path.
    if len(code_id) != 64 or any(c not in "0123456789abcdef" for c in code_id):
        raise ValueError("not a code id")
    return code_id


def _purchase_doc_id(purchase_id: str) -> str:
    # Stripe ids are letters, digits and underscores, transaction hashes 0x and hex. Anything else could name
    # another path, so it is refused rather than escaped.
    if not purchase_id or "/" in purchase_id or purchase_id in (".", "..") or purchase_id.startswith("__"):
        raise ValueError("not a usable purchase id")
    return purchase_id


def _purchase_record(
    purchase_id, key_id, pack, screenshots, amount_cents, currency, challenge_id, method, *,
    payment_intent=None, status=PURCHASE_CREDITED, tempo_terms=None, valid_before=None,
) -> dict:
    return {
        "id": purchase_id,
        "key_id": int(key_id),
        "pack": pack,
        "screenshots": int(screenshots),
        "amount_cents": int(amount_cents),
        "currency": currency,
        "challenge_id": challenge_id,
        "method": method,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "refunded_at": None,
        "credits_removed": 0,
        "refund_id": None,
        "payment_intent": payment_intent,
        "status": status,
        "tempo_terms": tempo_terms,
        "valid_before": None if valid_before is None else int(valid_before),
        "stripe_attempt": 0,
        "stripe_retry_at": None,
        "refund_amount_cents": None,
        "refund_status": None,
    }


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
        payment_intent=d.get("payment_intent"),
        status=d.get("status") or PURCHASE_CREDITED,
        tempo_terms=d.get("tempo_terms"),
        valid_before=None if d.get("valid_before") is None else int(d["valid_before"]),
        stripe_attempt=int(d.get("stripe_attempt") or 0),
        stripe_retry_at=d.get("stripe_retry_at"),
        refund_amount_cents=None if d.get("refund_amount_cents") is None else int(d["refund_amount_cents"]),
        refund_status=d.get("refund_status"),
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
        email=d.get("email"),
    )
