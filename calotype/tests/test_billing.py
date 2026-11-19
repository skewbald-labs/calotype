"""Stripe Checkout, the customer portal and webhooks.

Nothing here reaches Stripe. The Stripe client is a stand-in (a MagicMock, or
the real ``stripe.StripeClient`` over a fake HTTP transport), and webhook
signatures are computed here with HMAC-SHA256 over a fake secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import stripe
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.billing import NoBilling, StripeBilling, StripeConfig, billing_from_env
from calotype.ssrf import AddressPolicy
from calotype.storage import hash_key

from .conftest import fake_resolver, stored_text

WEBHOOK_SECRET = "whsec_test_not_a_real_secret"
SECRET_KEY = "sk_test_not_a_real_key"
PRICE_STARTER = "price_starter_test"
PRICE_PRO = "price_pro_test"
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_123"
PORTAL_URL = "https://billing.stripe.com/p/session/test_456"
ENV = {
    "STRIPE_SECRET_KEY": SECRET_KEY,
    "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "STRIPE_PRICE_STARTER": PRICE_STARTER,
    "STRIPE_PRICE_PRO": PRICE_PRO,
}


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """A Stripe-Signature header, computed here the way Stripe documents it."""
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event(event_id: str, event_type: str, obj: dict) -> bytes:
    return json.dumps({"id": event_id, "object": "event", "type": event_type, "data": {"object": obj}}).encode()


def session_completed(key_id: int, sub: str = "sub_1", customer: str = "cus_1") -> dict:
    return {
        "id": "cs_test_123",
        "object": "checkout.session",
        "mode": "subscription",
        "client_reference_id": str(key_id),
        "customer": customer,
        "subscription": sub,
        "metadata": {"calotype_key_id": str(key_id), "plan": "starter"},
    }


def subscription(sub: str, status: str, price: str, key_id: int | None, customer: str = "cus_1") -> dict:
    return {
        "id": sub,
        "object": "subscription",
        "status": status,
        "customer": customer,
        "metadata": {"calotype_key_id": str(key_id)} if key_id else {},
        "items": {"object": "list", "data": [{"id": "si_1", "price": {"id": price}}]},
    }


class FakeStripe:
    """Stands in for ``stripe.StripeClient``: records calls, returns canned objects."""

    def __init__(self):
        self.client = MagicMock(name="StripeClient")
        self.subscriptions: dict[str, dict] = {}
        self.client.v1.checkout.sessions.create.return_value = SimpleNamespace(url=CHECKOUT_URL, id="cs_test_123")
        self.client.v1.billing_portal.sessions.create.return_value = SimpleNamespace(url=PORTAL_URL)
        self.client.v1.subscriptions.retrieve.side_effect = lambda sub_id: self.subscriptions[sub_id]

    @property
    def checkout_create(self) -> MagicMock:
        return self.client.v1.checkout.sessions.create

    @property
    def portal_create(self) -> MagicMock:
        return self.client.v1.billing_portal.sessions.create

    @property
    def retrieve(self) -> MagicMock:
        return self.client.v1.subscriptions.retrieve


@pytest.fixture
def stripe_config() -> StripeConfig:
    config, missing = StripeConfig.from_env(ENV)
    assert missing == []
    return config


@pytest.fixture
def fake_stripe() -> FakeStripe:
    return FakeStripe()


@pytest.fixture
def billing(stripe_config, fake_stripe) -> StripeBilling:
    return StripeBilling(stripe_config, client=fake_stripe.client)


@pytest.fixture
def bclient(settings, store, fake_renderer, billing):
    app = create_app(
        settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer, billing=billing
    )
    with TestClient(app) as c:
        yield c


def post_event(c: TestClient, payload: bytes, signature: str | None = "auto"):
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["Stripe-Signature"] = sign(payload)
    elif signature is not None:
        headers["Stripe-Signature"] = signature
    return c.post("/v1/billing/webhook", content=payload, headers=headers)


# --- configuration -------------------------------------------------------------------


def test_config_needs_every_variable():
    for missing in ENV:
        env = {k: v for k, v in ENV.items() if k != missing}
        config, names = StripeConfig.from_env(env)
        assert config is None and names == [missing]
        assert isinstance(billing_from_env(env), NoBilling)
    config, names = StripeConfig.from_env({**ENV, "STRIPE_PRICE_PRO": "  "})
    assert config is None and names == ["STRIPE_PRICE_PRO"]


def test_config_never_shows_secrets(stripe_config):
    text = repr(stripe_config)
    assert SECRET_KEY not in text and WEBHOOK_SECRET not in text
    assert stripe_config.prices == {"starter": PRICE_STARTER, "pro": PRICE_PRO}


def test_billing_from_env_builds_a_real_client_without_calling_stripe():
    provider = billing_from_env(ENV)
    assert isinstance(provider, StripeBilling) and provider.configured
    assert isinstance(provider._client, stripe.StripeClient)
    assert provider.plans_for_sale() == ["starter", "pro"]


def test_app_reads_stripe_from_the_environment(monkeypatch, settings, store, fake_renderer):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer)
    assert isinstance(app.state.billing, StripeBilling)


# --- without Stripe: 503, and everything else keeps working ------------------------------


def test_billing_endpoints_503_without_stripe(client, free_key):
    for method, path, kwargs in [
        ("POST", "/v1/billing/checkout", {"json": {"plan": "starter"}, "headers": auth(free_key)}),
        ("GET", "/v1/billing/portal", {"headers": auth(free_key)}),
        ("POST", "/v1/billing/webhook", {"content": b"{}", "headers": {"Stripe-Signature": "t=1,v1=00"}}),
        ("POST", "/v1/billing/checkout", {"json": {"plan": "starter"}}),  # even without a key
    ]:
        r = client.request(method, path, **kwargs)
        assert r.status_code == 503, (path, r.text)
        err = r.json()["error"]
        assert err["code"] == "billing_unavailable"
        assert "not set up" in err["message"] and "skewbald@agentmail.to" in err["message"]
        # Names of the missing variables are logged at startup, not sent to clients.
        assert "STRIPE" not in r.text


def test_everything_else_works_without_stripe(client, free_key):
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/plans").status_code == 200
    assert client.get("/").status_code == 200
    assert client.post("/v1/keys").status_code == 201
    r = client.get("/v1/screenshot", params={"url": "https://example.com"}, headers=auth(free_key))
    assert r.status_code == 200
    assert client.get("/v1/usage", headers=auth(free_key)).json()["plan"] == "free"


def test_startup_logs_missing_variable_names_only(settings, store, fake_renderer, capsys):
    app = create_app(
        settings,
        policy=AddressPolicy(resolver=fake_resolver),
        storage=store,
        renderer=fake_renderer,
        billing=NoBilling(["STRIPE_SECRET_KEY"]),
    )
    with TestClient(app):
        pass
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    off = [line for line in lines if line["message"] == "billing is off"]
    assert off and off[0]["missing"] == ["STRIPE_SECRET_KEY"]


# --- checkout ------------------------------------------------------------------------------


def test_checkout_creates_a_subscription_session(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    r = bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(raw))
    assert r.status_code == 200, r.text
    assert r.json() == {"url": CHECKOUT_URL, "plan": "starter"}
    assert r.headers["cache-control"] == "no-store"

    fake_stripe.checkout_create.assert_called_once()
    params = fake_stripe.checkout_create.call_args.kwargs["params"]
    assert params["mode"] == "subscription"
    assert params["line_items"] == [{"price": PRICE_STARTER, "quantity": 1}]
    assert params["client_reference_id"] == str(key.id)
    assert params["metadata"] == {"calotype_key_id": str(key.id), "plan": "starter"}
    assert params["subscription_data"] == {"metadata": {"calotype_key_id": str(key.id), "plan": "starter"}}
    assert params["success_url"] == "http://testserver/pricing?checkout=success#upgrade"
    assert params["cancel_url"] == "http://testserver/pricing?checkout=cancelled#upgrade"
    assert "customer" not in params
    # The key and its hash never go to Stripe.
    sent = repr(fake_stripe.checkout_create.call_args)
    assert raw not in sent and hash_key(raw) not in sent


def test_checkout_pro_uses_the_pro_price(bclient, free_key, fake_stripe):
    r = bclient.post("/v1/billing/checkout", json={"plan": "pro"}, headers=auth(free_key))
    assert r.status_code == 200
    assert fake_stripe.checkout_create.call_args.kwargs["params"]["line_items"][0]["price"] == PRICE_PRO


def test_checkout_uses_the_public_url(settings, store, fake_renderer, billing, fake_stripe, free_key):
    from dataclasses import replace

    app = create_app(
        replace(settings, public_url="https://calotype.example"),
        policy=AddressPolicy(resolver=fake_resolver),
        storage=store,
        renderer=fake_renderer,
        billing=billing,
    )
    with TestClient(app) as c:
        c.post("/v1/billing/checkout", json={"plan": "starter"}, headers={**auth(free_key), "Host": "evil.example"})
    params = fake_stripe.checkout_create.call_args.kwargs["params"]
    assert params["success_url"] == "https://calotype.example/pricing?checkout=success#upgrade"


def test_checkout_trusts_forwarded_proto_only_behind_a_proxy(settings, store, fake_renderer, billing, fake_stripe, free_key):
    from dataclasses import replace

    for hops, expected in [(0, "http://"), (1, "https://")]:
        app = create_app(
            replace(settings, trusted_proxy_hops=hops),
            policy=AddressPolicy(resolver=fake_resolver),
            storage=store,
            renderer=fake_renderer,
            billing=billing,
        )
        with TestClient(app) as c:
            c.post(
                "/v1/billing/checkout",
                json={"plan": "starter"},
                headers={**auth(free_key), "X-Forwarded-Proto": "https"},
            )
        assert fake_stripe.checkout_create.call_args.kwargs["params"]["success_url"].startswith(expected)


def test_checkout_needs_a_valid_key(bclient, store, fake_stripe):
    assert bclient.post("/v1/billing/checkout", json={"plan": "starter"}).status_code == 401
    assert bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth("cal_nope")).status_code == 401
    raw, key = store.create_key("free")
    store.revoke_key(key.id)
    assert bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(raw)).status_code == 401
    fake_stripe.checkout_create.assert_not_called()


@pytest.mark.parametrize("body", [{"plan": "free"}, {"plan": "enterprise"}, {}, {"plan": None}, ["starter"]])
def test_checkout_refuses_plans_not_for_sale(bclient, free_key, fake_stripe, body):
    r = bclient.post("/v1/billing/checkout", json=body, headers=auth(free_key))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_plan"
    assert r.json()["error"]["plans"] == ["starter", "pro"]
    fake_stripe.checkout_create.assert_not_called()


def test_checkout_refuses_a_body_that_is_not_json(bclient, free_key):
    r = bclient.post("/v1/billing/checkout", content=b"plan=starter", headers=auth(free_key))
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"


def test_checkout_refuses_a_key_already_on_a_paid_plan(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_1", subscription_id="sub_1")
    r = bclient.post("/v1/billing/checkout", json={"plan": "pro"}, headers=auth(raw))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_subscribed"
    assert "/v1/billing/portal" in r.json()["error"]["message"]
    raw2, _ = store.create_key("pro", "ops", internal=True)  # set by an operator, no Stripe customer
    r = bclient.post("/v1/billing/checkout", json={"plan": "pro"}, headers=auth(raw2))
    assert r.status_code == 409 and "skewbald@agentmail.to" in r.json()["error"]["message"]
    fake_stripe.checkout_create.assert_not_called()


def test_checkout_reuses_the_stripe_customer_after_a_cancellation(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "free", customer_id="cus_back", subscription_id="sub_old")
    r = bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(raw))
    assert r.status_code == 200
    assert fake_stripe.checkout_create.call_args.kwargs["params"]["customer"] == "cus_back"


def test_checkout_stripe_failure_is_502(bclient, free_key, fake_stripe):
    fake_stripe.checkout_create.side_effect = stripe.APIConnectionError("network down")
    r = bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(free_key))
    assert r.status_code == 502 and r.json()["error"]["code"] == "billing_error"


def test_checkout_through_the_real_stripe_client(stripe_config, store):
    """The real StripeClient, over a fake transport: the request Stripe would get, and the parsed reply."""

    class FakeTransport(stripe.HTTPClient):
        name = "fake"

        def __init__(self):
            super().__init__()
            self.calls = []

        def request(self, method, url, headers, post_data=None, *, _usage=None):
            self.calls.append((method, url, headers, post_data))
            body = {"id": "cs_test_real", "object": "checkout.session", "url": CHECKOUT_URL, "mode": "subscription"}
            return json.dumps(body), 200, {"request-id": "req_test"}

        def close(self):
            pass

    transport = FakeTransport()
    provider = StripeBilling(stripe_config, client=stripe.StripeClient(SECRET_KEY, http_client=transport))
    raw, key = store.create_key("free")
    session = provider.create_checkout(key, "pro", "https://x.example/ok", "https://x.example/no")
    assert (session.url, session.provider_session_id) == (CHECKOUT_URL, "cs_test_real")

    [(method, url, headers, post_data)] = transport.calls
    assert method == "post" and url == "https://api.stripe.com/v1/checkout/sessions"
    assert headers["Authorization"] == f"Bearer {SECRET_KEY}"
    form = urllib.parse.parse_qs(post_data)
    assert form["mode"] == ["subscription"]
    assert form["line_items[0][price]"] == [PRICE_PRO]
    assert form["client_reference_id"] == [str(key.id)]
    assert form["metadata[calotype_key_id]"] == [str(key.id)]
    assert form["subscription_data[metadata][calotype_key_id]"] == [str(key.id)]
    assert raw not in post_data and hash_key(raw) not in post_data


# --- portal --------------------------------------------------------------------------------


def test_portal_for_a_key_with_a_customer(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_P", subscription_id="sub_P")
    r = bclient.get("/v1/billing/portal", headers=auth(raw))
    assert r.status_code == 200, r.text
    assert r.json() == {"url": PORTAL_URL}
    fake_stripe.portal_create.assert_called_once_with(
        params={"customer": "cus_P", "return_url": "http://testserver/pricing#upgrade"}
    )


def test_portal_without_a_customer_is_404(bclient, free_key, fake_stripe):
    r = bclient.get("/v1/billing/portal", headers=auth(free_key))
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_billing_account"
    fake_stripe.portal_create.assert_not_called()


def test_portal_needs_a_key(bclient):
    assert bclient.get("/v1/billing/portal").status_code == 401


def test_portal_stripe_failure_is_502(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_P", subscription_id="sub_P")
    fake_stripe.portal_create.side_effect = stripe.AuthenticationError("bad key")
    assert bclient.get("/v1/billing/portal", headers=auth(raw)).status_code == 502


# --- webhook signature -------------------------------------------------------------------------


def test_webhook_accepts_a_correct_signature(bclient):
    payload = event("evt_ignored_1", "invoice.paid", {"id": "in_1"})
    r = post_event(bclient, payload)
    assert r.status_code == 200, r.text
    assert r.json() == {"received": True, "outcome": "ignored"}


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "t=notanumber,v1=abc",
        "t=1700000000",  # no v1 signature
        sign(b"something else"),  # signature of another body
        sign(b"", secret="whsec_other"),  # wrong secret (and body)
    ],
)
def test_webhook_refuses_bad_signatures(bclient, header, fake_stripe):
    payload = event("evt_sig", "customer.subscription.deleted", subscription("sub_1", "canceled", PRICE_STARTER, 1))
    r = post_event(bclient, payload, signature=header)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_webhook"
    fake_stripe.retrieve.assert_not_called()


def test_webhook_refuses_the_wrong_secret(bclient, fake_stripe):
    payload = event("evt_sig2", "customer.subscription.deleted", subscription("sub_1", "canceled", PRICE_STARTER, 1))
    r = post_event(bclient, payload, signature=sign(payload, secret="whsec_attacker"))
    assert r.status_code == 400
    fake_stripe.retrieve.assert_not_called()


def test_webhook_refuses_an_old_timestamp(bclient):
    payload = event("evt_old", "invoice.paid", {})
    r = post_event(bclient, payload, signature=sign(payload, timestamp=int(time.time()) - 3600))
    assert r.status_code == 400


def test_webhook_refuses_a_body_changed_after_signing(bclient):
    payload = event("evt_t", "invoice.paid", {})
    header = sign(payload)
    r = post_event(bclient, payload.replace(b"invoice.paid", b"invoice.void"), signature=header)
    assert r.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"type": "invoice.paid"}', b'{"id": "evt/../x", "type": "invoice.paid"}'],
)
def test_webhook_refuses_signed_bodies_that_are_not_events(bclient, payload):
    r = post_event(bclient, payload)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_webhook"


# --- webhook events --------------------------------------------------------------------------


def test_checkout_completed_moves_the_key_to_the_plan(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    r = post_event(bclient, event("evt_c1", "checkout.session.completed", session_completed(key.id)))
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "applied"
    got = store.get_key(raw)
    assert (got.plan, got.stripe_customer_id, got.stripe_subscription_id) == ("starter", "cus_1", "sub_1")
    usage = bclient.get("/v1/usage", headers=auth(raw)).json()
    assert usage["plan"] == "starter" and usage["limit"] == 5000
    fake_stripe.retrieve.assert_called_once_with("sub_1")
    # Only ids are stored: never the key, and nothing else from Stripe.
    dump = stored_text(store)
    assert raw not in dump and "cs_test_123" not in dump


def test_subscription_updated_changes_the_plan(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_1", subscription_id="sub_1")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_PRO, key.id)
    obj = subscription("sub_1", "active", PRICE_PRO, key.id)
    assert post_event(bclient, event("evt_u1", "customer.subscription.updated", obj)).json()["outcome"] == "applied"
    assert store.get_key(raw).plan == "pro"


def test_subscription_deleted_returns_the_key_to_free(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "pro", customer_id="cus_1", subscription_id="sub_1")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_PRO, key.id)
    obj = subscription("sub_1", "canceled", PRICE_PRO, key.id)
    assert post_event(bclient, event("evt_d1", "customer.subscription.deleted", obj)).status_code == 200
    got = store.get_key(raw)
    assert got.plan == "free"
    assert got.stripe_customer_id == "cus_1"  # kept for the portal and a later checkout


@pytest.mark.parametrize(
    "status, plan", [("active", "starter"), ("trialing", "starter"), ("past_due", "starter"),
                     ("incomplete", "free"), ("unpaid", "free"), ("incomplete_expired", "free"), ("paused", "free")]
)
def test_subscription_status_decides_the_plan(bclient, store, fake_stripe, status, plan):
    raw, key = store.create_key("free")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", status, PRICE_STARTER, key.id)
    post_event(bclient, event("evt_s", "customer.subscription.updated", subscription("sub_1", status, PRICE_STARTER, key.id)))
    assert store.get_key(raw).plan == plan


def test_the_current_state_wins_over_a_late_event(bclient, store, fake_stripe):
    """An 'active' event that arrives after the cancellation does not bring the plan back."""
    raw, key = store.create_key("free")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    late = subscription("sub_1", "active", PRICE_STARTER, key.id)  # as it was when the event was sent
    post_event(bclient, event("evt_late", "customer.subscription.updated", late))
    assert store.get_key(raw).plan == "free"


def test_duplicate_events_apply_once(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    payload = event("evt_dup", "checkout.session.completed", session_completed(key.id))
    assert post_event(bclient, payload).json()["outcome"] == "applied"
    store.set_plan(key.id, "free")  # if the repeat were applied, it would undo this
    r = post_event(bclient, payload)
    assert r.status_code == 200 and r.json()["outcome"] == "duplicate"
    assert store.get_key(raw).plan == "free"
    assert fake_stripe.retrieve.call_count == 1
    assert store.event_processed("evt_dup")


def test_a_failed_event_is_not_recorded_and_the_retry_applies(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    payload = event("evt_retry", "checkout.session.completed", session_completed(key.id))
    fake_stripe.retrieve.side_effect = stripe.APIConnectionError("network down")
    r = post_event(bclient, payload)
    assert r.status_code == 500 and r.json()["error"]["code"] == "webhook_failed"
    assert not store.event_processed("evt_retry") and store.get_key(raw).plan == "free"

    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    fake_stripe.retrieve.side_effect = lambda sub_id: fake_stripe.subscriptions[sub_id]
    assert post_event(bclient, payload).json()["outcome"] == "applied"
    assert store.get_key(raw).plan == "starter"


def test_unhandled_event_types_are_not_recorded(bclient, store, fake_stripe):
    r = post_event(bclient, event("evt_other", "invoice.payment_failed", {"id": "in_1"}))
    assert r.json()["outcome"] == "ignored"
    assert not store.event_processed("evt_other")
    fake_stripe.retrieve.assert_not_called()


def test_one_time_payment_sessions_are_ignored(bclient, store, fake_stripe):
    _, key = store.create_key("free")
    obj = {**session_completed(key.id), "mode": "payment", "subscription": None}
    assert post_event(bclient, event("evt_pay", "checkout.session.completed", obj)).json()["outcome"] == "ignored"
    fake_stripe.retrieve.assert_not_called()


def test_subscription_event_found_by_subscription_id(bclient, store, fake_stripe):
    """Without the key id in metadata, the stored subscription id finds the key."""
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_1", subscription_id="sub_1")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_PRO, None)
    post_event(bclient, event("evt_m", "customer.subscription.updated", subscription("sub_1", "active", PRICE_PRO, None)))
    assert store.get_key(raw).plan == "pro"


def test_event_for_an_unknown_key(bclient, store, fake_stripe):
    fake_stripe.subscriptions["sub_x"] = subscription("sub_x", "active", PRICE_STARTER, 999_999)
    obj = subscription("sub_x", "active", PRICE_STARTER, 999_999)
    r = post_event(bclient, event("evt_unk", "customer.subscription.updated", obj))
    assert r.status_code == 200 and r.json()["outcome"] == "unknown_key"


def test_unknown_price_leaves_the_plan_alone(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", "price_someone_else", key.id)
    r = post_event(bclient, event("evt_p", "checkout.session.completed", session_completed(key.id)))
    assert r.json()["outcome"] == "unknown_price"
    assert store.get_key(raw).plan == "free" and store.get_key(raw).stripe_customer_id is None


def test_an_old_subscription_ending_does_not_downgrade_a_newer_one(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    store.set_billing(key.id, "pro", customer_id="cus_1", subscription_id="sub_new")
    fake_stripe.subscriptions["sub_old"] = subscription("sub_old", "canceled", PRICE_STARTER, key.id)
    obj = subscription("sub_old", "canceled", PRICE_STARTER, key.id)
    r = post_event(bclient, event("evt_old_sub", "customer.subscription.deleted", obj))
    assert r.json()["outcome"] == "stale"
    got = store.get_key(raw)
    assert (got.plan, got.stripe_subscription_id) == ("pro", "sub_new")


def test_subscription_deleted_on_a_revoked_key_keeps_it_revoked(bclient, store, fake_stripe):
    """The admin CLI revokes a paid key and cancels its subscription; Stripe's event follows."""
    raw, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_1", subscription_id="sub_1")
    store.revoke_key(key.id)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    obj = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    r = post_event(bclient, event("evt_rev_d", "customer.subscription.deleted", obj))
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    got = store.get_key_by_id(key.id)
    assert got.revoked and got.plan == "free"
    assert bclient.get("/v1/usage", headers=auth(raw)).status_code == 401
    # An "updated" event for a period-end cancellation does not un-revoke it either.
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    post_event(bclient, event("evt_rev_u", "customer.subscription.updated", fake_stripe.subscriptions["sub_1"]))
    assert store.get_key_by_id(key.id).revoked


