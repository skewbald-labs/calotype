"""MCP server for Calotype: one tool, ``screenshot``, over stdio.

Environment:
    CALOTYPE_API_KEY   required, the key sent to the API
    CALOTYPE_API_URL   base URL of the API (default http://127.0.0.1:8080)

Example client config:
    {"command": "python", "args": ["-m", "calotype.mcp_server"],
     "env": {"CALOTYPE_API_KEY": "cal_...", "CALOTYPE_API_URL": "https://..."}}
"""

from __future__ import annotations

import os
from typing import Literal

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer(
    "calotype",
    instructions="Take a screenshot of a public web page with the Calotype API. Returns an image.",
)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=60)


@server.tool()
async def screenshot(
    url: str,
    width: int = 1280,
    height: int = 800,
    full_page: bool = False,
    format: Literal["png", "jpeg", "webp"] = "jpeg",
    dark_mode: bool = False,
    block_ads: bool = True,
    delay_ms: int = 0,
    scroll_y: int = 0,
) -> Image:
    """Capture a screenshot of a public http(s) web page.

    The default is the first screen. scroll_y (CSS px, 0 to 20,000) captures one screen further
    down at the same readable size; past the end, the last screen. It cannot be combined with
    full_page. Full-page captures of long pages can be large and are scaled down by the model;
    jpeg or webp keeps them small.
    """
    key = os.environ.get("CALOTYPE_API_KEY", "").strip()
    if not key:
        raise ToolError("CALOTYPE_API_KEY is not set.")
    base = os.environ.get("CALOTYPE_API_URL", "http://127.0.0.1:8080").rstrip("/")
    body = {
        "url": url,
        "width": width,
        "height": height,
        "full_page": full_page,
        "format": format,
        "dark_mode": dark_mode,
        "block_ads": block_ads,
        "delay_ms": delay_ms,
    }
    if scroll_y:
        body["scroll_y"] = scroll_y
    # ToolError messages reach the model; other exceptions show only a generic error.
    try:
        async with _client() as client:
            resp = await client.post(f"{base}/v1/screenshot", json=body, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        raise ToolError(f"Could not reach the Calotype API ({type(e).__name__}).")
    if resp.status_code != 200:
        try:
            err = resp.json()["error"]
            message = f"{err.get('code')}: {err.get('message')}"
        except Exception:
            message = f"HTTP {resp.status_code}"
        raise ToolError(f"Calotype API error ({message})")
    return Image(data=resp.content, format=format)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
