# Telegram 40-Second Video Splitter

Telegram bot that sends a video in sequential 40-second parts. Only the first part is sent automatically; every later part is sent only after pressing **▶️ அடுத்து**.

## Railway

1. Create a Railway service from this GitHub repository.
2. Add environment variables `BOT_TOKEN`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`. Never commit these to GitHub.
3. Deploy. FFmpeg, TDLib, and the Local Bot API Server binary are built and installed by the Dockerfile.

## Commands

- `/start` — instructions
- `/cancel` — cancel the current video and remove temporary files
- `/reset` — same as cancel

## Local Bot API Server

This bot runs a Local Bot API Server (built from TDLib) inside the container, listening on `http://localhost:8081`. `entrypoint.sh` starts the local server first, waits for it to become ready, and then starts `main.py`, which points `python-telegram-bot` at the local server via `API_SERVER_URL` (defaults to `http://localhost:8081`). This bypasses the 20 MB file size limit imposed by Telegram's cloud Bot API, since files are downloaded directly to the container filesystem.