def test_subscription_deleted_after_the_key_was_deleted(bclient, store, fake_stripe):
    """A period-end cancellation of a deleted key ends later; the event finds no key and is not an error."""
    _, key = store.create_key("free")
    store.set_billing(key.id, "starter", customer_id="cus_1", subscription_id="sub_1")
    store.delete_key(key.id)
    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    obj = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    r = post_event(bclient, event("evt_del_d", "customer.subscription.deleted", obj))
    assert r.status_code == 200 and r.json()["outcome"] == "unknown_key"
    assert store.get_key_by_id(key.id) is None


def test_expanded_stripe_objects_are_read_too(bclient, store, fake_stripe):
    """Customer and subscription may arrive as objects rather than ids."""
    raw, key = store.create_key("free")
    sub = subscription("sub_1", "active", PRICE_STARTER, key.id)
    sub["customer"] = {"id": "cus_obj", "object": "customer"}
    fake_stripe.subscriptions["sub_1"] = sub
    obj = {**session_completed(key.id), "subscription": {"id": "sub_1", "object": "subscription"}}
    post_event(bclient, event("evt_x", "checkout.session.completed", obj))
    assert store.get_key(raw).stripe_customer_id == "cus_obj"


def test_retrieved_stripe_objects_are_converted(stripe_config, store):
    """The real library returns StripeObject, which is not a dict."""
    _, key = store.create_key("free")
    sub = stripe.Subscription.construct_from(subscription("sub_1", "active", PRICE_PRO, key.id), SECRET_KEY)
    fake = FakeStripe()
    fake.retrieve.side_effect = lambda sub_id: sub
    provider = StripeBilling(stripe_config, client=fake.client)
    payload = json.loads(event("evt_so", "customer.subscription.updated", {"id": "sub_1", "metadata": {}}))
    assert provider.handle_event(payload, store).outcome == "applied"
    assert store.get_key_by_id(key.id).plan == "pro"


