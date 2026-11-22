# Calotype

A hosted website-screenshot API. Send a URL, get back a PNG, JPEG or WebP. Built and run by Skewbald, an AI agent. Why this product was picked: `research/decision.md` and `research/deep-dives/rank-1.md`.

Status: first version, run locally only. Not deployed. No domain. Stripe billing is written and tested against mocks, but no Stripe account is connected.

## Layout

| Path | What it is |
|---|---|
| `app.py` | FastAPI app: `/v1/screenshot` (GET and POST), `/v1/keys`, `/v1/keys/email`, `/v1/usage`, `/v1/plans`, `/v1/billing/*`, `/v1/credits`, `/health` (also `/healthz`; Cloud Run keeps `/healthz` for itself on run.app addresses), and the website (`pages.py`) |
| `capture.py` | One Chromium, a fresh context per capture, a semaphore for concurrency |
| `egress.py` | The per-capture forward proxy that is Chromium's only route to the network |
| `ssrf.py` | The address rule: what may be fetched |
| `params.py` | Request parameters and limits |
| `storage.py` | The `Storage` interface, and keys (hashed) and usage counts in SQLite |
| `firestore_storage.py` | The same `Storage` in Google Cloud Firestore, for Cloud Run (`CALOTYPE_STORAGE=firestore`) |
| `ratelimit.py` | In-memory per-IP limits on self-serve key creation and free tries |
| `tries.py` | `/try`: the home page's free try, a screenshot with no key (form in `site/try-form.html`) |
| `notices.py` | The optional email address on a key, and the 80% and 100% quota notices: validation, masking, the signed removal link |
| `billing.py` | Stripe Checkout, customer portal and webhook handling (off unless the Stripe variables are set) |
| `credits.py` | Prepaid screenshots bought by an agent through Stripe machine payments, `POST /v1/credits` (off unless `CALOTYPE_CREDITS=1` and its settings are set) |
| `mpp.py` | The Machine Payments Protocol's wire format: the 402 challenge, the credential, the receipt |
| `deploy/` | Cloud Build config, `deploy.sh` for Cloud Run, and the IAM roles and cost notes (`deploy/README.md`) |
| `admin.py` | Key administration CLI |
| `remote_mcp.py` | Remote MCP server at `/mcp` (streamable HTTP) with one tool, `screenshot` |
| `oauth.py` | OAuth sign-in for `/mcp`: discovery documents, client registration, the consent page at `/oauth/authorize`, `/oauth/token` (off unless `CALOTYPE_OAUTH_SECRET` is set) |
| `mcp_server.py` | Local MCP server (stdio) that calls the API |
| `install_links.py` | Builds the one-click Cursor and VS Code install links in the README and on `/agents`; `python -m calotype.install_links` prints them with their decoded configs |
| `plans.json` | Plans and monthly quotas, and the prepaid packs (`credit_packs`). Prices are provisional |
| `adblock_domains.txt` | Domains blocked when `block_ads=true` |
| `pages.py`, `site/` | The public website: `site/layout.html` (shared header and footer) wraps each page in `site/pages/`; `pages.py` lists the routes (`/`, `/docs`, `/pricing`, `/acceptable-use`, `/privacy`, `/terms`, `/refunds`) and serves them with `/robots.txt` and `/sitemap.xml`, the logo files in `site/brand/` at `/icon.svg` and `/icon.png`, and `site/images/` at `/images/<name>` (the README links there, so add a new name rather than replacing a file) |
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

### Prepaid credit (Stripe machine payments; off by default)

`POST /v1/credits` with a key and `{"pack": "starter"}` lets an agent buy 5,000 screenshots for $19 with no person and no checkout page, over the Machine Payments Protocol (MPP). It has never run against Stripe and is off unless `CALOTYPE_CREDITS=1` and its settings are set; then it is also listed in `/openapi.json` and the packs in `/v1/plans`. Off, it answers 503 `billing_unavailable`.

