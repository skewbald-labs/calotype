# Publishing the client packages

Nothing in `clients/` is published yet. This page is the checklist for the day it is: what each registry asks of the account holder, the one-time setup, and the steps for each release. It was written on 21 November 2026 from the registries' own documentation as it read that day; the pages are listed under [Sources](#sources). Read them again before the first publish, because npm in particular has changed its rules several times this year.

| Package | Registry | Directory | Name free on 21 Nov 2026? |
|---|---|---|---|
| `calotype-mcp` | npm | `clients/npm` | Yes (registry answers 404) |
| `n8n-nodes-calotype` | npm | `clients/n8n` | Yes (404) |
| `calotype` | PyPI | `clients/python` | Yes (404) |

Also free that day and not used here: `calotype` on npm and `calotype-mcp` on PyPI. A name is not held until something is published under it. Check again with:

```sh
for u in registry.npmjs.org/calotype-mcp registry.npmjs.org/n8n-nodes-calotype registry.npmjs.org/calotype \
         pypi.org/pypi/calotype/json pypi.org/pypi/calotype-mcp/json; do
  echo "$(curl -s -o /dev/null -w '%{http_code}' https://$u) $u"   # 404 means free
done
```

## The short answer

Releases can run from GitHub Actions with no long-lived registry token, through trusted publishing (OIDC), from `skewbald-labs/calotype`. The workflow is `.github/workflows/publish-clients.yml`. It starts only by hand, and asks for the version to be typed again.

What cannot be automated:

| Step | Who | Why |
|---|---|---|
| Create the PyPI account | A person | PyPI's terms: "You must be a human to create an Account." The sign-up form has an hCaptcha. 2FA is mandatory. |
| Create the npm account | A person at a browser | The sign-up page sits behind a Cloudflare browser check, the address must be verified, and publishing needs 2FA, which npm now sets up as a security key or passkey (WebAuthn). |
| Add the trusted publishers | The account holder, signed in with 2FA | A web form on each registry. On npm it is a package setting, so it can only be filled in after the package exists. |
| First publish of each npm package | The account holder supplies a short-lived token once | npm has no equivalent of PyPI's "pending publisher": a package that does not exist cannot have a trusted publisher yet. |
| Submit the n8n node for verification | A person signed in to the n8n Creator Portal | A web form, then a review of up to four weeks. |

PyPI needs no token at any point. npm needs one token, once per package, deleted the same day.

## Before the first publish (both registries)

