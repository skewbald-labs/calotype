"""End to end in the Stripe sandbox: a prepaid pack paid in USDC on the Tempo testnet, no real money.

    python -I calotype/deploy/stripe-sandbox/tempo_e2e.py KEY_FILE SCRATCH_DIR [--refund]

The first argument is a file holding the Stripe SANDBOX secret key; it is read and deleted at once. A
live key is refused. SCRATCH_DIR (outside the repository) holds a throwaway testnet wallet key, made on
the first run, and the SQLite database; delete it afterwards.

It funds the wallet from the Moderato testnet's public faucet (``tempo_fundAddress``), runs Calotype
in this process on 127.0.0.1 with SQLite and stablecoin payment on, and lets ``pympp``'s own client
buy the starter pack through the tempo challenge. Then it reads the PaymentIntent that records the
payment until Stripe settles it, and with ``--refund`` refunds it through ``StripeCredits.refund``.
It prints ids, amounts and statuses only: never the Stripe key, the wallet key or the API key.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

API_VERSION = "2026-07-29.preview"
RPC = "https://rpc.moderato.tempo.xyz"
PATH_USD = "0x20c0000000000000000000000000000000000000"
PORT = 8789
REPO = Path(__file__).resolve().parents[3]


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
    print("deposit address:", deposit["id"], deposit["address"])
    me = client.raw_request("get", "/v2/network/business_profiles/me", stripe_version=API_VERSION)
    profile = client.deserialize(me, api_mode="V2").to_dict()["id"]

    os.environ.update({
        "CALOTYPE_DB_PATH": str(scratch / "calotype.db"), "CALOTYPE_SERVE_SITE": "0",
        "CALOTYPE_CREDITS": "1", "CALOTYPE_CREDITS_CARD": "1", "CALOTYPE_CREDITS_STABLECOIN": "1",
        "STRIPE_TEMPO_DEPOSIT_ADDRESS": deposit["address"], "STRIPE_PROFILE_ID": profile,
        "STRIPE_SECRET_KEY": stripe_key,
    })
    sys.path.insert(0, str(REPO))
    import uvicorn

    from calotype.app import create_app
    from calotype.config import Settings
    from calotype.credits import credits_for_admin
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

    from mpp.client import Mpp
    from mpp.methods.tempo import TempoAccount
    from mpp.methods.tempo import client as tempo_client

    async def buy() -> httpx.Response:
        method = tempo_client.charge(
            account=TempoAccount.from_key(wallet_key), currency=PATH_USD, allowed_chain_ids=[42431],
            expected_recipients=[deposit["address"]], max_atomic_amount=20_000_000,
        )
        async with Mpp(methods=[method]) as buyer, buyer.client(timeout=120) as http:
            url = f"http://127.0.0.1:{PORT}/v1/credits?access_key={api_key}"
            return await http.post(url, json={"pack": "starter"})

    try:
        response = asyncio.run(buy())
        body = response.json()
        print("POST /v1/credits:", response.status_code, {k: body.get(k) for k in ("credits", "payment", "error")})
        if response.status_code != 200:
            return 1
        tx = body["payment"]["reference"]
        purchase = store.get_credit_purchase(tx)
        print("purchase:", purchase.id, purchase.method, purchase.payment_intent, "balance",
              store.get_credit_balance(key.id))
        print("pathUSD after:", balance() / 1e6)
        intent = None
        for _ in range(12):
            intent = client.v1.payment_intents.retrieve(purchase.payment_intent)
            print("PaymentIntent:", intent.id, intent.status, intent.amount, intent.currency)
            if intent.status in ("succeeded", "canceled", "requires_payment_method"):
                break
            time.sleep(10)
        if "--refund" in sys.argv and intent is not None and intent.status == "succeeded":
            store.refund_credit_purchase(tx)
            refund_id = credits_for_admin({"STRIPE_SECRET_KEY": stripe_key}).refund(store.get_credit_purchase(tx), 1900)
            time.sleep(10)
            refund = client.v1.refunds.retrieve(refund_id)
            print("Refund:", refund.id, refund.status, refund.amount)
        return 0
    finally:
        server.should_exit = True
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
