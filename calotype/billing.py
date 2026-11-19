"""Paid plans through Stripe Checkout, the customer portal and webhooks.

How a key gets a paid plan:

1. ``POST /v1/billing/checkout`` with a free key and ``{"plan": "starter"}``
   creates a Checkout Session in subscription mode. The session carries the
   key's internal id (a small integer) as ``client_reference_id`` and in
   metadata, on the session and on the subscription. Never the key itself,
   and never its hash.
2. The buyer pays on Stripe's page. Stripe sends ``checkout.session.completed``
   to ``POST /v1/billing/webhook``, and later ``customer.subscription.*`` events
   as the subscription changes or ends.
3. The webhook checks Stripe's signature, then reads the subscription's current
   state from Stripe (so events that arrive late or out of order cannot undo a
   newer change), maps its price to a plan, and stores the plan with the Stripe
   customer id and subscription id. Each event id is recorded once handled, so
   a repeated delivery changes nothing.
4. ``GET /v1/billing/portal`` returns a Stripe customer-portal link for a key
   that has a Stripe customer, to change card, plan or cancel.
5. When the operator revokes or deletes a paid key (``python -m calotype.admin``),
   ``cancel_subscription`` ends its subscription: at once and without a
   prorated refund by default, or at the end of the paid month on request.
   The ``customer.subscription.*`` events that follow move the key's plan as
   usual and never un-revoke it.

Configuration, all from the environment (on Cloud Run the two secrets come from
Secret Manager references; see deploy/README.md):

``STRIPE_SECRET_KEY``       the API secret key (``sk_live_...`` or ``sk_test_...``)
``STRIPE_WEBHOOK_SECRET``   the webhook endpoint's signing secret (``whsec_...``)
``STRIPE_PRICE_STARTER``    the recurring Price id for the starter plan
``STRIPE_PRICE_PRO``        the recurring Price id for the pro plan

If any is missing, ``billing_from_env`` returns ``NoBilling``: the billing
endpoints answer 503 and everything else works as before. The admin CLI needs
only ``STRIPE_SECRET_KEY``, to cancel subscriptions; see ``stripe_for_admin``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .logs import log_event

if TYPE_CHECKING:
    from .storage import ApiKey, Storage

log = logging.getLogger("calotype.billing")

# Plans that can be bought, and the variable holding each one's Stripe Price id.
PRICE_ENV = {"starter": "STRIPE_PRICE_STARTER", "pro": "STRIPE_PRICE_PRO"}
REQUIRED_ENV = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", *PRICE_ENV.values())

# Subscription statuses that keep the paid plan. ``past_due`` keeps it while
# Stripe retries the card; when retries run out the status moves to
# ``canceled`` or ``unpaid`` and the key goes back to free. ``incomplete``
# (first payment not yet through) does not grant the plan.
LIVE_STATUSES = frozenset({"active", "trialing", "past_due"})
# Statuses after which there is nothing left to cancel.
ENDED_STATUSES = frozenset({"canceled", "incomplete_expired"})

# What ``cancel_subscription`` did.
CANCELED = "canceled"  # ended now; no further charge, no prorated refund
CANCELS_AT_PERIOD_END = "cancels_at_period_end"  # runs to the end of the paid month, then ends
ALREADY_ENDED = "already_ended"  # it had ended before; nothing was changed

HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)

METADATA_KEY_ID = "calotype_key_id"
UNAVAILABLE_MESSAGE = (
    "Card checkout is not set up on this server. Email skewbald@agentmail.to to ask about paid plans."
)

_EVENT_ID = re.compile(r"^evt_[A-Za-z0-9_]{1,250}$")
_STRIPE_ID = re.compile(r"^[a-z]{2,8}_[A-Za-z0-9_]{1,250}$")


class BillingUnavailable(Exception):
    """Billing is not configured on this server."""



class InvalidWebhook(Exception):
    """The webhook's signature, timestamp or body did not check out."""


class BillingProviderError(Exception):
    """Stripe could not be reached or refused the request."""


@dataclass(frozen=True)
class CheckoutSession:
    url: str
    provider_session_id: str


