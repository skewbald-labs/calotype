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

## Prepaid credit (`POST /v1/credits`): the sandbox run that has not happened yet

Written on 2026-11-20 from Stripe's docs. None of this has been run; `tests/test_credits.py` uses a mocked Stripe. A sandbox needs no agent and no wallet: Stripe has a test helper that grants a shared payment token (SPT) directly.

1. In the sandbox Dashboard, create a Stripe profile and note its `profile_test_...` id.
2. Add `payment_intent.succeeded` to the sandbox webhook `we_1UcrYwCOtiY7WH74FbdDl1yf`.
3. Deploy `calotype-sandbox` as above with `STRIPE_PROFILE_ID=profile_test_...` and `--credits` added.
4. Ask for the challenge. It should be a 402 with `method="stripe"` and, decoded, `"amount": "1900"`:

```sh
curl -si -X POST $HOST/v1/credits -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"pack": "starter"}'
```

5. Grant a test SPT with the same limits a wallet would set (https://docs.stripe.com/agentic-commerce/concepts/shared-payment-tokens, "Generate an SPT in a sandbox"). `expires_at` is a Unix time a few minutes ahead:

```sh
curl https://api.stripe.com/v1/test_helpers/shared_payment/granted_tokens \
  -u "$STRIPE_TEST_KEY:" -H "Stripe-Version: 2026-09-30.preview" \
  -d payment_method=pm_card_visa \
  -d "usage_limits[currency]=usd" -d "usage_limits[max_amount]=1900" \
  -d "usage_limits[expires_at]=$(( $(date +%s) + 300 ))"
```

6. Build the credential from the challenge and the returned `spt_...` id, and send the request again with it in `Payment-Authorization`. The credential is base64url (no padding) of `{"challenge": {...every parameter of the challenge except description, unchanged...}, "payload": {"spt": "spt_..."}}`. `tests/test_credits.py` has it as the `credential` helper. Expect 200 with `"credits": 5000`, a `Payment-Receipt` header, and the payment under Payments in the Dashboard with `machine_payment=true` in its metadata.
7. Send the same request again: 200, `"already_credited": true`, and still one payment in the Dashboard.
8. Check each failure once: `max_amount=100` on the token (too small), a token used twice, an expired token. Each should be a 402 `verification_failed` with nothing credited. If Stripe answers something else, `credits.py` (`_charge`) is where its error types are mapped.
9. Check the webhook: the event for the payment in step 6 should answer `already_credited`.
10. Refund it: `python -m calotype.admin refund-credit pi_... --yes`, then look for the refund in the Dashboard and `"credits": 0` from `/v1/usage`.
11. Run the protocol's own validator with the sandbox key, so that its payment step runs too (without a key it only checks the challenge, which passed locally on 2026-11-20): `MPPX_STRIPE_SECRET_KEY=sk_test_... npx mppx@0.21.1 validate $HOST --endpoint POST:/v1/credits --body '{"pack": "starter"}' -H "Authorization:Bearer $KEY" --yes`. Left unset, it looks for a key in a logged-in Stripe CLI (`stripe config --list`); set the variable so it uses the sandbox key and no other.

To test with a wallet instead of the helper, `npx @stripe/link-cli mpp pay $HOST/v1/credits -X POST -d '{"pack": "starter"}' --context "..."` pays from a personal Link account. That is a person's account and real money in live mode; it is the founder's call.
