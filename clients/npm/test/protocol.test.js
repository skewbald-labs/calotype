// The server over MCP: in memory through the SDK's client, then for real over stdio.

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';

import { createServer, INSTRUCTIONS, keyHelp, SCREENSHOT_TOOL, VERSION } from '../src/server.js';
import { errorResponse, fakeFetch, imageResponse, jpegHeader } from '../support/helpers.js';

const BIN = fileURLToPath(new URL('../bin/calotype-mcp.js', import.meta.url));

async function connect(options) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  const server = createServer(options);
  const client = new Client({ name: 'calotype-mcp-tests', version: '0' });
  await Promise.all([server.connect(serverSide), client.connect(clientSide)]);
  return client;
}

test('an MCP client sees the server, its instructions and exactly one tool', async () => {
  const client = await connect({ env: {}, fetch: fakeFetch().fetch });
  assert.deepEqual(client.getServerVersion(), {
    name: 'calotype',
    title: 'Calotype',
    version: VERSION,
    websiteUrl: 'https://calotype.dev',
  });
  assert.equal(client.getInstructions(), INSTRUCTIONS);
  const { tools } = await client.listTools();
  assert.deepEqual(tools, [SCREENSHOT_TOOL]);
  await client.close();
});

test('an MCP client can call the tool: image, summary, API error, no key, unknown tool', async () => {
  const { fetch, calls } = fakeFetch(
    imageResponse(jpegHeader(1280, 800), { 'x-calotype-usage': '1/100' }),
    errorResponse(400, { code: 'blocked_target', message: 'That address is not public.' }),
  );
  const client = await connect({ env: { CALOTYPE_API_KEY: 'cal_test' }, fetch });

  let result = await client.callTool({ name: 'screenshot', arguments: { url: 'https://example.com', dark_mode: true } });
  assert.equal(result.isError, undefined);
  assert.deepEqual(result.content.map((c) => c.type), ['image', 'text']);
  assert.equal(calls[0].body.dark_mode, true);

  result = await client.callTool({ name: 'screenshot', arguments: { url: 'http://10.0.0.1' } });
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, 'blocked_target: That address is not public.');

  result = await client.callTool({ name: 'nope', arguments: {} });
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, 'Unknown tool: nope');
  await client.close();

  const keyless = await connect({ env: {}, fetch });
  result = await keyless.callTool({ name: 'screenshot', arguments: { url: 'https://example.com' } });
  assert.equal(result.isError, true);
  assert.equal(result.content[0].text, keyHelp('https://calotype.dev'));
  assert.equal(calls.length, 2);
  await keyless.close();
});

test('the installed command speaks MCP over stdio: initialize, tools/list, a keyless tools/call', async () => {
  // No key and an address nothing listens on, so this test cannot reach the network.
  const env = { PATH: process.env.PATH, CALOTYPE_API_URL: 'http://127.0.0.1:9' };
  const child = spawn(process.execPath, [BIN], { env, stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
  const send = (message) => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
  const read = async () => JSON.parse((await lines.next()).value);

  try {
    send({
      id: 1,
      method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'stdio-test', version: '0' } },
    });
    const init = await read();
    assert.equal(init.id, 1);
    assert.equal(init.result.protocolVersion, '2025-06-18');
    assert.deepEqual(init.result.serverInfo, {
      name: 'calotype',
      title: 'Calotype',
      version: VERSION,
      websiteUrl: 'https://calotype.dev',
    });
    assert.equal(init.result.instructions, INSTRUCTIONS);
    assert.ok(init.result.capabilities.tools);

    send({ method: 'notifications/initialized' });
    send({ id: 2, method: 'tools/list' });
    const list = await read();
    assert.equal(list.id, 2);
    assert.deepEqual(list.result.tools, [SCREENSHOT_TOOL]);

    send({ id: 3, method: 'tools/call', params: { name: 'screenshot', arguments: { url: 'https://example.com' } } });
    const call = await read();
    assert.equal(call.id, 3);
    assert.equal(call.result.isError, true);
    assert.equal(call.result.content[0].text, keyHelp('http://127.0.0.1:9'));
  } finally {
    child.stdin.end();
    await once(child, 'exit');
  }
  assert.equal(child.exitCode, 0);
});
