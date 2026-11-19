"""Publish server.json to the official MCP Registry with DNS proof, without putting the key in argv.

    MCP_DNS_KEY_FILE=/path/to/hex-key python publish.py server.json

The file holds the Ed25519 private key (hex) whose public half is the `v=MCPv1` TXT record
on calotype.dev. It is read, used once and never printed. `mcp-publisher login dns` does the
same, but takes the key as a command-line argument, where other processes can see it.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REGISTRY = "https://registry.modelcontextprotocol.io"
DOMAIN = "calotype.dev"


def post(path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(REGISTRY + path, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}


def main(server_json: str) -> int:
    with open(os.environ["MCP_DNS_KEY_FILE"]) as f:
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(f.read().strip())[:32])
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status, auth = post("/v0/auth/dns", {"domain": DOMAIN, "timestamp": ts, "signed_timestamp": key.sign(ts.encode()).hex()})
    token = auth.get("registry_token")
    if not token:
        print("login failed", status, auth.get("error", ""), file=sys.stderr)
        return 1
    with open(server_json) as f:
        status, out = post("/v0/publish", json.load(f), token)
    server = out.get("server", {})
    print("publish", status, server.get("name"), server.get("version"), out.get("error", ""))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "server.json"))
