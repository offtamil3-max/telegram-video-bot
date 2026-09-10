import asyncio
import math
import re
import shutil
import subprocess
from pathlib import Path

import selectable_parts
from telethon import Button, events

SYSTEM_FFMPEG = shutil.which("ffmpeg")
if SYSTEM_FFMPEG:
    selectable_parts.FFMPEG = SYSTEM_FFMPEG


def list_audio_tracks(path):
    text = selectable_parts.probe(path)
    tracks = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Stream #"):
            m = re.search(r"Stream #0:(\d+)(?:\[[^\]]+\])?(?:\(([^)]+)\))?.*?:\s*Audio:\s*", line, re.I)
            if m:
                current = {"stream": int(m.group(1)), "lang": m.group(2) or "", "title": ""}
                tracks.append(current)
            else:
                current = None
            continue
        if current:
            m = re.match(r"(language|title)\s*:\s*(.*)$", line, re.I)
            if m:
                key, value = m.group(1).lower(), m.group(2)
                current["lang" if key == "language" else "title"] = value
    if not tracks:
        raise RuntimeError("No audio track found")
    for t in tracks:
        t["label"] = t["title"] if t["title"] else t["lang"]
        if not t["label"]:
            t["label"] = "\u200b"
    print("Audio tracks:", [(t["stream"], t["label"]) for t in tracks])
    return tracks


def audio_keyboard(tracks):
    rows = []
    for i, t in enumerate(tracks):
        rows.append([Button.inline(t["label"], data=f"AUDIO:{i}".encode())])
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


async def start_manual(event):
    if event.sender_id:
        selectable_parts.cleanup(event.sender_id)
    await event.respond(
        "🎬 40-Second Video Splitter\n\n"
        "1️⃣ Full Video அனுப்புங்கள்.\n"
        "2️⃣ File-ல் இருக்கும் Audio track-ஐ தேர்வு செய்யுங்கள்.\n"
        "3️⃣ தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்.\n\n"
        "ℹ️ Background / watermark / custom text எதுவும் சேர்க்கப்படாது."
    )


async def cancel_manual(event):
    selectable_parts.cleanup(event.sender_id)
    await event.respond("✅ Cancelled.")


async def video_manual(event):
    uid, msg = event.sender_id, event.message
    if not uid:
        return
    if uid in selectable_parts.active:
        await event.respond("⚠️ Video இன்னும் தயாராகிறது. முடியும் வரை காத்திருக்கவும்.")
        return
    mime = getattr(msg.file, "mime_type", None) if msg.file else None
    if not mime or not mime.startswith("video/"):
        return
    selectable_parts.active.add(uid)
    d = Path(selectable_parts.tempfile.mkdtemp(prefix=f"video_{uid}_")) if hasattr(selectable_parts, "tempfile") else Path(__import__("tempfile").mkdtemp(prefix=f"video_{uid}_"))
    src = d / "video.mkv"
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    try:
        await selectable_parts.download(msg, src, status)
        dur = await asyncio.to_thread(selectable_parts.duration, src)
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
            f"✅ Full video ready\n\n"
            f"⏱️ Duration: {selectable_parts.ts(dur)}\n"
            f"🎬 Total parts: {total}\n"
            f"🎧 Audio tracks: {len(tracks)}\n\n"
            "👇 File-ல் இருக்கும் audio track name-ஐ அப்படியே தேர்வு செய்யுங்கள்:",
            buttons=audio_keyboard(tracks),
        )
    except Exception as e:
        selectable_parts.cleanup(uid)
        print(f"Video prepare error for {uid}: {type(e).__name__}: {e}")
        try:
            await status.edit("❌ Video prepare செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")
        except Exception:
            await event.respond("❌ Video prepare செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")
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
        except Exception:
            await event.answer("Invalid audio")
            return
        s["audio_stream"] = track["stream"]
        s["audio_label"] = track["label"]
        s["state"] = "ready"
        selectable_parts.locks[uid] = asyncio.Lock()
        await event.answer("✅ Audio selected")
        await event.edit(
            f"✅ Audio track selected\n\n"
            f"🎧 {track['label'] if track['label'].strip() else 'Audio track'}\n"
            f"🎬 {s['total']} parts ready\n\n"
            "👇 தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்:",
            buttons=selectable_parts.all_keyboard(s["total"], s["duration"]),
        )
        return
    if data == "VIEWALL":
        if s.get("state") != "ready":
            await event.answer("முதலில் audio track தேர்வு செய்யுங்கள்.")
            return
        await event.answer("All parts")
        await event.edit(buttons=selectable_parts.all_keyboard(s["total"], s["duration"]))
        return
    if not data.startswith("PART:"):
        return
    if s.get("state") != "ready":
        await event.answer("முதலில் audio track தேர்வு செய்யுங்கள்.")
        return
    try:
        i = int(data.split(":", 1)[1])
    except Exception:
        await event.answer("Invalid part")
        return
    if i < 0 or i >= s["total"]:
        await event.answer("Invalid part")
        return
    await event.answer("⏳ Part தயாராகிறது...")
    async with selectable_parts.locks[uid]:
        s = selectable_parts.sessions.get(uid)
        if not s or s.get("state") != "ready":
            return
        a = i * selectable_parts.CHUNK
        length = min(selectable_parts.CHUNK, s["duration"] - a)
        out = Path(s["dir"]) / f"part_{i + 1}.mp4"
        selectable_parts.active.add(uid)
        try:
            await event.edit(
                f"⏳ Part {i + 1} தயாராகிறது...\n"
                f"🕐 {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}\n"
                f"🎧 {s['audio_label'] if s['audio_label'].strip() else 'Selected audio track'}"
            )
            await asyncio.to_thread(
                selectable_parts.make_part_clean,
                Path(s["source"]), out, a, length, s["audio_stream"],
            )
            await asyncio.to_thread(
                selectable_parts.send_part,
                uid, out,
                f"🎬 Part {i + 1} • {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}",
            )
            out.unlink(missing_ok=True)
            if i + 1 < s["total"]:
                await event.edit(
                    f"✅ Part {i + 1} sent\n\n👇 அடுத்த Part-ஐ தேர்வு செய்யுங்கள்:",
                    buttons=selectable_parts.next_keyboard(s["total"], s["duration"], i),
                )
            else:
                await event.edit("✅ Last Part sent.\n\n🗑️ வேலை முடிந்தது. /reset பயன்படுத்தி புதிய video தொடங்கலாம்.")
        except Exception as e:
            out.unlink(missing_ok=True)
            print(f"Part error for {uid}: {type(e).__name__}: {e}")
            await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")
        finally:
            selectable_parts.active.discard(uid)


async def main():
    client = selectable_parts.client
    for callback, builder in list(client.list_event_handlers()):
        client.remove_event_handler(callback, builder)

    client.add_event_handler(start_manual, events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?$"))
    client.add_event_handler(cancel_manual, events.NewMessage(incoming=True, pattern=r"^/(?:reset|cancel)(?:@\w+)?$"))
    client.add_event_handler(video_manual, events.NewMessage(incoming=True))
    client.add_event_handler(
        callback_manual,
        events.CallbackQuery(data=re.compile(rb"^(AUDIO:\d+|PART:\d+|VIEWALL|CANCEL)$")),
    )

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
