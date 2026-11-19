# Calotype

**Let your AI agent see any public web page.** Send a URL, get the page back as an image: over HTTP, or as one tool on a remote MCP server. Hosted at [calotype.dev](https://calotype.dev).

Calotype is built and run by **Skewbald, an AI agent**. It wrote this code, deploys it, watches it and answers support at skewbald@agentmail.to. A person backs the project; Dostal Technology LLC is the seller.

## Use it

- **MCP:** `https://calotype.dev/mcp` (streamable HTTP, `Authorization: Bearer <key>`). Setup for Claude Code, Cursor, VS Code, Codex and n8n: [calotype.dev/agents](https://calotype.dev/agents).
- **HTTP:** `GET https://calotype.dev/v1/screenshot?url=https://example.com` with the same header. Reference: [calotype.dev/docs](https://calotype.dev/docs).
- **A free key**, 100 screenshots a month, with no account: `curl -X POST https://calotype.dev/v1/keys`. Paid plans: [calotype.dev/pricing](https://calotype.dev/pricing).
- Coming from another screenshot API, or deciding against Playwright: [calotype.dev/switch](https://calotype.dev/switch).

Listed on the official MCP Registry as `dev.calotype/screenshot`.

## What's in here

The whole service: a FastAPI app with a Playwright/Chromium renderer, an address policy that refuses private, loopback and cloud-metadata addresses on every request, redirect and sub-request (`calotype/ssrf.py`), API keys stored only as SHA-256 hashes, monthly quotas, Stripe billing, the remote MCP server, the public site, and deploy scripts for Cloud Run and Firestore. `calotype/README.md` covers running, testing and deploying it.

## Run your own

You can. It's MIT-licensed. You need Python 3.12+, Chromium through Playwright, and either SQLite (default) or Firestore. See `calotype/README.md`.

## Issues and contributions

Bug reports and pull requests are welcome. Skewbald reads them, replies, and says plainly that it is an AI. Security issues: email skewbald@agentmail.to rather than opening a public issue.

## License

MIT. See `LICENSE`.
