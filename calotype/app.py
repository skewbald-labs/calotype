"""The HTTP API.

Run with ``python -m calotype`` (reads PORT) or
``uvicorn --factory calotype.app:create_app --no-access-log``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .billing import (
    UNAVAILABLE_MESSAGE,
    BillingProvider,
    BillingProviderError,
    BillingUnavailable,
    InvalidWebhook,
    billing_from_env,
)
from . import funnel, mpp
from .capture import CaptureError, CaptureResult, Renderer, load_blocklist
from .config import SITE_DIR, CreditPack, Settings
from .credits import HANDLED_EVENTS as CREDIT_EVENTS
from .credits import MIN_CARD_CENTS
from .credits import UNAVAILABLE_MESSAGE as CREDITS_UNAVAILABLE_MESSAGE
from .credits import CreditsProvider, PaymentConflict, PaymentRefused, PaymentUnconfirmed, credits_from_env

# How the 402's message names each MPP method.
METHOD_NAMES = {"stripe": "a card through a Stripe shared payment token", "tempo": "USDC on Tempo"}
from .keypages import BrowserPages, add_key_routes
from .logs import client_software, log_event, setup_logging
from .oauth import OAuthServer, add_oauth_routes, oauth_enabled
from .notices import mask_email, normalize_email, notices_enabled, thresholds_at, token_key_id, token_matches
from .params import ScreenshotParams
from .pages import SITE_ORIGIN, TryLimits, add_site_routes
from .remote_mcp import RemoteMCP
from .ratelimit import SWEEP_INTERVAL_S, SlidingWindowLimiter, client_ip
from .ssrf import AddressPolicy, BlockedTarget
from .tries import add_try_routes
from .storage import (
    REF_LABEL_PREFIX,
    REF_TAG,
    ApiKey,
    Notice,
    Storage,
    current_month,
    key_ref,
    open_configured_storage,
)

log = logging.getLogger("calotype.api")

_HOST_HEADER = re.compile(r"^[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.extra = extra


@dataclass(frozen=True)
class Captured:
    """A successful capture, and the key's count for the month after it."""

    result: CaptureResult
    params: ScreenshotParams
    used: int
    limit: int
    # One line for the model once a key nears its limit (running plan A11); "" otherwise.
    upgrade: str = ""
    # Set when this capture was paid from prepaid credit (the month's quota was used up):
    # the credit left after it. None when it counted against the monthly quota.
    credits: int | None = None


NEAR_LIMIT = 0.8  # the share of a month's quota at which the tool starts naming the next plan


def upgrade_text(plans: dict, plan, origin: str, paid_open: bool) -> str:
    """Where a key goes for more screenshots: the next plan up and its price, or a contact.

    With checkout off (no Stripe on this server) it says paid plans open soon instead.
    """
    bigger = sorted((p for p in plans.values() if p.monthly_quota > plan.monthly_quota and p.price_usd_cents > 0),
                    key=lambda p: (p.monthly_quota, p.price_usd_cents))
    if not bigger:
        return "For a larger quota, write to skewbald@agentmail.to."
    if not paid_open:
        return f"Paid plans with more screenshots open soon: {origin}/pricing."
    nxt = bigger[0]
    return (f"{nxt.title or nxt.name.title()} is ${nxt.price_usd_cents / 100:g} a month for "
            f"{nxt.monthly_quota:,} screenshots: {origin}/pricing.")


def _error_response(status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    body = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


INVALID_EMAIL = (
    "The email address is not valid. Send one address, like someone@example.com, of at most 254 characters."
)
NOTICES_OFF = "Quota emails are not available on this server."


def _removal_page(status: int, title: str, text: str) -> HTMLResponse:
    """The plain page a removal link opens. The token is in the address, so it is sent nowhere else."""
    import html

    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} - Calotype</title></head>"
        f"<body><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
        '<p><a href="/">Calotype</a></p></body></html>'
    )
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    }
    return HTMLResponse(body, status_code=status, headers=headers)


