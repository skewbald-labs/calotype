"""Prepaid credit bought by machine payment: ``POST /v1/credits``, the ledger, the quota order.

Nothing here reaches Stripe. The Stripe client is a stand-in (a MagicMock, or
the real ``stripe.StripeClient`` over a fake HTTP transport), as in
``test_billing``. The buyer is played by ``pay`` below, which does what an MPP
client does: read the challenge from the 402, wrap a shared payment token in a
credential, send the request again. One test lets ``pympp``, the protocol's
official Python SDK, be the buyer instead, when it is installed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import stripe
from fastapi.testclient import TestClient

from calotype import admin, mpp
from calotype.app import create_app
from calotype.billing import StripeBilling, StripeConfig
from calotype.config import DEFAULT_PLANS_FILE, CreditPack, Plan, load_credit_packs
from calotype.credits import (
    CreditsConfig,
    NoCredits,
    StripeCredits,
    credits_for_admin,
    credits_from_env,
    derive_challenge_secret,
)
from calotype.ssrf import AddressPolicy
from calotype.storage import current_month, hash_key

from .conftest import fake_resolver, stored_text
from .test_admin_keys import admin_store  # noqa: F401  (a fixture: the store the admin CLI opens)
from .test_billing import ENV as BILLING_ENV
from .test_billing import SECRET_KEY, event, post_event

PROFILE = "profile_test_61Calotype"
CREDITS_ENV = {
    "CALOTYPE_CREDITS": "1",
    "CALOTYPE_CREDITS_CARD": "1",
    "STRIPE_SECRET_KEY": SECRET_KEY,
    "STRIPE_PROFILE_ID": PROFILE,
}
SPT = "spt_test_1N4Zv32eZvKYlo2C"
MONTH = current_month()


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


class FakePayments:
    """Stands in for ``stripe.StripeClient``: one PaymentIntent per idempotency key, as Stripe keeps them."""

    def __init__(self):
        self.client = MagicMock(name="StripeClient")
        self.intents: dict[str, dict] = {}  # by PaymentIntent id
        self.by_idempotency_key: dict[str, str] = {}
        self.status = "succeeded"
        self.fail_with: Exception | None = None
        self.client.v1.payment_intents.create.side_effect = self._create
        self.client.v1.payment_intents.retrieve.side_effect = lambda intent_id: dict(self.intents[intent_id])
        self.client.v1.refunds.create.side_effect = lambda params, options=None: SimpleNamespace(
            id=f"re_test_{len(self.refunds)}", status="succeeded"
        )

    def _create(self, params, options=None):
        if self.fail_with is not None:
            raise self.fail_with
        idem = (options or {}).get("idempotency_key")
        replayed = idem in self.by_idempotency_key
        if replayed:
            intent = self.intents[self.by_idempotency_key[idem]]
        else:
            intent = {
                "id": f"pi_test_{len(self.intents) + 1}",
                "object": "payment_intent",
                "status": self.status,
                "amount": params["amount"],
                "amount_received": params["amount"] if self.status == "succeeded" else 0,
                "currency": params["currency"],
                "metadata": dict(params["metadata"]),
            }
            self.intents[intent["id"]] = intent
            self.by_idempotency_key[idem] = intent["id"]
        headers = {"idempotent-replayed": "true"} if replayed else {}
        return SimpleNamespace(**intent, last_response=SimpleNamespace(headers=headers))

    @property
    def create(self) -> MagicMock:
        return self.client.v1.payment_intents.create

    @property
    def refunds(self) -> list:
        return self.client.v1.refunds.create.call_args_list


@pytest.fixture
def credits_config() -> CreditsConfig:
    config, missing = CreditsConfig.from_env(CREDITS_ENV)
    assert missing == []
    return config


@pytest.fixture
def payments() -> FakePayments:
    return FakePayments()


@pytest.fixture
def credits(credits_config, payments) -> StripeCredits:
    return StripeCredits(credits_config, client=payments.client)


@pytest.fixture
def billing(payments) -> StripeBilling:
    """Billing over the same stand-in, so the webhook endpoint (and its signature check) is on."""
    config, _ = StripeConfig.from_env(BILLING_ENV)
    return StripeBilling(config, client=payments.client)


def make_client(settings, store, renderer, credits, billing=None, **changes) -> TestClient:
    app = create_app(
        replace(settings, **changes) if changes else settings,
        policy=AddressPolicy(resolver=fake_resolver),
        storage=store,
        renderer=renderer,
        billing=billing,
        credits=credits,
    )
    return TestClient(app)


@pytest.fixture
def cclient(settings, store, fake_renderer, credits, billing):
    with make_client(settings, store, fake_renderer, credits, billing) as c:
        yield c


# --- the buyer -------------------------------------------------------------------------------------


def challenge_params(response) -> dict[str, str]:
    """The auth-params of the Payment challenge in a 402."""
    value = response.headers["www-authenticate"]
    assert value.startswith("Payment "), value
    return dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', value))


def credential(params: dict[str, str], payload: dict, **changes) -> str:
    """The credential an MPP client builds: the challenge echoed unchanged, and the method's payload."""
    echo = {k: v for k, v in params.items() if k != "description"}
    echo.update(changes)
    return "Payment " + mpp.b64url(json.dumps({"challenge": echo, "payload": payload}).encode())


def logged(caplog) -> str:
    """Every log line made so far, with its fields, as the JSON formatter would write them."""
    return json.dumps([{"message": r.getMessage(), **getattr(r, "fields", {})} for r in caplog.records], default=str)


def ask(c: TestClient, key: str, pack: str = "starter"):
    return c.post("/v1/credits", json={"pack": pack}, headers=auth(key))


def send(c: TestClient, key: str, credential_header: str, pack: str = "starter"):
    headers = {**auth(key), "Payment-Authorization": credential_header}
    return c.post("/v1/credits", json={"pack": pack}, headers=headers)


def pay(c: TestClient, key: str, pack: str = "starter", spt: str = SPT):
    """Both steps. Returns (the 402's challenge params, the credential sent, the final response)."""
    unpaid = ask(c, key, pack)
    assert unpaid.status_code == 402, unpaid.text
    params = challenge_params(unpaid)
    header = credential(params, {"spt": spt})
    return params, header, send(c, key, header, pack)


def assert_refused(response, code: str):
    """A refused credential: 402, the problem named, a fresh challenge, and no receipt."""
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["type"] == "https://paymentauth.org/problems/" + code.replace("_", "-")
    assert challenge_params(response)["id"] == body["challengeId"]
    assert "payment-receipt" not in response.headers
    return body


# --- configuration: off unless switched on ------------------------------------------------------------


def test_the_plans_file_sells_one_pack():
    packs = load_credit_packs(DEFAULT_PLANS_FILE)
    assert packs == {"starter": CreditPack("starter", 5000, 1900, "Starter pack")}


def test_plans_files_without_packs_sell_none(tmp_path):
    path = tmp_path / "plans.json"
    path.write_text(json.dumps({"plans": {"free": {"monthly_quota": 5}}}))
    assert load_credit_packs(path) == {}
    path.write_text(json.dumps({"plans": {}, "credit_packs": {"x": {"screenshots": 0, "price_usd_cents": 100}}}))
    with pytest.raises(ValueError):
        load_credit_packs(path)


@pytest.mark.parametrize("missing", ["CALOTYPE_CREDITS", "CALOTYPE_CREDITS_CARD", "STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID"])
def test_config_needs_every_variable(missing):
    env = {k: v for k, v in CREDITS_ENV.items() if k != missing}
    config, names = CreditsConfig.from_env(env)
    assert config is None and missing in names
    assert isinstance(credits_from_env(env), NoCredits)
    assert SECRET_KEY not in str(names)


def test_the_flag_alone_decides_before_anything_else():
    config, names = CreditsConfig.from_env({k: v for k, v in CREDITS_ENV.items() if k != "CALOTYPE_CREDITS"})
    assert config is None and names == ["CALOTYPE_CREDITS"]
    assert CreditsConfig.from_env({**CREDITS_ENV, "CALOTYPE_CREDITS": "0"})[0] is None