- **Unpaid:** 402 with `WWW-Authenticate: Payment ...`, a challenge for the pack's price (method `stripe`, intent `charge`, amount in cents, the Stripe profile id), valid five minutes and bound to the key and the pack by an HMAC id. The body is problem details plus the usual `error` object.
- **Paid:** the same request with the credential in `Payment-Authorization: Payment <base64url>` (the key stays in `Authorization`; with the key in `?access_key=` the credential goes in `Authorization`). The credential holds a Stripe shared payment token. Calotype creates and confirms one PaymentIntent with it and, if it succeeded, adds the pack: 200 with `credits` (the new balance) and a `Payment-Receipt` header.
- **Once:** the purchase is stored under the PaymentIntent id in the same atomic step that adds the screenshots. The same credential again, the `payment_intent.succeeded` webhook, or a webhook replay add nothing more.
- **Use:** the month's plan quota is used first, then credit, one per screenshot (`X-Calotype-Credits` on the response). Credit does not expire or reset. `GET /v1/usage` reports it as `credits`. Credit already bought keeps working if the endpoint is switched off again.
- **Not built:** stablecoin payments. `CALOTYPE_CREDITS_STABLECOIN=1` only logs a warning.

A key may send 10 payment credentials an hour (`CALOTYPE_CREDIT_PAYMENTS_PER_KEY_PER_HOUR`; in memory, per instance), because each one that passes the checks makes a call to Stripe; asking for a challenge is not limited.

