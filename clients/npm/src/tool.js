// The `screenshot` tool as the hosted server at https://calotype.dev/mcp lists it.
// Keep this file equal to that listing: `npm run test:live` compares the two.
// The source of truth is calotype/remote_mcp.py in the same repository.

export const INSTRUCTIONS =
  "Calotype lets you see any public web page. Call `screenshot` with a page's http(s) address; " +
  'it returns the rendered page as an image. Private, local and cloud-metadata addresses are refused.';

export const SCREENSHOT_TOOL = {
  name: 'screenshot',
  title: 'Screenshot a web page',
  description:
    'Take a screenshot of a public web page and return it as an image. ' +
    'By default the image is the first screen only (the viewport, 1280x800); the result says how tall the ' +
    'whole page is, and full_page=true captures all of it. ' +
    'Use it to see what a page looks like: its layout, text, charts or errors as rendered in Chromium. ' +
    'Only public http(s) pages; private and local addresses are refused. ' +
    "Each successful call counts as one screenshot against the key's monthly quota; failed ones do not. " +
    'Full-page captures of long pages can be large; jpeg or webp keeps them small.',
  inputSchema: {
    type: 'object',
    title: 'screenshotArguments',
    properties: {
      url: {
        type: 'string',
        title: 'Url',
        description: 'The page to capture: a public http or https address.',
        minLength: 1,
        maxLength: 2048,
      },
      full_page: {
        type: 'boolean',
        title: 'Full Page',
        description: 'Capture the whole scrolling page (up to 20,000 px tall) instead of the viewport.',
        default: false,
      },
      width: {
        type: 'integer',
        title: 'Width',
        description: 'Viewport width in CSS pixels.',
        minimum: 100,
        maximum: 3840,
        default: 1280,
      },
      height: {
        type: 'integer',
        title: 'Height',
        description: 'Viewport height in CSS pixels.',
        minimum: 100,
        maximum: 2160,
        default: 800,
      },
      format: {
        type: 'string',
        title: 'Format',
        description: 'Image format. jpeg and webp are much smaller than png.',
        enum: ['png', 'jpeg', 'webp'],
        default: 'jpeg',
      },
      dark_mode: {
        type: 'boolean',
        title: 'Dark Mode',
        description: 'Ask the page for its dark colour scheme.',
        default: false,
      },
      block_ads: {
        type: 'boolean',
        title: 'Block Ads',
        description: 'Block requests to common ad and tracking domains.',
        default: true,
      },
    },
    required: ['url'],
  },
  annotations: {
    title: 'Screenshot a web page',
    readOnlyHint: true,
    destructiveHint: false,
    openWorldHint: true,
  },
};
