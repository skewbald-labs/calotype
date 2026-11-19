"""Rendering: one Chromium, a fresh context per capture, closed after."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, Error as PlaywrightError, Playwright, Route, async_playwright

from .egress import EgressSession
from .params import CONTENT_TYPES, ScreenshotParams
from .ssrf import AddressPolicy, BlockedTarget, is_blocked_hostname, parse_ip_literal

log = logging.getLogger("calotype.capture")

# Schemes a page may use for sub-resources. data: and blob: never touch the
# network; everything else that is not http(s) is aborted.
_INPAGE_LOCAL_SCHEMES = {"data", "blob"}

CHROMIUM_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--dns-prefetch-disable",
    "--no-pings",
    "--disable-quic",
    # WebRTC may not open UDP paths around the proxy.
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-features=WebRtcHideLocalIpsWithMdns,DialMediaRouteProvider,MediaRouter,Prerender2",
]


class CaptureError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


@dataclass
class CaptureResult:
    image: bytes
    content_type: str
    target_status: int | None
    truncated: bool
    blocked_requests: int
    duration_ms: int
    meta: dict = field(default_factory=dict)
    # The page's address after redirects. Returned to the caller (the MCP tool
    # shows it); never stored or logged.
    final_url: str | None = None


def load_blocklist(path: Path) -> frozenset[str]:
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return frozenset()
    return frozenset(ln.strip().lower() for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))


def host_in_blocklist(host: str, blocklist: frozenset[str]) -> bool:
    h = host.lower().rstrip(".")
    while h:
        if h in blocklist:
            return True
        if "." not in h:
            return False
        h = h.split(".", 1)[1]
    return False


class Renderer:
    def __init__(
        self,
        policy: AddressPolicy,
        *,
        max_concurrency: int = 4,
        queue_timeout_s: float = 10,
        capture_timeout_s: float = 30,
        max_page_bytes: int = 50 * 1024 * 1024,
        blocklist: frozenset[str] = frozenset(),
    ):
        self.policy = policy
        self.max_concurrency = max_concurrency
        self.queue_timeout_s = queue_timeout_s
        self.capture_timeout_s = capture_timeout_s
        self.max_page_bytes = max_page_bytes
        self.blocklist = blocklist
        self._sem = asyncio.Semaphore(max_concurrency)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()
        self.in_flight = 0

    async def start(self) -> None:
        await self._get_browser()

    async def stop(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @property
    def browser_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def _get_browser(self) -> Browser:
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._pw is None:
                self._pw = await async_playwright().start()
            log.info("launching chromium")
            self._browser = await self._pw.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            return self._browser

    async def capture(self, params: ScreenshotParams) -> CaptureResult:
        try:
            await asyncio.wait_for(self._sem.acquire(), self.queue_timeout_s)
        except asyncio.TimeoutError:
            raise CaptureError(503, "busy", "All renderers are busy. Retry shortly.", {"Retry-After": "5"})
        self.in_flight += 1
        try:
            async with asyncio.timeout(self.capture_timeout_s):
                return await self._capture(params)
        except TimeoutError:
            raise CaptureError(504, "timeout", f"The page did not finish within {self.capture_timeout_s:g} seconds.")
        finally:
            self.in_flight -= 1
            self._sem.release()

    async def _capture(self, params: ScreenshotParams) -> CaptureResult:
        started = time.monotonic()
        deadline = started + self.capture_timeout_s
        browser = await self._get_browser()
        session = await EgressSession(policy=self.policy, max_bytes=self.max_page_bytes).start()
        context = None
        blocked = 0
        try:
            context = await browser.new_context(
                viewport={"width": params.width, "height": params.height},
                device_scale_factor=params.device_scale_factor,
                color_scheme="dark" if params.dark_mode else "light",
                proxy={"server": session.proxy_url, "bypass": "<-loopback>"},
                service_workers="block",
                accept_downloads=False,
                ignore_https_errors=False,
            )

            async def route_handler(route: Route) -> None:
                nonlocal blocked
                url = route.request.url
                scheme = url.split(":", 1)[0].lower()
                if scheme in _INPAGE_LOCAL_SCHEMES:
                    await route.continue_()
                    return
                if scheme not in ("http", "https"):
                    blocked += 1
                    await route.abort("blockedbyclient")
                    return
                try:
                    host = (urlsplit(url).hostname or "").lower()
                except ValueError:
                    host = ""
                if not host or self._plainly_blocked(host, url):
                    blocked += 1
                    await route.abort("blockedbyclient")
                    return
                if params.block_ads and host_in_blocklist(host, self.blocklist):
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await context.route("**/*", route_handler)
            page = await context.new_page()
            remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
            page.set_default_timeout(remaining_ms)
            try:
                response = await self._goto(page, params, remaining_ms, session)
            except PlaywrightError as e:
                if session.exceeded:
                    raise CaptureError(
                        422, "page_too_large", f"The page downloaded more than {self.max_page_bytes} bytes."
                    )
                if session.blocked_hosts or blocked:
                    raise CaptureError(
                        422, "blocked_target", "The page or a redirect pointed to a private or disallowed address."
                    )
                if "Timeout" in type(e).__name__ or "Timeout" in str(e).split("\n", 1)[0]:
                    raise CaptureError(504, "timeout", "The page did not load in time.")
                raise CaptureError(422, "target_unreachable", _short_nav_error(str(e)))

            if response is not None:
                if response.headers.get("x-calotype-blocked"):
                    raise CaptureError(
                        422, "blocked_target", "The page or a redirect pointed to a private or disallowed address."
                    )
                req = response.request
                while req is not None:
                    if req.url.split(":", 1)[0].lower() not in ("http", "https"):
                        raise CaptureError(422, "blocked_target", "The page redirected to a disallowed scheme.")
                    req = req.redirected_from
            if page.url.split(":", 1)[0].lower() not in ("http", "https"):
                if session.exceeded:
                    raise CaptureError(
                        422, "page_too_large", f"The page downloaded more than {self.max_page_bytes} bytes."
                    )
                if page.url.startswith("chrome-error:"):
                    if session.blocked_hosts or blocked:
                        raise CaptureError(
                            422, "blocked_target", "The page or a redirect pointed to a private or disallowed address."
                        )
                    raise CaptureError(422, "target_unreachable", "The page could not be loaded.")
                raise CaptureError(422, "blocked_target", "The page ended on a disallowed scheme.")

            if params.delay_ms:
                await asyncio.sleep(params.delay_ms / 1000)

            image = await self._screenshot(page, params)
            return CaptureResult(
                image=image,
                content_type=CONTENT_TYPES[params.format],
                target_status=response.status if response is not None else None,
                truncated=session.exceeded,
                blocked_requests=blocked + len(session.blocked_hosts),
                duration_ms=int((time.monotonic() - started) * 1000),
                meta={"egress_bytes": session.bytes_in},
                final_url=page.url,
            )
        finally:
            if context is not None:
                try:
                    await context.close()
                except PlaywrightError:
                    pass
            await session.close()

    async def _goto(self, page, params: ScreenshotParams, timeout_ms: int, session: EgressSession):
        """Navigate, but stop waiting as soon as the page passes the byte cap."""
        nav = asyncio.ensure_future(page.goto(params.url, wait_until=params.wait_until, timeout=timeout_ms))
        cut = asyncio.ensure_future(session.exceeded_event.wait())
        try:
            await asyncio.wait({nav, cut}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            cut.cancel()
        if nav.done():
            return nav.result()
        # Over the cap mid-load: stop loading and capture what arrived.
        nav.add_done_callback(lambda f: f.cancelled() or f.exception())
        nav.cancel()
        try:
            await asyncio.wait_for(page.evaluate("() => window.stop()"), 2)
        except (PlaywrightError, asyncio.TimeoutError):
            pass
        return None

    def _plainly_blocked(self, host: str, url: str) -> bool:
        """Cheap check in the router; the proxy does the full check with DNS."""
        literal = None
        try:
            literal = parse_ip_literal(host)
        except BlockedTarget:
            return True
        if literal is not None:
            parts = urlsplit(url)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            return not self.policy.ip_allowed(literal, port)
        return is_blocked_hostname(host)

    async def _screenshot(self, page, params: ScreenshotParams) -> bytes:
        kind = "jpeg" if params.format == "jpeg" else "png"
        kwargs: dict = {"type": kind, "animations": "disabled", "caret": "hide"}
        if kind == "jpeg":
            kwargs["quality"] = params.effective_quality()
        if params.full_page:
            kwargs["full_page"] = True
            height = await page.evaluate(
                "() => Math.max(document.documentElement ? document.documentElement.scrollHeight : 0,"
                " document.body ? document.body.scrollHeight : 0)"
            )
            cap = params.full_page_height_cap()
            if int(height or 0) > cap:
                kwargs["clip"] = {"x": 0, "y": 0, "width": params.width, "height": cap}
        data = await page.screenshot(**kwargs)
        if params.format == "webp":
            data = await asyncio.to_thread(_to_webp, data, params.effective_quality() or 80)
        return data


def _to_webp(png: bytes, quality: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        out = io.BytesIO()
        im.save(out, format="WEBP", quality=quality, method=4)
        return out.getvalue()


def _short_nav_error(msg: str) -> str:
    first = msg.split("\n", 1)[0]
    for token in first.split():
        if token.startswith("net::ERR_"):
            return f"The page could not be loaded ({token.rstrip(':;,')})."
    return "The page could not be loaded."
