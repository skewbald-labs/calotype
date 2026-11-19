"""Set up Calotype's Stripe account: sandbox, or live with STRIPE_MODE=live.

Idempotent. Prints no secret.

argv: key_file webhook_url out_dir
Products carry Stripe Tax's code for SaaS (txcd_10701300) and prices are
tax-exclusive, so Stripe Tax adds Texas tax on top of the listed price
(the operator's sales-tax notes). Both are also set on products and prices
made before this was added, where Stripe allows it.
Writes out_dir/ids.json (non-secret ids) and, only when a webhook endpoint is
created, out_dir/whsec (the signing secret, mode 600) for the caller to store.
"""
import json
import os
import sys

import stripe

key_file, webhook_url, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
with open(key_file) as f:
    key = f.read().strip()
MODE = os.environ.get("STRIPE_MODE", "test")
assert MODE in ("test", "live"), "STRIPE_MODE is test or live"
assert key.startswith((f"sk_{MODE}_", f"rk_{MODE}_")), f"not a {MODE}-mode key"
SITE = webhook_url.rsplit("/v1/", 1)[0]
TAX_CODE = "txcd_10701300"  # Software as a service (SaaS), business use
c = stripe.StripeClient(key)

try:
    acct = c.v1.accounts.retrieve_current()
    print("account", acct.id, "country", acct.country, "charges_enabled", acct.charges_enabled)
    acct_id = acct.id
except stripe.PermissionError:
    # A restricted key may not read the account; nothing below needs it.
    print("account: not readable with this key")
    acct_id = None

PLANS = [
    ("starter", "Calotype Starter", 1900, "5,000 website screenshots a month"),
    ("pro", "Calotype Pro", 4900, "25,000 website screenshots a month"),
]
ids = {"account": acct_id}
for plan, name, cents, desc in PLANS:
    lk = f"calotype_{plan}_monthly"
    found = c.v1.prices.list(params={"lookup_keys": [lk], "active": True, "limit": 1}).data
    if found:
        price = found[0]
        print("price exists", lk, price.id)
        if price.tax_behavior == "unspecified":
            price = c.v1.prices.update(price.id, params={"tax_behavior": "exclusive"})
            print("price set tax-exclusive", price.id)
        prod_id = price.product if isinstance(price.product, str) else price.product.id
        prod = c.v1.products.retrieve(prod_id)
        if prod.tax_code != TAX_CODE:
            c.v1.products.update(prod_id, params={"tax_code": TAX_CODE})
            print("product tax code set", prod_id)
    else:
        prod = c.v1.products.create(params={
            "name": name, "description": desc,
            "metadata": {"calotype_plan": plan},
            "statement_descriptor": "CALOTYPE",
            "tax_code": TAX_CODE,
        })
        price = c.v1.prices.create(params={
            "product": prod.id, "currency": "usd", "unit_amount": cents,
            "recurring": {"interval": "month"}, "lookup_key": lk,
            "tax_behavior": "exclusive",
            "metadata": {"calotype_plan": plan},
        })
        print("created", lk, price.id)
    ids[f"price_{plan}"] = price.id
    ids[f"product_{plan}"] = price.product if isinstance(price.product, str) else price.product.id

# Customer portal: cancel at period end, change card, switch plan, see invoices.
portal_params = {
    "business_profile": {
        "headline": "Calotype: manage your plan",
        "privacy_policy_url": SITE + "/privacy",
        "terms_of_service_url": SITE + "/terms",
    },
    "features": {
        "invoice_history": {"enabled": True},
        "payment_method_update": {"enabled": True},
        "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
        "subscription_update": {
            "enabled": True,
            "default_allowed_updates": ["price"],
            "proration_behavior": "create_prorations",
            "products": [
                {"product": ids["product_starter"], "prices": [ids["price_starter"]]},
                {"product": ids["product_pro"], "prices": [ids["price_pro"]]},
            ],
        },
    },
    "default_return_url": SITE + "/pricing#upgrade",
}
existing = [p for p in c.v1.billing_portal.configurations.list(params={"limit": 20}).data if p.is_default]
if existing:
    conf = c.v1.billing_portal.configurations.update(existing[0].id, params=portal_params)
    print("portal config updated", conf.id)
else:
    conf = c.v1.billing_portal.configurations.create(params=portal_params)
    print("portal config created", conf.id, "default", conf.is_default)
ids["portal_config"] = conf.id

# Payment methods on the default configuration: cards, the card wallets and
# Link. Everything else off (decided 2026-11-17: a $19 developer subscription
# needs nothing more, and fewer methods mean fewer refund and dispute paths).
KEEP_ON = {"card", "link", "apple_pay", "google_pay"}
for pmc in c.v1.payment_method_configurations.list(params={"limit": 10}).data:
    if not pmc.is_default:
        continue
    d = pmc.to_dict()
    changes = {}
    for name, v in d.items():
        if not isinstance(v, dict) or "display_preference" not in v:
            continue
        want = "on" if name in KEEP_ON else "off"
        if v["display_preference"].get("value") != want:
            changes[name] = {"display_preference": {"preference": want}}
    if changes:
        c.v1.payment_method_configurations.update(pmc.id, params=changes)
    print("payment methods", pmc.id, "changed:", sorted(changes))
    ids["payment_method_configuration"] = pmc.id

EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
]
hooks = [w for w in c.v1.webhook_endpoints.list(params={"limit": 50}).data if w.url == webhook_url]
if hooks:
    print("webhook exists", hooks[0].id, "(secret not retrievable; stored copy is used)")
    ids["webhook"] = hooks[0].id
else:
    hook = c.v1.webhook_endpoints.create(params={
        "url": webhook_url, "enabled_events": EVENTS,
        "api_version": stripe.api_version,
        "description": f"Calotype ({MODE} mode)",
    })
    ids["webhook"] = hook.id
    path = os.path.join(out_dir, "whsec")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(hook.secret)
    print("webhook created", hook.id, "api_version", stripe.api_version)

with open(os.path.join(out_dir, "ids.json"), "w") as f:
    json.dump(ids, f, indent=1)
print(json.dumps(ids))
