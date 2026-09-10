import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import selectable_parts
from telethon import Button, events

FFPROBE = shutil.which("ffprobe") or selectable_parts.FFMPEG.replace("ffmpeg", "ffprobe")


def probe_json(path):
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("Could not inspect video")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError("Could not parse video metadata") from e


def duration(path):
    data = probe_json(path)
    value = data.get("format", {}).get("duration")
    if value:
        return float(value)
    values = [float(s["duration"]) for s in data.get("streams", []) if s.get("codec_type") == "video" and s.get("duration")]
    if values:
        return max(values)
    raise RuntimeError("Could not read video duration")


def list_audio_tracks(path):
    data = probe_json(path)
    tracks = []
    for s in data.get("streams", []):
        if s.get("codec_type") != "audio":
            continue
        tags = s.get("tags") or {}
        title = str(tags.get("title") or "").strip()
        lang = str(tags.get("language") or "").strip()
        label = title or lang or "\u200b"
        tracks.append({"stream": s.get("index"), "title": title, "lang": lang, "label": label})
    if not tracks:
        raise RuntimeError("No audio track found")
    print("Audio tracks:", [(t["stream"], t["label"]) for t in tracks])
    return tracks


def audio_keyboard(tracks):
    rows = [[Button.inline(t["label"], data=f"AUDIO:{i}".encode())] for i, t in enumerate(tracks)]
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


async def start_manual(event):
    selectable_parts.cleanup(event.sender_id)
    await event.respond("🎬 40-Second Video Splitter\n\n📤 உங்கள் Video-வை அனுப்புங்கள்.\n\n🎧 Audio track-ஐ நீங்கள் manually தேர்வு செய்யலாம்.\n🎬 பிறகு தேவையான 40-second Part-ஐ தேர்வு செய்யலாம்.\n\n❌ Background / watermark / text எதுவும் சேர்க்கப்படாது.")


async def cancel_manual(event):
    selectable_parts.cleanup(event.sender_id)
    await event.respond("✅ Cancelled. புதிய Video அனுப்பலாம்.")


async def video_manual(event):
    uid, msg = event.sender_id, event.message
    if not uid:
        return
    mime = getattr(msg.file, "mime_type", None) if msg.file else None
    if not mime or not mime.startswith("video/"):
        return
    if uid in selectable_parts.active:
        await event.respond("⚠️ இந்த Video இன்னும் processing-ல் உள்ளது.\n/cancel பயன்படுத்தவும்.")
        return

    old = selectable_parts.sessions.pop(uid, None)
    if old:
        shutil.rmtree(old.get("dir", ""), ignore_errors=True)
    d = Path(tempfile.mkdtemp(prefix=f"video_{uid}_"))
    src = d / "video_source"
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    selectable_parts.active.add(uid)
    try:
        await selectable_parts.download(msg, src, status)
        dur = await asyncio.to_thread(duration, src)
        tracks = await asyncio.to_thread(list_audio_tracks, src)
        total = max(1, math.ceil(dur / selectable_parts.CHUNK))
        selectable_parts.sessions[uid] = {
            "dir": str(d),
            "source": str(src),
            "duration": dur,
            "total": total,
            "audio_tracks": tracks,
            "state": "choose_audio",
        }
        await status.edit(
            f"✅ Full video ready\n\n⏱️ Duration: {selectable_parts.ts(dur)}\n🎬 Total parts: {total}\n🎧 Audio tracks: {len(tracks)}\n\n👇 File-ல் இருக்கும் track name / language-ஐ அப்படியே தேர்வு செய்யுங்கள்:",
            buttons=audio_keyboard(tracks),
        )
    except Exception as e:
        selectable_parts.cleanup(uid)
        print(f"Video prepare error for {uid}: {type(e).__name__}: {e}")
        try:
            await status.edit("❌ Video prepare செய்ய முடியவில்லை. File format / audio tracks check செய்யுங்கள்.")
        except Exception:
            await event.respond("❌ Video prepare செய்ய முடியவில்லை. File format / audio tracks check செய்யுங்கள்.")
    finally:
        selectable_parts.active.discard(uid)


