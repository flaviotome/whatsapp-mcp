# WhatsApp MCP

Read-only WhatsApp access for AI agents through the [Model Context Protocol](https://modelcontextprotocol.io).
Link your number by scanning a QR code once, then ask Claude (or any MCP client) things like
*"which chats have unread messages?"* or *"summarize today's messages in the Family group"*.

- **One container**, one command: `docker compose up -d`. Runs on any Linux VM (amd64 or arm64).
- **Read-only**: list chats, read and search messages, find contacts. Nothing is ever sent, deleted or changed.
- Built on [wacli](https://github.com/openclaw/wacli) (Go + [whatsmeow](https://github.com/tulir/whatsmeow)), which links as a WhatsApp Web device and keeps a local, full-text searchable copy of your messages.

> Not affiliated with WhatsApp or Meta. It uses the WhatsApp Web protocol as a linked device; use it with your own account.

## Quick start (on your VM)

Requirements: Docker with Compose v2.

```bash
mkdir whatsapp-mcp && cd whatsapp-mcp
curl -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/docker-compose.yml
docker compose up -d
docker compose logs whatsapp-mcp | grep -E "QR page|MCP endpoint"
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
mkdir $HOME\whatsapp-mcp; cd $HOME\whatsapp-mcp
curl.exe -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/docker-compose.yml
docker compose up -d
docker compose logs whatsapp-mcp | Select-String "QR page|MCP endpoint"
```

Use `curl.exe`, not `curl`: in Windows PowerShell `curl` is an alias for `Invoke-WebRequest`.
</details>

The log shows the **QR page URL with your access token**. The token is generated on first start and kept in the data volume; print it again any time with:

```bash
docker compose exec whatsapp-mcp cat /data/auth_token
```

The container restarts automatically with the VM (`restart: unless-stopped`) and keeps the WhatsApp session in the `whatsapp-data` volume, so you scan the QR code only once.

## Connect to the VM

The service listens on `127.0.0.1:8000` of the VM only. Pick one way to reach it:

### Option A — SSH tunnel (default, nothing exposed)

From your computer:

```bash
ssh -N -L 8000:localhost:8000 user@your-vm
```

While the tunnel is open, `http://localhost:8000` on your computer is the service.

### Option B — public HTTPS (optional)

Needs a domain whose DNS points to the VM and ports 80/443 open. Create a `.env` next to `docker-compose.yml`:

```bash
echo "DOMAIN=wa.example.com" > .env
docker compose --profile https up -d
```

[Caddy](https://caddyserver.com) obtains a Let's Encrypt certificate automatically and serves `https://wa.example.com`. Every route except `/health` requires the token.

## Link your WhatsApp

1. Open the QR page from the log: `http://localhost:8000/?token=<token>` (or `https://<DOMAIN>/?token=<token>`).
2. On your phone: **WhatsApp › Settings › Linked devices › Link a device**, scan the code.
3. The page switches to *Connected as +55…*. Recent history (up to ~3 months, as decided by the phone) arrives over the next minutes; new messages arrive live.

No camera at hand? Set `WA_PAIR_PHONE=5511999998888` in `.env` and restart; the page shows an 8-character code for *Link with phone number instead*.

## Connect your agent

MCP endpoint: `http://localhost:8000/mcp` (through the tunnel) or `https://<DOMAIN>/mcp`, with header `Authorization: Bearer <token>`.

**Claude Code**

```bash
claude mcp add --transport http whatsapp http://localhost:8000/mcp --header "Authorization: Bearer <token>"
```

**Claude Desktop** — add to `claude_desktop_config.json` (uses [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as a stdio bridge; requires Node.js), see [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json).

**claude.ai / remote connectors** — with Option B, add a custom connector pointing to `https://<DOMAIN>/mcp`.

Then just ask:

- "Am I connected to WhatsApp? If not, show me the QR code."
- "Which chats have unread messages?"
- "Summarize the last 50 messages in the Family group."
- "Find messages about 'invoice' since 2026-09-01."

## Tools

| Tool | Arguments | Description |
| --- | --- | --- |
| `whatsapp_status` | — | Connection state, linked phone, last error |
| `whatsapp_get_qr_code` | — | Current pairing QR code as an image |
| `list_chats` | `query?`, `limit`, `unread_only` | Chats, pinned first then most recent |
| `list_messages` | `chat?`, `limit`, `after?`, `before?` | Messages, newest first |
| `search_messages` | `query`, `chat?`, `limit`, `after?`, `before?` | Full-text search (SQLite FTS5) |
| `search_contacts` | `query`, `limit` | Contacts by name, phone or JID |

`chat` accepts a JID, a phone number or a (partial) chat name; ambiguous names return the candidates. Dates are `YYYY-MM-DD` or RFC 3339.

## HTTP endpoints

| Route | Description |
| --- | --- |
| `POST /mcp` | MCP Streamable HTTP endpoint (stateless) |
| `GET /` | Pairing page (auto-refreshing QR code) |
| `GET /qr.png` | Current QR code (404 when none) |
| `GET /status` | `state`, `phone`, `qr_available`, `last_error`, … |
| `GET /health` | Liveness probe (no auth) |

Authentication: `Authorization: Bearer <token>` or, for the browser page, `?token=<token>`.

## Configuration

All optional, via `.env` next to `docker-compose.yml` (see [`.env.example`](.env.example)):

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | auto-generated | Fixed bearer token |
| `HOST_PORT` | `8000` | Port on the VM, bound to `127.0.0.1` |
| `DOMAIN` | — | Public domain for the `https` profile |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Accepted `Host` headers (DNS-rebinding protection); `DOMAIN` is added automatically |
| `WA_PAIR_PHONE` | — | Pair with a phone-number code instead of QR |
| `WA_PRESENCE_MODE` | `quiet` | `quiet` keeps phone notifications audible; `normal` shows this device online |
| `WACLI_SYNC_MAX_MESSAGES` / `WACLI_SYNC_MAX_DB_SIZE` | — | Caps on local history growth |
| `WHATSAPP_MCP_VERSION` | `latest` | Image tag to run |

## Operations

```bash
docker compose pull && docker compose up -d   # update
docker compose logs -f whatsapp-mcp            # follow logs
docker compose down                            # stop (session kept)
docker compose down -v                         # stop and DELETE session + messages
```

To unlink, remove the device in *WhatsApp › Linked devices*; the service shows a new QR code.

## How it works

```
 agent ──MCP──► /mcp ┐                         ┌─► wacli auth --follow   (not linked: emits QR codes)
 browser ─────► /    ├─ Python (app/) ─ supervisor ┤
                     │                         └─► wacli sync --follow   (linked: live sync, auto-reconnect)
                     └─ tools ─► wacli --read-only --json chats|messages|contacts …
                                         │
                       /data: session.db (device keys) · wacli.db (messages, FTS5) · auth_token
```

- A supervisor thread runs the long-lived wacli process and turns its NDJSON events (`qr_code`, `connected`, `logged_out`, …) into the connection state.
- Each tool call runs a read-only wacli command (no shell, validated arguments) against the local index; reads work while sync holds the store lock.
- Message text, names and filenames are treated as untrusted data: the server instructions tell the agent never to follow instructions found in them.

## Security

- The QR code links a device to your account and `/mcp` exposes your conversations: keep the token secret and do not expose the port without the token/HTTPS setup above.
- Host-header allowlist against DNS rebinding; access logs are disabled so tokens never land in logs.
- Everything runs as an unprivileged user; the WhatsApp session never leaves the data volume.

## Development

```bash
pip install -r requirements-dev.txt && python -m pytest -q          # unit tests
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build   # build locally
```

`WACLI_REPO` / `WACLI_REF` (build args, also read by `docker-compose.dev.yml`) choose the wacli source; the default is the pinned upstream release.
Images for `linux/amd64` and `linux/arm64` are published to `ghcr.io/flaviotome/whatsapp-mcp` by [GitHub Actions](.github/workflows/docker-publish.yml) on every push to `main` (`latest`) and on `v*` tags.

## Limitations (v0.1)

- Read-only: no sending, media download or on-demand history backfill.
- Available history is what the phone sends at pairing plus everything received while the container runs.
- One WhatsApp account per container.

## License

[MIT](LICENSE). The bundled `wacli` binary is also MIT-licensed; its license is included in the image at `/usr/share/licenses/wacli/LICENSE`.
