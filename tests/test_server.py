import json
import subprocess

import pytest
from starlette.testclient import TestClient

from app import server, wacli
from app.config import Settings
from app.wacli import Supervisor, WacliError, run_json

CFG = Settings(allowed_hosts=["testserver"], auth_token="")


# ---- run_json ----------------------------------------------------------------
def _fake_run(stdout="", stderr="", code=0):
    def run(cmd, **kwargs):
        run.cmd = cmd
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)

    return run


def test_run_json_returns_data_and_forces_read_only(monkeypatch):
    fake = _fake_run(stdout=json.dumps({"success": True, "data": [{"jid": "1@s.whatsapp.net"}]}))
    monkeypatch.setattr(wacli.subprocess, "run", fake)
    assert run_json(CFG, "chats", "list") == [{"jid": "1@s.whatsapp.net"}]
    assert "--read-only" in fake.cmd and "--json" in fake.cmd


def test_run_json_raises_json_error_from_stderr(monkeypatch):
    err = json.dumps({"success": False, "data": None, "error": "store is locked"})
    monkeypatch.setattr(wacli.subprocess, "run", _fake_run(stderr=err, code=1))
    with pytest.raises(WacliError, match="store is locked"):
        run_json(CFG, "chats", "list")


def test_run_json_raises_plain_stderr(monkeypatch):
    monkeypatch.setattr(wacli.subprocess, "run", _fake_run(stderr="boom", code=2))
    with pytest.raises(WacliError, match="boom"):
        run_json(CFG, "doctor")


# ---- supervisor state machine ------------------------------------------------------
def test_supervisor_event_flow(monkeypatch):
    sup = Supervisor(CFG)
    sup.handle_event("qr_code", {"code": "2@abc"})
    assert sup.snapshot()["state"] == "waiting_qr"
    assert sup.snapshot()["qr_code"] == "2@abc"

    monkeypatch.setattr(
        wacli, "run_json", lambda *a: {"authenticated": True, "phone": "5511999", "linked_jid": "5511999@s.whatsapp.net"}
    )
    sup.handle_event("connected", {})
    snap = sup.snapshot()
    assert snap["state"] == "connected" and snap["qr_code"] is None and snap["phone"] == "5511999"

    sup.handle_event("logged_out", {})
    assert sup.snapshot()["state"] == "logged_out"
    assert sup.snapshot()["authenticated"] is False


def test_supervisor_commands():
    sup = Supervisor(Settings(pair_phone="5511999998888"))
    assert "sync" in sup._command(True) and "--follow" in sup._command(True)
    unpaired = sup._command(False)
    assert unpaired[unpaired.index("auth") :] == ["auth", "--follow", "--phone", "5511999998888"]


# ---- helpers ----------------------------------------------------------------------
def test_slim_message_drops_sensitive_fields():
    slim = server._slim_message(
        {"ChatJID": "c", "MsgID": "1", "Text": "oi", "DisplayText": "", "DirectPath": "/secret", "LocalPath": "/x"}
    )
    assert slim == {"chat_jid": "c", "id": "1", "text": "oi"}


def test_resolve_chat(monkeypatch):
    chats = [{"jid": "1@g.us", "name": "Familia"}, {"jid": "2@g.us", "name": "Familia Silva"}]
    monkeypatch.setattr(server, "run_json", lambda *a: chats)
    assert server._resolve_chat("5511999998888") == "5511999998888"
    assert server._resolve_chat("123@s.whatsapp.net") == "123@s.whatsapp.net"
    assert server._resolve_chat("familia") == "1@g.us"  # exact (case-insensitive) match wins
    monkeypatch.setattr(server, "run_json", lambda *a: chats[1:] + [{"jid": "3@g.us", "name": "Silva Jr"}])
    with pytest.raises(WacliError, match="ambiguous"):
        server._resolve_chat("silva")


def test_ensure_token_generates_persists_and_respects_env(tmp_path):
    token_file = tmp_path / "data" / "auth_token"
    first = server.ensure_token(Settings(auth_token="", token_file=str(token_file)))
    assert len(first.auth_token) >= 32 and token_file.read_text().strip() == first.auth_token
    again = server.ensure_token(Settings(auth_token="", token_file=str(token_file)))
    assert again.auth_token == first.auth_token  # survives restarts
    explicit = server.ensure_token(Settings(auth_token="mine", token_file=str(token_file)))
    assert explicit.auth_token == "mine"


# ---- HTTP + MCP over the real ASGI app ------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "settings", CFG)
    app = server.create_app()
    app.cfg = CFG
    with TestClient(app) as c:
        yield c


def test_health_and_status(client):
    assert client.get("/health").json() == {"ok": True}
    body = client.get("/status").json()
    assert body["state"] == "starting" and body["qr_available"] is False
    assert client.get("/qr.png").status_code == 404
    assert "WhatsApp MCP" in client.get("/").text


def test_qr_png(client, monkeypatch):
    monkeypatch.setattr(server.supervisor, "snapshot", lambda: {"qr_code": "2@abc"})
    res = client.get("/qr.png")
    assert res.status_code == 200 and res.content.startswith(b"\x89PNG")


def test_guard_rejects_foreign_host_and_bad_token(client):
    assert client.get("/status", headers={"host": "evil.example"}).status_code == 403
    client.app.cfg = Settings(allowed_hosts=["testserver"], auth_token="s3cret")
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/status?token=s3cret").status_code == 200
    assert client.get("/health").status_code == 200


def _rpc(client, method, params=None, id_=1):
    res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert res.status_code == 200, res.text
    return res.json()


def test_mcp_tools_over_http(client, monkeypatch):
    init = _rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )
    assert init["result"]["serverInfo"]["name"] == "whatsapp"

    names = {t["name"] for t in _rpc(client, "tools/list")["result"]["tools"]}
    assert {"whatsapp_status", "whatsapp_get_qr_code", "list_chats", "list_messages", "search_messages"} <= names

    monkeypatch.setattr(server.supervisor, "snapshot", lambda: {"state": "waiting_qr", "qr_code": "2@abc"})
    qr = _rpc(client, "tools/call", {"name": "whatsapp_get_qr_code", "arguments": {}})["result"]
    assert not qr.get("isError")
    assert [c["type"] for c in qr["content"]] == ["text", "image"]
    assert qr["content"][1]["mimeType"] == "image/png"

    calls = []

    def fake_run_json(_settings, *args):
        calls.append(args)
        return {"messages": [{"ChatJID": "1@g.us", "MsgID": "A", "Text": "bom dia"}], "page": {"has_more": True}}

    monkeypatch.setattr(server, "run_json", fake_run_json)
    result = _rpc(client, "tools/call", {"name": "search_messages", "arguments": {"query": "--bom", "limit": 999}})
    payload = result["result"]["structuredContent"]
    assert payload["messages"][0]["text"] == "bom dia" and payload["has_more"] is True
    # limit is clamped and the query is passed after "--" so it can never become a flag
    assert calls[0] == ("messages", "search", "--limit=200", "--", "--bom")


def test_tool_errors_reach_the_agent(client, monkeypatch):
    # Regression: mcp 2.x hides the text of non-ToolError exceptions from the client.
    chats = [{"jid": "1@g.us", "name": "Silva A"}, {"jid": "2@g.us", "name": "Silva B"}]
    monkeypatch.setattr(server, "run_json", lambda *a: chats)
    result = _rpc(client, "tools/call", {"name": "list_messages", "arguments": {"chat": "silva"}})["result"]
    assert result["isError"] and "ambiguous" in result["content"][0]["text"] and "1@g.us" in result["content"][0]["text"]
