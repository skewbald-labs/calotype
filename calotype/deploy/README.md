# Deploying Calotype to Cloud Run

Status: first run against project `skewbald` on 2026-11-17.

| File | What it does |
|---|---|
| `cloudbuild.yaml` | Cloud Build: `docker build` of `calotype/`, push to Artifact Registry |
| `deploy.sh` | Enables APIs, creates the Artifact Registry repo and the Firestore database if missing, submits the build, deploys the service |
| `../.gcloudignore` | Keeps `data/`, `*.db`, tests and caches out of the build upload |

```sh
# Print every command, run nothing:
calotype/deploy/deploy.sh --dry-run --project PROJECT_ID \
  --service-account calotype-run@PROJECT_ID.iam.gserviceaccount.com

# First deploy (setup + build + deploy), by someone holding the setup roles below:
calotype/deploy/deploy.sh --project PROJECT_ID \
  --service-account calotype-run@PROJECT_ID.iam.gserviceaccount.com \
  --build-service-account calotype-build@PROJECT_ID.iam.gserviceaccount.com

# Later deploys, with only the routine roles:
calotype/deploy/deploy.sh --skip-setup --project PROJECT_ID --service-account ... --build-service-account ...
```

The service runs with `CALOTYPE_STORAGE=firestore`, `CALOTYPE_TRUSTED_PROXY_HOPS=1`, min 0 and max 2 instances, 2 GiB, 1 vCPU, a 60 s timeout, gen2, and public access, since the API checks its own keys. Public access uses `--no-invoker-iam-check` rather than an `allUsers` invoker binding, so it works in organizations whose `iam.allowedPolicyMemberDomains` policy forbids `allUsers`. `--concurrency` (default 4) sets both Cloud Run's per-instance concurrency and `CALOTYPE_MAX_CONCURRENCY`, the app's capture semaphore, so the two always match. One vCPU for four Chromium pages is tight: if captures time out under load, use `--concurrency 2`.

## Billing (Stripe)

Billing is off unless you pass `--stripe`. Without it the service has no secret references, and `/v1/billing/*` answers 503. To turn it on:

1. In Stripe, create a Starter and a Pro product, each with a monthly recurring Price, and add a webhook endpoint at `https://HOST/v1/billing/webhook` for `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated` and `customer.subscription.deleted`.
2. Put the two secrets in Secret Manager from stdin, so they never appear in a command line or shell history (for example `gcloud secrets create calotype-stripe-secret-key --data-file=-`, then paste the value). Do the same for `calotype-stripe-webhook-secret`.
3. Grant the runtime account access to those two secrets only (see below).
4. Deploy with `STRIPE_PRICE_STARTER=price_... STRIPE_PRICE_PRO=price_... deploy.sh --stripe --public-url https://HOST ...`. Price ids are not secret and go in as plain environment variables. The secrets go in as `--set-secrets` references (`:latest` by default, or `--secret-version N` to pin one).

## Prepaid credit by machine payment (`POST /v1/credits`)

Status: written and tested against a mocked Stripe on 2026-11-20. Never run against Stripe, sandbox or live. Off everywhere.

An agent buys a pack of screenshots (one pack: 5,000 for $19, in `plans.json` under `credit_packs`) with no person and no checkout page, over the Machine Payments Protocol (MPP). `credits.py` describes the flow and `mpp.py` the wire format. It is off unless every required setting below is present; then `/v1/credits` answers 402 challenges instead of 503.

### Settings

| Variable | Required | Meaning |
|---|---|---|
| `CALOTYPE_CREDITS` | yes | `1` turns the endpoint on. Anything else, or unset: off, 503 |
| `CALOTYPE_CREDITS_CARD` | yes | `1` offers cards through Stripe shared payment tokens (SPTs). It is the only method built, so without it the endpoint stays off |
| `STRIPE_SECRET_KEY` | yes | the key billing already uses (Secret Manager). It creates the PaymentIntent |
| `STRIPE_PROFILE_ID` | yes | the Stripe profile id, `profile_...` (sandbox: `profile_test_...`). Not a secret; buyers see it in every challenge |
| `CALOTYPE_PUBLIC_URL` | recommended | its host is the challenge realm, which must be the same on every instance. `--public-url` sets it |
| `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | recommended | with them the webhook endpoint is on, and `payment_intent.succeeded` credits a payment the request did not get to record. Without them only the buyer's own retry does that |
| `MPP_SECRET_KEY` | no | signs challenges. Unset: derived from `STRIPE_SECRET_KEY` the way Stripe's sample derives it, so there is no second secret to store |
| `MPP_REALM` | no | the challenge realm, if it should not be the public host |
| `STRIPE_MPP_API_VERSION` | no | default `2026-07-29.preview`, the version `mppx` 0.21.1 sends |
| `STRIPE_SPT_PAYMENT_METHOD_TYPES` | no | default `card,link` |
| `CALOTYPE_CREDITS_STABLECOIN` | no | **not built.** `1` only logs a warning at startup; nothing is offered for it |

Deploy: `STRIPE_PROFILE_ID=profile_... deploy.sh --stripe --credits --public-url https://HOST ...` adds `CALOTYPE_CREDITS=1`, `CALOTYPE_CREDITS_CARD=1` and `STRIPE_PROFILE_ID` to the service. Without `--credits` the service is deployed exactly as before.

