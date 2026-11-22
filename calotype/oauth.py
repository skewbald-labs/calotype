"""OAuth sign-in for the remote MCP endpoint at ``/mcp``.

Some MCP clients cannot send a header and sign in only with OAuth: claude.ai
and ChatGPT connectors, among others. For them Calotype is both the resource
server and the authorization server, following the MCP authorization spec
(2025-11-25): OAuth 2.1, authorization code with PKCE (S256 only).

The access token is the Calotype API key itself. On the consent page the
person either approves, which makes a new free key (the same plan and the same
per-IP limits as ``POST /v1/keys``), or pastes a key they already have. Either
way the client receives that key as a bearer token and sends it to ``/mcp``
like any other key. There is no ``expires_in`` and no refresh token: the key
stays valid until it is revoked, and then ``/mcp`` answers 401 and the client
signs in again.

Endpoints (only when ``CALOTYPE_OAUTH_SECRET`` is set; otherwise all 404):

* ``/.well-known/oauth-protected-resource`` and ``.../oauth-protected-resource/mcp``:
  RFC 9728 protected resource metadata. ``resource`` is ``<origin>/mcp``.
* ``/.well-known/oauth-authorization-server`` (and the same with ``/mcp``):
  RFC 8414 authorization server metadata.
* ``POST /oauth/register``: RFC 7591 dynamic client registration, stateless.
  The client id is the registration itself (name, redirect URIs, auth method,
  time) with an HMAC, so registering stores nothing and any instance can check
  it. Limited per client IP.
* Client ID Metadata Documents: a ``client_id`` that is an https URL is fetched
  (through the same address policy as screenshots, so never a private address),
  checked, and cached in memory for five minutes. Public clients only.
* ``GET /oauth/authorize``: the consent page. ``POST /oauth/authorize``: its forms.
* ``POST /oauth/token``: the code for the key.

Authorization codes are random, single use, valid for a minute, and stored in
``Storage`` (``OAuthCode``) so any instance can redeem them. The stored record
holds the code's hash and the key sealed with a pad derived from the code, so
the database alone yields no key.

Redirect URIs must match the registration exactly, except that a loopback URI
(``http://127.0.0.1``, ``http://localhost``, ``http://[::1]``) matches on any
port, as RFC 8252 asks for native apps. Only https, loopback http and the
private-use schemes of a few known editors (``NATIVE_SCHEMES``, RFC 8252
section 7.1; Cursor signs in through ``cursor://``) are accepted, never a
fragment. PKCE binds each code to the client that asked for it, so a code
caught by another app on the same machine is useless.

The consent form uses no cookie. Its hidden fields carry an HMAC over the
request (the form token), checked again on submit, and a submit from another
site is refused (``Sec-Fetch-Site``, ``Origin``). The page cannot be framed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .logs import log_event
from .pages import CONTACT, render_document
from .ratelimit import client_ip
from .ssrf import AddressPolicy, BlockedTarget
from .storage import REF_LABEL_PREFIX, REF_TAG, OAuthCode

log = logging.getLogger("calotype.oauth")

MIN_SECRET_LEN = 32
CODE_TTL_S = 60  # an authorization code is redeemed by the client at once
FORM_TTL_S = 15 * 60  # how long the consent page may stay open
CLIENT_PREFIX = "c1."
AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_URI_LEN = 2000
MAX_REDIRECT_URIS = 10
MAX_NAME_LEN = 80
MAX_STATE_LEN = 1000
MAX_CLIENT_ID_LEN = 4000
MAX_BODY_BYTES = 16 * 1024
# Client ID Metadata Documents
DOC_MAX_BYTES = 16 * 1024
DOC_TIMEOUT_S = 5.0
DOC_CACHE_S = 300.0
DOC_CACHE_MAX = 256

_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

FORM_FIELDS = ("client_id", "redirect_uri", "code_challenge", "state", "resource")


def oauth_enabled(secret: str) -> bool:
    return len(secret or "") >= MIN_SECRET_LEN


class OAuthError(Exception):
    """An OAuth error: ``error`` is the RFC code, ``description`` a sentence for people."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pad(code: str, n: int) -> bytes:
    """The one-time pad that seals the key in a code's record. Only the code yields it."""
    return hashlib.shake_256(b"calotype oauth code pad\0" + code.encode("ascii")).digest(n)


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


# --- redirect URIs --------------------------------------------------------------------


# Private-use URI schemes (RFC 8252 section 7.1) of editors that sign in to MCP servers.
NATIVE_SCHEMES = frozenset({"cursor", "vscode", "vscode-insiders", "windsurf"})


