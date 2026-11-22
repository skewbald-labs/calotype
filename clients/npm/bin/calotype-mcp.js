#!/usr/bin/env node
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';

import { createServer } from '../src/server.js';

// stdout carries the protocol, so anything for a person goes to stderr.
try {
  await createServer().connect(new StdioServerTransport());
} catch (error) {
  console.error(`calotype-mcp could not start: ${error?.message ?? error}`);
  process.exit(1);
}
