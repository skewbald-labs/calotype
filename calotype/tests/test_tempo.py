"""Prepaid credit paid in USDC on Tempo: MPP method ``tempo`` on ``POST /v1/credits`` (``tempo.py``).

Nothing here reaches a chain or Stripe. The Tempo node is ``FakeChain``, an
in-process JSON-RPC answerer behind an ``httpx`` transport, built like the
fake in ``pympp``'s own adversarial tests; Stripe is ``FakePayments`` from
``test_credits``. The verification itself is the real SDK (``pympp``): the
buyer here signs real Tempo transactions with a throwaway key, and the server
decodes, checks and "broadcasts" them through ``ChargeIntent``. Skipped
without ``pympp[tempo]``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("mpp.methods.tempo.intents", reason="pympp[tempo], the official MPP SDK, is not installed")
pytest.importorskip("pytempo", reason="pympp[tempo], the official MPP SDK, is not installed")

import httpx  # noqa: E402
import stripe  # noqa: E402
from mpp.methods.tempo._attribution import encode as attribution_memo  # noqa: E402
from mpp.methods.tempo.intents import (  # noqa: E402
    TRANSFER_SELECTOR,
    TRANSFER_TOPIC,
    TRANSFER_WITH_MEMO_SELECTOR,
    TRANSFER_WITH_MEMO_TOPIC,
    _canonical_raw_transaction,
    _raw_transaction_hash,
)
from pytempo import Call, TempoTransaction  # noqa: E402

from calotype import admin, mpp, tempo  # noqa: E402
from calotype.credits import CreditsConfig, StripeCredits, credits_for_admin, credits_from_env  # noqa: E402
from calotype.storage import current_month  # noqa: E402

from .test_admin_keys import admin_store  # noqa: E402, F401  (a fixture: the store the admin CLI opens)
from .test_billing import SECRET_KEY, event, post_event  # noqa: E402
from .test_credits import (  # noqa: E402
    CREDITS_ENV,
    SPT,
    FakePayments,
    crypto_charge,
    auth,
    challenge_params,
    credential,
    logged,
    make_client,
)

DEPOSIT = "0x5ff8d73e8bccd3701c9aef78389f3b9771172b5c"
CHAIN = tempo.TEST_CHAIN_ID  # a sandbox key: the Moderato testnet
TOKEN = tempo.TEST_CURRENCY
OTHER_TOKEN = "0x20c0000000000000000000000000000000000001"
OTHER_ADDRESS = "0x9999999999999999999999999999999999999999"
PAYER_KEY = "0x" + "4f" * 32  # a throwaway key, only ever used against FakeChain
TEMPO_ENV = {**CREDITS_ENV, "CALOTYPE_CREDITS_STABLECOIN": "1", "STRIPE_TEMPO_DEPOSIT_ADDRESS": DEPOSIT}
PRICE = 19_000_000  # $19 in base units of a 6-decimal token


def word(value: int | str) -> str:
    if isinstance(value, str):
        return value.removeprefix("0x").lower().zfill(64)
    return hex(value)[2:].zfill(64)


def tx_hash_of(raw: str) -> str:
    """The hash a node reports for a submitted transaction (the SDK broadcasts the canonical form)."""
    return _raw_transaction_hash(_canonical_raw_transaction(raw)).lower()


class FakeChain:
    """A Tempo node: answers ``eth_chainId``, mines what it is sent, and returns receipts.

    ``effects`` maps a transaction hash to the Transfer logs the transaction
    emits when mined; a test that signs a transaction says what it does.
    Thread-safe, because each request settles in a worker thread of its own.
    """

    def __init__(self, chain_id: int = CHAIN):
        self.chain_id = chain_id
        self.receipts: dict[str, dict] = {}
        self.effects: dict[str, list[dict]] = {}
        self.calls: list[str] = []
        self.sent: list[str] = []
        self.down = False
        self.block_time: int | None = None  # the latest block's timestamp; None: now
        self.reject_sends: str | None = None  # an RPC error message for eth_sendRawTransactionSync
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self._handle)

    def count(self, method: str) -> int:
        return self.calls.count(method)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("the node is down", request=request)
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        with self._lock:
            self.calls.append(method)
            if method == "eth_chainId":
                answer: dict[str, Any] = {"result": hex(self.chain_id)}
            elif method == "eth_getTransactionReceipt":
                answer = {"result": self.receipts.get(params[0].lower())}
            elif method == "eth_getTransactionByHash":
                answer = {"result": None}
            elif method == "eth_getBlockByNumber":
                answer = {"result": {"number": "0x10", "timestamp": hex(self.block_time or int(time.time()))}}
            elif method == "eth_sendRawTransactionSync":
                tx_hash = tx_hash_of(params[0])
                if self.reject_sends:
                    answer = {"error": {"code": -32000, "message": self.reject_sends}}
                elif tx_hash in self.receipts:
                    answer = {"error": {"code": -32000, "message": "already known"}}
                else:
                    self.sent.append(params[0])
                    self.receipts[tx_hash] = receipt(tx_hash, self.effects.get(tx_hash, []))
                    answer = {"result": self.receipts[tx_hash]}
            else:
                answer = {"error": {"code": -32601, "message": f"unexpected {method}"}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **answer})


def transfer_log(*, amount=PRICE, recipient=DEPOSIT, token=TOKEN, memo: str | None, sender=OTHER_ADDRESS) -> dict:
    if memo is None:
        return {"address": token, "topics": [TRANSFER_TOPIC, "0x" + word(sender), "0x" + word(recipient)],
                "data": "0x" + word(amount)}
    return {
        "address": token,
        "topics": [TRANSFER_WITH_MEMO_TOPIC, "0x" + word(sender), "0x" + word(recipient), memo],
        "data": "0x" + word(amount),
    }


def receipt(tx_hash: str, logs: list[dict], status: str = "0x1") -> dict:
    return {"status": status, "transactionHash": tx_hash, "logs": logs, "from": OTHER_ADDRESS}


# --- the buyer -------------------------------------------------------------------------------------


def assert_refused(response, code: str):
    """A refused credential: 402, the problem named, fresh challenges for both methods, and no receipt."""
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["type"] == "https://paymentauth.org/problems/" + code.replace("_", "-")
    headers = response.headers.get_list("www-authenticate")
    assert len(headers) == 2 and f'id="{body["challengeId"]}"' in headers[0]
    assert "payment-receipt" not in response.headers
    return body


def tempo_params(response) -> dict[str, str]:
    """The tempo challenge among the 402's ``WWW-Authenticate`` headers."""
    import re

    for value in response.headers.get_list("www-authenticate"):
        params = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', value))
        if params.get("method") == "tempo":
            return params
    raise AssertionError("no tempo challenge in the 402")


def memo_for(params: dict[str, str]) -> str:
    """The MPP attribution memo a buyer's SDK puts on the transfer: bound to this challenge and realm."""
    return attribution_memo(challenge_id=params["id"], server_id=params["realm"])


def signed_transfer(
    chain: FakeChain,
    params: dict[str, str] | None,
    *,
    amount: int = PRICE,
    recipient: str = DEPOSIT,
    token: str = TOKEN,
    chain_id: int = CHAIN,
    memo: str | None = "bound",
    nonce: int = 0,
) -> str:
    """A buyer-signed Tempo transaction moving ``amount`` of ``token`` to ``recipient``; its effect is mined as such."""
    memo = memo_for(params) if memo == "bound" else memo
    if memo is None:
        data = f"0x{TRANSFER_SELECTOR}{word(recipient)}{word(amount)}"
    else:
        data = f"0x{TRANSFER_WITH_MEMO_SELECTOR}{word(recipient)}{word(amount)}{word(memo)}"
    tx = TempoTransaction.create(
        chain_id=chain_id,
        gas_limit=100_000,
        max_fee_per_gas=1,
        max_priority_fee_per_gas=1,
        nonce=nonce,
        nonce_key=(1 << 256) - 1,
        fee_token=None,
        valid_before=int(time.time()) + 300,
        calls=(Call.create(to=token, value=0, data=data),),
    )
    raw = "0x" + tx.sign(PAYER_KEY).encode().hex()
    chain.effects[tx_hash_of(raw)] = [transfer_log(amount=amount, recipient=recipient, token=token, memo=memo)]
    return raw


def pushed_transfer(chain: FakeChain, params: dict[str, str], tx_hash: str = "0x" + "ab" * 32, **log) -> str:
    """A transfer the buyer broadcast itself (push mode): already mined, known by its hash."""
    memo = log.pop("memo", memo_for(params))
    chain.receipts[tx_hash] = receipt(tx_hash, [transfer_log(memo=memo, **log)])
    return tx_hash


def ask(c, key: str, pack: str = "starter"):
    return c.post("/v1/credits", json={"pack": pack}, headers=auth(key))


def send(c, key: str, credential_header: str, pack: str = "starter"):
    return c.post("/v1/credits", json={"pack": pack}, headers={**auth(key), "Payment-Authorization": credential_header})


def pay_tempo(c, chain: FakeChain, key: str, *, push: bool = False, **tx):
    """Both steps, paying the tempo challenge. Returns (challenge params, credential, response)."""
    unpaid = ask(c, key)
    assert unpaid.status_code == 402, unpaid.text
    params = tempo_params(unpaid)
    if push:
        payload = {"type": "hash", "hash": pushed_transfer(chain, params, **tx)}
    else:
        payload = {"type": "transaction", "signature": signed_transfer(chain, params, **tx)}
    header = credential(params, payload)
    return params, header, send(c, key, header)


# --- fixtures ----------------------------------------------------------------------------------------


@pytest.fixture
def chain() -> FakeChain:
    return FakeChain()