def test_a_profile_id_must_look_like_one():
    config, names = CreditsConfig.from_env({**CREDITS_ENV, "STRIPE_PROFILE_ID": "acct_123"})
    assert config is None and names == ["STRIPE_PROFILE_ID"]


def test_stablecoins_alone_do_not_switch_it_on():
    """The stablecoin method is not built: asking for it alone leaves the endpoint off."""
    env = {**CREDITS_ENV, "CALOTYPE_CREDITS_STABLECOIN": "1"}
    del env["CALOTYPE_CREDITS_CARD"]
    config, names = CreditsConfig.from_env(env)
    assert config is None and names == ["CALOTYPE_CREDITS_CARD"]


def test_stablecoins_with_cards_are_named_as_not_built(settings, store, fake_renderer, capsys):
    config, _ = CreditsConfig.from_env({**CREDITS_ENV, "CALOTYPE_CREDITS_STABLECOIN": "1"})
    assert config.unsupported == ("CALOTYPE_CREDITS_STABLECOIN",)
    provider = StripeCredits(config, client=FakePayments().client)
    raw, _ = store.create_key("free")
    with make_client(settings, store, fake_renderer, provider) as c:
        methods = [challenge_params(ask(c, raw))["method"]]
    assert methods == ["stripe"]  # cards only; no stablecoin challenge is offered
    assert "not built" in capsys.readouterr().out


def test_config_never_shows_secrets(credits_config):
    assert SECRET_KEY not in repr(credits_config)
    assert credits_config.challenge_secret not in repr(credits_config)


def test_the_challenge_secret_is_derived_as_stripes_sample_derives_it(credits_config):
    """crypto.createHmac("sha256", STRIPE_SECRET_KEY).update("mpp-challenge-signing").digest("base64")"""
    expected = base64.b64encode(hmac.new(SECRET_KEY.encode(), b"mpp-challenge-signing", hashlib.sha256).digest())
    assert credits_config.challenge_secret == derive_challenge_secret(SECRET_KEY) == expected.decode()
    config, _ = CreditsConfig.from_env({**CREDITS_ENV, "MPP_SECRET_KEY": "own-secret"})
    assert config.challenge_secret == "own-secret"


def test_optional_settings(credits_config):
    assert credits_config.api_version == "2026-07-29.preview"
    assert credits_config.payment_method_types == ("card", "link")
    config, _ = CreditsConfig.from_env(
        {
            **CREDITS_ENV,
            "STRIPE_MPP_API_VERSION": "2026-09-30.preview",
            "STRIPE_SPT_PAYMENT_METHOD_TYPES": "card",
            "MPP_REALM": "calotype.dev",
        }
    )
    assert (config.api_version, config.payment_method_types, config.realm) == ("2026-09-30.preview", ("card",), "calotype.dev")
    assert CreditsConfig.from_env({**CREDITS_ENV, "STRIPE_SPT_PAYMENT_METHOD_TYPES": "card;drop table"})[0] is None


def test_credits_from_env_builds_a_real_client_without_calling_stripe():
    assert isinstance(credits_from_env(CREDITS_ENV), StripeCredits)
    assert isinstance(credits_from_env({}), NoCredits)


def test_off_by_default_the_endpoint_answers_like_billing_without_stripe(client, free_key):
    """No flag: 503 billing_unavailable, with or without a key, exactly as /v1/billing/* answers."""
    for headers in ({}, auth(free_key)):
        r = client.post("/v1/credits", json={"pack": "starter"}, headers=headers)
        assert r.status_code == 503 and r.json()["error"]["code"] == "billing_unavailable"
        assert "www-authenticate" not in r.headers
    assert client.post("/v1/billing/checkout", json={"plan": "starter"}).status_code == 503
    assert "/v1/credits" not in client.get("/openapi.json").json()["paths"]
    assert "credit_packs" not in client.get("/v1/plans").json()
    assert client.get("/v1/usage", headers=auth(free_key)).json()["credits"] == 0


def test_app_reads_credits_from_the_environment(monkeypatch, settings, store, fake_renderer):
    for name, value in CREDITS_ENV.items():
        monkeypatch.setenv(name, value)
    raw, _ = store.create_key("free")
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer)
    with TestClient(app) as c:
        assert ask(c, raw).status_code == 402
        assert "/v1/credits" in c.get("/openapi.json").json()["paths"]


def test_switched_on_but_incomplete_logs_the_missing_names_only(monkeypatch, settings, store, fake_renderer, capsys):
    monkeypatch.setenv("CALOTYPE_CREDITS", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", SECRET_KEY)
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer)
    with TestClient(app) as c:
        assert c.post("/v1/credits", json={"pack": "starter"}).status_code == 503
    out = capsys.readouterr().out
    assert "prepaid credit is off" in out and "STRIPE_PROFILE_ID" in out and SECRET_KEY not in out


def test_plans_list_the_packs_where_they_are_on_sale(cclient):
    assert cclient.get("/v1/plans").json()["credit_packs"] == [
        {"name": "starter", "title": "Starter pack", "screenshots": 5000, "price_usd": 19.0}
    ]


# --- unpaid: the 402 challenge ----------------------------------------------------------------------------


def test_unpaid_request_gets_a_402_challenge_for_the_packs_price(cclient, store, payments, credits_config):
    raw, key = store.create_key("free")
    r = ask(cclient, raw)
    assert r.status_code == 402
    assert r.headers["content-type"] == "application/problem+json"
    assert r.headers["cache-control"] == "no-store"
    assert "payment-receipt" not in r.headers

    p = challenge_params(r)
    assert (p["method"], p["intent"], p["realm"]) == ("stripe", "charge", "testserver")
    # The key is in Authorization, so the credential is asked for in the protocol's other header.
    assert p["header"] == "Payment-Authorization"
    assert mpp.decode_object(p["request"]) == {
        "amount": "1900",  # cents, as a string
        "currency": "usd",
        "description": "Calotype Starter pack: 5000 prepaid screenshots",
        "methodDetails": {"networkId": PROFILE, "paymentMethodTypes": ["card", "link"]},
    }
    opaque = mpp.decode_object(p["opaque"])
    assert (opaque["key"], opaque["pack"]) == (str(key.id), "starter")
    expires = mpp.parse_time(p["expires"])
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)
    # The id is the keyed hash of exactly these parameters.
    assert p["id"] == mpp.challenge_id(
        credits_config.challenge_secret,
        realm="testserver", method="stripe", intent="charge", request=p["request"], expires=p["expires"],
        header="Payment-Authorization", opaque=p["opaque"],
    )

    body = r.json()
    assert body["type"] == "https://paymentauth.org/problems/payment-required"
    assert (body["status"], body["title"], body["challengeId"]) == (402, "Payment Required", p["id"])
    assert body["error"]["code"] == "payment_required"
    assert body["error"]["pack"] == {"name": "starter", "title": "Starter pack", "screenshots": 5000, "price_usd": 19.0}
    assert "$19" in body["error"]["message"] and "Payment-Authorization" in body["error"]["message"]

    # Asking costs nothing and changes nothing.
    payments.create.assert_not_called()
    assert store.get_credit_balance(key.id) == 0 and store.list_credit_purchases(key.id) == []


def test_the_realm_is_the_public_host(settings, store, fake_renderer, credits):
    raw, _ = store.create_key("free")
    with make_client(settings, store, fake_renderer, credits, public_url="https://calotype.example") as c:
        assert challenge_params(ask(c, raw))["realm"] == "calotype.example"
    # The host name only, as the SDKs set it: mppx's validator compares the realm with the URL's hostname.
    with make_client(settings, store, fake_renderer, credits, public_url="https://calotype.example:8443") as c:
        assert challenge_params(ask(c, raw))["realm"] == "calotype.example"
    fixed = StripeCredits(replace(credits.config, realm="pay.calotype.example"), client=MagicMock())
    with make_client(settings, store, fake_renderer, fixed) as c:
        assert challenge_params(ask(c, raw))["realm"] == "pay.calotype.example"


