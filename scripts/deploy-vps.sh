#!/usr/bin/env bash
# Deploy WhatsApp MCP on a fresh Linux VPS (Debian/Ubuntu), as root.
#
#   PAIR_PHONE=5511999998888 SEND_ENABLED=true bash deploy-vps.sh
#
# PAIR_PHONE (optional): pair with an 8-character code shown here instead of scanning a QR code,
#   handy when the QR page (127.0.0.1 only) is not reachable from your browser.
# SEND_ENABLED (default false): expose the send_message / react_to_message tools.
# Safe to run again: keeps the existing .env, session and messages; pulls the latest image.
set -euo pipefail

DIR=${DIR:-/opt/whatsapp-mcp}
RAW=https://raw.githubusercontent.com/flaviotome/whatsapp-mcp/main
PAIR_PHONE=${PAIR_PHONE:-}
SEND_ENABLED=${SEND_ENABLED:-false}

echo "== System"
. /etc/os-release && echo "$PRETTY_NAME, $(nproc) CPU, $(free -h | awk '/Mem:/ {print $2}') RAM, $(df -h / | awk 'NR==2 {print $4}') free"
echo "== Listening TCP ports"
ss -tlnp | awk 'NR>1 {print $4, $6}' | sed 's/users:((//; s/,pid.*//' | sort -u

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "== Installing Docker (get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker --version && docker compose version

mkdir -p "$DIR" && cd "$DIR"
curl -fsSLo docker-compose.yml "$RAW/docker-compose.yml"

if [ ! -f .env ]; then
  port=8000
  while [ -n "$(ss -Htln "sport = :$port")" ]; do port=$((port + 1)); done
  cat > .env <<EOF
HOST_PORT=$port
WA_SEND_ENABLED=$SEND_ENABLED
WA_PAIR_PHONE=$PAIR_PHONE
WA_PRESENCE_MODE=quiet
TZ=America/Sao_Paulo
EOF
  chmod 600 .env
  echo "== Created $DIR/.env"
fi
cat .env

docker compose pull -q
docker compose up -d

echo "== Waiting for the service"
for _ in $(seq 1 30); do
  docker compose exec -T whatsapp-mcp wget -qO- http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 2
done
port=$(grep '^HOST_PORT=' .env | cut -d= -f2)
token=$(docker compose exec -T whatsapp-mcp cat /data/auth_token | tr -d '\r\n')
state=$(docker compose exec -T whatsapp-mcp wget -qO- --header "Authorization: Bearer $token" http://127.0.0.1:8000/status \
  | sed -E 's/.*"state":"([a-z_]+)".*/\1/' || echo unknown)

echo
echo "== WhatsApp MCP is running"
echo "   state:    $state"
echo "   endpoint: http://127.0.0.1:$port/mcp (VPS only)"
echo "   token:    $token"
if [ "$state" != "connected" ] && grep -q '^WA_PAIR_PHONE=.\+' .env; then
  echo "== Pairing code (WhatsApp > Linked devices > Link a device > Link with phone number instead):"
  for _ in $(seq 1 20); do
    code=$(docker compose logs whatsapp-mcp 2>/dev/null | sed -n 's/.*pairing code: //p' | tail -1)
    [ -n "$code" ] && break
    sleep 3
  done
  echo "   ${code:-not shown yet; run: docker compose -f $DIR/docker-compose.yml logs whatsapp-mcp | grep 'pairing code'}"
  echo "   Code expired? Get a new one with: cd $DIR && docker compose restart && docker compose logs -f | grep 'pairing code'"
fi