DEPOSIT_ADDRESS = {
    "id": "cda_test_1", "object": "crypto.deposit_address", "livemode": False, "network": "tempo",
    "address": DEPOSIT, "supported_tokens": [{"token_currency": "usdc", "token_contract_address": TOKEN}],
}


@pytest.fixture
def payments() -> FakePayments:
    fake = FakePayments()
    fake.deposit_addresses = [DEPOSIT_ADDRESS]  # the deposit address is this Stripe account's own
    return fake


@pytest.fixture
def tempo_config() -> CreditsConfig:
    config, missing = CreditsConfig.from_env(TEMPO_ENV)
    assert missing == [], missing
    return replace(config, tempo_settle_wait_s=0)  # no waiting for Stripe in tests


@pytest.fixture
def tcredits(tempo_config, payments, chain) -> StripeCredits:
    return StripeCredits(tempo_config, client=payments.client, tempo_transport=chain.transport)


@pytest.fixture
def tclient(settings, store, fake_renderer, tcredits):
    with make_client(settings, store, fake_renderer, tcredits) as c:
        yield c


@pytest.fixture
def billing(payments):
    from calotype.billing import StripeBilling, StripeConfig

    from .test_billing import ENV as BILLING_ENV

    config, _ = StripeConfig.from_env(BILLING_ENV)
    return StripeBilling(config, client=payments.client)


# --- configuration -----------------------------------------------------------------------------------


def test_a_sandbox_key_takes_the_testnet_and_a_live_key_the_mainnet(tempo_config):
    assert tempo_config.tempo == tempo.TempoConfig(recipient=DEPOSIT, livemode=False)
    assert (tempo_config.tempo.chain_id, tempo_config.tempo.currency) == (42431, tempo.TEST_CURRENCY)
    assert tempo_config.tempo.node == "https://rpc.moderato.tempo.xyz"
    live, _ = CreditsConfig.from_env({**TEMPO_ENV, "STRIPE_SECRET_KEY": "sk_live_not_a_real_key"})
    assert (live.tempo.chain_id, live.tempo.currency) == (4217, "0x20C000000000000000000000b9537d11c60E8b50")
    assert live.tempo.node == "https://rpc.tempo.xyz"
    restricted, _ = CreditsConfig.from_env({**TEMPO_ENV, "STRIPE_SECRET_KEY": "rk_live_not_a_real_key"})
    assert restricted.tempo.livemode


@pytest.mark.parametrize(
    "changes, name",
    [
        ({"STRIPE_TEMPO_DEPOSIT_ADDRESS": ""}, "STRIPE_TEMPO_DEPOSIT_ADDRESS"),
        ({"STRIPE_TEMPO_DEPOSIT_ADDRESS": "0x1234"}, "STRIPE_TEMPO_DEPOSIT_ADDRESS"),
        ({"STRIPE_TEMPO_DEPOSIT_ADDRESS": "So1anaAddress"}, "STRIPE_TEMPO_DEPOSIT_ADDRESS"),
        ({"CALOTYPE_TEMPO_RPC_URL": "http://insecure.example"}, "CALOTYPE_TEMPO_RPC_URL"),
    ],
)
def test_a_bad_tempo_setting_leaves_the_endpoint_off(changes, name):
    config, names = CreditsConfig.from_env({**TEMPO_ENV, **changes})
    assert config is None and names == [name]


def test_an_own_node_can_be_named(tempo_config):
    config, _ = CreditsConfig.from_env({**TEMPO_ENV, "CALOTYPE_TEMPO_RPC_URL": "https://tempo.node.example/rpc"})
    assert config.tempo.node == "https://tempo.node.example/rpc"


def test_the_app_logs_the_methods_on_offer(monkeypatch, settings, store, fake_renderer, capsys):
    for name, value in TEMPO_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(StripeCredits, "tempo_deposit_addresses", lambda self, create=False: [DEPOSIT_ADDRESS])
    provider = credits_from_env()
    assert provider.methods == ("stripe", "tempo")
    with make_client(settings, store, fake_renderer, provider):
        pass
    out = capsys.readouterr().out
    assert "prepaid credit is on" in out and '"tempo"' in out and "42431" in out
    assert SECRET_KEY not in out


# --- the 402 -------------------------------------------------------------------------------------------


def test_the_402_offers_a_card_and_usdc_on_tempo(tclient, store, chain):
    raw, key = store.create_key("free")
    r = ask(tclient, raw)
    assert r.status_code == 402
    headers = r.headers.get_list("www-authenticate")
    assert [h.split('method="')[1].split('"')[0] for h in headers] == ["stripe", "tempo"]  # card first
    params = tempo_params(r)
    assert (params["intent"], params["realm"], params["header"]) == ("charge", "testserver", "Payment-Authorization")
    assert mpp.decode_object(params["request"]) == {
        "amount": "19000000",
        "currency": TOKEN,
        "recipient": DEPOSIT,
        "methodDetails": {"chainId": 42431},
    }
    assert mpp.decode_object(params["opaque"])["key"] == str(key.id)
    body = r.json()
    assert body["error"]["methods"] == ["stripe", "tempo"]
    assert "'tempo' (USDC on Tempo)" in body["error"]["message"]
    assert chain.calls == []  # asking costs nothing on the chain


# --- paying -------------------------------------------------------------------------------------------


def test_a_signed_transfer_is_broadcast_credited_and_recorded_in_stripe(tclient, store, chain, payments):
    raw, key = store.create_key("free")
    params, _, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 200, r.text
    tx_hash = tx_hash_of(chain.sent[0])
    body = r.json()
    assert body == {
        "pack": "starter",
        "screenshots_added": 5000,
        "credits": 5000,
        "already_credited": False,
        "payment": {"method": "tempo", "reference": tx_hash, "amount_usd": 19.0},
    }
    receipt_body = mpp.decode_object(r.headers["payment-receipt"])
    assert (receipt_body["method"], receipt_body["reference"], receipt_body["status"]) == ("tempo", tx_hash, "success")
    assert len(chain.sent) == 1  # broadcast once, by the server

    # Recorded in Stripe once, as pympp's recorder records it.
    assert payments.create.call_count == 1
    call = payments.create.call_args.kwargs
    assert call["options"] == {"idempotency_key": f"calotype_tempo_{tx_hash}", "stripe_version": "2026-07-29.preview"}
    p = call["params"]
    assert (p["amount"], p["currency"], p["confirm"]) == (1900, "usd", True)
    assert p["payment_method_data"] == {"type": "crypto"} and p["payment_method_types"] == ["crypto"]
    assert p["payment_method_options"] == {
        "crypto": {
            "mode": "transaction_verification",
            "transaction_verification_options": {"network": "tempo", "transaction_hash": tx_hash},
        }
    }
    assert p["metadata"]["calotype_tempo_tx"] == tx_hash and p["metadata"]["mpp_challenge_id"] == params["id"]
    assert p["metadata"]["calotype_key_id"] == str(key.id)

    purchase = store.get_credit_purchase(tx_hash)
    assert (purchase.method, purchase.key_id, purchase.payment_intent, purchase.challenge_id) == (
        "tempo", key.id, "pi_test_1", params["id"]
    )
    assert store.get_credit_balance(key.id) == 5000


def test_a_transfer_the_buyer_broadcast_is_checked_by_its_hash(tclient, store, chain, payments):
    raw, key = store.create_key("free")
    _, _, r = pay_tempo(tclient, chain, raw, push=True)
    assert r.status_code == 200, r.text
    assert r.json()["payment"]["reference"] == "0x" + "ab" * 32
    assert chain.sent == [] and chain.count("eth_getTransactionReceipt") >= 1
    assert store.get_credit_balance(key.id) == 5000
    assert store.get_credit_purchase("0x" + "ab" * 32).payment_intent == "pi_test_1"


def test_the_key_and_the_wallet_key_are_not_logged_or_stored(tclient, store, chain, caplog):
    caplog.set_level(logging.INFO)
    raw, _ = store.create_key("free")
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 200
    text = logged(caplog)
    for secret in (raw, PAYER_KEY, PAYER_KEY[2:], SECRET_KEY):
        assert secret not in text
    assert header.split(" ", 1)[1] not in text  # the signed transaction is not logged either


def test_the_card_path_is_unchanged_when_tempo_is_offered(tclient, store, chain, payments):
    raw, key = store.create_key("free")
    unpaid = ask(tclient, raw)
    params = challenge_params_for(unpaid, "stripe")
    r = send(tclient, raw, credential(params, {"spt": SPT}))
    assert r.status_code == 200, r.text
    assert r.json()["payment"] == {"method": "stripe", "reference": "pi_test_1", "amount_usd": 19.0}
    assert payments.create.call_args.kwargs["params"]["shared_payment_granted_token"] == SPT
    assert store.get_credit_purchase("pi_test_1").method == "stripe"
    assert chain.calls == []


def challenge_params_for(response, method: str) -> dict[str, str]:
    import re

    for value in response.headers.get_list("www-authenticate"):
        params = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', value))
        if params["method"] == method:
            return params
    raise AssertionError(method)


# --- replays -------------------------------------------------------------------------------------------


def test_the_same_credential_again_credits_nothing_more(tclient, store, chain, payments):
    raw, key = store.create_key("free")
    _, header, first = pay_tempo(tclient, chain, raw)
    again = send(tclient, raw, header)
    assert again.status_code == 200
    assert again.json()["already_credited"] is True and again.json()["screenshots_added"] == 0
    assert again.json()["payment"]["reference"] == first.json()["payment"]["reference"]
    assert len(chain.sent) == 1 and payments.create.call_count == 1
    assert store.get_credit_balance(key.id) == 5000


