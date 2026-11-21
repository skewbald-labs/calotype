import type {
	IAuthenticateGeneric,
	Icon,
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

export class CalotypeApi implements ICredentialType {
	name = 'calotypeApi';

	displayName = 'Calotype API';

	icon: Icon = {
		light: 'file:../nodes/Calotype/calotype.svg',
		dark: 'file:../nodes/Calotype/calotype.dark.svg',
	};

	documentationUrl = 'https://github.com/skewbald-labs/calotype/tree/main/clients/n8n#credentials';

	properties: INodeProperties[] = [
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			required: true,
			default: '',
			placeholder: 'e.g. cal_...',
			description:
				'Your Calotype key. A free one needs no account: send one POST request to https://calotype.dev/v1/keys.',
		},
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'https://calotype.dev',
			description: 'Change this only if you run your own Calotype server',
		},
	];

	authenticate: IAuthenticateGeneric = {
		type: 'generic',
		properties: {
			headers: {
				Authorization: '=Bearer {{$credentials.apiKey}}',
			},
		},
	};

	// Reads the key's usage: it checks the key and takes no screenshot.
	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl}}',
			url: '/v1/usage',
			method: 'GET',
		},
	};
}
