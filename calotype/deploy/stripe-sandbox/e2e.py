"""End-to-end sandbox test of Calotype billing. Prints no secret.

argv: sk_file key_file. sk_file holds the sandbox secret key; key_file holds a
Calotype key on the sandbox service, made with `calotype.admin create-key`
(internal, so it never counts as a contact). Drives Stripe Checkout in a real
browser with test card 4242, then checks the webhook, the portal and a cancel.
Uses no real money. Run 2026-11-17: all seven steps passed.
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

import stripe
from playwright.async_api import async_playwright

BASE = "https://calotype-sandbox-1047391305118.us-central1.run.app"
sk = open(sys.argv[1]).read().strip()
assert sk.startswith("sk_test_")
sc = stripe.StripeClient(sk)


def call(method, path, key=None, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def plan_of(key):
    s, u = call("GET", "/v1/usage", key)
    return u.get("plan")


key = open(sys.argv[2]).read().strip()
print("1. internal free key, plan:", plan_of(key))

st, co = call("POST", "/v1/billing/checkout", key, {"plan": "starter"})
url = co.get("url") or co.get("checkout_url")
print("2. checkout:", st, "url on checkout.stripe.com:", bool(url and "checkout.stripe.com" in url))


async def pay(url):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page()
        await pg.goto(url, wait_until="load")
        await pg.wait_for_timeout(6000)
        fr = [f for f in pg.frames if "elements-inner" in f.url][0]
        await fr.fill("#linkAuthentication-emailInput", "sandbox-test@example.com")
        await fr.get_by_text("Card", exact=True).click()
        await pg.wait_for_timeout(2000)
        await fr.fill("#payment-numberInput", "4242424242424242")
        await fr.fill("#payment-expiryInput", "12 / 34")
        await fr.fill("#payment-cvcInput", "123")
        await fr.fill("#payment-nameInput", "Sandbox Test")
        await fr.fill("#payment-postalCodeInput", "68102")
        opt = fr.locator("#payment-linkOptInInput")
        if await opt.count() and await opt.is_checked():
            await opt.uncheck(force=True)
        await fr.click('[data-testid="payment-form-submit-button"]')
        try:
            await pg.wait_for_url(lambda u: not u.startswith("https://checkout.stripe.com"), timeout=45000)
        except Exception:
            await pg.screenshot(path=os.path.join(os.environ.get("TMPDIR", "."), "after-submit.png"), full_page=True)
            raise SystemExit("stuck on checkout; screenshot saved")
        landed = pg.url.split("?")[0]
        await b.close()
        return landed


landed = asyncio.run(pay(url))
print("3. paid with test card, returned to:", landed)

for i in range(30):
    if plan_of(key) == "starter":
        break
    time.sleep(2)
print("4. after webhook, plan:", plan_of(key))

st, pt = call("GET", "/v1/billing/portal", key)
purl = pt.get("url") or pt.get("portal_url")
print("5. portal:", st, "billing.stripe.com:", bool(purl and "billing.stripe.com" in purl))

subs = sc.v1.subscriptions.list(params={"limit": 10, "status": "active"}).data
for sub in subs:
    md = sub.metadata.to_dict() if sub.metadata else {}
    if md.get("plan") != "starter":
        continue
    cust = sub.customer if isinstance(sub.customer, str) else sub.customer.id
    sc.v1.subscriptions.cancel(sub.id)
    for i in range(30):
        if plan_of(key) == "free":
            break
        time.sleep(2)
    print("6. cancelled subscription, plan now:", plan_of(key))
    sc.v1.customers.delete(cust)
    print("7. test customer deleted")
