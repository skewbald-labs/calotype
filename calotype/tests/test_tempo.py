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
            elif method == "eth_sendRawTransactionSync":
                tx_hash = tx_hash_of(params[0])
                if tx_hash in self.receipts:
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


@pytest.fixture
def payments() -> FakePayments:
    return FakePayments()


@pytest.fixture
def tempo_config() -> CreditsConfig:
    config, missing = CreditsConfig.from_env(TEMPO_ENV)
    assert missing == [], missing
    return config


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


def test_a_stop_between_the_chain_and_the_credit_is_mended_by_sending_again(tclient, store, chain, monkeypatch):
    """The transfer is claimed for this challenge, then the server fails before crediting. The same
    credential is checked again (the claim is this challenge's own) and credited once."""
    raw, key = store.create_key("free")
    params = tempo_params(ask(tclient, raw))
    header = credential(params, {"type": "hash", "hash": pushed_transfer(chain, params)})
    real_add = store.add_credit_purchase
    monkeypatch.setattr(store, "add_credit_purchase", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stopped")))
    with pytest.raises(RuntimeError):
        send(tclient, raw, header)
    assert store.get_payment_claim(f"mpp:charge:{'0x' + 'ab' * 32}")[0] == params["id"]
    monkeypatch.setattr(store, "add_credit_purchase", real_add)
    r = send(tclient, raw, header)
    assert r.status_code == 200 and r.json()["already_credited"] is False
    assert store.get_credit_balance(key.id) == 5000


def test_a_failed_stripe_record_keeps_the_credit_and_is_retried(tclient, store, chain, payments, caplog):
    raw, key = store.create_key("free")
    payments.fail_with = stripe.APIConnectionError("no connection")
    _, header, r = pay_tempo(tclient, chain, raw)
    assert r.status_code == 200  # the coins moved: the buyer gets the pack whatever Stripe says
    tx_hash = r.json()["payment"]["reference"]
    assert store.get_credit_purchase(tx_hash).payment_intent is None
    assert "tempo payment not recorded in stripe" in logged(caplog)
    payments.fail_with = None
    again = send(tclient, raw, header)
    assert again.status_code == 200 and again.json()["already_credited"] is True
    assert store.get_credit_purchase(tx_hash).payment_intent == "pi_test_1"
    assert store.get_credit_balance(key.id) == 5000


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


def test_the_webhook_alone_credits_a_tempo_payment_under_its_hash(settings, store, fake_renderer, tcredits, billing,
                                                                  payments):
    """A server stopped after Stripe recorded the payment and before the credit was written."""
    raw, key = store.create_key("free")
    tx_hash = "0x" + "cd" * 32
    payments.intents["pi_hook_tempo"] = {
        "id": "pi_hook_tempo", "object": "payment_intent", "status": "succeeded", "amount": 1900,
        "amount_received": 1900, "currency": "usd",
        "metadata": {"calotype_key_id": str(key.id), "calotype_credit_pack": "starter",
                     "calotype_credit_screenshots": "5000", "calotype_tempo_tx": tx_hash.upper().replace("0X", "0x"),
                     "mpp_challenge_id": "ch"},
    }
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        hook = post_event(c, event("evt_tempo_2", "payment_intent.succeeded", payments.intents["pi_hook_tempo"]))
        replay = post_event(c, event("evt_tempo_3", "payment_intent.succeeded", payments.intents["pi_hook_tempo"]))
    assert hook.json()["outcome"] == "credited" and replay.json()["outcome"] == "already_credited"
    purchase = store.get_credit_purchase(tx_hash)
    assert (purchase.method, purchase.payment_intent, purchase.key_id) == ("tempo", "pi_hook_tempo", key.id)
    assert store.get_credit_balance(key.id) == 5000


def test_the_webhook_ignores_a_bad_transaction_hash(settings, store, fake_renderer, tcredits, billing, payments):
    raw, key = store.create_key("free")
    payments.intents["pi_bad"] = {
        "id": "pi_bad", "object": "payment_intent", "status": "succeeded", "amount": 1900, "amount_received": 1900,
        "currency": "usd", "metadata": {"calotype_key_id": str(key.id), "calotype_credit_pack": "starter",
                                        "calotype_credit_screenshots": "5000", "calotype_tempo_tx": "0x/../x"},
    }
    with make_client(settings, store, fake_renderer, tcredits, billing) as c:
        hook = post_event(c, event("evt_bad", "payment_intent.succeeded", payments.intents["pi_bad"]))
    assert hook.json()["outcome"] == "ignored" and store.get_credit_balance(key.id) == 0


# --- refunds and the admin CLI -------------------------------------------------------------------------------


@pytest.fixture
def admin_payments(monkeypatch) -> FakePayments:
    fake = FakePayments()
    monkeypatch.setattr(admin, "open_credits", lambda: credits_for_admin({"STRIPE_SECRET_KEY": SECRET_KEY}, client=fake.client))
    return fake


TX = "0x" + "ef" * 32


def bought_in_usdc(store, payment_intent: str | None = "pi_tempo_1"):
    _, key = store.create_key("free")
    store.add_credit_purchase(
        TX, key.id, pack="starter", screenshots=5000, amount_cents=1900, currency="usd", challenge_id="ch",
        method="tempo", payment_intent=payment_intent,
    )
    return key


def test_a_tempo_purchase_is_refunded_through_its_payment_intent(admin_store, admin_payments, capsys):
    key = bought_in_usdc(admin_store)
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
