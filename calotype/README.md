# Calotype

A hosted website-screenshot API. Send a URL, get back a PNG, JPEG or WebP. Built and run by Skewbald, an AI agent. Why this product was picked: `research/decision.md` and `research/deep-dives/rank-1.md`.

Status: first version, run locally only. Not deployed. No domain. Stripe billing is written and tested against mocks, but no Stripe account is connected.

## Layout

| Path | What it is |
|---|---|
| `app.py` | FastAPI app: `/v1/screenshot` (GET and POST), `/v1/keys`, `/v1/keys/email`, `/v1/usage`, `/v1/plans`, `/v1/billing/*`, `/health` (also `/healthz`; Cloud Run keeps `/healthz` for itself on run.app addresses), and the website (`pages.py`) |
| `capture.py` | One Chromium, a fresh context per capture, a semaphore for concurrency |
| `egress.py` | The per-capture forward proxy that is Chromium's only route to the network |
| `ssrf.py` | The address rule: what may be fetched |
| `params.py` | Request parameters and limits |
| `storage.py` | The `Storage` interface, and keys (hashed) and usage counts in SQLite |
| `firestore_storage.py` | The same `Storage` in Google Cloud Firestore, for Cloud Run (`CALOTYPE_STORAGE=firestore`) |
| `ratelimit.py` | In-memory limit on self-serve key creation |
| `notices.py` | The optional email address on a key, and the 80% and 100% quota notices: validation, masking, the signed removal link |
| `billing.py` | Stripe Checkout, customer portal and webhook handling (off unless the Stripe variables are set) |
| `deploy/` | Cloud Build config, `deploy.sh` for Cloud Run, and the IAM roles and cost notes (`deploy/README.md`) |
| `admin.py` | Key administration CLI |
| `remote_mcp.py` | Remote MCP server at `/mcp` (streamable HTTP) with one tool, `screenshot` |
| `mcp_server.py` | Local MCP server (stdio) that calls the API |
| `plans.json` | Plans and monthly quotas. Prices are provisional |
| `adblock_domains.txt` | Domains blocked when `block_ads=true` |
| `pages.py`, `site/` | The public website: `site/layout.html` (shared header and footer) wraps each page in `site/pages/`; `pages.py` lists the routes (`/`, `/docs`, `/pricing`, `/acceptable-use`, `/privacy`, `/terms`, `/refunds`) and serves them with `/robots.txt` and `/sitemap.xml` |
| `tests/` | pytest suite |

## Run locally

Commands run from the repo root. `SCRATCH` is any folder outside the repo for the virtualenv, browsers and a local database:

```sh
SCRATCH=$HOME/.cache/calotype   # or anywhere you like
export TMPDIR=$SCRATCH/tmp && mkdir -p $TMPDIR
export PLAYWRIGHT_BROWSERS_PATH=$SCRATCH/pw-browsers
V=$SCRATCH/venv

# One-time setup
python3 -m venv $V
$V/bin/pip install -r calotype/requirements-dev.txt
$V/bin/playwright install chromium

# A database outside the repo, then an operator key (printed once)
export CALOTYPE_DB_PATH=$SCRATCH/calotype.db
$V/bin/python -m calotype.admin create-key --plan free --label "local test"

# Start the API on 127.0.0.1:8080
HOST=127.0.0.1 PORT=8080 $V/bin/python -m calotype
```

Then:

```sh
curl -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8080/v1/screenshot?url=https://example.com&width=1280&height=800" -o example.png
curl -X POST http://127.0.0.1:8080/v1/keys          # a self-serve free key
open http://127.0.0.1:8080/                         # landing page
```

Do not use `uvicorn ... --access-log`: uvicorn's access log prints query strings, which can carry `access_key`. `python -m calotype` turns it off.

## Test

```sh
$V/bin/python -m pytest calotype
```

The browser tests start Chromium and local HTTP servers on 127.0.0.1. Nothing reaches the internet: DNS in the tests goes through a fake resolver.

