import json
import subprocess

import pytest
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from app import server, wacli
from app.config import Settings
from app.wacli import Supervisor, WacliError, run_send

CFG = Settings(allowed_hosts=["testserver"], auth_token="", send_enabled=True)
SENT = {"sent": True, "to": "161087496413237@lid", "id": "3EB0ABC"}  # wacli reports the wire (LID) address


# ---- run_send ----------------------------------------------------------------------
def test_run_send_is_writable_and_bounded(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd, env=kwargs["env"], timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"success": True, "data": SENT}), stderr="")

    monkeypatch.setenv("WACLI_READONLY", "1")
    monkeypatch.setattr(wacli.subprocess, "run", run)
    assert run_send(Settings(send_timeout=60), "text", "--to=x", "--message=oi") == SENT
    assert "--read-only" not in seen["cmd"] and "WACLI_READONLY" not in seen["env"]
    assert seen["cmd"][seen["cmd"].index("send") :] == ["send", "text", "--to=x", "--message=oi"]
    # wacli gives up before we do, so it can still report "not sent"
    assert "--timeout=55s" in seen["cmd"] and seen["timeout"] == 60


def test_run_send_timeout_warns_against_retry(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(wacli.subprocess, "run", run)
    with pytest.raises(WacliError, match="may still have been sent"):
        run_send(Settings(), "text")


def test_run_send_explains_store_lock_after_pairing(monkeypatch):
    err = json.dumps({"success": False, "error": "store is locked (another wacli is running?): store locked"})
    monkeypatch.setattr(
        wacli.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=err)
    )
    with pytest.raises(WacliError, match="still importing history"):
        run_send(Settings(), "text")


def test_send_spacing_only_when_sending_is_enabled():
    assert "--send-spacing" not in Supervisor(Settings(send_spacing="2s"))._command(True)
    cmd = Supervisor(Settings(send_enabled=True, send_spacing="1s-3s"))._command(True)
    assert cmd[cmd.index("--send-spacing") :][:2] == ["--send-spacing", "1s-3s"]


# ---- target resolution and guards ---------------------------------------------------------
@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setattr(server, "settings", CFG)
    monkeypatch.setattr(
        server.supervisor, "snapshot", lambda: {"state": "connected", "linked_jid": "5511900000000:7@s.whatsapp.net"}
    )
    calls = []

    def fake_send(_settings, *args):
        calls.append(args)
        return SENT

    monkeypatch.setattr(server, "run_send", fake_send)
    return calls


def test_send_requires_exact_chat_name(connected, monkeypatch):
    monkeypatch.setattr(server, "run_json", lambda *a: [{"jid": "1@g.us", "name": "Familia Silva"}])
    assert server._resolve_chat("familia") == "1@g.us"  # reads still accept a lone partial match
    with pytest.raises(WacliError, match="named exactly"):
        server.send_message("familia", "oi")
    assert connected == []
    server.send_message("Familia Silva", "oi")
    assert connected[0][:2] == ("text", "--to=1@g.us")


def test_send_refused_when_not_connected(connected, monkeypatch):
    monkeypatch.setattr(server.supervisor, "snapshot", lambda: {"state": "reconnecting"})
    with pytest.raises(WacliError, match="not connected"):
        server.send_message("5511999998888", "oi")
    assert connected == []


def test_send_allowlist(connected, monkeypatch):
    cfg = Settings(send_enabled=True, send_allowed_chats=["+55 11 99999-8888", "1@g.us"])
    monkeypatch.setattr(server, "settings", cfg)
    with pytest.raises(WacliError, match="WA_SEND_ALLOWED_CHATS"):
        server.send_message("5511777776666", "oi")
    server.send_message("5511999998888@s.whatsapp.net", "oi")
    server.send_message("1@g.us", "oi")
    assert len(connected) == 2


