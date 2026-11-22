"""The funnel: counts of the steps an agent leaves behind on its way to a key.

The question it answers: did agents try to use Calotype, or to get a key, and
fail? Each step is counted per UTC day in storage (``storage.FunnelDay``),
and nothing else is kept: no URL, no key, no user agent, no IP address.
``python -m calotype.admin funnel`` prints the counts.

The steps, and how each is seen:

``looked``
    ``initialize`` or ``tools/list`` on ``/mcp`` with no ``Authorization``
    header (one count per request; a client that connects and then lists
    the tool makes two). Directory crawlers do this.
``tool_no_key``
    ``tools/call`` on ``/mcp`` with no key: the agent reached the tool and was
    told how to get a key.
``bad_key``
    401 on ``/v1/screenshot`` or ``/mcp`` when a key was sent (in the header,
    or for ``/v1/screenshot`` as ``?access_key=``): a wrong or revoked key,
    or an ``Authorization`` header that is not a bearer token. A 401 for no
    key at all is not counted.
``oauth_probe``
    Any request for OAuth or OpenID metadata (``OAUTH_WELL_KNOWN``, whatever
    the answer, so it keeps counting once ``oauth.py`` answers 200), and
    requests to the usual OAuth step paths (``OAUTH_STEP_PATHS``, and
    ``POST /register``, dynamic client registration).
``key_miss``
    404 or 405 on a path where an agent might look for a key or a sign-up
    (``KEY_MISS_PATHS``), such as ``GET /v1/keys`` or ``/signup``.
``key_refused``
    ``POST /v1/keys`` answered with an error: rate limited, a bad email
    address, self-serve keys switched off.
``key_made``
    ``POST /v1/keys`` answered 201.
``first_capture``
    A non-internal key's first successful screenshot, through the API or the
    MCP tool. ``app.capture_for_key`` marks it with ``mark``.

How it stays out of the way: ``FunnelMiddleware`` looks at the method, the
path, whether a key was sent, the status and, for keyless ``POST /mcp``
only, the JSON-RPC method in the body the server read anyway (up to
``MAX_PEEK_BYTES``). It never reads a body the app did not read. Once the
response has gone, it hands the counts to ``FunnelCounter``, which writes them
on its own small thread pool, one ``Increment`` per step, and drops counts
rather than queue without end if storage is slow. A failed write is logged
and forgotten. The request never waits for a count, and a count can never
fail a request.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from typing import Any
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .storage import FUNNEL_EVENTS, Storage, current_day

log = logging.getLogger("calotype.funnel")

# OAuth and OpenID discovery, by path family: RFC 9728 (protected resource),
# RFC 8414 (authorization server) and OpenID Connect, with or without a suffix
# (``/.well-known/oauth-protected-resource/mcp``) or a prefix (``/mcp/.well-known/...``).
OAUTH_WELL_KNOWN = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)
# The steps after discovery, at the paths MCP clients fall back to when there is
# no metadata, and anything under /oauth/. Add oauth.py's paths here if they differ.
OAUTH_STEP_PATHS = frozenset({"/authorize", "/token", "/revoke"})
OAUTH_STEP_PREFIXES = ("/oauth/",)
# POST /register is dynamic client registration; GET /register is a sign-up page (a key miss).
OAUTH_REGISTER = "/register"

# Where an agent, or a person, might look for a key or a sign-up that Calotype does not have.
# Counted only when the answer is 404 or 405, so a path that later exists stops counting.
# Generic sign-in paths draw scanners as well as agents; read key_miss with that in mind.
KEY_MISS_PATHS = frozenset({
    "/v1/keys",  # GET: only POST makes a key
    "/v1/key",
    "/v1/api-keys",
    "/v1/signup",
    "/v1/register",
    "/api/keys",
    "/api/v1/keys",
    "/api-keys",
    "/keys",
    "/signup",
    "/sign-up",
    "/register",
    "/login",
})
MISS_STATUSES = (404, 405)

KEY_PATH = "/v1/keys"
MCP_PATH = "/mcp"
SCREENSHOT_PATH = "/v1/screenshot"
LOOK_METHODS = frozenset({"initialize", "tools/list"})

# The most of a keyless /mcp body kept to read its JSON-RPC method. MCP requests
# are small; a bigger body is not looked at.
MAX_PEEK_BYTES = 64 * 1024

# Where capture_for_key leaves a step for the middleware to count, in the request's state.
_STATE_EVENTS = "funnel_events"

# Writes waiting or running at once before new counts are dropped (storage is slow or down).
MAX_PENDING = 200


def mark(request: Any, event: str) -> None:
    """Note a step seen inside a handler; the middleware counts it after the response. Never raises."""
    try:
        state = request.state
        events = getattr(state, _STATE_EVENTS, None)
        if events is None:
            events = []
            setattr(state, _STATE_EVENTS, events)
        events.append(event)
    except Exception:
        log.warning("could not mark a funnel step", exc_info=True)


def _norm(path: str) -> str:
    path = path.lower()
    return path.rstrip("/") or "/"


def _rpc_methods(body: bytes) -> set[str]:
    """The JSON-RPC methods in a request body (one message or a batch); empty if unreadable."""
    try:
        data = json.loads(body)
    except Exception:
        return set()
    messages = data if isinstance(data, list) else [data]
    return {m["method"] for m in messages if isinstance(m, dict) and isinstance(m.get("method"), str)}


def classify(
    method: str,
    path: str,
    status: int,
    *,
    key_sent: bool,
    rpc_methods: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """The funnel steps one finished request counts as. ``status`` is 0 if no response started."""
    path = _norm(path)
    method = method.upper()
    events: list[str] = []

    if any(name in path for name in OAUTH_WELL_KNOWN) or path in OAUTH_STEP_PATHS or (
        path.startswith(OAUTH_STEP_PREFIXES)
    ) or (path == OAUTH_REGISTER and method == "POST"):
        return ["oauth_probe"]

    if path == KEY_PATH and method == "POST":
        if status == 201:
            events.append("key_made")
        elif status >= 400:
            events.append("key_refused")
        return events

    if path in KEY_MISS_PATHS and status in MISS_STATUSES:
        return ["key_miss"]

    if status == 401 and key_sent and path in (SCREENSHOT_PATH, MCP_PATH):
        return ["bad_key"]

    if path == MCP_PATH and method == "POST" and not key_sent and 0 < status < 400:
        if rpc_methods & LOOK_METHODS:
            events.append("looked")
        if "tools/call" in rpc_methods:
            events.append("tool_no_key")
    return events


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


def _key_sent(scope: Scope, path: str) -> bool:
    """Whether the request carried a key the API would look at (never what it was)."""
    if _header(scope, b"authorization").strip():
        return True
    if _norm(path) == SCREENSHOT_PATH:  # the only counted path that also reads ?access_key=
        query = (scope.get("query_string") or b"").decode("latin-1")
        return bool(parse_qs(query).get("access_key"))
    return False


class FunnelCounter:
    """Writes funnel counts off the request path: its own threads, bounded, best effort."""

    def __init__(self, storage: Callable[[], Storage | None], *, max_workers: int = 2, max_pending: int = MAX_PENDING):
        self._storage = storage
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="calotype-funnel")
        self._lock = threading.Lock()
        self._pending: set[Future] = set()
        self._dropped = 0

    def count(self, events: list[str], day: str | None = None) -> None:
        """Queue one count per event for ``day`` (today, UTC). Never blocks and never raises."""
        if not events:
            return
        try:
            day = day or current_day()
            with self._lock:
                if len(self._pending) >= self._max_pending:
                    before, self._dropped = self._dropped, self._dropped + len(events)
                    if before == 0 or before // 100 != self._dropped // 100:  # the first, then every 100th
                        log.warning("funnel counts dropped: storage is slow", extra={"fields": {"dropped": self._dropped}})
                    return
                future = self._executor.submit(self._write, day, list(events))
                self._pending.add(future)
            future.add_done_callback(self._done)
        except Exception:
            log.warning("could not queue a funnel count", exc_info=True)

    def _done(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)

    def _write(self, day: str, events: list[str]) -> None:
        store = self._storage()
        if store is None:
            return
        for event in events:
            try:
                store.record_funnel(day, event)
            except Exception:
                log.warning("could not count a funnel step", exc_info=True, extra={"fields": {"event": event}})

    def wait(self, timeout: float = 10.0) -> None:
        """Wait for the counts queued so far to be written (tests, and shutdown)."""
        with self._lock:
            pending = list(self._pending)
        wait_futures(pending, timeout=timeout)

    def close(self) -> None:
        self.wait(timeout=5.0)
        self._executor.shutdown(wait=False, cancel_futures=True)


class FunnelMiddleware:
    """Pure ASGI: watches each request, then counts its funnel steps after the response."""

    def __init__(self, app: ASGIApp, counter: FunnelCounter):
        self.app = app
        self.counter = counter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope.get("method", ""), scope.get("path", "")
        key_sent = _key_sent(scope, path)
        peek = method == "POST" and _norm(path) == MCP_PATH and not key_sent
        body = bytearray()
        status = 0
        # A state dict of our own, so a step marked by a handler is visible here afterwards.
        state = scope.setdefault("state", {})

        async def watching_receive() -> Message:
            message = await receive()
            if message.get("type") == "http.request" and len(body) <= MAX_PEEK_BYTES:
                body.extend(message.get("body", b"")[: MAX_PEEK_BYTES + 1 - len(body)])
            return message

        async def watching_send(message: Message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, watching_receive if peek else receive, watching_send)
        finally:
            try:
                methods = _rpc_methods(bytes(body)) if peek and 0 < len(body) <= MAX_PEEK_BYTES else set()
                events = classify(method, path, status, key_sent=key_sent, rpc_methods=methods)
                marked = state.get(_STATE_EVENTS) if isinstance(state, dict) else None
                if marked:
                    events += [e for e in marked if e in FUNNEL_EVENTS]
                self.counter.count(events)
            except Exception:
                log.warning("could not classify a request for the funnel", exc_info=True)


def install(app: Any) -> FunnelCounter:
    """Add the funnel to a Starlette or FastAPI app; the counter is kept at ``app.state.funnel``."""
    counter = FunnelCounter(lambda: getattr(app.state, "storage", None))
    app.state.funnel = counter
    app.add_middleware(FunnelMiddleware, counter=counter)
    return counter


__all__ = [
    "FUNNEL_EVENTS",
    "KEY_MISS_PATHS",
    "OAUTH_STEP_PATHS",
    "OAUTH_WELL_KNOWN",
    "FunnelCounter",
    "FunnelMiddleware",
    "classify",
    "install",
    "mark",
]
