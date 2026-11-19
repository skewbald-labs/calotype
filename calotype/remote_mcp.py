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
* **Origin.** The transport spec says a server must refuse a request whose
  ``Origin`` header is present and not allowed. Only the site's own origin is
  allowed; programs that are not browsers send no ``Origin``. The ``Host``
  check of the SDK's DNS-rebinding protection is off: it guards servers on
  localhost, while this one is public and every request needs a key.
* **Source tag.** A ``?ref=<tag>`` on the endpoint URL (each listing's
  config line carries its own) is stored on a key the first time the key is
  used with one, if the key has no tag yet, so ``admin contacts`` can name the
  listing that brought it. It is never the key, and a bad tag is ignored.
* **Methods.** Only POST. Stateless, the server has no stream to offer on
  GET and no session to end on DELETE; both answer 405, as the spec allows.
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
from .params import MAX_HEIGHT, MAX_WIDTH, MIN_SIDE

log = logging.getLogger("calotype.mcp")

# Where the key that passed the HTTP check is left for the tool, in the
# request's state (shared by every Request object made from one ASGI scope).
_KEY_STATE = "calotype_key"

# JSON-RPC's range for errors an implementation defines itself.
_UNAUTHORIZED = -32001
_FORBIDDEN = -32003

INSTRUCTIONS = (
    "Calotype lets you see any public web page. Call `screenshot` with a page's http(s) address; "
    "it returns the rendered page as an image. Private, local and cloud-metadata addresses are refused."
)

TOOL_DESCRIPTION = (
    "Take a screenshot of a public web page and return it as an image. "
    "Use it to see what a page looks like: its layout, text, charts or errors as rendered in Chromium. "
    "Only public http(s) pages; private and local addresses are refused. "
    "Each successful call counts as one screenshot against the key's monthly quota; failed ones do not. "
    "Full-page captures of long pages can be large; jpeg or webp keeps them small."
)


def key_help(site_origin: str, free_quota: int | None) -> str:
    """What to tell a caller who sent no key, or a wrong one."""
    free = f"{free_quota} screenshots a month, " if free_quota else ""
    return (
        "Calotype needs an API key in the header 'Authorization: Bearer <key>'. "
        f"Get a free key ({free}no sign-up) at {site_origin}/#key, or with: curl -X POST {site_origin}/v1/keys"
    )


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
    ):
        self._authenticate = authenticate
        self._tag_source = tag_source
        self._capture = capture
        self._api_error = api_error
        self.site_origin = site_origin.rstrip("/")
        self.key_help = key_help(self.site_origin, free_quota)
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
            bool, Field(description="Capture the whole scrolling page (up to 20,000 px tall) instead of the viewport.")
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
        if origin and origin.rstrip("/") != self.site_origin:
            await _rpc_error(403, _FORBIDDEN, "This origin may not call the MCP endpoint.", "forbidden_origin")(
                scope, receive, send
            )
            return
        if request.method != "POST":
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
                await _rpc_error(401, _UNAUTHORIZED, message, code, {"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
            # No key at all: let the client connect and list the tool, so
            # directories and inspectors can see what the server offers. A
            # tool call without a key answers with how to get one, and
            # captures nothing.
            key = None
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
        await self._manager.handle_request(scope, receive, send)


def _rpc_error(status: int, rpc_code: int, message: str, code: str, headers: dict[str, str] | None = None):
    """An HTTP error whose body is also a JSON-RPC error, for clients that read either."""
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": rpc_code, "message": message, "data": {"code": code}}}
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
    text += f" This key has used {shot.used} of {shot.limit} screenshots this month."
    if getattr(shot, "upgrade", ""):
        text += " " + shot.upgrade
    return text


def _dimensions(image: bytes) -> tuple[int, int] | None:
    from PIL import Image as PILImage

    try:
        with PILImage.open(io.BytesIO(image)) as im:  # reads the header only
            return im.size
    except Exception:
        return None


__all__ = ["RemoteMCP", "key_help", "INSTRUCTIONS", "TOOL_DESCRIPTION"]
