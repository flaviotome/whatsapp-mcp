"""Admin panel: login, dashboard, Claude connection details and settings, for people who
would rather not use a terminal.

Who can do what:
- /mcp: the bearer token (agents). /status and /qr.png: the token or an admin session.
- The panel and /api/*: an admin session only. A token holder can read and send through MCP,
  but cannot change settings, see or rotate the token, or unlink the device.

Sessions are a signed cookie (HMAC-SHA256 over expiry + a fingerprint of the password, key kept
in the data volume), HttpOnly and SameSite=Strict, so they survive restarts and a password change
logs everyone out. Every POST must also carry a same-origin Origin header (CSRF).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .config import EDITABLE, Settings, read_overrides, write_overrides

log = logging.getLogger("whatsapp-mcp.web")

STATIC = Path(__file__).parent / "static"
COOKIE = "wamcp_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


# ---- secrets ------------------------------------------------------------------------
def _read_or_create(path: str, make: Callable[[], str]) -> tuple[str, bool]:
    """Return the secret stored in path, creating it (owner-only) if missing; second item = created."""
    file = Path(path)
    value = file.read_text(encoding="utf-8").strip() if file.exists() else ""
    if value:
        return value, False
    value = make()
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(value + "\n", encoding="utf-8")
    file.chmod(0o600)
    return value, True


def ensure_admin_password(cfg: Settings) -> tuple[Settings, bool]:
    """ADMIN_PASSWORD, else the persisted password, else a new one. Second item: newly generated."""
    if cfg.admin_password:
        return cfg, False
    password, created = _read_or_create(cfg.admin_password_file, lambda: secrets.token_urlsafe(9))
    return replace(cfg, admin_password=password), created


class Sessions:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._key: bytes | None = None

    @property
    def key(self) -> bytes:
        # Created on first use, so a server without an admin password (tests) never writes it.
        if self._key is None:
            key, _ = _read_or_create(self.cfg.session_key_file, lambda: secrets.token_hex(32))
            self._key = key.encode()
        return self._key

    def _sign(self, expires: int) -> str:
        fingerprint = hashlib.sha256(self.cfg.admin_password.encode()).hexdigest()
        return hmac.new(self.key, f"{expires}|{fingerprint}".encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + self.cfg.session_max_age
        return f"{expires}.{self._sign(expires)}"

    def valid(self, cookie: str | None) -> bool:
        if not self.cfg.admin_password:  # tests only: main() always sets a password
            return True
        expires, _, signature = (cookie or "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(signature, self._sign(int(expires)))


class Throttle:
    """Global brake on password guessing: at most `limit` failures per `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 300):
        self.limit, self.window = limit, window
        self.failures: deque[float] = deque()
        self.lock = threading.Lock()

    def blocked(self) -> bool:
        with self.lock:
            while self.failures and self.failures[0] < time.time() - self.window:
                self.failures.popleft()
            return len(self.failures) >= self.limit

    def fail(self) -> None:
        with self.lock:
            self.failures.append(time.time())


# ---- helpers --------------------------------------------------------------------------
def same_origin(request: Request) -> bool:
    """CSRF check. Browsers send Origin on POST, but as "null" in some cases (e.g. form posts under
    strict referrer policies); Sec-Fetch-Site is then the signal, and pages cannot forge either."""
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlparse(origin).netloc == request.headers.get("host")
    return request.headers.get("sec-fetch-site") == "same-origin"


def secure_request(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def page(name: str) -> Response:
    return FileResponse(STATIC / name, media_type="text/html", headers=SECURITY_HEADERS)


def api(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def restart_soon(delay: float = 0.5) -> None:
    """Stop the process gracefully; Docker's restart policy starts it again with the new settings."""
    threading.Timer(delay, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


@dataclass
class Panel:
    """What the panel needs from the server, passed in to avoid a circular import."""

    settings: Callable[[], Settings]
    supervisor: Any
    media: Any
    activity: deque
    status: Callable[[], dict[str, Any]]
    run_json: Callable[..., Any]
    restart: Callable[[], None] = restart_soon
    throttle: Throttle | None = None
    sessions: Sessions | None = None

    def session_ok(self, request: Request) -> bool:
        return self._sessions().valid(request.cookies.get(COOKIE))

    def _sessions(self) -> Sessions:
        if self.sessions is None or self.sessions.cfg is not self.settings():
            self.sessions = Sessions(self.settings())
        return self.sessions


# ---- routes -----------------------------------------------------------------------------
def register(mcp: Any, panel: Panel) -> None:
    panel.throttle = panel.throttle or Throttle()
    route = mcp.custom_route

    @route("/login", methods=["GET"])
    async def login_page(request: Request) -> Response:
        if panel.session_ok(request):
            return RedirectResponse("/", status_code=303)
        return page("login.html")

    @route("/login", methods=["POST"])
    async def login(request: Request) -> Response:
        if not same_origin(request):
            return RedirectResponse("/login?e=origin", status_code=303)
        if panel.throttle.blocked():
            return RedirectResponse("/login?e=wait", status_code=303)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        password = (form.get("password") or [""])[0]
        if not secrets.compare_digest(password.encode(), panel.settings().admin_password.encode()):
            panel.throttle.fail()
            log.warning("admin login failed")
            return RedirectResponse("/login?e=1", status_code=303)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE,
            panel._sessions().issue(),
            max_age=panel.settings().session_max_age,
            httponly=True,
            samesite="strict",
            secure=secure_request(request),
        )
        return response

    @route("/logout", methods=["POST"])
    async def logout(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        if not panel.session_ok(request):
            return RedirectResponse("/login", status_code=303)
        return page("admin.html")

    @route("/static/{name}", methods=["GET"])
    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        file = STATIC / name
        if not re.fullmatch(r"[\w-]+\.(css|js|svg)", name) or not file.is_file():
            return Response(status_code=404)
        return FileResponse(file, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    # The routes below are reachable only with a session (GuardMiddleware); POSTs also need same origin.
    def post_guard(request: Request) -> Response | None:
        return None if same_origin(request) else api({"error": "pedido de outra origem recusado"}, 403)

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    @route("/api/overview", methods=["GET"])
    async def overview(_: Request) -> Response:
        cfg = panel.settings()
        try:
            stats = panel.media.db_stats()
        except Exception as exc:  # the panel must render even if wacli.db is busy or missing
            log.warning("db stats unavailable: %s", exc)
            stats = {}
        calls = list(panel.activity)
        return api(
            {
                "status": panel.status(),
                "stats": stats,
                "sends": sum(1 for a in calls if a["tool"] in ("send_message", "react_to_message") and a["ok"]),
                "activity": calls[:20],
                "send_enabled": cfg.send_enabled,
            }
        )

    @route("/api/connection", methods=["GET"])
    async def connection(request: Request) -> Response:
        cfg = panel.settings()
        base = f"https://{cfg.domain}" if cfg.domain else f"{request.url.scheme}://{request.headers.get('host')}"
        return api({"mcp_url": f"{base}/mcp", "token": cfg.auth_token, "token_from_env": bool(os.getenv("MCP_AUTH_TOKEN"))})

    @route("/api/token/rotate", methods=["POST"])
    async def rotate(request: Request) -> Response:
        if refused := post_guard(request):
            return refused
        if os.getenv("MCP_AUTH_TOKEN"):
            return api({"error": "o token está fixado em MCP_AUTH_TOKEN; troque-o lá"}, 409)
        path = Path(panel.settings().token_file)
        path.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        path.chmod(0o600)
        log.info("access token rotated from the admin panel; restarting")
        panel.restart()
        return api({"restarting": True})

    @route("/api/settings", methods=["GET"])
    async def get_settings(_: Request) -> Response:
        cfg = panel.settings()
        return api(
            {
                "values": {key: getattr(cfg, key) for key in EDITABLE},
                "saved": sorted(read_overrides(cfg.config_file)),
            }
        )

    @route("/api/settings", methods=["POST"])
    async def save_settings(request: Request) -> Response:
        if refused := post_guard(request):
            return refused
        try:
            write_overrides(panel.settings().config_file, await json_body(request))
        except ValueError as exc:
            return api({"error": str(exc)}, 400)
        log.info("settings saved from the admin panel; restarting")
        panel.restart()
        return api({"restarting": True})

    @route("/api/chats", methods=["GET"])
    async def chats(request: Request) -> Response:
        query = request.query_params.get("q", "").strip()[:100]
        args = ["chats", "list", "--limit=10"] + ([f"--query={query}"] if query else [])
        rows = panel.run_json(panel.settings(), *args) or []
        return api([{"jid": c.get("jid"), "name": c.get("name") or c.get("jid")} for c in rows])

    @route("/api/whatsapp/reconnect", methods=["POST"])
    async def reconnect(request: Request) -> Response:
        if refused := post_guard(request):
            return refused
        panel.supervisor.reconnect()
        return api({"ok": True})

    @route("/api/whatsapp/logout", methods=["POST"])
    async def unlink(request: Request) -> Response:
        if refused := post_guard(request):
            return refused
        panel.supervisor.logout()
        log.warning("WhatsApp unlink requested from the admin panel")
        return api({"ok": True})

    @route("/api/whatsapp/pair-phone", methods=["POST"])
    async def pair_phone(request: Request) -> Response:
        if refused := post_guard(request):
            return refused
        phone = re.sub(r"\D", "", str((await json_body(request)).get("phone", "")))
        if not 8 <= len(phone) <= 15:
            return api({"error": "digite o número completo com DDI e DDD, por exemplo 5511999998888"}, 400)
        if panel.status().get("authenticated"):
            return api({"error": "já está conectado; desconecte primeiro"}, 409)
        panel.supervisor.pair_with_phone(phone)
        return api({"ok": True})
