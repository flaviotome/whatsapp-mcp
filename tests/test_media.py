import io
import sqlite3
import time

import pytest
from PIL import Image as PILImage
from starlette.testclient import TestClient

from app import media as media_mod
from app import server
from app.config import Settings
from app.media import MediaStore, parse_time
from app.wacli import WacliError

NOW = int(time.time())
CHAT = "5511999990000@s.whatsapp.net"
GROUP = "120363000000000000@g.us"


def _png(size=(10, 10), mode="RGB") -> bytes:
    buf = io.BytesIO()
    PILImage.new(mode, size, "red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    (tmp_path / "store").mkdir()
    db = sqlite3.connect(tmp_path / "store" / "wacli.db")
    db.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE messages (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT, chat_name TEXT, msg_id TEXT, sender_jid TEXT,
            sender_name TEXT, ts INTEGER, from_me INTEGER, media_type TEXT, media_caption TEXT, mime_type TEXT,
            direct_path TEXT, media_key BLOB, file_length INTEGER, deleted_at INTEGER);
        """
    )
    db.execute("INSERT INTO chats VALUES (?, 'Alice'), (?, 'Family')", (CHAT, GROUP))
    rows = [
        # chat, id, ts, media_type, direct_path, deleted_at, caption
        (CHAT, "img-new", NOW - 3600, "image", "/p", None, "beach"),
        (CHAT, "img-old", NOW - 90 * 86400, "image", "/p", None, ""),
        (CHAT, "sticker", NOW - 60, "sticker", "/p", None, ""),
        (CHAT, "deleted", NOW - 60, "image", "/p", NOW, ""),
        (CHAT, "no-path", NOW - 60, "image", "", None, ""),
        (CHAT, "video", NOW - 60, "video", "/p", None, ""),
        (CHAT, "no-key", NOW - 60, "image", "/p", None, ""),
        (GROUP, "img-group", NOW - 7200, "image", "/p", None, ""),
    ]
    db.executemany(
        "INSERT INTO messages (chat_jid, msg_id, ts, media_type, direct_path, deleted_at, media_caption, from_me,"
        " sender_name, mime_type, file_length) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'Alice', 'image/jpeg', 2048)",
        rows,
    )
    db.execute("UPDATE messages SET media_key = x'01' WHERE msg_id != 'no-key'")
    db.commit()
    db.close()
    cfg = Settings(
        store_dir=str(tmp_path / "store"), media_dir=str(tmp_path / "media"), media_prefetch_days=30, media_prefetch_batch=10
    )
    return MediaStore(cfg)


@pytest.fixture
def fake_download(monkeypatch):
    """Fake `wacli media download`: writes an image to --output, or raises a scripted error."""
    calls, errors = [], {}

    def run(_settings, *args):
        calls.append(args)
        msg_id = next(a.split("=", 1)[1] for a in args if a.startswith("--id="))
        if msg_id in errors:
            raise WacliError(errors[msg_id])
        out = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
        with open(out, "wb") as f:
            f.write(_png())
        return {"path": out}

    monkeypatch.setattr(media_mod, "run_json", run)
    return calls, errors


def test_query_images_filters(store):
    ids = [r["msg_id"] for r in store.query_images()]
    assert ids == ["img-new", "img-group", "img-old"]  # newest first; no deleted, path-less, key-less, video or sticker
    assert [r["msg_id"] for r in store.query_images(include_stickers=True)][0] == "sticker"
    assert [r["msg_id"] for r in store.query_images(chat_jid=GROUP)] == ["img-group"]
    assert [r["msg_id"] for r in store.query_images(after=NOW - 86400)] == ["img-new", "img-group"]
    assert store.query_images(msg_id="img-new")[0]["chat_name"] == "Alice"


def test_query_images_without_db(tmp_path):
    assert MediaStore(Settings(store_dir=str(tmp_path))).query_images() == []


def test_fetch_downloads_once_and_remembers_expired(store, fake_download):
    calls, errors = fake_download
    path = store.fetch(CHAT, "img-new")
    assert path.exists() and store.cached(CHAT, "img-new") == path
    store.fetch(CHAT, "img-new")
    assert len(calls) == 1  # served from cache
    assert "--output=" in " ".join(calls[0]) and calls[0][:2] == ("media", "download")

    errors["img-old"] = "download failed with status code 403"
    with pytest.raises(WacliError):
        store.fetch(CHAT, "img-old")
    assert store.failed(CHAT, "img-old")
    with pytest.raises(WacliError, match="no longer available"):
        store.fetch(CHAT, "img-old")
    assert len(calls) == 2  # expired media is not requested again

    errors["img-group"] = "dial tcp: i/o timeout"  # transient: no marker, retried later
    with pytest.raises(WacliError):
        store.fetch(GROUP, "img-group")
    assert not store.failed(GROUP, "img-group")


def test_prefetch_downloads_recent_images_only(store, fake_download):
    calls, _ = fake_download
    assert store.prefetch_once() == {"downloaded": 2, "failed": 0}
    assert {c[3] for c in calls} == {"--id=img-new", "--id=img-group"}
    assert store.prefetch_once() == {"downloaded": 0, "failed": 0}  # nothing left in the window
    assert store.stats()["images_cached"] == 2


def test_prefetch_is_off_by_default(store, fake_download):
    calls, _ = fake_download
    assert Settings().media_prefetch_days == 0
    object.__setattr__(store.settings, "media_prefetch_days", 0)
    assert store.prefetch_once() == {"downloaded": 0, "failed": 0} and calls == []
    prefetcher = media_mod.Prefetcher(store)
    prefetcher.start()
    assert not prefetcher._thread.is_alive()  # no background thread: images download on demand only


def test_evict_keeps_cache_under_cap(store, fake_download):
    store.fetch(CHAT, "img-new")
    object.__setattr__(store.settings, "media_cache_max_mb", 0)
    store.evict()
    assert store.stats()["images_cached"] == 0


def test_prepare_downscales_and_picks_format(store, tmp_path):
    big = tmp_path / "big.img"
    big.write_bytes(_png((4000, 3000)))
    data, fmt = store.prepare(big, "image")
    with PILImage.open(io.BytesIO(data)) as out:
        assert fmt == "jpeg" and max(out.size) == 1568
    sticker = tmp_path / "s.img"
    sticker.write_bytes(_png((512, 512), "RGBA"))
    assert store.prepare(sticker, "sticker")[1] == "png"


def test_parse_time():
    assert parse_time("2026-10-01") == parse_time("2026-10-01T00:00:00Z")
    assert parse_time(None) is None


# ---- through the MCP endpoint ---------------------------------------------------------
@pytest.fixture
def client(monkeypatch, store):
    cfg = Settings(allowed_hosts=["testserver"], auth_token="")
    monkeypatch.setattr(server, "settings", cfg)
    monkeypatch.setattr(server, "media", store)
    with TestClient(server.create_app()) as c:
        yield c


def _call(client, name, arguments):
    res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert res.status_code == 200, res.text
    return res.json()["result"]


def test_image_tools_over_mcp(client, fake_download):
    listed = _call(client, "list_images", {"chat": CHAT, "limit": 5})["structuredContent"]
    assert listed["count"] == 2 and listed["images"][0]["id"] == "img-new"
    assert listed["images"][0]["caption"] == "beach" and listed["images"][0]["cached"] is False

    got = _call(client, "get_message_image", {"chat": CHAT, "message_id": "img-new"})
    assert not got.get("isError")
    assert [c["type"] for c in got["content"]] == ["text", "image"]
    assert "Caption: beach" in got["content"][0]["text"] and got["content"][1]["mimeType"] == "image/jpeg"

    missing = _call(client, "get_message_image", {"chat": CHAT, "message_id": "video"})
    assert missing["isError"] and "list_images" in missing["content"][0]["text"]
