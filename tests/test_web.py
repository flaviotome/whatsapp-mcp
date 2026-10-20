import json
import sqlite3
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from app import config, server, wacli, web
from app.config import Settings, apply_overrides, read_overrides, validate, write_overrides
from app.media import MediaStore
from app.wacli import Supervisor
from app.web import Sessions, Throttle, ensure_admin_password

ORIGIN = {"Origin": "http://testserver"}


# ---- settings overrides ----------------------------------------------------------------
def test_validate_accepts_panel_values_and_names_bad_field():
    ok = validate({"send_enabled": True, "send_allowed_chats": [" 1@g.us ", "+55 11 99999-8888", "1@g.us"],
                   "send_spacing": "1s-4s", "presence_mode": "quiet", "media_prefetch_days": 7})
    assert ok["send_allowed_chats"] == ["1@g.us", "+55 11 99999-8888"]  # trimmed, deduplicated
    for bad, field in [
        ({"send_enabled": "yes"}, "send_enabled"),
        ({"send_spacing": "soon"}, "send_spacing"),
        ({"media_cache_max_mb": 1}, "media_cache_max_mb"),
        ({"send_allowed_chats": ["; rm -rf /"]}, "send_allowed_chats"),
        ({"auth_token": "x"}, "unknown setting"),  # only the editable keys, never secrets or paths
    ]:
        with pytest.raises(ValueError, match=field):
            validate(bad)


def test_overrides_merge_persist_privately_and_apply(tmp_path):
    path = tmp_path / "config.json"
    write_overrides(str(path), {"send_enabled": True})
    write_overrides(str(path), {"send_spacing": "2s"})
    assert json.loads(path.read_text()) == {"send_enabled": True, "send_spacing": "2s"}
    if hasattr(path.stat(), "st_mode") and __import__("os").name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    cfg = apply_overrides(Settings(config_file=str(path), send_enabled=False))
    assert cfg.send_enabled is True and cfg.send_spacing == "2s"


def test_broken_overrides_file_is_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"send_enabled": "maybe"}')
    assert read_overrides(str(path)) == {}
    path.write_text("not json")
    assert read_overrides(str(path)) == {}


# ---- admin secrets ------------------------------------------------------------------------
def test_admin_password_generated_once_and_env_wins(tmp_path):
    cfg = Settings(admin_password="", admin_password_file=str(tmp_path / "admin_password"))
    first, created = ensure_admin_password(cfg)
    again, created_again = ensure_admin_password(cfg)
    assert created and not created_again and first.admin_password == again.admin_password
    assert len(first.admin_password) >= 12
    fixed, _ = ensure_admin_password(Settings(admin_password="mine"))
    assert fixed.admin_password == "mine"


def test_sessions_expire_resist_tampering_and_follow_the_password(tmp_path):
    cfg = Settings(admin_password="pw", session_key_file=str(tmp_path / "key"), session_max_age=60)
    sessions = Sessions(cfg)
    cookie = sessions.issue()
    assert sessions.valid(cookie)
    expires, sig = cookie.split(".")
    assert not sessions.valid(f"{int(expires) + 999}.{sig}")
    assert not sessions.valid("")
    assert not sessions.valid(f"{int(time.time()) - 1}.{sessions._sign(int(time.time()) - 1)}")
    assert Sessions(cfg).valid(cookie)  # same persisted key: survives a restart
    assert not Sessions(Settings(admin_password="new", session_key_file=cfg.session_key_file)).valid(cookie)


def test_no_session_key_written_without_admin_password(tmp_path):
    # Regression: Sessions created the key file eagerly, so servers without a password (and the
    # tests) tried to write /data/session_key.
    sessions = Sessions(Settings(admin_password="", session_key_file=str(tmp_path / "key")))
    assert sessions.valid(None)
    assert not (tmp_path / "key").exists()


def test_throttle():
    t = Throttle(limit=2, window=60)
    t.fail()
    assert not t.blocked()
    t.fail()
    assert t.blocked()


# ---- HTTP: panel ------------------------------------------------------------------------------
@pytest.fixture
def panel_client(monkeypatch, tmp_path):
    cfg = Settings(
        allowed_hosts=["testserver"],
        auth_token="tok",
        admin_password="pw",
        token_file=str(tmp_path / "auth_token"),
        config_file=str(tmp_path / "config.json"),
        session_key_file=str(tmp_path / "session_key"),
    )
    (tmp_path / "auth_token").write_text("tok\n")
    restarts = []
    monkeypatch.setattr(server, "settings", cfg)
    monkeypatch.setattr(server.panel, "restart", lambda: restarts.append(True))
    monkeypatch.setattr(server.panel, "throttle", Throttle())
    monkeypatch.setattr(server.panel, "sessions", None)
    monkeypatch.setattr(server.media, "db_stats", lambda: {"chats": 2, "messages": 5, "last_message_at": None})
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with TestClient(server.create_app(), follow_redirects=False) as client:
        client.restarts = restarts
        client.tmp = tmp_path
        yield client


