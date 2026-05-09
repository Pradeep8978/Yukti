#!/usr/bin/env bash
# scripts/renew_dhan_token.sh
#
# Renews the DhanHQ access token, writes it to both .env files, restarts the
# Yukti container, and sends a Telegram alert.
#
# Run via host cron (times are IST = UTC+5:30):
#   30 2  * * *  /opt/yukti/scripts/renew_dhan_token.sh   # 08:00 IST daily
#   0  12 * * *  /opt/yukti/scripts/renew_dhan_token.sh   # 17:30 IST daily
#
# Manual run: bash /opt/yukti/scripts/renew_dhan_token.sh

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
LOG_FILE="$REPO_DIR/logs/renew.log"
mkdir -p "$REPO_DIR/logs"
DHAN_BASE_URL="https://api.dhan.co/v2"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# ── Read credentials from .env ─────────────────────────────────────────────────
_read_env() { grep "^${1}=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r\n'; }
DHAN_ACCESS_TOKEN="$(_read_env DHAN_ACCESS_TOKEN)"
DHAN_CLIENT_ID="$(_read_env DHAN_CLIENT_ID)"
TELEGRAM_BOT_TOKEN="$(_read_env TELEGRAM_BOT_TOKEN)"
TELEGRAM_CHAT_ID="$(_read_env TELEGRAM_CHAT_ID)"

if [[ -z "$DHAN_ACCESS_TOKEN" || -z "$DHAN_CLIENT_ID" ]]; then
    log "ERROR: DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID missing from $ENV_FILE"
    exit 1
fi

# ── Telegram helper ────────────────────────────────────────────────────────────
send_telegram() {
    local msg="$1"
    [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]] && return 0
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${msg}" \
        -d "parse_mode=Markdown" > /dev/null 2>&1 || true
}

log "=== DhanHQ token renewal starting ==="

# ── Step 1: Call /RenewToken ───────────────────────────────────────────────────
RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "access-token: ${DHAN_ACCESS_TOKEN}" \
    -H "dhanClientId: ${DHAN_CLIENT_ID}" \
    "${DHAN_BASE_URL}/RenewToken" 2>&1) || true

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)
log "RenewToken response: HTTP $HTTP_CODE — ${BODY:0:200}"

if [[ "$HTTP_CODE" != "200" ]]; then
    msg="🚨 *Yukti: DhanHQ token renewal FAILED* (HTTP ${HTTP_CODE}).
Response: ${BODY:0:300}

Please generate a new token at https://dhanhq.co and run:
\`\`\`
sed -i 's|^DHAN_ACCESS_TOKEN=.*|DHAN_ACCESS_TOKEN=NEW_TOKEN|' /opt/yukti/.env
docker compose -f /opt/yukti/docker-compose.yml up -d --force-recreate yukti
\`\`\`"
    log "FAIL: renewal API returned $HTTP_CODE"
    send_telegram "$msg"
    exit 1
fi

# ── Step 2: Parse new token from JSON ─────────────────────────────────────────
NEW_TOKEN=$(python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    t = (d.get('token') or d.get('accessToken') or d.get('access_token')
         or d.get('AccessToken')
         or (d.get('data') or {}).get('token')
         or (d.get('data') or {}).get('accessToken')
         or (d.get('data') or {}).get('access_token') or '')
    print(t, end='')
except Exception:
    print('', end='')
" <<< "${BODY}" 2>/dev/null) || true

if [[ -z "$NEW_TOKEN" ]]; then
    msg="🚨 *Yukti: DhanHQ renewal returned 200 but no token found* in response.
Body: ${BODY:0:300}"
    log "FAIL: no token in response body"
    send_telegram "$msg"
    exit 1
fi

log "New token obtained (last 20 chars): ...${NEW_TOKEN: -20}"

# ── Step 3: Write new token to .env ──────────────────────────────────────────
for f in "$ENV_FILE"; do
    if [[ ! -f "$f" ]]; then
        log "SKIP: $f not found"
        continue
    fi
    # In-place replacement without temp file (avoids bind-mount rename issues)
    python3 - "$f" "$NEW_TOKEN" <<'PYEOF'
import sys
path, token = sys.argv[1], sys.argv[2]
lines = open(path).read().splitlines()
out = []
replaced = False
for l in lines:
    if l.startswith('DHAN_ACCESS_TOKEN='):
        out.append(f'DHAN_ACCESS_TOKEN={token}')
        replaced = True
    else:
        out.append(l)
if not replaced:
    out.append(f'DHAN_ACCESS_TOKEN={token}')
open(path, 'w').write('\n'.join(out) + '\n')
PYEOF
    log "Updated $f"
done

# ── Step 4: Recreate container so it picks up the new token ──────────────────
log "Restarting Yukti container..."
cd "$REPO_DIR"
docker compose up -d --force-recreate yukti >> "$LOG_FILE" 2>&1

# ── Step 5: Health check ──────────────────────────────────────────────────────
log "Waiting 25s for container to come healthy..."
sleep 25
HTTP_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")
log "Health check: HTTP $HTTP_HEALTH"

if [[ "$HTTP_HEALTH" == "200" ]]; then
    log "SUCCESS: token renewed, container healthy"
    send_telegram "✅ *Yukti: DhanHQ token renewed successfully.*
Container restarted and health check passed. Trading resumes normally."
else
    log "WARN: container health check returned $HTTP_HEALTH after renewal"
    send_telegram "⚠️ *Yukti: DhanHQ token renewed but health check returned ${HTTP_HEALTH}.*
Check logs: \`docker compose logs yukti --tail 30\`"
fi

log "=== Renewal complete ==="