def redirect_uri_problem(uri: Any) -> str | None:
    """Why ``uri`` cannot be a redirect URI, or None if it can.

    https with a host name, or http to a loopback address; no fragment, no
    user info. The host is also checked character by character, because its
    origin goes into the consent page's Content-Security-Policy.
    """
    if not isinstance(uri, str) or not uri:
        return "A redirect URI must be a non-empty string."
    if len(uri) > MAX_URI_LEN:
        return f"A redirect URI may be at most {MAX_URI_LEN} characters."
    if "#" in uri:
        return "A redirect URI may not have a fragment."
    if any(c.isspace() for c in uri) or _CONTROL.search(uri):
        return "A redirect URI may not contain spaces or control characters."
    try:
        parts = urlsplit(uri)
        parts.port  # raises ValueError for a bad port
    except ValueError:
        return "The redirect URI is not a valid URL."
    host = parts.hostname or ""
    if "@" in parts.netloc:
        return "A redirect URI may not carry a user name or password."
    if not (host == "::1" or _HOSTNAME.match(host)):
        return "The redirect URI's host is not valid."
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in LOOPBACK_HOSTS:
        return None
    if parts.scheme in NATIVE_SCHEMES and host:
        return None
    return ("A redirect URI must use https, http to 127.0.0.1, localhost or [::1], "
            "or an editor's own scheme (" + ", ".join(sorted(NATIVE_SCHEMES)) + ").")


def is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS


def redirect_matches(requested: str, registered: str) -> bool:
    """Exact match; for loopback http, any port (RFC 8252 section 7.3)."""
    if requested == registered:
        return True
    if not (is_loopback(requested) and is_loopback(registered)):
        return False
    a, b = urlsplit(requested), urlsplit(registered)
    return (a.hostname, a.path, a.query) == (b.hostname, b.path, b.query)