Errors: 400 `invalid_pack`, `invalid_json`; 429 `rate_limited`; 402 `payment_required`, and for a refused credential `malformed_credential`, `invalid_challenge`, `payment_expired`, `verification_failed`, each with a new challenge; 502 `payment_unconfirmed` (Stripe gave no answer; the same credential may be sent again), `billing_error`; 503 `billing_unavailable`. Settings, the steps a person must take in Stripe, what is unverified, and refunds are in `deploy/README.md`, "Prepaid credit". The sandbox run is in `deploy/stripe-sandbox/README.md`.

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
- **What is stored.** In either backend (SQLite or Firestore), for each key: the SHA-256 hash (never the key), plan, creation time, an `internal` flag, an optional operator label (CLI keys only), a lifetime count of successful screenshots, per-month usage counts, a revocation time, and, once a key has paid, its Stripe customer id and subscription id. Also the ids of Stripe events already handled. For prepaid credit: the key's balance, and per purchase the Stripe PaymentIntent id, pack, amount and times. No IP address or name is stored anywhere (Stripe holds the buyer's details), no email address unless the holder adds one (below), and logs carry no IPs or addresses.
- **Quota emails (optional).** `POST /v1/keys` takes `{"email": ...}`; `POST /v1/keys/email` sets or replaces it and `DELETE /v1/keys/email` removes it (both with the key). The address is used only to say when the key reaches 80% and 100% of its monthly quota. No endpoint returns it in full; `/v1/usage` shows `s***@example.com` and `notices`. When a capture's count reaches the 80% or the 100% mark, the service queues a notice (`notices` table or `{prefix}notices` collection: key id, month, threshold, counts, the upgrade line the MCP tool uses; never the address). It does not send mail. The `skewbald-checks` job in the free-reign repository (`ops/checks/main.py`) sends pending notices through AgentMail every 15 minutes, at most 4 per address per month across all keys, and marks each sent only after a 2xx. Each notice ends with `GET /v1/keys/email/remove?token=...` (also POST, for mail clients' one-click unsubscribe), whose token is an HMAC of the key id and the address under `CALOTYPE_EMAIL_TOKEN_SECRET`. Without that setting no address is stored and nothing is queued. Revoking or deleting a key removes its address; `python -m calotype.admin remove-email ADDRESS_OR_ID` handles a "stop" reply.
- **Rate limit.** Key creation is limited to 3 per client IP per hour and 200 per hour overall. Both limits live only in process memory (`ratelimit.py`), are never persisted or logged, and reset when the process restarts. With several instances, each instance keeps its own count.
- **Free tries.** The box at the top of the home page is a plain form (no script needed) that posts the address to `POST /try`. The answer is a site page with the screenshot inline as a `data:` URL, which the site's CSP already allowed (`img-src 'self' data:`); the policy itself is unchanged, only the inline hashes moved. The visitor picks only the address: 1280 × 800, not full page, JPEG at quality 70, ads blocked, normal timeouts; a bare `example.com` gets `https://`. Limits: 5 tries per client IP per hour, in memory like key creation (same `CALOTYPE_TRUSTED_PROXY_HOPS` logic, never stored or logged), and 300 renders per UTC day for everyone, counted in storage so every instance shares it. The address policy runs as for a keyed call, before the daily cap, so a refused address takes no render slot and shows the keyed call's message. A render slot is not given back when the capture fails. A form posted from another site (`Sec-Fetch-Site: cross-site`) is refused. The image is in that one response (`Cache-Control: no-store`) and kept nowhere. Each try adds to the day's counts (`total`, `rendered`, and one of `succeeded`, `failed`, `refused`, `limited`, `capped`; no URL, host or IP) and logs one `try` line like a screenshot's. `GET /try` shows the form (`?url=` only fills the box in). `python -m calotype.admin tries [--days N]` prints the counts. Under the image: `curl -X POST https://calotype.dev/v1/keys -d '{"ref":"try"}'` (the key is labelled `ref:try`), the MCP URL `/mcp?ref=try` (labels an untagged key on first use) and `/agents?ref=try` (whose key button sends `ref`).
- **OAuth sign-in.** With `CALOTYPE_OAUTH_SECRET` set, an MCP client can sign in instead of sending a key (see "MCP server" below). Registering a client stores nothing: the client id is the registration (name, redirect URIs, auth method, time) plus an HMAC. Approving on the consent page creates a free key exactly like `POST /v1/keys` (the same in-memory per-IP and global limits, shared with it), labelled `ref:oauth-<slug of the client's name>`; a pasted key is checked and, if it has no label, gets the same tag. The only stored record is the authorization code (`oauth_codes` table or `{prefix}oauth_codes` collection): the SHA-256 of the code, the SHA-256 of the client id, the redirect URI, the PKCE challenge, the resource and the key XORed with a pad derived from the code, so the record yields no key without the code. It is deleted when redeemed; one never redeemed expires after 60 seconds and is deleted when the next code is saved. Registrations are limited per client IP (in memory, like key creation). The consent page sets no cookie. Logs carry the client's tag, the outcome and the key id, never the key, the code or the redirect URI.
- **Contact ordinal.** A "contact" is a non-internal key with at least one successful screenshot. Its ordinal is its creation order among those keys. Keys made with `python -m calotype.admin create-key` are `internal` and never count. A test key made through the public button (such as key 1 on the live service) is marked the same way with `python -m calotype.admin mark-internal ID`. The demand test counts the first 20. List them with `python -m calotype.admin contacts`. The API also logs `first successful screenshot` with the ordinal when a key becomes a contact. The ordinal is derived on read, so a key created earlier that takes its first screenshot later moves ahead of later-created keys.

Other CLI commands: `list-keys`, `revoke-key ID`, `delete-key ID`, `key-id`, `mark-internal ID`, `set-plan ID PLAN`, `usage ID`, `remove-email ADDRESS_OR_ID`, `tries [--days N]`, `credits ID`, `refund-credit pi_...` (prepaid credit; see `deploy/README.md`).

`revoke-key` on a paid key cancels its Stripe subscription first: at once, with no prorated refund, or with `--at-period-end` at the end of the month already paid for. It needs `STRIPE_SECRET_KEY` in the environment and refuses without it, unless `--no-stripe` is given; then it revokes the key and warns that the subscription must be cancelled by hand. Running it again on a revoked key that still has a subscription retries the cancellation.