### What a person has to do in Stripe first

Each of these binds the company, so the founder does them or approves them. Sources are the pages read on 2026-11-20.

1. **Read and accept the preview terms.** Using SPTs accepts the "Stripe Agentic Commerce Seller Services (Preview)" terms: merchant of record, fraud on these transactions, tax, receipts, refunds and disputes are the seller's. https://stripe.com/legal/ssa-service-terms#stripe-agentic-commerce-seller-services-preview
2. **Create the Stripe profile.** Dashboard, Stripe profile, Get started: a display name and a handle. It is a public business identity. Copy its `profile_...` id, or read it with `GET /v2/network/business_profiles/me` and `Stripe-Version: 2026-07-29.preview`. https://docs.stripe.com/get-started/account/profile
3. **Check the account can use the preview API.** The PaymentIntent call sends `Stripe-Version: 2026-07-29.preview` and the preview parameter `shared_payment_granted_token`. The docs do not say whether an account must be enrolled; the sandbox run below answers it.
4. **Add `payment_intent.succeeded` to the webhook endpoint** (`/v1/billing/webhook`), beside the four subscription events. Other payments' events are ignored: only PaymentIntents carrying `calotype_credit_pack` in their metadata are acted on.
5. **Decide the live key's permissions.** A restricted key needs to write PaymentIntents and Refunds as well as what billing needs.
6. **Update Calotype's own terms, refund policy and privacy notice** for prepaid packs (no expiry, what a refund returns, that a purchase record with the Stripe payment id is kept). The site pages say "not yet live" until then; `site/pages/terms.html`, `refunds.html` and `privacy.html` were not changed.
7. Optional: email machine-payments@stripe.com for a Stripe Directory listing. https://docs.stripe.com/payments/machine

Only for stablecoins, which are not built: request the **Stablecoins and Crypto** payment method in the Dashboard (Stripe reviews it; not available to New York businesses), and create a Tempo deposit address with `POST /v1/crypto/deposit_addresses`.

### How it was built, and what is not verified

