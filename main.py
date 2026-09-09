import asyncio
import math
import re
import shutil
import subprocess
import tempfile
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
    rows = [[Button.inline(t["label"], data=f"AUDIO:{i}".encode())] for i, t in enumerate(tracks)]
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


def prepare_background(src, out):
    cmd = [
        selectable_parts.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
        "-i", str(src),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1",
        "-frames:v", "1", "-q:v", "5", str(out),
    ]
    subprocess.run(cmd, check=True, timeout=120)


def make_part_compat(src, bg, out, start, length, overlay_text, footer_text, audio_stream):
    title = selectable_parts.escape_drawtext(overlay_text)
    footer = selectable_parts.escape_drawtext(footer_text)
    filt = (
        "[1:v]scale=1000:1780:force_original_aspect_ratio=decrease[fg];"
        "[0:v][fg]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]drawtext=text='{title}':fontcolor=white:fontsize=58:box=1:boxcolor=black@0.65:boxborderw=18:x=(w-text_w)/2:y=45,"
        f"drawtext=text='{footer}':fontcolor=white:fontsize=42:box=1:boxcolor=black@0.65:boxborderw=14:x=(w-text_w)/2:y=h-text_h-55[v]"
    )
    cmd = [
        selectable_parts.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
        "-loop", "1", "-framerate", "30", "-i", str(bg),
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", f"1:{audio_stream}", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.0",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, check=True, timeout=900)


async def start_manual(event):
    if event.sender_id:
        selectable_parts.cleanup(event.sender_id)
    await event.respond(
        "🎬 40-Second Video Splitter\n\n"
        "1️⃣ முதலில் Background Photo அனுப்புங்கள்.\n"
        "2️⃣ அடுத்து Full Video அனுப்புங்கள்.\n"
        "3️⃣ எல்லா Audio tracks-லும் தேவையான track-ஐ தேர்வு செய்யுங்கள்.\n"
        "4️⃣ Season / Episode அல்லது Movie தேர்வு செய்யுங்கள்.\n"
        "5️⃣ கீழே வர வேண்டிய custom text-ஐ அனுப்பி Confirm செய்யுங்கள்.\n"
        "6️⃣ தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்."
    )


async def cancel_manual(event):
    selectable_parts.cleanup(event.sender_id)
    await event.respond("✅ Cancelled.")


