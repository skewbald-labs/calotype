import assert from 'node:assert/strict';
import { test } from 'node:test';

import { imageSize } from '../src/image.js';
import { keyHelp, screenshot, USER_AGENT, VERSION } from '../src/server.js';
import { errorResponse, fakeFetch, imageResponse, jpegHeader, pngHeader, webpHeader } from '../support/helpers.js';

const env = { CALOTYPE_API_KEY: 'cal_test' };

test('sends the tool defaults, the key and a User-Agent to POST /v1/screenshot', async () => {
  const { fetch, calls } = fakeFetch(imageResponse(jpegHeader(1280, 800), { 'x-calotype-usage': '3/100' }));
  const result = await screenshot({ url: 'https://example.com' }, { env, fetch });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://calotype.dev/v1/screenshot');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer cal_test');
  assert.equal(calls[0].init.headers['User-Agent'], `calotype-mcp/${VERSION}`);
  assert.equal(USER_AGENT, `calotype-mcp/${VERSION}`);
  assert.deepEqual(calls[0].body, {
    url: 'https://example.com',
    full_page: false,
    width: 1280,
    height: 800,
    format: 'jpeg',
    dark_mode: false,
    block_ads: true,
  });

  assert.equal(result.isError, undefined);
  assert.equal(result.content[0].type, 'image');
  assert.equal(result.content[0].mimeType, 'image/jpeg');
  assert.deepEqual(Buffer.from(result.content[0].data, 'base64'), jpegHeader(1280, 800));
  assert.equal(
    result.content[1].text,
    'Screenshot of https://example.com (jpeg, 1280x800 px, 20 bytes). This key has used 3 of 100 screenshots this month.',
  );
});

test('passes the caller’s arguments through and drops ones the tool does not have', async () => {
  const { fetch, calls } = fakeFetch(imageResponse(pngHeader(390, 5000), { 'content-type': 'image/png' }));
  const result = await screenshot(
    { url: 'https://example.com/a', full_page: true, width: 390, format: 'png', dark_mode: true, block_ads: false, nope: 1 },
    { env, fetch },
  );
  assert.deepEqual(calls[0].body, {
    url: 'https://example.com/a',
    full_page: true,
    width: 390,
    height: 800,
    format: 'png',
    dark_mode: true,
    block_ads: false,
  });
  assert.equal(result.content[0].mimeType, 'image/png');
  assert.match(result.content[1].text, /^Screenshot of https:\/\/example\.com\/a \(png, 390x5000 px, 33 bytes\)\.$/);
});

test('scroll_y is sent only when set, and the summary says which screen came back', async () => {
  const { fetch, calls } = fakeFetch(
    imageResponse(jpegHeader(1280, 800), { 'x-calotype-page-height': '5427', 'x-calotype-scroll-y': '800' }),
    imageResponse(jpegHeader(1280, 800), { 'x-calotype-page-height': '5427', 'x-calotype-scroll-y': '0' }),
    imageResponse(jpegHeader(1280, 800), { 'x-calotype-page-height': '5427', 'x-calotype-scroll-y': '4627' }),
  );
  const middle = await screenshot({ url: 'https://example.com', scroll_y: 800 }, { env, fetch });
  assert.equal(calls[0].body.scroll_y, 800);
  assert.equal(
    middle.content[1].text,
    'Screenshot of https://example.com (jpeg, 1280x800 px, 20 bytes). This is screen 2 of about 7 ' +
      '(y = 800 to 1,600 of about 5,427 px). For the next screen, call again with scroll_y=1600.',
  );

  const first = await screenshot({ url: 'https://example.com', scroll_y: 0 }, { env, fetch });
  assert.equal('scroll_y' in calls[1].body, false);
  assert.equal(
    first.content[1].text,
    'Screenshot of https://example.com (jpeg, 1280x800 px, 20 bytes). This is screen 1 of about 7 ' +
      '(the top 800 px of a page about 5,427 px tall). To see the next screen at readable size, call again with ' +
      'scroll_y=800; full_page=true captures the whole page in one (smaller) image.',
  );

  const past = await screenshot({ url: 'https://example.com', scroll_y: 9000 }, { env, fetch });
  assert.match(past.content[1].text, /scroll_y=9000 is past the end of the page, so the last screen was captured instead\./);
  assert.match(past.content[1].text, /This is screen 7 of about 7 \(y = 4,627 to 5,427 of about 5,427 px\): the bottom of the page\.$/);
});

test('the screen note stays quiet for a page that fits and for a full page', async () => {
  const { screenNote } = await import('../src/server.js');
  assert.equal(screenNote({ fullPage: false, view: 800, asked: 0, pageHeight: 900, y: 0 }), '');
  assert.equal(screenNote({ fullPage: true, view: 800, asked: 0, pageHeight: 9000 }), '');
  assert.equal(screenNote({ fullPage: false, view: 800, asked: 0, pageHeight: undefined, y: undefined }), '');
  assert.equal(
    screenNote({ fullPage: false, view: 800, asked: 800, pageHeight: 600, y: 0 }),
    ' The page is about 600 px tall and fits in one screen, so scroll_y=800 was moved to 0: this is the whole page.',
  );
});

