#!/usr/bin/env bash
# setup-vps.sh — One-time VPS setup for Hevy2Intervals webhook service.
#
# Prerequisites: Docker and Caddy should already be installed on the VPS.
# This script adds the hevy-sync service.
#
# Usage:
#   ssh root@<VPS_IP> 'bash -s' < scripts/setup-vps.sh <domain>
#   or: make setup-vps DOMAIN=hevy.yourdomain.com

set -euo pipefail

DOMAIN="${1:?Usage: $0 <domain>}"
DEPLOY_USER="coach"
DEPLOY_PATH="/opt/hevy-sync"

echo "=== Hevy2Intervals VPS Setup ==="
echo "  Domain: ${DOMAIN}"
echo "  Path:   ${DEPLOY_PATH}"
echo ""

# ── 1. Verify Docker and Caddy are available ─────────────────
echo "[1/4] Checking prerequisites..."
command -v docker &>/dev/null || { echo "ERROR: Docker not installed. Install Docker and Caddy first."; exit 1; }
command -v caddy &>/dev/null || { echo "ERROR: Caddy not installed. Install Docker and Caddy first."; exit 1; }
echo "  Docker: $(docker --version)"
echo "  Caddy:  $(caddy version)"

# ── 2. Create directory structure ─────────────────────────────
echo "[2/4] Creating directories..."
mkdir -p "${DEPLOY_PATH}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_PATH}"

# Create placeholder .env if it doesn't exist
if [ ! -f "${DEPLOY_PATH}/.env" ]; then
    cat > "${DEPLOY_PATH}/.env" <<'ENVEOF'
# ─── Hevy → Intervals.icu Sync — Production Config ──────────
HEVY_API_KEY=
INTERVALS_API_KEY=
INTERVALS_ATHLETE_ID=0
WEBHOOK_SECRET=change_me_to_a_random_string
SYNC_DB_PATH=/data/hevy_icu_sync.db
PORT=8400
LOG_LEVEL=INFO
ENVEOF
    chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_PATH}/.env"
    chmod 600 "${DEPLOY_PATH}/.env"
    echo "  Created placeholder .env (edit before first deploy!)"
fi

# ── 3. Add Caddy reverse proxy entry ─────────────────────────
echo "[3/4] Configuring Caddy..."
CADDYFILE="/etc/caddy/Caddyfile"

# Check if domain already configured
if grep -q "${DOMAIN}" "${CADDYFILE}" 2>/dev/null; then
    echo "  ${DOMAIN} already in Caddyfile, skipping"
else
    # Append new site block
    cat >> "${CADDYFILE}" <<EOF

${DOMAIN} {
    reverse_proxy localhost:8400

    header {
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
        Referrer-Policy strict-origin-when-cross-origin
    }

    log {
        output file /var/log/caddy/${DOMAIN}.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}
EOF
    systemctl reload caddy
    echo "  Added ${DOMAIN} → localhost:8400 to Caddyfile"
fi

# ── 4. Login to ghcr.io if needed ────────────────────────────
echo "[4/4] Checking container registry access..."
if docker pull ghcr.io/sebdenes/hevy2intervals:latest 2>/dev/null; then
    echo "  Registry access OK"
else
    echo "  NOTE: ghcr.io pull failed. You may need to authenticate:"
    echo "    echo <GITHUB_PAT> | docker login ghcr.io -u sebdenes --password-stdin"
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "  VPS setup complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Edit ${DEPLOY_PATH}/.env with your API keys"
echo "  2. Point DNS: ${DOMAIN} → $(curl -s ifconfig.me || echo '<VPS_IP>')"
echo "  3. From your local machine, deploy with:"
echo "       make deploy DEPLOY_HOST=$(hostname -f 2>/dev/null || echo '<VPS_IP>')"
echo "  4. Configure Hevy webhook:"
echo "       URL: https://${DOMAIN}/webhook/hevy"
echo "       Auth: Bearer <your WEBHOOK_SECRET>"
echo "  5. Verify: curl https://${DOMAIN}/health"
echo ""
