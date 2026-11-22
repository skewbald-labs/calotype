"""OAuth sign-in for /mcp (oauth.py): discovery, registration, consent, PKCE, codes, tokens, and /mcp's 401s.

Each test that uses the ``store`` runs once per storage backend (Firestore
only with the emulator), like the rest of the suite.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import secrets
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi.testclient import TestClient

from calotype import oauth as oauth_module
from calotype.app import create_app
from calotype.oauth import OAuthServer, client_tag, redirect_matches, redirect_uri_problem
from calotype.ssrf import AddressPolicy

from .conftest import fake_resolver, stored_text

SECRET = "test-oauth-secret-" + "x" * 32
ISSUER = "https://calotype.dev"
CB = "https://claude.ai/api/mcp/auth_callback"
ACCEPT = "application/json, text/event-stream"
WELL_KNOWN = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
)


@pytest.fixture
def oauth_settings(settings):
    return replace(settings, oauth_secret=SECRET)


def make_client(settings, store, renderer, **changes):
    app = create_app(
        replace(settings, **changes), policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=renderer
    )
    return TestClient(app)


@pytest.fixture
def oc(oauth_settings, store, fake_renderer):
    with make_client(oauth_settings, store, fake_renderer) as c:
        yield c


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(c, **body) -> dict:
    r = c.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": [CB], **body})
    assert r.status_code == 201, r.text
    return r.json()


def authorize_query(client_id: str, challenge: str, redirect_uri: str = CB, **over) -> dict:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-123",
        "resource": f"{ISSUER}/mcp",
    }
    q.update(over)
    return {k: v for k, v in q.items() if v is not None}


def hidden_fields(page: str) -> dict:
    return {
        m.group(1): html.unescape(m.group(2))
        for m in re.finditer(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page)
    }


def submit(c, fields: dict, action: str, key: str | None = None, **headers):
    data = {**fields, "action": action}
    if key is not None:
        data["key"] = key
    return c.post(
        "/oauth/authorize",
        content=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
        follow_redirects=False,
    )


def redirect_params(r) -> tuple[str, dict]:
    assert r.status_code == 303, r.text
    location = r.headers["location"]
    parts = urlsplit(location)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", {k: v[0] for k, v in parse_qs(parts.query).items()}


def consent(c, client_id: str, challenge: str, **over):
    r = c.get("/oauth/authorize", params=authorize_query(client_id, challenge, **over), follow_redirects=False)
    assert r.status_code == 200, r.text
    return r


def get_code(c, client_id: str, challenge: str, action="approve", key=None, **over) -> str:
    page = consent(c, client_id, challenge, **over)
    where, params = redirect_params(submit(c, hidden_fields(page.text), action, key))
    assert params["state"] == "st-123" and params["iss"] == ISSUER
    return params["code"]


def exchange(c, client_id: str, code: str, verifier: str, redirect_uri: str = CB, **extra):
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": f"{ISSUER}/mcp",
        **extra,
    }
    return c.post("/oauth/token", data={k: v for k, v in data.items() if v is not None})


def full_flow(c, **reg) -> tuple[dict, str]:
    client = register(c, **reg)
    verifier, challenge = pkce()
    code = get_code(c, client["client_id"], challenge)
    r = exchange(c, client["client_id"], code, verifier)
    assert r.status_code == 200, r.text
    return client, r.json()["access_token"]


def rpc(c, key: str | None, method: str, params: dict | None = None, path: str = "/mcp"):
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": 7, "method": method, **({"params": params} if params is not None else {})}
    return c.post(path, json=body, headers=headers)


# --- off without the secret ------------------------------------------------------------------


def test_without_the_secret_oauth_is_off_and_discovery_is_404(client, free_key):
    for path in WELL_KNOWN:
        assert client.get(path).status_code == 404, path
    assert client.post("/oauth/register", json={"redirect_uris": [CB]}).status_code in (404, 405)
    assert client.get("/oauth/authorize").status_code == 404
    # /mcp behaves as before: a keyless call is a tool error, a wrong key a plain Bearer 401.
    r = rpc(client, None, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}})
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    r = rpc(client, "cal_wrong", "tools/list", {})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_a_short_secret_does_not_turn_oauth_on(settings, store, fake_renderer):
    with make_client(settings, store, fake_renderer, oauth_secret="too-short") as c:
        assert c.get("/.well-known/oauth-protected-resource").status_code == 404
    with pytest.raises(ValueError):
        OAuthServer(secret="short", issuer=ISSUER, policy=AddressPolicy())


# --- discovery -------------------------------------------------------------------------------


def test_protected_resource_metadata_at_both_paths(oc):
    for path in WELL_KNOWN[:2]:
        r = oc.get(path)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
        body = r.json()
        assert body["resource"] == f"{ISSUER}/mcp"
        assert body["authorization_servers"] == [ISSUER]
        assert body["bearer_methods_supported"] == ["header"]
        assert "scopes_supported" not in body  # no scopes: the key is the whole grant


def test_protected_resource_metadata_keeps_a_ref_tag(oc):
    assert oc.get(WELL_KNOWN[1], params={"ref": "glama"}).json()["resource"] == f"{ISSUER}/mcp?ref=glama"
    assert oc.get(WELL_KNOWN[1], params={"ref": "Not A Tag!"}).json()["resource"] == f"{ISSUER}/mcp"


def test_authorization_server_metadata(oc):
    for path in WELL_KNOWN[2:]:
        body = oc.get(path).json()
        assert body["issuer"] == ISSUER
        assert body["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
        assert body["token_endpoint"] == f"{ISSUER}/oauth/token"
        assert body["registration_endpoint"] == f"{ISSUER}/oauth/register"
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["response_types_supported"] == ["code"]
        assert body["grant_types_supported"] == ["authorization_code"]
        # Claude uses a Client ID Metadata Document only with both of these.
        assert body["client_id_metadata_document_supported"] is True
        assert "none" in body["token_endpoint_auth_methods_supported"]
        assert body["authorization_response_iss_parameter_supported"] is True
    r = oc.options("/oauth/token")
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"


# --- registration ----------------------------------------------------------------------------


def test_registration_is_stateless_and_echoes_the_client(oc, store):
    before = stored_text(store)
    body = register(oc, grant_types=["authorization_code", "refresh_token"], response_types=["code"])
    assert body["client_id"].startswith("c1.") and body["client_name"] == "Claude"
    assert body["redirect_uris"] == [CB]
    assert body["token_endpoint_auth_method"] == "none" and "client_secret" not in body
    assert body["grant_types"] == ["authorization_code"]
    assert stored_text(store) == before  # nothing was written


def test_registration_with_a_secret(oc):
    body = register(oc, token_endpoint_auth_method="client_secret_post")
    assert body["client_secret"] and body["client_secret_expires_at"] == 0


@pytest.mark.parametrize(
    "uris",
    [
        ["http://example.com/cb"],  # http only for loopback
        ["https://example.com/cb#frag"],
        ["cursor://anysphere.cursor-mcp/oauth/callback"],
        ["javascript:alert(1)"],
        ["https://user:pw@example.com/cb"],
        ["https://exa mple.com/cb"],
        [],
        ["https://example.com/cb"] * 11,
        "https://example.com/cb",
        [42],
    ],
)
def test_registration_refuses_bad_redirect_uris(oc, uris):
    r = oc.post("/oauth/register", json={"client_name": "x", "redirect_uris": uris})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize(
    "body",
    [
        {"redirect_uris": [CB], "token_endpoint_auth_method": "private_key_jwt"},
        {"redirect_uris": [CB], "grant_types": ["client_credentials"]},
        {"redirect_uris": [CB], "response_types": ["token"]},
        {"redirect_uris": [CB], "client_name": 5},
    ],
)
def test_registration_refuses_bad_metadata(oc, body):
    r = oc.post("/oauth/register", json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_client_metadata"
    assert oc.post("/oauth/register", content=b"not json").json()["error"] == "invalid_client_metadata"


def test_loopback_redirects_are_accepted_on_any_port():
    for uri in ("http://127.0.0.1/callback", "http://localhost:3118/callback", "http://[::1]:9/cb"):
        assert redirect_uri_problem(uri) is None, uri
    assert redirect_matches("http://localhost:3118/callback", "http://localhost/callback")
    assert redirect_matches("http://127.0.0.1:5/cb", "http://127.0.0.1:6/cb")
    assert not redirect_matches("http://127.0.0.1:5/cb", "http://localhost:5/cb")
    assert not redirect_matches("http://127.0.0.1:5/other", "http://127.0.0.1/cb")
    assert not redirect_matches("https://claude.ai:444/api/mcp/auth_callback", CB)  # exact for https


def test_registration_is_rate_limited_per_ip(oauth_settings, store, fake_renderer):
    with make_client(oauth_settings, store, fake_renderer, oauth_registrations_per_ip_per_hour=2) as c:
        register(c)
        register(c)
        r = c.post("/oauth/register", json={"redirect_uris": [CB]})
        assert r.status_code == 429 and r.headers["retry-after"]


def test_client_tag_is_a_short_safe_ref():
    assert client_tag("Claude") == "oauth-claude"
    assert client_tag("ChatGPT") == "oauth-chatgpt"
    assert client_tag("  My <Weird> App!! v2 ") == "oauth-my-weird-app-v2"
    assert client_tag("") == client_tag("日本語") == "oauth"
    assert len(client_tag("a" * 200)) <= 40


# --- the consent page ------------------------------------------------------------------------


def test_consent_page_names_the_client_and_the_redirect_host(oc, store):
    client = register(oc, client_name="Claude <b>")
    _, challenge = pkce()
    r = consent(oc, client["client_id"], challenge)
    page = r.text
    assert "Claude &lt;b&gt;" in page and "<b>" not in page  # escaped
    assert "<code>claude.ai</code>" in page
    assert "free Calotype key (100 screenshots a month, no account, no email)" in page
    assert "Already have a key?" in page and 'value="deny"' in page and 'name="key"' in page
    assert "Calotype has not checked it" in page  # a registered name is self-asserted
    assert "AI agent" in page  # the shared layout's disclosure
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' https://claude.ai;" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers


def test_a_loopback_client_gets_a_warning_and_its_port_in_form_action(oc):
    client = register(oc, client_name="Claude Code", redirect_uris=["http://localhost/callback"])
    _, challenge = pkce()
    r = consent(oc, client["client_id"], challenge, redirect_uri="http://localhost:3118/callback")
    assert "runs on your own computer" in r.text
    assert "form-action 'self' http://localhost:3118;" in r.headers["content-security-policy"]


def test_a_bad_client_or_redirect_gets_a_page_not_a_redirect(oc):
    client = register(oc)
    _, challenge = pkce()
    tampered = client["client_id"][:-3] + ("AAA" if not client["client_id"].endswith("AAA") else "BBB")
    cases = [
        authorize_query(tampered, challenge),
        authorize_query("unknown-client", challenge),
        authorize_query(client["client_id"], challenge, redirect_uri="https://evil.example/cb"),
        authorize_query(client["client_id"], challenge, redirect_uri=None),
    ]
    for q in cases:
        r = oc.get("/oauth/authorize", params=q, follow_redirects=False)
        assert r.status_code == 400 and "location" not in r.headers
        assert "not valid" in r.text


@pytest.mark.parametrize(
    "over, error",
    [
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"resource": "https://other.example/mcp"}, "invalid_target"),
        ({"state": "s" * 1001}, "invalid_request"),
    ],
)
def test_other_authorize_errors_go_back_to_the_client(oc, over, error):
    client = register(oc)
    _, challenge = pkce()
    q = authorize_query(client["client_id"], challenge, **over)
    where, params = redirect_params(oc.get("/oauth/authorize", params=q, follow_redirects=False))
    assert where == CB and params["error"] == error and params["iss"] == ISSUER


def test_resource_forms_that_name_this_server_are_accepted(oc):
    client = register(oc)
    _, challenge = pkce()
    for resource in (ISSUER, f"{ISSUER}/", f"{ISSUER}/mcp", "HTTPS://CALOTYPE.DEV/mcp", f"{ISSUER}/mcp?ref=glama"):
        consent(oc, client["client_id"], challenge, resource=resource)
    consent(oc, client["client_id"], challenge, resource=None)  # clients must send it; tolerated when not


# --- approve, token, and the key -------------------------------------------------------------


def test_approve_makes_a_free_key_that_works_on_mcp(oc, store, fake_renderer):
    _, token = full_flow(oc)
    key = store.get_key(token)
    assert key.plan == "free" and key.label == "ref:oauth-claude" and not key.internal
    assert rpc(oc, token, "tools/list", {}).json()["result"]["tools"][0]["name"] == "screenshot"
    r = rpc(oc, token, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}})
    result = r.json()["result"]
    assert result["isError"] is False and result["content"][0]["type"] == "image"
    assert len(fake_renderer.calls) == 1


def test_token_response_shape(oc):
    client = register(oc)
    verifier, challenge = pkce()
    r = exchange(oc, client["client_id"], get_code(oc, client["client_id"], challenge), verifier)
    body = r.json()
    assert set(body) == {"access_token", "token_type"} and body["token_type"] == "Bearer"
    assert body["access_token"].startswith("cal_")
    assert r.headers["cache-control"] == "no-store"


def test_a_wrong_verifier_is_refused_and_spends_the_code(oc):
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge)
    other, _ = pkce()
    r = exchange(oc, client["client_id"], code, other)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    assert exchange(oc, client["client_id"], code, verifier).json()["error"] == "invalid_grant"


@pytest.mark.parametrize("verifier", ["", "short", "x" * 129, "has spaces" * 10])
def test_malformed_verifiers_are_refused(oc, verifier):
    client = register(oc)
    _, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge)
    assert exchange(oc, client["client_id"], code, verifier).json()["error"] == "invalid_grant"


def test_a_code_works_once(oc):
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge)
    assert exchange(oc, client["client_id"], code, verifier).status_code == 200
    r = exchange(oc, client["client_id"], code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_an_expired_code_is_refused(oc, monkeypatch):
    monkeypatch.setattr(oauth_module, "CODE_TTL_S", -5)
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge)
    assert exchange(oc, client["client_id"], code, verifier).json()["error"] == "invalid_grant"


def test_a_code_is_bound_to_its_client_and_redirect(oc):
    a, b = register(oc), register(oc, client_name="Other")
    verifier, challenge = pkce()
    code = get_code(oc, a["client_id"], challenge)
    assert exchange(oc, b["client_id"], code, verifier).json()["error"] == "invalid_grant"
    code = get_code(oc, a["client_id"], challenge)
    assert exchange(oc, a["client_id"], code, verifier, redirect_uri=CB + "x").json()["error"] == "invalid_grant"
    code = get_code(oc, a["client_id"], challenge)
    r = exchange(oc, a["client_id"], code, verifier, resource="https://other.example/mcp")
    assert r.json()["error"] == "invalid_target"


def test_token_endpoint_errors(oc):
    client = register(oc)
    r = oc.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": client["client_id"]})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
    r = oc.post("/oauth/token", data={"grant_type": "authorization_code", "code": "nope", "code_verifier": "v" * 43})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = exchange(oc, client["client_id"], "not-a-code", "v" * 43)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = exchange(oc, "c1.forged.AAAA", "x", "v" * 43)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_confidential_clients_must_authenticate(oc):
    for method in ("client_secret_post", "client_secret_basic"):
        client = register(oc, token_endpoint_auth_method=method)
        cid, secret = client["client_id"], client["client_secret"]
        verifier, challenge = pkce()
        # No secret, or a wrong one: refused, and the code is kept for the right attempt.
        code = get_code(oc, cid, challenge)
        assert exchange(oc, cid, code, verifier).status_code == 401
        assert exchange(oc, cid, code, verifier, client_secret="wrong").status_code == 401
        if method == "client_secret_post":
            r = exchange(oc, cid, code, verifier, client_secret=secret)
        else:
            basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
            r = oc.post(
                "/oauth/token",
                data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": CB},
                headers={"Authorization": f"Basic {basic}"},
            )
        assert r.status_code == 200, r.text


def test_a_revoked_key_behind_a_code_is_refused(oc, store, free_key):
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge, action="paste", key=free_key)
    store.revoke_key(store.get_key(free_key).id)
    assert exchange(oc, client["client_id"], code, verifier).json()["error"] == "invalid_grant"


def test_the_stored_code_holds_no_usable_key(oc, store, free_key):
    client = register(oc)
    _, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge, action="paste", key=free_key)
    dump = stored_text(store)
    assert "oauth_codes" in dump
    assert free_key not in dump and free_key[4:] not in dump and code not in dump


# --- deny, paste, limits, CSRF ---------------------------------------------------------------


def test_deny_returns_access_denied(oc, store):
    client = register(oc)
    _, challenge = pkce()
    page = consent(oc, client["client_id"], challenge)
    keys_before = len(store.list_keys())
    where, params = redirect_params(submit(oc, hidden_fields(page.text), "deny"))
    assert where == CB and params["error"] == "access_denied" and params["state"] == "st-123"
    assert "code" not in params and len(store.list_keys()) == keys_before


def test_paste_a_valid_key(oc, store):
    raw, key = store.create_key("starter", "")
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge, action="paste", key=raw)
    assert exchange(oc, client["client_id"], code, verifier).json()["access_token"] == raw
    assert store.get_key(raw).label == "ref:oauth-claude"  # an untagged key takes the client's tag
    labelled, _ = store.create_key("free", "ref:dt-linkedin")
    get_code(oc, client["client_id"], challenge, action="paste", key=labelled)
    assert store.get_key(labelled).label == "ref:dt-linkedin"  # never overwritten


@pytest.mark.parametrize("which", ["unknown", "revoked", "empty", "huge"])
def test_paste_an_invalid_key(oc, store, which):
    raw, key = store.create_key("free", "")
    if which == "revoked":
        store.revoke_key(key.id)
    value = {"unknown": "cal_nope", "revoked": raw, "empty": "", "huge": "cal_" + "x" * 300}[which]
    client = register(oc)
    _, challenge = pkce()
    page = consent(oc, client["client_id"], challenge)
    r = submit(oc, hidden_fields(page.text), "paste", value)
    assert r.status_code == 400 and "That key is not valid" in r.text and "location" not in r.headers
    assert 'name="form_token"' in r.text  # the page is shown again, ready for another try


def test_approve_obeys_the_key_creation_limit(oauth_settings, store, fake_renderer):
    with make_client(oauth_settings, store, fake_renderer, keys_per_ip_per_hour=1) as c:
        client = register(c)
        _, challenge = pkce()
        get_code(c, client["client_id"], challenge)
        page = consent(c, client["client_id"], challenge)
        r = submit(c, hidden_fields(page.text), "approve")
        assert r.status_code == 429 and "Too many keys" in r.text
        # It is the same limit as POST /v1/keys.
        assert c.post("/v1/keys").status_code == 429
        # Pasting a key still works.
        raw, _ = store.create_key("free", "")
        assert submit(c, hidden_fields(page.text), "paste", raw).status_code == 303


def test_approve_respects_self_serve_off(oauth_settings, store, fake_renderer):
    with make_client(oauth_settings, store, fake_renderer, self_serve_keys=False) as c:
        client = register(c)
        _, challenge = pkce()
        r = submit(c, hidden_fields(consent(c, client["client_id"], challenge).text), "approve")
        assert r.status_code == 403 and "switched off" in r.text


def test_the_form_is_protected_against_csrf(oc):
    client = register(oc)
    _, challenge = pkce()
    fields = hidden_fields(consent(oc, client["client_id"], challenge).text)
    assert submit(oc, fields, "approve", **{"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert submit(oc, fields, "approve", Origin="https://evil.example").status_code == 403
    # A changed field breaks the form token.
    for name, value in (("redirect_uri", "https://evil.example/cb"), ("state", "other"), ("code_challenge", "y" * 43)):
        r = submit(oc, {**fields, name: value}, "approve")
        assert r.status_code == 400 and "expired" in r.text and "location" not in r.headers
    assert submit(oc, {**fields, "form_token": ""}, "approve").status_code == 400
    assert submit(oc, fields, "approve", Origin=ISSUER).status_code == 303


def test_an_old_form_token_is_refused():
    server = OAuthServer(secret=SECRET, issuer=ISSUER, policy=AddressPolicy())
    fields = {"client_id": "c", "redirect_uri": CB, "code_challenge": "x", "state": "", "resource": ""}
    token = server.form_token(fields, now=1_000_000)
    assert server.form_token_ok(token, fields, now=1_000_000 + 60)
    assert not server.form_token_ok(token, fields, now=1_000_000 + oauth_module.FORM_TTL_S + 1)
    other = OAuthServer(secret=SECRET + "2", issuer=ISSUER, policy=AddressPolicy())
    assert not other.form_token_ok(token, fields, now=1_000_000 + 60)


def test_a_client_id_from_another_secret_is_refused():
    a = OAuthServer(secret=SECRET, issuer=ISSUER, policy=AddressPolicy())
    b = OAuthServer(secret=SECRET + "2", issuer=ISSUER, policy=AddressPolicy())
    cid = a.register({"redirect_uris": [CB]})["client_id"]
    assert a.registered_client(cid) is not None and b.registered_client(cid) is None


# --- Client ID Metadata Documents ------------------------------------------------------------

DOC_URL = "https://claude.ai/oauth/claude-code-client-metadata"


def use_documents(c, docs: dict):
    calls = []

    async def fetch(url, policy):
        calls.append(url)
        if isinstance(docs.get(url), Exception):
            raise docs[url]
        return docs[url]

    c.app.state.oauth.fetch_document = fetch
    return calls


def test_a_metadata_document_client_signs_in(oc, store):
    calls = use_documents(oc, {DOC_URL: {
        "client_id": DOC_URL, "client_name": "Claude Code",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "token_endpoint_auth_method": "none",
    }})
    verifier, challenge = pkce()
    page = consent(oc, DOC_URL, challenge, redirect_uri="http://localhost:4123/callback")
    assert "identified by <code>claude.ai</code>" in page.text and "has not checked" not in page.text
    _, params = redirect_params(submit(oc, hidden_fields(page.text), "approve"))
    r = exchange(oc, DOC_URL, params["code"], verifier, redirect_uri="http://localhost:4123/callback")
    assert r.status_code == 200
    assert store.get_key(r.json()["access_token"]).label == "ref:oauth-claude-code"
    assert calls == [DOC_URL]  # cached for the second look at submit time


@pytest.mark.parametrize(
    "doc",
    [
        {"client_id": "https://other.example/client.json", "redirect_uris": [CB]},
        {"client_id": DOC_URL, "redirect_uris": ["cursor://x/cb"]},
        {"client_id": DOC_URL, "redirect_uris": [CB], "token_endpoint_auth_method": "private_key_jwt"},
        ["not", "an", "object"],
        ValueError("HTTP 404"),
    ],
)
def test_a_bad_metadata_document_is_refused(oc, doc):
    use_documents(oc, {DOC_URL: doc})
    _, challenge = pkce()
    r = oc.get("/oauth/authorize", params=authorize_query(DOC_URL, challenge), follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


@pytest.mark.parametrize(
    "url",
    ["https://claude.ai/", "https://claude.ai", "https://claude.ai/a/../b", "https://u@claude.ai/x", "https://claude.ai/x#f"],
)
def test_metadata_document_urls_need_a_clean_path(oc, url):
    use_documents(oc, {})
    _, challenge = pkce()
    assert oc.get("/oauth/authorize", params=authorize_query(url, challenge)).status_code == 400


def test_the_document_fetch_never_reaches_a_private_address():
    import asyncio

    from calotype.ssrf import BlockedTarget

    policy = AddressPolicy(resolver=fake_resolver)
    for url in ("https://rebind.example/client.json", "https://127.0.0.1/client.json", "https://metadata.example/c"):
        with pytest.raises(BlockedTarget):
            asyncio.run(oauth_module.fetch_client_document(url, policy))


def test_a_private_metadata_url_is_an_invalid_client(oc):
    _, challenge = pkce()
    r = oc.get("/oauth/authorize", params=authorize_query("https://rebind.example/client.json", challenge))
    assert r.status_code == 400 and "does not fetch" in r.text


# --- /mcp: the 401s, and what still works without a key --------------------------------------


def test_keyless_discovery_methods_still_work(oc):
    params = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "glama", "version": "1"}}
    assert rpc(oc, None, "initialize", params).json()["result"]["serverInfo"]["name"] == "calotype"
    assert [t["name"] for t in rpc(oc, None, "tools/list", {}).json()["result"]["tools"]] == ["screenshot"]
    assert rpc(oc, None, "ping").json()["result"] == {}


def test_a_keyless_tool_call_answers_401_with_the_challenge(oc, fake_renderer):
    r = rpc(oc, None, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
    )
    body = r.json()
    assert body["id"] == 7 and body["error"]["data"]["code"] == "missing_key"
    assert "/v1/keys" in body["error"]["message"] and "OAuth" in body["error"]["message"]
    assert fake_renderer.calls == []


def test_the_challenge_keeps_the_ref_tag(oc):
    r = rpc(oc, None, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}},
            path="/mcp?ref=glama")
    assert 'oauth-protected-resource/mcp?ref=glama"' in r.headers["www-authenticate"]


def test_a_wrong_key_answers_401_with_invalid_token(oc):
    r = rpc(oc, "cal_wrong", "tools/list", {})
    assert r.status_code == 401
    header = r.headers["www-authenticate"]
    assert header.startswith(f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"')
    assert 'error="invalid_token"' in header


def test_keyless_call_mode_result_keeps_the_tool_error(oauth_settings, store, fake_renderer):
    with make_client(oauth_settings, store, fake_renderer, mcp_keyless_call="result") as c:
        r = rpc(c, None, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}})
        assert r.status_code == 200 and r.json()["result"]["isError"] is True
        assert "OAuth" in r.json()["result"]["content"][0]["text"]
        # Every 401 still carries the challenge.
        assert "resource_metadata=" in rpc(c, "cal_wrong", "tools/list", {}).headers["www-authenticate"]


def test_a_keyed_tool_call_is_unchanged(oc, free_key, fake_renderer):
    r = rpc(oc, free_key, "tools/call", {"name": "screenshot", "arguments": {"url": "https://example.com"}})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False


def test_oauth_logs_carry_no_key_or_code(oc, caplog):
    import logging

    caplog.set_level(logging.INFO)
    client = register(oc)
    verifier, challenge = pkce()
    code = get_code(oc, client["client_id"], challenge)
    token = exchange(oc, client["client_id"], code, verifier).json()["access_token"]
    text = caplog.text
    assert "oauth approved" in text and "oauth token issued" in text
    assert token not in text and code not in text and verifier not in text and client["client_id"] not in text
