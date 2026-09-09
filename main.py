import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN")
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")
CHUNK_SECONDS = 40

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sessions = {}
locks = {}


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
        check=True, timeout=600,
    )


def cleanup(user_id: int) -> None:
    session = sessions.pop(user_id, None)
    locks.pop(user_id, None)
    if session:
        shutil.rmtree(session["dir"], ignore_errors=True)


def keyboard(has_next: bool):
    if not has_next:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ அடுத்து", callback_data="NEXT")]])


async def download_local_file(file_path: str, destination: Path) -> None:
    if not LOCAL_BOT_API_URL:
        raise RuntimeError("LOCAL_BOT_API_URL is not configured")
    if file_path.startswith(("http://", "https://")):
        url = file_path
    else:
        relative_path = quote(file_path.lstrip("/"), safe="/")
        url = f"{LOCAL_BOT_API_URL}/file/bot{BOT_TOKEN}/{relative_path}"
    timeout = httpx.Timeout(connect=120.0, read=900.0, write=120.0, pool=120.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    out.write(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 40-Second Video Splitter\n\n"
        "ஒரு வீடியோ அனுப்புங்கள். முதல் 40 seconds மட்டும் அனுப்பப்படும்.\n"
        "அடுத்த பகுதி வேண்டும் என்றால் ▶️ அடுத்து அழுத்துங்கள்.\n\n"
        "/cancel — தற்போதைய வீடியோவை நிறுத்த"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cleanup(user_id)
    await update.message.reply_text("✅ Current video cancelled and temporary files deleted.")


async def process_and_send(user_id: int, bot, index: int):
    session = sessions[user_id]
    start = index * CHUNK_SECONDS
    remaining = session["duration"] - start
    duration = min(CHUNK_SECONDS, remaining)
    output = Path(session["dir"]) / f"part_{index + 1}.mp4"

    await bot.send_message(user_id, f"⏳ Part {index + 1}/{session['total']} தயாராகிறது...")
    await asyncio.to_thread(make_chunk, Path(session["source"]), output, start, duration)

    with output.open("rb") as f:
        await bot.send_video(
            chat_id=user_id,
            video=InputFile(f, filename=f"part_{index + 1}.mp4"),
            supports_streaming=True,
            caption=f"🎬 Part {index + 1}/{session['total']} • {start:.0f}s–{start + duration:.0f}s",
            reply_markup=keyboard(index + 1 < session["total"]),
        )

    output.unlink(missing_ok=True)
    session["index"] = index + 1
    if session["index"] >= session["total"]:
        cleanup(user_id)
        await bot.send_message(user_id, "✅ வீடியோ முழுவதும் முடிந்தது.")


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in sessions:
        await update.message.reply_text("⚠️ ஏற்கனவே ஒரு வீடியோ processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
        return

    msg = update.message
    if not msg or not (msg.video or (msg.document and (msg.document.mime_type or "").startswith("video/"))):
        if msg:
            await msg.reply_text("❌ Video file மட்டும் அனுப்புங்கள்.")
        return

    temp_dir = tempfile.mkdtemp(prefix=f"video_{user_id}_")
    source = Path(temp_dir) / "video.mp4"
    await msg.reply_text("📥 Video download செய்கிறேன்...")
    try:
        file_id = msg.video.file_id if msg.video else msg.document.file_id
        tg_file = await context.bot.get_file(
            file_id, read_timeout=900, connect_timeout=120, write_timeout=120, pool_timeout=120,
        )
        if LOCAL_BOT_API_URL and tg_file.file_path:
            await download_local_file(tg_file.file_path, source)
        else:
            await tg_file.download_to_drive(custom_path=str(source))

        duration = await asyncio.to_thread(ffprobe_duration, source)
        total = max(1, int((duration + CHUNK_SECONDS - 1) // CHUNK_SECONDS))
        sessions[user_id] = {
            "dir": temp_dir, "source": str(source), "duration": duration,
            "total": total, "index": 0,
        }
        locks[user_id] = asyncio.Lock()
        await msg.reply_text(f"✅ {duration:.1f} seconds. மொத்தம் {total} parts. முதல் 40 seconds மட்டும் அனுப்புகிறேன்.")
        async with locks[user_id]:
            await process_and_send(user_id, context.bot, 0)
    except Exception:
        logger.exception("Video processing failed")
        cleanup(user_id)
        await msg.reply_text("❌ Video process செய்ய முடியவில்லை. Local Bot API connection அல்லது video download-ஐ சரிபார்க்க வேண்டும்.")


async def next_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    session = sessions.get(user_id)
    if not session:
        await query.message.reply_text("❌ Active video இல்லை. புதிய video அனுப்புங்கள்.")
        return
    lock = locks[user_id]
    async with lock:
        session = sessions.get(user_id)
        if not session:
            return
        index = session["index"]
        if index >= session["total"]:
            cleanup(user_id)
            await query.message.reply_text("✅ எல்லா parts-உம் ஏற்கனவே அனுப்பப்பட்டுவிட்டது.")
            return
        await process_and_send(user_id, context.bot, index)


def main():
    # Use the official Telegram Bot API for normal bot operations.
    # Use the Local Bot API only for downloading the large file from its local storage.
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reset", cancel))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(CallbackQueryHandler(next_callback, pattern="^NEXT$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
