// The one test that uses the network. It runs only with CALOTYPE_LIVE=1, makes
// keyless calls to https://calotype.dev, creates no key and takes no screenshot.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { screenshot, SCREENSHOT_TOOL } from '../src/server.js';

const live = process.env.CALOTYPE_LIVE === '1';

async function remote(method, params) {
  const response = await fetch('https://calotype.dev/mcp', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json, text/event-stream',
      'MCP-Protocol-Version': '2025-06-18',
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
  });
  assert.equal(response.status, 200);
  return (await response.json()).result;
}

test('live: the tool equals the hosted server’s, and a placeholder key is refused', { skip: !live }, async () => {
  const { tools } = await remote('tools/list');
  assert.deepEqual(tools, [SCREENSHOT_TOOL]);

  const result = await screenshot({ url: 'https://example.com' }, { env: { CALOTYPE_API_KEY: 'cal_placeholder' } });
  assert.deepEqual(result, {
    content: [{ type: 'text', text: 'invalid_key: The API key is not valid.' }],
    isError: true,
  });
});
