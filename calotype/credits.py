"""Prepaid screenshots bought by an agent, with no person and no checkout page.

``POST /v1/credits`` with a key and ``{"pack": "starter"}`` sells a prepaid
pack through Stripe's machine payments, over the Machine Payments Protocol
(MPP; the wire format is in ``mpp.py``):

1. Without a payment the endpoint answers ``402`` with a challenge for the
   pack's price: MPP method ``stripe``, intent ``charge``, the amount in cents
   and the seller's Stripe profile id.
2. The buyer's wallet (for example Stripe's ``link-cli``) gets a shared
   payment token (SPT) for that amount and that profile, and sends the request
   again with a credential holding the token.
3. The credential is checked: issued by this server, for this key and this
   pack, unchanged, not expired. Then one PaymentIntent is created and
   confirmed with the token. Only if Stripe says ``succeeded`` is the pack
   added to the key's prepaid credit, and the new balance returned with a
   ``Payment-Receipt``.

One payment adds credit once. The purchase is recorded under the PaymentIntent
id, in the same atomic step that adds the screenshots (``add_credit_purchase``),
and each of these can therefore run any number of times:

- the same request sent again (same credential): Stripe's idempotency key
  ``mpp_<challenge id>_<token>`` returns the same PaymentIntent, and the
  purchase is already recorded. If the challenge has expired by then, the
  purchase is found by its challenge id first, so the buyer is told it was
  already paid for instead of being asked to pay again.
- the ``payment_intent.succeeded`` webhook, on first delivery or replayed. It
  also covers a server that was stopped between the charge and the credit.

The PaymentIntent call follows ``mppx`` 0.21.1, the SDK Stripe's guide tells
sellers to run (https://docs.stripe.com/payments/machine/mpp): top-level
``shared_payment_granted_token``, ``confirm``, ``automatic_payment_methods``
with redirects off, API version ``2026-07-29.preview``.

Configuration, all from the environment. The endpoint is off unless every
required one is set; ``credits_from_env`` then returns ``NoCredits`` and the
endpoint answers 503, like the other billing endpoints without Stripe.

``CALOTYPE_CREDITS``             ``1`` turns the endpoint on (required)
``CALOTYPE_CREDITS_CARD``        ``1`` offers cards through shared payment tokens (required today)
``CALOTYPE_CREDITS_STABLECOIN``  ``1`` asks for stablecoin payments. NOT IMPLEMENTED: it is
                                 reported at startup and nothing is offered for it
``STRIPE_SECRET_KEY``            the same key ``billing.py`` uses (required)
``STRIPE_PROFILE_ID``            the Stripe profile id, ``profile_...`` (required)
``MPP_SECRET_KEY``               signs challenges. Optional: derived from the Stripe key if unset,
                                 the way Stripe's sample derives it
``MPP_REALM``                    the challenge realm. Optional: the public host if unset
``STRIPE_MPP_API_VERSION``       optional, default ``2026-07-29.preview``
``STRIPE_SPT_PAYMENT_METHOD_TYPES``  optional, default ``card,link``

Not here: stablecoins (Tempo USDC). That path needs on-chain verification of
the buyer's transfer, which the official SDKs do and which was not rebuilt by
hand. See deploy/README.md, "Prepaid credit".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from . import __version__, mpp
from .billing import (
    METADATA_KEY_ID,
    BillingProviderError,
    BillingUnavailable,
    EventResult,
    _event_object,
    _int_or_none,
    _metadata_key_id,
    _plain,
)
from .logs import log_event

if TYPE_CHECKING:
    from .config import CreditPack
    from .storage import ApiKey, CreditPurchase, Storage

log = logging.getLogger("calotype.credits")

METHOD = "stripe"
INTENT = "charge"
CURRENCY = "usd"
DEFAULT_API_VERSION = "2026-07-29.preview"
DEFAULT_PAYMENT_METHOD_TYPES = ("card", "link")
# Stripe's minimum for a card payment through a shared payment token, in cents.
MIN_CARD_CENTS = 50

METADATA_PACK = "calotype_credit_pack"
METADATA_SCREENSHOTS = "calotype_credit_screenshots"
HANDLED_EVENTS = frozenset({"payment_intent.succeeded"})

UNAVAILABLE_MESSAGE = (
    "Prepaid credit is not on sale on this server. Paid plans are on the pricing page; "
    "questions go to skewbald@agentmail.to."
)

_TRUE = ("1", "true", "yes")
_PROFILE_ID = re.compile(r"^profile_[A-Za-z0-9_]{1,200}$")
_SPT = re.compile(r"^spt_[A-Za-z0-9_]{1,180}$")
_PAYMENT_INTENT = re.compile(r"^pi_[A-Za-z0-9_]{1,250}$")
_PAYMENT_METHOD_TYPE = re.compile(r"^[a-z_]{1,40}$")


class PaymentRefused(Exception):
    """The credential cannot be accepted and nothing was charged. Answer 402 with a new challenge.

    ``problem`` is one of the protocol's problem types (``mpp.PROBLEM_TITLES``).
    """

    def __init__(self, problem: str, detail: str):
        super().__init__(detail)
        self.problem = problem
        self.detail = detail


class PaymentUnconfirmed(Exception):
    """Stripe did not say whether the payment went through. The same credential may be sent again."""


@dataclass(frozen=True)
class Settlement:
    """A credential that has been paid for and credited (now, or by an earlier call)."""

    reference: str  # the Stripe PaymentIntent id
    added: bool  # False: this payment had already been credited
    screenshots: int
    balance: int


@dataclass(frozen=True)
class CreditsConfig:
    secret_key: str = field(repr=False)
    profile_id: str = ""
    challenge_secret: str = field(default="", repr=False)
    realm: str = ""  # "": the app uses its public host
    api_version: str = DEFAULT_API_VERSION
    payment_method_types: tuple[str, ...] = DEFAULT_PAYMENT_METHOD_TYPES
    expires_s: int = mpp.DEFAULT_EXPIRES_S
    # Asked for but not built; named at startup so nobody thinks it is on.
    unsupported: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> tuple["CreditsConfig | None", list[str]]:
        """The config, or None and the names (never the values) of what is missing or wrong."""
        env = os.environ if environ is None else environ

        def get(name: str) -> str:
            return env.get(name, "").strip()

        def on(name: str) -> bool:
            return get(name).lower() in _TRUE

        if not on("CALOTYPE_CREDITS"):
            return None, ["CALOTYPE_CREDITS"]
        missing = [name for name in ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID") if not get(name)]
        if get("STRIPE_PROFILE_ID") and not _PROFILE_ID.match(get("STRIPE_PROFILE_ID")):
            missing.append("STRIPE_PROFILE_ID")
        if not on("CALOTYPE_CREDITS_CARD"):
            # The only method built so far. Stablecoins alone cannot turn the endpoint on.
            missing.append("CALOTYPE_CREDITS_CARD")
        types = tuple(t.strip() for t in get("STRIPE_SPT_PAYMENT_METHOD_TYPES").split(",") if t.strip())
        if any(not _PAYMENT_METHOD_TYPE.match(t) for t in types):
            missing.append("STRIPE_SPT_PAYMENT_METHOD_TYPES")
        if missing:
            return None, missing
        secret_key = get("STRIPE_SECRET_KEY")
        return (
            cls(
                secret_key=secret_key,
                profile_id=get("STRIPE_PROFILE_ID"),
                challenge_secret=get("MPP_SECRET_KEY") or derive_challenge_secret(secret_key),
                realm=get("MPP_REALM"),
                api_version=get("STRIPE_MPP_API_VERSION") or DEFAULT_API_VERSION,
                payment_method_types=types or DEFAULT_PAYMENT_METHOD_TYPES,
                unsupported=("CALOTYPE_CREDITS_STABLECOIN",) if on("CALOTYPE_CREDITS_STABLECOIN") else (),
            ),
            [],
        )


def derive_challenge_secret(stripe_secret_key: str) -> str:
    """The challenge-signing secret, derived as Stripe's MPP sample derives it.

    https://docs.stripe.com/payments/machine/mpp : HMAC-SHA256 keyed with the
    Stripe secret key over ``mpp-challenge-signing``, in base64. It changes
    when the Stripe key is rolled; challenges last five minutes, so that only
    refuses credentials for challenges issued just before the change.
    """
    mac = hmac.new(stripe_secret_key.encode("utf-8"), b"mpp-challenge-signing", hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


class CreditsProvider(Protocol):
    configured: bool

    def challenge(self, key: "ApiKey", pack: "CreditPack", *, realm: str, header: str) -> mpp.Challenge: ...

    def settle(
        self, key: "ApiKey", pack: "CreditPack", authorization: str, store: "Storage", *, realm: str, header: str
    ) -> Settlement: ...

    def handle_event(self, event: dict[str, Any], store: "Storage") -> EventResult: ...


class NoCredits:
    """The provider when prepaid credit is not configured: every call is refused."""

    configured = False

    def __init__(self, missing: list[str] | None = None):
        self.missing = list(missing or [])
        self.unsupported: tuple[str, ...] = ()

    def challenge(self, key, pack, *, realm, header) -> mpp.Challenge:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def settle(self, key, pack, authorization, store, *, realm, header) -> Settlement:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def handle_event(self, event, store) -> EventResult:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)


class StripeCredits:
    """Sells prepaid packs for a card payment made with a shared payment token.

    ``client`` is a ``stripe.StripeClient``; tests pass a stand-in so that no
    call reaches Stripe.
    """

    configured = True

    def __init__(self, config: CreditsConfig, *, client: Any = None):
        self.config = config
        if client is None:
            import stripe

            client = stripe.StripeClient(config.secret_key, max_network_retries=2)
        self._client = client

    @property
    def unsupported(self) -> tuple[str, ...]:
        return self.config.unsupported

    # --- the challenge ---------------------------------------------------------------------

    def charge_request(self, pack: "CreditPack") -> dict[str, Any]:
        """The MPP ``request`` for a pack: what the buyer is asked to pay, and to whom.

        Fields as in https://paymentauth.org/draft-stripe-charge-00 section 6.
        """
        return {
            "amount": str(pack.price_usd_cents),
            "currency": CURRENCY,
            "description": describe(pack),
            "methodDetails": {
                "networkId": self.config.profile_id,
                "paymentMethodTypes": list(self.config.payment_method_types),
            },
        }

    def challenge(self, key: "ApiKey", pack: "CreditPack", *, realm: str, header: str) -> mpp.Challenge:
        """A new challenge for ``pack``, bound to ``key``. ``header`` says where the credential must come back."""
        if pack.price_usd_cents < MIN_CARD_CENTS:
            raise ValueError("Stripe's minimum for a card payment through a shared payment token is $0.50")
        return mpp.issue_challenge(
            self.config.challenge_secret,
            realm=realm,
            method=METHOD,
            intent=INTENT,
            request=self.charge_request(pack),
            opaque={"key": str(key.id), "pack": pack.name},
            description=describe(pack),
            header=header,
            expires=mpp.expires_at(seconds=self.config.expires_s),
        )

    # --- the payment -----------------------------------------------------------------------

    def settle(
        self, key: "ApiKey", pack: "CreditPack", authorization: str, store: "Storage", *, realm: str, header: str
    ) -> Settlement:
        """Check a credential, take the payment and add the pack to ``key``. Safe to call again.

        ``authorization`` is the credential header's value and ``header`` the
        field it arrived in (``""`` for ``Authorization``).

        Raises ``PaymentRefused`` (nothing was charged; answer 402 with a new
        challenge), ``PaymentUnconfirmed`` (Stripe gave no answer; the buyer
        may send the same credential again) or ``BillingProviderError`` (this
        server's Stripe setup is wrong).
        """
        try:
            credential = mpp.parse_credential(authorization)
        except mpp.MalformedCredential as e:
            raise PaymentRefused("malformed-credential", str(e)) from None

        # 1. The challenge is one of ours, unchanged, and was issued for this request.
        if not credential.issued_with(self.config.challenge_secret):
            raise PaymentRefused(
                "invalid-challenge", "The credential's challenge was not issued by this server, or was changed."
            )
        if (credential.realm, credential.method, credential.intent) != (realm, METHOD, INTENT):
            raise PaymentRefused("invalid-challenge", "The credential's challenge is for another resource.")
        if credential.header.lower() != header.lower():
            where = credential.header or "Authorization"
            raise PaymentRefused("invalid-challenge", f"This challenge asked for the credential in {where}.")
        try:
            opaque = mpp.decode_object(credential.opaque) if credential.opaque else {}
            request = mpp.decode_object(credential.request)
        except ValueError:
            raise PaymentRefused("malformed-credential", "The credential's challenge cannot be read.") from None
        if opaque.get("key") != str(key.id):
            raise PaymentRefused("invalid-challenge", "The credential's challenge was issued to another API key.")
        if opaque.get("pack") != pack.name:
            raise PaymentRefused(
                "invalid-challenge",
                f"The credential's challenge is for the pack {str(opaque.get('pack'))[:40]!r}, not {pack.name!r}.",
            )

        # 2. Already paid for? Looked up before the expiry check, so that sending a
        #    paid credential again after five minutes is never answered "pay again".
        prior = store.find_credit_purchase_by_challenge(credential.id)
        if prior is not None and prior.key_id == key.id:
            return Settlement(prior.id, False, prior.screenshots, store.get_credit_balance(key.id))

        # 3. Still valid, and still what this pack costs.
        expires = mpp.parse_time(credential.expires)
        if expires is None or expires <= datetime.now(UTC):
            raise PaymentRefused("payment-expired", "The challenge has expired. Pay the new one.")
        if request != self.charge_request(pack):
            raise PaymentRefused("invalid-challenge", "The pack's terms have changed. Pay the new challenge.")

        # 4. Only now is the token looked at.
        spt = credential.payload.get("spt")
        if not isinstance(spt, str) or not _SPT.match(spt):
            raise PaymentRefused(
                "malformed-credential", "The credential's payload needs 'spt', a shared payment token id (spt_...)."
            )

        intent_id = self._charge(key, pack, credential.id, spt)
        added, balance = store.add_credit_purchase(
            intent_id,
            key.id,
            pack=pack.name,
            screenshots=pack.screenshots,
            amount_cents=pack.price_usd_cents,
            currency=CURRENCY,
            challenge_id=credential.id,
            method=METHOD,
        )
        log_event(
            log,
            "credit purchase",
            key_id=key.id,
            pack=pack.name,
            payment_intent=intent_id,
            added=added,
            balance=balance,
        )
        return Settlement(intent_id, added, pack.screenshots, balance)

    def _charge(self, key: "ApiKey", pack: "CreditPack", challenge_id: str, spt: str) -> str:
        """Create and confirm the PaymentIntent. Returns its id once it has succeeded."""
        import stripe

        params: dict[str, Any] = {
            "amount": pack.price_usd_cents,
            "currency": CURRENCY,
            "confirm": True,
            # No redirect can be followed here: there is no person and no browser.
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
            # Not in the Stripe SDK's types yet (preview); sent as the SDKs send it.
            "shared_payment_granted_token": spt,
            "description": describe(pack),
            "metadata": {
                # The four keys the MPP SDKs set, so Stripe's reports group these with other machine payments.
                "machine_payment": "true",
                "mpp_sdk": f"calotype/{__version__}",
                "mpp_intent": INTENT,
                "mpp_challenge_id": challenge_id,
                # The key's internal id only. The key and its hash never leave Calotype.
                METADATA_KEY_ID: str(key.id),
                METADATA_PACK: pack.name,
                METADATA_SCREENSHOTS: str(pack.screenshots),
            },
        }
        options = {
            # The same credential sent twice is one payment.
            "idempotency_key": f"mpp_{challenge_id}_{spt}",
            "stripe_version": self.config.api_version,
        }
        try:
            intent = self._client.v1.payment_intents.create(params=params, options=options)
            if _replayed(intent):
                # Stripe answered from its idempotency record, which holds the first
                # reply, not the payment's state now. Read the state.
                intent = self._client.v1.payment_intents.retrieve(_field(intent, "id"))
        except stripe.CardError as e:
            self._log_failure(e, key, logging.WARNING)
            raise PaymentRefused("verification-failed", "The payment was declined. Nothing was charged.") from None
        except stripe.IdempotencyError as e:
            self._log_failure(e, key)
            raise PaymentUnconfirmed("idempotency") from None
        except stripe.InvalidRequestError as e:
            # A token that is unknown, used, expired, revoked, for too small an amount or for
            # another seller lands here. So does an account without the preview enabled, which
            # is why this is logged as an error: every purchase failing this way is a setup fault.
            self._log_failure(e, key)
            raise PaymentRefused(
                "verification-failed", "Stripe refused the shared payment token. Nothing was charged."
            ) from None
        except (stripe.AuthenticationError, stripe.PermissionError) as e:
            self._log_failure(e, key)
            raise BillingProviderError(type(e).__name__) from None
        except stripe.StripeError as e:
            # No connection, a timeout, a 5xx or a rate limit: the payment may or may not exist.
            self._log_failure(e, key)
            raise PaymentUnconfirmed(type(e).__name__) from None

        intent_id, status = _field(intent, "id"), _field(intent, "status")
        if not isinstance(intent_id, str) or not _PAYMENT_INTENT.match(intent_id):
            raise BillingProviderError("Stripe returned no PaymentIntent id")
        if status == "succeeded":
            if _field(intent, "amount") != pack.price_usd_cents or _field(intent, "currency") != CURRENCY:
                log_event(log, "payment amount differs from the pack", level=logging.ERROR, payment_intent=intent_id)
                raise BillingProviderError("amount mismatch")
            return intent_id
        log_event(
            log, "credit payment not completed", level=logging.WARNING, key_id=key.id, payment_intent=intent_id,
            status=status,
        )
        if status == "processing":
            # Not final yet. The webhook adds the credit if it succeeds.
            raise PaymentUnconfirmed("processing")
        if status == "requires_action":
            raise PaymentRefused(
                "verification-failed",
                "The card's bank asked for a step only the card holder can do. No credit was added.",
            )
        raise PaymentRefused("verification-failed", "The payment did not go through. No credit was added.")

    @staticmethod
    def _log_failure(e: Exception, key: "ApiKey", level: int = logging.ERROR) -> None:
        # The type and Stripe's code only: messages can echo request values.
        log_event(
            log,
            "stripe payment failed",
            level=level,
            key_id=key.id,
            error=type(e).__name__,
            code=getattr(e, "code", None),
            http_status=getattr(e, "http_status", None),
        )

    # --- webhook ---------------------------------------------------------------------------

    def handle_event(self, event: dict[str, Any], store: "Storage") -> EventResult:
        """Apply a verified ``payment_intent.succeeded`` event. Safe to call again with the same event.

        Only PaymentIntents this module created (they carry the pack in their
        metadata) are acted on; any other payment is ``ignored``. The
        PaymentIntent is read from Stripe again, so the event's own content is
        never trusted for the amount. Raises ``BillingProviderError`` if Stripe
        cannot be reached; the caller answers 500 and Stripe retries.
        """
        event_id, event_type = event["id"], event["type"]
        obj = _event_object(event)
        intent_id = obj.get("id")
        if (
            event_type not in HANDLED_EVENTS
            or not isinstance(intent_id, str)
            or not _PAYMENT_INTENT.match(intent_id)
            or METADATA_PACK not in (obj.get("metadata") or {})
        ):
            return EventResult("ignored")
        if store.event_processed(event_id):
            return EventResult("duplicate")
        result = self._credit_from_stripe(intent_id, store)
        store.mark_event_processed(event_id)
        log_event(
            log, "credit event", event_id=event_id, event_type=event_type, outcome=result.outcome, key_id=result.key_id
        )
        return result

    def _credit_from_stripe(self, intent_id: str, store: "Storage") -> EventResult:
        import stripe

        try:
            intent = _plain(self._client.v1.payment_intents.retrieve(intent_id))
        except stripe.StripeError as e:
            log_event(log, "stripe call failed", level=logging.ERROR, error=type(e).__name__)
            raise BillingProviderError(type(e).__name__) from None
        metadata = intent.get("metadata") or {}
        pack_name = metadata.get(METADATA_PACK)
        screenshots = _int_or_none(metadata.get(METADATA_SCREENSHOTS))
        key_id = _metadata_key_id(intent)
        amount = intent.get("amount_received") or intent.get("amount")
        if (
            intent.get("status") != "succeeded"
            or not isinstance(pack_name, str)
            or not pack_name
            or not screenshots
            or not key_id
            or not isinstance(amount, int)
        ):
            return EventResult("ignored")
        if store.get_key_by_id(key_id) is None:
            log_event(log, "credit payment for an unknown key", level=logging.WARNING, payment_intent=intent_id)
            return EventResult("unknown_key")
        added, _balance = store.add_credit_purchase(
            intent_id,
            key_id,
            pack=pack_name,
            screenshots=screenshots,
            amount_cents=amount,
            currency=str(intent.get("currency") or CURRENCY),
            challenge_id=str(metadata.get("mpp_challenge_id") or ""),
            method=METHOD,
        )
        return EventResult("credited" if added else "already_credited", key_id)

    # --- refunds (admin CLI) ---------------------------------------------------------------

    def refund(self, purchase: "CreditPurchase", amount_cents: int) -> str:
        """Refund ``amount_cents`` of a purchase through Stripe. Returns the Refund id.

        The idempotency key is the purchase's own, so running it twice makes
        one refund. Raises ``BillingProviderError`` if Stripe refuses or
        cannot be reached.
        """
        import stripe

        if not _PAYMENT_INTENT.match(purchase.id):
            raise ValueError("not a Stripe PaymentIntent id")
        if not 0 < amount_cents <= purchase.amount_cents:
            raise ValueError("the refund must be more than nothing and at most what was paid")
        try:
            refund = self._client.v1.refunds.create(
                params={
                    "payment_intent": purchase.id,
                    "amount": amount_cents,
                    "reason": "requested_by_customer",
                    "metadata": {METADATA_KEY_ID: str(purchase.key_id), METADATA_PACK: purchase.pack},
                },
                options={"idempotency_key": f"calotype_credit_refund_{purchase.id}"},
            )
        except stripe.StripeError as e:
            log_event(
                log,
                "stripe refund failed",
                level=logging.ERROR,
                error=type(e).__name__,
                code=getattr(e, "code", None),
                http_status=getattr(e, "http_status", None),
            )
            raise BillingProviderError(f"{type(e).__name__} {getattr(e, 'code', None) or ''}".strip()) from None
        refund_id = _field(refund, "id")
        if not isinstance(refund_id, str) or not refund_id:
            raise BillingProviderError("Stripe returned no Refund id")
        log_event(log, "credit purchase refunded", payment_intent=purchase.id, refund=refund_id, amount=amount_cents)
        return refund_id


def credits_from_env(environ: Mapping[str, str] | None = None) -> CreditsProvider:
    """``StripeCredits`` if prepaid credit is switched on and fully configured, otherwise ``NoCredits``."""
    config, missing = CreditsConfig.from_env(environ)
    if config is None:
        return NoCredits(missing)
    return StripeCredits(config)


def credits_for_admin(environ: Mapping[str, str] | None = None, *, client: Any = None) -> StripeCredits | None:
    """A provider for the admin CLI's refunds, or None if ``STRIPE_SECRET_KEY`` is not set.

    A refund needs only the secret key, so the other settings may be missing.
    """
    env = os.environ if environ is None else environ
    secret = env.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        return None
    return StripeCredits(CreditsConfig(secret_key=secret), client=client)


def describe(pack: "CreditPack") -> str:
    """One line for the buyer's wallet. Plain ASCII without commas, so every header parser reads it alike."""
    title = re.sub(r"[^A-Za-z0-9 ._-]", "", pack.title or pack.name).strip() or "pack"
    return f"Calotype {title}: {pack.screenshots} prepaid screenshots"


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _replayed(obj: Any) -> bool:
    """True if Stripe answered this call from its idempotency record."""
    headers = getattr(getattr(obj, "last_response", None), "headers", None)
    if not headers or not callable(getattr(headers, "items", None)):
        return False
    return any(str(k).lower() == "idempotent-replayed" and str(v).lower() == "true" for k, v in headers.items())
