"""Stablecoin payment for prepaid credit: MPP method ``tempo``, USDC on the Tempo chain.

``credits.py`` sells a pack over the Machine Payments Protocol. With
``CALOTYPE_CREDITS_STABLECOIN=1`` its 402 carries a second challenge next to
the card one: ``method="tempo"``, ``intent="charge"``, a TIP-20 transfer of
the pack's price to a Stripe crypto deposit address. Stripe turns what reaches
that address into dollars in the Stripe balance
(https://docs.stripe.com/payments/machine/mpp, "Create a deposit address").

What this module does, and what it leaves to the protocol's official SDK
(``pympp``, https://github.com/tempoxyz/pympp, by Tempo Labs):

- The challenge's ``request`` is built here, in the SDK's wire form
  (https://paymentauth.org/draft-tempo-charge-00 section 4): ``amount`` in
  base units (6 decimals), ``currency`` (the token's address), ``recipient``
  (the deposit address) and ``methodDetails.chainId``. No ``feePayer``: the
  buyer pays the network fee. No ``supportedModes``: both ``pull`` (a signed
  transaction the server broadcasts) and ``push`` (the hash of one the buyer
  broadcast) are accepted.
- ``credits.py`` checks the challenge binding first, as for a card: issued
  here (HMAC id), unchanged, for this key and pack, not expired, and its
  ``request`` exactly what this pack costs now. Every term the chain is then
  checked against therefore comes from this server, never from the buyer.
- The chain is checked by the SDK's ``ChargeIntent`` in two steps.
  ``check`` (the SDK's ``validate``) changes nothing: the RPC node is on the
  challenge's chain (``eth_chainId``); a pull transaction is a Tempo
  transaction for that chain whose transfer pays this amount of this token to
  this recipient; a push hash has a successful receipt with such a Transfer
  log. Either way the transfer must carry the MPP attribution memo bound to
  this challenge id and realm, so a transfer made for one challenge (someone
  else's, seen on the chain) cannot pay another. It returns the transaction
  hash, known before anything is broadcast.
- ``credits.py`` then binds the challenge to that one hash in storage
  (``bind_tempo_payment``) before anything moves, so a challenge is paid by
  one transfer at most. Only then does ``broadcast`` send a pull transaction
  (the SDK's ``broadcast``). Its replay record (``ClaimStore``) is scoped to
  one attempt, so a failing attempt never releases a claim a parallel one holds.
- ``lookup`` reconciles a transaction by its hash alone, and never sends it:
  the receipt is found and checked against the terms stored with the purchase
  (or, for a buyer-broadcast hash never bound, the expired challenge's own
  signed terms) ("paid"), or the transfer reverted or can no longer be
  included, by chain time past its ``valid_before`` ("failed"), or neither is
  known yet ("pending"). This is what answers a buyer who retries after the
  challenge expired. (Within the challenge's validity, ``credits.py`` may
  ``broadcast`` the same signed bytes of a pending purchase once more: the
  same transaction, not a new payment.)
- The result is a transaction hash. ``credits.py`` releases the pack's credit
  only once Stripe has independently matched a settled payment to it.

The chain follows the Stripe key, as ``mppx``'s ``stripe.create`` does: a
live key takes Tempo mainnet (chain 4217) and USDC.e; a sandbox key takes the
Moderato testnet (42431) and pathUSD, the test token the SDK pairs with
Stripe's sandbox and the one a sandbox deposit address lists. Neither can be
overridden, so a live server can never accept a testnet token.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import mpp
from .logs import log_event

if TYPE_CHECKING:
    import httpx

    from .storage import Storage

log = logging.getLogger("calotype.tempo")

METHOD = "tempo"
INTENT = "charge"
NETWORK = "tempo"  # Stripe's name for the chain, in deposit addresses and PaymentIntents
DECIMALS = 6
BASE_UNITS_PER_CENT = 10 ** (DECIMALS - 2)

# The SDK version this module is written against. ``lookup`` and ``credential_transaction_hash`` call
# private functions of it (``_get_transaction_receipt``, ``_confirm_not_included``, ``_validate_hash``,
# ``_assert_rpc_chain_id``, ``_raw_transaction_hash``, ``_canonical_raw_transaction``); requirements.txt pins
# it, and tests/test_tempo.py checks both and those functions' behaviour. Moving it means reading them again.
PYMPP_VERSION = "0.26.0"

# From pympp 0.26.0, mpp/methods/tempo/_defaults.py.
LIVE_CHAIN_ID = 4217  # Tempo mainnet
TEST_CHAIN_ID = 42431  # Moderato, the testnet
LIVE_CURRENCY = "0x20C000000000000000000000b9537d11c60E8b50"  # USDC.e
TEST_CURRENCY = "0x20c0000000000000000000000000000000000000"  # pathUSD
RPC_URLS = {LIVE_CHAIN_ID: "https://rpc.tempo.xyz", TEST_CHAIN_ID: "https://rpc.moderato.tempo.xyz"}

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_RPC_URL = re.compile(r"^https://[^\s/]+(/\S*)?$")


class Refused(Exception):
    """The credential does not pay this challenge. Nothing moved, or what moved pays something else."""

    def __init__(self, problem: str, detail: str):
        super().__init__(detail)
        self.problem = problem
        self.detail = detail


class Unconfirmed(Exception):
    """The outcome is not known (the chain could not be asked, or a broadcast gave no answer).

    The buyer must not be asked to pay again; the same credential may be sent again.
    """


@dataclass(frozen=True)
class Payment:
    transaction_hash: str  # lower case, 0x and 64 hex digits
    payer: str | None  # the paying address, when the SDK could tell it


@dataclass(frozen=True)
class Checked:
    """A credential the SDK found to pay its challenge, before anything was broadcast."""

    transaction_hash: str  # lower case, 0x and 64 hex digits
    mode: str  # "push": already on the chain, receipt checked; "pull": a signed transaction to broadcast
    valid_before: int | None  # pull: after this Unix time it can no longer be included


# What ``lookup`` found for a bound transaction.
CHAIN_PAID = "paid"
CHAIN_FAILED = "failed"
CHAIN_PENDING = "pending"


def is_live_key(stripe_secret_key: str) -> bool:
    """Stripe's sample decides live mode the same way: ``!key.includes('_test_')``."""
    return "_test_" not in stripe_secret_key


