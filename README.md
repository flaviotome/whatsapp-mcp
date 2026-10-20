# WhatsApp MCP

WhatsApp access for AI agents through the [Model Context Protocol](https://modelcontextprotocol.io): read-only by default, sending optional.
Link your number by scanning a QR code once, then ask Claude (or any MCP client) things like
*"which chats have unread messages?"* or *"summarize today's messages in the Family group"*.

- **One container**, one command: `docker compose up -d`. Runs on any Linux VM (amd64 or arm64).
- **Read-only by default**: list chats, read and search messages, find contacts, look at received images. Nothing is sent, deleted or changed.
- **Optional sending** (`WA_SEND_ENABLED=true`): send text messages, replies and reactions, optionally limited to an allowlist of chats.
- Built on [wacli](https://github.com/openclaw/wacli) (Go + [whatsmeow](https://github.com/tulir/whatsmeow)), which links as a WhatsApp Web device and keeps a local, full-text searchable copy of your messages.

> Not affiliated with WhatsApp or Meta. It uses the WhatsApp Web protocol as a linked device; use it with your own account.

## Quick start (on your VM)

Requirements: Docker with Compose v2.

```bash
mkdir whatsapp-mcp && cd whatsapp-mcp
curl -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/docker-compose.yml
docker compose up -d
docker compose logs whatsapp-mcp | grep -E "Admin panel|Admin password"
```

<details>
<summary>Windows (PowerShell)</summary>

```powershell
mkdir $HOME\whatsapp-mcp; cd $HOME\whatsapp-mcp
curl.exe -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/docker-compose.yml
docker compose up -d
docker compose logs whatsapp-mcp | Select-String "Admin panel|Admin password"
```

Use `curl.exe`, not `curl`: in Windows PowerShell `curl` is an alias for `Invoke-WebRequest`.
</details>

Open the **admin panel** (`http://localhost:8000`) and sign in with the **admin password** from the log. It is generated on first start and kept in the data volume; print it again any time with:

```bash
docker compose exec whatsapp-mcp cat /data/admin_password
```

From the panel you link WhatsApp, copy the ready-made Claude configuration and change settings, without a terminal.

The container restarts automatically with the VM (`restart: unless-stopped`) and keeps the WhatsApp session in the `whatsapp-data` volume, so you scan the QR code only once.

<details>
<summary>Fresh Debian/Ubuntu VPS, as root (installs Docker too)</summary>

[`scripts/deploy-vps.sh`](scripts/deploy-vps.sh) installs Docker if missing, sets everything up in `/opt/whatsapp-mcp` (on the first free port from 8000) and prints the admin password. With `PAIR_PHONE` it also prints a phone-number pairing code, useful when you only have the provider's browser console and cannot open the QR page:

```bash
curl -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/scripts/deploy-vps.sh
PAIR_PHONE=5511999998888 SEND_ENABLED=false bash deploy-vps.sh
```

Running it again keeps `.env`, the session and the messages, and updates the image.
</details>

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

### Option C — behind a Traefik that already uses ports 80/443

For VMs where another stack already runs Traefik, such as Hostinger's n8n template, [`docker-compose.traefik.yml`](docker-compose.traefik.yml) adds a router for this service and leaves the other stack untouched. Look up Traefik's network and certificate resolver (`docker inspect <traefik-container>` shows `--certificatesresolvers.<name>…` and its networks), then add them to `.env`:

```bash
curl -fsSLO https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main/docker-compose.traefik.yml
cat >> .env <<'EOF'
DOMAIN=wa.example.com
TRAEFIK_NETWORK=n8n_default
TRAEFIK_CERTRESOLVER=mytlschallenge
COMPOSE_FILE=docker-compose.yml:docker-compose.traefik.yml
EOF
docker compose up -d
```

`COMPOSE_FILE` makes every later `docker compose` command include the override. Traefik picks up the labels without a restart and requests the certificate within seconds.

## Link your WhatsApp

1. Open the admin panel (`http://localhost:8000` or `https://<DOMAIN>`) and sign in.
2. On your phone: **WhatsApp › Settings › Linked devices › Link a device**, scan the QR code shown in the panel.
3. The panel switches to *Connected as +55…*. Recent history (up to ~3 months, as decided by the phone) arrives over the next minutes; new messages arrive live.

No camera at hand? Enter your number under *Prefer a code instead of the QR?* in the panel (or set `WA_PAIR_PHONE` in `.env`) and type the 8-character code after *Link with phone number instead*.

## Connect your agent

MCP endpoint: `http://localhost:8000/mcp` (through the tunnel) or `https://<DOMAIN>/mcp`, with header `Authorization: Bearer <token>`. The panel's **Conectar ao Claude** page shows the address and token and copies ready-made configuration for each client below.

**Claude Code**

```bash
claude mcp add --transport http whatsapp http://localhost:8000/mcp --header "Authorization: Bearer <token>"
```

**Claude Desktop** — add to `claude_desktop_config.json` (uses [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) as a stdio bridge; requires Node.js), see [`clients/claude_desktop_config.json`](clients/claude_desktop_config.json).

**claude.ai / remote connectors** — with Option B or C, add a custom connector pointing to `https://<DOMAIN>/mcp`, with request header `Authorization` set to `Bearer <token>`.

Then just ask:

- "Am I connected to WhatsApp? If not, show me the QR code."
- "Which chats have unread messages?"
- "Summarize the last 50 messages in the Family group."
- "Find messages about 'invoice' since 2026-09-01."
- "What is in the photo Alice sent yesterday?"
- With sending enabled: "Reply to Alice that I'll be there at 8" (the agent shows you the message and asks before sending).

## Tools

| Tool | Arguments | Description |
| --- | --- | --- |
| `whatsapp_status` | — | Connection state, linked phone, last error |
| `whatsapp_get_qr_code` | — | Current pairing QR code as an image |
| `list_chats` | `query?`, `limit`, `unread_only` | Chats, pinned first then most recent |
| `list_messages` | `chat?`, `limit`, `after?`, `before?` | Messages, newest first |
| `search_messages` | `query`, `chat?`, `limit`, `after?`, `before?` | Full-text search (SQLite FTS5) |
| `search_contacts` | `query`, `limit` | Contacts by name, phone or JID |
| `list_images` | `chat?`, `limit`, `after?`, `before?`, `include_stickers` | Image messages with sender, caption and cache state |
| `get_message_image` | `chat`, `message_id` | The image itself (resized to ≤1568 px) plus sender/caption, for the agent to analyse |
| `send_message` ✉️ | `chat`, `text`, `reply_to?`, `mentions?`, `link_preview` | Send a text message (optionally quoting a message or @mentioning group members) |
| `react_to_message` ✉️ | `chat`, `message_id`, `emoji` | React to a message (`""` removes the reaction) |

✉️ Only with `WA_SEND_ENABLED=true`; otherwise these tools do not exist.

`chat` accepts a JID, a phone number or a (partial) chat name; ambiguous names return the candidates. Send tools only accept a name that matches one chat exactly. Dates are `YYYY-MM-DD` or RFC 3339.

### Images

Images are returned to the client as MCP image content, so multimodal agents (Claude) can describe, read or compare them. The server never analyses images itself.

- **On demand**: an image is downloaded only when an agent opens it with `get_message_image`, e.g. while summarizing a chat ("catch me up on the Family group since yesterday") the agent sees which messages are images and opens the relevant ones. Nothing is downloaded in the background by default.
- **Expiry**: WhatsApp's servers keep media for a limited time; older images may no longer be downloadable. If you prefer to keep a local copy of recent images anyway, set `MEDIA_PREFETCH_DAYS=N` to download every image from the last N days in the background (costs disk and bandwidth).
- **Cache**: `/data/media/images`, capped by `MEDIA_CACHE_MAX_MB` (default 1024; oldest evicted first). `whatsapp_status` reports `images_cached` and `cache_mb`.
- Videos, documents and voice notes are not downloaded yet (see the issues for planned audio transcription).

### Sending

Off by default. With `WA_SEND_ENABLED=true` in `.env` (then `docker compose up -d`), the agent gets `send_message` and `react_to_message` and writes **as you** from your account.

- Sends reuse the running connection: wacli delegates each send to the `sync --follow` process over a local socket, so there is no second WhatsApp session. The service must be `connected`; otherwise nothing is sent.
- The tools are annotated as destructive and the server instructions tell the agent to show you the recipient and text and wait for your OK, and never to send because a received message asks for it. Clients such as Claude also ask for permission before calling a non-read-only tool.
- `WA_SEND_ALLOWED_CHATS` restricts sending to some chats: comma-separated chat JIDs as shown by `list_chats` (e.g. `120363...@g.us`) or phone numbers (`5511999998888`). A contact stored under a `@lid` JID must be listed by that JID.
- `WA_SEND_SPACING` (e.g. `2s` or `1s-4s`) spaces consecutive sends; bursts of messages are what get accounts rate-limited or banned.
- `sent: true` means WhatsApp accepted the message, not that it was delivered. If a send times out, the message may still have gone out: the agent is told to check `list_messages` before retrying.

## HTTP endpoints

| Route | Description | Auth |
| --- | --- | --- |
| `POST /mcp` | MCP Streamable HTTP endpoint (stateless) | token |
| `GET /` | Admin panel: status and pairing, Claude connection, settings | admin |
| `GET/POST /login`, `POST /logout` | Admin sign-in (password) | — |
| `/api/*` | Panel API (settings, token rotation, reconnect, unlink, pairing by phone) | admin |
| `GET /qr.png` | Current QR code (404 when none) | token or admin |
| `GET /status` | `state`, `phone`, `qr_available`, `last_error`, … | token or admin |
| `GET /health` | Liveness probe | — |

*token* = `Authorization: Bearer <token>` (agents and scripts; never accepted in URLs). *admin* = the panel's session cookie, obtained with the admin password. The two are separate on purpose: the MCP token lets a client read and send, but not change settings, see or rotate the token, or unlink the device.

## Configuration

All optional, via `.env` next to `docker-compose.yml` (see [`.env.example`](.env.example)). Settings changed in the admin panel (sending, allowed chats, spacing, presence, image prefetch and cache) are saved to `/data/config.json` and take precedence over `.env`.

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_AUTH_TOKEN` | auto-generated | Fixed bearer token (then it cannot be rotated from the panel) |
| `ADMIN_PASSWORD` | auto-generated | Fixed admin panel password; otherwise generated on first start and stored in `/data/admin_password` |
| `HOST_PORT` | `8000` | Port on the VM, bound to `127.0.0.1` |
| `DOMAIN` | — | Public domain for the `https` profile |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Accepted `Host` headers (DNS-rebinding protection); `DOMAIN` is added automatically |
| `WA_PAIR_PHONE` | — | Pair with a phone-number code instead of QR |
| `WA_PRESENCE_MODE` | `quiet` | `quiet` keeps phone notifications audible; `normal` shows this device online |
| `WACLI_SYNC_MAX_MESSAGES` / `WACLI_SYNC_MAX_DB_SIZE` | — | Caps on local history growth |
| `MEDIA_PREFETCH_DAYS` | `0` | `0` = download images only when an agent opens them; `N` = also download every image from the last N days in the background |
| `MEDIA_CACHE_MAX_MB` | `1024` | Image cache size cap |
| `WA_SEND_ENABLED` | `false` | `true` adds the send tools (see [Sending](#sending)) |
| `WA_SEND_ALLOWED_CHATS` | — | Comma-separated chat JIDs / phone numbers the agent may send to (empty = any chat) |
| `WA_SEND_SPACING` | — | Minimum spacing between sends, e.g. `2s` or `1s-4s` |
| `WA_SEND_TIMEOUT` | `60` | Seconds to wait for one send |
| `WHATSAPP_MCP_VERSION` | `latest` | Image tag to run |

## Operations

```bash
docker compose pull && docker compose up -d   # update
docker compose logs -f whatsapp-mcp            # follow logs
docker compose down                            # stop (session kept)
docker compose down -v                         # stop and DELETE session + messages
```

To unlink, use *Desconectar WhatsApp* in the panel, or remove the device in *WhatsApp › Linked devices*; the service shows a new QR code.

## How it works

```
 agent ──MCP──► /mcp ┐                         ┌─► wacli auth            (not linked: emits QR codes, imports history, exits)
 browser ─────► /    ├─ Python (app/) ─ supervisor ┤
                     │                         └─► wacli sync --follow   (linked: live sync, auto-reconnect)
                     ├─ tools ─► wacli --read-only --json chats|messages|contacts …
                     └─ send tools ─► wacli send text|react ──socket──► sync --follow  (only with WA_SEND_ENABLED)
                                         │
                       /data: session.db (device keys) · wacli.db (messages, FTS5) · auth_token · media/images
```

- A supervisor thread runs the long-lived wacli process and turns its NDJSON events (`qr_code`, `connected`, `logged_out`, …) into the connection state.
- Each tool call runs a read-only wacli command (no shell, validated arguments) against the local index; reads work while sync holds the store lock.
- Images are found with a read-only SQLite query on `wacli.db` and fetched with `wacli --read-only media download`, which needs no store lock either.
- Sends run `wacli send …` without `--read-only`; wacli finds the running sync through `<store>/.send.sock` (mode 0600) and sends over its connection.
- Message text, names and filenames are treated as untrusted data: the server instructions tell the agent never to follow instructions found in them.

## Security

- The QR code links a device to your account and `/mcp` exposes your conversations: keep the token secret and do not expose the port without the token/HTTPS setup above.
- With `WA_SEND_ENABLED=true` the token also lets its holder **send messages as you**. Enable it only where you need it, and consider `WA_SEND_ALLOWED_CHATS`. Prompt injection is the main risk: a received message may try to make the agent send something, so review what the agent wants to send before approving it.
- Host-header allowlist against DNS rebinding; access logs are disabled and tokens are never accepted in URLs, so they do not land in logs or browser history.
- Admin panel: separate password; signed session cookie (HttpOnly, SameSite=Strict, Secure over HTTPS) that a password change invalidates; same-origin check on every change (CSRF); at most 10 wrong passwords per 5 minutes; strict Content-Security-Policy and no framing.
- Everything runs as an unprivileged user; the WhatsApp session never leaves the data volume.

## Development

```bash
pip install -r requirements-dev.txt && python -m pytest -q          # unit tests
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build   # build locally
```

`WACLI_REPO` / `WACLI_REF` (build args, also read by `docker-compose.dev.yml`) choose the wacli source; the default is the pinned upstream release.
Images for `linux/amd64` and `linux/arm64` are published to `ghcr.io/flaviotome/whatsapp-mcp` by [GitHub Actions](.github/workflows/docker-publish.yml) on every push to `main` (`latest`) and on `v*` tags.

## Limitations (v0.1)

- Sending covers text, replies, mentions and reactions; files, voice notes, polls, editing and deleting are not exposed yet. No on-demand history backfill. Only images are downloaded (no video, documents or audio yet).
- Available history is what the phone sends at pairing plus everything received while the container runs.
- One WhatsApp account per container.

## License

[MIT](LICENSE). The bundled `wacli` binary is also MIT-licensed; its license is included in the image at `/usr/share/licenses/wacli/LICENSE`.