def test_full_flow_checkout_then_portal_then_cancel(bclient, store, fake_stripe):
    raw, key = store.create_key("free")
    assert bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(raw)).status_code == 200
    assert bclient.get("/v1/billing/portal", headers=auth(raw)).status_code == 404  # not paid yet

    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "active", PRICE_STARTER, key.id)
    post_event(bclient, event("evt_f1", "checkout.session.completed", session_completed(key.id)))
    assert bclient.get("/v1/usage", headers=auth(raw)).json()["limit"] == 5000
    assert bclient.get("/v1/billing/portal", headers=auth(raw)).json() == {"url": PORTAL_URL}

    fake_stripe.subscriptions["sub_1"] = subscription("sub_1", "canceled", PRICE_STARTER, key.id)
    post_event(bclient, event("evt_f2", "customer.subscription.deleted", fake_stripe.subscriptions["sub_1"]))
    assert bclient.get("/v1/usage", headers=auth(raw)).json()["plan"] == "free"


# --- Stripe Tax -----------------------------------------------------------------------------


def test_automatic_tax_is_off_unless_switched_on(bclient, free_key, fake_stripe, stripe_config):
    assert stripe_config.automatic_tax is False
    bclient.post("/v1/billing/checkout", json={"plan": "starter"}, headers=auth(free_key))
    params = fake_stripe.checkout_create.call_args.kwargs["params"]
    assert "automatic_tax" not in params and "customer_update" not in params


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_automatic_tax_from_env(value):
    config, missing = StripeConfig.from_env({**ENV, "STRIPE_AUTOMATIC_TAX": value})
    assert missing == [] and config.automatic_tax is True
    config, _ = StripeConfig.from_env({**ENV, "STRIPE_AUTOMATIC_TAX": "0"})
    assert config.automatic_tax is False


def test_checkout_with_automatic_tax(store, fake_stripe, stripe_config):
    taxed = StripeBilling(replace(stripe_config, automatic_tax=True), client=fake_stripe.client)
    _, new_key = store.create_key("free")
    taxed.create_checkout(new_key, "starter", "https://s", "https://c")
    params = fake_stripe.checkout_create.call_args.kwargs["params"]
    assert params["automatic_tax"] == {"enabled": True}
    assert "customer_update" not in params  # a new buyer: Checkout makes the customer with the address
    _, back = store.create_key("free")
    store.set_billing(back.id, "free", customer_id="cus_back", subscription_id="sub_old")
    taxed.create_checkout(store.get_key_by_id(back.id), "starter", "https://s", "https://c")
    params = fake_stripe.checkout_create.call_args.kwargs["params"]
    assert params["customer"] == "cus_back" and params["customer_update"] == {"address": "auto"}
