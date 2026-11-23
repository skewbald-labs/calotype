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
- The chain is checked by the SDK's ``ChargeIntent`` (``validate`` then
  ``broadcast``, the lifecycle its own server runs): the RPC node is on the
  challenge's chain (``eth_chainId``); a pull transaction is a Tempo
  transaction for that chain, whose transfer pays this amount of this token
  to this recipient, is broadcast and must succeed; a push hash must have a
  successful receipt with such a Transfer log. Either way the transfer must
  carry the MPP attribution memo bound to this challenge id and realm, so a
  transfer made for one challenge (someone else's, seen on the chain) cannot
  pay another. The SDK's replay record (``ClaimStore``, in Calotype's own
  storage, shared by every instance) lets one transaction pay once.
- The result is a ``Payment`` with the transaction hash. ``credits.py``
  credits the pack under that hash (``add_credit_purchase``, atomic and
  once), then records the payment in Stripe as a crypto PaymentIntent.

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
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
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


def charge_request(config: TempoConfig, amount_cents: int) -> dict[str, Any]:
    """The MPP ``request`` for a Tempo charge of ``amount_cents`` US cents (1 USDC = 100 cents)."""
    return {
        "amount": str(amount_cents * BASE_UNITS_PER_CENT),
        "currency": config.currency,
        "recipient": config.recipient,
        "methodDetails": {"chainId": config.chain_id},
    }


class ClaimStore:
    """The SDK's replay store (``mpp.store.Store``) over Calotype's storage, for one challenge.

    A claim belongs to the challenge being paid. Another challenge cannot take
    or release it; the same challenge can, so a buyer who sends the same
    credential again after a failure (this server stopped between the chain
    and the credit) has it checked again instead of refused as used. Crediting
    is keyed by the transaction hash, so checking it twice credits it once.
    """

    def __init__(self, storage: Storage, owner: str):
        self._storage = storage
        self._owner = owner

    async def get(self, key: str) -> Any | None:
        found = self._storage.get_payment_claim(key)
        if found is None or found[0] == self._owner:
            return None
        return found[1]

    async def put(self, key: str, value: Any) -> None:
        if not self._storage.claim_payment(key, self._owner, str(value)):
            raise RuntimeError("the payment is claimed by another challenge")

    async def delete(self, key: str) -> None:
        self._storage.release_payment_claim(key, self._owner)

    async def put_if_absent(self, key: str, value: Any) -> bool:
        return self._storage.claim_payment(key, self._owner, str(value))


class TempoMethod:
    """Checks a ``tempo`` credential on the chain with ``pympp``. ``transport`` replaces the network in tests."""

    def __init__(self, config: TempoConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport

    def request(self, amount_cents: int) -> dict[str, Any]:
        return charge_request(self.config, amount_cents)

    def settle(self, credential: mpp.Credential, request: dict[str, Any], storage: Storage) -> Payment:
        """Verify, and for a signed transaction broadcast, the payment. Raises ``Refused`` or ``Unconfirmed``.

        ``credential`` must already be checked against its challenge, and
        ``request`` must be this server's own terms for it.
        """
        payload_type = credential.payload.get("type")
        if payload_type not in ("transaction", "hash"):
            # "proof" pays a zero amount and "zone" a Zone withdrawal; neither is offered.
            raise Refused(
                "malformed-credential",
                "A tempo credential's payload needs type 'transaction' (a signed transfer) or 'hash'.",
            )
        try:
            return _run(self._settle(credential, request, storage))
        except (Refused, Unconfirmed):
            raise
        except Exception as e:
            raise _translate(e) from None

    async def _settle(self, credential: mpp.Credential, request: dict[str, Any], storage: Storage) -> Payment:
        import httpx
        from mpp import ChallengeEcho
        from mpp import Credential as SdkCredential
        from mpp.methods.tempo.intents import ChargeIntent

        sdk_credential = SdkCredential(
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
        async with httpx.AsyncClient(transport=self._transport) as client:
            intent = ChargeIntent(
                rpc_url=self.config.node,
                http_client=client,
                store=ClaimStore(storage, credential.id),
            )
            # The SDK server's own order: the check that changes nothing, then the one that settles.
            await intent.validate(sdk_credential, request)
            receipt = await intent.broadcast(sdk_credential, request)
        tx_hash = str(receipt.reference).lower()
        if not TX_HASH.match(tx_hash):
            raise Unconfirmed("the SDK returned no transaction hash")
        return Payment(tx_hash, receipt.payer)


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


def _run(coro: Any) -> Any:
    """Run a coroutine to the end from synchronous code (``settle`` runs in a worker thread)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