@dataclass(frozen=True)
class EventResult:
    outcome: str  # applied, duplicate, ignored, stale, unknown_key, unknown_price
    key_id: int | None = None
    plan: str | None = None


@dataclass(frozen=True)
class StripeConfig:
    secret_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    prices: Mapping[str, str] = field(default_factory=dict)  # plan -> Stripe Price id
    webhook_tolerance_s: int = 300
    # Stripe Tax: Checkout works out and adds sales tax where the account is
    # registered. Prices are
    # tax-exclusive, so the listed $19 and $49 stay as listed.
    automatic_tax: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> tuple["StripeConfig | None", list[str]]:
        """The config, or None and the names (never the values) of missing variables."""
        env = os.environ if environ is None else environ
        values = {name: env.get(name, "").strip() for name in REQUIRED_ENV}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return None, missing
        return (
            cls(
                secret_key=values["STRIPE_SECRET_KEY"],
                webhook_secret=values["STRIPE_WEBHOOK_SECRET"],
                prices={plan: values[var] for plan, var in PRICE_ENV.items()},
                automatic_tax=env.get("STRIPE_AUTOMATIC_TAX", "").strip().lower() in ("1", "true", "yes"),
            ),
            [],
        )


class BillingProvider(Protocol):
    configured: bool

    def plans_for_sale(self) -> list[str]: ...

    def create_checkout(self, key: "ApiKey", plan: str, success_url: str, cancel_url: str) -> CheckoutSession: ...

    def create_portal(self, customer_id: str, return_url: str) -> str: ...

    def verify_webhook(self, payload: bytes, signature_header: str | None) -> dict[str, Any]: ...

    def handle_event(self, event: dict[str, Any], store: "Storage") -> EventResult: ...

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = False) -> str: ...


class NoBilling:
    """The provider when Stripe is not configured: every call is refused."""

    configured = False

    def __init__(self, missing: list[str] | None = None):
        self.missing = list(missing or [])

    def plans_for_sale(self) -> list[str]:
        return []

    def create_checkout(self, key, plan, success_url, cancel_url) -> CheckoutSession:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def create_portal(self, customer_id, return_url) -> str:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def verify_webhook(self, payload, signature_header) -> dict[str, Any]:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def handle_event(self, event, store) -> EventResult:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def cancel_subscription(self, subscription_id, *, at_period_end=False) -> str:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)


