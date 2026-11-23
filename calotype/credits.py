"""Prepaid screenshots bought by an agent, with no person and no checkout page.

``POST /v1/credits`` with a key and ``{"pack": "starter"}`` sells a prepaid
pack through Stripe's machine payments, over the Machine Payments Protocol
(MPP; the wire format is in ``mpp.py``). Two MPP methods can pay: ``stripe``
(a card, through a shared payment token; always offered) and ``tempo`` (USDC on
the Tempo chain; offered with ``CALOTYPE_CREDITS_STABLECOIN=1``, see
``tempo.py``). The card flow:

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

The Tempo flow differs after the challenge checks: the payment is a transfer
on the chain, checked (and for a signed transaction, broadcast) by the
official SDK in ``tempo.py``. Once the chain has it, the pack is credited under
the transaction hash, in the same atomic step, and only then is the payment
recorded in Stripe: one PaymentIntent with the ``crypto`` payment method in
``transaction_verification`` mode, idempotency key ``calotype_tempo_<hash>``,
as ``pympp``'s Stripe recorder does it. The transfer is final whatever Stripe
answers, so a failed recording does not take the credit back; it is logged,
tried again when the buyer resends the credential, and can be redone with
``admin record-tempo``. Its ``payment_intent.succeeded`` webhook links the
PaymentIntent to the purchase and credits nothing twice. A refund goes through
that PaymentIntent and Stripe returns stablecoins to the sending wallet
(https://docs.stripe.com/payments/deposit-mode-stablecoin-payments, "Refunds").

The PaymentIntent call follows ``mppx`` 0.21.1, the SDK Stripe's guide tells
sellers to run (https://docs.stripe.com/payments/machine/mpp): top-level
``shared_payment_granted_token``, ``confirm``, ``automatic_payment_methods``
with redirects off, API version ``2026-07-29.preview``.

Configuration, all from the environment. The endpoint is off unless every
required one is set; ``credits_from_env`` then returns ``NoCredits`` and the
endpoint answers 503, like the other billing endpoints without Stripe.

``CALOTYPE_CREDITS``             ``1`` turns the endpoint on (required)
``CALOTYPE_CREDITS_CARD``        ``1`` offers cards through shared payment tokens (required today)
``CALOTYPE_CREDITS_STABLECOIN``  ``1`` offers USDC on Tempo too (MPP method ``tempo``). Off by default
``STRIPE_TEMPO_DEPOSIT_ADDRESS`` the Stripe crypto deposit address on Tempo (required with the above;
                                 ``admin tempo-deposit-address`` lists or creates it)
``CALOTYPE_TEMPO_RPC_URL``       optional: a Tempo node of your own instead of the public one
``STRIPE_SECRET_KEY``            the same key ``billing.py`` uses (required)
``STRIPE_PROFILE_ID``            the Stripe profile id, ``profile_...`` (required)
``MPP_SECRET_KEY``               signs challenges. Optional: derived from the Stripe key if unset,
                                 the way Stripe's sample derives it
``MPP_REALM``                    the challenge realm. Optional: the public host if unset
``STRIPE_MPP_API_VERSION``       optional, default ``2026-07-29.preview``
``STRIPE_SPT_PAYMENT_METHOD_TYPES``  optional, default ``card,link``

Stablecoin payment needs ``CALOTYPE_CREDITS_CARD`` as well: cards stay the
method every wallet that pays Stripe can use. See deploy/README.md, "Prepaid
credit".
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

from . import __version__, mpp, tempo
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
METADATA_TEMPO_TX = "calotype_tempo_tx"  # on a PaymentIntent that records a Tempo payment
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
    """Stripe, or the Tempo chain, did not say whether the payment went through.

    The same credential may be sent again. ``method`` names who was asked.
    """

    def __init__(self, reason: str, method: str = METHOD):
        super().__init__(reason)
        self.method = method


@dataclass(frozen=True)
class Settlement:
    """A credential that has been paid for and credited (now, or by an earlier call)."""

    reference: str  # the Stripe PaymentIntent id (card) or the transaction hash (tempo)
    added: bool  # False: this payment had already been credited
    screenshots: int
    balance: int
    method: str = METHOD


@dataclass(frozen=True)
class CreditsConfig:
    secret_key: str = field(repr=False)
    profile_id: str = ""
    challenge_secret: str = field(default="", repr=False)
    realm: str = ""  # "": the app uses its public host
    api_version: str = DEFAULT_API_VERSION
    payment_method_types: tuple[str, ...] = DEFAULT_PAYMENT_METHOD_TYPES
    expires_s: int = mpp.DEFAULT_EXPIRES_S
    # Set when stablecoin payment is on (CALOTYPE_CREDITS_STABLECOIN=1).
    tempo: tempo.TempoConfig | None = None

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
        tempo_config = None
        if on("CALOTYPE_CREDITS_STABLECOIN"):
            # Asked for but incomplete: the endpoint stays off rather than quietly selling cards only.
            tempo_config, tempo_missing = tempo.TempoConfig.from_env(env, get("STRIPE_SECRET_KEY"))
            missing.extend(tempo_missing)
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
                tempo=tempo_config,
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

    def challenges(self, key: "ApiKey", pack: "CreditPack", *, realm: str, header: str) -> list[mpp.Challenge]: ...

    def settle(
        self, key: "ApiKey", pack: "CreditPack", authorization: str, store: "Storage", *, realm: str, header: str
    ) -> Settlement: ...

    def handle_event(self, event: dict[str, Any], store: "Storage") -> EventResult: ...


class NoCredits:
    """The provider when prepaid credit is not configured: every call is refused."""

    configured = False

    def __init__(self, missing: list[str] | None = None):
        self.missing = list(missing or [])
        self.methods: tuple[str, ...] = ()

    def challenges(self, key, pack, *, realm, header) -> list[mpp.Challenge]:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def settle(self, key, pack, authorization, store, *, realm, header) -> Settlement:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)

    def handle_event(self, event, store) -> EventResult:
        raise BillingUnavailable(UNAVAILABLE_MESSAGE)


class StripeCredits:
    """Sells prepaid packs for a card payment made with a shared payment token.

    ``client`` is a ``stripe.StripeClient``; tests pass a stand-in so that no
    call reaches Stripe. ``tempo_transport`` replaces the Tempo node the same
    way (an ``httpx`` transport).
    """

    configured = True

    def __init__(self, config: CreditsConfig, *, client: Any = None, tempo_transport: Any = None):
        self.config = config
        if client is None:
            import stripe

            client = stripe.StripeClient(config.secret_key, max_network_retries=2)
        self._client = client
        self._tempo = tempo.TempoMethod(config.tempo, transport=tempo_transport) if config.tempo else None

    @property
    def methods(self) -> tuple[str, ...]:
        """The MPP methods a buyer is offered, in the order of the 402's challenges."""
        return (METHOD, tempo.METHOD) if self._tempo else (METHOD,)

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

    def request_for(self, method: str, pack: "CreditPack") -> dict[str, Any]:
        """The ``request`` this server asks for ``pack`` in ``method``. Raises ``KeyError`` for another method."""
        if method == METHOD:
            return self.charge_request(pack)
        if method == tempo.METHOD and self._tempo is not None:
            return self._tempo.request(pack.price_usd_cents)
        raise KeyError(method)

    def challenge(
        self, key: "ApiKey", pack: "CreditPack", *, realm: str, header: str, method: str = METHOD
    ) -> mpp.Challenge:
        """A new challenge for ``pack``, bound to ``key``. ``header`` says where the credential must come back."""
        if pack.price_usd_cents < MIN_CARD_CENTS:
            raise ValueError("Stripe's minimum for a card payment through a shared payment token is $0.50")
        return mpp.issue_challenge(
            self.config.challenge_secret,
            realm=realm,
            method=method,
            intent=INTENT,
            request=self.request_for(method, pack),
            opaque={"key": str(key.id), "pack": pack.name},
            description=describe(pack),
            header=header,
            expires=mpp.expires_at(seconds=self.config.expires_s),
        )

    def challenges(self, key: "ApiKey", pack: "CreditPack", *, realm: str, header: str) -> list[mpp.Challenge]:
        """One challenge per method offered, card first. The buyer answers one of them."""
        return [self.challenge(key, pack, realm=realm, header=header, method=m) for m in self.methods]

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
        if (credential.realm, credential.intent) != (realm, INTENT) or credential.method not in self.methods:
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
            if prior.method == tempo.METHOD and not prior.payment_intent:
                # Credited, but Stripe has no record of it yet: try again now.
                self._record_tempo(prior.key_id, pack, credential.id, prior.id, store)
            return Settlement(prior.id, False, prior.screenshots, store.get_credit_balance(key.id), prior.method)

        # 3. Still valid, and still what this pack costs in this method.
        expires = mpp.parse_time(credential.expires)
        if expires is None or expires <= datetime.now(UTC):
            raise PaymentRefused("payment-expired", "The challenge has expired. Pay the new one.")
        if request != self.request_for(credential.method, pack):
            raise PaymentRefused("invalid-challenge", "The pack's terms have changed. Pay the new challenge.")

        if credential.method == tempo.METHOD:
            return self._settle_tempo(key, pack, credential, request, store)

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
        return Settlement(intent_id, added, pack.screenshots, balance, METHOD)

    def _settle_tempo(
        self, key: "ApiKey", pack: "CreditPack", credential: mpp.Credential, request: dict[str, Any], store: "Storage"
    ) -> Settlement:
        """The chain's part, then the credit, then Stripe's record. See the module docstring."""
        assert self._tempo is not None
        try:
            payment = self._tempo.settle(credential, request, store)
        except tempo.Refused as e:
            raise PaymentRefused(e.problem, e.detail) from None
        except tempo.Unconfirmed as e:
            raise PaymentUnconfirmed(str(e), method=tempo.METHOD) from None

        tx_hash = payment.transaction_hash
        # The transfer is on the chain now and cannot be undone: credit it first, once per transaction.
        added, balance = store.add_credit_purchase(
            tx_hash,
            key.id,
            pack=pack.name,
            screenshots=pack.screenshots,
            amount_cents=pack.price_usd_cents,
            currency=CURRENCY,
            challenge_id=credential.id,
            method=tempo.METHOD,
        )
        if not added:
            earlier = store.get_credit_purchase(tx_hash)
            if earlier is not None and earlier.key_id != key.id:
                # The SDK binds a transfer to one challenge, so this should never happen. Never credit twice.
                log_event(log, "tempo transaction already paid another key", level=logging.ERROR, key_id=key.id)
                raise PaymentRefused("verification-failed", "This transaction has already paid for another purchase.")
        log_event(
            log,
            "credit purchase",
            key_id=key.id,
            pack=pack.name,
            method=tempo.METHOD,
            transaction=tx_hash,
            added=added,
            balance=balance,
        )
        purchase = store.get_credit_purchase(tx_hash)
        if purchase is None or not purchase.payment_intent:
            self._record_tempo(key.id, pack, credential.id, tx_hash, store)
        return Settlement(tx_hash, added, pack.screenshots, balance, tempo.METHOD)

    def _record_tempo(
        self, key_id: int, pack: "CreditPack", challenge_id: str, tx_hash: str, store: "Storage"
    ) -> str | None:
        """Record a Tempo payment in Stripe as a crypto PaymentIntent and link it. Returns its id, or None.

        Never raises: the payment is credited already, and a failure here is a
        bookkeeping fault for the operator (logged as an error), not the buyer's.
        The call is ``pympp``'s (``mpp.methods.stripe.crypto_payment_recorder``):
        ``payment_method_data.type`` and ``payment_method_types`` ``crypto``,
        ``payment_method_options.crypto.mode`` ``transaction_verification`` with
        the network and transaction hash, idempotent on the hash.
        """
        import stripe

        params: dict[str, Any] = {
            "amount": pack.price_usd_cents,
            "currency": CURRENCY,
            "confirm": True,
            "payment_method_data": {"type": "crypto"},
            "payment_method_types": ["crypto"],
            "payment_method_options": {
                "crypto": {
                    "mode": "transaction_verification",
                    "transaction_verification_options": {"network": tempo.NETWORK, "transaction_hash": tx_hash},
                }
            },
            "description": describe(pack),
            "metadata": {
                "machine_payment": "true",
                "mpp_sdk": f"calotype/{__version__}",
                "mpp_intent": INTENT,
                "mpp_challenge_id": challenge_id,
                METADATA_KEY_ID: str(key_id),
                METADATA_PACK: pack.name,
                METADATA_SCREENSHOTS: str(pack.screenshots),
                METADATA_TEMPO_TX: tx_hash,
            },
        }
        options = {"idempotency_key": f"calotype_tempo_{tx_hash}", "stripe_version": self.config.api_version}
        try:
            intent = self._client.v1.payment_intents.create(params=params, options=options)
        except stripe.StripeError as e:
            log_event(
                log,
                "tempo payment not recorded in stripe",
                level=logging.ERROR,
                key_id=key_id,
                transaction=tx_hash,
                error=type(e).__name__,
                code=getattr(e, "code", None),
                http_status=getattr(e, "http_status", None),
            )
            return None
        intent_id, status = _field(intent, "id"), _field(intent, "status")
        if not isinstance(intent_id, str) or not _PAYMENT_INTENT.match(intent_id):
            log_event(log, "tempo payment not recorded in stripe", level=logging.ERROR, transaction=tx_hash)
            return None
        bad = status in ("canceled", "requires_payment_method")
        log_event(
            log,
            "tempo payment recorded in stripe",
            level=logging.ERROR if bad else logging.INFO,
            key_id=key_id,
            transaction=tx_hash,
            payment_intent=intent_id,
            status=status,
        )
        store.set_credit_payment_intent(tx_hash, intent_id)
        return intent_id

    def record_tempo_purchase(self, purchase: "CreditPurchase", store: "Storage") -> str | None:
        """``admin record-tempo``: record a credited Tempo purchase in Stripe now. Returns the PaymentIntent id."""
        from .config import CreditPack

        if purchase.method != tempo.METHOD or not tempo.TX_HASH.match(purchase.id):
            raise ValueError("not a Tempo purchase")
        if purchase.payment_intent:
            return purchase.payment_intent
        pack = CreditPack(purchase.pack, purchase.screenshots, purchase.amount_cents, purchase.pack)
        return self._record_tempo(purchase.key_id, pack, purchase.challenge_id, purchase.id, store)

    def tempo_deposit_addresses(self, *, create: bool = False) -> list[dict[str, Any]]:
        """``admin tempo-deposit-address``: this Stripe account's Tempo deposit addresses, after making one if asked.

        ``GET``/``POST /v1/crypto/deposit_addresses`` with ``network=tempo``
        (https://docs.stripe.com/payments/machine/mpp). Raises ``BillingProviderError``.
        """
        import stripe

        path, version = "/v1/crypto/deposit_addresses", self.config.api_version
        try:
            if create:
                made = self._client.raw_request("post", path, network=tempo.NETWORK, stripe_version=version)
                return [_plain(self._client.deserialize(made, api_mode="V1"))]
            listed = self._client.raw_request("get", path, network=tempo.NETWORK, stripe_version=version)
            return list(_plain(self._client.deserialize(listed, api_mode="V1")).get("data") or [])
        except stripe.StripeError as e:
            raise BillingProviderError(f"{type(e).__name__} {getattr(e, 'code', None) or ''}".strip()) from None

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
        purchase_id, method, recorded_by = intent_id, METHOD, None
        if METADATA_TEMPO_TX in metadata:
            # A Tempo payment this server credited (or was about to) under its transaction hash.
            tx_hash = metadata.get(METADATA_TEMPO_TX)
            if not isinstance(tx_hash, str) or not tempo.TX_HASH.match(tx_hash):
                return EventResult("ignored")
            purchase_id, method, recorded_by = tx_hash.lower(), tempo.METHOD, intent_id
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
            purchase_id,
            key_id,
            pack=pack_name,
            screenshots=screenshots,
            amount_cents=amount,
            currency=str(intent.get("currency") or CURRENCY),
            challenge_id=str(metadata.get("mpp_challenge_id") or ""),
            method=method,
            payment_intent=recorded_by,
        )
        if recorded_by and not added:
            store.set_credit_payment_intent(purchase_id, recorded_by)
        return EventResult("credited" if added else "already_credited", key_id)

    # --- refunds (admin CLI) ---------------------------------------------------------------

    def refund(self, purchase: "CreditPurchase", amount_cents: int) -> str:
        """Refund ``amount_cents`` of a purchase through Stripe. Returns the Refund id.

        The idempotency key is the purchase's own, so running it twice makes
        one refund. Raises ``BillingProviderError`` if Stripe refuses or
        cannot be reached, and ``ValueError`` for a Tempo payment that Stripe
        has no PaymentIntent for. A Tempo payment's refund goes back in
        stablecoins to the wallet that sent it (Stripe's rule for crypto).
        """
        import stripe

        intent_id = purchase.stripe_payment_intent
        if intent_id is None or not _PAYMENT_INTENT.match(intent_id):
            raise ValueError("no Stripe PaymentIntent records this payment")
        if not 0 < amount_cents <= purchase.amount_cents:
            raise ValueError("the refund must be more than nothing and at most what was paid")
        try:
            refund = self._client.v1.refunds.create(
                params={
                    "payment_intent": intent_id,
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
        log_event(log, "credit purchase refunded", payment_intent=intent_id, refund=refund_id, amount=amount_cents)
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