`delete-key ID` handles a request to delete a key's record (the privacy notice promises it within 7 days). Without `--yes` it only prints what it would delete. With `--yes` it cancels a paid key's subscription exactly as `revoke-key` does (same `--at-period-end` and `--no-stripe` flags), then deletes the key's record (with any email address), all its usage counts, its quota notices, its prepaid credit and its purchase records on either backend, and prints a note that Stripe keeps its own payment records. Ids are not reused. To find a key's id from the key someone sent, pipe the key into `python -m calotype.admin key-id` (it reads standard input, so the key stays out of the shell history).

## MCP server

The service is a remote MCP server at `POST /mcp` (`remote_mcp.py`): streamable HTTP, stateless, JSON responses, built on the official `mcp` SDK. The caller's key goes in `Authorization: Bearer <key>` and is checked like `/v1/screenshot`'s; the one tool, `screenshot(url, full_page, width, height, format, dark_mode, block_ads)`, runs through the same `capture_for_key` path as the API (plan, limits, SSRF policy, quota, refunds, logs). With no key, `initialize`, `tools/list` and `ping` still work, so directories can inspect the server; a wrong or revoked key answers 401; GET and DELETE answer 405. Client setup is on `/docs#mcp`. `deploy/mcp-registry/server.json` is the (unpublished) MCP Registry entry.

```sh
claude mcp add --transport http calotype https://calotype.dev/mcp --header "Authorization: Bearer cal_..."
```

### OAuth sign-in (`oauth.py`)

For clients that sign in only with OAuth (claude.ai and ChatGPT connectors), Calotype is its own authorization server, per the MCP authorization spec 2025-11-25. It is off, and every path below answers 404, unless `CALOTYPE_OAUTH_SECRET` (at least 32 characters) is set.

| Path | What it does |
|---|---|
| `/.well-known/oauth-protected-resource`, `.../oauth-protected-resource/mcp` | RFC 9728 metadata: `resource` is `<origin>/mcp` (with `?ref=<tag>` when asked with one), `authorization_servers` is the origin |
| `/.well-known/oauth-authorization-server`, `.../oauth-authorization-server/mcp` | RFC 8414 metadata: S256 only, `client_id_metadata_document_supported`, `token_endpoint_auth_methods_supported` `none`, `client_secret_post`, `client_secret_basic`, `iss` in responses |
| `POST /oauth/register` | RFC 7591 registration, stateless (the client id is the signed registration). 20 per client IP per hour |
| `GET /oauth/authorize` | The consent page: names the client and the redirect host; Approve makes a free key, "Use this key" takes a pasted one, Deny returns `access_denied` |
| `POST /oauth/token` | `authorization_code` with PKCE; returns `{"access_token": "cal_...", "token_type": "Bearer"}` |

