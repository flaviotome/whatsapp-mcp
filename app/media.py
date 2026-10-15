"""WhatsApp images for MCP clients (issue #1).

- Discovery: read-only SQLite queries on wacli.db (a documented wacli integration surface).
- Download: `wacli --read-only media download`, which takes no store lock and therefore
  runs while `sync --follow` is connected.
- Cache: <media_dir>/images/<sha1(chat|msg)>.img, capped by size (oldest evicted first).
  Downloads that WhatsApp's CDN refuses (expired media) leave a .failed marker so the
  background prefetcher does not retry them on every pass.
- Prefetch: a thread downloads recent images before WhatsApp's CDN expires them.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageOps

from .config import Settings
from .wacli import WacliError, run_json

log = logging.getLogger("whatsapp-mcp.media")

# Errors meaning the media is gone from WhatsApp's CDN (or was never downloadable).
_PERMANENT_ERROR = re.compile(r"\b(403|404|410)\b|no downloadable media", re.IGNORECASE)


class MediaStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.dir = Path(settings.media_dir) / "images"
        self.db_path = Path(settings.store_dir) / "wacli.db"
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    # ---- discovery -------------------------------------------------------------
    def query_images(
        self,
        chat_jid: str | None = None,
        msg_id: str | None = None,
        after: int | None = None,
        before: int | None = None,
        limit: int = 50,
        include_stickers: bool = False,
    ) -> list[dict[str, Any]]:
        """Image messages, newest first. Timestamps are unix seconds."""
        if not self.db_path.exists():
            return []
        types = ("image", "sticker") if include_stickers else ("image",)
        sql = [
            "SELECT m.chat_jid, COALESCE(NULLIF(c.name, ''), m.chat_name, '') AS chat_name, m.msg_id, m.ts,",
            " m.from_me, COALESCE(m.sender_jid, '') AS sender_jid, COALESCE(m.sender_name, '') AS sender_name,",
            " m.media_type, COALESCE(m.mime_type, '') AS mime_type, COALESCE(m.media_caption, '') AS caption,",
            " COALESCE(m.file_length, 0) AS file_length",
            " FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid",
            f" WHERE m.media_type IN ({','.join('?' * len(types))})",
            # Same requirements as `wacli media download`: a CDN path and a media key.
            " AND m.deleted_at IS NULL AND COALESCE(m.direct_path, '') != '' AND length(m.media_key) > 0",
        ]
        params: list[Any] = list(types)
        for clause, value in (
            (" AND m.chat_jid = ?", chat_jid),
            (" AND m.msg_id = ?", msg_id),
            (" AND m.ts >= ?", after),
            (" AND m.ts < ?", before),
        ):
            if value is not None:
                sql.append(clause)
                params.append(value)
        sql.append(" ORDER BY m.ts DESC, m.rowid DESC LIMIT ?")
        params.append(limit)
        # mode=ro: never writes; a normal read-only connection sees sync's WAL updates.
        conn = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("".join(sql), params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- cache -----------------------------------------------------------------
    def _key(self, chat_jid: str, msg_id: str) -> str:
        return hashlib.sha1(f"{chat_jid}|{msg_id}".encode()).hexdigest()

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[key]

    def cached(self, chat_jid: str, msg_id: str) -> Path | None:
        path = self.dir / f"{self._key(chat_jid, msg_id)}.img"
        return path if path.exists() else None

    def failed(self, chat_jid: str, msg_id: str) -> bool:
        return (self.dir / f"{self._key(chat_jid, msg_id)}.failed").exists()

    def fetch(self, chat_jid: str, msg_id: str) -> Path:
        """Return the cached image file, downloading it first if needed."""
        key = self._key(chat_jid, msg_id)
        final = self.dir / f"{key}.img"
        with self._lock(key):
            if final.exists():
                final.touch()  # mark as recently used for eviction
                return final
            marker = self.dir / f"{key}.failed"
            if marker.exists():
                raise WacliError(f"image is no longer available on WhatsApp: {marker.read_text().strip()}")
            self.dir.mkdir(parents=True, exist_ok=True)
            part = self.dir / f"{key}.part"
            try:
                run_json(self.settings, "media", "download", f"--chat={chat_jid}", f"--id={msg_id}", f"--output={part}")
            except WacliError as exc:
                part.unlink(missing_ok=True)
                if _PERMANENT_ERROR.search(str(exc)):
                    marker.write_text(str(exc)[:300])
                raise
            part.replace(final)
        self.evict()
        return final

    def stats(self) -> dict[str, Any]:
        files = list(self.dir.glob("*.img")) if self.dir.exists() else []
        return {"images_cached": len(files), "cache_mb": round(sum(f.stat().st_size for f in files) / 1048576, 1)}

    def evict(self) -> None:
        if not self.dir.exists():
            return
        limit = self.settings.media_cache_max_mb * 1048576
        files = sorted(self.dir.glob("*.img"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        while files and total > limit:
            oldest = files.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)

    def _cache_full(self) -> bool:
        return self.stats()["cache_mb"] >= self.settings.media_cache_max_mb * 0.9

    # ---- client output -----------------------------------------------------------
    def prepare(self, path: Path, media_type: str) -> tuple[bytes, str]:
        """Downscale for the client (long edge <= image_max_edge). Photos -> JPEG, stickers/alpha -> PNG."""
        with PILImage.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((self.settings.image_max_edge, self.settings.image_max_edge))
            buf = io.BytesIO()
            if media_type == "sticker" or img.mode in ("RGBA", "LA", "P"):
                img.save(buf, format="PNG", optimize=True)
                return buf.getvalue(), "png"
            img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), "jpeg"

    # ---- prefetch ----------------------------------------------------------------
    def prefetch_once(self) -> dict[str, int]:
        """Download recent, not-yet-cached images; bounded by batch size and cache cap."""
        counts = {"downloaded": 0, "failed": 0}
        if self.settings.media_prefetch_days <= 0 or self._cache_full():
            return counts
        since = int(time.time()) - self.settings.media_prefetch_days * 86400
        # Over-fetch candidates so already-cached rows don't starve the batch.
        candidates = self.query_images(after=since, limit=self.settings.media_prefetch_batch * 20)
        pending = [
            c for c in candidates if not self.cached(c["chat_jid"], c["msg_id"]) and not self.failed(c["chat_jid"], c["msg_id"])
        ]
        for row in pending[: self.settings.media_prefetch_batch]:
            try:
                self.fetch(row["chat_jid"], row["msg_id"])
                counts["downloaded"] += 1
            except WacliError as exc:
                counts["failed"] += 1
                log.debug("prefetch %s/%s failed: %s", row["chat_jid"], row["msg_id"], exc)
            if self._cache_full():
                break
        return counts


class Prefetcher:
    def __init__(self, store: MediaStore):
        self.store = store
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="image-prefetch", daemon=True)

    def start(self) -> None:
        if self.store.settings.media_prefetch_days > 0:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        delay = 15.0  # first pass shortly after start, then every media_prefetch_interval
        while not self._stop.wait(delay):
            delay = self.store.settings.media_prefetch_interval
            try:
                counts = self.store.prefetch_once()
            except (sqlite3.Error, OSError) as exc:
                log.warning("image prefetch skipped: %s", exc)
                continue
            if counts["downloaded"] or counts["failed"]:
                log.info("image prefetch: %(downloaded)d downloaded, %(failed)d unavailable", counts)


def parse_time(value: str | None) -> int | None:
    """YYYY-MM-DD or RFC 3339 -> unix seconds (naive values are UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise WacliError(f"invalid date {value!r}; use YYYY-MM-DD or RFC3339") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