def test_a_signed_transfer_cannot_pay_a_second_challenge(tclient, store, chain):
    raw, key = store.create_key("free")
    _, header, first = pay_tempo(tclient, chain, raw)
    used = json.loads(mpp.b64url_decode(header.split(" ", 1)[1]))["payload"]
    params = tempo_params(ask(tclient, raw))
    r = send(tclient, raw, credential(params, used))
    assert_refused(r, "verification_failed")
    assert len(chain.sent) == 1 and store.get_credit_balance(key.id) == 5000


def test_a_pushed_transaction_cannot_pay_a_second_challenge(tclient, store, chain):
    raw, key = store.create_key("free")
    _, header, first = pay_tempo(tclient, chain, raw, push=True)
    params = tempo_params(ask(tclient, raw))
    r = send(tclient, raw, credential(params, {"type": "hash", "hash": "0x" + "ab" * 32}))
    assert_refused(r, "verification_failed")
    assert store.get_credit_balance(key.id) == 5000


def test_someone_elses_transfer_seen_on_the_chain_pays_nothing(tclient, store, chain):
    """Front-running: another key's buyer paid; its hash is public. Its memo names the other challenge."""
    raw_a, key_a = store.create_key("free")
    raw_b, key_b = store.create_key("free")
    params_a = tempo_params(ask(tclient, raw_a))
    tx_hash = pushed_transfer(chain, params_a)
    params_b = tempo_params(ask(tclient, raw_b))
    stolen = send(tclient, raw_b, credential(params_b, {"type": "hash", "hash": tx_hash}))
    assert_refused(stolen, "verification_failed")
    assert store.get_credit_balance(key_b.id) == 0
    # The rightful buyer can still use it.
    assert send(tclient, raw_a, credential(params_a, {"type": "hash", "hash": tx_hash})).status_code == 200
    assert store.get_credit_balance(key_a.id) == 5000


def test_the_same_credential_sent_at_once_from_many_requests_credits_once(tclient, store, chain, payments):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)})
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: send(tclient, raw, header), range(6)))
    ok = [r for r in results if r.status_code == 200]
    assert ok, [r.text for r in results]
    assert all(r.status_code in (200, 402, 502) for r in results)
    assert store.get_credit_balance(key.id) == 5000
    assert sum(1 for r in ok if not r.json()["already_credited"]) == 1


# --- payments that do not pay this challenge -------------------------------------------------------------


@pytest.mark.parametrize(
    "tx",
    [
        {"amount": PRICE - 1},
        {"amount": PRICE + 1},
        {"recipient": OTHER_ADDRESS},
        {"token": OTHER_TOKEN},
        {"chain_id": tempo.LIVE_CHAIN_ID},
        {"memo": None},  # a plain transfer: bound to no challenge
        {"memo": "0x" + "00" * 31 + "01"},  # a memo, but not this challenge's
    ],
    ids=["underpays", "overpays", "wrong-recipient", "wrong-token", "wrong-chain", "no-memo", "foreign-memo"],
)
def test_a_signed_transfer_that_does_not_pay_this_challenge_is_refused_and_not_broadcast(tclient, store, chain, tx):
    raw, key = store.create_key("free")
    _, _, r = pay_tempo(tclient, chain, raw, **tx)
    assert_refused(r, "verification_failed")
    assert chain.sent == []  # refused before anything was broadcast
    assert store.get_credit_balance(key.id) == 0 and store.list_credit_purchases(key.id) == []


@pytest.mark.parametrize(
    "log",
    [
        {"amount": PRICE - 1},
        {"recipient": OTHER_ADDRESS},
        {"token": OTHER_TOKEN},
        {"memo": None},
    ],
    ids=["underpays", "wrong-recipient", "wrong-token", "no-memo"],
)
def test_a_pushed_transaction_that_does_not_pay_this_challenge_is_refused(tclient, store, chain, log):
    raw, key = store.create_key("free")
    _, _, r = pay_tempo(tclient, chain, raw, push=True, **log)
    assert_refused(r, "verification_failed")
    assert store.get_credit_balance(key.id) == 0


def test_a_reverted_transaction_is_refused_and_an_unknown_one_unconfirmed(tclient, store, chain):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    # Not on the chain (yet): it may be pending, so the buyer is not asked to pay again.
    unknown = send(tclient, raw, credential(params, {"type": "hash", "hash": "0x" + "12" * 32}))
    assert unknown.status_code == 502 and unknown.json()["error"]["code"] == "payment_unconfirmed"
    chain.receipts["0x" + "34" * 32] = receipt("0x" + "34" * 32, [transfer_log(memo=memo_for(params))], status="0x0")
    reverted = send(tclient, raw, credential(params, {"type": "hash", "hash": "0x" + "34" * 32}))
    assert_refused(reverted, "verification_failed")
    assert store.get_credit_balance(key.id) == 0


def test_a_node_on_another_chain_is_never_trusted(settings, store, fake_renderer, tempo_config, payments):
    """The node answers for the mainnet while the challenge is for the testnet: nothing is credited, and the
    buyer is not asked to pay again (the fault is the server's)."""
    chain = FakeChain(chain_id=tempo.LIVE_CHAIN_ID)
    provider = StripeCredits(tempo_config, client=payments.client, tempo_transport=chain.transport)
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, provider) as c:
        _, _, r = pay_tempo(c, chain, raw, push=True)
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_unconfirmed"
    assert "Tempo network" in r.json()["error"]["message"]
    assert store.get_credit_balance(key.id) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "proof", "signature": "0x" + "11" * 65},
        {"type": "zone", "hash": "0x" + "11" * 32, "senderTag": "0x" + "22" * 32, "zoneId": 1},
        {"type": "hash", "hash": "not-hex"},
        {"type": "transaction", "signature": "0x76c0"},
        {"spt": SPT},
    ],
    ids=["proof", "zone", "bad-hash", "bad-transaction", "card-token"],
)
def test_a_payload_that_is_not_a_tempo_transfer_is_refused(tclient, store, chain, payload):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    r = send(tclient, raw, credential(params, payload))
    assert r.status_code == 402, r.text
    assert r.json()["error"]["code"] in ("malformed_credential", "verification_failed")
    assert chain.sent == [] and store.get_credit_balance(key.id) == 0


# --- the challenge binding, checked before the chain is asked -----------------------------------------------


def test_an_expired_challenge_is_refused_without_asking_the_chain(settings, store, fake_renderer, payments, chain):
    config, _ = CreditsConfig.from_env(TEMPO_ENV)
    provider = StripeCredits(replace(config, expires_s=-1), client=payments.client, tempo_transport=chain.transport)
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, provider) as c:
        _, _, r = pay_tempo(c, chain, raw)
    assert_refused(r, "payment_expired")
    assert chain.calls == [] and store.get_credit_balance(key.id) == 0


def test_a_credential_issued_to_another_key_is_refused(tclient, store, chain):
    raw_a, _ = store.create_key("free")
    raw_b, key_b = store.create_key("free")
    params = tempo_params(ask(tclient, raw_a))
    payload = {"type": "transaction", "signature": signed_transfer(chain, params)}
    r = send(tclient, raw_b, credential(params, payload))
    assert_refused(r, "invalid_challenge")
    assert chain.calls == [] and store.get_credit_balance(key_b.id) == 0


