"""The remote MCP server at ``/mcp``: one tool, ``screenshot``, over streamable HTTP.

Any MCP client that takes a URL and a header can use it, with nothing to
install:

    URL:     https://calotype.dev/mcp
    Header:  Authorization: Bearer cal_...

Built on the official MCP Python SDK (``mcp``, already a dependency for the
stdio server in ``mcp_server.py``). The SDK owns the protocol: the
``initialize`` handshake and version negotiation (2024-11-05 through
2025-11-25, and the 2026-07-28 per-request envelope), ``tools/list``,
``tools/call``, ``ping``, JSON-RPC errors, and the streamable-HTTP rules for
``Accept``, ``Content-Type`` and ``MCP-Protocol-Version``.

The transport runs **stateless**, with plain JSON responses rather than SSE
streams: Cloud Run may send each request to a different instance, so a
session held in one instance's memory would not be found by the next. The
tool needs nothing from earlier requests, and never calls back to the client.

What this module adds around the SDK:

* **Auth.** A capture needs the caller's Calotype key in
  ``Authorization: Bearer <key>``, checked by the same function as
  ``/v1/screenshot`` (but never from the query string). A wrong or revoked
  key answers HTTP 401 before the SDK sees the request. With no key at all,
  ``initialize``, ``tools/list`` and ``ping`` work, so directories and
  inspectors can see the tool; ``tools/call`` answers a tool error that says
  how to get a free key in one call, and captures nothing.
* **OAuth.** With OAuth on (``oauth.py``), every 401 carries
  ``WWW-Authenticate: Bearer resource_metadata="..."`` (RFC 9728), which is
  how OAuth clients find the sign-in. A keyless ``tools/call`` then answers
  HTTP 401 with that header instead of the tool error, because claude.ai and
  other OAuth clients start sign-in only on a 401, never on a 200 tool result.
  ``initialize``, ``tools/list`` and ``ping`` still work with no key.
  ``CALOTYPE_MCP_KEYLESS_CALL=result`` keeps the old tool error instead.
* **Origin.** The transport spec says a server must refuse a request whose
  ``Origin`` header is present and not allowed. Allowed are the site's own
  origin and the web apps of the MCP clients that connect from their own
  servers and may name themselves in ``Origin`` (``CLIENT_ORIGINS``:
  Claude and ChatGPT; Anthropic's connector docs warn that strict Origin
  checks reject its requests). Programs that are not browsers send no
  ``Origin``. No cookie or other ambient credential exists here, so an
  allowed origin gains nothing a key holder does not already have. The ``Host``
  check of the SDK's DNS-rebinding protection is off: it guards servers on
  localhost, while this one is public and every request needs a key.
* **Source tag.** A ``?ref=<tag>`` on the endpoint URL (each listing's
  config line carries its own) is stored on a key the first time the key is
  used with one, if the key has no tag yet, so ``admin contacts`` can name the
  listing that brought it. It is never the key, and a bad tag is ignored.
* **Methods.** Only POST. Stateless, the server has no stream to offer on
  GET and no session to end on DELETE; both answer 405, as the spec allows.
  A browser's GET (``Accept`` names ``text/html`` and no event stream) gets
  the same 405 as a short page for a person (``keypages.py``).
* **Client name.** An ``initialize`` carries ``clientInfo``. Its name and
  version, cleaned to a few safe characters (``logs.clean_name``), are left
  in the request's state for the app's request log line. The body is only
  watched as the SDK reads it; nothing in any response changes.
* **One capture path.** The tool calls the app's ``capture_for_key``, the
  function behind ``/v1/screenshot``: the same plan check, parameter limits,
  SSRF policy, quota, refund of failed captures, and logging.
"""

from __future__ import annotations

import io
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from . import __version__
from .logs import MCP_CLIENT_MAX, MCP_CLIENT_VERSION_MAX, clean_name
from .params import MAX_HEIGHT, MAX_WIDTH, MIN_SIDE

log = logging.getLogger("calotype.mcp")

# Where the key that passed the HTTP check is left for the tool, in the
# request's state (shared by every Request object made from one ASGI scope).
_KEY_STATE = "calotype_key"

# Where an initialize's client name and version are left for the app's request log line.
_CLIENT_STATE = "mcp_client"
# The most of a body kept to read an initialize's clientInfo. MCP requests are small;
# a bigger body is passed on unread.
_MAX_PEEK_BYTES = 64 * 1024

# JSON-RPC's range for errors an implementation defines itself.
# Web apps of MCP clients whose requests may carry their own Origin (see "Origin" above).
CLIENT_ORIGINS = frozenset(
    {"https://claude.ai", "https://claude.com", "https://chatgpt.com", "https://chat.openai.com"}
)

