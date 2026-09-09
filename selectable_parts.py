import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import imageio_ffmpeg
import requests
from telethon import Button, TelegramClient, events

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_ROLE = os.environ.get("BOT_ROLE", "primary").lower()
CHUNK = 40
DOWNLOAD_REQUEST = 512 * 1024
DOWNLOAD_WORKERS = 2
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sessions, locks, active = {}, {}, set()


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


def find_tamil_audio(path):
    text = probe(path)
    tracks = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.search(r"Stream #0:(\d+)(?:\(([^)]+)\))?.*?:\s*Audio:\s*", line, re.I)
        if m:
            current = {"stream": int(m.group(1)), "lang": (m.group(2) or ""), "title": ""}
            tracks.append(current)
            continue
        if current and line.startswith("Metadata:"):
            continue
        if current and "title" in line.lower() and ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() == "title":
                current["title"] = value.strip()
        if current and line.startswith("Stream #"):
            current = None
    if not tracks:
        raise RuntimeError("No audio track found")
    tamil_re = re.compile(r"(?:^|[^a-z])(?:tamil|tam|ttam|ta)(?:$|[^a-z])", re.I)
    matches = [t for t in tracks if tamil_re.search(t["lang"]) or tamil_re.search(t["title"])]
    if not matches:
        raise RuntimeError("Tamil audio track not found")
    chosen = matches[0]
    print(f"Tamil audio selected: stream {chosen['stream']} lang={chosen['lang']!r} title={chosen['title']!r}")
    return chosen["stream"]


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


def mode_keyboard():
    return [
        [Button.inline("📺 Season / Episode", data=b"MODE:SEASON")],
        [Button.inline("🎬 Movie", data=b"MODE:MOVIE")],
        [Button.inline("🗑️ Cancel", data=b"CANCEL")],
    ]


def footer_confirm_keyboard():
    return [
        [Button.inline("✅ Confirm", data=b"FOOTER:OK"), Button.inline("✏️ Change", data=b"FOOTER:CHANGE")],
        [Button.inline("🗑️ Cancel", data=b"CANCEL")],
    ]


def part_title(s, i):
    n = i + 1
    if s.get("mode") == "MOVIE":
        return f"MOVIE PART {n}"
    return f"SEASON {s.get('season', 1)} EPISODE {s.get('episode', 1)} PART {n}"


def safe_draw_text(text):
    text = re.sub(r"[\r\n]+", " ", text).strip()
    return text[:180]


def escape_drawtext(text):
    return (safe_draw_text(text)
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'"))


async def _download_range(message, dest, start, end, size, state):
    chunk_count = math.ceil((end - start) / DOWNLOAD_REQUEST)
    written = 0
    with dest.open("r+b") as f:
        f.seek(start)
        async for chunk in client.iter_download(message, offset=start, limit=chunk_count, request_size=DOWNLOAD_REQUEST, chunk_size=DOWNLOAD_REQUEST, file_size=size):
            remaining = end - (start + written)
            data = chunk[:remaining]
            f.write(data)
            written += len(data)
            state["n"] += len(data)
            if start + written >= end:
                break
    if written != end - start:
        raise RuntimeError(f"Download range incomplete: {start}-{end}, got {written} bytes")


async def _parallel_download(message, dest, size, state):
    with dest.open("wb") as f:
        f.truncate(size)
    step = math.ceil(size / DOWNLOAD_WORKERS / DOWNLOAD_REQUEST) * DOWNLOAD_REQUEST
    ranges = []
    start = 0
    while start < size:
        end = min(size, start + step)
        ranges.append((start, end))
        start = end
    await asyncio.gather(*(_download_range(message, dest, a, b, size, state) for a, b in ranges))


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
            text = f"📥 Full video download\n{pct:.0f}% • {cur / 1024 / 1024:.1f} / {size / 1024 / 1024:.1f} MB\n⚡ {speed:.2f} MB/s • ETA ~{int(eta)}s\n🚀 {DOWNLOAD_WORKERS} parallel Telegram streams"
            if text != last:
                try:
                    await status.edit(text)
                    last = text
                except Exception:
                    pass
    task = asyncio.create_task(show())
    try:
        await _parallel_download(message, dest, size, state)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if not dest.exists() or dest.stat().st_size != size:
        raise RuntimeError("Downloaded video file is missing or incomplete")
    elapsed = max(time.monotonic() - started, 0.1)
    await status.edit(f"✅ Full video downloaded\n{size / 1024 / 1024:.1f} MB • {size / elapsed / 1024 / 1024:.2f} MB/s\n🚀 {DOWNLOAD_WORKERS} parallel Telegram streams")


def make_part(src, bg, out, start, length, overlay_text, footer_text, audio_stream):
    title = escape_drawtext(overlay_text)
    footer = escape_drawtext(footer_text)
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1[bg];"
        "[1:v]scale=1000:1780:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]drawtext=text='{title}':fontcolor=white:fontsize=58:box=1:boxcolor=black@0.65:boxborderw=18:x=(w-text_w)/2:y=45,"
        f"drawtext=text='{footer}':fontcolor=white:fontsize=42:box=1:boxcolor=black@0.65:boxborderw=14:x=(w-text_w)/2:y=h-text_h-55[v]"
    )
    subprocess.run([
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(bg),
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", f"1:{audio_stream}",
        "-sn", "-dn", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-threads", "0", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(out)
    ], check=True, timeout=900)


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


