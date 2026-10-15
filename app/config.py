"""Runtime settings, read once from environment variables."""

import os
from dataclasses import dataclass, field


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
    media_prefetch_days: int = int(os.getenv("MEDIA_PREFETCH_DAYS", "30"))  # 0 disables prefetch
    media_prefetch_interval: float = float(os.getenv("MEDIA_PREFETCH_INTERVAL", "120"))
    media_prefetch_batch: int = int(os.getenv("MEDIA_PREFETCH_BATCH", "50"))
    media_cache_max_mb: int = int(os.getenv("MEDIA_CACHE_MAX_MB", "1024"))
    image_max_edge: int = int(os.getenv("IMAGE_MAX_EDGE", "1568"))
    command_timeout: float = float(os.getenv("WACLI_COMMAND_TIMEOUT", "30"))
    restart_backoff: float = float(os.getenv("WACLI_RESTART_BACKOFF", "5"))


settings = Settings()