@dataclass(frozen=True)
class TempoConfig:
    recipient: str  # the Stripe crypto deposit address on Tempo
    livemode: bool
    rpc_url: str = ""  # "": the public node of the chain

    @property
    def chain_id(self) -> int:
        return LIVE_CHAIN_ID if self.livemode else TEST_CHAIN_ID

    @property
    def currency(self) -> str:
        return LIVE_CURRENCY if self.livemode else TEST_CURRENCY

    @property
    def node(self) -> str:
        return self.rpc_url or RPC_URLS[self.chain_id]

    @classmethod
    def from_env(cls, env: Mapping[str, str], stripe_secret_key: str) -> tuple[TempoConfig | None, list[str]]:
        """The config, or None and the names of the settings that are missing or wrong."""
        recipient = env.get("STRIPE_TEMPO_DEPOSIT_ADDRESS", "").strip()
        rpc_url = env.get("CALOTYPE_TEMPO_RPC_URL", "").strip()
        missing = []
        if not ADDRESS.match(recipient):
            missing.append("STRIPE_TEMPO_DEPOSIT_ADDRESS")
        if rpc_url and not _RPC_URL.match(rpc_url):
            missing.append("CALOTYPE_TEMPO_RPC_URL")
        try:
            import mpp.methods.tempo.intents  # noqa: F401  (the SDK and its "tempo" extra)
        except ImportError:
            missing.append("pympp[tempo]")
        if missing:
            return None, missing
        return cls(recipient=recipient, livemode=is_live_key(stripe_secret_key), rpc_url=rpc_url), []