- **No Node sidecar.** Stripe's guide uses the Node package `mppx`. The protocol is documented (https://mpp.dev/protocol, https://paymentauth.org) and has an official Python SDK, `pympp`. Calotype implements the card method's wire format itself in `mpp.py`, about 300 lines with no new dependency, and its tests check it against the specifications' own examples and, when `pympp` is installed, against that SDK as parser, verifier and paying client. `pympp` is not a runtime dependency: it had 12 releases in the four weeks before this was written, and its Stripe extra pulls in a blockchain stack.
- **Stablecoins are not built.** Accepting Tempo USDC means verifying or broadcasting the buyer's signed transaction, a replay store shared by all instances, and recording the payment in Stripe as a crypto PaymentIntent. `pympp` does this (`mpp.methods.stripe.machine_payments`). Rebuilding it by hand, untested against a chain, would be guessing with money. To add it: depend on `pympp[stripe]`, give it a Firestore-backed replay store, and test on Tempo's testnet with a sandbox deposit address.
- **Two documented forms of the PaymentIntent call disagree.** `mppx`, `pympp` and the IETF draft send the token as top-level `shared_payment_granted_token` with version `2026-07-29.preview`. Stripe's shared payment tokens page shows `payment_method_data[shared_payment_granted_token]` with `2026-09-30.preview`. Calotype sends the first form, the one Stripe's MPP guide runs. If the sandbox refuses it, try `STRIPE_MPP_API_VERSION=2026-09-30.preview`; the parameter's place is a one-line change in `credits.py` (`_charge`).
- **Not verified against Stripe at all:** that the account accepts the preview version; what Stripe answers for a used, expired or wrong-seller token (assumed: a 400, which becomes a 402 with a new challenge); whether a replayed idempotent request carries `Idempotent-Replayed: true` on this endpoint (assumed, as documented for the API in general).
- **Not verified with a real wallet:** `link-cli mpp pay`. In particular whether it honours a challenge's `header="Payment-Authorization"`. If it only sends `Authorization: Payment`, the buyer puts the key in the address (`/v1/credits?access_key=cal_...`) and the challenge then asks for `Authorization`; both forms are tested here.
- **Checked with the protocol's own tool:** `mppx validate` (mppx 0.21.1) against a local server with made-up Stripe values, on 2026-11-20: `mppx validate http://127.0.0.1:PORT --endpoint POST:/v1/credits --body '{"pack": "starter"}' -H "Authorization:Bearer KEY"`. All 15 challenge and error-handling checks passed (402 without credentials, challenge parseable as stripe/charge, id, realm matches host, expiry, amount, currency, networkId, paymentMethodTypes, malformed credential answers 402 with a fresh challenge). Its payment step was skipped: it needs a Stripe sandbox key.
- **Not done:** an MPP discovery document and `/llms.txt` (the validator suggests both), a `-32042` payment challenge from the MCP tool when the quota runs out, expiry of credit, and removing credit when a refund is made in the Stripe Dashboard instead of with the admin command.

### Refunds

```sh
python -m calotype.admin credits KEY_ID                 # the balance and the purchases, by PaymentIntent id
python -m calotype.admin refund-credit pi_...           # dry run: what would be taken back and refunded
python -m calotype.admin refund-credit pi_... --yes     # take back the unused credit, refund in full
python -m calotype.admin refund-credit pi_... --yes --prorated    # refund only the unused share of the price
python -m calotype.admin refund-credit pi_... --yes --no-stripe   # credit only; refund by hand in the Dashboard
```

It needs `STRIPE_SECRET_KEY` and the storage settings in the environment, like `revoke-key`. The credit goes first, then the money: the purchase is marked refunded and its unused credit removed in one step (the pack's size or the key's whole balance, whichever is smaller; screenshots already taken stay taken), then Stripe refunds the PaymentIntent with the idempotency key `calotype_credit_refund_<pi id>`. If Stripe fails, run the same command again: the credit is not taken twice and the money is not returned twice. A refund made in the Dashboard does not touch the credit; follow it with `refund-credit pi_... --yes --no-stripe`.

## IAM roles

### Deployer: the person or CI account running `deploy.sh`

Every deploy (enough for `--skip-setup`):

