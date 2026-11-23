'use strict';
// Runs against the built node in dist/ (npm test builds first). No network:
// n8n's HTTP helper is replaced by a fake. The one live test is at the end.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const { NodeApiError, NodeOperationError } = require('n8n-workflow');

const { Calotype } = require('../dist/nodes/Calotype/Calotype.node.js');
const { CalotypeApi } = require('../dist/credentials/CalotypeApi.credentials.js');
const pkg = require('../package.json');
const { apiError, fakeContext, image } = require('./context.js');

const node = new Calotype();
const run = (context) => node.execute.call(context);

test('takes a screenshot: one POST with the credential, a binary file and a summary out', async () => {
	const context = fakeContext({
		params: [{ url: ' https://example.com ' }],
		respond: () => image('PNGDATA', { 'X-Calotype-Usage': '3/100' }),
	});
	const [items] = await run(context);

	assert.equal(context.calls.length, 1);
	const { credentialType, options, self } = context.calls[0];
	assert.equal(credentialType, 'calotypeApi');
	assert.equal(self, context);
	assert.deepEqual(options, {
		method: 'POST',
		url: 'https://calotype.dev/v1/screenshot',
		body: { url: 'https://example.com', format: 'png', full_page: false },
		json: true,
		encoding: 'arraybuffer',
		returnFullResponse: true,
		ignoreHttpStatusErrors: true,
	});

	assert.deepEqual(items, [
		{
			json: {
				url: 'https://example.com',
				format: 'png',
				mimeType: 'image/png',
				fileName: 'screenshot.png',
				fileSize: 7,
				truncated: false,
				screenshotsUsed: 3,
				screenshotsLimit: 100,
			},
			binary: {
				data: {
					data: Buffer.from('PNGDATA').toString('base64'),
					fileName: 'screenshot.png',
					mimeType: 'image/png',
				},
			},
			pairedItem: { item: 0 },
		},
	]);
});

test('sends the options the user set, under the API’s names, and nothing else', async () => {
	const context = fakeContext({
		credentials: { apiKey: 'cal_test', baseUrl: 'http://127.0.0.1:8080/' },
		params: [
			{
				url: 'https://example.com/pricing',
				format: 'jpeg',
				fullPage: true,
				binaryPropertyName: 'shot',
				options: {
					width: 390,
					height: 844,
					quality: 70,
					deviceScaleFactor: 2,
					delayMs: 500,
					waitUntil: 'networkidle',
					blockAds: true,
					darkMode: false,
					scrollY: 1600,
					fileName: 'pricing.jpg',
				},
			},
		],
		respond: () =>
			image('JPEG', {
				'content-type': 'image/jpeg',
				'x-calotype-target-status': '404',
				'x-calotype-truncated': '1',
				'x-calotype-page-height': '5427',
				'x-calotype-scroll-y': '1600',
			}),
	});
	const [[item]] = await run(context);

	assert.equal(context.calls[0].options.url, 'http://127.0.0.1:8080/v1/screenshot');
	assert.deepEqual(context.calls[0].options.body, {
		url: 'https://example.com/pricing',
		format: 'jpeg',
		full_page: true,
		width: 390,
		height: 844,
		quality: 70,
		device_scale_factor: 2,
		delay_ms: 500,
		wait_until: 'networkidle',
		block_ads: true,
		dark_mode: false,
		scroll_y: 1600,
	});
	assert.deepEqual(Object.keys(item.binary), ['shot']);
	assert.equal(item.binary.shot.fileName, 'pricing.jpg');
	assert.equal(item.binary.shot.mimeType, 'image/jpeg');
	assert.deepEqual(item.json, {
		url: 'https://example.com/pricing',
		format: 'jpeg',
		mimeType: 'image/jpeg',
		fileName: 'pricing.jpg',
		fileSize: 4,
		truncated: true,
		targetStatus: 404,
		pageHeight: 5427,
		scrollY: 1600,
	});
});

test('runs once per input item and pairs each output with its input', async () => {
	const context = fakeContext({
		params: [{ url: 'https://example.com/a' }, { url: 'https://example.com/b', format: 'webp' }],
		respond: (options) =>
			image(options.body.url, { 'content-type': `image/${options.body.format}` }),
	});
	const [items] = await run(context);
	assert.deepEqual(
		items.map((item) => [item.json.url, item.json.fileName, item.pairedItem.item]),
		[
			['https://example.com/a', 'screenshot.png', 0],
			['https://example.com/b', 'screenshot.webp', 1],
		],
	);
});

test('an API error becomes a NodeApiError with the code, the message and the status', async () => {
	const context = fakeContext({
		params: [{ url: 'http://10.0.0.1' }],
		respond: () =>
			apiError(400, {
				code: 'invalid_parameters',
				message: 'Some parameters are not valid.',
				details: [{ field: 'width', message: 'Input should be less than or equal to 3840' }],
			}),
	});
	await assert.rejects(run(context), (error) => {
		assert.ok(error instanceof NodeApiError);
		assert.equal(error.message, 'Calotype: invalid_parameters');
		assert.equal(
			error.description,
			'Some parameters are not valid. width: Input should be less than or equal to 3840',
		);
		assert.equal(error.httpCode, '400');
		assert.equal(error.context.itemIndex, 0);
		return true;
	});

	const gateway = fakeContext({
		params: [{ url: 'https://example.com' }],
		respond: () => ({
			statusCode: 502,
			headers: {},
			body: Buffer.from('<html>bad gateway</html>'),
		}),
	});
	await assert.rejects(run(gateway), (error) => {
		assert.ok(error instanceof NodeApiError);
		assert.equal(error.message, 'The Calotype API returned an error');
		assert.equal(error.description, 'The Calotype API answered HTTP 502');
		return true;
	});
});