class StripeBilling:
    """Stripe Checkout (subscription mode), the customer portal and webhooks.

    ``client`` is a ``stripe.StripeClient``; tests pass a stand-in so that no
    call reaches Stripe.
    """

    configured = True

    def __init__(self, config: StripeConfig, *, client: Any = None):
        self.config = config
        if client is None:
            import stripe

            client = stripe.StripeClient(config.secret_key, max_network_retries=2)
        self._client = client
        self._plan_by_price = {price: plan for plan, price in config.prices.items()}

    def plans_for_sale(self) -> list[str]:
        return list(self.config.prices)

    # --- checkout and portal ---------------------------------------------------------

    def create_checkout(self, key: "ApiKey", plan: str, success_url: str, cancel_url: str) -> CheckoutSession:
        price = self.config.prices.get(plan)
        if not price:
            raise ValueError(f"plan {plan!r} is not for sale")
        # The key's internal id only. The key and its hash never leave Calotype.
        metadata = {METADATA_KEY_ID: str(key.id), "plan": plan}
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "client_reference_id": str(key.id),
            "metadata": metadata,
            "subscription_data": {"metadata": metadata},
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if key.stripe_customer_id:
            # A returning buyer: keep one Stripe customer per key.
            params["customer"] = key.stripe_customer_id
        if self.config.automatic_tax:
            params["automatic_tax"] = {"enabled": True}
            if key.stripe_customer_id:
                # Stripe Tax needs the buyer's address; let Checkout save the one
                # entered now onto the returning customer.
                params["customer_update"] = {"address": "auto"}
        session = self._call(self._client.v1.checkout.sessions.create, params=params)
        url = _get(session, "url")
        if not url:
            raise BillingProviderError("Stripe returned a session without a URL")
        return CheckoutSession(url=url, provider_session_id=_get(session, "id") or "")

    def create_portal(self, customer_id: str, return_url: str) -> str:
        session = self._call(
            self._client.v1.billing_portal.sessions.create,
            params={"customer": customer_id, "return_url": return_url},
        )
        url = _get(session, "url")
        if not url:
            raise BillingProviderError("Stripe returned a portal session without a URL")
        return url

    # --- cancellation ------------------------------------------------------------------

    def cancel_subscription(self, subscription_id: str, *, at_period_end: bool = False) -> str:
        """End a subscription. Returns ``CANCELED``, ``CANCELS_AT_PERIOD_END`` or ``ALREADY_ENDED``.

        By default it ends now, with no prorated refund for the rest of the
        month and no final invoice. With ``at_period_end`` it runs to the end
        of the month already paid for and is not renewed. A subscription that
        has already ended is left alone, so running this twice is safe.

        Raises ``BillingProviderError`` if Stripe cannot be reached or refuses.
        """
        if not isinstance(subscription_id, str) or not _STRIPE_ID.match(subscription_id):
            raise ValueError("not a Stripe subscription id")
        sub = _plain(self._call(self._client.v1.subscriptions.retrieve, subscription_id))
        if sub.get("status") in ENDED_STATUSES:
            outcome = ALREADY_ENDED
        elif at_period_end:
            self._call(
                self._client.v1.subscriptions.update, subscription_id, params={"cancel_at_period_end": True}
            )
            outcome = CANCELS_AT_PERIOD_END
        else:
            self._call(
                self._client.v1.subscriptions.cancel,
                subscription_id,
                params={"prorate": False, "invoice_now": False},
            )
            outcome = CANCELED
        log_event(log, "subscription cancelled", subscription_id=subscription_id, outcome=outcome)
        return outcome

    # --- webhooks ----------------------------------------------------------------------

    def verify_webhook(self, payload: bytes, signature_header: str | None) -> dict[str, Any]:
        """Check the ``Stripe-Signature`` header and return the event as a dict."""
        import stripe

        try:
            stripe.WebhookSignature.verify_header(
                payload, signature_header, self.config.webhook_secret, self.config.webhook_tolerance_s
            )
        except stripe.SignatureVerificationError:
            raise InvalidWebhook("The Stripe signature is missing, wrong or too old.") from None
        try:
            event = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise InvalidWebhook("The webhook body is not JSON.") from None
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("id"), str)
            or not _EVENT_ID.match(event["id"])
            or not isinstance(event.get("type"), str)
        ):
            raise InvalidWebhook("The webhook body is not a Stripe event.")
        return event

    def handle_event(self, event: dict[str, Any], store: "Storage") -> EventResult:
        """Apply one verified event. Safe to call again with the same event.

        Raises ``BillingProviderError`` if Stripe cannot be reached; the caller
        answers 500 so that Stripe delivers the event again later.
        """
        event_id, event_type = event["id"], event["type"]
        if event_type not in HANDLED_EVENTS:
            return EventResult("ignored")
        if store.event_processed(event_id):
            return EventResult("duplicate")

        result = self._apply(event_type, _event_object(event), store)
        # Recorded only after the change is stored: if anything above failed,
        # Stripe's retry runs it again. Applying twice is harmless because the
        # state comes from Stripe, not from the event.
        store.mark_event_processed(event_id)
        log_event(
            log,
            "billing event",
            event_id=event_id,
            event_type=event_type,
            outcome=result.outcome,
            key_id=result.key_id,
            plan=result.plan,
        )
        return result

    def _apply(self, event_type: str, obj: dict[str, Any], store: "Storage") -> EventResult:
        if event_type == "checkout.session.completed":
            if obj.get("mode") != "subscription":
                return EventResult("ignored")
            subscription_id = _ref_id(obj.get("subscription"))
            key_id = _int_or_none(obj.get("client_reference_id")) or _metadata_key_id(obj)
        else:
            subscription_id = _ref_id(obj.get("id"))
            key_id = _metadata_key_id(obj)
        if not subscription_id or not _STRIPE_ID.match(subscription_id):
            return EventResult("ignored")

        # The subscription as it is now, not as it was when the event was sent.
        sub = _plain(self._call(self._client.v1.subscriptions.retrieve, subscription_id))
        customer_id = _ref_id(sub.get("customer")) or _ref_id(obj.get("customer"))
        if not customer_id or not _STRIPE_ID.match(customer_id):
            return EventResult("ignored")
        key_id = key_id or _metadata_key_id(sub)

        key = store.get_key_by_id(key_id) if key_id else None
        if key is None:
            key = store.find_key_by_subscription(subscription_id)
        if key is None:
            log_event(log, "billing event for an unknown key", level=logging.WARNING, subscription_id=subscription_id)
            return EventResult("unknown_key")

        live = sub.get("status") in LIVE_STATUSES
        if live:
            plan = self._plan_for(sub)
            if plan is None:
                log_event(
                    log,
                    "subscription has a price that maps to no plan",
                    level=logging.ERROR,
                    key_id=key.id,
                    subscription_id=subscription_id,
                )
                return EventResult("unknown_price", key.id)
        else:
            plan = "free"

        if key.stripe_subscription_id and key.stripe_subscription_id != subscription_id:
            if not live:
                # An older subscription ended after the key moved to a newer one.
                return EventResult("stale", key.id)
            log_event(
                log,
                "key moves to another subscription",
                level=logging.WARNING,
                key_id=key.id,
                old_subscription_id=key.stripe_subscription_id,
                subscription_id=subscription_id,
            )

        store.set_billing(key.id, plan, customer_id=customer_id, subscription_id=subscription_id)
        return EventResult("applied", key.id, plan)

    def _plan_for(self, sub: dict[str, Any]) -> str | None:
        items = sub.get("items") or {}
        for item in items.get("data") or []:
            price = _ref_id((item or {}).get("price"))
            if price in self._plan_by_price:
                return self._plan_by_price[price]
        return None

    @staticmethod
    def _call(fn, *args, **kwargs):
        import stripe

        try:
            return fn(*args, **kwargs)
        except stripe.StripeError as e:
            # The type and Stripe's code only: messages can echo request values.
            log_event(
                log,
                "stripe call failed",
                level=logging.ERROR,
                error=type(e).__name__,
                code=getattr(e, "code", None),
                http_status=getattr(e, "http_status", None),
            )
            raise BillingProviderError(type(e).__name__) from None