_UNAUTHORIZED = -32001
_FORBIDDEN = -32003

INSTRUCTIONS = (
    "Calotype lets you see any public web page. Call `screenshot` with a page's http(s) address; "
    "it returns the rendered page as an image. Private, local and cloud-metadata addresses are refused."
)

TOOL_DESCRIPTION = (
    "Take a screenshot of a public web page and return it as an image. "
    "By default the image is the first screen only (the viewport, 1280x800); the result says how tall the "
    "whole page is, and full_page=true captures all of it. "
    "Use it to see what a page looks like: its layout, text, charts or errors as rendered in Chromium. "
    "Only public http(s) pages; private and local addresses are refused. "
    "Each successful call counts as one screenshot against the key's monthly quota; failed ones do not. "
    "Full-page captures of long pages can be large; jpeg or webp keeps them small."
)


def key_help(site_origin: str, free_quota: int | None, oauth: bool = False) -> str:
    """What to tell a caller who sent no key, or a wrong one."""
    free = f"{free_quota} screenshots a month, " if free_quota else ""
    text = (
        "Calotype needs an API key in the header 'Authorization: Bearer <key>'. "
        f"Get a free key ({free}no sign-up) at {site_origin}/#key, or with: curl -X POST {site_origin}/v1/keys"
    )
    if oauth:
        text += ". Or sign in with OAuth: a client that supports it makes a free key on approval."
    return text