Every test that uses the `store` fixture (the storage contract in `tests/test_storage.py`, and the API and key tests) runs once per backend. The Firestore runs need the Firestore emulator and are skipped unless `FIRESTORE_EMULATOR_HOST` is set; they never touch a real project. The emulator is a Java program from the gcloud SDK's `cloud-firestore-emulator` component. To run it without installing anything system-wide, unpack the component into a scratch folder and start the jar directly (the URL and SHA-256 are in `https://dl.google.com/dl/cloudsdk/channels/rapid/components-2.json`, entry `cloud-firestore-emulator`):

```sh
E=$SCRATCH/fs-emulator
mkdir -p $E/x $E/jtmp $E/home
curl -o $E/emu.tar.gz https://dl.google.com/dl/cloudsdk/channels/rapid/components/google-cloud-sdk-cloud-firestore-emulator-20261030071842.tar.gz
sha256sum $E/emu.tar.gz   # f92f5fc07cc2d7e1f1e11045ee2b3908d3401c45aaed2ea6bd50b0a9898b6f3b
tar -xzf $E/emu.tar.gz -C $E/x
java -Djava.io.tmpdir=$E/jtmp -Duser.home=$E/home -Duser.language=en \
  -cp $E/x/platform/cloud-firestore-emulator/cloud-firestore-emulator.jar \
  com.google.cloud.datastore.emulator.firestore.CloudFirestore start \
  --host=127.0.0.1 --port=8681 --project_id=calotype-test &

FIRESTORE_EMULATOR_HOST=127.0.0.1:8681 $V/bin/python -m pytest calotype
```

Each test writes under its own collection prefix and deletes it afterwards.

## API

`GET /v1/screenshot?url=...` or `POST /v1/screenshot` with a JSON body. Auth is `Authorization: Bearer <key>` or `?access_key=<key>`.

| Parameter | Default | Limits |
|---|---|---|
| `url` | required | http or https, public address, up to 2,048 characters, no `user:pass@` |
| `width`, `height` | 1280, 800 | 100 to 3840, 100 to 2160 |
| `full_page` | false | captures up to 20,000 px tall (WebP: 16,383 px, a format limit), and at most 80 megapixels |
| `format` | png | png, jpeg (or jpg), webp |
| `quality` | 80 | 1 to 100, jpeg and webp only |
| `device_scale_factor` | 1 | 1 to 3 |
| `delay_ms` | 0 | 0 to 10,000 |
| `wait_until` | load | load, domcontentloaded, networkidle, commit |
| `block_ads` | false | blocks the domains in `adblock_domains.txt` and their subdomains |
| `dark_mode` | false | sets `prefers-color-scheme: dark` |

Unknown parameters are refused, so typos fail loudly. A capture has 30 seconds in total. The image comes back with headers `X-Calotype-Usage` (`used/limit`), `X-Calotype-Target-Status` and, if the page hit the byte cap, `X-Calotype-Truncated: 1`.

Errors are JSON, `{"error": {"code": ..., "message": ...}}`:

| Status | Code | When |
|---|---|---|
| 400 | `invalid_parameters`, `invalid_url`, `invalid_scheme`, `blocked_target`, `dns_error` | bad input, or a URL that is refused before rendering |
| 401 | `missing_key`, `invalid_key` | no key, unknown or revoked key |
| 422 | `blocked_target`, `target_unreachable`, `page_too_large` | the page, a redirect or the network refused or failed during rendering |
| 429 | `quota_exceeded` (with `limit`, `used`, `resets_at`), `rate_limited` | monthly quota used up; too many keys created |
| 503 | `busy` | every renderer busy for 10 s; has `Retry-After` |
| 504 | `timeout` | the 30-second limit was hit |

### Paid plans (Stripe)

| Endpoint | Auth | What it does |
|---|---|---|
| `POST /v1/billing/checkout` | a free key | Body `{"plan": "starter"}` or `{"plan": "pro"}`. Creates a Stripe Checkout Session in subscription mode and returns `{"url": ...}`; send the buyer there |
| `GET /v1/billing/portal` | a key that has paid | Returns `{"url": ...}` for Stripe's customer portal: change card or plan, or cancel |
| `POST /v1/billing/webhook` | Stripe's signature | Receives Stripe events |

