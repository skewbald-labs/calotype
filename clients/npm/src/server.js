// A local stdio MCP server for Calotype. It lists the same `screenshot` tool as
// the hosted server at https://calotype.dev/mcp and forwards each call to the
// hosted HTTP API, for clients that cannot send a header to a remote server.
//
// Environment:
//   CALOTYPE_API_KEY   the key sent to the API (free: one POST to /v1/keys)
//   CALOTYPE_API_URL   base URL of the API (default https://calotype.dev)

import { createRequire } from 'node:module';

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

import { imageSize } from './image.js';
import { INSTRUCTIONS, SCREENSHOT_TOOL } from './tool.js';

export { INSTRUCTIONS, SCREENSHOT_TOOL };

export const VERSION = createRequire(import.meta.url)('../package.json').version;
export const DEFAULT_API_URL = 'https://calotype.dev';
export const USER_AGENT = `calotype-mcp/${VERSION}`;

const TIMEOUT_MS = 90_000;
const CONTENT_TYPES = { png: 'image/png', jpeg: 'image/jpeg', webp: 'image/webp' };
const ARGUMENTS = Object.entries(SCREENSHOT_TOOL.inputSchema.properties);

/** What to tell a caller whose environment has no key. */
export function keyHelp(origin) {
  return (
    'Calotype needs an API key in the environment variable CALOTYPE_API_KEY. ' +
    `Get a free key (no sign-up) at ${origin}/#key, or with: ` +
    `curl -X POST ${origin}/v1/keys -H 'Content-Type: application/json' -d '{"ref":"npm"}'`
  );
}

function toolError(text) {
  return { content: [{ type: 'text', text }], isError: true };
}

/** An API error as the model will read it: the stable code, then the message. */
function errorText(status, body) {
  let error;
  try {
    error = JSON.parse(body).error;
  } catch {
    error = null;
  }
  if (!error || typeof error !== 'object') return `HTTP ${status}`;
  let text = `${error.code ?? 'error'}: ${error.message ?? `HTTP ${status}`}`;
  if (Array.isArray(error.details) && error.details.length) {
    text += ' ' + error.details.map((d) => `${d.field}: ${d.message}`).join('; ');
  }
  return text;
}

function summary(url, format, image, headers) {
  const n = image.length;
  let size = n < 1024 ? `${n} bytes` : `${Math.round(n / 1024)} KB`;
  const dims = imageSize(image);
  if (dims) size = `${dims[0]}x${dims[1]} px, ${size}`;
  let text = `Screenshot of ${url} (${format}, ${size}).`;
  const status = Number(headers.get('x-calotype-target-status'));
  if (status >= 400) text += ` The page answered HTTP ${status}.`;
  if (headers.get('x-calotype-truncated')) {
    text += ' The page passed the 50 MB download limit; the image shows what had loaded by then.';
  }
  const usage = /^(\d+)\/(\d+)$/.exec(headers.get('x-calotype-usage') ?? '');
  if (usage) text += ` This key has used ${usage[1]} of ${usage[2]} screenshots this month.`;
  return text;
}

/**
 * Run one `screenshot` call against the HTTP API and return an MCP tool result.
 * `env` and `fetch` are parameters so tests can replace them.
 */
export async function screenshot(args, { env = process.env, fetch = globalThis.fetch } = {}) {
  const origin = (env.CALOTYPE_API_URL || DEFAULT_API_URL).trim().replace(/\/+$/, '');
  const key = (env.CALOTYPE_API_KEY || '').trim();
  if (!key) return toolError(keyHelp(origin));

  // The tool's defaults differ from the HTTP API's (jpeg and block_ads here), so
  // every argument is sent. The API checks the values and names any that are wrong.
  const body = {};
  for (const [name, schema] of ARGUMENTS) {
    const value = args?.[name] ?? schema.default;
    if (value !== undefined) body[name] = value;
  }

  let response;
  let image;
  try {
    response = await fetch(`${origin}/v1/screenshot`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (response.status !== 200) {
      return toolError(errorText(response.status, await response.text()));
    }
    image = Buffer.from(await response.arrayBuffer());
  } catch (error) {
    return toolError(`Could not reach the Calotype API (${error?.cause?.code ?? error?.name ?? 'Error'}).`);
  }

  const mimeType = (response.headers.get('content-type') ?? '').split(';')[0].trim();
  const format = Object.keys(CONTENT_TYPES).find((f) => CONTENT_TYPES[f] === mimeType) ?? String(body.format);
  return {
    content: [
      { type: 'image', data: image.toString('base64'), mimeType: CONTENT_TYPES[format] ?? mimeType },
      { type: 'text', text: summary(body.url, format, image, response.headers) },
    ],
  };
}

/** The MCP server: `initialize`, `tools/list` and `tools/call` for the one tool. */
export function createServer(options = {}) {
  const server = new Server(
    { name: 'calotype', title: 'Calotype', version: VERSION, websiteUrl: DEFAULT_API_URL },
    { capabilities: { tools: {} }, instructions: INSTRUCTIONS },
  );
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: [SCREENSHOT_TOOL] }));
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    if (request.params.name !== SCREENSHOT_TOOL.name) {
      return toolError(`Unknown tool: ${request.params.name}`);
    }
    return screenshot(request.params.arguments ?? {}, options);
  });
  return server;
}
