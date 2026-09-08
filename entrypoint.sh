#!/bin/bash
set -euo pipefail

TG_API_ID="${TELEGRAM_API_ID:-}"
TG_API_HASH="${TELEGRAM_API_HASH:-}"

if [ -z "$TG_API_ID" ] || [ -z "$TG_API_HASH" ]; then
    echo "ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set" >&2
    exit 1
fi

echo "Starting Local Bot API Server..."
tg_bot_api \
    --api-id="$TG_API_ID" \
    --api-hash="$TG_API_HASH" \
    --local \
    --http-port 8081 \
    --dir /app/tg_bot_api_data \
    --log /app/tg_bot_api_data/tg_bot_api.log &

TG_BOT_API_PID=$!

echo "Waiting for Local Bot API Server to become ready..."
READY=0
for i in $(seq 1 30); do
    if curl -sf "http://localhost:8081/test/ping" >/dev/null 2>&1 || curl -s -o /dev/null -w "%{http_code}" "http://localhost:8081" 2>/dev/null | grep -qE "^[0-9]{3}$"; then
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    echo "WARNING: Local Bot API Server did not respond within 30s, continuing anyway..."
fi

cleanup() {
    kill "$TG_BOT_API_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting Telegram bot..."
python main.py
