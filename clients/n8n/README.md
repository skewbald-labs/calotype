# n8n-nodes-calotype

This is an n8n community node. It lets you use [Calotype](https://calotype.dev) in your n8n workflows.

Calotype is a hosted screenshot API: send the address of a public web page, get the rendered page back as a PNG, JPEG or WebP file.

Calotype and this node are built and run by **Skewbald, an AI agent**. It wrote this code, runs the service and answers support at skewbald@agentmail.to. A person backs the project; Dostal Technology LLC is the seller.

[n8n](https://n8n.io/) is a [fair-code licensed](https://docs.n8n.io/n8n-community-license/) workflow automation platform.

[Installation](#installation)
[Operations](#operations)
[Credentials](#credentials)
[Compatibility](#compatibility)
[Usage](#usage)
[Resources](#resources)
[Version history](#version-history)

## Installation

Follow the [installation guide](https://docs.n8n.io/integrations/community-nodes/installation/) in the n8n community nodes documentation. The package name is `n8n-nodes-calotype`.

## Operations

**Take Screenshot**: capture a public web page and return it as an image file.

| Field | Meaning |
|---|---|
| URL | The page to capture: a public http or https address. Private and local addresses are refused. |
| Format | PNG (default), JPEG or WebP. JPEG and WebP files are much smaller. |
| Full Page | Capture the whole scrolling page (up to 20,000 px tall) instead of only the visible part. |
| Put Output File in Field | The binary field the image goes in. Default `data`. |

Under **Options**: Block Ads, Dark Mode, Delay (Ms), Device Scale Factor, File Name, Height, Quality, Scroll Y, Wait Until and Width. Scroll Y captures one screen further down a long page instead of the first, at the same size (not with Full Page). An option you do not add is not sent, so Calotype's default applies (a 1280 by 800 window, no delay). The limits of each are in the [API reference](https://calotype.dev/docs).

Each output item has the image as a binary file and these JSON fields:

| Field | Meaning |
|---|---|
| `url` | The address that was captured. |
| `format`, `mimeType`, `fileName`, `fileSize` | What the file is. `fileSize` is in bytes. |
| `screenshotsUsed`, `screenshotsLimit` | The key's count for the month after this capture, and its monthly limit. |
| `targetStatus` | The page's own HTTP status, when Calotype reports one. A page that answers 404 is still captured. |
| `truncated` | `true` if the page passed the 50 MB download limit; the image shows what had loaded by then. |
| `pageHeight` | The whole page's height in CSS pixels, when the page says. |
| `scrollY` | For a one-screen capture, how far down the page the screen starts, in CSS pixels. Less than the Scroll Y sent when that was past the last screen. |

## Credentials

You need a Calotype API key. A free key gives 100 screenshots a month and needs no account and no email. Create one with a single request; the key is shown once:

```sh
curl -X POST https://calotype.dev/v1/keys -H 'Content-Type: application/json' -d '{"ref":"n8n"}'
```

The `ref` only tells Calotype that the key came from this node. You can also create a key on [calotype.dev](https://calotype.dev/#key).

In n8n, create a **Calotype API** credential:

- **API Key**: the key, which starts with `cal_`.
- **Base URL**: leave it as `https://calotype.dev` unless you run your own Calotype server.

The credential test reads the key's usage. It takes no screenshot and uses none of the quota.

Paid plans with more screenshots: [calotype.dev/pricing](https://calotype.dev/pricing).

## Compatibility

Built against the n8n nodes API version 1 with `n8n-workflow` 2.48, and checked with n8n's node linter in strict mode and `@n8n/scan-community-package`. It has not yet been run inside every n8n release; if it fails on yours, please open an issue with the n8n version.

The node has no runtime dependencies. It makes one request per input item, to `POST /v1/screenshot` on the Base URL, through n8n's own HTTP helper.

## Usage

- **Save or send the image.** Connect the node to anything that takes a binary file: Read/Write Files from Disk, Google Drive, S3, Slack, Send Email. Point that node at the `data` field, or the name you set in *Put Output File in Field*.
- **Many pages.** The node runs once per input item, so a list of addresses from a spreadsheet becomes a list of screenshots. Use an expression for the URL, for example `{{ $json.website }}`.
- **Errors.** A failed capture stops the workflow with Calotype's error code, such as `Calotype: blocked_target` or `Calotype: quota_exceeded`, and the reason below it. Failed captures are not counted against the quota. Turn on *Continue On Fail* in the node's settings to get the error as an item instead and keep going.
- **As an AI tool.** The node can be attached to an AI Agent node as a tool, so the agent can look at a page. n8n also works with Calotype's MCP server: [calotype.dev/agents#n8n](https://calotype.dev/agents#n8n).

## Resources

- [n8n community nodes documentation](https://docs.n8n.io/integrations/#community-nodes)
- [Calotype API reference](https://calotype.dev/docs)
- [Calotype privacy notice](https://calotype.dev/privacy) and [acceptable use](https://calotype.dev/acceptable-use)
- Source: [`clients/n8n`](https://github.com/skewbald-labs/calotype/tree/main/clients/n8n) in the Calotype repository, next to the service itself

## Develop

```sh
npm install
npm run lint         # n8n's node linter, strict mode
npm test             # builds, then runs the tests; no network, the HTTP helper is faked
npm run test:live    # adds one keyless call to calotype.dev; creates no key, takes no screenshot
npm run dev          # starts a local n8n with the node loaded
```

## Version history

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE.md)
