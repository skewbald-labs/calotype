# Stripe sandbox

Set up on 2026-11-17. Billing is tested here before anything goes live. No real money moves.

## What exists

| What | Id or name |
|---|---|
| Stripe account (sandbox) | `acct_1UcrTqCOtiY7WH74`, the Skewbald account under Dostal Technology |
| Starter, $19 a month | `price_1UcrYuCOtiY7WH749scgFsgc` (lookup key `calotype_starter_monthly`) |
| Pro, $49 a month | `price_1UcrYvCOtiY7WH742vtl3SSj` (lookup key `calotype_pro_monthly`) |
| Customer portal | `bpc_1UcrYvCOtiY7WH74Ad7ULFbj`, the default: cancel at period end, change card, switch plan, invoices |
| Webhook | `we_1UcrYwCOtiY7WH74FbdDl1yf`, API version `2026-10-28.endive`, the four events `billing.py` handles |
| Cloud Run service | `calotype-sandbox`, https://calotype-sandbox-1047391305118.us-central1.run.app, Firestore prefix `calotype_sandbox_` |
| Secrets | Secret Manager `calotype-stripe-test-secret-key` and `calotype-stripe-test-webhook-secret`, readable only by `calotype-run@`; the operator keeps copies in a local secret store |

The main `calotype` service has billing off until live mode, so no visitor reaches a sandbox checkout.

## Files

- `setup.py`: creates the products, prices, portal settings and webhook. It is idempotent and prints no secret. When it creates the webhook, it writes the signing secret to a mode-600 file for the caller to store, and the caller then deletes it.
- `e2e.py`: the end-to-end test. It runs checkout in a real browser with test card 4242, then checks the webhook's plan change, the portal link and a cancel, and deletes the test customer. It passed on 2026-11-17.

## Deploy

```sh
STRIPE_PRICE_STARTER=price_1UcrYuCOtiY7WH749scgFsgc STRIPE_PRICE_PRO=price_1UcrYvCOtiY7WH742vtl3SSj \
STRIPE_KEY_SECRET=calotype-stripe-test-secret-key STRIPE_WEBHOOK_SECRET_NAME=calotype-stripe-test-webhook-secret \
calotype/deploy/deploy.sh --skip-setup --project skewbald --service calotype-sandbox \
  --firestore-prefix calotype_sandbox_ --stripe \
  --public-url https://calotype-sandbox-1047391305118.us-central1.run.app \
  --service-account calotype-run@skewbald.iam.gserviceaccount.com \
  --build-service-account calotype-build@skewbald.iam.gserviceaccount.com
```

## Left for live mode

- The sandbox checkout offers Cash App Pay, Klarna and bank payments as well as cards. These are Stripe's dynamic payment methods, set in the Dashboard. For a $19 developer subscription, cards and Link are enough. Decide this before going live.
- The live key gets narrower permissions, and the founder creates it after reading the customer terms.

## Live mode (prepared 2026-11-17)

The same `setup.py` sets up live mode with `STRIPE_MODE=live` and a live restricted key (`rk_live_...`). It refuses a key of the other mode.

- Products carry the tax code `txcd_10701300` (SaaS, business use) and prices are tax-exclusive. The sandbox's products and prices were updated to match on 2026-11-17.
- Checkout adds Stripe Tax only when the service runs with `STRIPE_AUTOMATIC_TAX=1` (`deploy.sh --stripe` passes it through). Turn it on only once the account has its head office address and a Texas registration, which needs the Texas sales and use tax permit number.
- The live webhook points at `https://calotype.dev/v1/billing/webhook`. Its signing secret goes to Secret Manager `calotype-stripe-webhook-secret`, and the key to `calotype-stripe-secret-key`, the names `deploy.sh` uses by default.