async def start(event):
    await event.respond("🎬 40-Second Video Splitter\n\n1️⃣ முதலில் Background Photo அனுப்புங்கள்.\n2️⃣ அடுத்து Video அனுப்புங்கள்.\n3️⃣ Season / Episode அல்லது Movie தேர்வு செய்யுங்கள்.\n4️⃣ கீழே வர வேண்டிய custom text-ஐ அனுப்பி Confirm செய்யுங்கள்.\n5️⃣ தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்.")


async def cancel(event):
    cleanup(event.sender_id)
    await event.respond("✅ Cancelled.")


async def photo(event):
    uid, msg = event.sender_id, event.message
    if not uid or not msg.photo:
        return
    if uid in active:
        await event.respond("⚠️ ஏற்கனவே ஒரு video processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
        return
    old = sessions.pop(uid, None)
    if old:
        shutil.rmtree(old.get("dir", ""), ignore_errors=True)
    d = tempfile.mkdtemp(prefix=f"video_{uid}_")
    bg = Path(d) / "background.jpg"
    try:
        await client.download_media(msg, file=str(bg))
        sessions[uid] = {"dir": d, "background": str(bg)}
        await event.respond("✅ Background photo saved.\n\n🎬 இப்போது Video அனுப்புங்கள்.")
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        print(f"Photo prepare error for {uid}: {type(e).__name__}: {e}")
        await event.respond("❌ Photo save செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


async def text(event):
    uid = event.sender_id
    s = sessions.get(uid)
    if not s or s.get("state") != "awaiting_footer":
        return
    value = (event.raw_text or "").strip()
    if not value:
        await event.respond("✏️ கீழே வர வேண்டிய text-ஐ அனுப்புங்கள்.")
        return
    s["footer_pending"] = value[:180]
    s["state"] = "confirm_footer"
    await event.respond(f"📝 கீழே வரும் text:\n\n{s['footer_pending']}\n\nஇதுதானா?", buttons=footer_confirm_keyboard())


async def video(event):
    uid, msg = event.sender_id, event.message
    if not uid or uid in active:
        if uid in active:
            await event.respond("⚠️ ஏற்கனவே ஒரு video processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
        return
    mime = getattr(msg.file, "mime_type", None) if msg.file else None
    if not mime or not mime.startswith("video/"):
        return
    s = sessions.get(uid)
    if not s or not s.get("background"):
        await event.respond("🖼️ முதலில் Background Photo அனுப்புங்கள்.")
        return
    active.add(uid)
    d = s["dir"]
    src = Path(d) / "video.mp4"
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    try:
        await download(msg, src, status)
        dur = await asyncio.to_thread(duration, src)
        audio_stream = await asyncio.to_thread(find_tamil_audio, src)
        total = max(1, math.ceil(dur / CHUNK))
        sessions[uid] = {
            "dir": d, "background": s["background"], "source": str(src),
            "duration": dur, "total": total, "audio_stream": audio_stream,
            "state": "choose_mode"
        }
        await event.respond(
            f"✅ Full video ready\n\n⏱️ Duration: {ts(dur)}\n🎧 Tamil audio selected\n🎬 Total parts: {total}\n\n👇 மேலே வர வேண்டிய label-ஐ தேர்வு செய்யுங்கள்:",
            buttons=mode_keyboard()
        )
    except Exception as e:
        cleanup(uid)
        print(f"Video prepare error for {uid}: {type(e).__name__}: {e}")
        await event.respond("❌ Tamil audio கண்டுபிடிக்க முடியவில்லை அல்லது video prepare செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


async def part(event):
    uid = event.sender_id
    data = event.data.decode()
    s = sessions.get(uid)
    if not s:
        await event.answer("❌ Active video இல்லை.")
        return
    if data == "CANCEL":
        await event.answer("Cancelled")
        cleanup(uid)
        await event.respond("✅ Cancelled.")
        return
    if data.startswith("MODE:"):
        if s.get("state") != "choose_mode":
            await event.answer("இந்த video-க்கு mode ஏற்கனவே தேர்வு செய்யப்பட்டது.")
            return
        s["mode"] = data.split(":", 1)[1]
        s["season"] = 1
        s["episode"] = 1
        s["state"] = "awaiting_footer"
        label = "MOVIE PART 1" if s["mode"] == "MOVIE" else "SEASON 1 EPISODE 1 PART 1"
        await event.answer("✅ Selected")
        await event.edit(f"✅ Selected: {label}\n\n📝 இந்த video-வின் கீழே என்ன text வர வேண்டும்?\nText-ஐ ஒரு message-ஆ அனுப்புங்கள்.", buttons=[[Button.inline("🗑️ Cancel", data=b"CANCEL")]])
        return
    if data == "FOOTER:CHANGE":
        s["state"] = "awaiting_footer"
        await event.answer("Change")
        await event.edit("✏️ புதிய கீழ் text-ஐ அனுப்புங்கள்.", buttons=[[Button.inline("🗑️ Cancel", data=b"CANCEL")]])
        return
    if data == "FOOTER:OK":
        if s.get("state") != "confirm_footer":
            await event.answer("முதலில் text அனுப்புங்கள்.")
            return
        s["footer"] = s.pop("footer_pending")
        s["state"] = "ready"
        locks[uid] = asyncio.Lock()
        await event.answer("Confirmed")
        await event.edit(
            f"✅ Setup complete\n\n⬆️ {part_title(s, 0)}\n⬇️ {s['footer']}\n\n👇 தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்:",
            buttons=all_keyboard(s["total"], s["duration"])
        )
        return
    if data == "VIEWALL":
        if s.get("state") != "ready":
            await event.answer("முதலில் setup முடிக்கவும்.")
            return
        await event.answer("All parts")
        await event.edit(buttons=all_keyboard(s["total"], s["duration"]))
        return
    if not data.startswith("PART:"):
        return
    await event.answer("⏳ Part தயாராகிறது...")
    if s.get("state") != "ready":
        await event.respond("❌ முதலில் setup complete செய்யுங்கள்.")
        return
    try:
        i = int(data.split(":")[1])
    except Exception:
        return
    if i < 0 or i >= s["total"]:
        return
    async with locks[uid]:
        s = sessions.get(uid)
        if not s:
            return
        a = i * CHUNK
        length = min(CHUNK, s["duration"] - a)
        out = Path(s["dir"]) / f"part_{i + 1}.mp4"
        try:
            await event.edit(buttons=next_keyboard(s["total"], s["duration"], i))
            await event.respond(f"⏳ {part_title(s, i)} தயாராகிறது...\n🕐 {ts(a)} → {ts(a + length)}")
            await asyncio.to_thread(make_part, Path(s["source"]), Path(s["background"]), out, a, length, part_title(s, i), s["footer"], s["audio_stream"])
            await asyncio.to_thread(send_part, uid, out, f"🎬 {part_title(s, i)} • {ts(a)} → {ts(a + length)}")
            out.unlink(missing_ok=True)
            await event.edit(buttons=next_keyboard(s["total"], s["duration"], i))
        except Exception as e:
            out.unlink(missing_ok=True)
            print(f"Part error for {uid}: {type(e).__name__}: {e}")
            await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


client = TelegramClient("telegram_video_bot", API_ID, API_HASH)
client.add_event_handler(start, events.NewMessage(pattern=r"^/start$", incoming=True))
client.add_event_handler(cancel, events.NewMessage(pattern=r"^/(cancel|reset)$", incoming=True))
client.add_event_handler(photo, events.NewMessage(incoming=True))
client.add_event_handler(text, events.NewMessage(incoming=True))
client.add_event_handler(video, events.NewMessage(incoming=True))
client.add_event_handler(part, events.CallbackQuery(data=re.compile(rb"^(PART:\d+|VIEWALL|CANCEL|MODE:(SEASON|MOVIE)|FOOTER:(OK|CHANGE))$")))


async def main():
    asyncio.create_task(asyncio.to_thread(health))
    if BOT_ROLE == "standby":
        while True:
            await asyncio.sleep(86400)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    print(f"Bot connected: @{getattr(me, 'username', 'unknown')}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