async def photo_manual(event):
    uid, msg = event.sender_id, event.message
    if not uid or not msg.photo:
        return
    if uid in selectable_parts.active:
        await event.respond("⚠️ ஏற்கனவே ஒரு video processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
        return
    old = selectable_parts.sessions.pop(uid, None)
    if old:
        shutil.rmtree(old.get("dir", ""), ignore_errors=True)
    d = tempfile.mkdtemp(prefix=f"video_{uid}_")
    bg = Path(d) / "background.jpg"
    try:
        await selectable_parts.client.download_media(msg, file=str(bg))
        selectable_parts.sessions[uid] = {"dir": d, "background": str(bg)}
        await event.respond("✅ Background photo saved.\n\n🎬 இப்போது Video அனுப்புங்கள்.")
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        print(f"Photo prepare error for {uid}: {type(e).__name__}: {e}")
        await event.respond("❌ Photo save செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


async def text_manual(event):
    uid = event.sender_id
    s = selectable_parts.sessions.get(uid)
    if not s or s.get("state") != "awaiting_footer":
        return
    value = (event.raw_text or "").strip()
    if not value:
        await event.respond("✏️ கீழே வர வேண்டிய text-ஐ அனுப்புங்கள்.")
        return
    s["footer_pending"] = value[:180]
    s["state"] = "confirm_footer"
    await event.respond(
        f"📝 கீழே வரும் text:\n\n{s['footer_pending']}\n\nஇதுதானா?",
        buttons=selectable_parts.footer_confirm_keyboard(),
    )


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
    s = selectable_parts.sessions.get(uid)
    if not s or not s.get("background"):
        await event.respond("🖼️ முதலில் Background Photo அனுப்புங்கள்.")
        return
    selectable_parts.active.add(uid)
    d = s["dir"]
    src = Path(d) / "video.mp4"
    bg_blur = Path(d) / "background_blur.jpg"
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    try:
        await selectable_parts.download(msg, src, status)
        dur = await asyncio.to_thread(selectable_parts.duration, src)
        tracks = await asyncio.to_thread(list_audio_tracks, src)
        await asyncio.to_thread(prepare_background, Path(s["background"]), bg_blur)
        total = max(1, math.ceil(dur / selectable_parts.CHUNK))
        selectable_parts.sessions[uid] = {
            "dir": d,
            "background": str(bg_blur),
            "source": str(src),
            "duration": dur,
            "total": total,
            "audio_tracks": tracks,
            "state": "choose_audio",
        }
        await status.edit(
            f"✅ Full video ready\n\n⏱️ Duration: {selectable_parts.ts(dur)}\n🎧 Audio tracks: {len(tracks)}\n\n👇 File-ல் இருக்கும் audio track name அப்படியே தேர்வு செய்யுங்கள்:",
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


async def part_manual(event):
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
        s["state"] = "choose_mode"
        await event.answer("✅ Audio selected")
        await event.edit("✅ Audio track selected.\n\n👇 இப்போது மேலே வர வேண்டிய label-ஐ தேர்வு செய்யுங்கள்:", buttons=selectable_parts.mode_keyboard())
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
        await event.edit(
            f"✅ Selected: {label}\n\n📝 இந்த video-வின் கீழே என்ன text வர வேண்டும்?\nText-ஐ ஒரு message-ஆ அனுப்புங்கள்.",
            buttons=[[Button.inline("🗑️ Cancel", data=b"CANCEL")]],
        )
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
        selectable_parts.locks[uid] = asyncio.Lock()
        await event.answer("Confirmed")
        await event.edit(
            f"✅ Setup complete\n\n⬆️ {selectable_parts.part_title(s, 0)}\n⬇️ {s['footer']}\n\n👇 தேவையான Part-ஐ மட்டும் தேர்வு செய்யுங்கள்:",
            buttons=selectable_parts.all_keyboard(s["total"], s["duration"]),
        )
        return
    if data == "VIEWALL":
        if s.get("state") != "ready":
            await event.answer("முதலில் setup முடிக்கவும்.")
            return
        await event.answer("All parts")
        await event.edit(buttons=selectable_parts.all_keyboard(s["total"], s["duration"]))
        return
    if not data.startswith("PART:"):
        return
    if s.get("state") != "ready":
        await event.answer("முதலில் setup complete செய்யுங்கள்.")
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
                f"⏳ {selectable_parts.part_title(s, i)} தயாராகிறது...\n🕐 {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}"
            )
            await asyncio.to_thread(
                make_part_compat,
                Path(s["source"]), Path(s["background"]), out,
                a, length, selectable_parts.part_title(s, i), s["footer"], s["audio_stream"],
            )
            await asyncio.to_thread(
                selectable_parts.send_part,
                uid, out,
                f"🎬 {selectable_parts.part_title(s, i)} • {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}",
            )
            out.unlink(missing_ok=True)
            await event.edit(
                f"✅ {selectable_parts.part_title(s, i)} sent\n\n👇 அடுத்த Part-ஐ தேர்வு செய்யுங்கள்:",
                buttons=selectable_parts.next_keyboard(s["total"], s["duration"], i),
            )
        except Exception as e:
            out.unlink(missing_ok=True)
            print(f"Part error for {uid}: {type(e).__name__}: {e}")
            await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")
        finally:
            selectable_parts.active.discard(uid)


async def main():
    client = selectable_parts.client

    # IMPORTANT: selectable_parts.py contains the legacy handlers. Remove ALL
    # imported handlers before registering the new state-machine handlers.
    # Keeping the old video handler was causing the automatic "PART 1" flow.
    for callback, builder in list(client.list_event_handlers()):
        client.remove_event_handler(callback, builder)

    client.add_event_handler(
        start_manual,
        events.NewMessage(incoming=True, pattern=r"^/start(?:@\w+)?$")
    )
    client.add_event_handler(
        cancel_manual,
        events.NewMessage(incoming=True, pattern=r"^/(?:reset|cancel)(?:@\w+)?$")
    )
    client.add_event_handler(
        photo_manual,
        events.NewMessage(incoming=True, func=lambda e: bool(e.message.photo)),
    )
    client.add_event_handler(
        video_manual,
        events.NewMessage(incoming=True),
    )
    client.add_event_handler(
        text_manual,
        events.NewMessage(incoming=True),
    )
    client.add_event_handler(
        part_manual,
        events.CallbackQuery(data=re.compile(rb"^(AUDIO:\d+|PART:\d+|VIEWALL|CANCEL|MODE:(SEASON|MOVIE)|FOOTER:(OK|CHANGE))$")),
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