- **Clients.** A registered client (`c1.` ids), or a Client ID Metadata Document: a `client_id` that is an https URL is fetched through `AddressPolicy` (resolved, checked and connected to by address, TLS to the original name, no redirects, 16 KB, 5 s) and cached in memory for 5 minutes. Documents must list `none` among their auth methods (Claude Code's and ChatGPT's do). Claude and ChatGPT prefer such documents when the metadata advertises them, so they do not register on every connection.
- **Redirect URIs.** Exact match, except loopback (`http://127.0.0.1`, `http://localhost`, `http://[::1]`), which matches on any port. Only https or loopback http; never a fragment. Custom schemes (such as `cursor://`) are refused, as the MCP spec requires.
- **PKCE and resource.** `code_challenge_method=S256` is required. `resource`, when sent, must name the origin or `/mcp`; anything else is `invalid_target`.
- **Codes.** Random, single use, 60 seconds, in `Storage`, so any instance can redeem them. A failed exchange still spends the code.
- **Token.** The Calotype key itself, so `/mcp` needs no change to accept it. No `expires_in`, no refresh token: the key works until revoked; then `/mcp` answers 401 and the client signs in again. Claude refreshes only on a 401 or a stated expiry, so it never asks for a refresh that does not exist.
- **Consent form.** No cookie. The hidden fields carry an HMAC over the request and the time (15 minutes), checked on submit; a submit with `Sec-Fetch-Site: cross-site` or a foreign `Origin` is refused. `frame-ancestors 'none'` and `X-Frame-Options: DENY`. The page's `form-action` allows the redirect URI's origin, because browsers apply it to the redirect after the form.
- **/mcp with OAuth on.** Every 401 carries `WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource/mcp"` (`error="invalid_token"` for a wrong key). A keyless `tools/call` answers that 401, with a JSON-RPC error saying how to get a key, instead of the tool error: claude.ai starts sign-in only on an HTTP 401 and ignores the header on a 200 (Anthropic's connector docs, "Lazy authentication"). `initialize`, `tools/list` and `ping` still work without a key. `CALOTYPE_MCP_KEYLESS_CALL=result` brings back the tool error for keyless calls, if a directory needs it, at the cost of claude.ai never offering sign-in.

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
| `CALOTYPE_CREDIT_PAYMENTS_PER_KEY_PER_HOUR` | 10 | payment credentials one key may send to `POST /v1/credits` |
| `CALOTYPE_TRIES_PER_IP_PER_HOUR` | 5 | free tries per client IP (in memory) |
| `CALOTYPE_TRIES_PER_DAY` | 300 | free-try renders per UTC day for everyone (in storage); 0 turns the box off |
| `CALOTYPE_TRUSTED_PROXY_HOPS` | 0 | proxies whose `X-Forwarded-For` entry is trusted. Set 1 on Cloud Run |
| `CALOTYPE_LOG_LEVEL` | INFO | |
| `CALOTYPE_SERVE_SITE` | 1 | 0 to skip the landing page |
| `CALOTYPE_PUBLIC_URL` | from the request | public origin (`https://host`) that Stripe sends buyers back to |
| `STRIPE_SECRET_KEY` | | Stripe API secret key. Secret: Secret Manager on Cloud Run; a local secret store when run by hand |
| `STRIPE_WEBHOOK_SECRET` | | the webhook endpoint's signing secret (`whsec_...`). Secret |
| `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PRO` | | the monthly Price ids for each plan |
| `CALOTYPE_CREDITS`, `CALOTYPE_CREDITS_CARD` | off | `1` and `1` turn on `POST /v1/credits` (prepaid credit, cards through shared payment tokens). Also needs `STRIPE_SECRET_KEY` and `STRIPE_PROFILE_ID` |
| `STRIPE_PROFILE_ID` | | the Stripe profile id (`profile_...`) that buyers' tokens are granted to. Not secret |
| `MPP_SECRET_KEY`, `MPP_REALM`, `STRIPE_MPP_API_VERSION`, `STRIPE_SPT_PAYMENT_METHOD_TYPES` | derived, the public host, `2026-07-29.preview`, `card,link` | optional; see `deploy/README.md` |
| `CALOTYPE_EMAIL_TOKEN_SECRET` | | signs the removal links in quota emails. Secret, at least 32 characters; the sender job needs the same value. Unset: no address is stored and no notice is queued |
| `CALOTYPE_OAUTH_SECRET` | | signs OAuth client ids, client secrets and the consent form (`oauth.py`). Secret, at least 32 characters, the same on every instance. Unset: OAuth is off and its paths answer 404. Changing it invalidates every registered client id; clients register again |
| `CALOTYPE_OAUTH_REGISTRATIONS_PER_IP_PER_HOUR` | 20 | OAuth client registrations per client IP (in memory) |
| `CALOTYPE_MCP_KEYLESS_CALL` | challenge | with OAuth on, what a keyless `tools/call` gets: `challenge` (401 with `WWW-Authenticate`) or `result` (the old tool error) |
| `CALOTYPE_API_KEY`, `CALOTYPE_API_URL` | | MCP server only |

No secrets are needed to run the API. OAuth sign-in needs `CALOTYPE_OAUTH_SECRET`. Billing needs all four `STRIPE_*` variables; with any missing it stays off and `/v1/billing/*` answers 503. Firestore uses the environment's default Google credentials (on Cloud Run, the service's service account); no key file is used, and none belongs in this repo. The landing-page prices are provisional (a comment in the HTML says so). `plans.json` is the source of truth for quotas.

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
  - OAuth codes live in `{prefix}oauth_codes`. Saving one runs a single-field range query on `expires_at` (indexed automatically) to delete expired codes, then one write; redeeming one is a transaction of one read and one delete. A sign-in with Approve costs about 3 reads and 4 writes, the key's 2 included.
  - Cost: a successful screenshot is 2 document reads (key lookup, quota check) and 2 writes (count, lifetime success); a failed capture adds one write for the refund; a refusal at the quota is 2 reads (the count, then the prepaid credit) and no write; a screenshot paid from prepaid credit is one more read and one write than usual; a new key is 2 writes. A free try is 1 read and 2 writes when rendered, 1 read and 1 write at the daily cap, and 1 write when refused or over the hourly limit. Firestore's free tier is 50,000 reads and 20,000 writes a day and 1 GiB stored, so about 10,000 screenshots a day cost nothing.
  - With Firestore, keys and quotas are shared across instances, so `--max-instances` can go above 1. The key-creation rate limit and the concurrency limit stay per instance.
  - Without Firestore, the fallback is one instance (`--max-instances 1`) with SQLite on a mounted volume, accepting the risk of loss.
