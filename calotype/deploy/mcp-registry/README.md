# MCP Registry entry (published)

`server.json` describes Calotype's remote MCP server for the official MCP Registry
(`registry.modelcontextprotocol.io`), in the 2025-12-11 `server.json` schema. It
validates against that schema. Published 2026-11-18 21:40 UTC as `dev.calotype/screenshot` 0.1.0.

- Name `dev.calotype/screenshot`: a reverse-DNS name, so publishing needs proof
  that we control `calotype.dev` (the registry's DNS or HTTP domain check).
- One remote, `streamable-http` at `https://calotype.dev/mcp`, with a required,
  secret `Authorization: Bearer {api_key}` header. No package: nothing to install.
- `version` follows `calotype/__init__.py`; bump both together.

- The remote URL carries `?ref=mcp-registry`, so keys first used through it are tagged.

Domain proof: a TXT record on `calotype.dev`, `v=MCPv1; k=ed25519; p=...`, in the
`calotype-dev` Cloud DNS zone. Its private key (hex) is kept in
the operator's secret store. To publish a new version, bump `version` here and in
`calotype/__init__.py`, then from this directory, with HOME and TMPDIR pointed at a scratch folder:
`mcp-publisher login dns --domain calotype.dev --private-key <hex>` and `mcp-publisher publish`.
