"""Runtime settings: environment variables, overridden by what the admin panel saved in config_file."""

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any

log = logging.getLogger("whatsapp-mcp.config")


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


DOMAIN = os.getenv("DOMAIN", "").strip()


@dataclass(frozen=True)
class Settings:
    wacli_bin: str = os.getenv("WACLI_BIN", "wacli")
    store_dir: str = os.getenv("WACLI_STORE_DIR", "/data/store")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    # Public domain served by the optional Caddy (HTTPS) profile.
    domain: str = DOMAIN
    # URL users open in the browser.
    public_url: str = os.getenv("PUBLIC_URL") or (
        f"https://{DOMAIN}" if DOMAIN else f"http://localhost:{os.getenv('HOST_PORT', '8000')}"
    )
    # Bearer token for every route except /health. When unset, server.main() generates one
    # and persists it in token_file so it survives restarts.
    auth_token: str = os.getenv("MCP_AUTH_TOKEN", "")
    token_file: str = os.getenv("MCP_TOKEN_FILE", "/data/auth_token")
    # Host header allowlist (DNS-rebinding protection). Hostnames only, no ports.
    allowed_hosts: list[str] = field(
        default_factory=lambda: _list("ALLOWED_HOSTS", "localhost,127.0.0.1") + ([DOMAIN] if DOMAIN else [])
    )
    # Pair by phone-number code instead of QR (international number, digits only).
    pair_phone: str = os.getenv("WA_PAIR_PHONE", "")
    # "quiet" keeps phone notifications audible while the linked device is online.
    presence_mode: str = os.getenv("WA_PRESENCE_MODE", "quiet")
    # Images (issue #1): cache dir, background prefetch window and cache cap.
    media_dir: str = os.getenv("MEDIA_DIR", "/data/media")
    # Opt-in background download of recent images (0 = only download when an agent asks).
    media_prefetch_days: int = int(os.getenv("MEDIA_PREFETCH_DAYS", "0"))
    media_prefetch_interval: float = float(os.getenv("MEDIA_PREFETCH_INTERVAL", "120"))
    media_prefetch_batch: int = int(os.getenv("MEDIA_PREFETCH_BATCH", "50"))
    media_cache_max_mb: int = int(os.getenv("MEDIA_CACHE_MAX_MB", "1024"))
    image_max_edge: int = int(os.getenv("IMAGE_MAX_EDGE", "1568"))
    # Sending (opt-in): without WA_SEND_ENABLED the server stays read-only and exposes no send tools.
    send_enabled: bool = os.getenv("WA_SEND_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    # Optional allowlist of chat JIDs / phone numbers the agent may write to (empty = any chat).
    send_allowed_chats: list[str] = field(default_factory=lambda: _list("WA_SEND_ALLOWED_CHATS", ""))
    # Passed to `sync --send-spacing` to pace delegated sends, e.g. "2s" or "1s-4s" (empty = no pacing).
    send_spacing: str = os.getenv("WA_SEND_SPACING", "").strip()
    send_timeout: float = float(os.getenv("WA_SEND_TIMEOUT", "60"))
    command_timeout: float = float(os.getenv("WACLI_COMMAND_TIMEOUT", "30"))
    restart_backoff: float = float(os.getenv("WACLI_RESTART_BACKOFF", "5"))
    # Admin panel: settings saved from the browser, the admin password (ADMIN_PASSWORD, else
    # generated and persisted) and the key that signs session cookies.
    config_file: str = os.getenv("WA_CONFIG_FILE", "/data/config.json")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    admin_password_file: str = os.getenv("ADMIN_PASSWORD_FILE", "/data/admin_password")
    session_key_file: str = os.getenv("SESSION_KEY_FILE", "/data/session_key")
    session_max_age: int = int(os.getenv("SESSION_MAX_AGE", str(7 * 86400)))


_SPACING = re.compile(r"^\d+(ms|s|m)(-\d+(ms|s|m))?$")
_CHAT = re.compile(r"^(\+?[\d\s().-]{6,}|[\w.:-]+@[\w.]+)$")


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def _int_range(low: int, high: int):
    def check(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"must be a whole number from {low} to {high}")
        return value

    return check


def _chats(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("must be a list of up to 200 chats")
    chats = [str(v).strip() for v in value if str(v).strip()]
    bad = [c for c in chats if not _CHAT.match(c)]
    if bad:
        raise ValueError(f"not a chat JID or phone number: {bad[0]!r}")
    return list(dict.fromkeys(chats))


def _spacing(value: Any) -> str:
    value = str(value).strip()
    if value and not _SPACING.match(value):
        raise ValueError("use a duration such as 2s or a range such as 1s-4s")
    return value


def _presence(value: Any) -> str:
    if value not in ("quiet", "normal"):
        raise ValueError("must be quiet or normal")
    return value


# Settings the admin panel may change, with their validators. Everything else stays env-only.
EDITABLE = {
    "send_enabled": _bool,
    "send_allowed_chats": _chats,
    "send_spacing": _spacing,
    "presence_mode": _presence,
    "media_prefetch_days": _int_range(0, 90),
    "media_cache_max_mb": _int_range(64, 102400),
}


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """Check panel input; raises ValueError naming the first bad field."""
    unknown = set(values) - set(EDITABLE)
    if unknown:
        raise ValueError(f"unknown setting: {sorted(unknown)[0]}")
    clean = {}
    for key, value in values.items():
        try:
            clean[key] = EDITABLE[key](value)
        except ValueError as exc:
            raise ValueError(f"{key} {exc}") from None
    return clean


def read_overrides(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return validate(json.load(fh))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        log.error("ignoring %s: %s", path, exc)
        return {}


def write_overrides(path: str, values: dict[str, Any]) -> None:
    """Merge validated values into the saved overrides (atomic replace, owner-only)."""
    merged = {**read_overrides(path), **validate(values)}
    tmp = f"{path}.tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
    os.replace(tmp, path)


def apply_overrides(cfg: Settings) -> Settings:
    return replace(cfg, **read_overrides(cfg.config_file))


settings = apply_overrides(Settings())