async def callback_manual(event):
    uid = event.sender_id
    data = event.data.decode()
    s = selectable_parts.sessions.get(uid)
    if not s:
        await event.answer("❌ Active video இல்லை.")
        return
    if data == "CANCEL":
        await event.answer("Cancelled")
        selectable_parts.cleanup(uid)
        await event.respond("✅ Cancelled.")
        return
    if data.startswith("AUDIO:"):
        if s.get("state") != "choose_audio":
            await event.answer("Audio ஏற்கனவே தேர்வு செய்யப்பட்டது.")
            return
        try:
            track = s["audio_tracks"][int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await event.answer("Invalid audio")
            return
        s["audio_stream"] = track["stream"]
        s["audio_label"] = track["label"]
        s["state"] = "ready"
        selectable_parts.locks[uid] = asyncio.Lock()
        await event.answer("✅ Audio selected")
        await event.edit(
            f"🎧 Audio selected\n\n🎬 {s['total']} parts ready.\n👇 தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்:",
            buttons=selectable_parts.all_keyboard(s["total"], s["duration"]),
        )
        return
    if data == "VIEWALL":
        if s.get("state") != "ready":
            await event.answer("முதலில் Audio track தேர்வு செய்யுங்கள்.")
            return
        await event.answer("All parts")
        await event.edit(buttons=selectable_parts.all_keyboard(s["total"], s["duration"]))
        return
    if not data.startswith("PART:"):
        return
    if s.get("state") != "ready":
        await event.answer("முதலில் Audio track தேர்வு செய்யுங்கள்.")
        return
    try:
        i = int(data.split(":", 1)[1])
    except ValueError:
        await event.answer("Invalid part")
        return
    if i < 0 or i >= s["total"]:
        await event.answer("Invalid part")
        return

    await event.answer("⏳ Preparing...")
    lock = selectable_parts.locks.setdefault(uid, asyncio.Lock())
    async with lock:
        s = selectable_parts.sessions.get(uid)
        if not s or s.get("state") != "ready":
            return
        start = i * selectable_parts.CHUNK
        length = min(selectable_parts.CHUNK, s["duration"] - start)
        out = Path(s["dir"]) / f"part_{i + 1}.mp4"
        selectable_parts.active.add(uid)
        try:
            await event.edit(f"⏳ Part {i + 1} தயாராகிறது...\n🕐 {selectable_parts.ts(start)} → {selectable_parts.ts(start + length)}")
            await asyncio.to_thread(selectable_parts.make_part_clean, Path(s["source"]), out, start, length, s["audio_stream"])
            await asyncio.to_thread(selectable_parts.send_part, uid, out, f"🎬 Part {i + 1} • {selectable_parts.ts(start)} → {selectable_parts.ts(start + length)}")
            out.unlink(missing_ok=True)
            if i + 1 < s["total"]:
                await event.edit("✅ Part sent.\n\n👇 Next Part:", buttons=selectable_parts.next_keyboard(s["total"], s["duration"], i))
            else:
                await event.edit("✅ Last Part sent.\n\n🗑️ வேலை முடிந்தது. /reset பயன்படுத்தி புதிய Video தொடங்கலாம்.")
        except Exception as e:
            out.unlink(missing_ok=True)
            print(f"Part error for {uid}: {type(e).__name__}: {e}")
            await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")
        finally:
            selectable_parts.active.discard(uid)


async def main():
    client = selectable_parts.client
    # Remove every handler imported/registered by older versions.
    for callback, builder in list(client.list_event_handlers()):
        client.remove_event_handler(callback, builder)

    client.add_event_handler(start_manual, events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?$"))
    client.add_event_handler(cancel_manual, events.NewMessage(incoming=True, pattern=r"^/(?:reset|cancel)(?:@\w+)?$"))
    client.add_event_handler(video_manual, events.NewMessage(incoming=True))
    client.add_event_handler(callback_manual, events.CallbackQuery(data=re.compile(rb"^(AUDIO:\d+|PART:\d+|VIEWALL|CANCEL)$")))

    asyncio.create_task(asyncio.to_thread(selectable_parts.health))
    if selectable_parts.BOT_ROLE == "standby":
        while True:
            await asyncio.sleep(86400)

    await client.start(bot_token=selectable_parts.BOT_TOKEN)
    me = await client.get_me()
    print(f"Bot connected: @{getattr(me, 'username', 'unknown')}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
