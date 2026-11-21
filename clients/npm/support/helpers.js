// A fake `fetch` that records each request and answers from a queue.

export function fakeFetch(...answers) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init, body: init?.body ? JSON.parse(init.body) : undefined });
    const answer = answers.shift();
    if (answer instanceof Error) throw answer;
    if (!answer) throw new Error('fakeFetch: no answer left');
    return answer;
  };
  return { fetch, calls };
}

export function imageResponse(bytes, headers = {}) {
  return new Response(bytes, { status: 200, headers: { 'content-type': 'image/jpeg', ...headers } });
}

export function errorResponse(status, error) {
  return new Response(JSON.stringify({ error }), { status, headers: { 'content-type': 'application/json' } });
}

/** The first bytes of a PNG that say it is `width` by `height`. */
export function pngHeader(width, height) {
  const b = Buffer.alloc(33);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(b);
  b.writeUInt32BE(13, 8);
  b.write('IHDR', 12, 'latin1');
  b.writeUInt32BE(width, 16);
  b.writeUInt32BE(height, 20);
  return b;
}

/** A minimal baseline JPEG header: SOI, an APP0 segment, then SOF0 with the size. */
export function jpegHeader(width, height) {
  const app0 = Buffer.from([0xff, 0xe0, 0x00, 0x04, 0x00, 0x00]);
  const sof = Buffer.alloc(12);
  sof[0] = 0xff;
  sof[1] = 0xc0;
  sof.writeUInt16BE(10, 2);
  sof[4] = 8;
  sof.writeUInt16BE(height, 5);
  sof.writeUInt16BE(width, 7);
  return Buffer.concat([Buffer.from([0xff, 0xd8]), app0, sof]);
}

/** A WebP header in the extended (VP8X) layout. */
export function webpHeader(width, height) {
  const b = Buffer.alloc(30);
  b.write('RIFF', 0, 'latin1');
  b.write('WEBP', 8, 'latin1');
  b.write('VP8X', 12, 'latin1');
  b.writeUIntLE(width - 1, 24, 3);
  b.writeUIntLE(height - 1, 27, 3);
  return b;
}
