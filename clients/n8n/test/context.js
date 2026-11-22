'use strict';
// A stand-in for the `this` n8n gives a node's execute(): parameters per item,
// a credential, and an HTTP helper that answers from a function instead of the network.

function fakeContext({ params = [{}], credentials, respond, continueOnFail = false } = {}) {
	const calls = [];
	const context = {
		calls,
		getInputData: () => params.map(() => ({ json: {} })),
		getNode: () => ({
			id: '1',
			name: 'Calotype',
			type: 'n8n-nodes-calotype.calotype',
			typeVersion: 1,
			position: [0, 0],
			parameters: {},
		}),
		getCredentials: async (name) => {
			if (name !== 'calotypeApi') throw new Error(`unexpected credential ${name}`);
			return credentials ?? { apiKey: 'cal_test', baseUrl: 'https://calotype.dev' };
		},
		getNodeParameter: (name, itemIndex, fallback) => {
			const value = params[itemIndex][name];
			return value === undefined ? fallback : value;
		},
		continueOnFail: () => continueOnFail,
		helpers: {
			async httpRequestWithAuthentication(credentialType, options) {
				calls.push({ credentialType, options, self: this });
				return respond(options, calls.length - 1);
			},
			prepareBinaryData: async (buffer, fileName, mimeType) => ({
				data: Buffer.from(buffer).toString('base64'),
				fileName,
				mimeType,
			}),
		},
	};
	return context;
}

function image(bytes, headers = {}) {
	return {
		statusCode: 200,
		headers: { 'content-type': 'image/png', ...headers },
		body: Buffer.from(bytes),
	};
}

function apiError(statusCode, error) {
	return {
		statusCode,
		headers: { 'content-type': 'application/json' },
		body: Buffer.from(JSON.stringify({ error })),
	};
}

module.exports = { fakeContext, image, apiError };