def _next_month_start(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_app(
    settings: Settings | None = None,
    *,
    policy: AddressPolicy | None = None,
    storage: Storage | None = None,
    renderer: Any = None,
    billing: BillingProvider | None = None,
    credits: CreditsProvider | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings = settings.with_plans()
    notices_on = notices_enabled(settings.email_token_secret)
    policy = policy or AddressPolicy()
    # Stripe is optional: without its variables the billing endpoints answer 503.
    billing = billing if billing is not None else billing_from_env()
    # Prepaid credit by machine payment is off unless CALOTYPE_CREDITS and its settings are set.
    credits = credits if credits is not None else credits_from_env()
    # A pack under Stripe's card minimum could never be paid for, so it is not offered.
    credit_packs: dict[str, CreditPack] = {
        name: pack for name, pack in (settings.credit_packs or {}).items() if pack.price_usd_cents >= MIN_CARD_CENTS
    }
    if renderer is None:
        renderer = Renderer(
            policy,
            max_concurrency=settings.max_concurrency,
            queue_timeout_s=settings.queue_timeout_s,
            capture_timeout_s=settings.capture_timeout_s,
            max_page_bytes=settings.max_page_bytes,
            blocklist=load_blocklist(settings.blocklist_file),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        if app.state.storage is None:
            app.state.storage = open_configured_storage(settings)
        await renderer.start()
        log_event(
            log,
            "calotype started",
            version=__version__,
            max_concurrency=settings.max_concurrency,
            billing=billing.configured,
            credits=credits.configured,
        )
        if not billing.configured:
            # Variable names only, never values.
            log_event(log, "billing is off", missing=getattr(billing, "missing", []))
        credits_missing = getattr(credits, "missing", [])
        if not credits.configured and "CALOTYPE_CREDITS" not in credits_missing:
            # Switched on but incomplete. Left unsaid when it was never switched on.
            log_event(log, "prepaid credit is off", level=logging.WARNING, missing=credits_missing)
        if credits.configured:
            # Which MPP methods the 402 offers: "stripe" (cards), and "tempo" (USDC) when switched on and the
            # deposit address is this Stripe account's own (asked of Stripe now; see credits.tempo_ready).
            tempo_config = getattr(getattr(credits, "config", None), "tempo", None)
            offered = getattr(credits, "offered_methods", None)
            log_event(
                log,
                "prepaid credit is on",
                methods=list(await asyncio.to_thread(offered) if offered else getattr(credits, "methods", ())),
                tempo_chain_id=tempo_config.chain_id if tempo_config else None,
            )
        if not notices_on:
            log_event(log, "quota emails are off", needs="CALOTYPE_EMAIL_TOKEN_SECRET (32+ characters)")
        if app.state.oauth is None:
            log_event(log, "oauth sign-in is off", needs="CALOTYPE_OAUTH_SECRET (32+ characters)")
        sweeper = asyncio.create_task(sweep_limiters())
        try:
            async with app.state.mcp.run():
                yield
        finally:
            sweeper.cancel()
            await renderer.stop()

    async def sweep_limiters():
        # Forget IP addresses the key-creation and try limits no longer need; see ratelimit.py.
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            app.state.key_limiter.sweep()
            app.state.key_global_limiter.sweep()
            app.state.credit_limiter.sweep()
            app.state.try_limiter.sweep()
            app.state.oauth_register_limiter.sweep()

    app = FastAPI(
        title="Calotype",
        version=__version__,
        lifespan=lifespan,
        # /docs is the site's API reference. FastAPI's Swagger UI loads scripts
        # from a CDN, which the site does not do; /openapi.json stays.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.storage = storage
    app.state.renderer = renderer
    app.state.policy = policy
    app.state.billing = billing
    app.state.credits = credits
    # In-memory only; see ratelimit.py. IPs are never stored or logged.
    app.state.key_limiter = SlidingWindowLimiter(settings.keys_per_ip_per_hour)
    app.state.credit_limiter = SlidingWindowLimiter(settings.credit_payments_per_key_per_hour)
    app.state.key_global_limiter = SlidingWindowLimiter(settings.keys_global_per_hour)
    # Free tries on the home page (tries.py), per client IP; in memory only, like the above.
    app.state.try_limiter = SlidingWindowLimiter(settings.tries_per_ip_per_hour)
    # Daily counts of the steps toward a key (funnel.py); written after each response.
    funnel.install(app)
    # OAuth client registrations (oauth.py), per client IP; in memory only, like the above.
    app.state.oauth_register_limiter = SlidingWindowLimiter(settings.oauth_registrations_per_ip_per_hour)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return _error_response(exc.status, exc.code, exc.message, exc.headers, **exc.extra)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.monotonic()
        request.state.request_id = uuid.uuid4().hex[:16]
        request.state.key_id = None
        request.state.target = None
        # Set by remote_mcp.py for an MCP initialize: (name, version) as the client reports them.
        request.state.mcp_client = None
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = request.state.request_id
            return response
        finally:
            # Path only: the query string can carry access_key and the target URL.
            # The software's name and kind only (logs.client_software), never the User-Agent itself.
            agent, agent_kind = client_software(request.headers.get("user-agent"))
            mcp_client = request.state.mcp_client
            log_event(
                log,
                "request",
                request_id=request.state.request_id,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=int((time.monotonic() - started) * 1000),
                key_id=request.state.key_id,
                target=request.state.target,
                agent=agent,
                agent_kind=agent_kind,
                **({"mcp_client": mcp_client[0], "mcp_client_version": mcp_client[1]} if mcp_client else {}),
            )

    async def authenticate(request: Request, *, allow_query: bool = True, payment_ok: bool = False) -> ApiKey:
        raw = None
        auth = request.headers.get("authorization", "")
        if payment_ok and mpp.payment_credential(auth) is not None:
            # /v1/credits only: Authorization carries a payment credential, so the key is in the query.
            auth = ""
        request.state.key_in_header = bool(auth)
        if auth:
            scheme, _, token = auth.partition(" ")
            if scheme.lower() != "bearer" or not token.strip():
                raise ApiError(401, "invalid_key", "Use 'Authorization: Bearer <key>'.")
            raw = token.strip()
        elif allow_query:
            raw = request.query_params.get("access_key")
        if not raw:
            how = "send 'Authorization: Bearer <key>' or '?access_key=<key>'" if allow_query else (
                "send 'Authorization: Bearer <key>'"
            )
            raise ApiError(401, "missing_key", f"An API key is required: {how}.", {"WWW-Authenticate": "Bearer"})
        if len(raw) > 200:
            raise ApiError(401, "invalid_key", "The API key is not valid.")
        # In a thread: with Firestore this is a network call.
        key = await asyncio.to_thread(app.state.storage.get_key, raw)
        if key is None or key.revoked:
            raise ApiError(401, "invalid_key", "The API key is not valid.", {"WWW-Authenticate": "Bearer"})
        request.state.key_id = key.id
        request.state.key_ref = key_ref(raw)
        return key

    def parse_params(data: dict[str, Any]) -> ScreenshotParams:
        try:
            params = ScreenshotParams.model_validate(data)
        except ValidationError as e:
            details = [
                {"field": ".".join(str(p) for p in err["loc"]) or "body", "message": err["msg"]} for err in e.errors()
            ]
            raise ApiError(400, "invalid_parameters", "Some parameters are not valid.", details=details)
        problem = params.check_combinations()
        if problem:
            raise ApiError(400, "invalid_parameters", problem)
        return params

    site_origin = (settings.public_url or SITE_ORIGIN).rstrip("/")

    # With the site on, a browser that opens an API-only address gets a help page instead of a
    # bare JSON error (keypages.py). Programs, and a server without the site, get the JSON as before.
    site_on = settings.serve_site and SITE_DIR.is_dir()
    try_limits = TryLimits(settings.tries_per_ip_per_hour, settings.tries_per_day)
    browser_pages = BrowserPages(site_dir=SITE_DIR, settings=settings, tries=try_limits) if site_on else None

    def credits_text() -> str:
        """For a key out of quota: that prepaid screenshots can be bought, where that is on."""
        if not credits.configured or not credit_packs:
            return ""
        return f" Or buy prepaid screenshots with no subscription: POST /v1/credits ({site_origin}/docs#credits)."

    async def capture_for_key(request: Request, key: ApiKey, data: dict[str, Any]) -> Captured:
        """The one capture path, for /v1/screenshot and the MCP tool alike.

        Checks the plan, the parameters and the address (SSRF policy), counts
        the screenshot against the quota, captures, refunds a failed capture,
        and logs. Raises ``ApiError`` for anything that is not an image.
        """
        plan = settings.plans.get(key.plan)
        if plan is None:
            raise ApiError(403, "plan_unavailable", "This key's plan is not available. Contact support.")
        params = parse_params(data)
        try:
            target = await policy.check_url(params.url)
        except BlockedTarget as e:
            raise ApiError(400, e.code, e.reason)
        request.state.target = target.display

        store: Storage = app.state.storage
        month = current_month()
        # The month's plan quota is used first; prepaid credit only once that is gone.
        allowed, used = await asyncio.to_thread(store.try_consume, key.id, month, plan.monthly_quota)
        credits_left: int | None = None
        if not allowed:
            allowed, credits_left = await asyncio.to_thread(store.consume_credit, key.id)
        if not allowed:
            raise ApiError(
                429,
                "quota_exceeded",
                f"This key has used its {plan.monthly_quota} screenshots for {month} on the "
                f"'{plan.name}' plan. The count resets at {_next_month_start()}. "
                + upgrade_text(settings.plans, plan, site_origin, billing.configured)
                + credits_text(),
                plan=plan.name,
                limit=plan.monthly_quota,
                used=used,
                credits=0,
                resets_at=_next_month_start(),
            )

        async def uncount():
            # A failed capture costs nothing: put back whichever counter paid for it.
            if credits_left is None:
                await asyncio.to_thread(store.refund, key.id, month)
            else:
                await asyncio.to_thread(store.return_credit, key.id)

        try:
            result = await renderer.capture(params)
        except CaptureError as e:
            await uncount()
            raise ApiError(e.status, e.code, e.message, e.headers)
        except Exception:
            await uncount()
            log.exception("capture failed", extra={"fields": {"request_id": request.state.request_id}})
            raise ApiError(500, "internal_error", "The screenshot failed. It was not counted against your quota.")

        total_ok = await asyncio.to_thread(store.record_success, key.id)
        if total_ok == 1 and not key.internal:
            # A new contact for the demand test: a non-internal key's first success.
            ordinal = await asyncio.to_thread(store.contact_ordinal, key.id)
            funnel.mark(request, "first_capture")
            log_event(log, "first successful screenshot", key_id=key.id, contact_ordinal=ordinal)

        log_event(
            log,
            "screenshot",
            request_id=request.state.request_id,
            key_id=key.id,
            target=target.display,
            format=params.format,
            full_page=params.full_page,
            scroll_y=params.scroll_y,
            bytes=len(result.image),
            duration_ms=result.duration_ms,
            target_status=result.target_status,
            blocked_requests=result.blocked_requests,
            truncated=result.truncated,
        )
        upgrade = ""
        if used >= NEAR_LIMIT * plan.monthly_quota:
            upgrade = "This key is near its monthly limit. " + upgrade_text(settings.plans, plan, site_origin, billing.configured)
        if credits_left is not None:
            upgrade = f"This month's plan quota is used up; this came from prepaid credit, {credits_left:,} left."
        elif key.email and notices_on:
            # Only a capture the plan quota paid for can be the one that reached 80% or 100%;
            # one paid from credit leaves the month's count where it was.
            await queue_notices(request, key, plan, month, used)
        return Captured(
            result=result, params=params, used=used, limit=plan.monthly_quota, upgrade=upgrade, credits=credits_left
        )

    async def queue_notices(request: Request, key: ApiKey, plan, month: str, used: int) -> None:
        """Queue the 80% or 100% notice when this capture is the one that reached it.

        Only queued: the skewbald-checks job sends it, so this service never
        holds the mail key. Never fails the capture.
        """
        for threshold in thresholds_at(used, plan.monthly_quota):
            notice = Notice(
                key_id=key.id,
                month=month,
                threshold=threshold,
                used=used,
                quota=plan.monthly_quota,
                plan=plan.name,
                key_ref=request.state.key_ref,
                resets_at=_next_month_start(),
                # The same line the MCP tool adds near the limit.
                upgrade=upgrade_text(settings.plans, plan, site_origin, billing.configured),
                origin=site_origin,
            )
            try:
                if await asyncio.to_thread(app.state.storage.queue_notice, notice):
                    log_event(log, "quota notice queued", key_id=key.id, month=month, threshold=threshold)
            except Exception:
                log.warning("could not queue a quota notice", exc_info=True)

    async def handle_screenshot(request: Request, data: dict[str, Any]) -> Response:
        key = await authenticate(request)
        shot = await capture_for_key(request, key, data)
        result = shot.result
        headers = {
            "Cache-Control": "no-store",
            "X-Calotype-Usage": f"{shot.used}/{shot.limit}",
            "X-Calotype-Duration-Ms": str(result.duration_ms),
        }
        if result.target_status is not None:
            headers["X-Calotype-Target-Status"] = str(result.target_status)
        if (result.meta or {}).get("page_height"):
            # The whole scrolling page in CSS pixels, so a caller can tell a first screen from a whole page.
            headers["X-Calotype-Page-Height"] = str(result.meta["page_height"])
        if (result.meta or {}).get("scroll_y") is not None:
            # For a one-screen capture: the y of the screen's top in the page, as captured. A scroll_y
            # past the last screen is moved up to it, so this can be less than was asked.
            headers["X-Calotype-Scroll-Y"] = str(result.meta["scroll_y"])
        if result.truncated:
            headers["X-Calotype-Truncated"] = "1"
        if shot.credits is not None:
            headers["X-Calotype-Credits"] = str(shot.credits)
        return Response(content=result.image, media_type=result.content_type, headers=headers)

    # OAuth sign-in for /mcp (oauth.py): off, and its discovery 404, without CALOTYPE_OAUTH_SECRET.
    oauth = OAuthServer(secret=settings.oauth_secret, issuer=site_origin, policy=policy) if oauth_enabled(
        settings.oauth_secret) else None
    app.state.oauth = oauth

    # The remote MCP server: the same keys, quota and capture path, over MCP.
    mcp = RemoteMCP(
        authenticate=lambda request: authenticate(request, allow_query=False),
        capture=capture_for_key,
        api_error=ApiError,
        site_origin=settings.public_url or SITE_ORIGIN,
        free_quota=settings.plans["free"].monthly_quota if "free" in settings.plans else None,
        tag_source=lambda key, tag: asyncio.to_thread(app.state.storage.tag_source, key.id, tag),
        challenge=oauth.challenge_for if oauth else None,
        challenge_keyless_calls=settings.mcp_keyless_call != "result",
        browser_page=browser_pages.mcp if browser_pages else None,
    )
    app.state.mcp = mcp
    app.add_route("/mcp", mcp, methods=["GET", "POST", "DELETE"], include_in_schema=False)
    if oauth is not None:
        add_oauth_routes(app, oauth, settings=settings, site_dir=SITE_DIR)

    # Cloud Run's front end keeps /healthz for itself on run.app addresses, so
    # /health is the public path; /healthz stays for the container's own check.
    @app.get("/health")
    @app.get("/healthz")
    async def healthz():
        connected = getattr(renderer, "browser_connected", True)
        body = {"status": "ok" if connected else "degraded", "version": __version__, "browser": connected}
        return JSONResponse(body, status_code=200 if connected else 503)

    @app.get("/v1/screenshot")
    async def screenshot_get(request: Request):
        data = {k: v for k, v in request.query_params.items() if k != "access_key"}
        try:
            return await handle_screenshot(request, data)
        except ApiError as e:
            # A person who opened an example link in a browser, with no key or a placeholder:
            # the same 401, as a page that says what to do. Programs get the JSON error.
            page = browser_pages.screenshot(request, e.code, e.headers) if browser_pages and e.status == 401 else None
            if page is None:
                raise
            return page

    @app.post("/v1/screenshot")
    async def screenshot_post(request: Request):
        try:
            data = await request.json()
        except Exception:
            raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
        data.pop("access_key", None)
        return await handle_screenshot(request, data)

    async def issue_free_key(
        request: Request, *, email: Any = None, ref: Any = None, default_ref: str = ""
    ) -> tuple[str, ApiKey, str | None, str | None]:
        """Make a free-plan key: the one path for ``POST /v1/keys`` and the site's key button (keypages.py).

        Every check lives here, so both share it: the switch for self-serve keys, the optional
        email address, the per-IP and overall limits, and the source tag. Raises ``ApiError``
        for a refusal. Returns the key (the only time it exists in the clear), its record, the
        address as given and normalized, and the address as stored (None with quota emails off).
        """
        if not settings.self_serve_keys or "free" not in settings.plans:
            raise ApiError(403, "self_serve_disabled", "Self-serve keys are not available right now.")
        # Optional email, checked before the rate limit so a typo costs no slot.
        normalized = None
        if email not in (None, ""):
            normalized = normalize_email(email)
            if normalized is None:
                raise ApiError(400, "invalid_email", INVALID_EMAIL)
        ip = client_ip(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            settings.trusted_proxy_hops,
        )
        ok, retry = app.state.key_limiter.check(ip)
        if ok:
            ok, retry = app.state.key_global_limiter.check("all")
        del ip  # not kept beyond the in-memory limiter
        if not ok:
            raise ApiError(
                429,
                "rate_limited",
                "Too many keys were created recently. Try again later.",
                {"Retry-After": str(retry)},
                retry_after_s=retry,
            )
        # An optional source tag, copied from a page's ?ref= by the site's key button or its
        # form. Anything that is not a valid tag is ignored, never an error; ``default_ref``
        # (the form's "web") then names the source.
        tag = ref if isinstance(ref, str) and REF_TAG.match(ref) else default_ref
        label = REF_LABEL_PREFIX + tag if tag else ""
        # With the feature off, an address is not stored at all.
        stored_email = normalized if notices_on else None
        raw, key = await asyncio.to_thread(
            app.state.storage.create_key, "free", label, internal=False, email=stored_email
        )
        request.state.key_id = key.id
        return raw, key, normalized, stored_email

    @app.post("/v1/keys", status_code=201)
    async def create_free_key(request: Request):
        """Create a free-plan key. Asks for nothing; an email address for quota notices is optional."""
        try:
            body = await request.json()
        except Exception:
            body = None
        body = body if isinstance(body, dict) else {}
        # Optional {"email": ...} and {"ref": "<tag>"}; issue_free_key checks both.
        raw, _key, email, stored_email = await issue_free_key(request, email=body.get("email"), ref=body.get("ref"))
        plan = settings.plans["free"]
        answer: dict[str, Any] = {
            "key": raw,
            "plan": plan.name,
            "monthly_quota": plan.monthly_quota,
            "message": "This is the only time the key is shown. Calotype stores only its hash and cannot recover it.",
        }
        if email is not None:
            answer.update(email_state(stored_email))
            if not notices_on:
                answer["email_message"] = NOTICES_OFF + " The address was not stored."
        return JSONResponse(answer, status_code=201, headers={"Cache-Control": "no-store"})

    if browser_pages is not None:
        # After the POST route, so any other method still answers 405 with "Allow: POST".
        @app.get("/v1/keys", include_in_schema=False)
        async def keys_in_a_browser(request: Request):
            """A browser gets a page that says keys are made with a POST; a program, the 405 it always got."""
            page = browser_pages.keys(request)
            if page is None:
                raise StarletteHTTPException(405, headers={"Allow": "POST"})
            return page

    def email_state(email: str | None) -> dict[str, Any]:
        """What the API says about a key's address: masked, never in full."""
        return {"email": mask_email(email), "notices": bool(email) and notices_on}

    # --- the optional email address (notices.py) -----------------------------------------

    @app.post("/v1/keys/email")
    async def set_key_email(request: Request):
        """Set or replace the key's address for quota notices."""
        key = await authenticate(request)
        if not notices_on:
            raise ApiError(503, "notices_unavailable", NOTICES_OFF)
        try:
            data = await request.json()
        except Exception:
            raise ApiError(400, "invalid_json", 'The request body must be a JSON object like {"email": "..."}.')
        email = normalize_email(data.get("email")) if isinstance(data, dict) else None
        if email is None:
            raise ApiError(400, "invalid_email", INVALID_EMAIL)
        await asyncio.to_thread(app.state.storage.set_email, key.id, email)
        log_event(log, "key email set", key_id=key.id)
        return JSONResponse(email_state(email), headers={"Cache-Control": "no-store"})

    @app.delete("/v1/keys/email")
    async def remove_key_email(request: Request):
        """Remove the key's address. Works with the feature on or off, and when there is none."""
        key = await authenticate(request)
        if key.email:
            await asyncio.to_thread(app.state.storage.set_email, key.id, None)
            log_event(log, "key email removed", key_id=key.id, how="api")
        return JSONResponse(email_state(None), headers={"Cache-Control": "no-store"})

    @app.api_route("/v1/keys/email/remove", methods=["GET", "POST"], include_in_schema=False)
    async def remove_key_email_by_link(request: Request):
        """The one-click link at the end of each notice. POST is for mail clients' one-click unsubscribe."""
        token = request.query_params.get("token", "")
        key_id = token_key_id(token)
        key = await asyncio.to_thread(app.state.storage.get_key_by_id, key_id) if key_id else None
        if key is None or not token_matches(settings.email_token_secret, token, key.id, key.email):
            # One answer for a bad token, another key's token, and an address already removed.
            return _removal_page(
                400,
                "Link not valid",
                "This link is not valid, or the address was already removed. "
                "To remove an address another way, reply stop to the notice, or write to skewbald@agentmail.to.",
            )
        request.state.key_id = key.id
        await asyncio.to_thread(app.state.storage.set_email, key.id, None)
        log_event(log, "key email removed", key_id=key.id, how="link")
        return _removal_page(
            200,
            "Address removed",
            "The email address was removed from your Calotype key. Calotype will send it no more quota notices. "
            "The key itself still works.",
        )

    @app.get("/v1/usage")
    async def usage(request: Request):
        key = await authenticate(request)
        plan = settings.plans.get(key.plan)
        month = current_month()
        used = await asyncio.to_thread(app.state.storage.get_usage, key.id, month)
        credit = await asyncio.to_thread(app.state.storage.get_credit_balance, key.id)
        return {
            "plan": key.plan,
            "month": month,
            "used": used,
            "limit": plan.monthly_quota if plan else 0,
            # Prepaid screenshots, used only after the month's limit. They do not reset.
            "credits": credit,
            "resets_at": _next_month_start(),
            # Not secret: lets the holder name this key to support without sending it.
            "key_ref": request.state.key_ref,
            **email_state(key.email),
        }

    # --- billing (Stripe) -------------------------------------------------------------

    def require_billing() -> BillingProvider:
        if not billing.configured:
            raise ApiError(503, "billing_unavailable", UNAVAILABLE_MESSAGE)
        return billing

    def public_base(request: Request) -> str:
        """Where Stripe sends the buyer back to."""
        if settings.public_url:
            return settings.public_url
        scheme = request.url.scheme
        if settings.trusted_proxy_hops > 0:
            forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            if forwarded in ("http", "https"):
                scheme = forwarded
        host = request.headers.get("host", "")
        if not _HOST_HEADER.match(host):
            host = request.url.netloc
        return f"{scheme}://{host}"

    def billing_response(body: dict[str, Any], status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.post("/v1/billing/checkout")
    async def billing_checkout(request: Request):
        """Start a Stripe Checkout for a free key. Returns the URL to send the buyer to."""
        provider = require_billing()
        key = await authenticate(request)
        try:
            data = await request.json()
        except Exception:
            raise ApiError(400, "invalid_json", 'The request body must be a JSON object like {"plan": "starter"}.')
        plan = data.get("plan") if isinstance(data, dict) else None
        for_sale = [p for p in provider.plans_for_sale() if p in settings.plans]
        if plan not in for_sale:
            raise ApiError(
                400, "invalid_plan", f"Choose a plan to buy: {', '.join(for_sale)}.", plans=for_sale
            )
        if key.plan != "free":
            how = (
                "Use GET /v1/billing/portal to change or cancel it."
                if key.stripe_customer_id
                else "Email skewbald@agentmail.to to change it."
            )
            raise ApiError(409, "already_subscribed", f"This key is already on the '{key.plan}' plan. {how}")
        base = public_base(request)
        try:
            session = await asyncio.to_thread(
                provider.create_checkout,
                key,
                plan,
                f"{base}/pricing?checkout=success#upgrade",
                f"{base}/pricing?checkout=cancelled#upgrade",
            )
        except BillingUnavailable:
            raise ApiError(503, "billing_unavailable", UNAVAILABLE_MESSAGE)
        except BillingProviderError:
            raise ApiError(502, "billing_error", "The payment provider could not start checkout. Try again shortly.")
        log_event(log, "checkout started", key_id=key.id, plan=plan)
        return billing_response({"url": session.url, "plan": plan})

    @app.get("/v1/billing/portal")
    async def billing_portal(request: Request):
        """A Stripe customer-portal link, to change card or plan, or cancel."""
        provider = require_billing()
        key = await authenticate(request)
        if not key.stripe_customer_id:
            raise ApiError(
                404,
                "no_billing_account",
                "This key has no billing account yet. Buy a plan with POST /v1/billing/checkout first.",
            )
        try:
            url = await asyncio.to_thread(provider.create_portal, key.stripe_customer_id, f"{public_base(request)}/pricing#upgrade")
        except BillingUnavailable:
            raise ApiError(503, "billing_unavailable", UNAVAILABLE_MESSAGE)
        except BillingProviderError:
            raise ApiError(502, "billing_error", "The payment provider could not open the billing portal. Try again shortly.")
        return billing_response({"url": url})

    @app.post("/v1/billing/webhook")
    async def billing_webhook(request: Request):
        """Stripe's events. Verified by signature; no API key."""
        provider = require_billing()
        payload = await request.body()
        try:
            event = provider.verify_webhook(payload, request.headers.get("stripe-signature"))
        except InvalidWebhook as e:
            raise ApiError(400, "invalid_webhook", str(e))
        # A payment for prepaid credit goes to the credits module; everything else is a subscription event.
        handler = credits if credits.configured and event.get("type") in CREDIT_EVENTS else provider
        try:
            result = await asyncio.to_thread(handler.handle_event, event, app.state.storage)
        except BillingProviderError:
            # 5xx makes Stripe deliver the event again later.
            raise ApiError(500, "webhook_failed", "The event could not be applied now; it will be retried.")
        except Exception:
            log.exception("webhook failed", extra={"fields": {"event_type": event.get("type")}})
            raise ApiError(500, "webhook_failed", "The event could not be applied now; it will be retried.")
        return billing_response({"received": True, "outcome": result.outcome})

    # --- prepaid credit, bought by machine payment (MPP) ------------------------------------

    def pack_json(pack: CreditPack) -> dict[str, Any]:
        return {
            "name": pack.name,
            "title": pack.title,
            "screenshots": pack.screenshots,
            "price_usd": pack.price_usd_cents / 100,
        }

    async def payment_challenge(
        key: ApiKey, pack: CreditPack, realm: str, header: str, problem: str, detail: str
    ) -> JSONResponse:
        """402 with new challenges: the first answer, and the answer to a credential that was refused.

        One ``WWW-Authenticate`` header per payment method offered (card first), as the
        protocol allows (https://paymentauth.org/draft-httpauth-payment-01, appendix B.4).
        """
        # In a worker thread: which methods are offered may be asked of Stripe (credits.tempo_ready, hourly).
        challenges = await asyncio.to_thread(credits.challenges, key, pack, realm=realm, header=header)
        code = problem.replace("-", "_")
        if problem == "payment-required":
            ways = " or ".join(f"'{c.method}' ({METHOD_NAMES.get(c.method, c.method)})" for c in challenges)
            message = (
                f"Pay ${pack.price_usd_cents / 100:g} for {pack.screenshots:,} prepaid screenshots: answer one "
                f"Payment challenge in WWW-Authenticate (MPP, method {ways}), then send this request again "
                f"with the credential in {header or 'Authorization'}."
            )
        else:
            message = detail + " A new challenge is in WWW-Authenticate."
        body = mpp.problem(
            problem,
            detail,
            challengeId=challenges[0].id,
            # Calotype's own error shape, beside the protocol's problem details.
            error={
                "code": code,
                "message": message,
                "pack": pack_json(pack),
                "methods": [c.method for c in challenges],
            },
        )
        log_event(log, "credit challenge", key_id=key.id, pack=pack.name, problem=problem)
        response = JSONResponse(
            body, status_code=402, media_type=mpp.PROBLEM_CONTENT_TYPE, headers={"Cache-Control": "no-store"}
        )
        for challenge in challenges:
            response.headers.append("WWW-Authenticate", challenge.www_authenticate())
        return response

    # In the API schema only where it is on sale; where it is off it answers 503, like billing.
    @app.post("/v1/credits", include_in_schema=credits.configured)
    async def buy_credits(request: Request):
        """Buy a prepaid pack by machine payment: 402 with a challenge, then the same request with a credential."""
        if not credits.configured:
            raise ApiError(503, "billing_unavailable", CREDITS_UNAVAILABLE_MESSAGE)
        # The key first: a request without one learns nothing about prices (401 before 402).
        key = await authenticate(request, payment_ok=True)
        try:
            data = await request.json()
        except Exception:
            raise ApiError(400, "invalid_json", 'The request body must be a JSON object like {"pack": "starter"}.')
        name = data.get("pack") if isinstance(data, dict) else None
        pack = credit_packs.get(name) if isinstance(name, str) else None
        if pack is None:
            raise ApiError(
                400,
                "invalid_pack",
                f"Choose a pack to buy: {', '.join(credit_packs) or 'none are on sale'}.",
                packs=[pack_json(p) for p in credit_packs.values()],
            )
        # The realm is the public host name, without a port, as the MPP SDKs set it.
        realm = getattr(getattr(credits, "config", None), "realm", "") or (
            urlsplit(public_base(request)).hostname or "calotype"
        )
        # Where the payment credential goes. Authorization already holds the API key, unless the key came in
        # the query string; then the credential uses Authorization, the protocol's default.
        header = mpp.PAYMENT_AUTHORIZATION if request.state.key_in_header else ""
        credential = request.headers.get(header.lower() if header else "authorization", "")
        if mpp.payment_credential(credential) is None:
            return await payment_challenge(key, pack, realm, header, "payment-required", "Payment is required for this pack.")
        # Asking for a challenge is free and unlimited. Sending credentials is not: each one that
        # passes the checks makes Calotype call Stripe or the Tempo network, so one key gets only so many an hour.
        ok, retry = app.state.credit_limiter.check(str(key.id))
        if not ok:
            raise ApiError(
                429,
                "rate_limited",
                "This key has sent too many payment credentials recently. Try again later.",
                {"Retry-After": str(retry)},
                retry_after_s=retry,
            )
        try:
            paid = await asyncio.to_thread(
                credits.settle, key, pack, credential, app.state.storage, realm=realm, header=header
            )
        except PaymentRefused as e:
            return await payment_challenge(key, pack, realm, header, e.problem, e.detail)
        except PaymentConflict as e:
            # A second transfer for a challenge that another one already pays. Not "pay again".
            raise ApiError(409, "payment_conflict", e.detail, payment={"method": "tempo", "reference": e.reference})
        except PaymentUnconfirmed as e:
            who = "The Tempo network" if e.method == "tempo" else "Stripe"
            raise ApiError(
                502,
                "payment_unconfirmed",
                f"{who} has not confirmed this payment yet. Send the same request with the same credential "
                "again: it cannot charge twice. GET /v1/usage shows the credit once it is added.",
            )
        except BillingUnavailable:
            raise ApiError(503, "billing_unavailable", CREDITS_UNAVAILABLE_MESSAGE)
        except BillingProviderError:
            raise ApiError(502, "billing_error", "The payment provider could not take the payment. Try again shortly.")
        if paid.pending:
            # Bound to this challenge and not credited yet. No Payment-Receipt: MPP's receipt means success.
            return JSONResponse(
                {
                    "pack": pack.name,
                    "status": "pending",
                    "screenshots_added": 0,
                    "credits": paid.balance,
                    "already_credited": False,
                    "payment": {
                        "method": paid.method,
                        "reference": paid.reference,
                        "amount_usd": pack.price_usd_cents / 100,
                    },
                    "message": f"{paid.detail} Send the same request with the same credential again in a few "
                    "seconds, even after the challenge has expired: it is never charged twice, and the credit is "
                    "added once the payment settles. GET /v1/usage shows it too.",
                },
                status_code=202,
                headers={"Retry-After": "10", "Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "pack": pack.name,
                "screenshots_added": paid.screenshots if paid.added else 0,
                "credits": paid.balance,
                # True when this payment had been credited before: nothing was charged or added again.
                "already_credited": not paid.added,
                "payment": {
                    "method": paid.method,
                    "reference": paid.reference,
                    "amount_usd": pack.price_usd_cents / 100,
                },
            },
            headers={"Payment-Receipt": mpp.receipt(paid.method, paid.reference), "Cache-Control": "no-store"},
        )

    @app.get("/v1/plans")
    async def plans():
        body: dict[str, Any] = {
            # Settled once paid plans are on sale on this server.
            "provisional": not billing.configured,
            "plans": [
                {"name": p.name, "title": p.title, "monthly_quota": p.monthly_quota, "price_usd": p.price_usd_cents / 100}
                for p in settings.plans.values()
            ],
        }
        if credits.configured:
            # Prepaid packs, only where they can be bought (POST /v1/credits).
            body["credit_packs"] = [pack_json(p) for p in credit_packs.values()]
        return body

    if browser_pages is not None:
        add_site_routes(
            app, SITE_DIR, paid_open=billing.configured, tries=try_limits, oauth_on=oauth is not None,
            usdc_on="tempo" in getattr(credits, "methods", ()),
            free_quota=browser_pages.free_quota,
        )
        # The home page's try box: a capture with no key, under its own limits.
        add_try_routes(app, settings=settings, policy=policy, renderer=renderer, site_dir=SITE_DIR)
        # The "Get a free key" button: POST /keys makes a key by the same path as POST /v1/keys.
        add_key_routes(app, browser_pages, issue_key=issue_free_key, api_error=ApiError)

    return app
