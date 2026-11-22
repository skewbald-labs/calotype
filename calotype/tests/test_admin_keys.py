"""The admin CLI's key lifecycle commands, run against every storage backend.

Revoking a paid key cancels its Stripe subscription. Nothing here reaches
Stripe: the provider wraps the ``FakeStripe`` stand-in from ``test_billing``.
"""

from __future__ import annotations

import pytest
import stripe

from calotype import admin
from calotype.billing import StripeBilling, StripeConfig, stripe_for_admin
from calotype.config import Settings
from calotype.storage import open_configured_storage

from .conftest import BACKENDS, FIRESTORE_TEST_PROJECT
from .test_billing import ENV, PRICE_STARTER, SECRET_KEY, FakeStripe, subscription

NO_PRORATION = {"prorate": False, "invoice_now": False}


@pytest.fixture(params=BACKENDS)
def admin_store(request, monkeypatch, tmp_path):
    """The store the admin CLI opens from the environment, for each backend."""
    for name in ("CALOTYPE_STORAGE", "CALOTYPE_DATABASE_URL", "CALOTYPE_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "sqlite":
        monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "admin.db"))
    else:
        request.getfixturevalue("firestore_client")  # skips without the emulator
        monkeypatch.setenv("CALOTYPE_STORAGE", "firestore")
        monkeypatch.setenv("CALOTYPE_FIRESTORE_PROJECT", FIRESTORE_TEST_PROJECT)
        monkeypatch.setenv("CALOTYPE_FIRESTORE_COLLECTION_PREFIX", request.getfixturevalue("firestore_prefix"))
    store = open_configured_storage(Settings.from_env())
    yield store
    if request.param == "firestore":
        client = request.getfixturevalue("firestore_client")
        for collection in store.collections():
            client.recursive_delete(collection)


@pytest.fixture
def fake_stripe(monkeypatch) -> FakeStripe:
    """Stripe for the admin CLI, as configured on the server, over the stand-in client."""
    fake = FakeStripe()
    config, _ = StripeConfig.from_env(ENV)
    monkeypatch.setattr(admin, "open_billing", lambda: StripeBilling(config, client=fake.client))
    return fake


@pytest.fixture
def no_stripe_allowed(monkeypatch):
    """Fails the test if the command reaches for Stripe at all."""

    def refuse():
        raise AssertionError("Stripe was used")

    monkeypatch.setattr(admin, "open_billing", refuse)


def paid_key(store, sub: str = "sub_1", plan: str = "starter"):
    raw, key = store.create_key("free")
    store.set_billing(key.id, plan, customer_id="cus_1", subscription_id=sub)
    return raw, store.get_key_by_id(key.id)


# --- revoke-key ---------------------------------------------------------------------


def test_revoke_free_key_never_touches_stripe(admin_store, no_stripe_allowed, capsys):
    raw, key = admin_store.create_key("free")
    assert admin.main(["revoke-key", str(key.id)]) == 0
    assert admin_store.get_key(raw).revoked
    assert "revoked" in capsys.readouterr().err


