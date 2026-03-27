#!/usr/bin/env bash
# Deploy Hevy2Intervals to production VPS.
# Usage: make deploy DEPLOY_HOST=your-server-ip
#    or: DEPLOY_HOST=your-server-ip ./scripts/deploy.sh

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:?Set DEPLOY_HOST (e.g., your-server-ip)}"
DEPLOY_USER="${DEPLOY_USER:-coach}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/hevy-sync}"

SSH_TARGET="${DEPLOY_USER}@${DEPLOY_HOST}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo "=== Deploying Hevy2Intervals to ${SSH_TARGET}:${DEPLOY_PATH} ==="

# 1. Sync compose files
echo "[1/4] Syncing compose files..."
rsync -avz --delete \
    docker-compose.yml docker-compose.prod.yml \
    "${SSH_TARGET}:${DEPLOY_PATH}/"

# 2. Pull latest image
echo "[2/4] Pulling latest image from ghcr.io..."
ssh "${SSH_TARGET}" "cd ${DEPLOY_PATH} && ${COMPOSE} pull hevy-sync"

# 3. Restart
echo "[3/4] Restarting service..."
ssh "${SSH_TARGET}" "cd ${DEPLOY_PATH} && ${COMPOSE} up -d --force-recreate --wait"

# 4. Verify health
echo "[4/4] Verifying deploy..."
for i in 1 2 3 4 5; do
    HEALTH=$(ssh "${SSH_TARGET}" "curl -sf http://localhost:8400/health" 2>/dev/null) && break
    echo "  Attempt $i failed, retrying in 5s..."
    sleep 5
done

if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['status']=='healthy'" 2>/dev/null; then
    SYNCED=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('synced_workouts','?'))")
    echo ""
    echo "=== Deploy successful! ==="
    echo "  Synced workouts: ${SYNCED}"
    echo "  Health: ${HEALTH}"
else
    echo ""
    echo "=== WARNING: Health check failed ==="
    echo "  Response: ${HEALTH:-none}"
    echo "  Debug: ssh ${SSH_TARGET} 'cd ${DEPLOY_PATH} && docker compose logs --tail=50'"
    exit 1
fi
