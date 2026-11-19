"""The Storage contract, run against every backend (see the ``store`` fixture).

Firestore runs only when FIRESTORE_EMULATOR_HOST points at the emulator.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from calotype import admin
from calotype.config import Settings
from calotype.storage import SQLiteStorage, hash_key, open_configured_storage

from .conftest import FIRESTORE_TEST_PROJECT, needs_firestore_emulator, stored_key_fields, stored_text

MONTH = "2026-11"


# --- keys ---------------------------------------------------------------------------


def test_create_and_get_key(store):
    raw, key = store.create_key("starter", "label")
    assert raw.startswith("cal_")
    got = store.get_key(raw)
    assert got == key
    assert (got.plan, got.label, got.revoked, got.internal, got.successful_screenshots) == (
        "starter", "label", False, False, 0
    )
    assert got.created_at.endswith("+00:00")
    assert store.get_key("cal_not-a-key") is None
    assert store.get_key(hash_key(raw)) is None  # the hash is not a key


def test_ids_are_increasing_integers_in_creation_order(store):
    ids = [store.create_key("free")[1].id for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5 and all(isinstance(i, int) for i in ids)
    assert [k.id for k in store.list_keys()] == ids


def test_internal_flag(store):
    raw, key = store.create_key("free", "ops", internal=True)
    assert key.internal and store.get_key(raw).internal


def test_revoke_key(store):
    raw, key = store.create_key("free")
    assert store.revoke_key(key.id) is True
    assert store.get_key(raw).revoked is True
    assert store.revoke_key(key.id) is False  # already revoked
    assert store.revoke_key(999_999) is False


def test_delete_key_removes_the_record_and_its_usage(store):
    keep_raw, keep = store.create_key("free", "keep")
    store.try_consume(keep.id, MONTH, 10)
    raw, key = store.create_key("starter", "to delete")  # the newest key, so id reuse would show
    store.set_billing(key.id, "starter", customer_id="cus_del", subscription_id="sub_del")
    for month in ("2026-10", MONTH):
        store.try_consume(key.id, month, 10)
    store.record_success(key.id)

    assert store.delete_key(key.id) is True
    assert store.get_key(raw) is None and store.get_key_by_id(key.id) is None
    assert store.find_key_by_subscription("sub_del") is None
    assert store.get_usage(key.id, MONTH) == 0 and store.get_usage(key.id, "2026-10") == 0
    assert [k.id for k in store.list_keys()] == [keep.id]
    assert key.id not in [c.key_id for c in store.list_contacts()]
    dump = stored_text(store)
    assert hash_key(raw) not in dump and "cus_del" not in dump and "sub_del" not in dump and "to delete" not in dump
    # Other keys are untouched, and the id is never handed out again.
    assert store.get_key(keep_raw) == keep and store.get_usage(keep.id, MONTH) == 1
    _, newer = store.create_key("free")
    assert newer.id > key.id


def test_delete_key_unknown_or_twice(store):
    _, key = store.create_key("free")
    assert store.delete_key(key.id) is True
    assert store.delete_key(key.id) is False
    assert store.delete_key(999_999) is False


def test_set_plan(store):
    raw, key = store.create_key("free")
    assert store.set_plan(key.id, "pro") is True
    assert store.get_key(raw).plan == "pro"
    assert store.set_plan(999_999, "pro") is False


def test_stores_only_the_hash_and_the_listed_fields(store):
    raw, key = store.create_key("free", "")
    store.try_consume(key.id, MONTH, 10)
    store.record_success(key.id)
    dump = stored_text(store)
    assert raw not in dump and hash_key(raw) in dump
    assert stored_key_fields(store) == {
        "id", "key_hash", "plan", "label", "created_at", "revoked_at", "internal", "successful_screenshots",
        "stripe_customer_id", "stripe_subscription_id",
    }


# --- billing (Stripe ids and processed webhook events) ----------------------------------


def test_new_keys_have_no_stripe_ids(store):
    raw, key = store.create_key("free")
    assert (key.stripe_customer_id, key.stripe_subscription_id) == (None, None)
    assert store.get_key(raw) == key


def test_get_key_by_id(store):
    raw, key = store.create_key("free", "x")
    assert store.get_key_by_id(key.id) == store.get_key(raw)
    assert store.get_key_by_id(999_999) is None


def test_set_billing_sets_plan_and_ids_together(store):
    raw, key = store.create_key("free")
    assert store.set_billing(key.id, "starter", customer_id="cus_A1", subscription_id="sub_A1") is True
    got = store.get_key(raw)
    assert (got.plan, got.stripe_customer_id, got.stripe_subscription_id) == ("starter", "cus_A1", "sub_A1")
    assert store.get_key_by_id(key.id) == got
    # Cancellation: back to free, the ids stay so the portal and a later checkout reuse the customer.
    assert store.set_billing(key.id, "free", customer_id="cus_A1", subscription_id="sub_A1") is True
    assert store.get_key(raw).plan == "free" and store.get_key(raw).stripe_customer_id == "cus_A1"
    assert store.set_billing(999_999, "pro", customer_id="cus_X", subscription_id="sub_X") is False
    # set_plan (the admin CLI) leaves the ids alone.
    assert store.set_plan(key.id, "pro") is True
    assert store.get_key(raw).stripe_subscription_id == "sub_A1"


def test_find_key_by_subscription(store):
    _, a = store.create_key("free")
    _, b = store.create_key("free")
    store.set_billing(b.id, "pro", customer_id="cus_B", subscription_id="sub_B")
    assert store.find_key_by_subscription("sub_B").id == b.id
    assert store.find_key_by_subscription("sub_none") is None
    assert store.get_key_by_id(a.id).stripe_subscription_id is None


def test_mark_event_processed_once(store):
    assert store.event_processed("evt_1") is False
    assert store.mark_event_processed("evt_1") is True
    assert store.event_processed("evt_1") is True
    assert store.mark_event_processed("evt_1") is False
    assert store.event_processed("evt_2") is False


def test_parallel_mark_event_processed_has_one_winner(store):
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: store.mark_event_processed("evt_race"), range(16)))
    assert results.count(True) == 1


def test_billing_stores_ids_only(store):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_Q", subscription_id="sub_Q")
    store.mark_event_processed("evt_Q")
    dump = stored_text(store)
    assert raw not in dump
    assert "cus_Q" in dump and "sub_Q" in dump and "evt_Q" in dump


def test_sqlite_upgrades_an_old_database(tmp_path):
    """A database made before billing gains the new columns and table on init."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL UNIQUE, plan TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, revoked_at TEXT,
            internal INTEGER NOT NULL DEFAULT 0, successful_screenshots INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE usage (key_id INTEGER NOT NULL, month TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key_id, month));
        """
    )
    db.execute(
        "INSERT INTO api_keys (key_hash, plan, created_at) VALUES (?, 'free', '2026-11-01T00:00:00+00:00')",
        (hash_key("cal_old"),),
    )
    db.commit()
    db.close()

    store = SQLiteStorage(path)
    store.init()
    store.init()  # twice: the upgrade is idempotent
    try:
        old = store.get_key("cal_old")
        assert old.plan == "free" and old.stripe_customer_id is None
        assert store.set_billing(old.id, "pro", customer_id="cus_O", subscription_id="sub_O")
        assert store.find_key_by_subscription("sub_O").id == old.id
        assert store.mark_event_processed("evt_O") is True
    finally:
        store.close()


# --- usage --------------------------------------------------------------------------


def test_try_consume_counts_up_to_the_limit(store):
    _, key = store.create_key("free")
    assert store.get_usage(key.id, MONTH) == 0
    assert [store.try_consume(key.id, MONTH, 3) for _ in range(4)] == [(True, 1), (True, 2), (True, 3), (False, 3)]
    assert store.get_usage(key.id, MONTH) == 3
    assert store.get_usage(key.id, "2026-12") == 0


def test_zero_limit_refuses_without_counting(store):
    _, key = store.create_key("free")
    assert store.try_consume(key.id, MONTH, 0) == (False, 0)
    assert store.get_usage(key.id, MONTH) == 0


def test_lowered_limit_refuses_and_keeps_count(store):
    _, key = store.create_key("free")
    for _ in range(5):
        store.try_consume(key.id, MONTH, 10)
    assert store.try_consume(key.id, MONTH, 3) == (False, 5)
    assert store.get_usage(key.id, MONTH) == 5


def test_refund(store):
    _, key = store.create_key("free")
    store.try_consume(key.id, MONTH, 2)
    store.try_consume(key.id, MONTH, 2)
    assert store.try_consume(key.id, MONTH, 2)[0] is False
    store.refund(key.id, MONTH)
    assert store.get_usage(key.id, MONTH) == 1
    assert store.try_consume(key.id, MONTH, 2) == (True, 2)


def test_refund_never_goes_below_zero(store):
    _, key = store.create_key("free")
    store.refund(key.id, MONTH)  # nothing counted yet
    assert store.get_usage(key.id, MONTH) == 0
    store.try_consume(key.id, MONTH, 5)
    store.refund(key.id, MONTH)
    store.refund(key.id, MONTH)
    assert store.get_usage(key.id, MONTH) == 0
    assert store.try_consume(key.id, MONTH, 1) == (True, 1)


def test_parallel_consumes_never_pass_the_limit(store):
    _, key = store.create_key("free")
    threads, limit, calls = 16, 25, 64  # calls: a multiple of threads, so every barrier round fills
    barrier = threading.Barrier(threads)

    def consume(_):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return store.try_consume(key.id, MONTH, limit)

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(consume, range(calls)))
    allowed = sorted(count for ok, count in results if ok)
    assert allowed == list(range(1, limit + 1))  # each slot handed out exactly once
    assert all(count <= limit for ok, count in results if not ok)
    assert store.get_usage(key.id, MONTH) == limit


def test_parallel_key_creation_gives_unique_ids(store):
    with ThreadPoolExecutor(8) as pool:
        ids = [k.id for _, k in pool.map(lambda _: store.create_key("free"), range(24))]
    assert len(set(ids)) == 24
    assert sorted(k.id for k in store.list_keys()) == sorted(ids)


# --- successes and contacts ------------------------------------------------------------


def test_record_success(store):
    raw, key = store.create_key("free")
    assert store.record_success(key.id) == 1
    assert store.record_success(key.id) == 2
    assert store.get_key(raw).successful_screenshots == 2
    assert store.record_success(999_999) == 0


def test_parallel_record_success_sees_one_first(store):
    _, key = store.create_key("free")
    with ThreadPoolExecutor(8) as pool:
        totals = list(pool.map(lambda _: store.record_success(key.id), range(20)))
    assert sorted(totals) == list(range(1, 21))  # exactly one caller sees 1


def test_contacts_follow_creation_order_and_skip_internal(store):
    _, internal = store.create_key("free", "ops", internal=True)
    _, a = store.create_key("free")
    _, b = store.create_key("starter")
    _, never = store.create_key("free")
    store.record_success(internal.id)
    store.record_success(b.id)  # b succeeds first, but a was created first
    store.record_success(a.id)
    store.record_success(a.id)
    contacts = store.list_contacts()
    assert [(c.ordinal, c.key_id, c.plan, c.successful_screenshots) for c in contacts] == [
        (1, a.id, "free", 2),
        (2, b.id, "starter", 1),
    ]
    assert contacts[0].created_at == a.created_at
    assert store.contact_ordinal(b.id) == 2
    assert store.contact_ordinal(never.id) is None
    assert store.contact_ordinal(internal.id) is None


# --- choosing the backend ---------------------------------------------------------------


def test_backend_defaults_to_sqlite(tmp_path, monkeypatch):
    monkeypatch.delenv("CALOTYPE_STORAGE", raising=False)
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "x.db"))
    settings = Settings.from_env()
    assert settings.storage_backend == "sqlite"
    store = open_configured_storage(settings)
    assert isinstance(store, SQLiteStorage)
    store.close()


def test_backend_settings_from_env(monkeypatch):
    monkeypatch.setenv("CALOTYPE_STORAGE", "Firestore")
    monkeypatch.setenv("CALOTYPE_FIRESTORE_PROJECT", "some-project")
    monkeypatch.setenv("CALOTYPE_FIRESTORE_COLLECTION_PREFIX", "staging_")
    s = Settings.from_env()
    assert (s.storage_backend, s.firestore_project, s.firestore_collection_prefix) == (
        "firestore", "some-project", "staging_"
    )
    monkeypatch.delenv("CALOTYPE_FIRESTORE_COLLECTION_PREFIX")
    assert Settings.from_env().firestore_collection_prefix == "calotype_"


def test_unknown_backend_is_refused(monkeypatch):
    monkeypatch.setenv("CALOTYPE_STORAGE", "postgres")
    with pytest.raises(ValueError, match="CALOTYPE_STORAGE"):
        Settings.from_env()


def test_collection_prefix_may_not_contain_a_slash():
    from calotype.firestore_storage import FirestoreStorage

    with pytest.raises(ValueError, match="may not contain"):
        FirestoreStorage(collection_prefix="a/b", client=object())


@needs_firestore_emulator
def test_firestore_selected_by_env_and_admin_cli(monkeypatch, capsys, firestore_client, firestore_prefix):
    from calotype.firestore_storage import FirestoreStorage

    monkeypatch.setenv("CALOTYPE_STORAGE", "firestore")
    monkeypatch.setenv("CALOTYPE_FIRESTORE_PROJECT", FIRESTORE_TEST_PROJECT)
    monkeypatch.setenv("CALOTYPE_FIRESTORE_COLLECTION_PREFIX", firestore_prefix)
    store = open_configured_storage(Settings.from_env())
    try:
        assert isinstance(store, FirestoreStorage) and store.prefix == firestore_prefix

        assert admin.main(["create-key", "--plan", "pro", "--label", "ops"]) == 0
        raw = capsys.readouterr().out.strip()
        key = store.get_key(raw)
        assert key.internal is True and key.plan == "pro"
        store.record_success(key.id)
        _, ext = store.create_key("free")
        store.record_success(ext.id)
        assert admin.main(["contacts"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[1].split("\t")[:2] == ["1", str(ext.id)] and len(lines) == 2
        assert admin.main(["revoke-key", str(key.id)]) == 0
        assert store.get_key(raw).revoked
    finally:
        for collection in store.collections():
            firestore_client.recursive_delete(collection)


def test_set_internal_takes_a_key_out_of_the_contacts(store):
    raw, key = store.create_key("free", "ref:dt-linkedin")  # made through the public button
    store.record_success(key.id)
    _, other = store.create_key("free")
    store.record_success(other.id)
    assert [c.key_id for c in store.list_contacts()] == [key.id, other.id]

    assert store.set_internal(key.id) is True
    got = store.get_key(raw)
    assert got.internal and got.label == "ref:dt-linkedin" and got.successful_screenshots == 1
    assert [(c.ordinal, c.key_id) for c in store.list_contacts()] == [(1, other.id)]
    assert store.contact_ordinal(key.id) is None

    assert store.set_internal(key.id, False) is True
    assert not store.get_key(raw).internal
    assert store.set_internal(999_999) is False


def test_tag_source_tags_an_untagged_key_once(store):
    raw, key = store.create_key("free")
    store.record_success(key.id)
    assert store.tag_source(key.id, "glama") is True
    assert store.get_key(raw).label == "ref:glama"
    assert [c.source for c in store.list_contacts()] == ["glama"]
    # The first tag stays.
    assert store.tag_source(key.id, "smithery") is False
    assert store.get_key(raw).label == "ref:glama"


def test_tag_source_leaves_labelled_internal_and_missing_keys_alone(store):
    raw_labelled, labelled = store.create_key("free", "ref:dt-x")
    raw_internal, internal = store.create_key("free", internal=True)
    raw_plain, plain = store.create_key("free")
    assert store.tag_source(labelled.id, "glama") is False
    assert store.get_key(raw_labelled).label == "ref:dt-x"
    assert store.tag_source(internal.id, "glama") is False
    assert store.get_key(raw_internal).label == ""
    assert store.tag_source(plain.id, "Not A Tag!") is False
    assert store.get_key(raw_plain).label == ""
    assert store.tag_source(999_999, "glama") is False


def test_find_key_by_ref(store):
    from calotype.storage import key_ref

    raw_a, a = store.create_key("free")
    raw_b, b = store.create_key("free")
    assert store.find_key_by_ref(key_ref(raw_a)).id == a.id
    assert store.find_key_by_ref(key_ref(raw_b)).id == b.id
    assert store.find_key_by_ref("0" * 12 if not key_ref(raw_a).startswith("0" * 12) else "f" * 12) is None
    for bad in ("", "abc", key_ref(raw_a).upper(), key_ref(raw_a) + "0", "zzzzzzzzzzzz", raw_a):
        assert store.find_key_by_ref(bad) is None, bad