- `/health` and `/healthz` return 503 if Chromium is not connected. Use either as the startup and liveness probe; from outside a run.app address, only `/health` reaches the app.
- Cloud Run egress is the public internet, but the metadata server (`metadata.google.internal`, 169.254.169.254) is reachable from the container. The SSRF rule blocks it. Do not add a Serverless VPC connector: without one, private networks are not even routable, which is a second line behind the rule.
- Chromium runs without its own sandbox, as is usual in containers. Cloud Run's gen2 environment provides the isolation boundary.

## Known gaps

- Billing is tested against a mocked Stripe client. Live Stripe has run since 17 November 2026; a server without Stripe settings keeps billing off (503). `plans.json` prices must be kept equal to the Stripe Prices by hand.
- Prepaid credit (`credits.py`) is tested against a mocked Stripe client and the protocol's published examples only. It has never run against Stripe, sandbox or live, or been paid by a real wallet. Stablecoin payment is not built. `deploy/README.md` lists what is unverified.
- The Stripe processed-event list grows without bound (one small row per event). Firestore TTL or a periodic delete can trim it later. So do quota-notice records (at most two per key per month), until the key is deleted.
- `admin remove-email ADDRESS` matches the address as stored: the domain in any case, the local part exactly.
- A removal link is a GET that acts at once, as asked. A mail scanner that opens links could remove an address; the holder can add it again.
- The Firestore backend is tested against the Firestore emulator only, not against a real Firestore database. Nothing moves data from SQLite to Firestore (there is no deployed data to move).
- The image is built by Cloud Build (`deploy/cloudbuild.yaml`), not locally; the Playwright base image is about 2 GB.
- No PDF output, no element selector, no custom headers or cookies, and no caching of repeat captures.
- `adblock_domains.txt` is a short hand-kept list, not EasyList.
- Rate limits and the concurrency limit are per instance. The free tries' daily cap is shared (in storage); their per-IP limit is per instance.
- OAuth has been walked end to end with the official MCP Python SDK's OAuth client (registration and Client ID Metadata Document), not yet from claude.ai or ChatGPT themselves. Registration limits and the metadata-document cache are per instance.
- With OAuth on, a keyless `tools/call` answers 401, not a tool result. A client that cannot do OAuth sees an HTTP error whose JSON-RPC body says how to get a key.
- Free tries share the renderers with keyed calls; a burst of tries can make keyed calls wait or answer `503 busy`. The daily cap bounds it.