def test_a_key_comes_before_any_price(cclient, store, payments):
    """401 before 402: a request without a valid key is told nothing about payment."""
    for headers in ({}, auth("cal_not_a_key")):
        r = cclient.post("/v1/credits", json={"pack": "starter"}, headers=headers)
        assert r.status_code == 401
        assert not r.headers.get("www-authenticate", "").startswith("Payment")
    raw, key = store.create_key("free")
    store.revoke_key(key.id)
    assert ask(cclient, raw).status_code == 401
    payments.create.assert_not_called()


@pytest.mark.parametrize("body", [{}, {"pack": "pro"}, {"pack": 5}, {"plan": "starter"}, ["starter"]])
def test_unknown_packs_are_refused_with_the_list(cclient, free_key, body):
    r = cclient.post("/v1/credits", json=body, headers=auth(free_key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_pack"
    assert [p["name"] for p in r.json()["error"]["packs"]] == ["starter"]


def test_a_body_that_is_not_json_is_refused(cclient, free_key):
    r = cclient.post("/v1/credits", content=b"pack=starter", headers=auth(free_key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"


def test_a_pack_under_stripes_card_minimum_is_not_offered(settings, store, fake_renderer, credits):
    packs = {"tiny": CreditPack("tiny", 10, 49, "Tiny"), "ok": CreditPack("ok", 10, 50, "Ok")}
    raw, _ = store.create_key("free")
    with make_client(settings, store, fake_renderer, credits, credit_packs=packs) as c:
        assert ask(c, raw, "tiny").status_code == 400
        assert ask(c, raw, "ok").status_code == 402


# --- paid: the credential is charged and credited -------------------------------------------------------


def test_a_paid_request_adds_the_pack_and_returns_the_balance(cclient, store, payments):
    raw, key = store.create_key("free")
    params, _header, r = pay(cclient, raw)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "pack": "starter",
        "screenshots_added": 5000,
        "credits": 5000,
        "already_credited": False,
        "payment": {"method": "stripe", "reference": "pi_test_1", "amount_usd": 19.0},
    }
    assert r.headers["cache-control"] == "no-store"
    receipt = mpp.decode_object(r.headers["payment-receipt"])
    assert (receipt["method"], receipt["reference"], receipt["status"]) == ("stripe", "pi_test_1", "success")
    assert abs(mpp.parse_time(receipt["timestamp"]) - datetime.now(UTC)) < timedelta(minutes=1)

    # One PaymentIntent, created and confirmed with the token, as mppx 0.21.1 sends it.
    payments.create.assert_called_once()
    sent = payments.create.call_args.kwargs
    assert sent["params"] == {
        "amount": 1900,
        "currency": "usd",
        "confirm": True,
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        "shared_payment_granted_token": SPT,
        "description": "Calotype Starter pack: 5000 prepaid screenshots",
        "metadata": {
            "machine_payment": "true",
            "mpp_sdk": sent["params"]["metadata"]["mpp_sdk"],
            "mpp_intent": "charge",
            "mpp_challenge_id": params["id"],
            "calotype_key_id": str(key.id),
            "calotype_credit_pack": "starter",
            "calotype_credit_screenshots": "5000",
        },
    }
    assert sent["params"]["metadata"]["mpp_sdk"].startswith("calotype/")
    assert sent["options"] == {"idempotency_key": f"mpp_{params['id']}_{SPT}", "stripe_version": "2026-07-29.preview"}

    # The ledger: one purchase under the PaymentIntent id, and the balance.
    assert store.get_credit_balance(key.id) == 5000
    [purchase] = store.list_credit_purchases(key.id)
    assert (purchase.id, purchase.pack, purchase.screenshots, purchase.amount_cents, purchase.currency) == (
        "pi_test_1", "starter", 5000, 1900, "usd",
    )
    assert (purchase.challenge_id, purchase.method, purchase.refunded_at) == (params["id"], "stripe", None)
    assert cclient.get("/v1/usage", headers=auth(raw)).json()["credits"] == 5000


def test_the_key_and_the_token_are_neither_stored_nor_sent_to_stripe_nor_logged(cclient, store, payments, caplog):
    caplog.set_level(logging.INFO)
    raw, key = store.create_key("free")
    assert pay(cclient, raw)[2].status_code == 200
    sent = json.dumps(payments.create.call_args.kwargs)
    assert raw not in sent and hash_key(raw) not in sent
    dump = stored_text(store)
    assert raw not in dump and SPT not in dump
    logs = logged(caplog)
    assert raw not in logs and SPT not in logs and SECRET_KEY not in logs
    assert '"message": "credit purchase"' in logs and '"payment_intent": "pi_test_1"' in logs


def test_buying_twice_adds_twice(cclient, store, payments):
    raw, key = store.create_key("free")
    assert pay(cclient, raw, spt="spt_first")[2].json()["credits"] == 5000
    second = pay(cclient, raw, spt="spt_second")[2].json()
    assert (second["screenshots_added"], second["credits"]) == (5000, 10000)
    assert [p.id for p in store.list_credit_purchases(key.id)] == ["pi_test_1", "pi_test_2"]


def test_a_paid_plan_key_can_buy_credit_too(cclient, store):
    raw, key = store.create_key("pro")
    assert pay(cclient, raw)[2].json()["credits"] == 5000


def test_the_key_may_ride_in_the_query_and_the_credential_in_authorization(cclient, store, payments, credits_config):
    """For a client that can only send ``Authorization: Payment``: the protocol's default header."""
    raw, key = store.create_key("free")
    url = f"/v1/credits?access_key={raw}"
    unpaid = cclient.post(url, json={"pack": "starter"})
    assert unpaid.status_code == 402
    params = challenge_params(unpaid)
    assert "header" not in params
    assert "in Authorization" in unpaid.json()["error"]["message"]
    assert params["id"] == mpp.challenge_id(
        credits_config.challenge_secret,
        realm="testserver", method="stripe", intent="charge", request=params["request"], expires=params["expires"],
        opaque=params["opaque"],
    )
    r = cclient.post(url, json={"pack": "starter"}, headers={"Authorization": credential(params, {"spt": SPT})})
    assert r.status_code == 200 and r.json()["credits"] == 5000


def test_a_credential_in_the_wrong_header_is_refused(cclient, store, payments):
    raw, key = store.create_key("free")
    params = challenge_params(ask(cclient, raw))  # issued for Payment-Authorization
    r = cclient.post(
        f"/v1/credits?access_key={raw}", json={"pack": "starter"},
        headers={"Authorization": credential(params, {"spt": SPT})},
    )
    assert_refused(r, "invalid_challenge")
    payments.create.assert_not_called()


def test_authorization_payment_is_not_a_key_anywhere_else(cclient, free_key):
    assert cclient.get("/v1/usage", headers={"Authorization": "Payment abc"}).status_code == 401
    assert cclient.post("/v1/credits", json={"pack": "starter"}, headers={"Authorization": "Payment abc"}).status_code == 401


# --- one payment credits once ------------------------------------------------------------------------------


def test_a_replayed_credential_is_not_charged_or_credited_again(cclient, store, payments):
    raw, key = store.create_key("free")
    _params, header, first = pay(cclient, raw)
    assert first.json()["screenshots_added"] == 5000
    for _ in range(3):
        again = send(cclient, raw, header)
        assert again.status_code == 200
        assert again.json() == {
            "pack": "starter",
            "screenshots_added": 0,
            "credits": 5000,
            "already_credited": True,
            "payment": {"method": "stripe", "reference": "pi_test_1", "amount_usd": 19.0},
        }
        assert mpp.decode_object(again.headers["payment-receipt"])["reference"] == "pi_test_1"
    payments.create.assert_called_once()  # the ledger answered; Stripe was not asked again
    assert store.get_credit_balance(key.id) == 5000 and len(store.list_credit_purchases(key.id)) == 1


def test_a_replay_after_the_challenge_expired_still_says_already_paid(settings, store, fake_renderer, credits, payments):
    """Otherwise a buyer whose reply was lost would be handed a new challenge and pay twice."""
    short = StripeCredits(replace(credits.config, expires_s=2), client=payments.client)
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, short) as c:
        _params, header, first = pay(c, raw)
        assert first.status_code == 200
        time.sleep(2.2)
        again = send(c, raw, header)
        assert again.status_code == 200 and again.json()["already_credited"] is True
    payments.create.assert_called_once()
    assert store.get_credit_balance(key.id) == 5000


def test_a_charge_that_was_not_recorded_is_credited_once_on_retry(cclient, store, payments, monkeypatch):
    """The server stops between Stripe's answer and the ledger write; the buyer sends the credential again."""
    raw, key = store.create_key("free")
    params = challenge_params(ask(cclient, raw))
    header = credential(params, {"spt": SPT})

    real_add = store.add_credit_purchase
    monkeypatch.setattr(store, "add_credit_purchase", MagicMock(side_effect=RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        send(cclient, raw, header)
    assert len(payments.intents) == 1 and store.get_credit_balance(key.id) == 0  # charged, not credited

    monkeypatch.setattr(store, "add_credit_purchase", real_add)
    r = send(cclient, raw, header)
    assert r.status_code == 200
    assert (r.json()["screenshots_added"], r.json()["credits"], r.json()["already_credited"]) == (5000, 5000, False)
    # Stripe was asked again with the same idempotency key and answered with the same PaymentIntent.
    assert payments.create.call_count == 2 and len(payments.intents) == 1
    keys = {call.kwargs["options"]["idempotency_key"] for call in payments.create.call_args_list}
    assert keys == {f"mpp_{params['id']}_{SPT}"}
    payments.client.v1.payment_intents.retrieve.assert_called_once_with("pi_test_1")
    assert send(cclient, raw, header).json()["already_credited"] is True
    assert store.get_credit_balance(key.id) == 5000


def test_the_same_credential_sent_at_once_from_many_requests_credits_once(cclient, store, payments):
    raw, key = store.create_key("free")
    header = credential(challenge_params(ask(cclient, raw)), {"spt": SPT})
    results: list = []

    def go():
        results.append(send(cclient, raw, header))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [r.status_code for r in results] == [200] * 6
    assert sum(r.json()["screenshots_added"] for r in results) == 5000
    assert store.get_credit_balance(key.id) == 5000 and len(store.list_credit_purchases(key.id)) == 1
    assert len(payments.intents) == 1


# --- credentials that must not be charged -------------------------------------------------------------------


def test_a_credential_with_a_changed_amount_is_refused(cclient, store, payments):
    raw, key = store.create_key("free")
    params = challenge_params(ask(cclient, raw))
    cheaper = {**mpp.decode_object(params["request"]), "amount": "100"}
    r = send(cclient, raw, credential(params, {"spt": SPT}, request=mpp.encode_object(cheaper)))
    body = assert_refused(r, "invalid_challenge")
    assert mpp.decode_object(challenge_params(r)["request"])["amount"] == "1900"  # the new challenge: full price
    assert "changed" in body["detail"]
    payments.create.assert_not_called()
    assert store.get_credit_balance(key.id) == 0


def test_a_credential_for_another_pack_is_refused(settings, store, fake_renderer, credits, payments):
    packs = {"starter": CreditPack("starter", 5000, 1900, "Starter pack"), "big": CreditPack("big", 50000, 9900, "Big")}
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, credits, credit_packs=packs) as c:
        starter = challenge_params(ask(c, raw, "starter"))
        # Paid at the starter price, claimed as the big pack.
        r = send(c, raw, credential(starter, {"spt": SPT}), pack="big")
        body = assert_refused(r, "invalid_challenge")
        assert "'starter'" in body["detail"] and "'big'" in body["detail"]
        assert mpp.decode_object(challenge_params(r)["request"])["amount"] == "9900"
        payments.create.assert_not_called()
        assert store.get_credit_balance(key.id) == 0
        # The same credential still buys what it was issued for.
        assert send(c, raw, credential(starter, {"spt": SPT}), pack="starter").json()["credits"] == 5000


def test_a_credential_issued_before_a_price_change_is_refused(settings, store, fake_renderer, credits, payments):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, credits) as c:
        old = challenge_params(ask(c, raw))
    dearer = {"starter": CreditPack("starter", 5000, 2900, "Starter pack")}
    with make_client(settings, store, fake_renderer, credits, credit_packs=dearer) as c:
        assert_refused(send(c, raw, credential(old, {"spt": SPT})), "invalid_challenge")
    payments.create.assert_not_called()


def test_a_credential_issued_to_another_key_is_refused(cclient, store, payments):
    raw_a, _ = store.create_key("free")
    raw_b, key_b = store.create_key("free")
    params = challenge_params(ask(cclient, raw_a))
    assert_refused(send(cclient, raw_b, credential(params, {"spt": SPT})), "invalid_challenge")
    payments.create.assert_not_called()
    assert store.get_credit_balance(key_b.id) == 0


def test_a_challenge_signed_with_another_secret_is_refused(cclient, store, payments, credits_config):
    raw, key = store.create_key("free")
    forged = mpp.issue_challenge(
        "not-the-servers-secret",
        realm="testserver", method="stripe", intent="charge",
        request={"amount": "1", "currency": "usd", "methodDetails": {"networkId": PROFILE, "paymentMethodTypes": ["card"]}},
        opaque={"key": str(key.id), "pack": "starter"},
        header="Payment-Authorization",
    )
    params = challenge_params(SimpleNamespace(headers={"www-authenticate": forged.www_authenticate()}))
    assert_refused(send(cclient, raw, credential(params, {"spt": SPT})), "invalid_challenge")
    payments.create.assert_not_called()


def test_an_expired_challenge_is_refused_with_a_new_one(settings, store, fake_renderer, credits, payments):
    expired = StripeCredits(replace(credits.config, expires_s=-5), client=payments.client)
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, expired) as c:
        r = pay(c, raw)[2]
    assert_refused(r, "payment_expired")
    payments.create.assert_not_called()
    assert store.get_credit_balance(key.id) == 0


@pytest.mark.parametrize("payload", [{}, {"spt": ""}, {"spt": 5}, {"spt": "pm_card_visa"}, {"spt": "spt_bad token"}])
def test_a_credential_without_a_token_is_refused(cclient, free_key, payments, payload):
    params = challenge_params(ask(cclient, free_key))
    assert_refused(send(cclient, free_key, credential(params, payload)), "malformed_credential")
    payments.create.assert_not_called()


@pytest.mark.parametrize("header", ["Payment !!!", "Payment " + mpp.b64url(b'{"challenge": 1}'), "Payment e30"])
def test_a_credential_that_cannot_be_read_is_refused(cclient, free_key, payments, header):
    assert_refused(send(cclient, free_key, header), "malformed_credential")
    payments.create.assert_not_called()


def test_a_header_that_is_not_a_payment_credential_is_just_unpaid(cclient, free_key, payments):
    r = send(cclient, free_key, "Bearer something")
    assert r.status_code == 402 and r.json()["error"]["code"] == "payment_required"


# --- what Stripe can answer --------------------------------------------------------------------------------


def test_a_declined_card_is_refused_and_nothing_is_added(cclient, store, payments):
    raw, key = store.create_key("free")
    payments.fail_with = stripe.CardError("Your card was declined.", "payment_method", "card_declined", http_status=402)
    body = assert_refused(pay(cclient, raw)[2], "verification_failed")
    assert "declined" in body["detail"] and "Your card" not in json.dumps(body)
    assert store.get_credit_balance(key.id) == 0 and store.list_credit_purchases(key.id) == []


def test_a_token_stripe_refuses_is_refused(cclient, store, payments, caplog):
    """Unknown, used, expired, revoked or issued to another seller: Stripe answers 400."""
    raw, key = store.create_key("free")
    payments.fail_with = stripe.InvalidRequestError("No such shared payment token: spt_x", "shared_payment_granted_token", http_status=400)
    body = assert_refused(pay(cclient, raw)[2], "verification_failed")
    assert "spt_x" not in json.dumps(body)
    assert store.get_credit_balance(key.id) == 0
    logs = logged(caplog)
    assert "stripe payment failed" in logs and "InvalidRequestError" in logs and "No such" not in logs


@pytest.mark.parametrize("status", ["requires_action", "requires_payment_method", "canceled"])
def test_a_payment_that_did_not_succeed_adds_nothing(cclient, store, payments, status):
    raw, key = store.create_key("free")
    payments.status = status
    assert_refused(pay(cclient, raw)[2], "verification_failed")
    assert store.get_credit_balance(key.id) == 0 and store.list_credit_purchases(key.id) == []


@pytest.mark.parametrize(
    "error",
    [stripe.APIConnectionError("network down"), stripe.APIError("boom", http_status=500),
     stripe.RateLimitError("slow down", http_status=429), stripe.IdempotencyError("in use", http_status=409)],
)
def test_no_answer_from_stripe_is_502_and_the_same_credential_finishes_the_job(cclient, store, payments, error):
    raw, key = store.create_key("free")
    payments.fail_with = error
    _params, header, r = pay(cclient, raw)
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_unconfirmed"
    assert "www-authenticate" not in r.headers  # no new challenge: the buyer must not pay a second one
    assert "same credential" in r.json()["error"]["message"]
    assert store.get_credit_balance(key.id) == 0

    payments.fail_with = None
    again = send(cclient, raw, header)
    assert again.status_code == 200 and again.json()["credits"] == 5000


def test_a_payment_still_processing_is_unconfirmed(cclient, store, payments):
    raw, key = store.create_key("free")
    payments.status = "processing"
    r = pay(cclient, raw)[2]
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_unconfirmed"
    assert store.get_credit_balance(key.id) == 0


@pytest.mark.parametrize("error", [stripe.AuthenticationError("bad key", http_status=401), stripe.PermissionError("no", http_status=403)])
def test_a_broken_stripe_setup_is_502_billing_error(cclient, store, payments, error):
    raw, key = store.create_key("free")
    payments.fail_with = error
    r = pay(cclient, raw)[2]
    assert r.status_code == 502 and r.json()["error"]["code"] == "billing_error"
    assert "bad key" not in r.text and store.get_credit_balance(key.id) == 0


def test_an_amount_that_differs_from_the_pack_is_never_credited(cclient, store, payments):
    raw, key = store.create_key("free")
    payments.create.side_effect = lambda params, options=None: SimpleNamespace(
        id="pi_test_odd", status="succeeded", amount=100, currency="usd", last_response=None
    )
    r = pay(cclient, raw)[2]
    assert r.status_code == 502 and r.json()["error"]["code"] == "billing_error"
    assert store.get_credit_balance(key.id) == 0


def test_the_payment_through_the_real_stripe_client(credits_config, store):
    """The real StripeClient over a fake transport: the request Stripe would get, and how a replay is read."""

    class FakeTransport(stripe.HTTPClient):
        name = "fake"

        def __init__(self):
            super().__init__()
            self.calls = []
            self.replayed = False

        def request(self, method, url, headers, post_data=None, *, _usage=None):
            self.calls.append((method, url, headers, post_data))
            body = {"id": "pi_test_real", "object": "payment_intent", "status": "succeeded", "amount": 1900, "currency": "usd"}
            reply_headers = {"request-id": "req_test"}
            if method == "post" and self.replayed:
                reply_headers["idempotent-replayed"] = "true"
            return json.dumps(body), 200, reply_headers

        def close(self):
            pass

    transport = FakeTransport()
    provider = StripeCredits(credits_config, client=stripe.StripeClient(SECRET_KEY, http_client=transport))
    raw, key = store.create_key("free")
    pack = CreditPack("starter", 5000, 1900, "Starter pack")
    challenge = provider.challenge(key, pack, realm="calotype.test", header="")
    header = credential(challenge_params(SimpleNamespace(headers={"www-authenticate": challenge.www_authenticate()})), {"spt": SPT})

    paid = provider.settle(key, pack, header, store, realm="calotype.test", header="")
    assert (paid.reference, paid.added, paid.balance) == ("pi_test_real", True, 5000)
    [(method, url, headers, post_data)] = transport.calls
    assert method == "post" and url == "https://api.stripe.com/v1/payment_intents"
    assert headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert headers["Stripe-Version"] == "2026-07-29.preview"
    assert headers["Idempotency-Key"] == f"mpp_{challenge.id}_{SPT}"
    form = urllib.parse.parse_qs(post_data)
    assert form["shared_payment_granted_token"] == [SPT]
    assert (form["amount"], form["currency"], form["confirm"]) == (["1900"], ["usd"], ["true"])
    assert form["automatic_payment_methods[enabled]"] == ["true"]
    assert form["automatic_payment_methods[allow_redirects]"] == ["never"]
    assert form["metadata[calotype_key_id]"] == [str(key.id)]
    assert form["metadata[mpp_challenge_id]"] == [challenge.id]
    assert raw not in post_data and hash_key(raw) not in post_data

    # A replay from Stripe's idempotency record is followed by a read of the payment as it is now.
    transport.calls.clear()
    transport.replayed = True
    assert provider._charge(key, pack, challenge.id, SPT) == "pi_test_real"
    assert [(m, u) for m, u, _h, _d in transport.calls] == [
        ("post", "https://api.stripe.com/v1/payment_intents"),
        ("get", "https://api.stripe.com/v1/payment_intents/pi_test_real"),
    ]


# --- the official SDK as the buyer ----------------------------------------------------------------------------


def test_pympp_the_official_client_can_buy_a_pack(settings, store, fake_renderer, credits, payments):
    """pympp's own client reads the 402, makes the credential and retries; no code of ours on the buying side."""
    pytest.importorskip("mpp", reason="pympp, the official MPP SDK, is not installed")
    import httpx
    from mpp.client import Mpp
    from mpp.methods.stripe import client as stripe_method

    raw, key = store.create_key("free")
    app = create_app(
        settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer, credits=credits
    )
    asked: list = []

    async def create_token(params) -> str:
        asked.append(params)
        return "spt_from_pympp"

    async def buy():
        method = stripe_method.charge(create_token=create_token, payment_method="pm_card_visa")
        buyer = Mpp(methods=[method], allowed_origins=["http://testserver"])
        transport = buyer.payment_transport
        transport._inner = httpx.ASGITransport(app=app)  # the app in-process instead of a socket
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return await http.post("/v1/credits", json={"pack": "starter"}, headers=auth(raw))

    r = asyncio.run(buy())
    assert r.status_code == 200, r.text
    assert r.json()["credits"] == 5000 and r.json()["payment"]["reference"] == "pi_test_1"
    # What the SDK understood from our challenge, and asked a token for.
    [params] = asked
    assert (params.amount, params.currency, params.network_id) == ("1900", "usd", PROFILE)
    assert params.payment_method_types == ["card", "link"]
    assert payments.create.call_args.kwargs["params"]["shared_payment_granted_token"] == "spt_from_pympp"
    assert store.get_credit_balance(key.id) == 5000


# --- quota: the month's plan first, then credit --------------------------------------------------------------


SMALL = {"free": Plan("free", 2, 0, "Free")}


def shoot(c: TestClient, key: str):
    return c.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(key))


