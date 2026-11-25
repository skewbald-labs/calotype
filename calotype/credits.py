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

The Tempo flow differs after the challenge checks. Its state lives on the
purchase record, keyed by the transaction hash (``storage.PURCHASE_*``):

1. The official SDK checks the credential without changing anything
   (``tempo.TempoMethod.check``) and names the transaction hash.
2. The challenge is bound to that one hash, and the purchase recorded as
   ``pending`` (a signed transaction still to send) or ``paid`` (a pushed one,
   already on the chain), in one atomic step (``bind_tempo_payment``), with
   its terms fixed: amount, token, recipient, chain, realm, pack, key. Nothing
   is credited yet. A second, different transfer for the same challenge is
   refused before it is sent (pull) or recorded ``unmatched`` so it can be
   refunded (push, already on the chain); either way it adds nothing.
3. A pending transaction is broadcast by the SDK. If that gives no answer,
   the purchase stays ``pending`` and is reconciled later by its hash
   (``TempoMethod.lookup``), never by sending a fresh payment: when the buyer
   sends the credential again (within the challenge's validity the SDK first
   sends the same signed bytes once more, which the node treats as the same
   transaction; after expiry it is only looked up), when the webhook arrives,
   or with ``admin record-tempo``. It becomes ``paid`` once the receipt is
   checked, or ``failed`` if the transfer reverted or chain time passed its
   ``valid_before`` with no receipt; only then is the buyer asked to pay a new
   challenge.

   A ``hash`` credential (a transfer the buyer broadcast) for an expired
   challenge that was never bound (sent first after expiry, sent before its
   receipt was visible, or lost to a fault between the chain check and the
   binding) is looked up by its hash against the challenge's own signed terms
   and then bound as above (``_recover_pushed``). A signed transaction never
   bound was never sent here; after expiry it is refused.
4. A paid transfer is recorded in Stripe: one PaymentIntent with the
   ``crypto`` payment method in ``transaction_verification`` mode, as
   ``pympp``'s Stripe recorder does it, idempotency key
   ``calotype_tempo_<hash>`` (``_<n>`` after an attempt whose outcome Stripe
   left unknown, and only once a listing of the PaymentIntents made since it
   shows that it made none).
5. The credit is released (``credit_tempo_purchase``) only when that
   PaymentIntent has succeeded and its charge is Stripe's own record of this
   transaction: ``payment_method_details.crypto`` names this hash on Tempo,
   and the amount received and currency are the purchase's. Until then the
   buyer gets ``202`` with the hash, and the same credential, sent again,
   answers with the credit once it is released. The
   ``payment_intent.succeeded`` webhook does the same for an existing
   purchase and never creates one from a PaymentIntent's metadata. A
   PaymentIntent whose charge settled the transfer takes the place of a
   linked one that did not (declined, or still processing); one that cannot
   be applied now is not acknowledged, so Stripe delivers it again.

A refund goes through that PaymentIntent, only once its charge is checked to be
this transfer's settlement, and Stripe returns stablecoins to the sending
wallet (https://docs.stripe.com/payments/deposit-mode-stablecoin-payments,
"Refunds"). Before stablecoins are offered, the deposit address must be one of
this Stripe account's own, in the key's mode, taking the token asked for
(``tempo_ready``); until it is, the 402 offers cards only.

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
``CALOTYPE_TEMPO_SETTLE_WAIT_S`` optional, default 20: how long a purchase request waits for Stripe to
                                 settle a Tempo payment before answering 202 (pending)
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
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from .storage import (
    BOUND_ALREADY,
    CHALLENGE_TAKEN,
    PURCHASE_CREDITED,
    PURCHASE_FAILED,
    PURCHASE_PAID,
    PURCHASE_PENDING,
    PURCHASE_UNMATCHED,
    TRANSACTION_TAKEN,
)

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
METADATA_REFUND_OF = "calotype_purchase"  # on a Refund made by ``refund``: the purchase it refunds
HANDLED_EVENTS = frozenset({"payment_intent.succeeded"})

# A PaymentIntent in one of these will not settle; one in another (processing, requires_action) still may.
DECLINED_STATUSES = frozenset({"canceled", "requires_payment_method"})
# After Stripe answered a recording with a 5xx (which its idempotency layer replays for the same key), its
# outcome is unknown: Stripe may have made the PaymentIntent, or may still be making it behind a gateway
# timeout. Wait this long, then list what Stripe made since just before that attempt (``_record_tempo``).
STRIPE_RECONCILE_DELAY_S = 90
STRIPE_RECONCILE_MARGIN_S = 120  # how long before the attempt the listing starts
STRIPE_RECONCILE_MAX_PAGES = 20  # of 100 PaymentIntents; past it the attempt stays unreconciled (logged)
DEFAULT_TEMPO_SETTLE_WAIT_S = 20
# The deposit address is checked against the Stripe account again after this long; after a failed check, sooner.
TEMPO_ADDRESS_RECHECK_S = 3600
TEMPO_ADDRESS_RETRY_S = 300
SUPPORT = "skewbald@agentmail.to"

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


class TempoNotConfigured(BillingProviderError):
    """A Tempo purchase must be looked up on the chain, and this process has no Tempo settings."""

    def __init__(self, missing: list[str] | tuple[str, ...] = ()):
        names = ", ".join(missing) or "STRIPE_TEMPO_DEPOSIT_ADDRESS"
        super().__init__(
            f"Tempo is not configured here ({names}): set the Tempo settings the service runs with "
            "(STRIPE_TEMPO_DEPOSIT_ADDRESS, and CALOTYPE_TEMPO_RPC_URL if it uses one) and run again."
        )
        self.missing = tuple(missing)


