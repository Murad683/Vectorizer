#!/bin/bash
# Manual deploy of Vectorizer on the VPS. Run as `<deploy-user>`:
#   bash /opt/vectorizer/deploy/deploy.sh            # deploys origin/main
#   BRANCH=deploy/contabo bash /opt/vectorizer/deploy/deploy.sh
# Touches only the "vectorizer" compose project. No global docker prune.
set -euo pipefail

cd /opt/vectorizer
BRANCH="${BRANCH:-main}"
C="docker compose -p vectorizer -f docker-compose.prod.yml"

[ -f .env ] || { echo "ERROR: /opt/vectorizer/.env is missing"; exit 1; }

echo "== git ($BRANCH) =="
git fetch --prune origin
git reset --hard "origin/$BRANCH"

echo "== keep previous backend image for rollback =="
docker tag vectorizer-backend:latest vectorizer-backend:rollback 2>/dev/null || true

echo "== build & up =="
$C up -d --build

echo "== wait for health =="
for i in $(seq 1 45); do
  if curl -sf -m 5 http://127.0.0.1:4300/api/health >/dev/null; then break; fi
  sleep 2
done

echo "== health =="
curl -sf -m 10 http://127.0.0.1:4300/api/health && echo " <- OK"
$C ps
echo "== deploy done =="