def billing_from_env(environ: Mapping[str, str] | None = None) -> BillingProvider:
    """``StripeBilling`` if every Stripe variable is set, otherwise ``NoBilling``."""
    config, missing = StripeConfig.from_env(environ)
    if config is None:
        return NoBilling(missing)
    return StripeBilling(config)


def stripe_for_admin(environ: Mapping[str, str] | None = None, *, client: Any = None) -> StripeBilling | None:
    """A Stripe provider for the admin CLI, or None if ``STRIPE_SECRET_KEY`` is not set.

    Cancelling a subscription needs only the secret key, so the webhook secret
    and Price ids may be missing here; the provider is used for nothing else.
    """
    env = os.environ if environ is None else environ
    secret = env.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        return None
    config, _missing = StripeConfig.from_env(env)
    if config is None:
        config = StripeConfig(secret_key=secret, webhook_secret="")
    return StripeBilling(config, client=client)


# --- helpers -----------------------------------------------------------------------------


def _plain(obj: Any) -> dict[str, Any]:
    """A Stripe object as a plain dict (StripeObject is not a dict subclass)."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj or {})


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def _ref_id(value: Any) -> str | None:
    """An id from a field that is either an id string or an expanded object."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        inner = value.get("id")
        return inner if isinstance(inner, str) and inner else None
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, str) and value.isdigit() and len(value) <= 18:
        number = int(value)
        return number if number > 0 else None
    return None


def _metadata_key_id(obj: Mapping[str, Any]) -> int | None:
    metadata = obj.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return _int_or_none(metadata.get(METADATA_KEY_ID))