test('the summary names a failing page, a truncated download and sizes in KB', async () => {
  const image = Buffer.concat([webpHeader(800, 600), Buffer.alloc(4096)]);
  const { fetch } = fakeFetch(
    imageResponse(image, {
      'content-type': 'image/webp',
      'x-calotype-target-status': '404',
      'x-calotype-truncated': '1',
      'x-calotype-usage': '81/100',
    }),
  );
  const result = await screenshot({ url: 'https://example.com/missing', format: 'webp' }, { env, fetch });
  assert.equal(
    result.content[1].text,
    'Screenshot of https://example.com/missing (webp, 800x600 px, 4 KB). The page answered HTTP 404. ' +
      'The page passed the 50 MB download limit; the image shows what had loaded by then. ' +
      'This key has used 81 of 100 screenshots this month.',
  );
});

test('with no key it says how to get one and sends nothing', async () => {
  const { fetch, calls } = fakeFetch();
  for (const noKey of [{}, { CALOTYPE_API_KEY: '  ' }]) {
    const result = await screenshot({ url: 'https://example.com' }, { env: noKey, fetch });
    assert.equal(result.isError, true);
    assert.equal(result.content[0].text, keyHelp('https://calotype.dev'));
  }
  assert.equal(calls.length, 0);
  assert.match(keyHelp('https://calotype.dev'), /CALOTYPE_API_KEY/);
  assert.match(keyHelp('https://calotype.dev'), /curl -X POST https:\/\/calotype\.dev\/v1\/keys .*"ref":"npm"/);
});

test('CALOTYPE_API_URL replaces the base address', async () => {
  const { fetch, calls } = fakeFetch(imageResponse(jpegHeader(10, 10)));
  await screenshot(
    { url: 'https://example.com' },
    { env: { ...env, CALOTYPE_API_URL: 'http://127.0.0.1:8080/' }, fetch },
  );
  assert.equal(calls[0].url, 'http://127.0.0.1:8080/v1/screenshot');
});

test('an API error becomes a tool error with the code, the message and the details', async () => {
  const { fetch } = fakeFetch(
    errorResponse(401, { code: 'invalid_key', message: 'The API key is not valid.' }),
    errorResponse(400, {
      code: 'invalid_parameters',
      message: 'Some parameters are not valid.',
      details: [
        { field: 'width', message: 'Input should be less than or equal to 3840' },
        { field: 'format', message: "Input should be 'png', 'jpeg' or 'webp'" },
      ],
    }),
    new Response('<html>bad gateway</html>', { status: 502 }),
  );
  const args = { url: 'https://example.com' };
  let result = await screenshot(args, { env, fetch });
  assert.deepEqual(result, {
    content: [{ type: 'text', text: 'invalid_key: The API key is not valid.' }],
    isError: true,
  });
  result = await screenshot(args, { env, fetch });
  assert.equal(
    result.content[0].text,
    'invalid_parameters: Some parameters are not valid. ' +
      "width: Input should be less than or equal to 3840; format: Input should be 'png', 'jpeg' or 'webp'",
  );
  result = await screenshot(args, { env, fetch });
  assert.deepEqual(result, { content: [{ type: 'text', text: 'HTTP 502' }], isError: true });
});

test('a network failure becomes a tool error that names the cause, never the key', async () => {
  const refused = new TypeError('fetch failed', { cause: Object.assign(new Error('connect'), { code: 'ECONNREFUSED' }) });
  const timeout = new DOMException('The operation was aborted due to timeout', 'TimeoutError');
  const { fetch } = fakeFetch(refused, timeout);
  let result = await screenshot({ url: 'https://example.com' }, { env, fetch });
  assert.deepEqual(result, {
    content: [{ type: 'text', text: 'Could not reach the Calotype API (ECONNREFUSED).' }],
    isError: true,
  });
  result = await screenshot({ url: 'https://example.com' }, { env, fetch });
  assert.equal(result.content[0].text, 'Could not reach the Calotype API (TimeoutError).');
});

test('imageSize reads PNG, JPEG and WebP headers and gives up quietly on anything else', () => {
  assert.deepEqual(imageSize(pngHeader(1280, 800)), [1280, 800]);
  assert.deepEqual(imageSize(jpegHeader(1280, 20000)), [1280, 20000]);
  assert.deepEqual(imageSize(webpHeader(3840, 16383)), [3840, 16383]);
  assert.equal(imageSize(Buffer.from('not an image')), null);
  assert.equal(imageSize(Buffer.alloc(0)), null);
  assert.equal(imageSize(Buffer.from([0xff, 0xd8, 0xff])), null);
});
