#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="/home/ubuntu/trading_bot"
cd "$DEPLOY_DIR"

echo "=== Creating venv if needed ==="
if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

echo "=== Installing dependencies ==="
venv/bin/pip install --upgrade pip --quiet
venv/bin/pip install -r requirements.txt --quiet

echo "=== Creating logs directory ==="
mkdir -p logs

echo "=== Making start.sh executable ==="
chmod +x start.sh

echo "=== Restarting PM2 process ==="
pm2 delete trading-bot > /dev/null 2>&1 || true
pm2 start ecosystem.config.js

pm2 save

echo "=== Deploy complete ==="
pm2 status