class RemoteMCP:
    """The MCP server, its HTTP endpoint and its lifecycle, for one app.

    ``authenticate(request)`` returns the request's key or raises
    ``api_error``; ``capture(request, key, data)`` is the app's capture path
    and raises ``api_error`` for anything that is not an image.
    """

    def __init__(
        self,
        *,
        authenticate: Callable[[Request], Awaitable[Any]],
        capture: Callable[[Request, Any, dict[str, Any]], Awaitable[Any]],
        api_error: type[Exception],
        site_origin: str,
        free_quota: int | None,
        tag_source: Callable[[Any, str], Awaitable[None]] | None = None,
        challenge: Callable[[Request, str | None], str] | None = None,
        challenge_keyless_calls: bool = False,
        browser_page: Callable[[Request], Any] | None = None,
    ):
        # With the site on: the page a browser gets when a person opens this address
        # (keypages.BrowserPages.mcp). It returns None for anything that is not a browser's GET.
        self._browser_page = browser_page
        # With OAuth on: the WWW-Authenticate value for a 401 (oauth.OAuthServer.challenge_for),
        # and whether a keyless tools/call answers 401 rather than a tool error.
        self._challenge = challenge
        self._challenge_keyless_calls = bool(challenge) and challenge_keyless_calls
        self._authenticate = authenticate
        self._tag_source = tag_source
        self._capture = capture
        self._api_error = api_error
        self.site_origin = site_origin.rstrip("/")
        self.key_help = key_help(self.site_origin, free_quota, oauth=challenge is not None)
        self.server = self._build_server()
        self._manager = None

    # --- the MCP server ----------------------------------------------------------------

    def _build_server(self) -> MCPServer:
        server = MCPServer(
            "calotype",
            title="Calotype",
            instructions=INSTRUCTIONS,
            website_url=self.site_origin,
            version=__version__,
        )
        server.add_tool(
            self.screenshot,
            name="screenshot",
            title="Screenshot a web page",
            description=TOOL_DESCRIPTION,
            annotations=ToolAnnotations(
                title="Screenshot a web page",
                read_only_hint=True,
                destructive_hint=False,
                open_world_hint=True,
            ),
            structured_output=False,
        )
        return server

    async def screenshot(
        self,
        url: Annotated[
            str, Field(min_length=1, max_length=2048, description="The page to capture: a public http or https address.")
        ],
        ctx: Context,
        full_page: Annotated[
            bool, Field(description="Capture the whole scrolling page (up to 20,000 px tall) instead of the viewport. The page is scrolled through first, so sections that appear on scroll are shown.")
        ] = False,
        width: Annotated[int, Field(ge=MIN_SIDE, le=MAX_WIDTH, description="Viewport width in CSS pixels.")] = 1280,
        height: Annotated[int, Field(ge=MIN_SIDE, le=MAX_HEIGHT, description="Viewport height in CSS pixels.")] = 800,
        format: Annotated[
            Literal["png", "jpeg", "webp"], Field(description="Image format. jpeg and webp are much smaller than png.")
        ] = "jpeg",
        dark_mode: Annotated[bool, Field(description="Ask the page for its dark colour scheme.")] = False,
        block_ads: Annotated[bool, Field(description="Block requests to common ad and tracking domains.")] = True,
    ) -> list[ImageContent | TextContent]:
        request = ctx.request_context.request
        state = getattr(request, "state", None)
        key = getattr(state, _KEY_STATE, None) if state is not None else None
        if key is None:  # no key was sent: say how to get one
            raise ToolError(self.key_help)
        data = {
            "url": url,
            "full_page": full_page,
            "width": width,
            "height": height,
            "format": format,
            "dark_mode": dark_mode,
            "block_ads": block_ads,
        }
        try:
            shot = await self._capture(request, key, data)
        except self._api_error as e:
            raise ToolError(_error_text(e)) from None
        result = shot.result
        return [
            Image(data=result.image, format=shot.params.format).to_image_content(),
            TextContent(type="text", text=_summary(shot)),
        ]

    # --- HTTP -----------------------------------------------------------------------------

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run the SDK's session manager for the app's lifetime (from its lifespan)."""
        # Building the SDK's app makes a fresh session manager; it can run only once.
        self.server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        manager = self.server.session_manager
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The ASGI app for ``/mcp``: check the origin and the key, then hand the request to the SDK."""
        request = Request(scope, receive)
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != self.site_origin and origin.rstrip("/").lower() not in CLIENT_ORIGINS:
            await _rpc_error(403, _FORBIDDEN, "This origin may not call the MCP endpoint.", "forbidden_origin")(
                scope, receive, send
            )
            return
        if request.method != "POST":
            # A person who followed a directory's link here in a browser gets a page that says
            # what this address is, with the same 405 and Allow header. Any client that asks
            # for an event stream, or does not ask for HTML, gets the JSON below as before.
            page = self._browser_page(request) if self._browser_page is not None else None
            if page is not None:
                await page(scope, receive, send)
                return
            # Stateless: no server-to-client stream (GET) and no session to end
            # (DELETE). The transport spec allows 405 for both.
            await _rpc_error(
                405, -32600, "This MCP endpoint takes POST only; it opens no event stream.", "method_not_allowed",
                {"Allow": "POST"},
            )(scope, receive, send)
            return
        try:
            key = await self._authenticate(request)
        except self._api_error as e:
            code = getattr(e, "code", "invalid_key")
            if code != "missing_key":
                message = f"The API key is not valid. {self.key_help}"
                header = {"WWW-Authenticate": self._www_authenticate(request, "invalid_token")}
                await _rpc_error(401, _UNAUTHORIZED, message, code, header)(scope, receive, send)
                return
            # No key at all: let the client connect and list the tool, so
            # directories and inspectors can see what the server offers. A
            # tool call without a key answers with how to get one, and
            # captures nothing.
            key = None
        if key is None and self._challenge_keyless_calls:
            # OAuth clients sign in only after an HTTP 401 with WWW-Authenticate, so a
            # keyless tool call gets one; the discovery methods above still pass.
            body = await request.body()
            call_id = _tool_call_id(body)
            if call_id is not _NO_CALL:
                header = {"WWW-Authenticate": self._www_authenticate(request, None)}
                await _rpc_error(401, _UNAUTHORIZED, self.key_help, "missing_key", header, call_id)(
                    scope, receive, send
                )
                return
            receive = _replay(body, receive)
        if key is not None:
            setattr(request.state, _KEY_STATE, key)
            ref = request.query_params.get("ref")
            if ref and self._tag_source is not None and not key.label and not key.internal:
                # The listing's URL carries its own ?ref= tag; an untagged key
                # takes the first one it is used with. Never fails the request.
                try:
                    await self._tag_source(key, ref)
                except Exception:
                    log.warning("could not record the ref tag", exc_info=True)
        if self._manager is None:
            await _rpc_error(503, -32603, "The MCP server is starting. Retry shortly.", "busy")(scope, receive, send)
            return
        await self._manager.handle_request(scope, _noting_client(receive, request), send)


    def _www_authenticate(self, request: Request, error: str | None) -> str:
        return self._challenge(request, error) if self._challenge else "Bearer"


_NO_CALL = object()


def _tool_call_id(body: bytes) -> Any:
    """The JSON-RPC id of a ``tools/call`` in ``body`` (a message or a batch); ``_NO_CALL`` if none."""
    import json

    try:
        data = json.loads(body)
    except ValueError:
        return _NO_CALL
    for message in data if isinstance(data, list) else [data]:
        if isinstance(message, dict) and message.get("method") == "tools/call":
            ident = message.get("id")
            return ident if isinstance(ident, (str, int)) and not isinstance(ident, bool) else None
    return _NO_CALL