@pytest.mark.parametrize(
    "terms",
    [
        {"amount": "1"},
        {"recipient": OTHER_ADDRESS},
        {"currency": OTHER_TOKEN},
        {"methodDetails": {"chainId": tempo.LIVE_CHAIN_ID}},
    ],
    ids=["amount", "recipient", "token", "chain"],
)
def test_a_credential_with_changed_terms_is_refused(tclient, store, chain, terms):
    """The buyer cannot pick the amount, the recipient, the token or the chain: they are bound to the id."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    request = {**mpp.decode_object(params["request"]), **terms}
    payload = {"type": "hash", "hash": pushed_transfer(chain, params, amount=int(request["amount"]))}
    r = send(tclient, raw, credential(params, payload, request=mpp.encode_object(request)))
    assert_refused(r, "invalid_challenge")
    assert chain.calls == [] and store.get_credit_balance(key.id) == 0


def test_a_tempo_challenge_issued_before_stablecoins_were_turned_off_is_refused(
    settings, store, fake_renderer, tcredits, payments, chain
):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, tcredits) as c:
        params = tempo_params(ask(c, raw))
    config, _ = CreditsConfig.from_env(CREDITS_ENV)  # same secret, cards only
    with make_client(settings, store, fake_renderer, StripeCredits(config, client=payments.client)) as c:
        r = send(c, raw, credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)}))
    assert r.status_code == 402 and r.json()["error"]["code"] == "invalid_challenge"
    assert r.json()["error"]["methods"] == ["stripe"]  # the new challenge offers cards only
    assert chain.calls == [] and store.get_credit_balance(key.id) == 0


# --- when something fails half way --------------------------------------------------------------------------


def test_an_unreachable_node_is_unconfirmed_and_the_same_credential_works_later(tclient, store, chain):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)})
    chain.down = True
    r = send(tclient, raw, header)
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_unconfirmed"
    assert "payment-receipt" not in r.headers and "www-authenticate" not in r.headers  # never "pay again"
    chain.down = False
    assert send(tclient, raw, header).status_code == 200
    assert store.get_credit_balance(key.id) == 5000


class Later:
    """``credits.datetime`` with the clock moved on, so a challenge has expired without waiting for it."""

    def __init__(self, minutes: int = 10):
        self.offset = timedelta(minutes=minutes)

    def now(self, tz=None):
        return datetime.now(tz) + self.offset

    def fromisoformat(self, text):
        return datetime.fromisoformat(text)


def expire_challenges(monkeypatch, minutes: int = 10):
    from calotype import credits as credits_module

    monkeypatch.setattr(credits_module, "datetime", Later(minutes))


def test_a_settled_transfer_whose_credit_failed_is_credited_after_expiry(tclient, store, chain, monkeypatch):
    """FR-26 #1: the transfer is on the chain, then the server fails before the credit is written. The same
    credential sent again after the challenge expired is credited once, and never answered "pay again"."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "transaction", "signature": signed_transfer(chain, params)})
    real = store.credit_tempo_purchase
    monkeypatch.setattr(store, "credit_tempo_purchase", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        send(tclient, raw, header)
    tx_hash = tx_hash_of(chain.sent[0])
    assert store.get_credit_purchase(tx_hash).status == "paid" and store.get_credit_balance(key.id) == 0
    monkeypatch.setattr(store, "credit_tempo_purchase", real)
    expire_challenges(monkeypatch)
    r = send(tclient, raw, header)
    assert r.status_code == 200, r.text
    assert r.json()["already_credited"] is False and r.json()["payment"]["reference"] == tx_hash
    assert store.get_credit_balance(key.id) == 5000 and len(chain.sent) == 1


def lose_the_broadcast(monkeypatch, provider, chain, *, lands: bool):
    """The broadcast's answer is lost (a timeout, a stopped server). ``lands``: the node had it anyway."""

    def broadcast(credential, request, storage):
        raw = credential.payload["signature"]
        if lands:
            tx_hash = tx_hash_of(raw)
            chain.receipts[tx_hash] = receipt(tx_hash, chain.effects.get(tx_hash, []))
        raise tempo.Unconfirmed("PaymentOutcomeUnknownError")

    monkeypatch.setattr(provider._tempo, "broadcast", broadcast)


def test_a_broadcast_with_no_answer_is_pending_and_reconciled_after_expiry(tclient, tcredits, store, chain,
                                                                           monkeypatch):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    signed = signed_transfer(chain, params)
    header = credential(params, {"type": "transaction", "signature": signed})
    lose_the_broadcast(monkeypatch, tcredits, chain, lands=False)
    r = send(tclient, raw, header)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending" and body["payment"]["reference"] == tx_hash_of(signed)
    assert "payment-receipt" not in r.headers and "www-authenticate" not in r.headers
    assert store.get_credit_balance(key.id) == 0

    # It reaches the chain later. The challenge has expired by the time the buyer asks again.
    tx_hash = tx_hash_of(signed)
    chain.receipts[tx_hash] = receipt(tx_hash, chain.effects[tx_hash])
    monkeypatch.undo()
    expire_challenges(monkeypatch)
    again = send(tclient, raw, header)
    assert again.status_code == 200, again.text
    assert again.json()["already_credited"] is False and store.get_credit_balance(key.id) == 5000
    assert chain.count("eth_sendRawTransactionSync") == 0  # reconciled by its hash, never sent afresh


def test_a_transfer_that_can_no_longer_be_included_fails_and_only_then_asks_to_pay_again(
    tclient, tcredits, store, chain, monkeypatch
):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    signed = signed_transfer(chain, params)
    header = credential(params, {"type": "transaction", "signature": signed})
    lose_the_broadcast(monkeypatch, tcredits, chain, lands=False)
    assert send(tclient, raw, header).status_code == 202
    assert send(tclient, raw, header).status_code == 202  # still within valid_before: still pending
    chain.block_time = int(time.time()) + 400  # chain time is past the transaction's valid_before
    r = send(tclient, raw, header)
    body = assert_refused(r, "verification_failed")
    assert "did not pay" in body["detail"]
    assert store.get_credit_purchase(tx_hash_of(signed)).status == "failed"
    assert store.get_credit_balance(key.id) == 0


def test_a_failed_stripe_record_is_pending_and_retried(tclient, store, chain, payments, caplog):
    """FR-26 #2: the coins moved, but credit waits for Stripe's settlement."""
    raw, key = store.create_key("free")
    payments.fail_with = stripe.APIConnectionError("no connection")
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 202, r.text
    tx_hash = r.json()["payment"]["reference"]
    assert store.get_credit_purchase(tx_hash).status == "paid" and store.get_credit_balance(key.id) == 0
    assert "tempo payment not recorded in stripe" in logged(caplog)
    payments.fail_with = None
    again = send(tclient, raw, header)
    assert again.status_code == 200 and again.json()["already_credited"] is False
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1"
    assert store.get_credit_balance(key.id) == 5000
    # The same idempotency key both times: a lost connection is not an answer Stripe kept.
    keys = {c.kwargs["options"]["idempotency_key"] for c in payments.create.call_args_list}
    assert keys == {f"calotype_tempo_{tx_hash}"}


def test_credit_is_not_spendable_until_stripe_settles(tclient, store, chain, payments, fake_renderer):
    raw, key = store.create_key("free")
    payments.crypto_status = "processing"
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 202 and r.json()["credits"] == 0
    assert store.consume_credit(key.id) == (False, 0)
    assert tclient.get("/v1/usage", headers=auth(raw)).json()["credits"] == 0
    payments.settle_crypto("pi_test_1")
    r = send(tclient, raw, header)
    assert r.status_code == 200 and r.json()["credits"] == 5000 and r.json()["screenshots_added"] == 5000
    assert payments.create.call_count == 1  # the linked PaymentIntent was read again, not made again


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"latest_charge": None}, "transaction"),
        ({"amount_received": 100}, "amount"),
        ({"currency": "eur"}, "amount"),
        ({"network": "base"}, "network"),
        ({"transaction_hash": "0x" + "77" * 32}, "transaction"),
    ],
    ids=["no-charge", "amount", "currency", "network", "other-transaction"],
)
def test_a_settlement_that_does_not_match_the_purchase_is_not_credited(tclient, store, chain, payments, caplog,
                                                                      change, problem):
    raw, key = store.create_key("free")
    original = payments._create

    def create(params, options=None):
        made = original(params, options)
        intent = payments.intents[made.id]
        for name in ("network", "transaction_hash"):
            if name in change:
                intent["latest_charge"]["payment_method_details"]["crypto"][name] = change[name]
        intent.update({k: v for k, v in change.items() if k not in ("network", "transaction_hash")})
        return made

    payments.client.v1.payment_intents.create.side_effect = create
    _, _, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 202 and "does not match" in r.json()["message"]
    assert store.get_credit_balance(key.id) == 0
    assert "stripe settlement does not match the tempo purchase" in logged(caplog) and problem in logged(caplog)


def test_a_declined_payment_intent_is_not_credited_and_a_new_one_is_made_only_on_request(
    tclient, store, chain, payments, admin_payments_from
):
    raw, key = store.create_key("free")
    payments.crypto_status = "requires_payment_method"
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 202 and store.get_credit_balance(key.id) == 0
    tx_hash = r.json()["payment"]["reference"]
    assert send(tclient, raw, header).status_code == 202
    assert payments.create.call_count == 1  # a declined PaymentIntent is not silently replaced

    payments.crypto_status = "succeeded"
    admin_payments_from(payments)
    assert admin.main(["record-tempo", tx_hash, "--again"]) == 0
    assert payments.create.call_args.kwargs["options"]["idempotency_key"] == f"calotype_tempo_{tx_hash}_1"
    purchase = store.get_credit_purchase(tx_hash)
    assert (purchase.status, purchase.payment_intent) == ("credited", "pi_test_2")
    assert store.get_credit_balance(key.id) == 5000


@pytest.mark.parametrize("stripe_made_it", [False, True], ids=["nothing-made", "made-before-the-500"])
def test_a_stripe_500_is_reconciled_before_a_new_idempotency_key(tclient, store, chain, payments, stripe_made_it):
    """Stripe's idempotency layer replays a 500 for the same key. List what Stripe made after a while (a list is
    read-after-write; search is not), then use a new key only if it made nothing."""
    raw, key = store.create_key("free")
    original = payments._create

    def failing(params, options=None):
        if stripe_made_it:
            original(params, options)
        raise stripe.APIError("internal", http_status=500)

    payments.client.v1.payment_intents.create.side_effect = failing
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 202
    tx_hash = r.json()["payment"]["reference"]
    purchase = store.get_credit_purchase(tx_hash)
    assert purchase.stripe_attempt == 1 and purchase.stripe_retry_at
    payments.client.v1.payment_intents.create.side_effect = original
    assert send(tclient, raw, header).status_code == 202  # too soon to reconcile: nothing new is made
    assert payments.create.call_count == 1 and not payments.client.v1.payment_intents.list.called

    store.update_credit_purchase(tx_hash, {"stripe_retry_at": datetime.now(UTC).isoformat(timespec="seconds")})
    r = send(tclient, raw, header)
    assert r.status_code == 200, r.text
    assert payments.client.v1.payment_intents.list.called and not payments.client.v1.payment_intents.search.called
    if stripe_made_it:
        assert payments.create.call_count == 1 and len(payments.intents) == 1  # found, not made twice
    else:
        assert payments.create.call_args.kwargs["options"]["idempotency_key"] == f"calotype_tempo_{tx_hash}_1"
    assert store.get_credit_balance(key.id) == 5000


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        [{**DEPOSIT_ADDRESS, "address": OTHER_ADDRESS}],
        [{**DEPOSIT_ADDRESS, "supported_tokens": [{"token_currency": "usdc", "token_contract_address": OTHER_TOKEN}]}],
        [{**DEPOSIT_ADDRESS, "livemode": True}],
    ],
    ids=["none", "another-address", "another-token", "live-address"],
)
def test_a_deposit_address_this_stripe_account_does_not_hold_is_never_offered(
    settings, store, fake_renderer, tempo_config, chain, addresses, capsys
):
    payments = FakePayments()
    payments.deposit_addresses = addresses
    provider = StripeCredits(tempo_config, client=payments.client, tempo_transport=chain.transport)
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, provider) as c:
        r = ask(c, raw)
        assert r.json()["error"]["methods"] == ["stripe"]
    assert "tempo deposit address not verified" in capsys.readouterr().out
    assert not provider.tempo_ready()  # asked again only after five minutes


