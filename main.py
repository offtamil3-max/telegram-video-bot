import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import imageio_ffmpeg
import requests
from telethon import TelegramClient, Button, events

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")

CHUNK_SECONDS = 40
DOWNLOAD_REQUEST_SIZE = 512 * 1024
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not API_ID or not API_HASH:
    raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables are required")

API_ID = int(API_ID)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sessions = {}
locks = {}
active_users = set()


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Telegram video bot is running")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", port)


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    match = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
        result.stderr,
    )
    if not match:
        raise RuntimeError(
            f"Could not read video duration. ffmpeg output: {result.stderr[-1000:]}"
        )

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def fast_download(message, destination: Path, status_message) -> None:
    file_size = getattr(message.file, "size", None)
    if not message.media or not file_size:
        raise RuntimeError("Telegram media/file size is unavailable")

    logger.info(
        "Fast download started: %.1f MB using Telegram request size %d KB",
        file_size / 1024 / 1024,
        DOWNLOAD_REQUEST_SIZE // 1024,
    )

    progress = {"current": 0}
    started = time.monotonic()

    def progress_callback(current, total):
        progress["current"] = current

    async def progress_loop():
        last_text = ""
        while True:
            await asyncio.sleep(2)
            current = progress["current"]
            total = file_size or 1
            pct = min(100.0, current * 100.0 / total)
            elapsed = max(time.monotonic() - started, 0.1)
            speed = current / elapsed / 1024 / 1024
            remaining = max(total - current, 0)
            eta = remaining / max(speed * 1024 * 1024, 1)
            eta_text = f"~{int(eta)}s" if current else "..."
            text = (
                f"📥 Video download: {pct:.0f}%\n"
                f"{current / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB\n"
                f"⚡ {speed:.2f} MB/s • ETA {eta_text}"
            )
            if text != last_text:
                try:
                    await status_message.edit(text)
                    last_text = text
                except Exception:
                    pass

    progress_task = asyncio.create_task(progress_loop())
    try:
        await client.download_media(
            message,
            file=str(destination),
            progress_callback=progress_callback,
        )
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    elapsed = time.monotonic() - started
    written = destination.stat().st_size if destination.exists() else 0
    speed = written / max(elapsed, 0.001) / 1024 / 1024
    logger.info(
        "Fast download complete: %.1f MB in %.1fs (%.2f MB/s)",
        written / 1024 / 1024,
        elapsed,
        speed,
    )
    try:
        await status_message.edit(
            f"✅ Download முடிந்தது — {written / 1024 / 1024:.1f} MB in {elapsed:.1f}s"
        )
    except Exception:
        pass


def make_chunk(src: Path, dst: Path, start: float, duration: float) -> None:
    started = time.monotonic()

    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(src),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "24",
            "-threads",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(dst),
        ],
        check=True,
        timeout=900,
    )

    elapsed = time.monotonic() - started
    size_mb = dst.stat().st_size / 1024 / 1024
    logger.info(
        "Part created: %.1fs, %.2f MB, %.1fs processing time",
        duration,
        size_mb,
        elapsed,
    )


def send_via_bot_api(
    chat_id: int,
    output: Path,
    caption: str,
    has_next: bool,
    duration: float,
) -> None:
    keyboard = None
    if has_next:
        keyboard = {
            "inline_keyboard": [
                [{"text": "▶️ அடுத்து", "callback_data": "NEXT"}]
            ]
        }

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "supports_streaming": "true",
        "duration": str(max(1, math.ceil(duration))),
    }

    if keyboard:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)

    size_mb = output.stat().st_size / 1024 / 1024
    if size_mb >= 49:
        raise RuntimeError(
            f"Part is too large for Bot API upload: {size_mb:.2f} MB"
        )

    started = time.monotonic()
    logger.info("Bot API upload started: %.2f MB", size_mb)

    with output.open("rb") as video:
        response = requests.post(
            f"{BOT_API}/sendVideo",
            data=data,
            files={"video": (output.name, video, "video/mp4")},
            timeout=(30, 900),
        )

    if not response.ok:
        raise RuntimeError(
            f"Telegram Bot API HTTP {response.status_code}: {response.text[-1000:]}"
        )

    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram Bot API error: {payload}")

    elapsed = time.monotonic() - started
    logger.info(
        "Bot API upload complete: %.2f MB in %.1fs",
        size_mb,
        elapsed,
    )


def cleanup(user_id: int) -> None:
    session = sessions.pop(user_id, None)
    locks.pop(user_id, None)
    active_users.discard(user_id)

    if session:
        shutil.rmtree(session["dir"], ignore_errors=True)


def next_button():
    return [[Button.inline("▶️ அடுத்து", b"NEXT")]]