def _client_info(body: bytes) -> tuple[str | None, str | None] | None:
    """``(name, version)`` from the ``clientInfo`` of an ``initialize`` in ``body``, cleaned for the log.

    ``None`` when the body holds no ``initialize``; each of the two is ``None`` when absent or not a string.
    """
    import json

    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    for message in data if isinstance(data, list) else [data]:
        if isinstance(message, dict) and message.get("method") == "initialize":
            params = message.get("params")
            info = params.get("clientInfo") if isinstance(params, dict) else None
            info = info if isinstance(info, dict) else {}
            return clean_name(info.get("name"), MCP_CLIENT_MAX), clean_name(info.get("version"), MCP_CLIENT_VERSION_MAX)
    return None


def _noting_client(receive: Receive, request: Request) -> Receive:
    """An ASGI ``receive`` that passes the body on untouched and, once the SDK has read all of it,
    leaves an ``initialize``'s client name and version in the request's state. Never fails the request."""
    body = bytearray()
    done = False

    async def noting() -> dict:
        nonlocal done
        message = await receive()
        if done or message.get("type") != "http.request":
            return message
        try:
            if len(body) <= _MAX_PEEK_BYTES:
                body.extend(message.get("body", b""))
            if not message.get("more_body"):
                done = True
                info = _client_info(bytes(body)) if len(body) <= _MAX_PEEK_BYTES else None
                if info is not None:
                    setattr(request.state, _CLIENT_STATE, info)
        except Exception:
            done = True
            log.warning("could not read the client's name", exc_info=True)
        return message

    return noting


def _replay(body: bytes, receive: Receive) -> Receive:
    """An ASGI ``receive`` that hands the SDK the body this module already read, then the real stream."""
    sent = False

    async def replay() -> dict:
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _rpc_error(
    status: int, rpc_code: int, message: str, code: str, headers: dict[str, str] | None = None, rpc_id: Any = None
):
    """An HTTP error whose body is also a JSON-RPC error, for clients that read either."""
    body = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": rpc_code, "message": message, "data": {"code": code}}}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **(headers or {})})


def _error_text(e: Exception) -> str:
    """An API error as the model will read it: the stable code, then the message."""
    text = f"{getattr(e, 'code', 'error')}: {getattr(e, 'message', str(e))}"
    details = (getattr(e, "extra", None) or {}).get("details")
    if details:
        text += " " + "; ".join(f"{d['field']}: {d['message']}" for d in details)
    return text


def _summary(shot: Any) -> str:
    result, params = shot.result, shot.params
    where = result.final_url or params.url
    n = len(result.image)
    size = f"{n} bytes" if n < 1024 else f"{n / 1024:.0f} KB"
    dims = _dimensions(result.image)
    if dims:
        size = f"{dims[0]}x{dims[1]} px, {size}"
    text = f"Screenshot of {where} ({params.format}, {size})."
    if result.target_status is not None and result.target_status >= 400:
        text += f" The page answered HTTP {result.target_status}."
    if result.truncated:
        text += " The page passed the 50 MB download limit; the image shows what had loaded by then."
    text += below_the_fold(params, (result.meta or {}).get("page_height"))
    text += f" This key has used {shot.used} of {shot.limit} screenshots this month."
    if getattr(shot, "upgrade", ""):
        text += " " + shot.upgrade
    return text


def below_the_fold(params: Any, page_height: int | None) -> str:
    """One sentence for the agent when the image is not the whole page, else ''.

    A default capture is the first screen only. Without this an agent cannot tell a short page
    from the top of a very long one, and reports on a fraction of the page as if it were all of it.
    """
    if not page_height:
        return ""
    if params.full_page:
        cap = params.full_page_height_cap()
        if page_height > cap:
            return f" The page is about {page_height:,} px tall; a full-page capture stops at {cap:,} px, so the rest is not shown."
        return ""
    if page_height <= params.height * 1.25:
        return ""
    screens = -(-page_height // params.height)
    return (
        f" This is only the first screen: the top {params.height:,} px of a page about {page_height:,} px tall"
        f" (about {screens} screens). To see the rest, call again with full_page=true."
    )


def _dimensions(image: bytes) -> tuple[int, int] | None:
    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(image)) as im:  # reads the header only
            return im.size
    except Exception:
        return None


__all__ = ["RemoteMCP", "key_help", "INSTRUCTIONS", "TOOL_DESCRIPTION"]