class RefundBlocked(Exception):
    """A refund this command must not make: Stripe's records need a person to reconcile them first.

    Nothing was made in Stripe. ``detail`` says what was found.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class PaymentConflict(Exception):
    """The challenge was already paid by another transfer. Nothing is credited for this one; answer 409."""

    def __init__(self, detail: str, reference: str):
        super().__init__(detail)
        self.detail = detail
        self.reference = reference  # the transfer that was refused or recorded unmatched


@dataclass(frozen=True)
class Settlement:
    """A credential that has been paid for: credited (now, or by an earlier call), or still pending."""

    reference: str  # the Stripe PaymentIntent id (card) or the transaction hash (tempo)
    added: bool  # False: this payment had already been credited (or is not credited yet)
    screenshots: int
    balance: int
    method: str = METHOD
    # True: the payment is bound to the challenge but not credited yet (the chain or Stripe has not
    # confirmed it). The buyer sends the same credential again; it never pays twice.
    pending: bool = False
    detail: str = ""


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
    # How long a purchase request waits for Stripe to settle a Tempo payment before answering 202.
    tempo_settle_wait_s: float = DEFAULT_TEMPO_SETTLE_WAIT_S

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
        wait = get("CALOTYPE_TEMPO_SETTLE_WAIT_S")
        if wait and not (wait.isdigit() and int(wait) <= 120):
            missing.append("CALOTYPE_TEMPO_SETTLE_WAIT_S")
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
                tempo_settle_wait_s=int(wait) if wait else DEFAULT_TEMPO_SETTLE_WAIT_S,
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
        # Why there is no Tempo here, for a clear error (the admin CLI sets it; see credits_for_admin).
        self.tempo_missing: tuple[str, ...] = ()
        self._tempo_verified = False
        self._tempo_next_check = 0.0

    @property
    def methods(self) -> tuple[str, ...]:
        """The MPP methods configured, in the order of the 402's challenges (``offered_methods`` says which
        are on offer now)."""
        return (METHOD, tempo.METHOD) if self._tempo else (METHOD,)

    def offered_methods(self) -> tuple[str, ...]:
        """The methods a new challenge offers: stablecoins only once the deposit address is verified."""
        return (METHOD, tempo.METHOD) if self.tempo_ready() else (METHOD,)

    def tempo_ready(self) -> bool:
        """Whether ``STRIPE_TEMPO_DEPOSIT_ADDRESS`` is this Stripe account's own Tempo deposit address, in the
        key's mode, for the token this server asks for. A success holds for an hour
        (``TEMPO_ADDRESS_RECHECK_S``), then Stripe is asked again; a failure is asked again after five
        minutes, and until then no Tempo challenge is issued. A transfer to an address of another
        account (or one the account no longer lists) would reach the chain and pass every chain check,
        and Stripe would never settle it. It may call Stripe: call it from a worker thread."""
        if self._tempo is None:
            return False
        now = time.monotonic()
        if now < self._tempo_next_check:
            return self._tempo_verified
        config = self._tempo.config
        try:
            addresses = self.tempo_deposit_addresses()
        except BillingProviderError as e:
            addresses, error = [], str(e)
        else:
            error = None
        self._tempo_verified = any(tempo.deposit_address_matches(config, a) for a in addresses)
        self._tempo_next_check = now + (TEMPO_ADDRESS_RECHECK_S if self._tempo_verified else TEMPO_ADDRESS_RETRY_S)
        if not self._tempo_verified:
            log_event(
                log,
                "tempo deposit address not verified; stablecoins are not offered",
                level=logging.ERROR,
                error=error,
                addresses_listed=len(addresses),
                livemode=config.livemode,
                token=config.currency,
            )
        return self._tempo_verified

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
        return [self.challenge(key, pack, realm=realm, header=header, method=m) for m in self.offered_methods()]

    # --- the payment -----------------------------------------------------------------------

    def settle(
        self, key: "ApiKey", pack: "CreditPack", authorization: str, store: "Storage", *, realm: str, header: str
    ) -> Settlement:
        """Check a credential, take the payment and add the pack to ``key``. Safe to call again.

        ``authorization`` is the credential header's value and ``header`` the
        field it arrived in (``""`` for ``Authorization``).

        Raises ``PaymentRefused`` (nothing was charged; answer 402 with a new
        challenge), ``PaymentUnconfirmed`` (Stripe gave no answer; the buyer
        may send the same credential again), ``PaymentConflict`` (a Tempo
        challenge already paid by another transfer; answer 409) or
        ``BillingProviderError`` (this server's Stripe setup is wrong). A
        Tempo payment bound to its challenge but not yet credited returns a
        ``Settlement`` with ``pending`` set.
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

        # 2. Already paid for, or (Tempo) bound to a transfer? Looked up before the expiry check, so that
        #    sending a paid credential again after five minutes is never answered "pay again".
        prior = store.find_credit_purchase_by_challenge(credential.id)
        if prior is not None and prior.key_id == key.id:
            if prior.method == tempo.METHOD:
                carried = tempo.credential_transaction_hash(credential)
                if carried is not None and carried != prior.id and self._tempo is not None:
                    return self._second_transfer(prior, carried, credential, key, store)
                return self._advance_tempo(prior, store, credential=credential)
            return Settlement(prior.id, False, prior.screenshots, store.get_credit_balance(key.id), prior.method)

        # 3. Still valid, and still what this pack costs in this method.
        expires = mpp.parse_time(credential.expires)
        if expires is None or expires <= datetime.now(UTC):
            if expires is not None and self._tempo is not None and credential.method == tempo.METHOD and (
                credential.payload.get("type") == "hash"
            ):
                # A transfer the buyer broadcast may have moved before the challenge expired, unbound here.
                return self._recover_pushed(key, pack, credential, request, store)
            raise PaymentRefused("payment-expired", "The challenge has expired. Pay the new one.")
        if request != self.request_for(credential.method, pack):
            raise PaymentRefused("invalid-challenge", "The pack's terms have changed. Pay the new challenge.")

        if credential.method == tempo.METHOD:
            if not self.tempo_ready():
                raise PaymentRefused(
                    "invalid-challenge", "Stablecoin payment is not available right now. Pay the new challenge."
                )
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

    # --- Tempo: the purchase's state machine (see the module docstring) ------------------------------

    def _settle_tempo(
        self, key: "ApiKey", pack: "CreditPack", credential: mpp.Credential, request: dict[str, Any], store: "Storage"
    ) -> Settlement:
        """A new Tempo credential: check it, bind the challenge to its transfer, then move the purchase on."""
        assert self._tempo is not None
        try:
            checked = self._tempo.check(credential, request)
        except tempo.Refused as e:
            raise PaymentRefused(e.problem, e.detail) from None
        except tempo.Unconfirmed as e:
            # Nothing is bound and nothing was sent: the same credential can be checked again.
            raise PaymentUnconfirmed(str(e), method=tempo.METHOD) from None
        return self._bind_tempo(key, pack, credential, request, store, checked, pack.price_usd_cents)

    def _recover_pushed(
        self, key: "ApiKey", pack: "CreditPack", credential: mpp.Credential, request: dict[str, Any], store: "Storage"
    ) -> Settlement:
        """A transfer the buyer broadcast (a ``hash`` credential) for an expired challenge never bound here.

        The hash may have come before its receipt was visible, the server may
        have failed between the chain check and the binding, or this may be
        its first submission. It is looked up on the chain by its hash alone
        (``TempoMethod.lookup``), never sent, against the challenge's own
        terms: issued and signed by this server (checked above), so the
        amount, token, recipient and chain are what this server asked for
        then, even if the pack's price has changed since. Then it is bound
        exactly as before expiry, or recorded ``unmatched`` if another
        transfer took the challenge. A transfer not on the chain yet is
        ``PaymentUnconfirmed`` (send it again); one that does not pay these
        terms, or reverted, is refused.
        """
        assert self._tempo is not None
        config = self._tempo.config
        tx_hash = tempo.credential_transaction_hash(credential)
        amount_cents = tempo.request_cents(request)
        if (
            tx_hash is None
            or amount_cents is None
            or str(request.get("currency") or "").lower() != config.currency.lower()
            or (request.get("methodDetails") or {}).get("chainId") != config.chain_id
        ):
            raise PaymentRefused("payment-expired", "The challenge has expired. Pay the new one.")
        terms = {"request": request, "realm": credential.realm, "challenge_id": credential.id}
        try:
            found = self._tempo.lookup(tx_hash, terms, None)
        except tempo.Unconfirmed as e:
            raise PaymentUnconfirmed(str(e), method=tempo.METHOD) from None
        log_event(log, "tempo transfer looked up after expiry", key_id=key.id, transaction=tx_hash, found=found)
        if found == tempo.CHAIN_PENDING:
            raise PaymentUnconfirmed("not on the chain yet", method=tempo.METHOD)
        if found == tempo.CHAIN_FAILED:
            raise PaymentRefused(
                "verification-failed",
                f"Transaction {tx_hash} does not pay this challenge, or it reverted. Nothing was credited. "
                "Pay the new challenge.",
            )
        return self._bind_tempo(
            key, pack, credential, request, store, tempo.Checked(tx_hash, "push", None), amount_cents
        )

    def _bind_tempo(
        self,
        key: "ApiKey",
        pack: "CreditPack",
        credential: mpp.Credential,
        request: dict[str, Any],
        store: "Storage",
        checked: tempo.Checked,
        amount_cents: int,
    ) -> Settlement:
        """Bind the challenge to the checked transfer (one atomic step), then move the purchase on."""
        tx_hash = checked.transaction_hash
        terms = json.dumps(
            {"request": request, "realm": credential.realm, "challenge_id": credential.id}, sort_keys=True
        )
        outcome, purchase = store.bind_tempo_payment(
            tx_hash,
            key.id,
            pack=pack.name,
            screenshots=pack.screenshots,
            amount_cents=amount_cents,
            currency=CURRENCY,
            challenge_id=credential.id,
            # A pushed transfer was found on the chain by the check; a signed one is not sent yet.
            status=PURCHASE_PAID if checked.mode == "push" else PURCHASE_PENDING,
            tempo_terms=terms,
            valid_before=checked.valid_before,
        )
        log_event(
            log, "tempo payment bound", key_id=key.id, pack=pack.name, transaction=tx_hash, mode=checked.mode,
            outcome=outcome,
        )
        if outcome == CHALLENGE_TAKEN:
            other = purchase.id if purchase else "another transaction"
            if checked.mode == "push":
                # A second transfer for this challenge, already on the chain. It adds nothing, and is kept
                # (uncredited, without the challenge) so that it can be recorded in Stripe and refunded.
                store.add_credit_purchase(
                    tx_hash, key.id, pack=pack.name, screenshots=pack.screenshots, amount_cents=amount_cents,
                    currency=CURRENCY, challenge_id="", method=tempo.METHOD, status=PURCHASE_UNMATCHED,
                    tempo_terms=terms,
                )
                log_event(log, "tempo transfer for a challenge already paid", level=logging.ERROR, key_id=key.id,
                          transaction=tx_hash, bound=other)
                raise PaymentConflict(
                    f"This challenge was already paid by transaction {other}. Transaction {tx_hash} was not "
                    f"credited; it is recorded so that it can be refunded. Write to {SUPPORT}.",
                    tx_hash,
                )
            raise PaymentConflict(
                f"This challenge is already being paid by transaction {other}. This transaction was not "
                "broadcast and nothing moved. Send the first credential again to see its outcome.",
                tx_hash,
            )
        if outcome == TRANSACTION_TAKEN or purchase is None:
            # The SDK binds a transfer to one challenge by its memo, so this should never happen. Never credit twice.
            log_event(log, "tempo transaction already paid another purchase", level=logging.ERROR, key_id=key.id,
                      transaction=tx_hash)
            raise PaymentRefused("verification-failed", "This transaction has already paid for another purchase.")
        if outcome == BOUND_ALREADY and purchase.status != PURCHASE_PENDING:
            return self._advance_tempo(purchase, store)
        if purchase.status == PURCHASE_PENDING:
            purchase = self._broadcast_tempo(purchase, credential, request, store)
        return self._advance_tempo(purchase, store, credential=None, sent=True)

    def _second_transfer(
        self, bound: "CreditPurchase", tx_hash: str, credential: mpp.Credential, key: "ApiKey", store: "Storage"
    ) -> Settlement:
        """A credential for a challenge already bound to another transfer. Never sent; never credited.

        A pushed one is already on the chain: it is looked up by its hash (the
        challenge may have expired) against the bound purchase's terms, and if
        it paid, kept as ``unmatched`` so that it can be refunded.
        """
        assert self._tempo is not None
        if credential.payload.get("type") == "hash":
            try:
                found = self._tempo.lookup(tx_hash, json.loads(bound.tempo_terms or "{}"), None)
            except (tempo.Unconfirmed, ValueError, KeyError) as e:
                raise PaymentUnconfirmed(type(e).__name__, method=tempo.METHOD) from None
            if found == tempo.CHAIN_PAID:
                store.add_credit_purchase(
                    tx_hash, key.id, pack=bound.pack, screenshots=bound.screenshots, amount_cents=bound.amount_cents,
                    currency=bound.currency, challenge_id="", method=tempo.METHOD, status=PURCHASE_UNMATCHED,
                    tempo_terms=bound.tempo_terms,
                )
                log_event(log, "tempo transfer for a challenge already paid", level=logging.ERROR, key_id=key.id,
                          transaction=tx_hash, bound=bound.id)
                raise PaymentConflict(
                    f"This challenge was already paid by transaction {bound.id}. Transaction {tx_hash} was not "
                    f"credited; it is recorded so that it can be refunded. Write to {SUPPORT}.",
                    tx_hash,
                )
            raise PaymentConflict(
                f"This challenge was already paid by transaction {bound.id}. Transaction {tx_hash} does not pay "
                "it and was not credited.",
                tx_hash,
            )
        raise PaymentConflict(
            f"This challenge is already paid by transaction {bound.id}. This transaction was not broadcast and "
            "nothing moved. Send the first credential again to see its outcome.",
            tx_hash,
        )

    def _broadcast_tempo(
        self, purchase: "CreditPurchase", credential: mpp.Credential, request: dict[str, Any], store: "Storage"
    ) -> "CreditPurchase":
        """Send the bound signed transaction. Any answer but a checked receipt leaves the purchase pending."""
        assert self._tempo is not None
        try:
            payment = self._tempo.broadcast(credential, request, store)
        except (tempo.Refused, tempo.Unconfirmed) as e:
            # Not proof that nothing moved: another attempt may have sent it, or the buyer may still.
            # Only the chain decides that (``lookup``), so the purchase stays pending.
            log_event(log, "tempo broadcast gave no receipt", level=logging.WARNING, transaction=purchase.id,
                      error=type(e).__name__, problem=getattr(e, "problem", None))
            return purchase
        if payment.transaction_hash != purchase.id:
            log_event(log, "tempo broadcast returned another hash", level=logging.ERROR, transaction=purchase.id)
            return purchase
        return store.update_credit_purchase(
            purchase.id, {"status": PURCHASE_PAID}, only_if_status=(PURCHASE_PENDING,)
        ) or store.get_credit_purchase(purchase.id) or purchase

    def _advance_tempo(
        self,
        purchase: "CreditPurchase",
        store: "Storage",
        *,
        credential: mpp.Credential | None = None,
        sent: bool = False,
        wait: bool = True,
    ) -> Settlement:
        """Move a bound Tempo purchase as far as it can go now, and say where it is.

        A pending one is reconciled by its hash on the chain. If its signed
        credential is resent while the challenge is still valid, the SDK sends
        those same signed bytes once more first (``_resend_tempo``): the node
        treats them as the same transaction, so at most one transfer moves.
        After expiry it is only looked up. It is never paid with a fresh
        transaction. A paid one is recorded in Stripe and credited once Stripe
        has settled it; that needs no chain. ``sent`` says this call has just
        broadcast it. Raises ``TempoNotConfigured`` for a pending purchase in
        a process without Tempo settings.
        """
        if purchase.status == PURCHASE_PENDING and not sent and credential is not None:
            purchase = self._resend_tempo(purchase, credential, store)
        if purchase.status == PURCHASE_PENDING:
            purchase = self._look_up_tempo(purchase, store)

        if purchase.status == PURCHASE_CREDITED:
            return self._tempo_settlement(purchase, store, added=False)
        if purchase.status == PURCHASE_FAILED:
            raise PaymentRefused(
                "verification-failed",
                f"Transaction {purchase.id} did not pay: it reverted, or it can no longer be included. "
                "Nothing was credited. Pay the new challenge.",
            )
        if purchase.status == PURCHASE_UNMATCHED:
            raise PaymentConflict(
                f"Transaction {purchase.id} was not credited: its challenge was paid by another transfer. "
                f"Write to {SUPPORT} for a refund.",
                purchase.id,
            )
        if purchase.status == PURCHASE_PENDING:
            return self._tempo_settlement(
                purchase, store, pending="The Tempo network has not confirmed this transaction yet."
            )
        return self._settle_with_stripe(purchase, store, wait=wait)

    def _resend_tempo(self, purchase: "CreditPurchase", credential: mpp.Credential, store: "Storage"):
        """A credential resent for a pending purchase while its challenge is still valid: the SDK may send
        the same signed transaction once more (it recovers the receipt if the node already has it)."""
        expires = mpp.parse_time(credential.expires)
        if (
            credential.payload.get("type") != "transaction"
            or tempo.credential_transaction_hash(credential) != purchase.id
            or expires is None
            or expires <= datetime.now(UTC)
        ):
            return purchase
        terms = json.loads(purchase.tempo_terms or "{}")
        request = terms.get("request")
        try:
            decoded = mpp.decode_object(credential.request)
        except ValueError:
            return purchase
        if request is None or decoded != request:
            return purchase
        return self._broadcast_tempo(purchase, credential, request, store)

    def _look_up_tempo(self, purchase: "CreditPurchase", store: "Storage") -> "CreditPurchase":
        if self._tempo is None:
            raise TempoNotConfigured(self.tempo_missing)
        try:
            found = self._tempo.lookup(purchase.id, json.loads(purchase.tempo_terms or "{}"), purchase.valid_before)
        except (tempo.Unconfirmed, ValueError, KeyError):
            return purchase
        if found == tempo.CHAIN_PENDING:
            return purchase
        status = PURCHASE_PAID if found == tempo.CHAIN_PAID else PURCHASE_FAILED
        log_event(log, "tempo transaction reconciled", level=logging.INFO if found == tempo.CHAIN_PAID
                  else logging.WARNING, transaction=purchase.id, status=status)
        return store.update_credit_purchase(
            purchase.id, {"status": status}, only_if_status=(PURCHASE_PENDING,)
        ) or store.get_credit_purchase(purchase.id) or purchase

    def _tempo_settlement(
        self, purchase: "CreditPurchase", store: "Storage", *, added: bool = False, balance: int | None = None,
        pending: str = "",
    ) -> Settlement:
        if balance is None:
            balance = store.get_credit_balance(purchase.key_id)
        return Settlement(
            purchase.id, added, purchase.screenshots, balance, tempo.METHOD, pending=bool(pending), detail=pending
        )

    def _settle_with_stripe(self, purchase: "CreditPurchase", store: "Storage", *, wait: bool = True) -> Settlement:
        """A transfer on the chain: record it in Stripe and credit it once Stripe has settled and matched it."""
        intent = self._record_tempo(purchase, store)
        deadline = time.monotonic() + (self.config.tempo_settle_wait_s if wait else 0)
        while (
            intent is not None
            and intent.get("status") not in DECLINED_STATUSES | {"succeeded"}
            and time.monotonic() < deadline
        ):
            time.sleep(min(2.0, max(deadline - time.monotonic(), 0)))
            intent = self._retrieve_intent(str(intent["id"])) or intent
        if intent is None:
            return self._tempo_settlement(purchase, store, pending="Stripe has not recorded this payment yet.")
        intent_id, status = str(intent["id"]), intent.get("status")
        if status in DECLINED_STATUSES:
            log_event(log, "stripe declined to settle a tempo payment", level=logging.ERROR, transaction=purchase.id,
                      payment_intent=intent_id, status=status)
            return self._tempo_settlement(
                purchase, store,
                pending=f"Stripe has not settled this payment. It is being looked into; write to {SUPPORT}.",
            )
        if status != "succeeded":
            return self._tempo_settlement(purchase, store, pending="Stripe is still settling this payment.")
        problem = _settlement_mismatch(intent, purchase)
        if problem:
            log_event(log, "stripe settlement does not match the tempo purchase", level=logging.ERROR,
                      transaction=purchase.id, payment_intent=intent_id, problem=problem)
            return self._tempo_settlement(
                purchase, store,
                pending=f"Stripe's record of this payment does not match it. It is being looked into; write to {SUPPORT}.",
            )
        added, balance = store.credit_tempo_purchase(purchase.id, intent_id)
        now = store.get_credit_purchase(purchase.id) or purchase
        log_event(log, "credit purchase", key_id=now.key_id, pack=now.pack, method=tempo.METHOD,
                  transaction=now.id, payment_intent=intent_id, added=added, balance=balance)
        if not now.credited:
            # Refunded before Stripe settled it, or linked to another PaymentIntent: never credited here.
            return self._tempo_settlement(now, store, pending=f"This payment is on hold; write to {SUPPORT}.")
        return self._tempo_settlement(now, store, added=added, balance=balance)

    def _retrieve_intent(self, intent_id: str) -> dict[str, Any] | None:
        """The PaymentIntent with its latest charge, or None if Stripe cannot be asked now."""
        import stripe

        try:
            return _plain(self._client.v1.payment_intents.retrieve(intent_id, params={"expand": ["latest_charge"]}))
        except stripe.StripeError as e:
            log_event(log, "stripe call failed", level=logging.WARNING, error=type(e).__name__,
                      http_status=getattr(e, "http_status", None))
            return None

    def _record_tempo(self, purchase: "CreditPurchase", store: "Storage") -> dict[str, Any] | None:
        """The PaymentIntent that records a paid Tempo purchase in Stripe, made now if need be. None: unknown yet.

        Never raises. The call is ``pympp``'s (``mpp.methods.stripe.crypto_payment_recorder``):
        ``payment_method_data.type`` and ``payment_method_types`` ``crypto``,
        ``payment_method_options.crypto.mode`` ``transaction_verification``
        with the network and transaction hash. Amount, pack and key come from
        the purchase as it was bound, never from the pack's terms today.
        """
        import stripe

        if purchase.payment_intent:
            return self._retrieve_intent(purchase.payment_intent)
        if purchase.stripe_retry_at:
            # The last attempt's outcome is unknown, and it is stored (``stripe_attempt``, ``stripe_retry_at``).
            # It is reconciled before anything new is made: the PaymentIntents Stripe made since just before it
            # are listed (a list reads what Stripe has written; its search can lag by minutes, longer in an
            # outage). One that settled this transfer is linked; else one still live; only if there is none is
            # a new idempotency key used.
            retry_at = datetime.fromisoformat(purchase.stripe_retry_at)
            if datetime.now(UTC) < retry_at:
                return None
            since = retry_at - timedelta(seconds=STRIPE_RECONCILE_DELAY_S + STRIPE_RECONCILE_MARGIN_S)
            found = self._list_tempo_intents(purchase.id, since)
            if found is None:
                return None
            chosen = _best_candidate(found, purchase)
            if chosen is not None:
                return self._link_intent(purchase, str(chosen["id"]), store)
            purchase = store.update_credit_purchase(purchase.id, {"stripe_retry_at": None}) or purchase

        params: dict[str, Any] = {
            "amount": purchase.amount_cents,
            "currency": purchase.currency,
            "confirm": True,
            "payment_method_data": {"type": "crypto"},
            "payment_method_types": ["crypto"],
            "payment_method_options": {
                "crypto": {
                    "mode": "transaction_verification",
                    "transaction_verification_options": {"network": tempo.NETWORK, "transaction_hash": purchase.id},
                }
            },
            "description": f"Calotype {purchase.pack}: {purchase.screenshots} prepaid screenshots",
            "metadata": {
                "machine_payment": "true",
                "mpp_sdk": f"calotype/{__version__}",
                "mpp_intent": INTENT,
                "mpp_challenge_id": purchase.challenge_id,
                METADATA_KEY_ID: str(purchase.key_id),
                METADATA_PACK: purchase.pack,
                METADATA_SCREENSHOTS: str(purchase.screenshots),
                METADATA_TEMPO_TX: purchase.id,
            },
        }
        idempotency_key = f"calotype_tempo_{purchase.id}" + (
            f"_{purchase.stripe_attempt}" if purchase.stripe_attempt else ""
        )
        options = {"idempotency_key": idempotency_key, "stripe_version": self.config.api_version}
        try:
            intent = self._client.v1.payment_intents.create(params=params, options=options)
        except stripe.StripeError as e:
            status = getattr(e, "http_status", None)
            log_event(log, "tempo payment not recorded in stripe", level=logging.ERROR, key_id=purchase.key_id,
                      transaction=purchase.id, error=type(e).__name__, code=getattr(e, "code", None),
                      http_status=status)
            if isinstance(e, stripe.IdempotencyError) or (isinstance(status, int) and status >= 500):
                # Stripe's idempotency layer replays this answer for the same key. Reconcile it later, then
                # (only if Stripe made nothing) try a new key.
                retry_at = (datetime.now(UTC) + timedelta(seconds=STRIPE_RECONCILE_DELAY_S)).isoformat(
                    timespec="seconds"
                )
                store.update_credit_purchase(
                    purchase.id, {"stripe_attempt": purchase.stripe_attempt + 1, "stripe_retry_at": retry_at}
                )
            return None
        intent_id = _field(intent, "id")
        if not isinstance(intent_id, str) or not _PAYMENT_INTENT.match(intent_id):
            log_event(log, "tempo payment not recorded in stripe", level=logging.ERROR, transaction=purchase.id)
            return None
        return self._link_intent(purchase, intent_id, store)

    def _list_tempo_intents(self, tx_hash: str, since: datetime) -> list[dict[str, Any]] | None:
        """The PaymentIntents made since ``since`` that record ``tx_hash``, with their charges. None: not known now."""
        import stripe

        params: dict[str, Any] = {
            "created": {"gte": int(since.timestamp())}, "limit": 100, "expand": ["data.latest_charge"],
        }
        found: list[dict[str, Any]] = []
        for _ in range(STRIPE_RECONCILE_MAX_PAGES):
            try:
                page = _plain(self._client.v1.payment_intents.list(params=params))
            except stripe.StripeError as e:
                log_event(log, "stripe list failed", level=logging.WARNING, error=type(e).__name__,
                          http_status=getattr(e, "http_status", None))
                return None
            data = [i for i in page.get("data") or [] if isinstance(i, Mapping)]
            found += [i for i in data if str((i.get("metadata") or {}).get(METADATA_TEMPO_TX) or "").lower() == tx_hash]
            if not page.get("has_more") or not data:
                return found
            params = {**params, "starting_after": str(data[-1]["id"])}
        log_event(log, "too many payment intents to reconcile a tempo recording", level=logging.ERROR,
                  transaction=tx_hash)
        return None

    def _link_intent(self, purchase: "CreditPurchase", intent_id: str, store: "Storage") -> dict[str, Any] | None:
        if not store.set_credit_payment_intent(purchase.id, intent_id):
            # Linked to another PaymentIntent meanwhile (a parallel call, or the webhook): follow that one.
            now = store.get_credit_purchase(purchase.id)
            if now is None or not now.payment_intent:
                return None
            intent_id = now.payment_intent
        log_event(log, "tempo payment recorded in stripe", key_id=purchase.key_id, transaction=purchase.id,
                  payment_intent=intent_id)
        return self._retrieve_intent(intent_id)

    def record_tempo_purchase(self, purchase: "CreditPurchase", store: "Storage", *, again: bool = False) -> Settlement:
        """``admin record-tempo``: move a Tempo purchase on now (look it up, record it in Stripe, credit it).

        With ``again``, a PaymentIntent Stripe declined (canceled, or needing a
        payment method) is let go and a new one made under a new idempotency key.
        """
        if purchase.method != tempo.METHOD or not tempo.TX_HASH.match(purchase.id):
            raise ValueError("not a Tempo purchase")
        if again and purchase.payment_intent and not purchase.credited:
            intent = self._retrieve_intent(purchase.payment_intent)
            if intent is not None and intent.get("status") in DECLINED_STATUSES:
                purchase = store.update_credit_purchase(
                    purchase.id,
                    {"payment_intent": None, "stripe_attempt": purchase.stripe_attempt + 1, "stripe_retry_at": None},
                ) or purchase
        if purchase.status == PURCHASE_UNMATCHED:
            # Never credited: only recorded in Stripe, so that it can be refunded.
            if not purchase.payment_intent:
                self._record_tempo(purchase, store)
            purchase = store.get_credit_purchase(purchase.id) or purchase
            raise PaymentConflict("unmatched: recorded for a refund only", purchase.id)
        if purchase.credited and not purchase.payment_intent:
            # Credited before this state machine existed, when Stripe's record could fail afterwards.
            self._record_tempo(purchase, store)
            return self._tempo_settlement(store.get_credit_purchase(purchase.id) or purchase, store)
        return self._advance_tempo(purchase, store, wait=False)

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
        cannot be reached, or a Tempo purchase this settles cannot be moved on
        now (the chain or Stripe gave no answer); the event is then not marked
        processed, the caller answers 500 and Stripe delivers it again.
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
            intent = _plain(self._client.v1.payment_intents.retrieve(intent_id, params={"expand": ["latest_charge"]}))
        except stripe.StripeError as e:
            log_event(log, "stripe call failed", level=logging.ERROR, error=type(e).__name__)
            raise BillingProviderError(type(e).__name__) from None
        metadata = intent.get("metadata") or {}
        if METADATA_TEMPO_TX in metadata or "crypto" in (intent.get("payment_method_types") or []):
            return self._tempo_from_stripe(intent, store)
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

    def _tempo_from_stripe(self, intent: dict[str, Any], store: "Storage") -> EventResult:
        """A PaymentIntent recording a Tempo payment. It only ever settles a purchase this server bound.

        The transaction is the one Stripe verified (the charge's
        ``payment_method_details.crypto``), not the metadata; the purchase must
        exist already; amount, currency and hash must match it; and the link is
        made only if the purchase has no other PaymentIntent. No purchase, key
        or amount is ever taken from metadata.
        """
        intent_id = str(intent.get("id"))
        crypto = _charge_crypto(intent)
        tx_hash = str(crypto.get("transaction_hash") or "").lower()
        if not tempo.TX_HASH.match(tx_hash):
            log_event(log, "tempo payment intent without a transaction", level=logging.WARNING, payment_intent=intent_id)
            return EventResult("ignored")
        purchase = store.get_credit_purchase(tx_hash)
        if purchase is None or purchase.method != tempo.METHOD:
            log_event(log, "tempo payment intent for no purchase here", level=logging.WARNING, payment_intent=intent_id,
                      transaction=tx_hash)
            return EventResult("ignored")
        problem = _settlement_mismatch(intent, purchase)
        if problem:
            log_event(log, "stripe settlement does not match the tempo purchase", level=logging.ERROR,
                      transaction=tx_hash, payment_intent=intent_id, problem=problem)
            return EventResult("ignored", purchase.key_id)
        # This PaymentIntent has settled this transfer, by Stripe's own record of it. It becomes the purchase's
        # link, unless the purchase is linked to another one that has settled it too. One the purchase is linked
        # to that has not (a replacement made after an attempt Stripe left unknown, then declined or still
        # processing) gives way: otherwise this settlement would be ignored and the credit stranded.
        linked, replacing = purchase.payment_intent, None
        if linked and linked != intent_id:
            current = self._retrieve_intent(linked)
            if current is None:
                raise BillingProviderError("Stripe could not be asked about the linked PaymentIntent")
            if _settlement_mismatch(current, purchase) is None:
                log_event(log, "a second stripe settlement of a tempo transaction; refund it", level=logging.ERROR,
                          transaction=tx_hash, payment_intent=intent_id, linked=linked)
                return EventResult("ignored", purchase.key_id)
            replacing = linked
        if not store.set_credit_payment_intent(tx_hash, intent_id, replacing=replacing):
            # Linked meanwhile (a parallel call). Decided again when Stripe delivers the event again.
            raise BillingProviderError("the purchase's PaymentIntent changed meanwhile")
        if replacing:
            log_event(log, "tempo purchase linked to the payment intent that settled it", level=logging.WARNING,
                      transaction=tx_hash, payment_intent=intent_id, replaced=replacing)
        purchase = store.get_credit_purchase(tx_hash) or purchase
        if purchase.status in (PURCHASE_UNMATCHED, PURCHASE_FAILED) or self._tempo is None:
            return EventResult("ignored", purchase.key_id)
        if purchase.credited:
            return EventResult("already_credited", purchase.key_id)
        if purchase.refunded_at:
            return EventResult("ignored", purchase.key_id)  # refunded before Stripe settled it: never credited
        try:
            result = self._advance_tempo(purchase, store, wait=False)
        except (PaymentRefused, PaymentConflict):
            return EventResult("ignored", purchase.key_id)
        if result.pending:
            # Work remains that could not be done now: the chain or Stripe could not be asked. Not acknowledged,
            # and not marked processed: the caller answers 5xx and Stripe delivers the event again.
            log_event(log, "tempo settlement not applied yet; stripe will deliver it again", level=logging.WARNING,
                      transaction=tx_hash, payment_intent=intent_id, status=purchase.status)
            raise BillingProviderError("the Tempo purchase could not be advanced now")
        return EventResult("credited" if result.added else "already_credited", purchase.key_id)

    # --- refunds (admin CLI) ---------------------------------------------------------------

    def refund(self, purchase: "CreditPurchase", amount_cents: int) -> tuple[str, str]:
        """Refund ``amount_cents`` of a purchase through Stripe. Returns (Refund id, its status).

        Stripe is asked first for the refunds its PaymentIntent already has
        (``check_refund``). This purchase's own refund (made by this method:
        ``calotype_purchase`` in its metadata) of this amount, not failed or
        canceled, is adopted and no other is made: its id may never have been
        saved, and Stripe forgets idempotency keys after a day. A new one is
        made, under a new idempotency key, only when every earlier one of ours
        failed. Raises ``RefundBlocked`` (nothing made) when the PaymentIntent
        is not Stripe's settlement of this purchase, or holds a refund not
        made for this purchase (a Dashboard refund, say) or of another amount:
        a person reconciles those. Raises ``BillingProviderError`` if Stripe
        refuses or cannot be reached (and then nothing was made), and
        ``ValueError`` for a Tempo payment that Stripe has no PaymentIntent
        for. A Tempo payment's refund goes back in stablecoins to the wallet
        that sent it (Stripe's rule for crypto).
        """
        import stripe

        if not 0 < amount_cents <= purchase.amount_cents:
            raise ValueError("the refund must be more than nothing and at most what was paid")
        intent_id, ours = self.check_refund(purchase, amount_cents)
        try:
            live = [r for r in ours if r.get("status") not in ("failed", "canceled")]
            if live:
                found = max(live, key=lambda r: int(r.get("created") or 0))
                log_event(log, "credit refund found in stripe", payment_intent=intent_id, refund=found.get("id"),
                          status=found.get("status"))
                return str(found["id"]), str(found.get("status") or "")
            suffix = f"_{len(ours)}" if ours else ""
            refund = self._client.v1.refunds.create(
                params={
                    "payment_intent": intent_id,
                    "amount": amount_cents,
                    "reason": "requested_by_customer",
                    "metadata": {
                        METADATA_KEY_ID: str(purchase.key_id),
                        METADATA_PACK: purchase.pack,
                        METADATA_REFUND_OF: purchase.id,
                    },
                },
                options={"idempotency_key": f"calotype_credit_refund_{purchase.id}{suffix}"},
            )
        except stripe.StripeError as e:
            self._log_refund_failure(e)
            raise BillingProviderError(f"{type(e).__name__} {getattr(e, 'code', None) or ''}".strip()) from None
        refund_id, status = _field(refund, "id"), _field(refund, "status")
        if not isinstance(refund_id, str) or not refund_id:
            raise BillingProviderError("Stripe returned no Refund id")
        log_event(log, "credit purchase refunded", payment_intent=intent_id, refund=refund_id, amount=amount_cents,
                  status=status)
        return refund_id, str(status or "")

    def check_refund(
        self, purchase: "CreditPurchase", amount_cents: int | None = None
    ) -> tuple[str, list[dict[str, Any]]]:
        """Whether Stripe's records let ``refund`` refund this purchase. Makes nothing.

        Returns (the PaymentIntent to refund, this purchase's own earlier
        refunds on it). Raises ``RefundBlocked`` if a Tempo purchase's
        PaymentIntent is not Stripe's settlement of its transfer (not
        succeeded, or its charge names another transaction or amount), if the
        PaymentIntent holds a live refund not made for this purchase, or (with
        ``amount_cents``) one of ours of another amount. Raises
        ``BillingProviderError`` if Stripe cannot be asked and ``ValueError``
        if Stripe has no PaymentIntent for the purchase.
        """
        import stripe

        intent_id = purchase.stripe_payment_intent
        if intent_id is None or not _PAYMENT_INTENT.match(intent_id):
            raise ValueError("no Stripe PaymentIntent records this payment")
        try:
            if purchase.method == tempo.METHOD:
                intent = _plain(
                    self._client.v1.payment_intents.retrieve(intent_id, params={"expand": ["latest_charge"]})
                )
                problem = _settlement_mismatch(intent, purchase)
                if problem:
                    raise RefundBlocked(
                        f"PaymentIntent {intent_id} is not Stripe's settlement of transaction {purchase.id} "
                        f"({problem}; status {intent.get('status')}). Run 'record-tempo {purchase.id}' and refund "
                        "once it has settled."
                    )
            listed = _plain(self._client.v1.refunds.list(params={"payment_intent": intent_id, "limit": 100}))
        except stripe.StripeError as e:
            self._log_refund_failure(e)
            raise BillingProviderError(f"{type(e).__name__} {getattr(e, 'code', None) or ''}".strip()) from None
        earlier = [r for r in listed.get("data") or [] if isinstance(r, Mapping)]
        ours = [r for r in earlier if (r.get("metadata") or {}).get(METADATA_REFUND_OF) == purchase.id]
        others = [
            r for r in earlier
            if r.get("status") not in ("failed", "canceled") and (
                r not in ours or (amount_cents is not None and r.get("amount") != amount_cents)
            )
        ]
        if others:
            found = "; ".join(
                f"{r.get('id')} ${int(r.get('amount') or 0) / 100:.2f} {r.get('status')}"
                + ("" if r in ours else " (not made by refund-credit)")
                for r in others
            )
            log_event(log, "refund needs reconciling", level=logging.WARNING, payment_intent=intent_id,
                      refunds=[r.get("id") for r in others])
            raise RefundBlocked(
                f"PaymentIntent {intent_id} already has refunds this refund does not match: {found}. No refund was "
                "made. Reconcile them in the Stripe Dashboard and refund what is still owed by hand."
            )
        return intent_id, ours

    @staticmethod
    def _log_refund_failure(e: Exception) -> None:
        log_event(
            log,
            "stripe refund failed",
            level=logging.ERROR,
            error=type(e).__name__,
            code=getattr(e, "code", None),
            http_status=getattr(e, "http_status", None),
        )


def credits_from_env(environ: Mapping[str, str] | None = None) -> CreditsProvider:
    """``StripeCredits`` if prepaid credit is switched on and fully configured, otherwise ``NoCredits``."""
    config, missing = CreditsConfig.from_env(environ)
    if config is None:
        return NoCredits(missing)
    return StripeCredits(config)


def credits_for_admin(
    environ: Mapping[str, str] | None = None, *, client: Any = None, tempo_transport: Any = None
) -> StripeCredits | None:
    """A provider for the admin CLI, or None if ``STRIPE_SECRET_KEY`` is not set.

    A refund, a deposit address and a Tempo purchase already on the chain
    (``paid``, ``unmatched``) need only the secret key. Looking a ``pending``
    Tempo purchase up on the chain needs the Tempo settings the service runs
    with (``STRIPE_TEMPO_DEPOSIT_ADDRESS``, ``CALOTYPE_TEMPO_RPC_URL``): read
    here, the chain following the key's mode as in the service. Without them
    that lookup raises ``TempoNotConfigured``, naming what is missing.
    """
    env = os.environ if environ is None else environ
    secret = env.get("STRIPE_SECRET_KEY", "").strip()
    if not secret:
        return None
    tempo_config, missing = tempo.TempoConfig.from_env(env, secret)
    config = CreditsConfig(
        secret_key=secret,
        api_version=env.get("STRIPE_MPP_API_VERSION", "").strip() or DEFAULT_API_VERSION,
        tempo=tempo_config,
    )
    provider = StripeCredits(config, client=client, tempo_transport=tempo_transport)
    provider.tempo_missing = tuple(missing)
    return provider


def describe(pack: "CreditPack") -> str:
    """One line for the buyer's wallet. Plain ASCII without commas, so every header parser reads it alike."""
    title = re.sub(r"[^A-Za-z0-9 ._-]", "", pack.title or pack.name).strip() or "pack"
    return f"Calotype {title}: {pack.screenshots} prepaid screenshots"


def _charge_crypto(intent: Mapping[str, Any]) -> Mapping[str, Any]:
    """Stripe's own record of the crypto transfer behind a PaymentIntent (its latest charge), or {}."""
    charge = intent.get("latest_charge")
    if not isinstance(charge, Mapping):
        return {}
    details = charge.get("payment_method_details")
    if not isinstance(details, Mapping):
        return {}
    crypto = details.get("crypto")
    return crypto if isinstance(crypto, Mapping) else {}


def _best_candidate(intents: list[dict[str, Any]], purchase: "CreditPurchase") -> dict[str, Any] | None:
    """Of the PaymentIntents that record a Tempo purchase, the one to link: one that settled it (by its
    charge), else one that still may; None if every one was declined."""
    for intent in intents:
        if _settlement_mismatch(intent, purchase) is None:
            return intent
    for intent in intents:
        if intent.get("status") not in DECLINED_STATUSES:
            return intent
    return None


def _settlement_mismatch(intent: Mapping[str, Any], purchase: "CreditPurchase") -> str | None:
    """Why a succeeded PaymentIntent is not the settlement of this Tempo purchase, or None if it is."""
    if intent.get("status") != "succeeded":
        return "not succeeded"
    if intent.get("amount_received") != purchase.amount_cents or intent.get("currency") != purchase.currency:
        return "amount"
    crypto = _charge_crypto(intent)
    if str(crypto.get("transaction_hash") or "").lower() != purchase.id:
        return "transaction"
    if crypto.get("network") != tempo.NETWORK:
        return "network"
    tagged = (intent.get("metadata") or {}).get(METADATA_TEMPO_TX)
    if tagged is not None and str(tagged).lower() != purchase.id:
        return "metadata"
    return None


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