def test_the_monthly_quota_is_used_before_credit(settings, store, fake_renderer, credits):
    raw, key = store.create_key("free")
    store.add_credit_purchase("pi_seed", key.id, pack="starter", screenshots=3, amount_cents=1900, currency="usd")
    with make_client(settings, store, fake_renderer, credits, plans=SMALL) as c:
        # The plan's two screenshots go first, and credit is not touched.
        for used in (1, 2):
            r = shoot(c, raw)
            assert r.status_code == 200 and r.headers["x-calotype-usage"] == f"{used}/2"
            assert "x-calotype-credits" not in r.headers
            assert store.get_credit_balance(key.id) == 3
        # Then credit, one per screenshot; the month's count stays at its limit.
        for left in (2, 1, 0):
            r = shoot(c, raw)
            assert r.status_code == 200 and r.headers["x-calotype-credits"] == str(left)
            assert r.headers["x-calotype-usage"] == "2/2"
            assert (store.get_usage(key.id, MONTH), store.get_credit_balance(key.id)) == (2, left)
        usage = c.get("/v1/usage", headers=auth(raw)).json()
        assert (usage["used"], usage["limit"], usage["credits"]) == (2, 2, 0)
        # Both used up.
        r = shoot(c, raw)
        assert r.status_code == 429 and r.json()["error"]["code"] == "quota_exceeded"
        assert (r.json()["error"]["used"], r.json()["error"]["credits"]) == (2, 0)
        assert "POST /v1/credits" in r.json()["error"]["message"]
        assert store.get_credit_balance(key.id) == 0 and len(fake_renderer.calls) == 5