The session carries the key's internal id (as `client_reference_id` and in metadata), never the key or its hash. The webhook verifies the `Stripe-Signature` header against `STRIPE_WEBHOOK_SECRET`. For `checkout.session.completed` and `customer.subscription.created`, `.updated` and `.deleted`, it reads the subscription's current state from Stripe, maps its price to a plan (`active`, `trialing` and `past_due` keep the paid plan; anything else means free), and stores the plan with the Stripe customer id and subscription id. Handled event ids are stored, so a repeated delivery changes nothing. If Stripe cannot be reached, the webhook answers 500 and records nothing, so Stripe retries. An ended subscription that is not the key's current one is ignored.

Billing errors: 400 `invalid_plan`, `invalid_json`, `invalid_webhook`; 404 `no_billing_account` (portal before any payment); 409 `already_subscribed` (checkout for a key that is already paid; use the portal); 502 `billing_error` (Stripe refused or was unreachable); 503 `billing_unavailable` when the Stripe variables are not set. Everything else works without them.

Quota counts one screenshot per call, per key, per calendar month in UTC. A call is counted before rendering and refunded if it fails, so failed captures are free. Refused URLs are rejected before counting.

## How SSRF is blocked

The rule (`ssrf.py`) allows only public unicast addresses. It refuses loopback, private (RFC 1918), CGNAT, link-local and cloud metadata (169.254.169.254, `fd00:ec2::254`, 100.100.100.200), unique-local `fc00::/7`, multicast, reserved, documentation and benchmarking ranges, IPv4-mapped, NAT64 and 6to4 forms that embed a blocked IPv4 address, IPv6 zone IDs, and anything Python's `ipaddress` does not call global. Names such as `localhost`, `*.internal` and `metadata.google.internal` are refused before any lookup. Legacy IPv4 forms that browsers accept (`2130706433`, `0x7f.1`, `0177.0.0.1`) are parsed as addresses. Below port 1024, only 80 and 443 are allowed. A host is refused if any of its DNS records is disallowed.

Three layers apply that rule:

1. **Before rendering.** The API checks the scheme, host and port and resolves the name. Only `http` and `https` pass, so `file:`, `data:`, `chrome:`, `javascript:` and the rest are refused with 400.
2. **In the page.** A Playwright route on the context aborts any request whose scheme is not http, https, data or blob, and any request to an IP literal or name that is plainly disallowed.
3. **At the network edge.** Each capture gets its own forward proxy on 127.0.0.1 (`egress.py`), and the browser context is pointed at it with loopback included (`<-loopback>`). Every connection Chromium makes goes through it: navigations, redirects, sub-resources, iframes, fetch and XHR, WebSockets, beacons, popups. The proxy resolves the host itself, applies the rule, and connects to the exact address it checked. That closes DNS rebinding, and it covers redirects, which Playwright routes do not see. It also counts bytes and resets the connection when a capture passes `CALOTYPE_MAX_PAGE_BYTES` (50 MB by default).

Also: service workers are blocked, downloads are refused, WebRTC may not use UDP outside the proxy, QUIC and DNS prefetch are off, and each context is closed after its capture. The tests prove this with a "canary" server on another loopback port. A page that tries to reach it by `127.0.0.1`, `localhost`, `[::1]`, a decimal IP, a public-looking name that resolves to loopback, a redirect or a meta refresh gets no request through. A control test shows that the same page does reach the canary once it is allowed.

The only way past the rule is `AddressPolicy.exceptions`, a set of exact `(ip, port)` pairs that tests use for their own local server. It can be set only in code, never from the environment.

## Keys, privacy and the demand-test contacts