def _origin(uri: str) -> str:
    """scheme://host[:port] of a checked redirect URI, for the consent page's form-action."""
    parts = urlsplit(uri)
    host = f"[{parts.hostname}]" if parts.hostname == "::1" else parts.hostname
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def _with_params(uri: str, params: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = parts.query + ("&" if parts.query else "") + urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# --- clients -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str  # as the client describes itself; "" if it gave none
    redirect_uris: tuple[str, ...]
    auth_method: str  # one of AUTH_METHODS
    kind: str  # "registered" (RFC 7591) or "metadata" (a Client ID Metadata Document)
    host: str = ""  # for "metadata": the host of the client_id URL, which vouches for the document

    def allows(self, redirect_uri: str) -> bool:
        return redirect_uri_problem(redirect_uri) is None and any(
            redirect_matches(redirect_uri, r) for r in self.redirect_uris
        )


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return " ".join(_CONTROL.sub(" ", name).split())[:MAX_NAME_LEN]


def client_tag(name: str) -> str:
    """The source tag of a key made through this client: ``oauth-<slug of its name>``."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24].strip("-")
    tag = f"oauth-{slug}" if slug else "oauth"
    return tag if REF_TAG.match(tag) else "oauth"


def metadata_url_problem(url: str) -> str | None:
    """Why ``url`` cannot be a Client ID Metadata Document URL, or None."""
    if len(url) > 1024 or "#" in url or any(c.isspace() for c in url):
        return "The client id is not a usable URL."
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return "The client id is not a valid URL."
    if parts.scheme != "https" or "@" in parts.netloc or not _HOSTNAME.match(parts.hostname or ""):
        return "A client id URL must be https with a host name."
    segments = parts.path.split("/")
    if parts.path in ("", "/") or "." in segments or ".." in segments:
        return "A client id URL must have a path, without dot segments."
    return None


async def fetch_client_document(url: str, policy: AddressPolicy) -> Any:
    """GET a Client ID Metadata Document, through the address policy, and parse it.

    The host is resolved and checked by ``policy`` (the screenshot SSRF rule),
    and the request goes to the exact address that was checked, with the
    original name for TLS and Host, so a DNS answer cannot change in between.
    No redirects, at most ``DOC_MAX_BYTES``, ``DOC_TIMEOUT_S`` in all.
    """
    target = await policy.check_url(url)
    addresses = await policy.resolve_checked(target.host, target.port)
    ip = addresses[0]
    netloc = (f"[{ip}]" if ip.version == 6 else str(ip)) + f":{target.port}"
    parts = urlsplit(url)
    ip_url = urlunsplit(("https", netloc, parts.path, parts.query, ""))
    headers = {"Host": parts.netloc, "Accept": "application/json", "User-Agent": "Calotype-OAuth/1"}
    async with asyncio.timeout(DOC_TIMEOUT_S):
        async with httpx.AsyncClient(timeout=DOC_TIMEOUT_S, follow_redirects=False, trust_env=False) as http:
            async with http.stream(
                "GET", ip_url, headers=headers, extensions={"sni_hostname": target.host}
            ) as response:
                if response.status_code != 200:
                    raise ValueError(f"the document answered HTTP {response.status_code}")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > DOC_MAX_BYTES:
                        raise ValueError("the document is too large")
    return json.loads(body)


# --- the server ---------------------------------------------------------------------------


class OAuthServer:
    """The authorization server's state and rules; ``add_oauth_routes`` puts it on the app."""

    def __init__(
        self,
        *,
        secret: str,
        issuer: str,
        policy: AddressPolicy,
        fetch_document: Callable[[str, AddressPolicy], Awaitable[Any]] = fetch_client_document,
    ):
        if not oauth_enabled(secret):
            raise ValueError(f"the OAuth secret must be at least {MIN_SECRET_LEN} characters")
        self._secret = secret.encode("utf-8")
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.policy = policy
        self.fetch_document = fetch_document
        parts = urlsplit(self.issuer)
        self._scheme, self._netloc = parts.scheme.lower(), parts.netloc.lower()
        self._documents: dict[str, tuple[float, Client]] = {}

    # --- signing ---------------------------------------------------------------------

    def _mac(self, purpose: bytes, data: bytes) -> bytes:
        return hmac.new(self._secret, purpose + b"\0" + data, hashlib.sha256).digest()

    def client_secret(self, client_id: str) -> str:
        return _b64e(self._mac(b"client secret", client_id.encode("utf-8")))

    def form_token(self, fields: dict[str, str], now: int | None = None) -> str:
        ts = int(time.time()) if now is None else now
        data = json.dumps([ts, *(fields.get(f, "") for f in FORM_FIELDS)]).encode("utf-8")
        return f"{ts}.{_b64e(self._mac(b'consent form', data))}"

    def form_token_ok(self, token: str, fields: dict[str, str], now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        ts_text, _, _ = token.partition(".")
        if not ts_text.isdigit() or not 0 <= now - int(ts_text) <= FORM_TTL_S:
            return False
        return hmac.compare_digest(token, self.form_token(fields, int(ts_text)))

    # --- discovery -------------------------------------------------------------------

    def metadata_url(self, ref: str | None = None) -> str:
        url = f"{self.issuer}/.well-known/oauth-protected-resource/mcp"
        return f"{url}?ref={ref}" if ref and REF_TAG.match(ref) else url

    def challenge(self, error: str | None = None, ref: str | None = None) -> str:
        """The WWW-Authenticate value for a 401 from /mcp (RFC 9728 section 5.1, RFC 6750)."""
        value = f'Bearer resource_metadata="{self.metadata_url(ref)}"'
        if error == "invalid_token":
            value += ', error="invalid_token", error_description="The API key is not valid."'
        return value

    def challenge_for(self, request: Request, error: str | None = None) -> str:
        return self.challenge(error, request.query_params.get("ref"))

    def protected_resource_metadata(self, ref: str | None = None) -> dict[str, Any]:
        # A URL carrying ?ref=<tag> (a directory's copy) is its own resource, so a
        # client that checks resource against the URL it was given still matches.
        resource = f"{self.resource}?ref={ref}" if ref and REF_TAG.match(ref) else self.resource
        return {
            "resource": resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "Calotype",
            "resource_documentation": f"{self.issuer}/docs#oauth",
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/oauth/authorize",
            "token_endpoint": f"{self.issuer}/oauth/token",
            "registration_endpoint": f"{self.issuer}/oauth/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code"],
            "token_endpoint_auth_methods_supported": list(AUTH_METHODS),
            "code_challenge_methods_supported": ["S256"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "service_documentation": f"{self.issuer}/docs#oauth",
        }

    def resource_ok(self, value: str) -> bool:
        """Whether an RFC 8707 ``resource`` names this server: the origin or ``/mcp``."""
        if not value or len(value) > MAX_URI_LEN or "#" in value:
            return False
        parts = urlsplit(value)
        return (
            parts.scheme.lower() == self._scheme
            and parts.netloc.lower() == self._netloc
            and parts.path.rstrip("/") in ("", "/mcp")
        )

    # --- clients ---------------------------------------------------------------------

    def register(self, data: Any, now: int | None = None) -> dict[str, Any]:
        """RFC 7591 registration. Stores nothing: the client id carries the registration, signed."""
        if not isinstance(data, dict):
            raise OAuthError("invalid_client_metadata", "The body must be a JSON object.")
        uris = data.get("redirect_uris")
        if not isinstance(uris, list) or not 1 <= len(uris) <= MAX_REDIRECT_URIS:
            raise OAuthError(
                "invalid_redirect_uri", f"redirect_uris must list 1 to {MAX_REDIRECT_URIS} redirect URIs."
            )
        for uri in uris:
            problem = redirect_uri_problem(uri)
            if problem:
                raise OAuthError("invalid_redirect_uri", problem)
        if data.get("client_name") is not None and not isinstance(data.get("client_name"), str):
            raise OAuthError("invalid_client_metadata", "client_name must be a string.")
        name = clean_name(data.get("client_name"))
        method = data.get("token_endpoint_auth_method") or "none"
        if method not in AUTH_METHODS:
            raise OAuthError(
                "invalid_client_metadata", f"token_endpoint_auth_method must be one of {', '.join(AUTH_METHODS)}."
            )
        grants = data.get("grant_types")
        if grants is not None and (not isinstance(grants, list) or "authorization_code" not in grants):
            raise OAuthError("invalid_client_metadata", "Only the authorization_code grant is offered.")
        responses = data.get("response_types")
        if responses is not None and (not isinstance(responses, list) or "code" not in responses):
            raise OAuthError("invalid_client_metadata", "Only the code response type is offered.")
        issued = int(time.time()) if now is None else now
        payload = json.dumps(
            {"n": name, "r": uris, "a": method, "t": issued}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        client_id = f"{CLIENT_PREFIX}{_b64e(payload)}.{_b64e(self._mac(b'client', payload)[:16])}"
        if len(client_id) > MAX_CLIENT_ID_LEN:
            raise OAuthError("invalid_client_metadata", "The registration is too large.")
        answer: dict[str, Any] = {
            "client_id": client_id,
            "client_id_issued_at": issued,
            "redirect_uris": uris,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if name:
            answer["client_name"] = name
        if method != "none":
            answer["client_secret"] = self.client_secret(client_id)
            answer["client_secret_expires_at"] = 0
        return answer

    def registered_client(self, client_id: str) -> Client | None:
        """The client a ``c1.`` id describes, if its signature holds."""
        if not client_id.startswith(CLIENT_PREFIX) or len(client_id) > MAX_CLIENT_ID_LEN:
            return None
        body, _, mac = client_id[len(CLIENT_PREFIX):].rpartition(".")
        try:
            payload = _b64d(body)
            if not hmac.compare_digest(_b64d(mac), self._mac(b"client", payload)[:16]):
                return None
            data = json.loads(payload)
            return Client(client_id, data["n"], tuple(data["r"]), data["a"], "registered")
        except (ValueError, KeyError, TypeError):
            return None

    async def metadata_client(self, url: str) -> Client:
        """The client a Client ID Metadata Document describes; fetched, checked and cached."""
        cached = self._documents.get(url)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        problem = metadata_url_problem(url)
        if problem:
            raise OAuthError("invalid_client", problem)
        try:
            doc = await self.fetch_document(url, self.policy)
        except BlockedTarget:
            raise OAuthError("invalid_client", "The client id URL points at an address Calotype does not fetch.")
        except Exception as e:
            log_event(log, "oauth client document not fetched", host=urlsplit(url).hostname, reason=type(e).__name__)
            raise OAuthError("invalid_client", "The client's metadata document could not be fetched.")
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            raise OAuthError("invalid_client", "The client's metadata document does not name this client id.")
        uris = doc.get("redirect_uris")
        valid = tuple(u for u in uris if redirect_uri_problem(u) is None) if isinstance(uris, list) else ()
        if not valid:
            raise OAuthError("invalid_client", "The client's metadata document lists no usable redirect URI.")
        # Public clients only. A document may prefer another method but list "none" among
        # the ones it supports (ChatGPT's does: private_key_jwt, then none); it then uses none here.
        methods = doc.get("token_endpoint_auth_methods_supported")
        public = (doc.get("token_endpoint_auth_method") or "none") == "none" or (
            isinstance(methods, list) and "none" in methods
        )
        if not public:
            raise OAuthError("invalid_client", "Calotype accepts metadata-document clients as public clients only.")
        host = urlsplit(url).hostname or ""
        client = Client(url, clean_name(doc.get("client_name")), valid, "none", "metadata", host)
        if len(self._documents) >= DOC_CACHE_MAX:
            self._documents.clear()
        self._documents[url] = (time.monotonic() + DOC_CACHE_S, client)
        return client

    async def resolve_client(self, client_id: str) -> Client:
        if not client_id:
            raise OAuthError("invalid_request", "client_id is missing.")
        if client_id.startswith("https://"):
            return await self.metadata_client(client_id)
        client = self.registered_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "This client id is not known to Calotype. Register the client again.")
        return client

    # --- codes -----------------------------------------------------------------------

    def new_code(
        self, client: Client, redirect_uri: str, code_challenge: str, resource: str, raw_key: str
    ) -> tuple[str, OAuthCode]:
        code = secrets.token_urlsafe(32)
        key = raw_key.encode("utf-8")
        now = int(time.time())
        record = OAuthCode(
            id=_sha256_hex(code),
            client_hash=_sha256_hex(client.client_id),
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            resource=resource,
            sealed_key=_b64e(_xor(key, _pad(code, len(key)))),
            expires_at=now + CODE_TTL_S,
            created_at=now,
        )
        return code, record

    @staticmethod
    def unseal(code: str, record: OAuthCode) -> str:
        sealed = _b64d(record.sealed_key)
        return _xor(sealed, _pad(code, len(sealed))).decode("utf-8")


def pkce_ok(verifier: str, challenge: str) -> bool:
    if not _VERIFIER.match(verifier or ""):
        return False
    return hmac.compare_digest(_b64e(hashlib.sha256(verifier.encode("ascii")).digest()), challenge)


# --- the consent page ----------------------------------------------------------------------

TITLE = "Connect an app to Calotype | Calotype"
DESCRIPTION = "An app asks to use Calotype's screenshot tool for you. Approve to give it a free key, or paste your own."


def _hidden(fields: dict[str, str], token: str) -> str:
    inputs = [
        f'<input type="hidden" name="{name}" value="{html.escape(fields.get(name, ""), quote=True)}">'
        for name in FORM_FIELDS
    ]
    inputs.append(f'<input type="hidden" name="form_token" value="{html.escape(token, quote=True)}">')
    return "\n    ".join(inputs)


def consent_content(
    client: Client, fields: dict[str, str], token: str, free_quota: int | None, error: str = ""
) -> str:
    name = html.escape(client.name or "An app that gave no name")
    back = html.escape(urlsplit(fields["redirect_uri"]).hostname or "")
    if client.kind == "metadata":
        who = f"<strong>{name}</strong>, identified by <code>{html.escape(client.host)}</code>"
    else:
        who = f"<strong>{name}</strong> (the name it registered with; Calotype has not checked it)"
    local = ""
    if is_loopback(fields["redirect_uri"]):
        local = (
            '  <p class="notice">This app runs on your own computer: it returns to '
            f"<code>{back}</code>. Approve only if you started this sign-in yourself, just now.</p>\n"
        )
    alert = f'  <p class="error" role="alert">{html.escape(error)}</p>\n' if error else ""
    quota = f"{free_quota:,} screenshots a month, " if free_quota else ""
    hidden = _hidden(fields, token)
    return f"""  <h1>Connect an app to Calotype</h1>
  <p class="lede">An app wants to take screenshots with Calotype for you.</p>
  <ul>
    <li>App: {who}</li>
    <li>After you choose, you go back to <code>{back}</code></li>
  </ul>
{local}{alert}
  <h2 id="new-key">Give it a free key</h2>
  <p>Approve makes a new free Calotype key ({quota}no account, no email) and hands it to the app. The app holds the key and sends it with each screenshot. You do not see the key, and Calotype keeps only its hash.</p>
  <form method="post" action="/oauth/authorize">
    {hidden}
    <div class="actions">
      <button type="submit" name="action" value="approve">Approve</button>
      <button type="submit" name="action" value="deny" class="secondary">Deny</button>
    </div>
  </form>

  <h2 id="own-key">Already have a key?</h2>
  <p>Paste it to let the app use your key instead, with its plan and its quota. Calotype checks the key and hands it to the app.</p>
  <form method="post" action="/oauth/authorize" class="upgrade">
    {hidden}
    <p><label for="key">Your key</label>
    <input id="key" name="key" type="password" autocomplete="off" spellcheck="false" placeholder="cal_..."></p>
    <div class="actions"><button type="submit" name="action" value="paste">Use this key</button></div>
  </form>

  <p class="muted">To stop the app using the key, remove Calotype from the app. To have a key revoked, write to <a href="mailto:{CONTACT}">{CONTACT}</a>. What is stored: <a href="/privacy#oauth">Privacy</a>.</p>
"""


def problem_content(title: str, text: str, back: str = "") -> str:
    """An error page. ``back``: a link to the app's redirect URI carrying the error, for the person to follow."""
    link = ""
    if back:
        host = html.escape(urlsplit(back).hostname or "")
        link = f'  <p><a href="{html.escape(back, quote=True)}">Return to {host} with this error</a></p>\n'
    return f"""  <h1>{html.escape(title)}</h1>
  <p class="lede">{html.escape(text)}</p>
{link}  <p>Start again from the app you were connecting. How sign-in works: <a href="/docs#oauth">Docs</a>.</p>
"""


# --- routes --------------------------------------------------------------------------------

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, MCP-Protocol-Version",
    "Access-Control-Max-Age": "86400",
}


def add_oauth_routes(app: FastAPI, server: OAuthServer, *, settings, site_dir: Path) -> None:
    """Put the discovery documents, registration, the consent page and the token endpoint on ``app``.

    Uses ``app.state.storage``, the key-creation limiters (``key_limiter``,
    ``key_global_limiter``, shared with ``POST /v1/keys``) and
    ``app.state.oauth_register_limiter``.
    """
    free_quota = settings.plans["free"].monthly_quota if "free" in settings.plans else None

    def ip_of(request: Request) -> str:
        return client_ip(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            settings.trusted_proxy_hops,
        )

    def as_json(body: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(
            body, status_code=status, headers={"Cache-Control": "no-store", **_CORS, **(headers or {})}
        )

    def oauth_error(e: OAuthError, headers: dict | None = None) -> JSONResponse:
        return as_json({"error": e.error, "error_description": e.description}, e.status, headers)

    async def preflight(_request: Request) -> Response:
        return Response(status_code=204, headers=_CORS)

    def html_page(content: str, status: int = 200, form_origin: str = "") -> Response:
        doc = render_document(site_dir, path="/oauth/authorize", title=TITLE, description=DESCRIPTION, content=content)
        csp = doc.csp
        if form_origin:
            # The form's answer is a redirect to the app; browsers apply form-action to it.
            csp = csp.replace("form-action 'self'", f"form-action 'self' {form_origin}")
        return Response(
            doc.body,
            status_code=status,
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": csp,
                "X-Frame-Options": "DENY",
                "X-Content-Type-Options": "nosniff",
                # Not no-referrer: with it, browsers send "Origin: null" on the form's POST,
                # which the Origin check below must refuse. same-origin sends nothing to the app.
                "Referrer-Policy": "same-origin",
                "X-Robots-Tag": "noindex",
            },
        )

    def problem_page(title: str, text: str, status: int = 400) -> Response:
        return html_page(problem_content(title, text), status)

    def consent_page(client: Client, fields: dict[str, str], error: str = "", status: int = 200) -> Response:
        token = server.form_token(fields)
        return html_page(
            consent_content(client, fields, token, free_quota, error), status, _origin(fields["redirect_uri"])
        )

    def back_to_client(redirect_uri: str, params: dict[str, str], state: str) -> Response:
        params = {**params, **({"state": state} if state else {}), "iss": server.issuer}
        return RedirectResponse(
            _with_params(redirect_uri, params), status_code=303, headers={"Cache-Control": "no-store"}
        )

    # --- discovery ---------------------------------------------------------------------

    async def resource_metadata(request: Request) -> Response:
        return as_json(server.protected_resource_metadata(request.query_params.get("ref")),
                       headers={"Cache-Control": "public, max-age=300"})

    async def server_metadata(_request: Request) -> Response:
        return as_json(server.authorization_server_metadata(), headers={"Cache-Control": "public, max-age=300"})

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        app.add_route(path, resource_metadata, methods=["GET"], include_in_schema=False)
        app.add_route(path, preflight, methods=["OPTIONS"], include_in_schema=False)
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp"):
        app.add_route(path, server_metadata, methods=["GET"], include_in_schema=False)
        app.add_route(path, preflight, methods=["OPTIONS"], include_in_schema=False)

    # --- registration ------------------------------------------------------------------

    async def register(request: Request) -> Response:
        ok, retry = app.state.oauth_register_limiter.check(ip_of(request))
        if not ok:
            return oauth_error(
                OAuthError("temporarily_unavailable", "Too many registrations from this address. Try again later.", 429),
                {"Retry-After": str(retry)},
            )
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return oauth_error(OAuthError("invalid_client_metadata", "The registration is too large."))
        try:
            data = json.loads(body or b"null")
        except ValueError:
            return oauth_error(OAuthError("invalid_client_metadata", "The body must be a JSON object."))
        try:
            answer = server.register(data)
        except OAuthError as e:
            return oauth_error(e)
        log_event(log, "oauth client registered", client=client_tag(answer.get("client_name", "")),
                  auth_method=answer["token_endpoint_auth_method"])
        return as_json(answer, 201)

    app.add_route("/oauth/register", register, methods=["POST"], include_in_schema=False)
    app.add_route("/oauth/register", preflight, methods=["OPTIONS"], include_in_schema=False)

    # --- authorization -----------------------------------------------------------------

    async def checked_client(fields: dict[str, str]) -> Client | Response:
        """The client, if it and its redirect URI hold; otherwise a page (never a redirect)."""
        try:
            client = await server.resolve_client(fields["client_id"])
        except OAuthError as e:
            return problem_page("This sign-in link is not valid", e.description)
        if not fields["redirect_uri"]:
            return problem_page("This sign-in link is not valid", "redirect_uri is missing.")
        if not client.allows(fields["redirect_uri"]):
            return problem_page(
                "This sign-in link is not valid", "The redirect URI is not one the app registered."
            )
        return client

    async def authorize_page(request: Request) -> Response:
        q = request.query_params
        fields = {name: q.get(name, "") for name in FORM_FIELDS}
        client = await checked_client(fields)
        if isinstance(client, Response):
            return client
        state = fields["state"]

        def refuse(error: str, description: str) -> Response:
            # A page, not a redirect: anyone can register any https redirect URI, so sending the
            # browser there before the person has chosen anything would make this an open
            # redirector (OAuth 2.1 section 7.12.2). The person may follow the link back.
            log_event(log, "oauth authorize refused", error=error, client=client_tag(client.name))
            back = _with_params(
                fields["redirect_uri"],
                {"error": error, "error_description": description, **({"state": state} if state else {}),
                 "iss": server.issuer},
            )
            return html_page(problem_content("This sign-in request is not valid", description, back), 400)

        if len(state) > MAX_STATE_LEN:
            return refuse("invalid_request", "state is too long.")
        if q.get("response_type") != "code":
            return refuse("unsupported_response_type", "Only response_type=code is offered.")
        if q.get("code_challenge_method") != "S256" or not _CHALLENGE.match(fields["code_challenge"]):
            return refuse("invalid_request", "PKCE is required: send a code_challenge with code_challenge_method=S256.")
        resources = q.getlist("resource")
        if len(set(resources)) > 1 or (resources and not server.resource_ok(resources[0])):
            return refuse("invalid_target", f"The resource must be {server.resource}.")
        log_event(log, "oauth consent shown", client=client_tag(client.name), kind=client.kind)
        return consent_page(client, fields)

    async def authorize_submit(request: Request) -> Response:
        if request.headers.get("sec-fetch-site", "").lower() in ("cross-site", "same-site"):
            return problem_page("Not allowed", "This form can only be sent from Calotype's own page.", 403)
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != server.issuer:
            return problem_page("Not allowed", "This form can only be sent from Calotype's own page.", 403)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return problem_page("This form is too large", "The form could not be read.", 413)
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)

        def get(name: str) -> str:
            return (form.get(name) or [""])[0]

        fields = {name: get(name) for name in FORM_FIELDS}
        if not server.form_token_ok(get("form_token"), fields):
            return problem_page("This page has expired", "The consent page was open too long, or was changed.")
        client = await checked_client(fields)
        if isinstance(client, Response):
            return client
        tag = client_tag(client.name)
        action = get("action")
        if action == "deny":
            log_event(log, "oauth denied", client=tag)
            return back_to_client(
                fields["redirect_uri"], {"error": "access_denied", "error_description": "The person denied access."},
                fields["state"],
            )
        store = app.state.storage
        if action == "paste":
            raw = get("key").strip()
            key = await asyncio.to_thread(store.get_key, raw) if raw and len(raw) <= 200 else None
            if key is None or key.revoked:
                return consent_page(client, fields, "That key is not valid. Check it and paste it again.", 400)
            if not key.label and not key.internal:
                await asyncio.to_thread(store.tag_source, key.id, tag)
            how = "pasted"
        elif action == "approve":
            if not settings.self_serve_keys or "free" not in settings.plans:
                return consent_page(client, fields, "New keys are switched off right now. Paste a key you already have.", 403)
            ip = ip_of(request)
            ok, _ = app.state.key_limiter.check(ip)
            if ok:
                ok, _ = app.state.key_global_limiter.check("all")
            del ip  # not kept beyond the in-memory limiter
            if not ok:
                return consent_page(
                    client, fields,
                    "Too many keys were made from your address recently. Try again later, or paste a key you already have.",
                    429,
                )
            raw, key = await asyncio.to_thread(store.create_key, "free", REF_LABEL_PREFIX + tag, internal=False)
            how = "new"
        else:
            return consent_page(client, fields, "Choose Approve, Deny or Use this key.", 400)
        request.state.key_id = key.id
        resource = fields["resource"] if server.resource_ok(fields["resource"]) else ""
        code, record = server.new_code(client, fields["redirect_uri"], fields["code_challenge"], resource, raw)
        await asyncio.to_thread(store.save_oauth_code, record)
        log_event(log, "oauth approved", client=tag, kind=client.kind, key_id=key.id, how=how)
        return back_to_client(fields["redirect_uri"], {"code": code}, fields["state"])

    app.add_route("/oauth/authorize", authorize_page, methods=["GET"], include_in_schema=False)
    app.add_route("/oauth/authorize", authorize_submit, methods=["POST"], include_in_schema=False)

    # --- token -------------------------------------------------------------------------

    def client_credentials(request: Request, form: dict[str, list[str]]) -> tuple[str, str | None, str]:
        """(client_id, secret or None, how it came: "basic", "post" or "none")."""
        form_id = (form.get("client_id") or [""])[0]
        auth = request.headers.get("authorization", "")
        if auth[:6].lower() == "basic ":
            try:
                user, _, password = base64.b64decode(auth[6:].strip()).decode("utf-8").partition(":")
            except ValueError:
                raise OAuthError("invalid_client", "The Basic credentials could not be read.", 401)
            client_id, secret = unquote_plus(user), unquote_plus(password)
            if form_id and form_id != client_id:
                raise OAuthError("invalid_request", "Two different client ids were sent.")
            return client_id, secret, "basic"
        secret = (form.get("client_secret") or [None])[0]
        return form_id, secret, "post" if secret is not None else "none"

    def authenticate_client(request: Request, form: dict[str, list[str]]) -> str:
        client_id, secret, how = client_credentials(request, form)
        if not client_id:
            raise OAuthError("invalid_client", "client_id is missing.", 401)
        if client_id.startswith("https://"):
            method = "none"  # metadata-document clients are public (checked at /oauth/authorize)
        else:
            client = server.registered_client(client_id)
            if client is None:
                raise OAuthError("invalid_client", "This client id is not known to Calotype.", 401)
            method = client.auth_method
        if method == "none":
            if secret:
                raise OAuthError("invalid_client", "This client was registered without a secret.", 401)
            return client_id
        expected = {"client_secret_basic": "basic", "client_secret_post": "post"}[method]
        if how != expected or not secret or not hmac.compare_digest(secret, server.client_secret(client_id)):
            raise OAuthError("invalid_client", "The client could not be authenticated.", 401)
        return client_id

    async def token(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return oauth_error(OAuthError("invalid_request", "The request is too large."))
        form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)

        def get(name: str) -> str:
            return (form.get(name) or [""])[0]

        try:
            if get("grant_type") != "authorization_code":
                raise OAuthError("unsupported_grant_type", "Only grant_type=authorization_code is offered.")
            client_id = authenticate_client(request, form)
            code = get("code")
            record = None
            if code:
                code_id = _sha256_hex(code)
                record = await asyncio.to_thread(app.state.storage.take_oauth_code, code_id)
            if record is None:
                raise OAuthError("invalid_grant", "The code is not valid: unknown, already used or expired.")
            if not hmac.compare_digest(record.client_hash, _sha256_hex(client_id)):
                raise OAuthError("invalid_grant", "The code was issued to another client.")
            if get("redirect_uri") and get("redirect_uri") != record.redirect_uri:
                raise OAuthError("invalid_grant", "redirect_uri does not match the one the code was issued for.")
            if not pkce_ok(get("code_verifier"), record.code_challenge):
                raise OAuthError("invalid_grant", "The code_verifier does not match the code_challenge.")
            if get("resource") and not server.resource_ok(get("resource")):
                raise OAuthError("invalid_target", f"The resource must be {server.resource}.")
            raw = server.unseal(code, record)
            key = await asyncio.to_thread(app.state.storage.get_key, raw)
            if key is None or key.revoked:
                raise OAuthError("invalid_grant", "The key behind this code is no longer valid.")
        except OAuthError as e:
            log_event(log, "oauth token refused", error=e.error)
            headers = {"WWW-Authenticate": "Basic"} if e.status == 401 and "basic" in request.headers.get(
                "authorization", "").lower() else None
            return oauth_error(e, headers)
        request.state.key_id = key.id
        log_event(log, "oauth token issued", key_id=key.id)
        # The key is the token. It does not expire on its own, so there is no
        # expires_in and no refresh token; a revoked key answers 401 at /mcp.
        return as_json({"access_token": raw, "token_type": "Bearer"}, headers={"Pragma": "no-cache"})

    app.add_route("/oauth/token", token, methods=["POST"], include_in_schema=False)
    app.add_route("/oauth/token", preflight, methods=["OPTIONS"], include_in_schema=False)


__all__ = [
    "OAuthServer",
    "OAuthError",
    "Client",
    "add_oauth_routes",
    "oauth_enabled",
    "redirect_uri_problem",
    "redirect_matches",
    "client_tag",
    "pkce_ok",
    "fetch_client_document",
]
