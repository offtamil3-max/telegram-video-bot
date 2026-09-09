import asyncio
import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telethon import TelegramClient, Button, events

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("TELEGRAM_API_ID")
API_HASH = os.environ.get("TELEGRAM_API_HASH")
CHUNK_SECONDS = 40

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not API_ID or not API_HASH:
    raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH environment variables are required")

API_ID = int(API_ID)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sessions = {}
locks = {}


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
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return float(result.stdout.strip())


def make_chunk(src: Path, dst: Path, start: float, duration: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
         "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)],
        check=True, timeout=900,
    )


def cleanup(user_id: int) -> None:
    session = sessions.pop(user_id, None)
    locks.pop(user_id, None)
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

    await client.send_message(user_id, f"⏳ Part {index + 1}/{session['total']} தயாராகிறது...")
    await asyncio.to_thread(make_chunk, Path(session["source"]), output, start, duration)

    buttons = next_button() if index + 1 < session["total"] else None
    await client.send_file(
        user_id,
        str(output),
        force_document=False,
        supports_streaming=True,
        caption=f"🎬 Part {index + 1}/{session['total']} • {start:.0f}s–{start + duration:.0f}s",
        buttons=buttons,
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

    if user_id in sessions:
        await event.respond("⚠️ ஏற்கனவே ஒரு வீடியோ processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
        return

    mime = getattr(message.file, "mime_type", None) if message.file else None
    if not mime or not mime.startswith("video/"):
        return

    temp_dir = tempfile.mkdtemp(prefix=f"video_{user_id}_")
    source = Path(temp_dir) / "video.mp4"
    await event.respond("📥 Video download செய்கிறேன்...")

    try:
        # Telethon uses Telegram's MTProto file transfer directly, avoiding the
        # hosted Bot API file-download limit that caused the Railway failures.
        downloaded = await message.download_media(file=str(source))
        if not downloaded or not source.exists() or source.stat().st_size == 0:
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

        await event.respond(f"✅ {duration:.1f} seconds. மொத்தம் {total} parts. முதல் 40 seconds மட்டும் அனுப்புகிறேன்.")
        async with locks[user_id]:
            await process_and_send(client, user_id, 0)
    except Exception:
        logger.exception("Video processing failed for user %s", user_id)
        cleanup(user_id)
        await event.respond("❌ Video process செய்ய முடியவில்லை. Server logs-ல் காரணத்தை சரிபார்க்கவும்.")


async def next_handler(event):
    await event.answer()
    user_id = event.sender_id
    session = sessions.get(user_id)
    if not session:
        await event.respond("❌ Active video இல்லை. புதிய video அனுப்புங்கள்.")
        return

    lock = locks[user_id]
    async with lock:
        session = sessions.get(user_id)
        if not session:
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

client.add_event_handler(start_handler, events.NewMessage(pattern=r"^/start$", incoming=True))
client.add_event_handler(cancel_handler, events.NewMessage(pattern=r"^/(cancel|reset)$", incoming=True))
client.add_event_handler(video_handler, events.NewMessage(incoming=True))
client.add_event_handler(next_handler, events.CallbackQuery(data=b"NEXT"))


async def main():
    start_health_server()
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info("Bot connected successfully: @%s", getattr(me, "username", "unknown"))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