| Role | Granted on | Why |
|---|---|---|
| `roles/cloudbuild.builds.editor` | project | submit the build |
| `roles/serviceusage.serviceUsageConsumer` | project | call the project's APIs from gcloud |
| `roles/storage.admin` | the Cloud Build source bucket only (gcloud's default is `gs://PROJECT_ID_cloudbuild`; confirm the name after the first build) | upload the source that `gcloud builds submit` stages |
| `roles/logging.viewer` | project | stream the build log, which goes to Cloud Logging only |
| `roles/run.admin` | project | create and update the service, including `--no-invoker-iam-check`, which `roles/run.developer` cannot set |
| `roles/iam.serviceAccountUser` | the runtime service account only, and the build service account if you use one | deploy a service, and start a build, that run as those accounts |

First run only (setup), in addition:

| Role | Granted on | Why |
|---|---|---|
| `roles/serviceusage.serviceUsageAdmin` | project | enable the run, cloudbuild, artifactregistry and firestore APIs (and secretmanager with `--stripe`) |
| `roles/artifactregistry.admin` | project | check for and create the repository |
| `roles/datastore.owner` | project | check for and create the Firestore Native `(default)` database |
| `roles/storage.admin` | project | the first `gcloud builds submit` creates its source bucket |
| `roles/secretmanager.viewer` | each of the two Stripe secrets (`--stripe` only) | check that they exist. Metadata only; this role cannot read secret values |

Simplest is to have a project Owner do the first run once, then deploy with `--skip-setup` and the routine roles.

### Runtime service account (`--service-account`, for example `calotype-run@`)

| Role | Granted on | Why |
|---|---|---|
| `roles/datastore.user` | project | read and write the Firestore documents (keys, usage counts, processed Stripe events, prepaid credit and purchases) |
| `roles/secretmanager.secretAccessor` | `calotype-stripe-secret-key` and `calotype-stripe-webhook-secret` only, not the project (`--stripe` only) | Cloud Run resolves the two secret references at start |

Nothing else. Cloud Run sends stdout to Cloud Logging without a logging role, and the image is pulled by Cloud Run's own service agent. Do not use the Compute Engine default account; `deploy.sh` refuses it.

### Build service account (`--build-service-account`, for example `calotype-build@`; recommended)

| Role | Granted on | Why |
|---|---|---|
| `roles/artifactregistry.writer` | the `calotype` repository only | push the image |
| `roles/logging.logWriter` | project | write the build log |
| `roles/storage.objectViewer` | the Cloud Build source bucket only | read the uploaded source |

Without `--build-service-account`, Cloud Build uses the project's default build account, whose roles depend on the project's age and org policy.

Example grants, run once by an admin (none of these have been run):

```sh
P=PROJECT_ID; RUN=calotype-run@$P.iam.gserviceaccount.com
gcloud iam service-accounts create calotype-run --project $P
gcloud projects add-iam-policy-binding $P --member serviceAccount:$RUN --role roles/datastore.user
gcloud secrets add-iam-policy-binding calotype-stripe-secret-key --project $P \
  --member serviceAccount:$RUN --role roles/secretmanager.secretAccessor
gcloud secrets add-iam-policy-binding calotype-stripe-webhook-secret --project $P \
  --member serviceAccount:$RUN --role roles/secretmanager.secretAccessor
```

## Expected monthly cost at low volume

Every figure below is an **estimate**. They come from memory of Google's published free tiers and prices, not from the live pages: the pricing pages could not be read when this was written. Check them on the linked pages before relying on them. "Low volume" means up to about 3,000 screenshots a month, roughly the demand-test phase.

| Item | Assumption | Estimated cost |
|---|---|---|
| Cloud Run ([pricing](https://cloud.google.com/run/pricing)) | Request-based billing, min 0 instances. About 3 s per capture, so 3,000 captures are about 9,000 vCPU-seconds and 18,000 GiB-seconds, plus cold starts. Estimated free tier: 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests a month | $0 (estimate) |
| Network egress ([pricing](https://cloud.google.com/run/pricing)) | About 300 KB per image, so about 1 GB a month. Estimated 1 GiB free in North America, then about $0.12 per GiB | $0 to $0.15 (estimate) |
| Artifact Registry ([pricing](https://cloud.google.com/artifact-registry/pricing)) | The image is about 2 GB, mostly the Playwright base layers that tags share. Estimated 0.5 GB free, then about $0.10 per GB-month. Delete old tags to keep this flat | about $0.15 to $0.30 (estimate) |
| Cloud Build ([pricing](https://cloud.google.com/build/pricing)) | 5 to 10 minutes per build, a few builds a month, within the estimated free build minutes | $0 (estimate) |
| Firestore ([pricing](https://cloud.google.com/firestore/pricing)) | About 2 reads and 2 writes per screenshot. Estimated free tier: 50,000 reads and 20,000 writes a day, 1 GiB stored | $0 (estimate) |
| Secret Manager ([pricing](https://cloud.google.com/secret-manager/pricing)) | 2 secrets, read at instance start. Estimated free tier: 6 active versions and 10,000 accesses a month | $0 (estimate) |
| Cloud Logging ([pricing](https://cloud.google.com/stackdriver/pricing)) | One short JSON line per request, far below the estimated 50 GiB a month free | $0 (estimate) |
| **Total** | | **under $1 a month (estimate)** |

Stripe's fee is separate. It comes out of each payment and is not billed to the project: about 2.9% plus $0.30 per card charge in the US (estimate; see [stripe.com/pricing](https://stripe.com/pricing)), or about $0.85 on a $19 Starter payment.

Before the first deploy, set a billing budget with an alert on the project. That needs the billing account, so the founder has to do it. Cost grows with traffic mainly through Cloud Run time and egress. `--max-instances 2` caps how much can run at once.

## Not done

- Nothing has been run against a real project: no APIs enabled, no repo or database created, no image built, no IAM granted.
- No custom domain, no startup probe on `/healthz` (Cloud Run's default TCP probe is used), and no Artifact Registry cleanup policy.