1. Merge `feat/clients` so that `clients/` and `.github/workflows/publish-clients.yml` are on `main` of `skewbald-labs/calotype`. Pushing a file under `.github/workflows/` needs a GitHub token with the workflow permission ("Workflows: read and write" on a fine-grained token, the `workflow` scope on a classic one).
2. The repository must be public (it is). npm provenance and n8n's source check both need that.
3. In the repository's Settings > Environments, create two environments, `npm` and `pypi`. The workflow names them, and the trusted publishers below are bound to them. Add a required reviewer to each if a second person should approve every release.
4. Decide who holds each registry account. See [Who holds the accounts](#who-holds-the-accounts): this is a question for the founder, not something this page settles.

## PyPI: `calotype`

### What PyPI requires of the account holder

- A person creates the account. From the terms of service (effective 25 February 2025): "You must be a human to create an Account. Accounts registered by 'bots' or other automated methods are not permitted." A "machine account" is allowed only where it "pre-date[s]" upload tokens (2019) and organization accounts (2025), so a new one for an agent is not.
- The registration form asks for an email address, a username and a password, agreement to the terms and the acceptable use policy, and an hCaptcha.
- The email address must be verified before publishing. PyPI's account page says: "Disposable or temporary email addresses are rejected, including throwaway addresses on otherwise legitimate providers." Whether an `agentmail.to` address passes that check is not known; a long-lived mailbox on a common provider or on `calotype.dev` is the safe choice.
- Two-factor authentication is mandatory for every account. The order is fixed: generate recovery codes, prove one was saved by entering it, then add a TOTP app or a WebAuthn security device.

### One-time setup (no token, the project does not need to exist)

Signed in at pypi.org: account sidebar > Publishing > add a new *pending* publisher, GitHub tab:

| Field | Value |
|---|---|
| PyPI Project Name | `calotype` |
| Owner | `skewbald-labs` |
| Repository name | `calotype` |
| Workflow name | `publish-clients.yml` |
| Environment name | `pypi` |

A pending publisher does not reserve the name. If someone else registers `calotype` before the first publish, the pending publisher is void. So add it and publish on the same day.

### Each release

1. Set the version in `clients/python/src/calotype/_version.py`. Commit and merge to `main`.
2. On GitHub: Actions > Publish clients > Run workflow, on `main`. Package: `calotype (PyPI)`. Version: the same version, typed again.
3. The `pypi-build` job builds the sdist and the wheel, installs the wheel and runs the tests. The `pypi-publish` job uploads with `pypa/gh-action-pypi-publish`, which exchanges GitHub's OIDC token for a PyPI token that lasts 15 minutes and attaches attestations. The first run creates the project and turns the pending publisher into a normal one.
4. Check: `pip install calotype==<version>` in a clean environment, then `CALOTYPE_LIVE=1 pytest` from `clients/python`.

To rehearse first, add the same pending publisher on test.pypi.org (a separate account) and add `repository-url: https://test.pypi.org/legacy/` to the publish step on a branch.

A version cannot be uploaded twice. If a release is wrong, yank it on pypi.org and publish the next version.

## npm: `calotype-mcp`

### What npm requires of the account holder

- An account: username, a public email address, a password, and agreement to the terms. The address "will be added to the metadata of your packages and will be visible to anyone", and must be verified before publishing. The sign-up page is served behind a Cloudflare browser check.
- npm's terms (last updated 10 March 2022) do not say an account holder must be a person. They do say "You will not impersonate any other individual", that the holder is "responsible for all action taken using your account", and that the address must be kept current.
- Publishing requires either 2FA enabled on the account, or a granular access token with "bypass 2FA" enabled. npm's 2FA pages now describe one method, a security key: a hardware key or a passkey through WebAuthn (Touch ID, Windows Hello, a YubiKey). Recovery codes are shown once at setup.
- Changing a package's settings, which is where trusted publishers live, always needs an interactive 2FA prompt. A token cannot do it.

### Why the first publish is different

npm's trusted publisher is a setting of a package (Packages > the package > Settings > Trusted Publisher), and `npm trust` says plainly: "The package you're configuring must already exist on the npm registry." So version 0.1.0 of each npm package has to get there another way. Two ways work:

- **A token, used once from GitHub Actions (recommended).** The package gets provenance from its first version, which matters for the n8n node (below).
  1. On npmjs.com: Access Tokens > Generate New Token > granular access token. Permissions: Read and write (publish and stage). Packages: All Packages, because a package that does not exist cannot be selected. Tick "Bypass two-factor authentication". Expiration: the shortest offered, one day.
  2. In the GitHub repository: Settings > Secrets and variables > Actions > New repository secret, name `NPM_TOKEN`.
  3. Run the workflow for the package (steps under "Each release").
  4. Delete the `NPM_TOKEN` secret and revoke the token on npmjs.com. Then add the trusted publisher (next section).

  This token can publish any package the account owns while it lives, so keep its life to minutes. npm has also said that direct publishing with bypass-2FA tokens "will be removed in January 2027". After that the first publish will need a stage-only token and `npm stage publish`, with the account holder approving the staged version under 2FA. The workflow would need its publish command changed for that.

- **By hand.** The account holder runs `npm publish --access public` in `clients/npm` on their own machine and answers the 2FA prompt. No token exists at any point, but that version has no provenance. Fine for `calotype-mcp`; not for the n8n node.

### One-time setup, after the package exists

On npmjs.com, signed in with 2FA: Packages > `calotype-mcp` > Settings > Trusted Publisher > GitHub Actions:

| Field | Value |
|---|---|
| Organization or user | `skewbald-labs` |
| Repository | `calotype` |
| Workflow filename | `publish-clients.yml` |
| Environment name | `npm` |
| Allowed actions | tick **npm publish** (see the note below) |

Notes from npm's page that are easy to miss:

- Since 3 September 2026 a new trusted publisher allows only `npm stage publish` unless `npm publish` is ticked. The workflow runs `npm publish`, so tick it. The stricter choice is to leave it unticked, change the workflow to `npm stage publish`, and approve each staged release on npmjs.com with 2FA.
- npm does not check the fields when they are saved; a typo shows only as a failed publish. Every field is case-sensitive.
- A new trusted publisher "must complete its first successful publish within 2 days", or it expires and has to be deleted and made again. So add it when the next release is ready, or publish a patch version to prove it.
- `repository.url` in `package.json` must match the GitHub repository exactly. It does: `skewbald-labs/calotype`.
- Only GitHub-hosted runners work, with Node 22.14 or newer and npm 11.5.1 or newer. The workflow uses Node 24.

Then, in the same Settings page under Publishing access, choose "Require two-factor authentication and disallow tokens". Trusted publishing still works; tokens no longer do.

### Each release

1. Set `version` in `clients/npm/package.json` and run `npm install` there so `package-lock.json` matches. Commit and merge to `main`.
2. On GitHub: Actions > Publish clients > Run workflow, on `main`. Package: `calotype-mcp (npm)`. Version: typed again.
3. The job installs with `npm ci`, runs the tests and runs `npm publish --access public`. With a trusted publisher, npm authenticates through OIDC and attaches a provenance attestation without being asked.
4. Check: `npx -y calotype-mcp@<version>` starts and answers `initialize`; the package page on npmjs.com shows the provenance badge.

A version cannot be published twice. Unpublishing is limited; deprecate a bad version and publish the next one.

## npm and n8n: `n8n-nodes-calotype`

The npm account, the first-publish token, the trusted publisher (package `n8n-nodes-calotype`, same five fields) and the release steps are the same as for `calotype-mcp`, with package `n8n-nodes-calotype (npm)` in the workflow and the version in `clients/n8n/package.json` and `clients/n8n/CHANGELOG.md`. Two things differ:

- **It must be published from GitHub Actions with provenance, from its first version.** n8n: "From May 1st 2026, nodes submitted for verification must be published using GitHub Actions with a provenance statement. n8n won't accept verified nodes published directly from a local machine." So use the token route above for 0.1.0, never the by-hand route.
- **The workflow runs `npm run release`,** not `npm publish`. `package.json` has `prepublishOnly: n8n-node prerelease`, which refuses a bare `npm publish`. Inside GitHub Actions, `n8n-node release` lints, builds and publishes with provenance turned on.

After it is on npm, anyone can install it in a self-hosted n8n from Settings > Community Nodes. Listing it in n8n's own nodes panel, on n8n Cloud too, needs verification.

### Submitting for verification

1. Run n8n's scanner against the published package: `npx @n8n/scan-community-package n8n-nodes-calotype`. It checks the provenance and the source as well as the code. (Against the local build it already passes; those two checks are skipped until the package is published.)
2. Run the node once in a real n8n (`npm run dev` in `clients/n8n`, which needs Docker or Podman) and take a screenshot with it. This has not been done yet.
3. Sign up or sign in at the n8n Creator Portal, https://creators.n8n.io/nodes, and submit the package. This is a person's step.
4. "Reviews can take up to four weeks. n8n emails you as your submission moves through review." n8n "reserves the right to reject nodes that compete with any of n8n's paid features".

### Where the node stands against n8n's requirements

| Requirement (n8n's verification and UX guidelines) | Status |
|---|---|
| Package name starts with `n8n-nodes-` | Met |
| `n8n-community-node-package` in the keywords | Met |
| Nodes and credentials listed under `n8n` in `package.json` | Met |
| Built from the `n8n-node` scaffolding | Met: generated with `npm create @n8n/node` (programmatic template), then written |
| Linter passes (`npm run lint`, strict mode, n8n's own ESLint config) | Met: no errors, no warnings |
| `npx @n8n/scan-community-package` passes | Met for the local build; the provenance and source checks need the published package |
| No runtime dependencies | Met: `dependencies` is absent; `n8n-workflow` is a peer dependency |
| No access to environment variables or the file system | Met |
| MIT licence | Met |
| TypeScript, with error handling through `NodeApiError` and `NodeOperationError`, and Continue On Fail | Met |
| English only | Met |
| README with usage and authentication details | Met |
| One third-party service per package; not a logic or flow-control node; not a copy of an existing node | Met |
| API key is a password field | Met |
| Title Case for names, sentence case for actions and descriptions, "e.g." placeholders | Met |
| Light and dark icons | Met in form: both files exist and are the same mark, which reads on both themes |
| OAuth credential "if available" | Not applicable: Calotype has no OAuth |
| CRUD operations per resource | Not applicable: the API has one action |
| Published from GitHub Actions with a provenance statement | Not yet: nothing is published. The workflow is ready |
| npm repository URL matches a public GitHub repository | Will be met: `repository` points at `skewbald-labs/calotype`, directory `clients/n8n` |
| "Package author / maintainer matches between npm and the repository" | Open: depends on which npm account publishes. The npm account's name and address should match the GitHub owner's |
| Tested in a running n8n | Not yet (step 2 above) |

Two things to know before submitting. The node lives in a subdirectory of a larger repository, where n8n's starter assumes one repository per node; npm provenance supports that through `repository.directory`, but n8n's reviewers may ask about it. And the node is written in the programmatic style, where n8n suggests the declarative style for simple HTTP APIs: it needs the response as a file with a name and a type, the API's JSON error decoded from a binary response, and the usage headers copied into the item, and its `execute` can be tested without a running n8n.

## Who holds the accounts

This decides who does the human steps, and it is the founder's call.

- The project's rule is that Skewbald uses its own accounts and never the founder's identity. The packages publish from Skewbald's GitHub organization, `skewbald-labs`.
- PyPI's terms require a person to create the account and make that person responsible for it. So the PyPI account is a person's account, whatever it is named. With a pending publisher that person needs to sign in once, add five fields, and never hand over a credential: the releases come from the GitHub workflow.
- npm's terms do not require a person, but its sign-up and 2FA need a browser check and a security key or passkey. If the account is to be Skewbald's own, a person still has to pass the browser check and register the passkey, and somebody has to hold that passkey for the later settings changes (adding each trusted publisher, one prompt each).
- For the n8n Creator Portal, n8n checks that the npm maintainer matches the repository's owner.

Once the trusted publishers exist, no credential for either registry is needed by the agent or stored anywhere.

## Sources

Read on 21 November 2026.

npm:

- Trusted publishing: https://docs.npmjs.com/trusted-publishers (last edited 6 October 2026)
- `npm trust`: https://docs.npmjs.com/cli/v11/commands/npm-trust
- Provenance: https://docs.npmjs.com/generating-provenance-statements
- Staged publishing: https://docs.npmjs.com/staged-publishing (last edited 10 November 2026)
- Access tokens: https://docs.npmjs.com/about-access-tokens and https://docs.npmjs.com/creating-and-viewing-access-tokens
- 2FA: https://docs.npmjs.com/about-two-factor-authentication, https://docs.npmjs.com/configuring-two-factor-authentication and https://docs.npmjs.com/requiring-2fa-for-package-publishing-and-settings-modification (last edited 3 August 2026)
- Sign-up: https://docs.npmjs.com/creating-a-new-npm-user-account
- Terms: https://docs.npmjs.com/policies/open-source-terms

PyPI:

- Trusted publishers: https://docs.pypi.org/trusted-publishers/
- Pending publishers: https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/
- Adding a publisher: https://docs.pypi.org/trusted-publishers/adding-a-publisher/
- Using a publisher: https://docs.pypi.org/trusted-publishers/using-a-publisher/
- Attestations: https://docs.pypi.org/attestations/producing-attestations/
- 2FA: https://docs.pypi.org/your-account/two-factor-authentication/
- Managing your account (email rules): https://docs.pypi.org/your-account/managing-your-account/
- Registration form: https://pypi.org/account/register/
- Terms of service: https://policies.python.org/pypi.org/Terms-of-Service/

n8n:

- Submit community nodes: https://docs.n8n.io/integrations/creating-nodes/deploy/submit-community-nodes/
- Building community nodes: https://docs.n8n.io/integrations/community-nodes/build-community-nodes/
- Verification guidelines: https://docs.n8n.io/integrations/creating-nodes/build/reference/verification-guidelines/
- UX guidelines: https://docs.n8n.io/integrations/creating-nodes/build/reference/ux-guidelines/
- The `n8n-node` tool: https://docs.n8n.io/integrations/creating-nodes/build/n8n-node/
- Starter repository and its publish workflow: https://github.com/n8n-io/n8n-nodes-starter
- Scanner: https://www.npmjs.com/package/@n8n/scan-community-package
- Creator Portal: https://creators.n8n.io/nodes

Two places where the pages disagree: npm's trusted publishing page says a package can have up to 10 trusted publishers, while the `npm trust` page says one; and npm's page shows `actions/checkout@v6` and `actions/setup-node@v6`, while the latest majors on GitHub that day were v7, which the workflow uses.