def test_a_new_month_goes_back_to_the_plan_and_leaves_credit_alone(settings, store, fake_renderer, credits, monkeypatch):
    raw, key = store.create_key("free")
    store.add_credit_purchase("pi_seed", key.id, pack="starter", screenshots=5, amount_cents=1900, currency="usd")
    with make_client(settings, store, fake_renderer, credits, plans=SMALL) as c:
        for _ in range(3):
            assert shoot(c, raw).status_code == 200
        assert store.get_credit_balance(key.id) == 4
        monkeypatch.setattr("calotype.app.current_month", lambda: "2099-01")
        r = shoot(c, raw)
        assert r.headers["x-calotype-usage"] == "1/2" and "x-calotype-credits" not in r.headers
        assert store.get_credit_balance(key.id) == 4  # credit does not expire or reset


def test_a_failed_capture_gives_the_credit_back(settings, store, fake_renderer, credits):
    from calotype.capture import CaptureError

    raw, key = store.create_key("free")
    store.add_credit_purchase("pi_seed", key.id, pack="starter", screenshots=2, amount_cents=1900, currency="usd")
    with make_client(settings, store, fake_renderer, credits, plans=SMALL) as c:
        shoot(c, raw), shoot(c, raw)
        fake_renderer.fail_with = CaptureError(504, "timeout", "too slow")
        assert shoot(c, raw).status_code == 504
        assert (store.get_usage(key.id, MONTH), store.get_credit_balance(key.id)) == (2, 2)
        fake_renderer.fail_with = RuntimeError("boom")
        assert shoot(c, raw).status_code == 500
        assert (store.get_usage(key.id, MONTH), store.get_credit_balance(key.id)) == (2, 2)
        fake_renderer.fail_with = None
        assert shoot(c, raw).headers["x-calotype-credits"] == "1"


