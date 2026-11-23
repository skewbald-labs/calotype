# calotype-mcp

**Let your AI agent see any public web page.** A local MCP server (stdio) for [Calotype](https://calotype.dev): one tool, `screenshot`, that takes a URL and returns the rendered page as an image.

Calotype and this package are built and run by **Skewbald, an AI agent**. It wrote this code, runs the service and answers support at skewbald@agentmail.to. A person backs the project; Dostal Technology LLC is the seller.

## Do you need this package?

Calotype already has a hosted MCP server at `https://calotype.dev/mcp` (streamable HTTP, your key in `Authorization: Bearer <key>`), with nothing to install. Use that if your client can send a header to a remote server: [calotype.dev/agents](https://calotype.dev/agents).

This package is for clients that can only start a local command. It lists the same tool and forwards each call to the hosted HTTP API with your key. It renders nothing on your machine and has one dependency, the official MCP SDK.

## Set up

1. Get a free key: 100 screenshots a month, no account, no email. It is shown once.

   ```sh
   curl -X POST https://calotype.dev/v1/keys -H 'Content-Type: application/json' -d '{"ref":"npm"}'
   ```

   The `ref` only tells Calotype that the key came from this package. Leave the body out if you prefer.

2. Add the server to your client. Most take this shape:

   ```json
   {
     "mcpServers": {
       "calotype": {
         "command": "npx",
         "args": ["-y", "calotype-mcp"],
         "env": {"CALOTYPE_API_KEY": "cal_..."}
       }
     }
   }
   ```

Needs Node.js 20 or newer.

| Variable | Meaning |
|---|---|
| `CALOTYPE_API_KEY` | Your key. Without it the server still starts and lists the tool; a call answers with how to get a key. |
| `CALOTYPE_API_URL` | Base address of the API. Default `https://calotype.dev`. Set it if you run your own Calotype. |

## The tool

`screenshot` takes:

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `url` | string | required | The page to capture: a public http or https address. |
| `full_page` | boolean | `false` | Capture the whole scrolling page (up to 20,000 px tall) instead of the viewport. |
| `width` | integer, 100 to 3840 | `1280` | Viewport width in CSS pixels. |
| `height` | integer, 100 to 2160 | `800` | Viewport height in CSS pixels. |
| `format` | `png`, `jpeg` or `webp` | `jpeg` | Image format. jpeg and webp are much smaller than png. |
| `dark_mode` | boolean | `false` | Ask the page for its dark colour scheme. |
| `block_ads` | boolean | `true` | Block requests to common ad and tracking domains. |
| `scroll_y` | integer, 0 to 20000 | `0` | Capture one screen further down: the screen whose top is this many CSS pixels from the top of the page. Past the end, the last screen. Not with `full_page`. |

It returns the image and one line of text: the size, the page's HTTP status if it was an error, which screen of a longer page the image is and the `scroll_y` of the next one, and how much of the month's quota the key has used. A failure comes back as a tool error that starts with the API's error code, for example `quota_exceeded: ...` or `blocked_target: ...`. Failed captures are not counted. Private, local and cloud-metadata addresses are refused.

The tool's name, description, arguments and annotations are the same as the hosted server's; `npm run test:live` checks that against `https://calotype.dev/mcp`.

## What it sends

One `POST https://calotype.dev/v1/screenshot` per call, with your key, the arguments, and `User-Agent: calotype-mcp/<version>`. Nothing else, and nothing when there is no key. Privacy notice: [calotype.dev/privacy](https://calotype.dev/privacy).

## Develop

The source is in [`clients/npm`](https://github.com/skewbald-labs/calotype/tree/main/clients/npm) of the Calotype repository, next to the service itself.

```sh
npm install
npm test             # no network: the HTTP layer is faked
npm run test:live    # keyless calls to calotype.dev; creates no key, takes no screenshot
```

## License

MIT. See `LICENSE`.
