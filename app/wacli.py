"""Thin wrapper around the wacli binary.

- `run_json` executes one-shot read commands (`--read-only --json`), never via a shell.
- `run_send` executes one send command; while `sync --follow` holds the store lock, wacli
  delegates it to that process over its local socket (<store>/.send.sock).
- `Supervisor` keeps the long-running WhatsApp connection alive:
  `wacli auth` while unpaired (emits QR codes, then imports history until idle and exits),
  `wacli sync --follow` once paired. Only `sync --follow` serves delegated sends, so `auth`
  deliberately runs without --follow.
  Both emit NDJSON lifecycle events on stderr, which drive the connection state below.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings

log = logging.getLogger("whatsapp-mcp.wacli")


class WacliError(ToolError):
    """An anticipated failure. As a ToolError, MCP returns its message to the agent
    (any other exception reaches the agent only as a generic "Error executing tool")."""


def run_json(settings: Settings, *args: str) -> Any:
    """Run a read-only wacli command and return the `data` field of its JSON envelope."""
    cmd = [settings.wacli_bin, "--store", settings.store_dir, "--read-only", "--json", *args]
    env = {**os.environ, "WACLI_READONLY": "1"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.command_timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise WacliError(f"wacli timed out after {settings.command_timeout}s") from exc
    return _envelope(proc)


def run_send(settings: Settings, *args: str) -> Any:
    """Run a `wacli send ...` command (writable) and return its JSON `data`.

    wacli gets a shorter --timeout than the subprocess so that it reports its own outcome;
    if we still have to kill it, the message may already have gone out and must not be retried.
    """
    wacli_timeout = max(5, int(settings.send_timeout) - 5)
    cmd = [settings.wacli_bin, "--store", settings.store_dir, "--json", f"--timeout={wacli_timeout}s", "send", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=settings.send_timeout, env=_writable_env())
    except subprocess.TimeoutExpired as exc:
        raise WacliError(
            f"no answer from wacli after {settings.send_timeout}s; the message may still have been sent, "
            "so check list_messages before retrying"
        ) from exc
    try:
        return _envelope(proc)
    except WacliError as exc:
        # Lock held by a process without a send socket: `auth` still importing history right after pairing.
        if "store is locked" in str(exc):
            raise WacliError(
                "WhatsApp is still importing history after pairing; nothing was sent. Try again in a minute."
            ) from exc
        raise


def _writable_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "WACLI_READONLY"}


def _envelope(proc: subprocess.CompletedProcess[str]) -> Any:
    # Success goes to stdout; with --json, errors are a JSON envelope on stderr.
    for stream in (proc.stdout, proc.stderr):
        for line in reversed(stream.strip().splitlines()):
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(envelope, dict) and "success" in envelope:
                if envelope["success"]:
                    return envelope.get("data")
                raise WacliError(envelope.get("error") or "wacli command failed")
    raise WacliError((proc.stderr or proc.stdout).strip()[-500:] or f"wacli exited with code {proc.returncode}")


@dataclass
class ConnectionState:
    state: str = "starting"  # starting | waiting_qr | waiting_pair_code | connected | reconnecting | logged_out | error
    authenticated: bool = False
    phone: str | None = None
    linked_jid: str | None = None
    qr_code: str | None = None
    pair_code: str | None = None
    last_event: str | None = None
    last_error: str | None = None
    updated_at: float = 0.0


class Supervisor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._state = ConnectionState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._wake = threading.Event()  # cuts a restart backoff short
        self._restart_now = False
        self._logout_requested = False
        self._thread = threading.Thread(target=self._run, name="wacli-supervisor", daemon=True)

    # ---- public API -------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._terminate()

    def reconnect(self) -> None:
        """Restart the wacli process (a fresh connection); the session is kept."""
        self._restart_now = True
        self._terminate()

    def logout(self) -> None:
        """Unlink this device from WhatsApp. Messages stay; the next start shows a new QR code."""
        self._logout_requested = True
        self._terminate()

    def pair_with_phone(self, phone: str) -> None:
        """Restart pairing with an 8-character phone-number code instead of a QR code."""
        self.settings = replace(self.settings, pair_phone=phone)
        self.reconnect()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._state)

    # ---- internals --------------------------------------------------------
    def _terminate(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        self._wake.set()

    def _unlink(self) -> None:
        # `auth logout` needs the store lock, so it runs only once sync/auth has exited.
        cmd = [self.settings.wacli_bin, "--store", self.settings.store_dir, "--json", "auth", "logout"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.settings.command_timeout, env=_writable_env()
            )
            _envelope(proc)
        except (WacliError, subprocess.TimeoutExpired, OSError) as exc:
            self._update(last_error=f"unlink failed: {exc}")
            log.error("unlink failed: %s", exc)
            return
        self._update(state="logged_out", authenticated=False, phone=None, linked_jid=None, last_error=None)
        log.warning("device unlinked from WhatsApp")

    def _update(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._state, key, value)
            self._state.updated_at = time.time()

    def _refresh_auth(self) -> bool:
        try:
            data = run_json(self.settings, "auth", "status") or {}
        except WacliError as exc:
            self._update(last_error=str(exc))
            return False
        self._update(
            authenticated=bool(data.get("authenticated")),
            phone=data.get("phone"),
            linked_jid=data.get("linked_jid"),
        )
        return bool(data.get("authenticated"))

    def _command(self, authenticated: bool) -> list[str]:
        base = [self.settings.wacli_bin, "--store", self.settings.store_dir, "--events"]
        if authenticated:
            cmd = [*base, "sync", "--follow", "--max-reconnect", "0", "--presence-mode", self.settings.presence_mode]
            if self.settings.send_enabled and self.settings.send_spacing:
                cmd += ["--send-spacing", self.settings.send_spacing]
            return cmd
        cmd = [*base, "auth"]
        if self.settings.pair_phone:
            cmd += ["--phone", self.settings.pair_phone]
        return cmd

    def _run(self) -> None:
        os.makedirs(self.settings.store_dir, exist_ok=True)
        while not self._stop.is_set():
            self._wake.clear()
            if self._logout_requested:
                self._logout_requested = False
                self._unlink()
            authenticated = self._refresh_auth()
            if not authenticated:
                self._update(state="starting", qr_code=None, pair_code=None)
            cmd = self._command(authenticated)
            log.info("starting: %s", " ".join(cmd[3:]))
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
                )
                assert self._proc.stderr is not None
                for line in self._proc.stderr:
                    self._handle_line(line)
                code = self._proc.wait()
            except OSError as exc:
                self._update(state="error", last_error=f"cannot start wacli: {exc}")
                code = -1
            if self._stop.is_set():
                break
            if code == 0 and not authenticated and self._refresh_auth():
                log.info("pairing complete; switching to continuous sync")
                continue
            if self._restart_now or self._logout_requested:
                self._restart_now = False
                continue
            log.warning("wacli exited with code %s; restarting in %ss", code, self.settings.restart_backoff)
            if self._state.state not in ("logged_out", "error"):
                self._update(state="reconnecting")
            self._wake.wait(self.settings.restart_backoff)

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.info("wacli: %s", line)
            return
        if not isinstance(event, dict) or "event" not in event:
            return
        self.handle_event(event["event"], event.get("data") or {})

    def handle_event(self, name: str, data: dict[str, Any]) -> None:
        self._update(last_event=name)
        if name == "qr_code":
            self._update(state="waiting_qr", qr_code=data.get("code"))
            log.info("new QR code available at /")
        elif name == "pair_code":
            self._update(state="waiting_pair_code", pair_code=data.get("code"))
            log.info("pairing code: %s", data.get("code"))
        elif name == "connected":
            self._update(state="connected", qr_code=None, pair_code=None, last_error=None)
            self._refresh_auth()
            log.info("connected to WhatsApp")
        elif name in ("disconnected", "reconnecting"):
            self._update(state="reconnecting")
        elif name == "logged_out":
            self._update(state="logged_out", authenticated=False, phone=None, linked_jid=None)
            log.warning("WhatsApp session was logged out; a new QR code will be generated")
        elif name == "error":
            message = data.get("message") or json.dumps(data)
            self._update(last_error=str(message))
            log.error("wacli error: %s", message)
        elif name == "warning":
            log.warning("wacli warning: %s", data.get("message") or data.get("code") or data)
