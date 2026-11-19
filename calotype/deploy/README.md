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
| `roles/datastore.user` | project | read and write the Firestore documents (keys, usage counts, processed Stripe events) |
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