def test_a_tempo_credential_is_refused_while_the_address_is_unverified(tclient, tcredits, store, chain, payments):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    tcredits._tempo_verified, tcredits._tempo_next_check = False, time.monotonic() + 300
    r = send(tclient, raw, credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)}))
    assert r.status_code == 402 and r.json()["error"]["methods"] == ["stripe"]
    assert store.list_credit_purchases(key.id) == []


# --- one challenge, one transfer (FR-26 #4) ----------------------------------------------------------------


def test_a_second_signed_transfer_for_a_paid_challenge_is_not_sent(tclient, store, chain):
    raw, key = store.create_key("free")
    params, _, first = pay_tempo(tclient, chain, raw)
    assert first.status_code == 200
    second = credential(params, {"type": "transaction", "signature": signed_transfer(chain, params, nonce=1)})
    r = send(tclient, raw, second)
    assert r.status_code == 409 and r.json()["error"]["code"] == "payment_conflict"
    assert "nothing moved" in r.json()["error"]["message"]
    assert len(chain.sent) == 1 and store.get_credit_balance(key.id) == 5000


def test_two_signed_transfers_for_one_challenge_at_once_only_one_is_sent(tclient, store, chain):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    headers = [
        credential(params, {"type": "transaction", "signature": signed_transfer(chain, params, nonce=n)})
        for n in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda h: send(tclient, raw, h), headers))
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409], [r.text for r in results]
    assert len(chain.sent) == 1 and store.get_credit_balance(key.id) == 5000
    assert len(store.list_credit_purchases(key.id)) == 1


def test_a_second_pushed_transfer_is_recorded_unmatched_and_can_be_refunded(
    tclient, store, chain, payments, admin_payments_from, capsys
):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    first = send(tclient, raw, credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)}))
    assert first.status_code == 200
    other = pushed_transfer(chain, params, tx_hash="0x" + "cd" * 32)  # the buyer paid the same challenge twice
    r = send(tclient, raw, credential(params, {"type": "hash", "hash": other}))
    assert r.status_code == 409 and "recorded so that it can be refunded" in r.json()["error"]["message"]
    purchase = store.get_credit_purchase(other)
    assert (purchase.status, purchase.challenge_id) == ("unmatched", "")
    assert store.get_credit_balance(key.id) == 5000
    # The retry of the first credential still answers with the first transfer.
    again = send(tclient, raw, credential(params, {"type": "hash", "hash": "0x" + "ab" * 32}))
    assert again.status_code == 200 and again.json()["payment"]["reference"] == "0x" + "ab" * 32

    admin_payments_from(payments)
    assert admin.main(["record-tempo", other]) == 0
    assert admin.main(["refund-credit", other, "--yes", "--prorated"]) == 0
    refund = payments.refunds[-1].kwargs["params"]
    assert refund["amount"] == 1900 and refund["payment_intent"] == store.get_credit_purchase(other).payment_intent
    assert store.get_credit_balance(key.id) == 5000  # the first purchase's credit is untouched
    assert "no credit is taken back" not in capsys.readouterr().err


def test_claims_belong_to_one_attempt(store):
    import asyncio

    first, second = tempo.ClaimStore(store, "ch_1"), tempo.ClaimStore(store, "ch_1")

    async def run():
        assert await first.put_if_absent("mpp:charge:0xab", "0xab") is True
        assert await second.put_if_absent("mpp:charge:0xab", "0xab") is False  # the same challenge, another attempt
        assert await second.get("mpp:charge:0xab") == "0xab" and await first.get("mpp:charge:0xab") is None
        await second.delete("mpp:charge:0xab")  # a failing attempt releases only its own claim
        assert store.get_payment_claim("mpp:charge:0xab") is not None

    asyncio.run(run())


def test_settling_on_an_event_loop_is_refused(tcredits):
    import asyncio

    async def on_the_loop():
        with pytest.raises(RuntimeError, match="worker thread"):
            tcredits._tempo.lookup("0x" + "ab" * 32, {}, None)

    asyncio.run(on_the_loop())


# --- the webhook ---------------------------------------------------------------------------------------------


def test_the_webhook_links_the_payment_intent_and_credits_nothing_twice(
    settings, store, fake_renderer, tcredits, billing, chain, payments
):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        _, _, r = pay_tempo(c, chain, raw)
        tx_hash = r.json()["payment"]["reference"]
        hook = post_event(c, event("evt_tempo_1", "payment_intent.succeeded", payments.intents["pi_test_1"]))
    assert hook.status_code == 200 and hook.json()["outcome"] == "already_credited"
    assert store.get_credit_balance(key.id) == 5000
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1"


def tempo_intent(key_id: int, tx_hash: str, intent_id: str = "pi_hook_tempo", charged: str | None = None) -> dict:
    """A succeeded crypto PaymentIntent, as Stripe returns it. ``charged``: the transaction Stripe verified."""
    return {
        "id": intent_id, "object": "payment_intent", "status": "succeeded", "amount": 1900,
        "amount_received": 1900, "currency": "usd", "payment_method_types": ["crypto"],
        "latest_charge": crypto_charge(charged or tx_hash),
        "metadata": {"calotype_key_id": str(key_id), "calotype_credit_pack": "starter",
                     "calotype_credit_screenshots": "5000", "calotype_tempo_tx": tx_hash, "mpp_challenge_id": "ch"},
    }


def test_a_webhook_with_tempo_metadata_alone_credits_nothing(settings, store, fake_renderer, tcredits, billing,
                                                             payments):
    """FR-26 #3: a succeeded PaymentIntent whose metadata names a key, a pack and a transaction this server never
    bound creates no purchase."""
    raw, key = store.create_key("free")
    tx_hash = "0x" + "cd" * 32
    payments.intents["pi_hook_tempo"] = tempo_intent(key.id, tx_hash)
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        hook = post_event(c, event("evt_tempo_2", "payment_intent.succeeded", payments.intents["pi_hook_tempo"]))
    assert hook.json()["outcome"] == "ignored"
    assert store.get_credit_purchase(tx_hash) is None and store.get_credit_balance(key.id) == 0


def test_a_webhook_for_another_transaction_cannot_link_or_credit_a_purchase(
    settings, store, fake_renderer, tcredits, billing, chain, payments
):
    """The metadata names our purchase, but the transaction Stripe verified is another one."""
    raw, key = store.create_key("free")
    payments.fail_with = stripe.APIConnectionError("down")  # recording fails: the purchase is paid, unlinked
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        _, header, r = pay_tempo(c, chain, raw)
        tx_hash = r.json()["payment"]["reference"]
        payments.intents["pi_wrong"] = tempo_intent(key.id, tx_hash, "pi_wrong", charged="0x" + "77" * 32)
        wrong = post_event(c, event("evt_wrong", "payment_intent.succeeded", payments.intents["pi_wrong"]))
        assert wrong.json()["outcome"] == "ignored"
        assert store.get_credit_purchase(tx_hash).payment_intent is None and store.get_credit_balance(key.id) == 0

        payments.intents["pi_right"] = tempo_intent(key.id, tx_hash, "pi_right")
        right = post_event(c, event("evt_right", "payment_intent.succeeded", payments.intents["pi_right"]))
        assert right.json()["outcome"] == "credited"
        payments.fail_with = None
        assert send(c, raw, header).json()["already_credited"] is True
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_right"
    assert store.get_credit_balance(key.id) == 5000 and payments.create.call_count == 1


def test_the_webhook_credits_a_payment_stripe_settled_after_the_request(
    settings, store, fake_renderer, tcredits, billing, chain, payments
):
    raw, key = store.create_key("free")
    payments.crypto_status = "processing"
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        _, header, r = pay_tempo(c, chain, raw)
        assert r.status_code == 202
        payments.settle_crypto("pi_test_1")
        hook = post_event(c, event("evt_late", "payment_intent.succeeded", payments.intents["pi_test_1"]))
        assert hook.json()["outcome"] == "credited"
        again = send(c, raw, header)
    assert again.status_code == 200 and again.json()["already_credited"] is True
    assert store.get_credit_balance(key.id) == 5000


def test_the_webhook_ignores_a_bad_transaction_hash(settings, store, fake_renderer, tcredits, billing, payments):
    raw, key = store.create_key("free")
    payments.intents["pi_bad"] = tempo_intent(key.id, "0x/../x", "pi_bad")
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        hook = post_event(c, event("evt_bad", "payment_intent.succeeded", payments.intents["pi_bad"]))
    assert hook.json()["outcome"] == "ignored" and store.get_credit_balance(key.id) == 0


# --- refunds and the admin CLI -------------------------------------------------------------------------------


@pytest.fixture
def admin_payments(monkeypatch) -> FakePayments:
    fake = FakePayments()
    monkeypatch.setattr(admin, "open_credits", lambda: credits_for_admin({"STRIPE_SECRET_KEY": SECRET_KEY}, client=fake.client))
    return fake


