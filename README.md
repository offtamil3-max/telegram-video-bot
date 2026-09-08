# Telegram 40-Second Video Splitter

Telegram bot that sends a video in sequential 40-second parts. Only the first part is sent automatically; every later part is sent only after pressing **▶️ அடுத்து**.

## Railway

1. Create a Railway service from this GitHub repository.
2. Add environment variable `BOT_TOKEN` with your BotFather token. Never commit the token to GitHub.
3. Deploy. FFmpeg is installed by the Dockerfile.

## Commands

- `/start` — instructions
- `/cancel` — cancel the current video and remove temporary files
- `/reset` — same as cancel

## Important Telegram file limits

Standard Telegram Bot API limits may prevent very large videos from being downloaded by the bot. For very large inputs, use a Telegram Local Bot API Server or another supported large-file architecture.

To remove the 20MB download limit, run a `telegram-local-api` service (e.g. `aiogram/telegram-bot-api:latest` with `TELEGRAM_LOCAL=1`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` configured) and set `TELEGRAM_LOCAL_API_SERVER` on this bot service to its private Railway address, e.g. `http://telegram-local-api.railway.internal:8081`. When set, all Bot API calls (including `get_file` and `send_video`) are routed through the local server automatically.

## Healthcheck

This bot uses long-polling and exposes no HTTP server, so Railway's service healthcheck path must be left unset (`healthcheckPath: null`). A healthcheck against `GET /` will fail even though the bot is running correctly — startup success is confirmed via the deploy logs instead.