def test_credit_is_spent_even_where_the_endpoint_is_off(settings, store, fake_renderer):
    """Credit that was paid for keeps working if CALOTYPE_CREDITS is switched off again."""
    raw, key = store.create_key("free")
    store.add_credit_purchase("pi_seed", key.id, pack="starter", screenshots=1, amount_cents=1900, currency="usd")
    with make_client(settings, store, fake_renderer, NoCredits(), plans=SMALL) as c:
        assert [shoot(c, raw).status_code for _ in range(4)] == [200, 200, 200, 429]
        r = shoot(c, raw)
        assert "POST /v1/credits" not in r.json()["error"]["message"]


def test_the_mcp_tool_and_the_api_share_the_credit_path(settings, store, fake_renderer, credits):
    """capture_for_key is the one path for both; a capture paid from credit says so."""
    raw, key = store.create_key("free")
    store.add_credit_purchase("pi_seed", key.id, pack="starter", screenshots=5000, amount_cents=1900, currency="usd")
    store.try_consume(key.id, MONTH, 2), store.try_consume(key.id, MONTH, 2)
    with make_client(settings, store, fake_renderer, credits, plans=SMALL) as c:
        r = c.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "screenshot", "arguments": {"url": "https://example.com"}}},
            headers={**auth(raw), "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
        )
        assert r.status_code == 200, r.text
        assert "prepaid credit, 4,999 left" in r.text
    assert store.get_credit_balance(key.id) == 4999


# --- the webhook --------------------------------------------------------------------------------------------


def intent_object(payments: FakePayments, key_id: int, intent_id: str = "pi_hook_1", **changes) -> dict:
    obj = {
        "id": intent_id,
        "object": "payment_intent",
        "status": "succeeded",
        "amount": 1900,
        "amount_received": 1900,
        "currency": "usd",
        "metadata": {
            "calotype_key_id": str(key_id), "calotype_credit_pack": "starter", "calotype_credit_screenshots": "5000",
            "mpp_challenge_id": "challenge-from-the-hook",
        },
        **changes,
    }
    payments.intents[intent_id] = obj
    return obj


def test_the_webhook_credits_a_payment_the_request_never_recorded(cclient, store, payments):
    raw, key = store.create_key("free")
    obj = intent_object(payments, key.id)
    r = post_event(cclient, event("evt_credit_1", "payment_intent.succeeded", obj))
    assert r.status_code == 200 and r.json() == {"received": True, "outcome": "credited"}
    assert store.get_credit_balance(key.id) == 5000
    [purchase] = store.list_credit_purchases(key.id)
    assert (purchase.id, purchase.pack, purchase.amount_cents, purchase.challenge_id) == (
        "pi_hook_1", "starter", 1900, "challenge-from-the-hook",
    )


def test_a_replayed_webhook_credits_once(cclient, store, payments):
    raw, key = store.create_key("free")
    payload = event("evt_credit_1", "payment_intent.succeeded", intent_object(payments, key.id))
    assert post_event(cclient, payload).json()["outcome"] == "credited"
    assert post_event(cclient, payload).json()["outcome"] == "duplicate"
    # The same payment under a new event id is still the same payment.
    other = event("evt_credit_2", "payment_intent.succeeded", payments.intents["pi_hook_1"])
    assert post_event(cclient, other).json()["outcome"] == "already_credited"
    assert store.get_credit_balance(key.id) == 5000 and len(store.list_credit_purchases(key.id)) == 1


def test_the_webhook_after_the_request_adds_nothing(cclient, store, payments):
    """The usual order: the request credits, then Stripe's event for the same PaymentIntent arrives."""
    raw, key = store.create_key("free")
    assert pay(cclient, raw)[2].json()["credits"] == 5000
    r = post_event(cclient, event("evt_after", "payment_intent.succeeded", payments.intents["pi_test_1"]))
    assert r.json()["outcome"] == "already_credited"
    assert store.get_credit_balance(key.id) == 5000