test('a request with no answer becomes a NodeApiError that says so', async () => {
	const failing = (error) =>
		fakeContext({
			params: [{ url: 'https://example.com' }],
			respond: () => {
				throw error;
			},
		});

	await assert.rejects(run(failing(new Error('socket hang up'))), (error) => {
		assert.ok(error instanceof NodeApiError);
		assert.equal(error.message, 'Could not reach the Calotype API');
		assert.equal(error.context.itemIndex, 0);
		return true;
	});

	// For the error codes n8n knows, it puts its own wording first.
	const refused = Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:9'), {
		code: 'ECONNREFUSED',
	});
	await assert.rejects(run(failing(refused)), (error) => {
		assert.ok(error instanceof NodeApiError);
		assert.match(error.message, /refused the connection/);
		return true;
	});
});

test('an empty URL is a NodeOperationError and sends nothing', async () => {
	const context = fakeContext({ params: [{ url: '   ' }], respond: () => image('x') });
	await assert.rejects(run(context), (error) => {
		assert.ok(error instanceof NodeOperationError);
		assert.equal(error.message, 'The URL is empty');
		return true;
	});
	assert.equal(context.calls.length, 0);
});

test('with Continue On Fail, a failed item carries its error and the rest still run', async () => {
	const context = fakeContext({
		continueOnFail: true,
		params: [{ url: 'https://example.com/a' }, { url: 'https://example.com/b' }],
		respond: (_options, call) =>
			call === 0
				? apiError(429, {
						code: 'quota_exceeded',
						message: 'This key has used its 100 screenshots.',
					})
				: image('ok'),
	});
	const [items] = await run(context);
	assert.equal(items.length, 2);
	assert.deepEqual(items[0], {
		json: { error: 'Calotype: quota_exceeded' },
		pairedItem: { item: 0 },
	});
	assert.equal(items[1].json.url, 'https://example.com/b');
	assert.ok(items[1].binary.data);
});

test('the node and the credential describe themselves as n8n expects', () => {
	const { description } = node;
	assert.equal(description.displayName, 'Calotype');
	assert.equal(description.name, 'calotype');
	assert.equal(description.usableAsTool, true);
	assert.deepEqual(description.credentials, [{ name: 'calotypeApi', required: true }]);

	const operation = description.properties.find((p) => p.name === 'operation');
	assert.deepEqual(
		operation.options.map((o) => [o.name, o.value, o.action]),
		[['Take Screenshot', 'takeScreenshot', 'Take screenshot']],
	);
	const url = description.properties.find((p) => p.name === 'url');
	assert.equal(url.required, true);

	const credential = new CalotypeApi();
	assert.equal(credential.name, 'calotypeApi');
	const apiKey = credential.properties.find((p) => p.name === 'apiKey');
	assert.deepEqual(apiKey.typeOptions, { password: true });
	assert.equal(
		credential.authenticate.properties.headers.Authorization,
		'=Bearer {{$credentials.apiKey}}',
	);
	assert.equal(credential.test.request.url, '/v1/usage');
});

test('the package meets the community-node rules that can be checked from here', () => {
	assert.match(pkg.name, /^n8n-nodes-/);
	assert.ok(pkg.keywords.includes('n8n-community-node-package'));
	assert.equal(pkg.license, 'MIT');
	assert.equal(
		pkg.dependencies,
		undefined,
		'a verified community node may have no runtime dependencies',
	);
	assert.deepEqual(pkg.files, ['dist', '!dist/tsconfig.tsbuildinfo']);
	for (const file of [...pkg.n8n.nodes, ...pkg.n8n.credentials]) {
		assert.ok(fs.existsSync(path.join(__dirname, '..', file)), `${file} is missing from the build`);
	}
	for (const icon of ['calotype.svg', 'calotype.dark.svg', 'Calotype.node.json']) {
		assert.ok(fs.existsSync(path.join(__dirname, '..', 'dist', 'nodes', 'Calotype', icon)), icon);
	}
	const built = fs.readFileSync(path.join(__dirname, '..', pkg.n8n.nodes[0]), 'utf8');
	assert.doesNotMatch(
		built,
		/process\.env|require\(['"](node:)?fs['"]\)/,
		'no environment or file access',
	);
});

// The one test that uses the network: a keyless call that creates no key and takes no screenshot.
test(
	'live: calotype.dev refuses a placeholder key with invalid_key',
	{ skip: process.env.CALOTYPE_LIVE !== '1' },
	async () => {
		const context = fakeContext({
			credentials: { apiKey: 'cal_placeholder', baseUrl: 'https://calotype.dev' },
			params: [{ url: 'https://example.com' }],
			respond: async (options) => {
				const response = await fetch(options.url, {
					method: options.method,
					headers: { Authorization: 'Bearer cal_placeholder', 'Content-Type': 'application/json' },
					body: JSON.stringify(options.body),
				});
				return {
					statusCode: response.status,
					headers: Object.fromEntries(response.headers),
					body: Buffer.from(await response.arrayBuffer()),
				};
			},
		});
		await assert.rejects(run(context), (error) => {
			assert.ok(error instanceof NodeApiError);
			assert.equal(error.message, 'Calotype: invalid_key');
			assert.equal(error.description, 'The API key is not valid.');
			assert.equal(error.httpCode, '401');
			return true;
		});
	},
);