def deposit_address_matches(config: TempoConfig, address: Mapping[str, Any]) -> bool:
    """Whether one of the Stripe account's deposit addresses (``GET /v1/crypto/deposit_addresses``) is the
    configured recipient, on Tempo, in the key's mode, and takes the token this server asks for."""
    if str(address.get("address") or "").lower() != config.recipient.lower():
        return False
    if address.get("network") not in (None, NETWORK):
        return False
    if "livemode" in address and bool(address.get("livemode")) != config.livemode:
        return False
    tokens = address.get("supported_tokens") or []
    return any(
        isinstance(t, Mapping) and str(t.get("token_contract_address") or "").lower() == config.currency.lower()
        for t in tokens
    )


def credential_transaction_hash(credential: mpp.Credential) -> str | None:
    """The transaction a credential carries, lower case, without asking the chain: a push credential's hash,
    or the hash of a pull credential's signed transaction (pympp's own function). None if it has none."""
    payload = credential.payload
    if payload.get("type") == "hash":
        found = str(payload.get("hash") or "").lower()
        return found if TX_HASH.match(found) else None
    if payload.get("type") == "transaction" and isinstance(payload.get("signature"), str):
        try:
            from mpp.methods.tempo.intents import _canonical_raw_transaction, _raw_transaction_hash

            found = _raw_transaction_hash(_canonical_raw_transaction(payload["signature"])).lower()
        except Exception:
            return None
        return found if TX_HASH.match(found) else None
    return None


def charge_request(config: TempoConfig, amount_cents: int) -> dict[str, Any]:
    """The MPP ``request`` for a Tempo charge of ``amount_cents`` US cents (1 USDC = 100 cents)."""
    return {
        "amount": str(amount_cents * BASE_UNITS_PER_CENT),
        "currency": config.currency,
        "recipient": config.recipient,
        "methodDetails": {"chainId": config.chain_id},
    }


def request_cents(request: Mapping[str, Any]) -> int | None:
    """The US cents a Tempo ``request`` asks for (its ``amount`` in base units), or None if not whole cents."""
    amount = str(request.get("amount") or "")
    if not amount.isdigit() or int(amount) <= 0 or int(amount) % BASE_UNITS_PER_CENT:
        return None
    return int(amount) // BASE_UNITS_PER_CENT


class ClaimStore:
    """The SDK's replay store (``mpp.store.Store``) over Calotype's storage, for one settlement attempt.

    The owner is the challenge id and an id of this attempt alone, so two
    attempts at once (the same credential sent twice) never share a claim: the
    second is refused while the first holds it, and a failing attempt can only
    release its own. A retry after a failure does not need the claim back:
    ``credits.py`` has bound the challenge to the transaction already and
    reconciles it by its hash (``TempoMethod.lookup``).
    """

    def __init__(self, storage: Storage, challenge_id: str):
        self._storage = storage
        self._owner = f"{challenge_id}#{uuid.uuid4().hex}"

    async def get(self, key: str) -> Any | None:
        found = self._storage.get_payment_claim(key)
        if found is None or found[0] == self._owner:
            return None
        return found[1]

    async def put(self, key: str, value: Any) -> None:
        if not self._storage.claim_payment(key, self._owner, str(value)):
            raise RuntimeError("the payment is claimed by another attempt")

    async def delete(self, key: str) -> None:
        self._storage.release_payment_claim(key, self._owner)

    async def put_if_absent(self, key: str, value: Any) -> bool:
        return self._storage.claim_payment(key, self._owner, str(value))