@pytest.fixture
def admin_payments_from(monkeypatch, tcredits, store):
    """The admin CLI over the app's own provider (its Stripe stand-in and its chain) and the app's store."""

    def use(payments):
        monkeypatch.setattr(admin, "open_credits", lambda: tcredits)
        monkeypatch.setattr(admin, "open_configured_storage", lambda settings: store)

    return use


TX = "0x" + "ef" * 32


def bought_in_usdc(store, payment_intent: str | None = "pi_tempo_1", payments: FakePayments | None = None):
    """A credited Tempo purchase. With ``payments``, Stripe holds its settled PaymentIntent."""
    _, key = store.create_key("free")
    store.add_credit_purchase(
        TX, key.id, pack="starter", screenshots=5000, amount_cents=1900, currency="usd", challenge_id="ch",
        method="tempo", payment_intent=payment_intent,
    )
    if payments is not None and payment_intent:
        payments.intents[payment_intent] = tempo_intent(key.id, TX, payment_intent)
    return key


def test_a_tempo_purchase_is_refunded_through_its_payment_intent(admin_store, admin_payments, capsys):
    key = bought_in_usdc(admin_store, payments=admin_payments)
    assert admin.main(["refund-credit", TX.upper().replace("0X", "0x"), "--yes"]) == 0
    refund = admin_payments.refunds[0].kwargs
    assert refund["params"]["payment_intent"] == "pi_tempo_1" and refund["params"]["amount"] == 1900
    assert refund["options"] == {"idempotency_key": f"calotype_credit_refund_{TX}"}
    assert admin_store.get_credit_balance(key.id) == 0
    assert admin_store.get_credit_purchase(TX).refund_id.startswith("re_test_")
    err = capsys.readouterr().err
    assert "stablecoins to the wallet that sent it" in err


def test_a_tempo_purchase_stripe_has_no_record_of_is_not_refunded(admin_store, admin_payments, capsys):
    key = bought_in_usdc(admin_store, payment_intent=None)
    assert admin.main(["refund-credit", TX, "--yes"]) == 1
    assert "record-tempo" in capsys.readouterr().err
    assert admin_payments.refunds == []
    assert admin_store.get_credit_balance(key.id) == 5000  # nothing was changed
    assert admin_store.get_credit_purchase(TX).refunded_at is None


def test_record_tempo_records_a_payment_stripe_missed(admin_store, admin_payments, capsys):
    bought_in_usdc(admin_store, payment_intent=None)
    assert admin.main(["credits", str(admin_store.get_credit_purchase(TX).key_id)]) == 0
    assert "payment_intent=NONE (run record-tempo)" in capsys.readouterr().out
    assert admin.main(["record-tempo", TX]) == 0
    params = admin_payments.create.call_args.kwargs["params"]
    assert params["payment_method_options"]["crypto"]["transaction_verification_options"]["transaction_hash"] == TX
    assert admin_store.get_credit_purchase(TX).payment_intent == "pi_test_1"
    assert admin.main(["record-tempo", TX]) == 0  # already recorded: nothing more
    assert admin_payments.create.call_count == 1
    assert admin.main(["record-tempo", "0x" + "00" * 32]) == 1


def test_the_deposit_address_command_lists_and_creates(admin_payments, capsys):
    made = {"id": "cda_test_1", "object": "crypto.deposit_address", "livemode": False, "network": "tempo",
            "address": DEPOSIT, "supported_tokens": [{"token_currency": "usdc", "token_contract_address": TOKEN}]}
    client = admin_payments.client
    client.raw_request.side_effect = lambda method, path, **params: (method, path, params)
    client.deserialize.side_effect = lambda answer, api_mode: (
        {"object": "list", "data": [made]} if answer[0] == "get" else made
    )
    assert admin.main(["tempo-deposit-address"]) == 0
    out = capsys.readouterr().out
    assert DEPOSIT in out and f"usdc:{TOKEN}" in out
    assert client.raw_request.call_args.kwargs == {"network": "tempo", "stripe_version": "2026-07-29.preview"}
    assert admin.main(["tempo-deposit-address", "--create"]) == 0
    assert client.raw_request.call_args.args == ("post", "/v1/crypto/deposit_addresses")
    client.raw_request.side_effect = stripe.PermissionError("not enabled")
    assert admin.main(["tempo-deposit-address"]) == 1


def test_usage_shows_tempo_credit_like_any_other(tclient, store, chain):
    raw, key = store.create_key("free")
    pay_tempo(tclient, chain, raw)
    usage = tclient.get("/v1/usage", headers=auth(raw)).json()
    assert usage["credits"] == 5000 and usage["month"] == current_month()


def test_challenge_expiry_is_five_minutes(tclient, store):
    raw, _ = store.create_key("free")
    expires = datetime.fromisoformat(tempo_params(ask(tclient, raw))["expires"].replace("Z", "+00:00"))
    assert timedelta(minutes=4) < expires - datetime.now(UTC) <= timedelta(minutes=5)


# --- the site says what this server offers ---------------------------------------------------------------


def site_text(c) -> dict[str, str]:
    from calotype.pages import PAGES

    texts = {p.path: c.get(p.path).text for p in PAGES}
    texts["/llms.txt"] = c.get("/llms.txt").text
    return texts


def test_with_usdc_on_the_pages_describe_it(settings, store, fake_renderer, tcredits):
    with make_client(settings, store, fake_renderer, tcredits, serve_site=True) as c:
        texts = site_text(c)
    for path, text in texts.items():
        assert "<!--usdc" not in text and "<!--nousdc" not in text and "<!--/usdc" not in text, path
    assert 'method="tempo"' in texts["/docs"] and "Stablecoin payment is not offered" not in texts["/docs"]
    assert "USDC on Tempo" in texts["/pricing"] and "Tempo network" in texts["/agents"]
    assert "method `tempo`" in texts["/llms.txt"]
    assert "USDC.e on the Tempo network" in texts["/terms"]
    assert "to the wallet address the payment came from" in texts["/refunds"]
    assert "transaction hash" in texts["/privacy"] and "Tempo network, which is public" in texts["/privacy"]


def test_with_usdc_off_the_pages_do_not_offer_it(settings, store, fake_renderer, payments):
    config, _ = CreditsConfig.from_env(CREDITS_ENV)
    with make_client(settings, store, fake_renderer, StripeCredits(config, client=payments.client),
                     serve_site=True) as c:
        texts = site_text(c)
    for path, text in texts.items():
        assert "<!--usdc" not in text and "<!--nousdc" not in text, path
        assert "USDC" not in text and "Tempo" not in text, path
    assert "Stablecoin payment is not offered on this server." in texts["/docs"]


# --- FR-26 re-check #1: a transfer the buyer broadcast is never stranded at expiry -------------------------------


def test_a_pushed_hash_sent_before_its_receipt_was_visible_is_credited_after_expiry(tclient, store, chain, monkeypatch):
    """The hash came before the node had its receipt (502, nothing bound). The transfer settles; the same
    credential, sent after the challenge expired, is looked up by its hash and credited, never a fresh 402."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    tx_hash = "0x" + "a1" * 32
    header = credential(params, {"type": "hash", "hash": tx_hash})
    early = send(tclient, raw, header)
    assert early.status_code == 502 and early.json()["error"]["code"] == "payment_unconfirmed"
    assert store.list_credit_purchases(key.id) == []

    pushed_transfer(chain, params, tx_hash=tx_hash)  # now on the chain
    expire_challenges(monkeypatch)
    r = send(tclient, raw, header)
    assert r.status_code == 200, r.text
    assert r.json()["already_credited"] is False and r.json()["payment"]["reference"] == tx_hash
    purchase = store.get_credit_purchase(tx_hash)
    assert (purchase.status, purchase.challenge_id, purchase.amount_cents) == ("credited", params["id"], 1900)
    assert store.get_credit_balance(key.id) == 5000 and chain.sent == []
    assert send(tclient, raw, header).json()["already_credited"] is True  # and once only


def test_a_pushed_hash_whose_binding_failed_is_credited_after_expiry(tclient, store, chain, monkeypatch):
    """The chain check passed, then storage failed before the challenge was bound."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)})
    real = store.bind_tempo_payment
    monkeypatch.setattr(store, "bind_tempo_payment", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        send(tclient, raw, header)
    assert store.list_credit_purchases(key.id) == []
    monkeypatch.setattr(store, "bind_tempo_payment", real)
    expire_challenges(monkeypatch)
    r = send(tclient, raw, header)
    assert r.status_code == 200, r.text
    assert store.get_credit_balance(key.id) == 5000


def test_a_pushed_hash_first_sent_after_expiry_is_credited(tclient, store, chain, payments, monkeypatch):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)})
    expire_challenges(monkeypatch)
    r = send(tclient, raw, header)
    assert r.status_code == 200, r.text
    assert store.get_credit_balance(key.id) == 5000 and payments.create.call_count == 1


def test_after_expiry_a_pushed_hash_not_on_the_chain_is_unconfirmed_and_bound_to_nothing(
    tclient, store, chain, monkeypatch
):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    expire_challenges(monkeypatch)
    r = send(tclient, raw, credential(params, {"type": "hash", "hash": "0x" + "a2" * 32}))
    assert r.status_code == 502 and r.json()["error"]["code"] == "payment_unconfirmed"
    assert "www-authenticate" not in r.headers  # not "pay again": it may still land
    assert store.list_credit_purchases(key.id) == [] and chain.sent == []


@pytest.mark.parametrize(
    "log",
    [{"memo": "0x" + "00" * 31 + "01"}, {"amount": PRICE - 1}, {"recipient": OTHER_ADDRESS}],
    ids=["foreign-memo", "underpays", "wrong-recipient"],
)
def test_after_expiry_a_pushed_hash_that_does_not_pay_the_challenge_is_refused(tclient, store, chain, monkeypatch,
                                                                              log):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params, **log)})
    expire_challenges(monkeypatch)
    assert_refused(send(tclient, raw, header), "verification_failed")
    assert store.list_credit_purchases(key.id) == [] and store.get_credit_balance(key.id) == 0


