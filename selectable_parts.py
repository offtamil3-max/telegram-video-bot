import asyncio
import math
import os
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import imageio_ffmpeg
import requests
from telethon import Button, TelegramClient

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_ROLE = os.environ.get("BOT_ROLE", "primary").lower()
CHUNK = 40
DOWNLOAD_REQUEST = 512 * 1024
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sessions, locks, active = {}, {}, set()
client = TelegramClient("telegram_video_bot", API_ID, API_HASH)


def health():
    port = int(os.environ.get("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            return
    HTTPServer(("0.0.0.0", port), H).serve_forever()


def probe(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, check=False, timeout=60)
    return r.stderr


def duration(path):
    text = probe(path)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise RuntimeError("Could not read video duration")
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def ts(s):
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def all_keyboard(total, dur):
    rows = []
    for i in range(total):
        a, b = i * CHUNK, min(dur, (i + 1) * CHUNK)
        rows.append([Button.inline(f"🎬 Part {i + 1} • {ts(a)} → {ts(b)}", data=f"PART:{i}".encode())])
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


def next_keyboard(total, dur, current):
    rows = []
    nxt = current + 1
    if nxt < total:
        a, b = nxt * CHUNK, min(dur, (nxt + 1) * CHUNK)
        rows.append([Button.inline(f"🎬 Part {nxt + 1} • {ts(a)} → {ts(b)}", data=f"PART:{nxt}".encode())])
    rows.append([Button.inline("📋 View All", data=b"VIEWALL")])
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


async def download(message, dest, status):
    size = getattr(message.file, "size", 0) or 0
    if size <= 0:
        raise RuntimeError("Telegram did not provide the video size")
    state = {"n": 0}
    started = time.monotonic()

    async def show():
        last = ""
        while True:
            await asyncio.sleep(2)
            cur = min(state["n"], size)
            pct = cur * 100 / size
            elapsed = max(time.monotonic() - started, 0.1)
            speed = cur / elapsed / 1024 / 1024
            eta = (size - cur) / max(speed * 1024 * 1024, 1)
            text = f"📥 Full video download\n{pct:.0f}% • {cur/1024/1024:.1f} / {size/1024/1024:.1f} MB\n⚡ {speed:.2f} MB/s • ETA ~{int(eta)}s"
            if text != last:
                try:
                    await status.edit(text)
                    last = text
                except Exception:
                    pass

    task = asyncio.create_task(show())
    try:
        with dest.open("wb") as f:
            async for chunk in client.iter_download(message.media, request_size=DOWNLOAD_REQUEST):
                f.write(chunk)
                state["n"] += len(chunk)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if not dest.exists() or dest.stat().st_size != size:
        raise RuntimeError("Downloaded video file is missing or incomplete")
    elapsed = max(time.monotonic() - started, 0.1)
    await status.edit(f"✅ Full video downloaded\n{size/1024/1024:.1f} MB • {size/elapsed/1024/1024:.2f} MB/s")


def make_part_clean(src, out, start, length, audio_stream):
    common = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", f"0:{audio_stream}", "-sn", "-dn",
    ]
    # Best quality: copy the original video/audio streams without re-encoding.
    r = subprocess.run(common + ["-c", "copy", "-movflags", "+faststart", str(out)], capture_output=True, text=True, timeout=900)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 1024:
        print(f"{out.name}: stream copy OK")
        return

    # If the selected audio cannot be copied to MP4, keep the original video and encode only audio.
    out.unlink(missing_ok=True)
    r = subprocess.run(common + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)], capture_output=True, text=True, timeout=900)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 1024:
        print(f"{out.name}: video copy + audio encode OK")
        return

    # Last resort only: re-encode the selected part at high quality.
    out.unlink(missing_ok=True)
    subprocess.run(common + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)], check=True, timeout=900)
    print(f"{out.name}: full re-encode fallback")


def send_part(chat, out, caption):
    mb = out.stat().st_size / 1024 / 1024
    if mb >= 49:
        raise RuntimeError(f"Part too large: {mb:.2f} MB")
    with out.open("rb") as f:
        r = requests.post(f"{BOT_API}/sendVideo", data={"chat_id": str(chat), "caption": caption, "supports_streaming": "true"}, files={"video": (out.name, f, "video/mp4")}, timeout=(30, 900))
    if not r.ok or not r.json().get("ok"):
        raise RuntimeError(r.text[-1000:])


def cleanup(uid):
    s = sessions.pop(uid, None)
    locks.pop(uid, None)
    active.discard(uid)
    if s:
        shutil.rmtree(s["dir"], ignore_errors=True)
