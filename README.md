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