def test_after_expiry_a_second_pushed_hash_takes_nothing_from_the_first(tclient, store, chain, monkeypatch):
    """Two transfers for one challenge, both first sent after expiry: the first binds, the second is unmatched."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    first = pushed_transfer(chain, params)
    second = pushed_transfer(chain, params, tx_hash="0x" + "cd" * 32)
    expire_challenges(monkeypatch)
    assert send(tclient, raw, credential(params, {"type": "hash", "hash": first})).status_code == 200
    r = send(tclient, raw, credential(params, {"type": "hash", "hash": second}))
    assert r.status_code == 409 and store.get_credit_purchase(second).status == "unmatched"
    assert store.get_credit_balance(key.id) == 5000


def test_after_expiry_a_signed_transfer_never_bound_is_refused_and_never_sent(tclient, store, chain, monkeypatch):
    """What the README promises: a signed transfer this server never bound was never sent by it, so nothing moved;
    after expiry it is refused (402 payment_expired) rather than broadcast."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "transaction", "signature": signed_transfer(chain, params)})
    expire_challenges(monkeypatch)
    assert_refused(send(tclient, raw, header), "payment_expired")
    assert chain.sent == [] and chain.calls == [] and store.list_credit_purchases(key.id) == []


# --- FR-26 re-check #2: the admin CLI's own provider can move every purchase on -----------------------------------