class TempoMethod:
    """Checks a ``tempo`` credential on the chain with ``pympp``. ``transport`` replaces the network in tests.

    Every method is synchronous and runs the SDK's coroutines itself: call it
    from a worker thread (the app uses ``asyncio.to_thread``), never from a
    running event loop.
    """

    def __init__(self, config: TempoConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    def request(self, amount_cents: int) -> dict[str, Any]:
        return charge_request(self.config, amount_cents)

    def check(self, credential: mpp.Credential, request: dict[str, Any]) -> Checked:
        """Whether the credential pays its challenge, changing nothing. Raises ``Refused`` or ``Unconfirmed``.

        ``credential`` must already be checked against its challenge, and
        ``request`` must be this server's own terms for it.
        """
        _no_running_loop()
        payload_type = credential.payload.get("type")
        if payload_type not in ("transaction", "hash"):
            # "proof" pays a zero amount and "zone" a Zone withdrawal; neither is offered.
            raise Refused(
                "malformed-credential",
                "A tempo credential's payload needs type 'transaction' (a signed transfer) or 'hash'.",
            )
        try:
            return _run(self._check(credential, request))
        except (Refused, Unconfirmed):
            raise
        except Exception as e:
            raise _translate(e) from None

    def broadcast(self, credential: mpp.Credential, request: dict[str, Any], storage: Storage) -> Payment:
        """Send a checked pull transaction and wait for its receipt. Raises ``Refused`` or ``Unconfirmed``."""
        _no_running_loop()
        try:
            return _run(self._broadcast(credential, request, storage))
        except (Refused, Unconfirmed):
            raise
        except Exception as e:
            raise _translate(e) from None

    def lookup(self, tx_hash: str, terms: Mapping[str, Any], valid_before: int | None) -> str:
        """Where a bound transaction is, by its hash alone: ``CHAIN_PAID``, ``CHAIN_FAILED`` or ``CHAIN_PENDING``.

        ``terms`` are the ones stored when the challenge was bound (``request``,
        ``realm``, ``challenge_id``). Nothing is broadcast. Raises
        ``Unconfirmed`` if the node cannot be asked.
        """
        _no_running_loop()
        try:
            return _run(self._lookup(tx_hash, terms, valid_before))
        except Unconfirmed:
            raise
        except Exception as e:
            log_event(log, "tempo lookup failed", level=logging.WARNING, error=type(e).__name__)
            raise Unconfirmed(type(e).__name__) from None

    @staticmethod
    def _sdk_credential(credential: mpp.Credential) -> Any:
        from mpp import ChallengeEcho
        from mpp import Credential as SdkCredential

        return SdkCredential(
            challenge=ChallengeEcho(
                id=credential.id,
                realm=credential.realm,
                method=credential.method,
                intent=credential.intent,
                request=credential.request,
                expires=credential.expires or None,
                digest=credential.digest or None,
                opaque=credential.opaque or None,
                header=credential.header or None,
            ),
            payload=dict(credential.payload),
            source=credential.source or None,
        )

    async def _check(self, credential: mpp.Credential, request: dict[str, Any]) -> Checked:
        import httpx
        from mpp.methods.tempo.intents import ChargeIntent

        async with httpx.AsyncClient(transport=self._transport) as client:
            # No store: validate only reads it, and the binding in storage is the replay record here.
            intent = ChargeIntent(rpc_url=self.config.node, http_client=client, store=None)
            validation = await intent.validate(self._sdk_credential(credential), request)
        details = validation.details or {}
        tx_hash = str(details.get("transaction_hash") or "").lower()
        if not TX_HASH.match(tx_hash):
            # Only a fee-sponsored transaction has no hash before it is sent, and none is offered.
            raise Refused("malformed-credential", "The tempo credential's transaction hash cannot be known.")
        mode = "push" if details.get("mode") == "push" else "pull"
        valid_before = details.get("valid_before")
        return Checked(tx_hash, mode, int(valid_before) if isinstance(valid_before, int) else None)

    async def _broadcast(self, credential: mpp.Credential, request: dict[str, Any], storage: Storage) -> Payment:
        import httpx
        from mpp.methods.tempo.intents import ChargeIntent

        async with httpx.AsyncClient(transport=self._transport) as client:
            intent = ChargeIntent(
                rpc_url=self.config.node,
                http_client=client,
                store=ClaimStore(storage, credential.id),
            )
            receipt = await intent.broadcast(self._sdk_credential(credential), request)
        tx_hash = str(receipt.reference).lower()
        if not TX_HASH.match(tx_hash):
            raise Unconfirmed("the SDK returned no transaction hash")
        return Payment(tx_hash, receipt.payer)

    async def _lookup(self, tx_hash: str, terms: Mapping[str, Any], valid_before: int | None) -> str:
        import httpx
        from mpp.errors import VerificationError
        from mpp.methods.tempo.intents import ChargeIntent
        from mpp.methods.tempo.schemas import ChargeRequest, HashCredentialPayload

        request = ChargeRequest.model_validate(terms["request"])
        async with httpx.AsyncClient(transport=self._transport) as client:
            intent = ChargeIntent(rpc_url=self.config.node, http_client=client, store=None)
            # pympp 0.26.0's own receipt check, the one its validate runs for a hash credential, without
            # the challenge's expiry (the challenge was authorized when it was bound). The SDK is pinned.
            await intent._assert_rpc_chain_id(request.methodDetails.chainId)
            if await intent._get_transaction_receipt(client, tx_hash) is None:
                if valid_before is not None and await intent._confirm_not_included(client, tx_hash, valid_before):
                    return CHAIN_FAILED
                return CHAIN_PENDING
            try:
                await intent._validate_hash(
                    HashCredentialPayload(type="hash", hash=tx_hash),
                    request,
                    challenge_id=str(terms["challenge_id"]),
                    realm=str(terms["realm"]),
                )
            except VerificationError as e:
                # A receipt that reverted, or that does not pay what was bound. Either way nothing was paid.
                log_event(log, "tempo transaction did not pay", level=logging.WARNING, error=type(e).__name__)
                return CHAIN_FAILED
        return CHAIN_PAID


def _translate(e: Exception) -> Exception:
    """The SDK's verdict as ``Refused`` (nothing to pay for; ask again) or ``Unconfirmed`` (do not)."""
    from mpp import errors
    from pydantic import ValidationError

    if isinstance(e, (errors.InternalPaymentError, errors.PaymentOutcomeUnknownError)):
        log_event(log, "tempo payment unconfirmed", level=logging.ERROR, error=type(e).__name__,
                  cause=type(e.__cause__).__name__ if e.__cause__ else None)
        return Unconfirmed(type(e).__name__)
    if isinstance(e, errors.PaymentExpiredError):
        return Refused("payment-expired", "The challenge has expired. Pay the new one.")
    if isinstance(e, (errors.MalformedCredentialError, errors.InvalidPayloadError, ValidationError)):
        return Refused("malformed-credential", "The tempo credential's payload cannot be read.")
    if isinstance(e, errors.PaymentError):
        log_event(log, "tempo payment refused", level=logging.WARNING, error=type(e).__name__)
        detail = re.sub(r"[^\x20-\x7e]", "", str(e))[:300] or "The payment does not match the challenge."
        return Refused("verification-failed", f"The Tempo payment was not accepted: {detail}")
    # Anything else is a fault here or on the network. It may have happened after a broadcast,
    # so the answer must not be "pay again".
    log_event(log, "tempo payment check failed", level=logging.ERROR, error=type(e).__name__)
    return Unconfirmed(type(e).__name__)


def _no_running_loop() -> None:
    """Refuse to run on an event loop's thread: the SDK's coroutines would block it until they finish."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("tempo payments must be settled from a worker thread (asyncio.to_thread), not an event loop")


def _run(coro: Any) -> Any:
    """Run a coroutine to the end from a worker thread, which has no event loop of its own."""
    return asyncio.run(coro)
