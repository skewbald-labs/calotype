"""One-click install links for Cursor and VS Code, so they can be regenerated and checked.

Run ``python -m calotype.install_links`` to print every link with its decoded config.
The README and ``site/pages/agents.html`` carry these links; ``tests/test_site.py``
checks that they match what this module builds.

Neither link carries a key. VS Code prompts for it (an ``inputs`` entry with
``password: true``) and keeps it in its secret storage. Cursor's install link has no
prompt, so its config reads the key from the ``CALOTYPE_KEY`` environment variable
(``${env:...}`` interpolation in ``headers``, documented at cursor.com/docs/mcp).

Formats, as documented on 20 November 2026:

* Cursor (cursor.com/docs/mcp/install-links):
  ``cursor://anysphere.cursor-deeplink/mcp/install?name=NAME&config=BASE64(JSON)``, where the
  JSON is the server's own entry from ``mcp.json``. GitHub drops ``cursor:`` links from
  READMEs, so the README uses ``https://cursor.com/en/install-mcp`` with the same query,
  which is the web form other READMEs use (it is not on the docs page).
* VS Code (code.visualstudio.com/api/extension-guides/ai/mcp):
  ``vscode:mcp/install?`` + URL-encoded JSON of ``{"name": ..., <server config>, "inputs": [...]}``,
  and for web pages ``https://vscode.dev/redirect?url=`` + the URL-encoded ``vscode:`` link.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote

SERVER_NAME = "calotype"
MCP_URL = "https://calotype.dev/mcp"
KEY_ENV = "CALOTYPE_KEY"
VSCODE_INPUT_ID = "calotype-key"

CURSOR_BADGE = "https://cursor.com/deeplink/mcp-install-dark.svg"
VSCODE_BADGE = "https://img.shields.io/badge/VS_Code-Install_Calotype-0098FF?style=flat-square"


def _uri_component(text: str) -> str:
    """JavaScript's encodeURIComponent, which both clients' docs use."""
    return quote(text, safe="-_.!~*'()")


def cursor_config(ref: str) -> dict:
    return {"url": f"{MCP_URL}?ref={ref}", "headers": {"Authorization": f"Bearer ${{env:{KEY_ENV}}}"}}


def vscode_config(ref: str) -> dict:
    return {
        "name": SERVER_NAME,
        "type": "http",
        "url": f"{MCP_URL}?ref={ref}",
        "headers": {"Authorization": f"Bearer ${{input:{VSCODE_INPUT_ID}}}"},
        "inputs": [
            {"type": "promptString", "id": VSCODE_INPUT_ID, "description": "Calotype API key, starts cal_", "password": True}
        ],
    }


def _json(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":"))


def cursor_deeplink(ref: str) -> str:
    config = base64.b64encode(_json(cursor_config(ref)).encode("utf-8")).decode("ascii")
    return f"cursor://anysphere.cursor-deeplink/mcp/install?name={SERVER_NAME}&config={_uri_component(config)}"


def cursor_web_link(ref: str) -> str:
    query = cursor_deeplink(ref).split("?", 1)[1]
    return f"https://cursor.com/en/install-mcp?{query}"


def vscode_deeplink(ref: str) -> str:
    return "vscode:mcp/install?" + _uri_component(_json(vscode_config(ref)))


def vscode_web_link(ref: str) -> str:
    return "https://vscode.dev/redirect?url=" + _uri_component(vscode_deeplink(ref))


@dataclass(frozen=True)
class Buttons:
    cursor: str
    vscode: str


# The README is read on GitHub, which needs https links; the site links straight to the apps.
README = Buttons(cursor=cursor_web_link("btn-cursor"), vscode=vscode_web_link("btn-vscode"))
SITE = Buttons(cursor=cursor_deeplink("btn-cursor-site"), vscode=vscode_deeplink("btn-vscode-site"))


def readme_markdown() -> str:
    return (
        f"[![Add to Cursor]({CURSOR_BADGE})]({README.cursor})\n"
        f"[![Install in VS Code]({VSCODE_BADGE})]({README.vscode})"
    )


def main() -> None:
    for label, ref, cursor_link, vscode_link in (
        ("README", "btn-cursor / btn-vscode", README.cursor, README.vscode),
        ("site /agents", "btn-cursor-site / btn-vscode-site", SITE.cursor, SITE.vscode),
    ):
        print(f"== {label} ({ref})")
        print(f"Cursor:  {cursor_link}")
        print(f"VS Code: {vscode_link}")
    print("\n== decoded")
    print("Cursor config:", _json(cursor_config("btn-cursor")))
    print("VS Code config:", _json(vscode_config("btn-vscode")))
    print("\n== README markdown\n" + readme_markdown())


if __name__ == "__main__":
    main()