@pytest.fixture
def cli_provider(monkeypatch, store, payments, chain):
    """The admin CLI as deployed: ``admin.open_credits`` is ``credits_for_admin`` reading the environment. Only
    the network is replaced: Stripe's client by ``payments``, the Tempo node by ``chain``."""
    from calotype import credits as credits_module

    monkeypatch.setenv("STRIPE_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("STRIPE_TEMPO_DEPOSIT_ADDRESS", DEPOSIT)
    monkeypatch.delenv("CALOTYPE_TEMPO_RPC_URL", raising=False)
    monkeypatch.setattr(stripe, "StripeClient", lambda *a, **k: payments.client)
    real_init = tempo.TempoMethod.__init__
    monkeypatch.setattr(
        tempo.TempoMethod, "__init__", lambda self, config, transport=None: real_init(self, config, transport=chain.transport)
    )
    monkeypatch.setattr(admin, "open_credits", credits_module.credits_for_admin)
    monkeypatch.setattr(admin, "open_configured_storage", lambda settings: store)


def test_record_tempo_moves_a_pending_purchase_on_with_the_clis_own_provider(
    tclient, tcredits, store, chain, payments, monkeypatch, cli_provider
):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    signed = signed_transfer(chain, params)
    lose_the_broadcast(monkeypatch, tcredits, chain, lands=False)
    assert send(tclient, raw, credential(params, {"type": "transaction", "signature": signed})).status_code == 202
    tx_hash = tx_hash_of(signed)
    assert store.get_credit_purchase(tx_hash).status == "pending"
    chain.receipts[tx_hash] = receipt(tx_hash, chain.effects[tx_hash])  # it landed after all
    assert admin.main(["record-tempo", tx_hash]) == 0
    purchase = store.get_credit_purchase(tx_hash)
    assert (purchase.status, purchase.payment_intent) == ("credited", "pi_test_1")
    assert store.get_credit_balance(key.id) == 5000 and chain.count("eth_sendRawTransactionSync") == 0


def test_record_tempo_moves_a_paid_purchase_on_with_the_clis_own_provider(tclient, store, chain, payments,
                                                                          cli_provider):
    raw, key = store.create_key("free")
    payments.fail_with = stripe.APIConnectionError("no connection")
    _, _, r = pay_tempo(tclient, chain, raw)
    tx_hash = r.json()["payment"]["reference"]
    assert store.get_credit_purchase(tx_hash).status == "paid"
    payments.fail_with = None
    assert admin.main(["record-tempo", tx_hash]) == 0
    assert store.get_credit_purchase(tx_hash).status == "credited" and store.get_credit_balance(key.id) == 5000


def test_record_tempo_again_replaces_a_declined_payment_intent_with_the_clis_own_provider(
    tclient, store, chain, payments, cli_provider
):
    raw, key = store.create_key("free")
    payments.crypto_status = "requires_payment_method"
    _, _, r = pay_tempo(tclient, chain, raw)
    tx_hash = r.json()["payment"]["reference"]
    payments.crypto_status = "succeeded"
    assert admin.main(["record-tempo", tx_hash]) == 1  # still declined: not replaced without --again
    assert admin.main(["record-tempo", tx_hash, "--again"]) == 0
    assert payments.create.call_args.kwargs["options"]["idempotency_key"] == f"calotype_tempo_{tx_hash}_1"
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_2"
    assert store.get_credit_balance(key.id) == 5000


def test_record_tempo_without_tempo_settings_says_so_and_changes_nothing(
    tclient, tcredits, store, chain, payments, monkeypatch, cli_provider, capsys
):
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    signed = signed_transfer(chain, params)
    lose_the_broadcast(monkeypatch, tcredits, chain, lands=False)
    assert send(tclient, raw, credential(params, {"type": "transaction", "signature": signed})).status_code == 202
    monkeypatch.delenv("STRIPE_TEMPO_DEPOSIT_ADDRESS")
    capsys.readouterr()
    assert admin.main(["record-tempo", tx_hash_of(signed)]) == 1
    err = capsys.readouterr().err
    assert "STRIPE_TEMPO_DEPOSIT_ADDRESS" in err and "Nothing was changed" in err
    assert store.get_credit_purchase(tx_hash_of(signed)).status == "pending" and payments.create.call_count == 0


# --- FR-26 re-check #3: a webhook that cannot finish is not acknowledged ------------------------------------------


@pytest.mark.parametrize("fault", ["chain-down", "stripe-down"])
def test_a_webhook_that_cannot_advance_its_purchase_is_retried_and_its_redelivery_credits(
    settings, store, fake_renderer, tcredits, billing, chain, payments, monkeypatch, fault
):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        params = tempo_params(ask(c, raw))
        signed = signed_transfer(chain, params)
        tx_hash = tx_hash_of(signed)
        if fault == "chain-down":
            # The broadcast's receipt was lost (the transfer landed); Stripe settles a PaymentIntent for it; the
            # node is down when the event arrives.
            lose_the_broadcast(monkeypatch, tcredits, chain, lands=False)
            assert send(c, raw, credential(params, {"type": "transaction", "signature": signed})).status_code == 202
            chain.receipts[tx_hash] = receipt(tx_hash, chain.effects[tx_hash])  # it landed after all
            payments.intents["pi_hook_tempo"] = tempo_intent(key.id, tx_hash)
            intent_id = "pi_hook_tempo"
            chain.down = True
        else:
            # Stripe settles after the request answered 202; reading the PaymentIntent again fails once.
            payments.crypto_status = "processing"
            assert send(c, raw, credential(params, {"type": "transaction", "signature": signed})).status_code == 202
            payments.settle_crypto("pi_test_1")
            intent_id, calls = "pi_test_1", []
            real = payments._retrieve

            def flaky(intent, params=None):
                calls.append(intent)
                if len(calls) == 2:
                    raise stripe.APIConnectionError("no connection")
                return real(intent, params)

            payments.client.v1.payment_intents.retrieve.side_effect = flaky
        hook = event("evt_retry_me", "payment_intent.succeeded", payments.intents[intent_id])
        first = post_event(c, hook)
        assert first.status_code == 500, first.text  # not acknowledged: Stripe delivers it again
        assert not store.event_processed("evt_retry_me") and store.get_credit_balance(key.id) == 0
        chain.down = False
        again = post_event(c, hook)
        assert again.status_code == 200 and again.json()["outcome"] == "credited"
        assert post_event(c, hook).json()["outcome"] == "duplicate"
    assert store.get_credit_balance(key.id) == 5000


def test_a_webhook_for_a_purchase_refunded_before_settlement_is_acknowledged(
    settings, store, fake_renderer, tcredits, billing, chain, payments, admin_payments_from
):
    raw, key = store.create_key("free")
    payments.crypto_status = "processing"
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        _, _, r = pay_tempo(c, chain, raw)
        tx_hash = r.json()["payment"]["reference"]
        store.refund_credit_purchase(tx_hash)
        payments.settle_crypto("pi_test_1")
        hook = post_event(c, event("evt_refunded", "payment_intent.succeeded", payments.intents["pi_test_1"]))
    assert hook.status_code == 200 and hook.json()["outcome"] == "ignored"
    assert store.get_credit_balance(key.id) == 0


# --- FR-26 re-check #4: the PaymentIntent that settled the transfer is the one that counts ------------------------


def test_a_stripe_500_whose_payment_intent_search_cannot_see_yet_is_not_replaced(tclient, store, chain, payments):
    """Stripe made the PaymentIntent and answered 500; its search index has not caught up. A list sees it."""
    raw, key = store.create_key("free")
    original = payments._create

    def failing(params, options=None):
        original(params, options)
        raise stripe.APIError("internal", http_status=500)

    payments.client.v1.payment_intents.create.side_effect = failing
    payments.client.v1.payment_intents.search.side_effect = lambda params: {"object": "search_result", "data": []}
    _, header, r = pay_tempo(tclient, chain, raw)
    tx_hash = r.json()["payment"]["reference"]
    payments.client.v1.payment_intents.create.side_effect = original
    store.update_credit_purchase(tx_hash, {"stripe_retry_at": datetime.now(UTC).isoformat(timespec="seconds")})
    assert send(tclient, raw, header).status_code == 200
    assert payments.create.call_count == 1 and list(payments.intents) == ["pi_test_1"]
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1"


def test_a_late_settlement_of_an_unknown_attempt_takes_the_place_of_its_declined_replacement(
    settings, store, fake_renderer, tcredits, billing, chain, payments, capsys
):
    """The first recording got a 500 and Stripe made nothing visible; a replacement (key _1) was made. Then Stripe
    finished the first one after all: it settles the transfer, and the replacement is declined (one transfer
    settles once). The first one's success event credits the purchase instead of being ignored."""
    raw, key = store.create_key("free")
    original = payments._create
    payments.client.v1.payment_intents.create.side_effect = lambda params, options=None: (_ for _ in ()).throw(
        stripe.APIError("gateway timeout", http_status=504)
    )
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        params = tempo_params(ask(c, raw))
        header = credential(params, {"type": "transaction", "signature": signed_transfer(chain, params)})
        r = send(c, raw, header)
        tx_hash = r.json()["payment"]["reference"]
        payments.client.v1.payment_intents.create.side_effect = original
        payments.crypto_status = "processing"
        store.update_credit_purchase(tx_hash, {"stripe_retry_at": datetime.now(UTC).isoformat(timespec="seconds")})
        assert send(c, raw, header).status_code == 202  # nothing listed: the replacement is made
        assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1"

        payments.intents["pi_late"] = tempo_intent(key.id, tx_hash, "pi_late")  # the first attempt, settled
        payments.settle_crypto("pi_test_1", "canceled")
        hook = post_event(c, event("evt_late_first", "payment_intent.succeeded", payments.intents["pi_late"]))
        assert hook.status_code == 200 and hook.json()["outcome"] == "credited"
        purchase = store.get_credit_purchase(tx_hash)
        assert (purchase.status, purchase.payment_intent) == ("credited", "pi_late")

        # Had the replacement settled too, its event would credit nothing more, and say so.
        payments.settle_crypto("pi_test_1")
        dup = post_event(c, event("evt_second_settlement", "payment_intent.succeeded", payments.intents["pi_test_1"]))
        assert dup.json()["outcome"] == "ignored"
    assert store.get_credit_balance(key.id) == 5000
    assert "a second stripe settlement of a tempo transaction" in capsys.readouterr().out


def test_a_webhook_does_not_replace_a_payment_intent_that_settled_the_transfer(
    settings, store, fake_renderer, tcredits, billing, chain, payments
):
    raw, key = store.create_key("free")
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        _, _, r = pay_tempo(c, chain, raw)
        tx_hash = r.json()["payment"]["reference"]
        payments.intents["pi_other"] = tempo_intent(key.id, tx_hash, "pi_other")
        hook = post_event(c, event("evt_other", "payment_intent.succeeded", payments.intents["pi_other"]))
    assert hook.json()["outcome"] == "ignored"
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1" and store.get_credit_balance(key.id) == 5000


@pytest.mark.parametrize("status", ["processing", "requires_payment_method"])
def test_a_tempo_refund_goes_only_through_a_payment_intent_that_settled_the_transfer(
    tclient, store, chain, payments, admin_payments_from, capsys, status
):
    raw, key = store.create_key("free")
    payments.crypto_status = status
    _, _, r = pay_tempo(tclient, chain, raw)
    tx_hash = r.json()["payment"]["reference"]
    admin_payments_from(payments)
    capsys.readouterr()
    assert admin.main(["refund-credit", tx_hash, "--yes"]) == 1
    assert "is not Stripe's settlement" in capsys.readouterr().err
    assert payments.refunds == [] and store.get_credit_purchase(tx_hash).refunded_at is None  # nothing changed


# --- lower: the pinned SDK's private calls, and the deposit address checked again ---------------------------------


def test_the_sdk_is_the_version_its_private_calls_were_read_in():
    from importlib.metadata import version
    from pathlib import Path

    assert version("pympp") == tempo.PYMPP_VERSION == "0.26.0"
    pins = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    assert f"pympp=={tempo.PYMPP_VERSION}" in pins.splitlines()


LOOKUP_PARAMS = {"id": "ch_lookup", "realm": "testserver"}


def lookup_terms(config, amount_cents: int = 1900) -> dict:
    return {"request": tempo.charge_request(config, amount_cents), "realm": "testserver", "challenge_id": "ch_lookup"}


@pytest.mark.parametrize(
    "receipt_status, log, expected",
    [
        ("0x1", {}, tempo.CHAIN_PAID),
        ("0x0", {}, tempo.CHAIN_FAILED),  # reverted
        ("0x1", {"memo": "0x" + "00" * 31 + "01"}, tempo.CHAIN_FAILED),  # another challenge's memo
        ("0x1", {"amount": PRICE - 1}, tempo.CHAIN_FAILED),
        ("0x1", {"recipient": OTHER_ADDRESS}, tempo.CHAIN_FAILED),
        ("0x1", {"token": OTHER_TOKEN}, tempo.CHAIN_FAILED),
    ],
    ids=["paid", "reverted", "foreign-memo", "underpays", "wrong-recipient", "wrong-token"],
)
def test_lookup_checks_a_receipt_with_the_pinned_sdk(tempo_config, chain, receipt_status, log, expected):
    method = tempo.TempoMethod(tempo_config.tempo, transport=chain.transport)
    tx_hash = "0x" + "b1" * 32
    memo = log.pop("memo", memo_for(LOOKUP_PARAMS))
    chain.receipts[tx_hash] = receipt(tx_hash, [transfer_log(memo=memo, **log)], status=receipt_status)
    assert method.lookup(tx_hash, lookup_terms(tempo_config.tempo), None) == expected
    assert chain.sent == []  # a lookup never sends


def test_lookup_without_a_receipt_is_pending_until_chain_time_passes_valid_before(tempo_config, chain):
    method = tempo.TempoMethod(tempo_config.tempo, transport=chain.transport)
    tx_hash, now = "0x" + "b2" * 32, int(time.time())
    terms = lookup_terms(tempo_config.tempo)
    assert method.lookup(tx_hash, terms, None) == tempo.CHAIN_PENDING  # pushed: no valid_before known
    assert method.lookup(tx_hash, terms, now + 300) == tempo.CHAIN_PENDING
    chain.block_time = now + 301
    assert method.lookup(tx_hash, terms, now + 300) == tempo.CHAIN_FAILED


def test_lookup_ignores_the_challenges_expiry_and_refuses_a_node_on_another_chain(tempo_config):
    """The receipt check has no expiry in it: terms of a challenge that expired long ago still look up. A node
    on another chain is never answered for."""
    method = tempo.TempoMethod(tempo_config.tempo, transport=FakeChain(chain_id=tempo.LIVE_CHAIN_ID).transport)
    with pytest.raises(tempo.Unconfirmed):
        method.lookup("0x" + "b3" * 32, lookup_terms(tempo_config.tempo), None)
    chain = FakeChain()
    method = tempo.TempoMethod(tempo_config.tempo, transport=chain.transport)
    pushed_transfer(chain, LOOKUP_PARAMS, tx_hash="0x" + "b4" * 32)
    assert method.lookup("0x" + "b4" * 32, lookup_terms(tempo_config.tempo), None) == tempo.CHAIN_PAID


def test_the_pinned_sdk_refuses_an_expired_challenge_and_names_the_hash_we_compute(tempo_config, chain):
    """``check`` relies on the SDK's validate for expiry; ``credential_transaction_hash`` on its private hash
    functions. Both against the pinned version."""
    method = tempo.TempoMethod(tempo_config.tempo, transport=chain.transport)
    request = tempo.charge_request(tempo_config.tempo, 1900)
    params = {"id": "ch_sdk", "realm": "testserver", "method": "tempo", "intent": "charge",
              "request": mpp.encode_object(request), "expires": mpp.expires_at(seconds=300)}
    signed = signed_transfer(chain, params)
    fresh = mpp.parse_credential(credential(params, {"type": "transaction", "signature": signed}))
    checked = method.check(fresh, request)
    assert checked.transaction_hash == tempo.credential_transaction_hash(fresh) == tx_hash_of(signed)
    assert checked.mode == "pull" and checked.valid_before is not None
    stale = mpp.parse_credential(credential({**params, "expires": "2020-01-01T00:00:00Z"},
                                            {"type": "transaction", "signature": signed}))
    with pytest.raises(tempo.Refused) as refused:
        method.check(stale, request)
    # pympp 0.26.0 raises VerificationError("Request has expired"), not PaymentExpiredError.
    assert refused.value.problem == "verification-failed" and "Request has expired" in refused.value.detail
    assert chain.sent == []


def test_the_deposit_address_is_checked_again_after_an_hour(tcredits, payments, monkeypatch):
    from calotype import credits as credits_module

    clock = [1000.0]
    monkeypatch.setattr(credits_module.time, "monotonic", lambda: clock[0])
    assert tcredits.tempo_ready()
    listed = payments.client.raw_request.call_count
    clock[0] += 3599
    assert tcredits.tempo_ready() and payments.client.raw_request.call_count == listed  # not asked again yet
    payments.deposit_addresses = []  # the account no longer lists it
    clock[0] += 2
    assert not tcredits.tempo_ready() and tcredits.offered_methods() == ("stripe",)
    payments.deposit_addresses = [DEPOSIT_ADDRESS]
    clock[0] += 299
    assert not tcredits.tempo_ready()  # a failure is asked again after five minutes
    clock[0] += 2
    assert tcredits.tempo_ready()