def test_the_request_after_the_webhook_adds_nothing(cclient, store, payments, monkeypatch):
    """The other order: the ledger write failed, the webhook credited, then the buyer retried."""
    raw, key = store.create_key("free")
    header = credential(challenge_params(ask(cclient, raw)), {"spt": SPT})
    real_add = store.add_credit_purchase
    monkeypatch.setattr(store, "add_credit_purchase", MagicMock(side_effect=RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        send(cclient, raw, header)
    monkeypatch.setattr(store, "add_credit_purchase", real_add)
    assert post_event(cclient, event("evt_first", "payment_intent.succeeded", payments.intents["pi_test_1"])).json()[
        "outcome"
    ] == "credited"
    again = send(cclient, raw, header)
    assert again.status_code == 200 and again.json()["already_credited"] is True
    assert store.get_credit_balance(key.id) == 5000


def test_the_webhook_trusts_stripe_not_the_event(cclient, store, payments):
    """The event says succeeded; Stripe, asked again, says it is not. Nothing is added."""
    raw, key = store.create_key("free")
    claimed = intent_object(payments, key.id)
    payments.intents["pi_hook_1"] = {**claimed, "status": "requires_payment_method"}
    r = post_event(cclient, event("evt_lie", "payment_intent.succeeded", claimed))
    assert r.json()["outcome"] == "ignored" and store.get_credit_balance(key.id) == 0


def test_other_payments_are_ignored_and_not_recorded(cclient, store, payments):
    """A subscription invoice's PaymentIntent has none of our metadata."""
    obj = {"id": "pi_invoice", "object": "payment_intent", "status": "succeeded", "amount": 1900, "metadata": {}}
    r = post_event(cclient, event("evt_other", "payment_intent.succeeded", obj))
    assert r.json() == {"received": True, "outcome": "ignored"}
    assert not store.event_processed("evt_other")
    payments.client.v1.payment_intents.retrieve.assert_not_called()


def test_a_webhook_for_a_deleted_key_adds_nothing(cclient, store, payments):
    raw, key = store.create_key("free")
    obj = intent_object(payments, key.id)
    store.delete_key(key.id)
    assert post_event(cclient, event("evt_gone", "payment_intent.succeeded", obj)).json()["outcome"] == "unknown_key"
    assert store.get_credit_purchase("pi_hook_1") is None


def test_a_webhook_stripe_cannot_confirm_is_retried(cclient, store, payments):
    raw, key = store.create_key("free")
    obj = intent_object(payments, key.id)
    payments.client.v1.payment_intents.retrieve.side_effect = stripe.APIConnectionError("down")
    payload = event("evt_retry", "payment_intent.succeeded", obj)
    assert post_event(cclient, payload).status_code == 500
    assert not store.event_processed("evt_retry") and store.get_credit_balance(key.id) == 0
    payments.client.v1.payment_intents.retrieve.side_effect = lambda intent_id: dict(payments.intents[intent_id])
    assert post_event(cclient, payload).json()["outcome"] == "credited"


def test_the_webhook_still_needs_stripes_signature(cclient, store, payments):
    raw, key = store.create_key("free")
    payload = event("evt_unsigned", "payment_intent.succeeded", intent_object(payments, key.id))
    assert post_event(cclient, payload, signature=None).status_code == 400
    assert post_event(cclient, payload, signature="t=1,v1=00").status_code == 400
    assert store.get_credit_balance(key.id) == 0


def test_without_the_flag_the_payment_event_is_ignored(settings, store, fake_renderer, billing, payments):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, NoCredits(), billing) as c:
        r = post_event(c, event("evt_off", "payment_intent.succeeded", intent_object(payments, key.id)))
    assert r.json()["outcome"] == "ignored" and store.get_credit_balance(key.id) == 0


def test_subscription_events_still_go_to_billing(cclient, store, payments):
    r = post_event(cclient, event("evt_inv", "invoice.paid", {"id": "in_1"}))
    assert r.status_code == 200 and r.json() == {"received": True, "outcome": "ignored"}


# --- the ledger, on every storage backend ------------------------------------------------------------------------


def seed(store, key_id: int, purchase_id: str = "pi_1", screenshots: int = 10, **more):
    args = dict(pack="starter", screenshots=screenshots, amount_cents=1900, currency="usd", challenge_id="ch_" + purchase_id)
    args.update(more)
    return store.add_credit_purchase(purchase_id, key_id, **args)


def test_a_new_key_has_no_credit(store):
    _, key = store.create_key("free")
    assert store.get_credit_balance(key.id) == 0
    assert store.consume_credit(key.id) == (False, 0)
    assert store.list_credit_purchases(key.id) == []
    assert store.get_credit_purchase("pi_none") is None
    assert store.find_credit_purchase_by_challenge("nope") is None
    assert store.find_credit_purchase_by_challenge("") is None
    store.return_credit(key.id)  # nothing to give back to; not an error
    assert store.get_credit_balance(key.id) == 0


def test_a_purchase_is_added_once_per_payment(store):
    _, key = store.create_key("free")
    assert seed(store, key.id) == (True, 10)
    assert seed(store, key.id) == (False, 10)
    assert seed(store, key.id, screenshots=999, pack="other") == (False, 10)  # the first record stands
    assert seed(store, key.id, "pi_2", 5) == (True, 15)
    first, second = store.list_credit_purchases(key.id)
    assert (first.id, first.screenshots, first.pack, first.key_id) == ("pi_1", 10, "starter", key.id)
    assert (first.amount_cents, first.currency, first.method, first.challenge_id) == (1900, "usd", "stripe", "ch_pi_1")
    assert first.created_at and first.refunded_at is None and first.credits_removed == 0 and first.refund_id is None
    assert second.id == "pi_2"
    assert store.get_credit_purchase("pi_1") == first
    assert store.find_credit_purchase_by_challenge("ch_pi_2") == second


def test_a_purchase_for_a_key_that_does_not_exist_is_not_recorded(store):
    assert seed(store, 999_999) == (False, 0)
    assert store.get_credit_purchase("pi_1") is None
    with pytest.raises(ValueError):
        store.create_key("free") and seed(store, 1, screenshots=0)


def test_credit_is_per_key(store):
    _, a = store.create_key("free")
    _, b = store.create_key("free")
    seed(store, a.id)
    assert store.get_credit_balance(b.id) == 0 and store.consume_credit(b.id) == (False, 0)
    assert store.list_credit_purchases(b.id) == []


def test_consume_counts_down_to_zero_and_no_further(store):
    _, key = store.create_key("free")
    seed(store, key.id, screenshots=3)
    assert [store.consume_credit(key.id) for _ in range(5)] == [(True, 2), (True, 1), (True, 0), (False, 0), (False, 0)]
    store.return_credit(key.id)
    assert store.get_credit_balance(key.id) == 1
    assert store.consume_credit(key.id) == (True, 0)


def test_parallel_requests_never_spend_more_credit_than_there_is(store):
    _, key = store.create_key("free")
    seed(store, key.id, screenshots=5)
    taken: list[bool] = []

    def go():
        taken.append(store.consume_credit(key.id)[0])

    threads = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(taken) == 5
    assert store.get_credit_balance(key.id) == 0


def test_parallel_records_of_one_payment_add_once(store):
    _, key = store.create_key("free")
    added: list[bool] = []

    def go():
        added.append(seed(store, key.id, "pi_same", 7)[0])

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(added) == 1 and store.get_credit_balance(key.id) == 7


def test_a_refund_takes_back_what_is_left_of_the_pack_once(store):
    _, key = store.create_key("free")
    seed(store, key.id, screenshots=10)
    for _ in range(4):
        store.consume_credit(key.id)
    purchase, changed = store.refund_credit_purchase("pi_1")
    assert changed and purchase.credits_removed == 6 and purchase.refunded_at and purchase.refund_id is None
    assert store.get_credit_balance(key.id) == 0
    again, changed = store.refund_credit_purchase("pi_1")
    assert not changed and again == purchase
    assert store.set_credit_refund_id("pi_1", "re_1") is True
    assert store.get_credit_purchase("pi_1").refund_id == "re_1"
    assert store.refund_credit_purchase("pi_none") is None
    assert store.set_credit_refund_id("pi_none", "re_x") is False


def test_a_refund_takes_at_most_the_packs_size(store):
    """Two packs, one refunded: the other pack's credit stays."""
    _, key = store.create_key("free")
    seed(store, key.id, "pi_1", 10)
    seed(store, key.id, "pi_2", 10)
    store.consume_credit(key.id)
    purchase, _ = store.refund_credit_purchase("pi_1")
    assert purchase.credits_removed == 10 and store.get_credit_balance(key.id) == 9
    assert store.set_credit_refund_id("pi_2", "re_2") is False  # not refunded, so no refund id


def test_deleting_a_key_deletes_its_credit_and_purchases(store):
    raw, key = store.create_key("free")
    _, other = store.create_key("free")
    seed(store, key.id, "pi_1")
    seed(store, other.id, "pi_2")
    assert store.delete_key(key.id) is True
    assert store.get_credit_balance(key.id) == 0
    assert store.get_credit_purchase("pi_1") is None and store.find_credit_purchase_by_challenge("ch_pi_1") is None
    assert "pi_1" not in stored_text(store)
    assert store.get_credit_balance(other.id) == 10 and store.get_credit_purchase("pi_2") is not None


def test_revoking_a_key_keeps_its_credit_record(store):
    _, key = store.create_key("free")
    seed(store, key.id)
    store.revoke_key(key.id)
    assert store.get_credit_balance(key.id) == 10


@pytest.mark.parametrize("purchase_id", ["", "a/b", "..", "__x__"])
def test_firestore_refuses_purchase_ids_that_could_name_another_path(firestore_store, purchase_id):
    _, key = firestore_store.create_key("free")
    with pytest.raises(ValueError):
        seed(firestore_store, key.id, purchase_id)


def test_an_older_sqlite_database_gains_the_credit_tables(tmp_path):
    import sqlite3

    from calotype.storage import SQLiteStorage

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL UNIQUE, plan TEXT NOT NULL, "
        "label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, revoked_at TEXT, internal INTEGER NOT NULL DEFAULT 0, "
        "successful_screenshots INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO api_keys (key_hash, plan, created_at) VALUES ('abc', 'free', '2026-11-01T00:00:00+00:00');"
    )
    db.close()
    s = SQLiteStorage(path)
    s.init()
    assert seed(s, 1) == (True, 10) and s.consume_credit(1) == (True, 9)
    s.close()