def test_revoke_paid_key_cancels_the_subscription_now(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    assert admin.main(["revoke-key", str(key.id)]) == 0
    fake_stripe.client.v1.subscriptions.cancel.assert_called_once_with("sub_1", params=NO_PRORATION)
    fake_stripe.client.v1.subscriptions.update.assert_not_called()
    assert admin_store.get_key(raw).revoked
    err = capsys.readouterr().err
    assert "Cancelled Stripe subscription sub_1 now" in err and "no prorated refund" in err


def test_revoke_paid_key_at_period_end(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    assert admin.main(["revoke-key", str(key.id), "--at-period-end"]) == 0
    fake_stripe.client.v1.subscriptions.update.assert_called_once_with(
        "sub_1", params={"cancel_at_period_end": True}
    )
    fake_stripe.client.v1.subscriptions.cancel.assert_not_called()
    assert admin_store.get_key(raw).revoked
    assert "end of the month already paid for" in capsys.readouterr().err


def test_revoke_paid_key_whose_subscription_already_ended(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    assert admin.main(["revoke-key", str(key.id)]) == 0
    fake_stripe.client.v1.subscriptions.cancel.assert_not_called()
    assert admin_store.get_key(raw).revoked
    assert "had already ended" in capsys.readouterr().err


def test_revoke_paid_key_without_stripe_configured_refuses(admin_store, monkeypatch, capsys):
    monkeypatch.setattr(admin, "open_billing", stripe_for_admin)  # the real one; the env has no Stripe
    raw, key = paid_key(admin_store)
    assert admin.main(["revoke-key", str(key.id)]) == 1
    assert not admin_store.get_key(raw).revoked
    err = capsys.readouterr().err
    assert "STRIPE_SECRET_KEY is not set" in err and "--no-stripe" in err and "Nothing was changed" in err


def test_revoke_paid_key_with_no_stripe_revokes_and_warns(admin_store, no_stripe_allowed, capsys):
    raw, key = paid_key(admin_store)
    assert admin.main(["revoke-key", str(key.id), "--no-stripe"]) == 0
    assert admin_store.get_key(raw).revoked
    err = capsys.readouterr().err
    assert "WARNING" in err and "sub_1" in err and "by hand" in err


def test_revoke_paid_key_stripe_failure_changes_nothing(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    fake_stripe.client.v1.subscriptions.cancel.side_effect = stripe.APIConnectionError("network down")
    assert admin.main(["revoke-key", str(key.id)]) == 1
    assert not admin_store.get_key(raw).revoked
    err = capsys.readouterr().err
    assert "could not cancel" in err and "network down" not in err


def test_revoke_can_be_retried_to_cancel_a_skipped_subscription(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    assert admin.main(["revoke-key", str(key.id), "--no-stripe"]) == 0
    fake_stripe.client.v1.subscriptions.cancel.assert_not_called()
    assert admin.main(["revoke-key", str(key.id)]) == 0
    fake_stripe.client.v1.subscriptions.cancel.assert_called_once_with("sub_1", params=NO_PRORATION)
    assert "already revoked" in capsys.readouterr().err


def test_revoke_unknown_or_already_revoked_key(admin_store, no_stripe_allowed):
    assert admin.main(["revoke-key", "999999"]) == 1
    _, key = admin_store.create_key("free")
    admin_store.revoke_key(key.id)
    assert admin.main(["revoke-key", str(key.id)]) == 1


# --- the Stripe provider for the CLI --------------------------------------------------


def test_stripe_for_admin_needs_only_the_secret_key():
    assert stripe_for_admin({}) is None
    assert stripe_for_admin({"STRIPE_SECRET_KEY": "  "}) is None
    provider = stripe_for_admin({"STRIPE_SECRET_KEY": SECRET_KEY}, client=object())
    assert isinstance(provider, StripeBilling) and provider.plans_for_sale() == []
    assert SECRET_KEY not in repr(provider.config)
    full = stripe_for_admin(ENV, client=object())
    assert full.plans_for_sale() == ["starter", "pro"]


def test_cancel_subscription_refuses_a_malformed_id():
    provider = stripe_for_admin({"STRIPE_SECRET_KEY": SECRET_KEY}, client=FakeStripe().client)
    for bad in ("", "sub", "../sub_1", "sub_1/x"):
        with pytest.raises(ValueError):
            provider.cancel_subscription(bad)


# --- delete-key ---------------------------------------------------------------------


def test_delete_key_without_yes_only_shows_what_it_would_delete(admin_store, no_stripe_allowed, capsys):
    raw, key = paid_key(admin_store)
    admin_store.try_consume(key.id, "2026-11", 10)
    assert admin.main(["delete-key", str(key.id)]) == 0
    err = capsys.readouterr().err
    assert f"key {key.id}" in err and "usage counts" in err and "sub_1" in err and "--yes" in err
    assert admin_store.get_key(raw) is not None and admin_store.get_usage(key.id, "2026-11") == 1


def test_delete_free_key(admin_store, no_stripe_allowed, capsys):
    raw, key = admin_store.create_key("free")
    admin_store.try_consume(key.id, "2026-11", 10)
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 0
    assert admin_store.get_key(raw) is None and admin_store.get_usage(key.id, "2026-11") == 0
    err = capsys.readouterr().err
    assert "deleted" in err and "Stripe keeps" not in err


def test_delete_paid_key_cancels_first_and_notes_stripe_records(admin_store, fake_stripe, capsys):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 0
    fake_stripe.client.v1.subscriptions.cancel.assert_called_once_with("sub_1", params=NO_PRORATION)
    assert admin_store.get_key(raw) is None
    err = capsys.readouterr().err
    assert "Stripe keeps its own records" in err and "as the law requires" in err


def test_delete_paid_key_at_period_end(admin_store, fake_stripe):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    assert admin.main(["delete-key", str(key.id), "--yes", "--at-period-end"]) == 0
    fake_stripe.client.v1.subscriptions.update.assert_called_once_with(
        "sub_1", params={"cancel_at_period_end": True}
    )
    assert admin_store.get_key(raw) is None


def test_delete_paid_key_without_stripe_configured_refuses(admin_store, monkeypatch, capsys):
    monkeypatch.setattr(admin, "open_billing", stripe_for_admin)
    raw, key = paid_key(admin_store)
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 1
    assert admin_store.get_key(raw) is not None
    assert "STRIPE_SECRET_KEY is not set" in capsys.readouterr().err


def test_delete_paid_key_with_no_stripe_deletes_and_warns(admin_store, no_stripe_allowed, capsys):
    raw, key = paid_key(admin_store)
    assert admin.main(["delete-key", str(key.id), "--yes", "--no-stripe"]) == 0
    assert admin_store.get_key(raw) is None
    err = capsys.readouterr().err
    assert "WARNING" in err and "by hand" in err and "Stripe keeps its own records" in err


def test_delete_paid_key_stripe_failure_deletes_nothing(admin_store, fake_stripe):
    raw, key = paid_key(admin_store)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    fake_stripe.client.v1.subscriptions.cancel.side_effect = stripe.APIConnectionError("down")
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 1
    assert admin_store.get_key(raw) is not None


def test_delete_unknown_key(admin_store, no_stripe_allowed):
    assert admin.main(["delete-key", "999999", "--yes"]) == 1


# --- key-id --------------------------------------------------------------------------


def test_key_id_reads_the_key_from_stdin(admin_store, monkeypatch, capsys):
    import io

    raw, key = admin_store.create_key("free")
    monkeypatch.setattr("sys.stdin", io.StringIO(raw + "\n"))
    assert admin.main(["key-id"]) == 0
    assert capsys.readouterr().out.strip() == str(key.id)
    monkeypatch.setattr("sys.stdin", io.StringIO("cal_not_a_key\n"))
    assert admin.main(["key-id"]) == 1


def test_the_privacy_notice_describes_the_deletion_this_cli_does():
    from calotype.config import SITE_DIR

    text = " ".join((SITE_DIR / "pages" / "privacy.html").read_text().split())
    assert (
        "Within 7 days of your request, the key's record (with its email address, if it had one), "
        "all its monthly usage counts, its prepaid balance and purchase records, and its quota-email records are deleted"
    ) in text
    assert "subscription is cancelled first" in text and "Stripe keeps its own records" in text
    with pytest.raises(SystemExit) as done:
        admin.main(["delete-key", "--help"])
    assert done.value.code == 0


# --- mark-internal ---------------------------------------------------------------------


def test_mark_internal(admin_store, capsys):
    raw, key = admin_store.create_key("free")  # as the public button makes it
    admin_store.record_success(key.id)
    assert admin.main(["contacts"]) == 0
    assert str(key.id) in capsys.readouterr().out

    assert admin.main(["mark-internal", str(key.id)]) == 0
    assert admin_store.get_key(raw).internal
    assert "no longer counts" in capsys.readouterr().err
    assert admin.main(["contacts"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[1:] == []
    assert admin.main(["list-keys"]) == 0
    assert f"{key.id}\tfree\tactive\tinternal" in capsys.readouterr().out

    assert admin.main(["mark-internal", str(key.id)]) == 0
    assert "already internal" in capsys.readouterr().err
    assert admin.main(["mark-internal", "999999"]) == 1


# --- find-ref --------------------------------------------------------------------------


def test_find_ref_finds_the_key_its_holder_quotes(admin_store, capsys):
    from calotype.storage import key_ref

    raw, key = admin_store.create_key("free")
    assert admin.main(["find-ref", key_ref(raw).upper()]) == 0
    out = capsys.readouterr()
    assert f"key {key.id}:" in out.out and raw not in out.out + out.err
    assert admin.main(["find-ref", "000000000000" if not key_ref(raw).startswith("000000000000") else "ffffffffffff"]) == 1


def test_admin_tries_prints_daily_counts(admin_store, capsys):
    from calotype.storage import current_day

    day = current_day()
    admin_store.try_admit(day, 300)
    admin_store.record_try(day, "succeeded")
    admin_store.record_try(day, "refused")
    assert admin.main(["tries", "--days", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "day\ttotal\trendered\tsucceeded\tfailed\trefused\tlimited\tcapped"
    assert lines[1] == f"{day}\t2\t1\t1\t0\t1\t0\t0"
    assert len(lines) == 3 and lines[2].endswith("\t0\t0\t0\t0\t0\t0\t0")
    with pytest.raises(SystemExit):
        admin.main(["tries", "--days", "0"])


def test_admin_funnel_prints_daily_counts_and_a_total(admin_store, capsys):
    from datetime import UTC, datetime, timedelta

    from calotype.storage import FUNNEL_EVENTS, current_day

    today = current_day()
    yesterday = current_day(datetime.now(UTC) - timedelta(days=1))
    admin_store.record_funnel(today, "looked")
    admin_store.record_funnel(today, "key_made")
    admin_store.record_funnel(yesterday, "looked")
    assert admin.main(["funnel", "--days", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "day\t" + "\t".join(FUNNEL_EVENTS)
    assert lines[0].split("\t")[1:] == [
        "looked", "tool_no_key", "bad_key", "oauth_probe", "key_miss", "key_refused", "key_made", "first_capture",
    ]
    assert lines[1] == f"{today}\t1\t0\t0\t0\t0\t0\t1\t0"
    assert lines[2] == f"{yesterday}\t1\t0\t0\t0\t0\t0\t0\t0"
    assert lines[3].endswith("\t0\t0\t0\t0\t0\t0\t0\t0")
    assert lines[4] == "total\t2\t0\t0\t0\t0\t0\t1\t0"
    assert len(lines) == 5
    with pytest.raises(SystemExit):
        admin.main(["funnel", "--days", "0"])