async def process_and_send(client: TelegramClient, user_id: int, index: int):
    session = sessions[user_id]
    start = index * CHUNK_SECONDS
    remaining = session["duration"] - start
    duration = min(CHUNK_SECONDS, remaining)
    output = Path(session["dir"]) / f"part_{index + 1}.mp4"

    await client.send_message(
        user_id,
        f"⏳ Part {index + 1}/{session['total']} தயாராகிறது...",
    )

    await asyncio.to_thread(
        make_chunk,
        Path(session["source"]),
        output,
        start,
        duration,
    )

    caption = (
        f"🎬 Part {index + 1}/{session['total']} • "
        f"{start:.0f}s–{start + duration:.0f}s"
    )

    await asyncio.to_thread(
        send_via_bot_api,
        user_id,
        output,
        caption,
        index + 1 < session["total"],
        duration,
    )

    output.unlink(missing_ok=True)
    session["index"] = index + 1

    if session["index"] >= session["total"]:
        cleanup(user_id)
        await client.send_message(user_id, "✅ வீடியோ முழுவதும் முடிந்தது.")


async def start_handler(event):
    await event.respond(
        "🎬 40-Second Video Splitter\n\n"
        "ஒரு வீடியோ அனுப்புங்கள். முதல் 40 seconds மட்டும் அனுப்பப்படும்.\n"
        "அடுத்த பகுதி வேண்டும் என்றால் ▶️ அடுத்து அழுத்துங்கள்.\n\n"
        "/cancel — தற்போதைய வீடியோவை நிறுத்த"
    )


async def cancel_handler(event):
    user_id = event.sender_id
    cleanup(user_id)
    await event.respond("✅ Current video cancelled and temporary files deleted.")


async def video_handler(event):
    message = event.message
    user_id = event.sender_id

    if not user_id:
        return

    if user_id in active_users:
        await event.respond(
            "⚠️ ஏற்கனவே ஒரு வீடியோ processing-ல் உள்ளது. /cancel பயன்படுத்தவும்."
        )
        return

    mime = getattr(message.file, "mime_type", None) if message.file else None
    if not mime or not mime.startswith("video/"):
        return

    active_users.add(user_id)

    temp_dir = tempfile.mkdtemp(prefix=f"video_{user_id}_")
    source = Path(temp_dir) / "video.mp4"
    sessions[user_id] = {"dir": temp_dir}

    download_status = await event.respond("📥 Video download தொடங்குகிறது...\n0%")

    try:
        await fast_download(message, source, download_status)

        if not source.exists() or source.stat().st_size == 0:
            raise RuntimeError("Telegram media download failed")

        duration = await asyncio.to_thread(ffprobe_duration, source)
        total = max(1, math.ceil(duration / CHUNK_SECONDS))

        sessions[user_id] = {
            "dir": temp_dir,
            "source": str(source),
            "duration": duration,
            "total": total,
            "index": 0,
        }
        locks[user_id] = asyncio.Lock()

        await event.respond(
            f"✅ {duration:.1f} seconds. மொத்தம் {total} parts. "
            "முதல் 40 seconds மட்டும் அனுப்புகிறேன்."
        )

        async with locks[user_id]:
            await process_and_send(client, user_id, 0)

    except Exception:
        logger.exception("Video processing failed for user %s", user_id)
        cleanup(user_id)
        await event.respond(
            "❌ Video process செய்ய முடியவில்லை. Server logs-ல் காரணத்தை சரிபார்க்கவும்."
        )


async def next_handler(event):
    await event.answer()
    user_id = event.sender_id

    session = sessions.get(user_id)
    if not session or "duration" not in session:
        await event.respond("❌ Active video இல்லை. புதிய video அனுப்புங்கள்.")
        return

    lock = locks.get(user_id)
    if not lock:
        await event.respond("❌ Processing session இல்லை. புதிய video அனுப்புங்கள்.")
        return

    async with lock:
        session = sessions.get(user_id)
        if not session or "duration" not in session:
            return

        index = session["index"]
        if index >= session["total"]:
            cleanup(user_id)
            await event.respond("✅ எல்லா parts-உம் ஏற்கனவே அனுப்பப்பட்டுவிட்டது.")
            return

        try:
            await process_and_send(client, user_id, index)
        except Exception:
            logger.exception("Next part failed for user %s", user_id)
            cleanup(user_id)
            await event.respond("❌ அடுத்த part உருவாக்க முடியவில்லை.")


client = TelegramClient("telegram_video_bot", API_ID, API_HASH)
client.add_event_handler(
    start_handler,
    events.NewMessage(pattern=r"^/start$", incoming=True),
)
client.add_event_handler(
    cancel_handler,
    events.NewMessage(pattern=r"^/(cancel|reset)$", incoming=True),
)
client.add_event_handler(video_handler, events.NewMessage(incoming=True))
client.add_event_handler(next_handler, events.CallbackQuery(data=b"NEXT"))


async def main():
    start_health_server()
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info(
        "Bot connected successfully: @%s",
        getattr(me, "username", "unknown"),
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
