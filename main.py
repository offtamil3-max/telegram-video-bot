import asyncio
import math
import os
import re
import subprocess
from pathlib import Path

import selectable_parts
from telethon import Button, events


RENDER_FFMPEG = "/usr/bin/ffmpeg" if os.path.exists("/usr/bin/ffmpeg") else selectable_parts.FFMPEG


def list_audio_tracks(path):
    """Return every audio stream with only metadata actually present in the file."""
    text = selectable_parts.probe(path)
    tracks = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Stream #"):
            m = re.search(
                r"Stream #0:(\d+)(?:\[[^\]]+\])?(?:\(([^)]+)\))?.*?:\s*Audio:\s*",
                line,
                re.I,
            )
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
                if key == "language":
                    current["lang"] = value
                else:
                    current["title"] = value

    if not tracks:
        raise RuntimeError("No audio track found")

    # Use only metadata actually stored in the file. Never invent a language
    # or track name. If both are absent, keep the button visually blank.
    for t in tracks:
        t["label"] = t["title"] if t["title"] != "" else t["lang"]
        if t["label"] == "":
            t["label"] = "\u200b"
    print("Audio tracks:", [(t["stream"], t["label"]) for t in tracks])
    return tracks


def audio_keyboard(tracks):
    rows = []
    for i, t in enumerate(tracks):
        rows.append([Button.inline(t["label"], data=f"AUDIO:{i}".encode())])
    rows.append([Button.inline("🗑️ Cancel", data=b"CANCEL")])
    return rows


def selected_audio_keyboard():
    return [[Button.inline("🗑️ Cancel", data=b"CANCEL")]]


def make_part_compat(src, bg, out, start, length, overlay_text, footer_text, audio_stream):
    title = selectable_parts.escape_drawtext(overlay_text)
    footer = selectable_parts.escape_drawtext(footer_text)
    filt = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1[bg];"
        "[1:v]scale=1000:1780:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]drawtext=text='{title}':fontcolor=white:fontsize=58:box=1:boxcolor=black@0.65:boxborderw=18:x=(w-text_w)/2:y=45,"
        f"drawtext=text='{footer}':fontcolor=white:fontsize=42:box=1:boxcolor=black@0.65:boxborderw=14:x=(w-text_w)/2:y=h-text_h-55[v]"
    )
    subprocess.run([
        RENDER_FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(bg),
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-filter_complex", filt,
        "-map", "[v]", "-map", f"1:{audio_stream}",
        "-sn", "-dn",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.0",
        "-threads", "0", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
        str(out),
    ], check=True, timeout=900)


async def video_manual(event):
    uid, msg = event.sender_id, event.message
    if not uid or uid in selectable_parts.active:
        if uid in selectable_parts.active:
            await event.respond("⚠️ ஏற்கனவே ஒரு video processing-ல் உள்ளது. /cancel பயன்படுத்தவும்.")
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
    status = await event.respond("📥 Full video download தொடங்குகிறது...\n0%")
    try:
        await selectable_parts.download(msg, src, status)
        dur = await asyncio.to_thread(selectable_parts.duration, src)
        tracks = await asyncio.to_thread(list_audio_tracks, src)
        total = max(1, math.ceil(dur / selectable_parts.CHUNK))
        selectable_parts.sessions[uid] = {
            "dir": d, "background": s["background"], "source": str(src),
            "duration": dur, "total": total, "audio_tracks": tracks,
            "state": "choose_audio",
        }
        await event.respond(
            f"✅ Full video ready\n\n⏱️ Duration: {selectable_parts.ts(dur)}\n🎧 Audio tracks: {len(tracks)}\n\n👇 File-ல் இருக்கும் audio track name அப்படியே தேர்வு செய்யுங்கள்:",
            buttons=audio_keyboard(tracks),
        )
    except Exception as e:
        selectable_parts.cleanup(uid)
        print(f"Video prepare error for {uid}: {type(e).__name__}: {e}")
        await event.respond("❌ Video prepare செய்ய முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


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
            idx = int(data.split(":", 1)[1])
            track = s["audio_tracks"][idx]
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
            buttons=selected_audio_keyboard(),
        )
        return

    if data == "FOOTER:CHANGE":
        s["state"] = "awaiting_footer"
        await event.answer("Change")
        await event.edit("✏️ புதிய கீழ் text-ஐ அனுப்புங்கள்.", buttons=selected_audio_keyboard())
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
    await event.answer("⏳ Part தயாராகிறது...")
    if s.get("state") != "ready":
        await event.respond("❌ முதலில் setup complete செய்யுங்கள்.")
        return
    try:
        i = int(data.split(":", 1)[1])
    except Exception:
        return
    if i < 0 or i >= s["total"]:
        return

    async with selectable_parts.locks[uid]:
        s = selectable_parts.sessions.get(uid)
        if not s:
            return
        a = i * selectable_parts.CHUNK
        length = min(selectable_parts.CHUNK, s["duration"] - a)
        out = Path(s["dir"]) / f"part_{i + 1}.mp4"
        try:
            await event.edit(buttons=selectable_parts.next_keyboard(s["total"], s["duration"], i))
            await event.respond(f"⏳ {selectable_parts.part_title(s, i)} தயாராகிறது...\n🕐 {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}")
            await asyncio.to_thread(
                make_part_compat, Path(s["source"]), Path(s["background"]), out,
                a, length, selectable_parts.part_title(s, i), s["footer"], s["audio_stream"]
            )
            await asyncio.to_thread(
                selectable_parts.send_part, uid, out,
                f"🎬 {selectable_parts.part_title(s, i)} • {selectable_parts.ts(a)} → {selectable_parts.ts(a + length)}",
            )
            out.unlink(missing_ok=True)
            await event.edit(buttons=selectable_parts.next_keyboard(s["total"], s["duration"], i))
        except Exception as e:
            out.unlink(missing_ok=True)
            print(f"Part error for {uid}: {type(e).__name__}: {e}")
            await event.respond("❌ Part அனுப்ப முடியவில்லை. மீண்டும் முயற்சி செய்யுங்கள்.")


async def main():
    selectable_parts.client.remove_event_handler(selectable_parts.video)
    selectable_parts.client.remove_event_handler(selectable_parts.part)
    selectable_parts.client.add_event_handler(video_manual, events.NewMessage(incoming=True))
    selectable_parts.client.add_event_handler(
        part_manual,
        events.CallbackQuery(data=re.compile(rb"^(AUDIO:\d+|PART:\d+|VIEWALL|CANCEL|MODE:(SEASON|MOVIE)|FOOTER:(OK|CHANGE))$")),
    )
    asyncio.create_task(asyncio.to_thread(selectable_parts.health))
    if selectable_parts.BOT_ROLE == "standby":
        while True:
            await asyncio.sleep(86400)
    await selectable_parts.client.start(bot_token=selectable_parts.BOT_TOKEN)
    me = await selectable_parts.client.get_me()
    print(f"Bot connected: @{getattr(me, 'username', 'unknown')}")
    await selectable_parts.client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