def test_send_message_args_and_result(connected):
    result = server.send_message(
        "+55 (11) 99999-8888", "--not-a-flag", reply_to="ABC", mentions=["5511888887777", " "], link_preview=False
    )
    assert connected[0] == (
        "text",
        "--to=5511999998888@s.whatsapp.net",
        "--message=--not-a-flag",  # attached with "=", so text can never be parsed as a flag
        "--reply-to=ABC",
        "--mention=5511888887777",
        "--no-preview",
    )
    assert result == {"sent": True, "chat_jid": "5511999998888@s.whatsapp.net", "id": "3EB0ABC"}


def test_send_message_validates_text(connected):
    with pytest.raises(WacliError, match="empty"):
        server.send_message("5511999998888", "   ")
    with pytest.raises(WacliError, match="limit"):
        server.send_message("5511999998888", "x" * (server.MAX_TEXT + 1))
    assert connected == []


def test_store_warning_says_do_not_resend(connected, monkeypatch):
    monkeypatch.setattr(server, "run_send", lambda *a: {**SENT, "store_warning": "disk full"})
    assert "do not send it again" in server.send_message("5511999998888", "oi")["store_warning"]


@pytest.mark.parametrize(
    ("chat", "stored", "sender"),
    [
        ("1@g.us", {"from_me": 0, "sender_jid": "5511777@s.whatsapp.net"}, "5511777@s.whatsapp.net"),
        ("1@g.us", {"from_me": 1, "sender_jid": ""}, "5511900000000:7@s.whatsapp.net"),  # own group message
        ("5511777@s.whatsapp.net", {"from_me": 0, "sender_jid": ""}, "5511777@s.whatsapp.net"),  # DM: the contact
    ],
)
def test_react_names_the_message_sender(connected, monkeypatch, chat, stored, sender):
    monkeypatch.setattr(server.media, "find_message", lambda c, m: {"chat_jid": c, "msg_id": m, **stored})
    result = server.react_to_message(chat, "MSG1", "❤️")
    assert connected[0] == ("react", f"--to={chat}", "--id=MSG1", "--reaction=❤️", f"--sender={sender}")
    assert result["target"] == "MSG1" and result["reaction"] == "❤️"


def test_react_unknown_message(connected, monkeypatch):
    monkeypatch.setattr(server.media, "find_message", lambda c, m: None)
    with pytest.raises(WacliError, match="list_messages"):
        server.react_to_message("1@g.us", "NOPE")
    assert connected == []


def test_find_message_reads_wacli_db(tmp_path):
    import sqlite3

    from app.media import MediaStore

    conn = sqlite3.connect(tmp_path / "wacli.db")
    conn.execute("CREATE TABLE messages (chat_jid TEXT, msg_id TEXT, from_me INTEGER, sender_jid TEXT)")
    conn.execute("INSERT INTO messages VALUES ('1@g.us', 'A', 0, NULL)")
    conn.commit()
    conn.close()
    store = MediaStore(Settings(store_dir=str(tmp_path)))
    assert store.find_message("1@g.us", "A") == {"chat_jid": "1@g.us", "msg_id": "A", "from_me": 0, "sender_jid": ""}
    assert store.find_message("1@g.us", "B") is None


# ---- MCP surface -------------------------------------------------------------------------
def _tools(monkeypatch, mcp):
    monkeypatch.setattr(server, "mcp", mcp)
    monkeypatch.setattr(server, "settings", CFG)
    with TestClient(server.create_app()) as client:
        res = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
    return {t["name"]: t for t in res.json()["result"]["tools"]}


def test_send_tools_are_opt_in(monkeypatch):
    default = _tools(monkeypatch, server.mcp)  # tests run without WA_SEND_ENABLED
    assert "send_message" not in default and "react_to_message" not in default
    assert default["list_chats"]["annotations"]["readOnlyHint"] is True

    enabled = MCPServer("whatsapp")
    server.register_send_tools(enabled)
    tools = _tools(monkeypatch, enabled)
    assert set(tools) == {"send_message", "react_to_message"}
    annotations = tools["send_message"]["annotations"]
    assert annotations["readOnlyHint"] is False and annotations["destructiveHint"] is True
