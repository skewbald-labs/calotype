"""End to end in the Stripe sandbox: a prepaid pack paid in USDC on the Tempo testnet, no real money.

    python -I calotype/deploy/stripe-sandbox/tempo_e2e.py KEY_FILE SCRATCH_DIR [--refund]

The first argument is a file holding the Stripe SANDBOX secret key; it is read and deleted at once. A
live key is refused. SCRATCH_DIR (outside the repository) holds a throwaway testnet wallet key, made on
the first run, and the SQLite database; delete it afterwards.

It funds the wallet from the Moderato testnet's public faucet (``tempo_fundAddress``), runs Calotype
in this process on 127.0.0.1 with SQLite, stablecoin payment on and ``CALOTYPE_TEMPO_SETTLE_WAIT_S=0``,
and lets ``pympp``'s own client buy the starter pack through the tempo challenge. Then:

1. The startup check found the deposit address among the account's own (the 402 offers ``tempo``).
2. The purchase answers ``202`` pending: the transfer is on the chain, the credit waits for Stripe.
3. It waits until Stripe settles the crypto PaymentIntent, fetches the real ``payment_intent.succeeded``
   event from Stripe's Events API, signs it with a webhook secret made for this run (the sandbox's
   webhook endpoint points at the Cloud Run sandbox, not here) and posts it to ``/v1/billing/webhook``.
   The webhook credits the pack.
4. The same credential, sent again after the challenge expired, answers ``200`` already credited.
5. With ``--refund``, ``admin refund-credit`` refunds it and is run again to read Stripe's outcome.

It prints ids, amounts and statuses only: never the Stripe key, the wallet key, the API key, the
credential or the webhook secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

API_VERSION = "2026-07-29.preview"
RPC = "https://rpc.moderato.tempo.xyz"
PATH_USD = "0x20c0000000000000000000000000000000000000"
PORT = 8789
REPO = Path(__file__).resolve().parents[3]
# The sandbox prices (stripe-sandbox/README.md); billing must be on for the webhook endpoint to exist.
PRICES = {"STRIPE_PRICE_STARTER": "price_1UcrYuCOtiY7WH749scgFsgc", "STRIPE_PRICE_PRO": "price_1UcrYvCOtiY7WH742vtl3SSj"}


def main() -> int:
    key_file, scratch = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
    stripe_key = key_file.read_text().strip()
    key_file.unlink()
    if not stripe_key.startswith(("sk_test_", "rk_test_")):
        print("refused: not a sandbox key", file=sys.stderr)
        return 2
    if REPO in scratch.parents or scratch == REPO:
        print("refused: SCRATCH_DIR must be outside the repository", file=sys.stderr)
        return 2
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)

    import httpx
    import stripe
    from eth_account import Account

    wallet_file = scratch / "wallet.key"
    if not wallet_file.exists():
        wallet_file.write_text(Account.create().key.hex())
        wallet_file.chmod(0o600)
    wallet_key = wallet_file.read_text().strip()
    wallet_key = wallet_key if wallet_key.startswith("0x") else "0x" + wallet_key
    address = Account.from_key(wallet_key).address
    print("testnet wallet:", address)

    def rpc(method: str, params: list) -> dict:
        return httpx.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60).json()

    def balance() -> int:
        data = "0x70a08231" + address[2:].lower().zfill(64)
        return int(rpc("eth_call", [{"to": PATH_USD, "data": data}, "latest"]).get("result") or "0x0", 16)

    if balance() < 20_000_000:
        print("faucet:", "ok" if "result" in rpc("tempo_fundAddress", [address]) else "refused")
        for _ in range(30):
            if balance() >= 20_000_000:
                break
            time.sleep(2)
    print("pathUSD before:", balance() / 1e6)

    client = stripe.StripeClient(stripe_key)
    listed = client.raw_request("get", "/v1/crypto/deposit_addresses", network="tempo", stripe_version=API_VERSION)
    found = client.deserialize(listed, api_mode="V1").to_dict()["data"]
    if not found:
        made = client.raw_request("post", "/v1/crypto/deposit_addresses", network="tempo", stripe_version=API_VERSION)
        found = [client.deserialize(made, api_mode="V1").to_dict()]
    deposit = found[0]
    print("deposit address:", deposit["id"], deposit["address"],
          [t.get("token_contract_address") for t in deposit.get("supported_tokens") or []])
    me = client.raw_request("get", "/v2/network/business_profiles/me", stripe_version=API_VERSION)
    profile = client.deserialize(me, api_mode="V2").to_dict()["id"]

    webhook_secret = "whsec_" + secrets.token_hex(24)  # this run's only; never printed
    os.environ.update({
        "CALOTYPE_DB_PATH": str(scratch / "calotype.db"), "CALOTYPE_SERVE_SITE": "0",
        "CALOTYPE_CREDITS": "1", "CALOTYPE_CREDITS_CARD": "1", "CALOTYPE_CREDITS_STABLECOIN": "1",
        "STRIPE_TEMPO_DEPOSIT_ADDRESS": deposit["address"], "STRIPE_PROFILE_ID": profile,
        "STRIPE_SECRET_KEY": stripe_key, "STRIPE_WEBHOOK_SECRET": webhook_secret,
        "CALOTYPE_TEMPO_SETTLE_WAIT_S": "0", **PRICES,
    })
    sys.path.insert(0, str(REPO))
    import uvicorn

    from calotype import admin, credits as credits_module
    from calotype.app import create_app
    from calotype.config import Settings
    from calotype.storage import open_configured_storage

    class NoBrowser:
        async def start(self): ...
        async def stop(self): ...

    settings = Settings.from_env()
    store = open_configured_storage(settings)
    store.init()
    api_key, key = store.create_key("free", "tempo sandbox e2e", internal=True)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, storage=store, renderer=NoBrowser()),
                                           host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    url = f"http://127.0.0.1:{PORT}/v1/credits?access_key={api_key}"

    from mpp.client.transport import PaymentTransport
    from mpp.methods.tempo import TempoAccount
    from mpp.methods.tempo import client as tempo_client

    class Recording(httpx.AsyncBaseTransport):
        """The network, keeping the credential the buyer's SDK sent, so the same one can be sent again."""

        def __init__(self):
            self.inner, self.credential = httpx.AsyncHTTPTransport(), None

        async def handle_async_request(self, request):
            if request.headers.get("authorization", "").startswith("Payment "):
                self.credential = request.headers["authorization"]
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    recording = Recording()

    async def buy() -> httpx.Response:
        method = tempo_client.charge(
            account=TempoAccount.from_key(wallet_key), currency=PATH_USD, allowed_chain_ids=[42431],
            expected_recipients=[deposit["address"]], max_atomic_amount=20_000_000,
        )
        async with httpx.AsyncClient(transport=PaymentTransport([method], inner=recording), timeout=120) as http:
            return await http.post(url, json={"pack": "starter"})

    def show(label: str, response: httpx.Response) -> dict:
        body = response.json()
        print(label, response.status_code, {k: body.get(k) for k in ("status", "credits", "already_credited",
                                                                    "payment")},
              (body.get("error") or {}).get("code"))
        return body

    try:
        unpaid = httpx.post(url, json={"pack": "starter"}, timeout=30)
        print("1. the 402 offers:", unpaid.json()["error"]["methods"])
        if "tempo" not in unpaid.json()["error"]["methods"]:
            return 1

        body = show("2. POST /v1/credits:", asyncio.run(buy()))
        tx = (body.get("payment") or {}).get("reference")
        if not tx or recording.credential is None:
            return 1
        purchase = store.get_credit_purchase(tx)
        print("   purchase:", purchase.id, purchase.status, purchase.payment_intent,
              "balance", store.get_credit_balance(key.id))
        print("   pathUSD after:", balance() / 1e6)

        intent = None
        for _ in range(18):
            intent = client.v1.payment_intents.retrieve(purchase.payment_intent, params={"expand": ["latest_charge"]})
            print("3. PaymentIntent:", intent.id, intent.status, intent.amount, intent.currency)
            if intent.status in ("succeeded", "canceled", "requires_payment_method"):
                break
            time.sleep(10)
        if intent is None or intent.status != "succeeded":
            return 1
        charge = intent.latest_charge.to_dict() if hasattr(intent.latest_charge, "to_dict") else {}
        crypto = (charge.get("payment_method_details") or {}).get("crypto") or {}
        print("   charge:", charge.get("id"), {k: crypto.get(k) for k in ("network", "token_currency",
                                                                       "transaction_hash")})
        print("   matches the purchase:", credits_module._settlement_mismatch(intent.to_dict(), purchase) is None)

        event = None
        for _ in range(12):
            for candidate in client.v1.events.list(params={"type": "payment_intent.succeeded", "limit": 30}).data:
                if candidate.to_dict()["data"]["object"].get("id") == intent.id:
                    event = candidate
                    break
            if event is not None:
                break
            time.sleep(5)
        if event is None:
            print("   no payment_intent.succeeded event found")
            return 1
        payload = json.dumps(event.to_dict()).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(webhook_secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
        hook = httpx.post(f"http://127.0.0.1:{PORT}/v1/billing/webhook", content=payload, timeout=60,
                          headers={"Stripe-Signature": f"t={stamp},v1={signature}", "Content-Type": "application/json"})
        print("   webhook", event.id, "->", hook.status_code, hook.json().get("outcome"))
        purchase = store.get_credit_purchase(tx)
        print("   purchase:", purchase.status, purchase.payment_intent, "balance", store.get_credit_balance(key.id))

        expires = json.loads(__import__("base64").urlsafe_b64decode(
            recording.credential.split(" ", 1)[1] + "==="))["challenge"]["expires"]
        wait = max(0.0, __import__("datetime").datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
                   - time.time()) + 2
        print(f"4. waiting {wait:.0f}s for the challenge to expire, then sending the same credential")
        time.sleep(wait)
        show("   resent:", httpx.post(url, json={"pack": "starter"}, headers={"Authorization": recording.credential},
                                      timeout=60))

        if "--refund" in sys.argv:
            print("5. refund-credit:", admin.refund_credit(store, tx, yes=True, prorated=False, no_stripe=False))
            for _ in range(6):
                time.sleep(10)
                purchase = store.get_credit_purchase(tx)
                if purchase.refund_status == "succeeded":
                    break
                admin.refund_credit(store, tx, yes=True, prorated=False, no_stripe=False)
            purchase = store.get_credit_purchase(tx)
            print("   refund:", purchase.refund_id, purchase.refund_status, purchase.refund_amount_cents,
                  "balance", store.get_credit_balance(key.id))
            refunds = client.v1.refunds.list(params={"payment_intent": intent.id}).data
            print("   refunds on the PaymentIntent:", [(r.id, r.status, r.amount) for r in refunds])
        return 0
    finally:
        server.should_exit = True
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
