"""The admin CLI and the MCP tool."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from calotype import admin, mcp_server
from calotype.storage import SQLiteStorage
from mcp.server.mcpserver.exceptions import ToolError

from .conftest import PNG_1X1


def test_create_key_prints_once_and_stores_hash(tmp_path, monkeypatch, capsys):
    db = tmp_path / "admin.db"
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(db))
    assert admin.main(["create-key", "--plan", "free", "--label", "first"]) == 0
    out = capsys.readouterr()
    raw = out.out.strip()
    assert raw.startswith("cal_") and len(raw) > 40
    assert "only time" in out.err
    assert raw.encode() not in db.read_bytes()
    store = SQLiteStorage(db)
    assert store.get_key(raw).plan == "free"

    assert admin.main(["list-keys"]) == 0
    listing = capsys.readouterr().out
    assert raw not in listing and "first" in listing


def test_create_key_unknown_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("CALOTYPE_DB_PATH", str(tmp_path / "a.db"))
    assert admin.main(["create-key", "--plan", "enterprise"]) == 2


def test_mcp_tool_calls_api(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, content=PNG_1X1, headers={"content-type": "image/png"})

    monkeypatch.setenv("CALOTYPE_API_KEY", "cal_test")
    monkeypatch.setenv("CALOTYPE_API_URL", "https://api.test")
    monkeypatch.setattr(mcp_server, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    img = asyncio.run(mcp_server.screenshot("https://example.com", format="png"))
    assert img.data == PNG_1X1
    assert seen["auth"] == "Bearer cal_test"
    assert seen["url"] == "https://api.test/v1/screenshot"
    assert b'"url":"https://example.com"' in seen["body"].replace(b" ", b"")


def test_mcp_tool_reports_api_error(monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": {"code": "quota_exceeded", "message": "used up"}})

    monkeypatch.setenv("CALOTYPE_API_KEY", "cal_test")
    monkeypatch.setattr(mcp_server, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="quota_exceeded"):
        asyncio.run(mcp_server.screenshot("https://example.com"))


def test_mcp_tool_needs_key(monkeypatch):
    monkeypatch.delenv("CALOTYPE_API_KEY", raising=False)
    with pytest.raises(ToolError, match="CALOTYPE_API_KEY"):
        asyncio.run(mcp_server.screenshot("https://example.com"))


def test_mcp_server_lists_tool():
    tools = asyncio.run(mcp_server.server.list_tools())
    names = [t.name for t in tools]
    assert names == ["screenshot"]
