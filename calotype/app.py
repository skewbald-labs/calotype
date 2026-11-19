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

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import __version__
from .billing import (
    UNAVAILABLE_MESSAGE,
    BillingProvider,
    BillingProviderError,
    BillingUnavailable,
    InvalidWebhook,
    billing_from_env,
)
from .capture import CaptureError, CaptureResult, Renderer, load_blocklist
from .config import SITE_DIR, Settings
from .logs import log_event, setup_logging
from .params import ScreenshotParams
from .pages import SITE_ORIGIN, add_site_routes
from .remote_mcp import RemoteMCP
from .ratelimit import SWEEP_INTERVAL_S, SlidingWindowLimiter, client_ip
from .ssrf import AddressPolicy, BlockedTarget
from .storage import REF_LABEL_PREFIX, REF_TAG, ApiKey, Storage, current_month, key_ref, open_configured_storage

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
) -> FastAPI:
    settings = settings or Settings.from_env()
    settings = settings.with_plans()
    policy = policy or AddressPolicy()
    # Stripe is optional: without its variables the billing endpoints answer 503.
    billing = billing if billing is not None else billing_from_env()
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
        )
        if not billing.configured:
            # Variable names only, never values.
            log_event(log, "billing is off", missing=getattr(billing, "missing", []))
        sweeper = asyncio.create_task(sweep_limiters())
        try:
            async with app.state.mcp.run():
                yield
        finally:
            sweeper.cancel()
            await renderer.stop()

    async def sweep_limiters():
        # Forget IP addresses the key-creation limit no longer needs; see ratelimit.py.
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            app.state.key_limiter.sweep()
            app.state.key_global_limiter.sweep()

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
    # In-memory only; see ratelimit.py. IPs are never stored or logged.
    app.state.key_limiter = SlidingWindowLimiter(settings.keys_per_ip_per_hour)
    app.state.key_global_limiter = SlidingWindowLimiter(settings.keys_global_per_hour)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return _error_response(exc.status, exc.code, exc.message, exc.headers, **exc.extra)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.monotonic()
        request.state.request_id = uuid.uuid4().hex[:16]
        request.state.key_id = None
        request.state.target = None
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-Id"] = request.state.request_id
            return response
        finally:
            # Path only: the query string can carry access_key and the target URL.
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
            )

    async def authenticate(request: Request, *, allow_query: bool = True) -> ApiKey:
        raw = None
        auth = request.headers.get("authorization", "")
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
        allowed, used = await asyncio.to_thread(store.try_consume, key.id, month, plan.monthly_quota)
        if not allowed:
            raise ApiError(
                429,
                "quota_exceeded",
                f"This key has used its {plan.monthly_quota} screenshots for {month} on the "
                f"'{plan.name}' plan. The count resets at {_next_month_start()}. "
                + upgrade_text(settings.plans, plan, site_origin, billing.configured),
                plan=plan.name,
                limit=plan.monthly_quota,
                used=used,
                resets_at=_next_month_start(),
            )
        try:
            result = await renderer.capture(params)
        except CaptureError as e:
            await asyncio.to_thread(store.refund, key.id, month)
            raise ApiError(e.status, e.code, e.message, e.headers)
        except Exception:
            await asyncio.to_thread(store.refund, key.id, month)
            log.exception("capture failed", extra={"fields": {"request_id": request.state.request_id}})
            raise ApiError(500, "internal_error", "The screenshot failed. It was not counted against your quota.")

        total_ok = await asyncio.to_thread(store.record_success, key.id)
        if total_ok == 1 and not key.internal:
            # A new contact for the demand test: a non-internal key's first success.
            ordinal = await asyncio.to_thread(store.contact_ordinal, key.id)
            log_event(log, "first successful screenshot", key_id=key.id, contact_ordinal=ordinal)

        log_event(
            log,
            "screenshot",
            request_id=request.state.request_id,
            key_id=key.id,
            target=target.display,
            format=params.format,
            full_page=params.full_page,
            bytes=len(result.image),
            duration_ms=result.duration_ms,
            target_status=result.target_status,
            blocked_requests=result.blocked_requests,
            truncated=result.truncated,
        )
        upgrade = ""
        if used >= NEAR_LIMIT * plan.monthly_quota:
            upgrade = "This key is near its monthly limit. " + upgrade_text(settings.plans, plan, site_origin, billing.configured)
        return Captured(result=result, params=params, used=used, limit=plan.monthly_quota, upgrade=upgrade)

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
        if result.truncated:
            headers["X-Calotype-Truncated"] = "1"
        return Response(content=result.image, media_type=result.content_type, headers=headers)

    # The remote MCP server: the same keys, quota and capture path, over MCP.
    mcp = RemoteMCP(
        authenticate=lambda request: authenticate(request, allow_query=False),
        capture=capture_for_key,
        api_error=ApiError,
        site_origin=settings.public_url or SITE_ORIGIN,
        free_quota=settings.plans["free"].monthly_quota if "free" in settings.plans else None,
        tag_source=lambda key, tag: asyncio.to_thread(app.state.storage.tag_source, key.id, tag),
    )
    app.state.mcp = mcp
    app.add_route("/mcp", mcp, methods=["GET", "POST", "DELETE"], include_in_schema=False)

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
        return await handle_screenshot(request, data)

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

    @app.post("/v1/keys", status_code=201)
    async def create_free_key(request: Request):
        """Create a free-plan key. Asks for nothing and stores no personal data."""
        if not settings.self_serve_keys or "free" not in settings.plans:
            raise ApiError(403, "self_serve_disabled", "Self-serve keys are not available right now.")
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
        # Optional {"ref": "<tag>"} from the site's key button, copied from the
        # page's ?ref= tag. Anything that is not a valid tag is ignored, never an error.
        label = ""
        try:
            body = await request.json()
        except Exception:
            body = None
        ref = body.get("ref") if isinstance(body, dict) else None
        if isinstance(ref, str) and REF_TAG.match(ref):
            label = REF_LABEL_PREFIX + ref
        raw, key = await asyncio.to_thread(app.state.storage.create_key, "free", label, internal=False)
        request.state.key_id = key.id
        plan = settings.plans["free"]
        return JSONResponse(
            {
                "key": raw,
                "plan": plan.name,
                "monthly_quota": plan.monthly_quota,
                "message": "This is the only time the key is shown. Calotype stores only its hash and cannot recover it.",
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/v1/usage")
    async def usage(request: Request):
        key = await authenticate(request)
        plan = settings.plans.get(key.plan)
        month = current_month()
        used = await asyncio.to_thread(app.state.storage.get_usage, key.id, month)
        return {
            "plan": key.plan,
            "month": month,
            "used": used,
            "limit": plan.monthly_quota if plan else 0,
            "resets_at": _next_month_start(),
            # Not secret: lets the holder name this key to support without sending it.
            "key_ref": request.state.key_ref,
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
        try:
            result = await asyncio.to_thread(provider.handle_event, event, app.state.storage)
        except BillingProviderError:
            # 5xx makes Stripe deliver the event again later.
            raise ApiError(500, "webhook_failed", "The event could not be applied now; it will be retried.")
        except Exception:
            log.exception("webhook failed", extra={"fields": {"event_type": event.get("type")}})
            raise ApiError(500, "webhook_failed", "The event could not be applied now; it will be retried.")
        return billing_response({"received": True, "outcome": result.outcome})

    @app.get("/v1/plans")
    async def plans():
        return {
            # Settled once paid plans are on sale on this server.
            "provisional": not billing.configured,
            "plans": [
                {"name": p.name, "title": p.title, "monthly_quota": p.monthly_quota, "price_usd": p.price_usd_cents / 100}
                for p in settings.plans.values()
            ],
        }

    if settings.serve_site and SITE_DIR.is_dir():
        add_site_routes(app, SITE_DIR, paid_open=billing.configured)

    return app