# --- refunds, from the admin CLI ----------------------------------------------------------------------------------


@pytest.fixture
def admin_payments(monkeypatch) -> FakePayments:
    """Stripe for the admin CLI's refunds, over the stand-in client."""
    fake = FakePayments()
    monkeypatch.setattr(admin, "open_credits", lambda: credits_for_admin({"STRIPE_SECRET_KEY": SECRET_KEY}, client=fake.client))
    return fake


@pytest.fixture
def no_stripe_allowed(monkeypatch):
    def refuse():
        raise AssertionError("Stripe was used")

    monkeypatch.setattr(admin, "open_credits", refuse)


def bought(store, used: int = 0, purchase_id: str = "pi_bought"):
    raw, key = store.create_key("free")
    store.add_credit_purchase(purchase_id, key.id, pack="starter", screenshots=5000, amount_cents=1900, currency="usd")
    for _ in range(used):
        store.consume_credit(key.id)
    return key


def test_admin_shows_a_keys_credit(admin_store, capsys):
    key = bought(admin_store, used=2)
    assert admin.main(["credits", str(key.id)]) == 0
    out = capsys.readouterr().out
    assert "credits\t4998" in out and "pi_bought" in out and "paid=$19.00 usd" in out and " paid" in out
    assert admin.main(["credits", "999999"]) == 1


def test_refund_is_a_dry_run_without_yes(admin_store, no_stripe_allowed, capsys):
    key = bought(admin_store, used=10)
    assert admin.main(["refund-credit", "pi_bought"]) == 0
    err = capsys.readouterr().err
    assert "Would refund" in err and "take back 4990 of" in err and "10 of this pack are already used" in err
    assert "refund $19.00 of $19.00 through Stripe" in err and "--yes" in err
    assert admin_store.get_credit_balance(key.id) == 4990
    assert admin_store.get_credit_purchase("pi_bought").refunded_at is None


def test_refund_takes_the_unused_credit_back_and_refunds_in_full(admin_store, admin_payments, capsys):
    key = bought(admin_store, used=10)
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 0
    admin_payments.client.v1.refunds.create.assert_called_once_with(
        params={
            "payment_intent": "pi_bought",
            "amount": 1900,
            "reason": "requested_by_customer",
            "metadata": {"calotype_key_id": str(key.id), "calotype_credit_pack": "starter"},
        },
        options={"idempotency_key": "calotype_credit_refund_pi_bought"},
    )
    assert admin_store.get_credit_balance(key.id) == 0
    purchase = admin_store.get_credit_purchase("pi_bought")
    assert (purchase.credits_removed, purchase.refund_id) == (4990, "re_test_1") and purchase.refunded_at
    err = capsys.readouterr().err
    assert "took back 4990 unused credits" in err and "refunded $19.00 through Stripe: re_test_1" in err


def test_a_prorated_refund_returns_the_unused_share(admin_store, admin_payments, capsys):
    key = bought(admin_store, used=1250)  # a quarter used
    assert admin.main(["refund-credit", "pi_bought", "--prorated"]) == 0
    assert "refund $14.25 of $19.00" in capsys.readouterr().err
    assert admin.main(["refund-credit", "pi_bought", "--prorated", "--yes"]) == 0
    assert admin_payments.client.v1.refunds.create.call_args.kwargs["params"]["amount"] == 1425
    assert admin_store.get_credit_balance(key.id) == 0


def test_a_prorated_refund_of_a_used_up_pack_refunds_nothing(admin_store, admin_payments, capsys):
    raw, key = admin_store.create_key("free")
    admin_store.add_credit_purchase("pi_small", key.id, pack="starter", screenshots=2, amount_cents=1900, currency="usd")
    admin_store.consume_credit(key.id), admin_store.consume_credit(key.id)
    assert admin.main(["refund-credit", "pi_small", "--prorated", "--yes"]) == 0
    admin_payments.client.v1.refunds.create.assert_not_called()
    assert "no refund was made" in capsys.readouterr().err


def test_refund_only_takes_this_packs_share(admin_store, admin_payments):
    key = bought(admin_store, purchase_id="pi_a")
    admin_store.add_credit_purchase("pi_b", key.id, pack="starter", screenshots=5000, amount_cents=1900, currency="usd")
    assert admin.main(["refund-credit", "pi_a", "--yes"]) == 0
    assert admin_store.get_credit_balance(key.id) == 5000
    assert admin_store.get_credit_purchase("pi_b").refunded_at is None


def test_refund_twice_refunds_once(admin_store, admin_payments, capsys):
    key = bought(admin_store)
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 0
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 0
    admin_payments.client.v1.refunds.create.assert_called_once()
    assert "already refunded" in capsys.readouterr().err


def test_refund_without_stripe_configured_changes_nothing(admin_store, monkeypatch, capsys):
    monkeypatch.setattr(admin, "open_credits", credits_for_admin)  # the real one; the env has no Stripe
    key = bought(admin_store)
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "STRIPE_SECRET_KEY is not set" in err and "Nothing was changed" in err and "--no-stripe" in err
    assert admin_store.get_credit_balance(key.id) == 5000
    assert admin_store.get_credit_purchase("pi_bought").refunded_at is None


def test_refund_with_no_stripe_takes_the_credit_and_warns(admin_store, no_stripe_allowed, capsys):
    key = bought(admin_store, used=1)
    assert admin.main(["refund-credit", "pi_bought", "--yes", "--no-stripe"]) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "was NOT refunded" in err and "$19.00 by hand" in err
    assert admin_store.get_credit_balance(key.id) == 0
    purchase = admin_store.get_credit_purchase("pi_bought")
    assert purchase.refunded_at and purchase.credits_removed == 4999 and purchase.refund_id is None


def test_a_refund_stripe_refuses_can_be_finished_later(admin_store, admin_payments, capsys):
    key = bought(admin_store, used=100)
    ok = admin_payments.client.v1.refunds.create.side_effect
    admin_payments.client.v1.refunds.create.side_effect = stripe.APIConnectionError("network down")
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "could not refund" in err and "Run the same command again" in err and "network down" not in err
    assert admin_store.get_credit_balance(key.id) == 0  # the credit is back with us; the money is still owed

    admin_payments.client.v1.refunds.create.side_effect = ok
    assert admin.main(["refund-credit", "pi_bought", "--yes"]) == 0
    err = capsys.readouterr().err
    assert "had already been taken back" in err and "refunded $19.00" in err
    purchase = admin_store.get_credit_purchase("pi_bought")
    assert purchase.credits_removed == 4900 and purchase.refund_id
    # Both attempts carried the same idempotency key, so Stripe makes one refund whatever happened to the first.
    keys = {c.kwargs["options"]["idempotency_key"] for c in admin_payments.client.v1.refunds.create.call_args_list}
    assert keys == {"calotype_credit_refund_pi_bought"}


def test_refund_of_an_unknown_purchase(admin_store, no_stripe_allowed, capsys):
    assert admin.main(["refund-credit", "pi_nope", "--yes"]) == 1
    assert "no credit purchase" in capsys.readouterr().err


def test_delete_key_says_what_credit_goes_with_it(admin_store, capsys):
    key = bought(admin_store, used=1)
    assert admin.main(["delete-key", str(key.id)]) == 0
    err = capsys.readouterr().err
    assert "4999 unused prepaid credits and 1 purchase records" in err and "refund-credit" in err
    assert admin.main(["delete-key", str(key.id), "--yes"]) == 0
    assert admin_store.get_credit_purchase("pi_bought") is None


def test_refund_amount_is_bounded(credits_config):
    from calotype.storage import CreditPurchase

    provider = StripeCredits(credits_config, client=MagicMock())
    purchase = CreditPurchase("pi_x", 1, "starter", 5000, 1900, "usd", "", "stripe", "2026-11-20T00:00:00+00:00")
    for amount in (0, -1, 1901):
        with pytest.raises(ValueError):
            provider.refund(purchase, amount)
    with pytest.raises(ValueError):
        provider.refund(replace(purchase, id="not-an-intent"), 100)
