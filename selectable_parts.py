import asyncio
import json
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
from telethon import TelegramClient, events

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_ROLE = os.environ.get("BOT_ROLE", "primary").lower()
CHUNK = 40
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sessions, locks, active = {}, {}, set()


def health():
    port = int(os.environ.get("PORT", "10000"))
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self, *args): return
    HTTPServer(("0.0.0.0", port), H).serve_forever()


def duration(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True, check=False, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m: raise RuntimeError("Could not read video duration")
    h, mnt, sec = m.groups(); return int(h)*3600 + int(mnt)*60 + float(sec)


def ts(s):
    s = max(0, int(round(s))); return f"{s//60}:{s%60:02d}"


def keyboard(total, dur):
    rows = []
    for i in range(total):
        a, b = i*CHUNK, min(dur, (i+1)*CHUNK)
        rows.append([{"text": f"🎬 Part {i+1} • {ts(a)} → {ts(b)}", "callback_data": f"PART:{i}"}])
    rows.append([{"text": "🗑️ Cancel", "callback_data": "CANCEL"}])
    return rows


async def download(message, dest, status):
    size = getattr(message.file, "size", 0) or 1
    state = {"n": 0}; started = time.monotonic()
    def cb(cur, total): state["n"] = cur
    async def show():
        last = ""
        while True:
            await asyncio.sleep(2); cur = state["n"]; pct = cur*100/size
            elapsed = max(time.monotonic()-started, .1); speed = cur/elapsed/1024/1024
            eta = (size-cur)/max(speed*1024*1024, 1)
            text = f"📥 Full video download\n{pct:.0f}% • {cur/1024/1024:.1f} / {size/1024/1024:.1f} MB\n⚡ {speed:.2f} MB/s • ETA ~{int(eta)}s"
            if text != last:
                try: await status.edit(text); last = text
                except Exception: pass
    task = asyncio.create_task(show())
    try: await client.download_media(message, file=str(dest), progress_callback=cb)
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    actual = dest.stat().st_size; elapsed = max(time.monotonic()-started, .1)
    await status.edit(f"✅ Full video downloaded\n{actual/1024/1024:.1f} MB • {actual/elapsed/1024/1024:.2f} MB/s")


def make_part(src, out, start, length):
    subprocess.run([FFMPEG,"-y","-hide_banner","-loglevel","error","-ss",f"{start:.3f}","-i",str(src),"-t",f"{length:.3f}","-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","ultrafast","-crf","24","-threads","0","-c:a","aac","-b:a","128k","-movflags","+faststart",str(out)], check=True, timeout=900)


def send_part(chat, out, caption):
    mb = out.stat().st_size/1024/1024
    if mb >= 49: raise RuntimeError(f"Part too large: {mb:.2f} MB")
    with out.open("rb") as f:
        r = requests.post(f"{BOT_API}/sendVideo", data={"chat_id":str(chat),"caption":caption,"supports_streaming":"true"}, files={"video":(out.name,f,"video/mp4")}, timeout=(30,900))
    if not r.ok or not r.json().get("ok"): raise RuntimeError(r.text[-1000:])


def cleanup(uid):
    s = sessions.pop(uid, None); locks.pop(uid, None); active.discard(uid)
    if s: shutil.rmtree(s["dir"], ignore_errors=True)


async def start(event):
    await event.respond("🎬 40-Second Video Splitter\n\nVideo அனுப்புங்கள். Full video download ஆனதும் எல்லா Parts-உம் button-ஆக வரும். ஒவ்வொரு button-லும் M:SS → M:SS நேரம் இருக்கும். நீங்கள் அழுத்தும் Part மட்டும் உருவாக்கி அனுப்பப்படும்.")


async def cancel(event):
    cleanup(event.sender_id); await event.respond("✅ Cancelled.")


async def video(event):
    uid, msg = event.sender_id, event.message
    if not uid: return
    if uid in active:
        await event.respond("⚠️ ஏற்கனவே ஒரு video processing-ல் உள்ளது. /cancel பயன்படுத்தவும்."); return
    mime = getattr(msg.file, "mime_type", None) if msg.file else None
    if not mime or not mime.startswith("video/"): return
    active.add(uid); d = tempfile.mkdtemp(prefix=f"video_{uid}_"); src = Path(d)/"video.mp4"; sessions[uid]={"dir":d}
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    try:
        await download(msg, src, status); dur = await asyncio.to_thread(duration, src); total = max(1, math.ceil(dur/CHUNK))
        sessions[uid]={"dir":d,"source":str(src),"duration":dur,"total":total}; locks[uid]=asyncio.Lock()
        await event.respond(f"✅ Full video ready\n\n⏱️ Duration: {ts(dur)}\n🎬 Total parts: {total}\n\n👇 தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்:", buttons=keyboard(total,dur))
    except Exception:
        cleanup(uid); await event.respond("❌ Video prepare செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


async def part(event):
    await event.answer("⏳ Part தயாராகிறது..."); uid = event.sender_id; s = sessions.get(uid)
    if not s or "duration" not in s: await event.respond("❌ Active video இல்லை."); return
    data = event.data.decode()
    if data == "CANCEL": cleanup(uid); await event.respond("✅ Cancelled."); return
    try: i = int(data.split(":")[1])
    except Exception: return
    if i < 0 or i >= s["total"]: return
    async with locks[uid]:
        s = sessions.get(uid)
        if not s: return
        a=i*CHUNK; length=min(CHUNK,s["duration"]-a); out=Path(s["dir"])/f"part_{i+1}.mp4"
        try:
            await event.respond(f"⏳ Part {i+1}/{s['total']} தயாராகிறது...\n🕐 {ts(a)} → {ts(a+length)}")
            await asyncio.to_thread(make_part, Path(s["source"]), out, a, length)
            await asyncio.to_thread(send_part, uid, out, f"🎬 Part {i+1}/{s['total']} • {ts(a)} → {ts(a+length)}")
            out.unlink(missing_ok=True)
            await event.respond("✅ Part அனுப்பப்பட்டது. வேறு Part வேண்டுமென்றால் கீழே தேர்வு செய்யுங்கள்.", buttons=keyboard(s["total"],s["duration"]))
        except Exception:
            out.unlink(missing_ok=True); await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


client=TelegramClient("telegram_video_bot",API_ID,API_HASH)
client.add_event_handler(start,events.NewMessage(pattern=r"^/start$",incoming=True))
client.add_event_handler(cancel,events.NewMessage(pattern=r"^/(cancel|reset)$",incoming=True))
client.add_event_handler(video,events.NewMessage(incoming=True))
client.add_event_handler(part,events.CallbackQuery(data=re.compile(rb"^(PART:\d+|CANCEL)$")))


async def main():
    asyncio.create_task(asyncio.to_thread(health))
    if BOT_ROLE == "standby":
        while True: await asyncio.sleep(86400)
    await client.start(bot_token=BOT_TOKEN)
    me=await client.get_me(); print(f"Bot connected: @{getattr(me,'username','unknown')}")
    await client.run_until_disconnected()

if __name__ == "__main__": asyncio.run(main())
