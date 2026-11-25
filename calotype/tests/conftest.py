from __future__ import annotations

import http.server
import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from calotype.app import create_app
from calotype.capture import CaptureResult
from calotype.config import Settings, load_plans, DEFAULT_PLANS_FILE
from calotype.params import CONTENT_TYPES
from calotype.ssrf import AddressPolicy
from calotype.storage import SQLiteStorage

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201a5f1a2d60000000049454e44ae426082"
)


async def fake_resolver(host: str, port: int):
    """Resolve without the network. Unknown names map to a public test address."""
    import ipaddress

    table = {
        "example.com": ["93.184.215.14"],
        "public.example": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
        "rebind.example": ["127.0.0.1"],
        "mixed.example": ["93.184.215.14", "10.0.0.5"],
        "metadata.example": ["169.254.169.254"],
        "v6local.example": ["fd00:ec2::254"],
        "mapped.example": ["::ffff:127.0.0.1"],
    }
    if host == "nxdomain.example":
        raise OSError("no such host")
    return [ipaddress.ip_address(a) for a in table.get(host, ["93.184.215.14"])]


@dataclass
class FakeRenderer:
    calls: list = field(default_factory=list)
    fail_with: Exception | None = None
    browser_connected: bool = True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def capture(self, params):
        self.calls.append(params)
        if self.fail_with:
            raise self.fail_with
        return CaptureResult(PNG_1X1, CONTENT_TYPES[params.format], 200, False, 0, 5)


@pytest.fixture(autouse=True)
def no_stripe_env(monkeypatch):
    """Billing is off unless a test turns it on: no Stripe variable leaks in from the shell."""
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_STARTER", "STRIPE_PRICE_PRO"):
        monkeypatch.delenv(name, raising=False)
    # The same for prepaid credit (credits.py).
    for name in (
        "CALOTYPE_CREDITS", "CALOTYPE_CREDITS_CARD", "CALOTYPE_CREDITS_STABLECOIN", "STRIPE_PROFILE_ID",
        "MPP_SECRET_KEY", "MPP_REALM", "STRIPE_MPP_API_VERSION", "STRIPE_SPT_PAYMENT_METHOD_TYPES",
        "STRIPE_TEMPO_DEPOSIT_ADDRESS", "CALOTYPE_TEMPO_RPC_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        plans=load_plans(DEFAULT_PLANS_FILE),
        serve_site=True,
    )


# Firestore tests run only against the emulator, never a real project: the
# client goes to FIRESTORE_EMULATOR_HOST when it is set, and the fixtures below
# make no client when it is not. README.md says how to start the emulator.
FIRESTORE_EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
FIRESTORE_TEST_PROJECT = "calotype-test"
needs_firestore_emulator = pytest.mark.skipif(
    not FIRESTORE_EMULATOR_HOST, reason="Firestore tests need the emulator: set FIRESTORE_EMULATOR_HOST"
)
BACKENDS = ["sqlite", pytest.param("firestore", marks=needs_firestore_emulator)]


@pytest.fixture(scope="session")
def firestore_client():
    if not FIRESTORE_EMULATOR_HOST:
        pytest.skip("Firestore tests need the emulator: set FIRESTORE_EMULATOR_HOST")
    from google.cloud import firestore

    return firestore.Client(project=FIRESTORE_TEST_PROJECT)


@pytest.fixture
def firestore_prefix() -> str:
    """A collection prefix of its own for each test, so tests never share data."""
    return f"test{uuid.uuid4().hex[:12]}_"


@pytest.fixture
def firestore_store(firestore_client, firestore_prefix):
    from calotype.firestore_storage import FirestoreStorage

    s = FirestoreStorage(client=firestore_client, collection_prefix=firestore_prefix)
    s.init()
    yield s
    for collection in s.collections():
        firestore_client.recursive_delete(collection)


@pytest.fixture
def sqlite_store(settings) -> SQLiteStorage:
    s = SQLiteStorage(settings.database_url[len("sqlite:///") :])
    s.init()
    yield s
    s.close()


@pytest.fixture(params=BACKENDS)
def store(request):
    """Each test that uses the store runs once per storage backend."""
    return request.getfixturevalue(f"{request.param}_store")


def stored_text(store, *, funnel: bool = True) -> str:
    """Everything the store has written, as one string, for privacy checks.

    ``funnel=False`` leaves out the funnel's daily counts, which funnel.py writes on its own
    thread after a response: a test comparing the store before and after a request waits for
    them (``app.state.funnel.wait()``) and checks them apart.
    """
    if isinstance(store, SQLiteStorage):
        db = sqlite3.connect(store.path)
        try:
            return "\n".join(line for line in db.iterdump() if funnel or not line.startswith('INSERT INTO "funnel"'))
        finally:
            db.close()
    docs = [{"path": d.reference.path, **d.to_dict()} for c in store.collections()
            if funnel or not c.id.endswith("funnel") for d in c.stream()]
    return json.dumps(docs, default=str)


def stored_key_fields(store) -> set[str]:
    """The fields stored for an API key (SQLite columns, Firestore document fields)."""
    if isinstance(store, SQLiteStorage):
        db = sqlite3.connect(store.path)
        try:
            return {row[1] for row in db.execute("PRAGMA table_info(api_keys)")}
        finally:
            db.close()
    fields: set[str] = set()
    for d in store.collections()[0].stream():
        fields |= set(d.to_dict())
    return fields


@pytest.fixture
def fake_renderer() -> FakeRenderer:
    return FakeRenderer()


@pytest.fixture
def client(settings, store, fake_renderer):
    app = create_app(settings, policy=AddressPolicy(resolver=fake_resolver), storage=store, renderer=fake_renderer)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def free_key(store) -> str:
    raw, _ = store.create_key("free", "test")
    return raw


class LocalServer:
    """A threaded HTTP server on 127.0.0.1 that records each path it serves."""

    def __init__(self, routes: dict[str, Callable[["LocalServer"], tuple[int, dict, bytes]]] | None = None):
        self.hits: list[str] = []
        self.routes = routes or {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits.append(self.path)
                route = outer.routes.get(self.path.split("?")[0])
                if route is None:
                    status, headers, body = 200, {"Content-Type": "text/plain"}, b"canary"
                else:
                    status, headers, body = route(outer)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