- **Self-serve keys.** `POST /v1/keys`, or the button on the landing page, creates a free-plan key and returns it once. It asks for nothing: no email, no name, no account.
- **What is stored.** In either backend (SQLite or Firestore), for each key: the SHA-256 hash (never the key), plan, creation time, an `internal` flag, an optional operator label (CLI keys only), a lifetime count of successful screenshots, per-month usage counts, a revocation time, and, once a key has paid, its Stripe customer id and subscription id. Also the ids of Stripe events already handled. No IP address or name is stored anywhere (Stripe holds the buyer's details), no email address unless the holder adds one (below), and logs carry no IPs or addresses.
- **Quota emails (optional).** `POST /v1/keys` takes `{"email": ...}`; `POST /v1/keys/email` sets or replaces it and `DELETE /v1/keys/email` removes it (both with the key). The address is used only to say when the key reaches 80% and 100% of its monthly quota. No endpoint returns it in full; `/v1/usage` shows `s***@example.com` and `notices`. When a capture's count reaches the 80% or the 100% mark, the service queues a notice (`notices` table or `{prefix}notices` collection: key id, month, threshold, counts, the upgrade line the MCP tool uses; never the address). It does not send mail. The `skewbald-checks` job in the free-reign repository (`ops/checks/main.py`) sends pending notices through AgentMail every 15 minutes, at most 4 per address per month across all keys, and marks each sent only after a 2xx. Each notice ends with `GET /v1/keys/email/remove?token=...` (also POST, for mail clients' one-click unsubscribe), whose token is an HMAC of the key id and the address under `CALOTYPE_EMAIL_TOKEN_SECRET`. Without that setting no address is stored and nothing is queued. Revoking or deleting a key removes its address; `python -m calotype.admin remove-email ADDRESS_OR_ID` handles a "stop" reply.
- **Rate limit.** Key creation is limited to 3 per client IP per hour and 200 per hour overall. Both limits live only in process memory (`ratelimit.py`), are never persisted or logged, and reset when the process restarts. With several instances, each instance keeps its own count.
- **Contact ordinal.** A "contact" is a non-internal key with at least one successful screenshot. Its ordinal is its creation order among those keys. Keys made with `python -m calotype.admin create-key` are `internal` and never count. A test key made through the public button (such as key 1 on the live service) is marked the same way with `python -m calotype.admin mark-internal ID`. The demand test counts the first 20. List them with `python -m calotype.admin contacts`. The API also logs `first successful screenshot` with the ordinal when a key becomes a contact. The ordinal is derived on read, so a key created earlier that takes its first screenshot later moves ahead of later-created keys.

Other CLI commands: `list-keys`, `revoke-key ID`, `delete-key ID`, `key-id`, `mark-internal ID`, `set-plan ID PLAN`, `usage ID`, `remove-email ADDRESS_OR_ID`.

`revoke-key` on a paid key cancels its Stripe subscription first: at once, with no prorated refund, or with `--at-period-end` at the end of the month already paid for. It needs `STRIPE_SECRET_KEY` in the environment and refuses without it, unless `--no-stripe` is given; then it revokes the key and warns that the subscription must be cancelled by hand. Running it again on a revoked key that still has a subscription retries the cancellation.

`delete-key ID` handles a request to delete a key's record (the privacy notice promises it within 7 days). Without `--yes` it only prints what it would delete. With `--yes` it cancels a paid key's subscription exactly as `revoke-key` does (same `--at-period-end` and `--no-stripe` flags), then deletes the key's record (with any email address), all its usage counts and its quota notices on either backend, and prints a note that Stripe keeps its own payment records. Ids are not reused. To find a key's id from the key someone sent, pipe the key into `python -m calotype.admin key-id` (it reads standard input, so the key stays out of the shell history).

## MCP server

The service is a remote MCP server at `POST /mcp` (`remote_mcp.py`): streamable HTTP, stateless, JSON responses, built on the official `mcp` SDK. The caller's key goes in `Authorization: Bearer <key>` and is checked like `/v1/screenshot`'s; the one tool, `screenshot(url, full_page, width, height, format, dark_mode, block_ads)`, runs through the same `capture_for_key` path as the API (plan, limits, SSRF policy, quota, refunds, logs). No key answers 401 with how to get one; GET and DELETE answer 405. Client setup is on `/docs#mcp`. `deploy/mcp-registry/server.json` is the (unpublished) MCP Registry entry.

```sh
claude mcp add --transport http calotype https://calotype.dev/mcp --header "Authorization: Bearer cal_..."
```

There is also a local stdio server, for development against a local API:

