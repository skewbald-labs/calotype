"""The default urllib transport, against a server on the loopback address, and the live smoke test."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from calotype import APIConnectionError, AuthenticationError, Calotype, InvalidRequestError


class Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def _answer(self, status: int, content_type: str, body: bytes, **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        Handler.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if body.get("url") == "bad":
            error = {"error": {"code": "invalid_url", "message": "The URL is not valid."}}
            self._answer(400, "application/json", json.dumps(error).encode())
        else:
            self._answer(200, "image/jpeg", b"\xff\xd8 jpeg", **{"X-Calotype-Usage": "1/100"})

    def log_message(self, *args) -> None:  # keep the test output quiet
        pass


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    Handler.seen.clear()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_urllib_transport_round_trip(server):
    client = Calotype("cal_test", base_url=server, timeout=5)

    shot = client.capture("https://example.com", format="jpeg")
    assert shot.image == b"\xff\xd8 jpeg"
    assert (shot.content_type, shot.used, shot.limit) == ("image/jpeg", 1, 100)
    seen = Handler.seen[-1]
    assert seen["path"] == "/v1/screenshot"
    assert seen["body"] == {"url": "https://example.com", "format": "jpeg"}
    assert seen["headers"]["Authorization"] == "Bearer cal_test"
    assert seen["headers"]["User-Agent"].startswith("calotype-python/")

    # An error status is an answer, not a connection failure.
    with pytest.raises(InvalidRequestError) as caught:
        client.screenshot("bad")
    assert (caught.value.code, caught.value.status) == ("invalid_url", 400)


def test_urllib_transport_reports_an_unreachable_server():
    # Bind a port, then close it, so connections to it are refused.
    closed = HTTPServer(("127.0.0.1", 0), Handler)
    port = closed.server_port
    closed.server_close()
    client = Calotype("cal_secret_value", base_url=f"http://127.0.0.1:{port}", timeout=5)
    with pytest.raises(APIConnectionError) as caught:
        client.usage()
    assert "Could not reach the Calotype API" in str(caught.value)
    assert "cal_secret_value" not in str(caught.value)


@pytest.mark.skipif(os.environ.get("CALOTYPE_LIVE") != "1", reason="set CALOTYPE_LIVE=1 to call https://calotype.dev")
def test_live_placeholder_key_is_refused():
    """The one test that uses the network: a keyless call that creates no key and takes no screenshot."""
    with pytest.raises(AuthenticationError) as caught:
        Calotype("cal_placeholder").usage()
    assert (caught.value.code, caught.value.status) == ("invalid_key", 401)
