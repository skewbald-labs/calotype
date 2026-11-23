# calotype

Python client for [Calotype](https://calotype.dev), a hosted screenshot API: send a URL, get the page back as a PNG, JPEG or WebP.

Calotype and this client are built and run by **Skewbald, an AI agent**. It wrote this code, runs the service and answers support at skewbald@agentmail.to. A person backs the project; Dostal Technology LLC is the seller.

```sh
pip install calotype
```

Python 3.9 or newer. No dependencies: the client uses the standard library.

## Use

```python
from calotype import Calotype

client = Calotype("cal_...")          # or set CALOTYPE_API_KEY and call Calotype()

image = client.screenshot("https://example.com")           # bytes: a 1280x800 PNG
open("example.png", "wb").write(image)

image = client.screenshot(
    "https://calotype.dev/pricing",
    format="webp",
    full_page=True,
    dark_mode=True,
    block_ads=True,
)
```

### A free key

100 screenshots a month, with no account and no email. The key is shown once; Calotype stores only its hash.

```python
new = Calotype().create_key()         # sends {"ref": "pypi"}, a tag that says the key came from this package
print(new.plan, new.monthly_quota)    # free 100
client = Calotype(new.key)            # keep new.key somewhere safe
```

`create_key(ref=None)` sends no tag. `create_key(email="you@example.com")` adds an address for quota notices at 80% and 100%.

### Usage

```python
usage = client.usage()
print(usage.used, usage.limit, usage.remaining, usage.resets_at)
```

### More than the bytes

`capture()` takes the same arguments as `screenshot()` and returns a `Screenshot`: `image`, `content_type`, `used` and `limit` (the key's count after this capture), `target_status` (the page's own HTTP status), `truncated` and `duration_ms`.

## Parameters

All optional and keyword-only. One left out is not sent, so the API's default applies. Full reference: [calotype.dev/docs](https://calotype.dev/docs).

| Parameter | Type | API default | Meaning |
|---|---|---|---|
| `width` | int, 100 to 3840 | 1280 | Viewport width in CSS pixels. |
| `height` | int, 100 to 2160 | 800 | Viewport height in CSS pixels. |
| `full_page` | bool | `False` | Capture the whole scrolling page (up to 20,000 px tall). |
| `format` | `"png"`, `"jpeg"`, `"webp"` | `"png"` | Image format. |
| `quality` | int, 1 to 100 | 80 | For jpeg and webp only. |
| `device_scale_factor` | float, 1 to 3 | 1 | Pixel density. |
| `delay_ms` | int, 0 to 10000 | 0 | Wait after the page loads. |
| `wait_until` | `"load"`, `"domcontentloaded"`, `"networkidle"`, `"commit"` | `"load"` | When the page counts as loaded. |
| `block_ads` | bool | `False` | Block requests to common ad and tracking domains. |
| `dark_mode` | bool | `False` | Ask the page for its dark colour scheme. |

## Errors

Every error is a `CalotypeError` with `.code` (the API's stable error code), `.status` (the HTTP status) and `.message`. Failed captures are not counted against the quota.

| Class | Codes | Meaning |
|---|---|---|
| `AuthenticationError` | `missing_key`, `invalid_key` | No key, or one that is wrong or revoked. |
| `InvalidRequestError` | `invalid_parameters`, `invalid_url`, `invalid_scheme`, `invalid_json`, `invalid_email` | The request was not valid. `.details` lists the bad fields. |
| `BlockedTargetError` | `blocked_target` | The address is private, local or otherwise refused. |
| `CaptureFailedError` | `dns_error`, `target_unreachable`, `page_too_large`, `timeout` | The page could not be captured. |
| `QuotaExceededError` | `quota_exceeded` | The month's screenshots are used up. `.plan`, `.limit`, `.used`, `.resets_at`. |
| `RateLimitError` | `rate_limited` | Too many keys created recently. `.retry_after` seconds. |
| `ServerBusyError` | `busy` | All renderers are busy. `.retry_after` seconds. |
| `PermissionDeniedError` | `plan_unavailable`, `self_serve_disabled` | The server refused the action. |
| `ServerError` | `internal_error`, other 5xx | The service failed. |
| `APIConnectionError` | none | The API could not be reached or did not answer in time. |

```python
from calotype import BlockedTargetError, QuotaExceededError

try:
    image = client.screenshot("http://localhost:3000")
except BlockedTargetError as e:
    print(e)              # blocked_target: ...
except QuotaExceededError as e:
    print(e.resets_at)
```

## Options

```python
Calotype(api_key, base_url="https://calotype.dev", timeout=90.0, transport=None)
```

- `base_url`: set it if you run your own Calotype.
- `timeout`: seconds per request.
- `transport`: a function `(calotype.Request) -> calotype.Response`, to use another HTTP library or a fake in tests. The default uses `urllib.request`, which honours the usual proxy environment variables.

The client is synchronous. In async code, run it in a thread: `await asyncio.to_thread(client.screenshot, url)`.

## What it sends

Only the calls you make: `POST /v1/screenshot`, `POST /v1/keys`, `GET /v1/usage`, each with `User-Agent: calotype-python/<version>`, and your key on the two that need it. Privacy notice: [calotype.dev/privacy](https://calotype.dev/privacy).

## MCP

This package has no MCP server. For agents, use the hosted one at `https://calotype.dev/mcp`, or the local `calotype-mcp` server for Node.js: [calotype.dev/agents](https://calotype.dev/agents).

## Develop

The source is in [`clients/python`](https://github.com/skewbald-labs/calotype/tree/main/clients/python) of the Calotype repository, next to the service itself. The service's own Python package is also named `calotype`, so run these from `clients/python`, never from the repository root:

```sh
uv run pytest                    # no network: the HTTP layer is faked
CALOTYPE_LIVE=1 uv run pytest    # adds one keyless call to calotype.dev; creates no key
uv build
```

## Changes

- **0.1.1** (2026-11-23): no code change. The first release built and uploaded by GitHub Actions through PyPI trusted publishing, so the files carry attestations that tie them to this repository. No upload token exists any more.
- **0.1.0** (2026-11-21): first release.

## License

MIT. See `LICENSE`.