```json
{"mcpServers": {"calotype": {
  "command": "/path/to/venv/bin/python", "args": ["-m", "calotype.mcp_server"],
  "env": {"CALOTYPE_API_KEY": "cal_...", "CALOTYPE_API_URL": "http://127.0.0.1:8080"}}}}
```

It runs from a checkout of this repo (the working directory must contain `calotype/`). It has one tool, `screenshot(url, width, height, full_page, format, dark_mode, block_ads, delay_ms)`. It defaults to JPEG with ads blocked, to keep images small for model context. It is not published to PyPI; use the remote endpoint instead.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT`, `HOST` | 8080, 0.0.0.0 | listen address (`python -m calotype`) |
| `CALOTYPE_STORAGE` | sqlite | `sqlite` or `firestore` |
| `CALOTYPE_DB_PATH` | `calotype/data/calotype.db` | SQLite file. Or `CALOTYPE_DATABASE_URL=sqlite:///path` |
| `CALOTYPE_FIRESTORE_PROJECT` | the credentials' project | Google Cloud project of the Firestore database |
| `CALOTYPE_FIRESTORE_COLLECTION_PREFIX` | `calotype_` | prefix of the collection names, so two deployments can share a database |
| `CALOTYPE_PLANS_FILE` | `calotype/plans.json` | plans and quotas |
| `CALOTYPE_BLOCKLIST_FILE` | `calotype/adblock_domains.txt` | ad domains |
| `CALOTYPE_MAX_CONCURRENCY` | 4 | captures at once per instance |
| `CALOTYPE_QUEUE_TIMEOUT_S` | 10 | wait for a free renderer before 503 |
| `CALOTYPE_CAPTURE_TIMEOUT_S` | 30 | per capture, capped at 30 |
| `CALOTYPE_MAX_PAGE_BYTES` | 52428800 | network bytes per capture |
| `CALOTYPE_SELF_SERVE_KEYS` | 1 | 0 turns off `POST /v1/keys` |
| `CALOTYPE_KEYS_PER_IP_PER_HOUR` | 3 | self-serve keys per client IP |
| `CALOTYPE_KEYS_GLOBAL_PER_HOUR` | 200 | self-serve keys overall |
| `CALOTYPE_TRUSTED_PROXY_HOPS` | 0 | proxies whose `X-Forwarded-For` entry is trusted. Set 1 on Cloud Run |
| `CALOTYPE_LOG_LEVEL` | INFO | |
| `CALOTYPE_SERVE_SITE` | 1 | 0 to skip the landing page |
| `CALOTYPE_PUBLIC_URL` | from the request | public origin (`https://host`) that Stripe sends buyers back to |
| `STRIPE_SECRET_KEY` | | Stripe API secret key. Secret: Secret Manager on Cloud Run; a local secret store when run by hand |
| `STRIPE_WEBHOOK_SECRET` | | the webhook endpoint's signing secret (`whsec_...`). Secret |
| `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | | the monthly Price ids for each plan |
| `CALOTYPE_EMAIL_TOKEN_SECRET` | | signs the removal links in quota emails. Secret, at least 32 characters; the sender job needs the same value. Unset: no address is stored and no notice is queued |
| `CALOTYPE_API_KEY`, `CALOTYPE_API_URL` | | MCP server only |

No secrets are needed to run the API. Billing needs all four `STRIPE_*` variables; with any missing it stays off and `/v1/billing/*` answers 503. Firestore uses the environment's default Google credentials (on Cloud Run, the service's service account); no key file is used, and none belongs in this repo. The landing-page prices are provisional (a comment in the HTML says so). `plans.json` is the source of truth for quotas.

## Logging

One JSON object per line on stdout, with a `severity` field that Cloud Logging reads. Each request logs method, path (never the query string), status, duration, key id and target as `scheme://host` only. A redaction pass in the formatter also strips URL paths and queries, `access_key=` values, bearer tokens and `cal_` keys from every message and traceback. uvicorn's access log is off, and httpx logs are raised to WARNING.

## Deploy to Google Cloud Run (notes; not done)

`deploy/deploy.sh` builds the image in Cloud Build, pushes it to Artifact Registry and deploys it. `deploy/README.md` has the IAM roles and the cost estimate. Try it with `--dry-run` first, which only prints the gcloud commands:

```sh
calotype/deploy/deploy.sh --dry-run --project PROJECT_ID \
  --service-account calotype-run@PROJECT_ID.iam.gserviceaccount.com
```

- The image is `mcr.microsoft.com/playwright/python:v1.65.2-noble`, with Python 3.12 and Chromium included. Keep its tag equal to the `playwright` pin. It runs as the unprivileged `pwuser`.
- Set the Cloud Run `--concurrency` equal to `CALOTYPE_MAX_CONCURRENCY`, so requests queue at the load balancer, not in the app. Each Chromium page needs roughly 200 to 500 MB.
- **Storage: use Firestore.** SQLite on Cloud Run's disk is lost on every restart and is not shared between instances. Set `CALOTYPE_STORAGE=firestore`. Firestore lives in the same Google Cloud project, so no other account is needed. One-time setup (notes; none of this has been run):
  - Create the database in **Native mode** (not Datastore mode), as the project's `(default)` database: `gcloud firestore databases create --location=us-central1 --type=firestore-native`. The free tier covers one database per project; use the `(default)` one. Put it in or near the Cloud Run region.
  - Give the Cloud Run service its own service account and grant it **`roles/datastore.user`** on the project (`gcloud projects add-iam-policy-binding PROJECT_ID --member=serviceAccount:calotype-run@PROJECT_ID.iam.gserviceaccount.com --role=roles/datastore.user`). That role reads and writes documents and nothing more. The client finds the credentials itself through the metadata server; do not create or mount a key file.
  - `CALOTYPE_FIRESTORE_PROJECT` is only needed if the database is in another project than the service. `CALOTYPE_FIRESTORE_COLLECTION_PREFIX` (default `calotype_`) separates, say, staging from production in one database.
  - No index needs deploying: every query uses one field, which Firestore indexes automatically.
  - Cost: a successful screenshot is 2 document reads (key lookup, quota check) and 2 writes (count, lifetime success); a failed capture adds one write for the refund; a refusal at the quota is 1 read and no write; a new key is 2 writes. Firestore's free tier is 50,000 reads and 20,000 writes a day and 1 GiB stored, so about 10,000 screenshots a day cost nothing.
  - With Firestore, keys and quotas are shared across instances, so `--max-instances` can go above 1. The key-creation rate limit and the concurrency limit stay per instance.
  - Without Firestore, the fallback is one instance (`--max-instances 1`) with SQLite on a mounted volume, accepting the risk of loss.
- `/health` and `/healthz` return 503 if Chromium is not connected. Use either as the startup and liveness probe; from outside a run.app address, only `/health` reaches the app.
- Cloud Run egress is the public internet, but the metadata server (`metadata.google.internal`, 169.254.169.254) is reachable from the container. The SSRF rule blocks it. Do not add a Serverless VPC connector: without one, private networks are not even routable, which is a second line behind the rule.
- Chromium runs without its own sandbox, as is usual in containers. Cloud Run's gen2 environment provides the isolation boundary.

## Known gaps

- Billing is tested against a mocked Stripe client. Live Stripe has run since 17 November 2026; a server without Stripe settings keeps billing off (503). `plans.json` prices must be kept equal to the Stripe Prices by hand.
- The Stripe processed-event list grows without bound (one small row per event). Firestore TTL or a periodic delete can trim it later. So do quota-notice records (at most two per key per month), until the key is deleted.
- `admin remove-email ADDRESS` matches the address as stored: the domain in any case, the local part exactly.
- A removal link is a GET that acts at once, as asked. A mail scanner that opens links could remove an address; the holder can add it again.
- The Firestore backend is tested against the Firestore emulator only, not against a real Firestore database. Nothing moves data from SQLite to Firestore (there is no deployed data to move).
- The image is built by Cloud Build (`deploy/cloudbuild.yaml`), not locally; the Playwright base image is about 2 GB.
- No PDF output, no element selector, no custom headers or cookies, and no caching of repeat captures.
- `adblock_domains.txt` is a short hand-kept list, not EasyList.
- Rate limits and the concurrency limit are per instance.