def _login(client, password="pw"):
    return client.post("/login", data={"password": password}, headers=ORIGIN)


def test_panel_requires_login(panel_client):
    res = panel_client.get("/")
    assert res.status_code == 303 and res.headers["location"] == "/login"
    assert panel_client.get("/api/overview").status_code == 401
    login = panel_client.get("/login")
    assert login.status_code == 200 and "frame-ancestors 'none'" in login.headers["content-security-policy"]
    assert panel_client.get("/static/admin.js").status_code == 200


def test_login_sets_strict_httponly_cookie(panel_client):
    assert _login(panel_client, "wrong").headers["location"] == "/login?e=1"
    res = _login(panel_client)
    assert res.status_code == 303 and res.headers["location"] == "/"
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    page = panel_client.get("/")
    assert page.status_code == 200 and "admin.js" in page.text
    assert page.headers["x-frame-options"] == "DENY"


def test_login_needs_same_origin_and_is_throttled(panel_client):
    res = panel_client.post("/login", data={"password": "pw"})
    assert res.headers["location"] == "/login?e=origin"
    res = panel_client.post("/login", data={"password": "pw"}, headers={"Origin": "https://evil.example"})
    assert res.headers["location"] == "/login?e=origin"
    res = panel_client.post("/login", data={"password": "pw"}, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
    assert res.headers["location"] == "/login?e=origin"
    # Real browsers may send Origin: null on a same-origin form post; Sec-Fetch-Site vouches for it.
    res = panel_client.post("/login", data={"password": "pw"}, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert res.headers["location"] == "/"
    panel_client.cookies.clear()
    for _ in range(10):
        _login(panel_client, "guess")
    assert _login(panel_client).headers["location"] == "/login?e=wait"  # even the right password waits


def test_token_and_session_are_separate(panel_client):
    bearer = {"Authorization": "Bearer tok"}
    assert panel_client.get("/api/overview", headers=bearer).status_code == 401  # MCP token is not admin
    assert panel_client.get("/status", headers=bearer).status_code == 200
    _login(panel_client)
    assert panel_client.get("/status").status_code == 200
    mcp = panel_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Accept": "application/json, text/event-stream"})
    assert mcp.status_code == 401  # a browser session cannot drive the MCP endpoint


def test_logout_clears_session(panel_client):
    _login(panel_client)
    res = panel_client.post("/logout", headers=ORIGIN)
    assert res.status_code == 303 and 'wamcp_session=""' in res.headers["set-cookie"]


def test_overview_and_connection(panel_client):
    _login(panel_client)
    data = panel_client.get("/api/overview").json()
    assert data["stats"]["messages"] == 5 and data["status"]["state"]
    conn = panel_client.get("/api/connection").json()
    assert conn == {"mcp_url": "http://testserver/mcp", "token": "tok", "token_from_env": False}


def test_save_settings_validates_needs_origin_and_restarts(panel_client):
    _login(panel_client)
    assert panel_client.post("/api/settings", json={"send_enabled": True}).status_code == 403
    assert not panel_client.restarts
    bad = panel_client.post("/api/settings", json={"send_spacing": "now"}, headers=ORIGIN)
    assert bad.status_code == 400 and "send_spacing" in bad.json()["error"]
    ok = panel_client.post("/api/settings", json={"send_enabled": True, "send_allowed_chats": ["1@g.us"]},
                           headers=ORIGIN)
    assert ok.json() == {"restarting": True} and panel_client.restarts == [True]
    saved = json.loads((panel_client.tmp / "config.json").read_text())
    assert saved == {"send_enabled": True, "send_allowed_chats": ["1@g.us"]}
    assert panel_client.get("/api/settings").json()["saved"] == ["send_allowed_chats", "send_enabled"]


def test_rotate_token(panel_client, monkeypatch):
    _login(panel_client)
    assert panel_client.post("/api/token/rotate").status_code == 403
    assert panel_client.post("/api/token/rotate", headers=ORIGIN).json() == {"restarting": True}
    new = (panel_client.tmp / "auth_token").read_text().strip()
    assert new != "tok" and len(new) >= 40 and panel_client.restarts
    monkeypatch.setenv("MCP_AUTH_TOKEN", "fixed")
    assert panel_client.post("/api/token/rotate", headers=ORIGIN).status_code == 409


def test_whatsapp_actions(panel_client, monkeypatch):
    calls = []
    monkeypatch.setattr(server.supervisor, "reconnect", lambda: calls.append("reconnect"))
    monkeypatch.setattr(server.supervisor, "logout", lambda: calls.append("logout"))
    monkeypatch.setattr(server.supervisor, "pair_with_phone", lambda p: calls.append(p))
    _login(panel_client)
    panel_client.post("/api/whatsapp/reconnect", headers=ORIGIN)
    panel_client.post("/api/whatsapp/logout", headers=ORIGIN)
    assert panel_client.post("/api/whatsapp/pair-phone", json={"phone": "12"}, headers=ORIGIN).status_code == 400
    panel_client.post("/api/whatsapp/pair-phone", json={"phone": "+55 (11) 99999-8888"}, headers=ORIGIN)
    assert calls == ["reconnect", "logout", "5511999998888"]
    assert panel_client.post("/api/whatsapp/logout").status_code == 403


def test_chat_search_for_allowlist(panel_client, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "run_json", lambda _cfg, *args: seen.append(args) or [{"jid": "1@g.us", "name": "Família"}])
    _login(panel_client)
    assert panel_client.get("/api/chats?q=fam").json() == [{"jid": "1@g.us", "name": "Família"}]
    assert seen[0] == ("chats", "list", "--limit=10", "--query=fam")


def test_static_only_serves_known_assets(panel_client):
    assert panel_client.get("/static/style.css").status_code == 200
    for path in ("/static/login.html", "/static/..%2Fweb.py", "/static/missing.js"):
        assert panel_client.get(path).status_code == 404


# ---- activity log and stats ---------------------------------------------------------------------
def test_tool_calls_are_recorded_without_content(monkeypatch):
    server.activity.clear()
    monkeypatch.setattr(server, "run_json", lambda *a: {"messages": [{"Text": "secret"}]})
    server.list_messages(chat="1@g.us")
    with pytest.raises(Exception):
        server.search_messages(query="  ")
    first, second = server.activity
    assert first["tool"] == "search_messages" and first["ok"] is False
    assert second == {"tool": "list_messages", "chat": "1@g.us", "at": second["at"], "ok": True}
    assert "secret" not in json.dumps(list(server.activity))


def test_db_stats(tmp_path):
    conn = sqlite3.connect(tmp_path / "wacli.db")
    conn.execute("CREATE TABLE chats (jid TEXT)")
    conn.execute("CREATE TABLE messages (chat_jid TEXT, ts INTEGER)")
    conn.executemany("INSERT INTO messages VALUES (?, ?)", [("a", 100), ("a", 1792441847)])
    conn.execute("INSERT INTO chats VALUES ('a')")
    conn.commit()
    conn.close()
    stats = MediaStore(Settings(store_dir=str(tmp_path))).db_stats()
    assert stats == {"chats": 1, "messages": 2, "last_message_at": "2026-10-19T20:30:47Z"}


# ---- supervisor actions -------------------------------------------------------------------------
def test_unlink_runs_auth_logout_once_wacli_has_stopped(monkeypatch):
    sup = Supervisor(Settings(restart_backoff=60))
    events = []

    def fake_run(cmd, **kwargs):
        events.append(cmd[cmd.index("auth") :])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"success": True, "data": {"logged_out": True}}), stderr="")

    class FakeProc:
        stderr: list[str] = []

        def __init__(self, cmd, **kwargs):
            events.append("start " + ("sync" if "sync" in cmd else "auth"))
            if len([e for e in events if isinstance(e, str)]) == 2:
                sup._stop.set()
            else:
                sup.logout()  # requested while the first process runs

        def poll(self):
            return 0

        def wait(self):
            return 0

    monkeypatch.setattr(wacli.subprocess, "run", fake_run)
    monkeypatch.setattr(wacli.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(wacli.os, "makedirs", lambda *a, **k: None)
    auth = iter([True, False])
    monkeypatch.setattr(sup, "_refresh_auth", lambda: next(auth))
    sup._run()  # would block for restart_backoff if the unlink did not skip it
    assert events == ["start sync", ["auth", "logout"], "start auth"]
    assert sup.snapshot()["phone"] is None  # unlinked; auth now shows a new QR code


def test_pair_with_phone_restarts_auth_with_the_number():
    sup = Supervisor(Settings())
    sup.pair_with_phone("5511999998888")
    assert sup._command(False)[-2:] == ["--phone", "5511999998888"] and sup._restart_now
