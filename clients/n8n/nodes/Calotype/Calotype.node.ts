import type {
	IDataObject,
	IExecuteFunctions,
	IHttpRequestOptions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
	JsonObject,
} from 'n8n-workflow';
import { NodeApiError, NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

const DEFAULT_BASE_URL = 'https://calotype.dev';
const EXTENSIONS: Record<string, string> = {
	'image/png': 'png',
	'image/jpeg': 'jpg',
	'image/webp': 'webp',
};

interface CalotypeError {
	code?: string;
	message?: string;
	details?: Array<{ field?: string; message?: string }>;
}

// Header names arrive in any case; values may be repeated.
function header(headers: IDataObject | undefined, name: string): string | undefined {
	for (const [key, value] of Object.entries(headers ?? {})) {
		if (key.toLowerCase() === name) return Array.isArray(value) ? String(value[0]) : String(value);
	}
	return undefined;
}

function toBuffer(body: unknown): Buffer {
	if (Buffer.isBuffer(body)) return body;
	if (body instanceof ArrayBuffer) return Buffer.from(body);
	if (typeof body === 'string') return Buffer.from(body);
	return Buffer.from(JSON.stringify(body ?? ''));
}

// The API answers an error as {"error": {"code", "message", ...}}.
function parseError(body: Buffer): CalotypeError {
	try {
		const error = (JSON.parse(body.toString('utf8')) as { error?: CalotypeError }).error;
		return error && typeof error === 'object' ? error : {};
	} catch {
		return {};
	}
}

// Option names in the node, and the API parameters they set.
const OPTIONAL_PARAMETERS: Array<[string, string]> = [
	['width', 'width'],
	['height', 'height'],
	['quality', 'quality'],
	['deviceScaleFactor', 'device_scale_factor'],
	['delayMs', 'delay_ms'],
	['waitUntil', 'wait_until'],
	['blockAds', 'block_ads'],
	['darkMode', 'dark_mode'],
];

// One item: one request to POST /v1/screenshot. Every failure is thrown as a
// NodeOperationError (bad input) or a NodeApiError (the API, or no answer).
async function takeScreenshot(
	this: IExecuteFunctions,
	baseUrl: string,
	i: number,
): Promise<INodeExecutionData> {
	const url = (this.getNodeParameter('url', i) as string).trim();
	if (!url) {
		throw new NodeOperationError(this.getNode(), 'The URL is empty', {
			itemIndex: i,
			description: 'Enter the address of the page to capture, for example https://example.com',
		});
	}
	const binaryPropertyName = this.getNodeParameter('binaryPropertyName', i, 'data') as string;
	const options = this.getNodeParameter('options', i, {}) as IDataObject;

	const body: IDataObject = {
		url,
		format: this.getNodeParameter('format', i, 'png') as string,
		full_page: this.getNodeParameter('fullPage', i, false) as boolean,
	};
	// Only what the user set: the API's own defaults cover the rest.
	for (const [option, parameter] of OPTIONAL_PARAMETERS) {
		if (options[option] !== undefined && options[option] !== '') body[parameter] = options[option];
	}

	const request: IHttpRequestOptions = {
		method: 'POST',
		url: `${baseUrl}/v1/screenshot`,
		body,
		json: true,
		encoding: 'arraybuffer',
		returnFullResponse: true,
		ignoreHttpStatusErrors: true,
	};
	let response: { statusCode: number; headers?: IDataObject; body: unknown };
	try {
		response = await this.helpers.httpRequestWithAuthentication.call(this, 'calotypeApi', request);
	} catch (error) {
		throw new NodeApiError(this.getNode(), error as JsonObject, {
			message: 'Could not reach the Calotype API',
			itemIndex: i,
		});
	}
	const data = toBuffer(response.body);

	if (response.statusCode !== 200) {
		const error = parseError(data);
		let description = error.message ?? `The Calotype API answered HTTP ${response.statusCode}`;
		if (error.details?.length) {
			description += ' ' + error.details.map((d) => `${d.field}: ${d.message}`).join('; ');
		}
		throw new NodeApiError(this.getNode(), { ...error } as JsonObject, {
			message: error.code ? `Calotype: ${error.code}` : 'The Calotype API returned an error',
			description,
			httpCode: String(response.statusCode),
			itemIndex: i,
		});
	}

	const mimeType = (header(response.headers, 'content-type') ?? `image/${body.format}`)
		.split(';')[0]
		.trim();
	const extension = EXTENSIONS[mimeType] ?? String(body.format);
	const fileName = (options.fileName as string | undefined)?.trim() || `screenshot.${extension}`;
	const [used, limit] = (header(response.headers, 'x-calotype-usage') ?? '').split('/').map(Number);
	const targetStatus = Number(header(response.headers, 'x-calotype-target-status'));

	const json: IDataObject = {
		url,
		format: extension === 'jpg' ? 'jpeg' : extension,
		mimeType,
		fileName,
		fileSize: data.length,
		truncated: header(response.headers, 'x-calotype-truncated') !== undefined,
	};
	if (targetStatus) json.targetStatus = targetStatus;
	if (Number.isFinite(used) && Number.isFinite(limit)) {
		json.screenshotsUsed = used;
		json.screenshotsLimit = limit;
	}

	return {
		json,
		binary: {
			[binaryPropertyName]: await this.helpers.prepareBinaryData(data, fileName, mimeType),
		},
		pairedItem: { item: i },
	};
}

export class Calotype implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'Calotype',
		name: 'calotype',
		icon: { light: 'file:calotype.svg', dark: 'file:calotype.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["url"]}}',
		description: 'Take a screenshot of a public web page',
		defaults: {
			name: 'Calotype',
		},
		inputs: [NodeConnectionTypes.Main],
		outputs: [NodeConnectionTypes.Main],
		usableAsTool: true,
		credentials: [
			{
				name: 'calotypeApi',
				required: true,
			},
		],
		properties: [
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				options: [
					{
						name: 'Take Screenshot',
						value: 'takeScreenshot',
						description: 'Capture a public web page and return it as an image file',
						action: 'Take screenshot',
					},
				],
				default: 'takeScreenshot',
			},
			{
				displayName: 'URL',
				name: 'url',
				type: 'string',
				required: true,
				default: '',
				placeholder: 'e.g. https://example.com',
				description:
					'The page to capture. It must be a public http or https address; private and local addresses are refused.',
			},
			{
				displayName: 'Format',
				name: 'format',
				type: 'options',
				options: [
					{ name: 'JPEG', value: 'jpeg' },
					{ name: 'PNG', value: 'png' },
					{ name: 'WebP', value: 'webp' },
				],
				default: 'png',
				description: 'The image format. JPEG and WebP files are much smaller than PNG.',
			},
			{
				displayName: 'Full Page',
				name: 'fullPage',
				type: 'boolean',
				default: false,
				description:
					'Whether to capture the whole scrolling page (up to 20,000 px tall) instead of only the visible part',
			},
			{
				displayName: 'Put Output File in Field',
				name: 'binaryPropertyName',
				type: 'string',
				required: true,
				default: 'data',
				placeholder: 'e.g. data',
				hint: 'The name of the output binary field to put the file in',
			},
			{
				displayName: 'Options',
				name: 'options',
				type: 'collection',
				placeholder: 'Add option',
				default: {},
				options: [
					{
						displayName: 'Block Ads',
						name: 'blockAds',
						type: 'boolean',
						default: true,
						description: 'Whether to block requests to common ad and tracking domains',
					},
					{
						displayName: 'Dark Mode',
						name: 'darkMode',
						type: 'boolean',
						default: true,
						description: 'Whether to ask the page for its dark colour scheme',
					},
					{
						displayName: 'Delay (Ms)',
						name: 'delayMs',
						type: 'number',
						typeOptions: { minValue: 0, maxValue: 10000 },
						default: 1000,
						description:
							'How long to wait after the page loads before capturing it, in milliseconds',
					},
					{
						displayName: 'Device Scale Factor',
						name: 'deviceScaleFactor',
						type: 'number',
						typeOptions: { minValue: 1, maxValue: 3, numberPrecision: 2 },
						default: 2,
						description:
							'The pixel density. 2 gives an image twice as wide and tall, as on a high-density screen.',
					},
					{
						displayName: 'File Name',
						name: 'fileName',
						type: 'string',
						default: '',
						placeholder: 'e.g. homepage.png',
						description:
							'The name of the output file. Left empty, it is "screenshot" with the format\'s extension.',
					},
					{
						displayName: 'Height',
						name: 'height',
						type: 'number',
						typeOptions: { minValue: 100, maxValue: 2160 },
						default: 800,
						description: 'The height of the browser window, in CSS pixels',
					},
					{
						displayName: 'Quality',
						name: 'quality',
						type: 'number',
						typeOptions: { minValue: 1, maxValue: 100 },
						default: 80,
						description: 'The image quality, from 1 to 100. For JPEG and WebP only.',
					},
					{
						displayName: 'Wait Until',
						name: 'waitUntil',
						type: 'options',
						options: [
							{
								name: 'Commit',
								value: 'commit',
								description: 'The first response from the page has arrived',
							},
							{
								name: 'DOM Content Loaded',
								value: 'domcontentloaded',
								description: "The page's HTML has been read",
							},
							{
								name: 'Load',
								value: 'load',
								description: 'The page and its images and styles have loaded',
							},
							{
								name: 'Network Idle',
								value: 'networkidle',
								description: 'The page has made no network requests for a short while',
							},
						],
						default: 'load',
						description: 'When the page counts as loaded',
					},
					{
						displayName: 'Width',
						name: 'width',
						type: 'number',
						typeOptions: { minValue: 100, maxValue: 3840 },
						default: 1280,
						description: 'The width of the browser window, in CSS pixels',
					},
				],
			},
		],
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];
		const credentials = await this.getCredentials('calotypeApi');
		const baseUrl = String(credentials.baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, '');

		for (let i = 0; i < items.length; i++) {
			const result = await takeScreenshot.call(this, baseUrl, i).catch((error: Error) => error);
			if (!(result instanceof Error)) {
				returnData.push(result);
			} else if (this.continueOnFail()) {
				returnData.push({ json: { error: result.message }, pairedItem: { item: i } });
			} else {
				throw result;
			}
		}

		return [returnData];
	}
}
