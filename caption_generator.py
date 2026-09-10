import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

import requests

AI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
AI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
AI_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions").strip()


def _frame(video: Path, seconds: float) -> str | None:
    fd, name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    p = Path(name)
    try:
        r = subprocess.run([
            "ffmpeg", "-y", "-ss", str(max(0, seconds)), "-i", str(video),
            "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "5", str(p)
        ], capture_output=True, timeout=60)
        if r.returncode != 0 or not p.exists():
            return None
        return base64.b64encode(p.read_bytes()).decode("ascii")
    finally:
        p.unlink(missing_ok=True)


def _fallback(part: int, audio: str, filename: str) -> str:
    stem = Path(filename).stem.replace("_", " ").strip() or "Anime வீடியோ"
    audio_text = audio if audio.strip() else "தேர்வு செய்த Audio"
    return (
        f"Link in Bio 🔗\n\n"
        f"🎬 {stem} — Part {part}\n\n"
        f"📝 இந்த பகுதியில் கதையின் முக்கியமான தருணம் இடம்பெறுகிறது. "
        f"முழு கதையையும் தொடர Part {part} ஐ பாருங்கள்.\n\n"
        f"🎵 Song: {audio_text}\n\n"
        f"🔥 Anime ரசிகர்களுக்கான ஒரு சிறந்த தருணம்!\n\n"
        f"#Anime #AnimeTamil #AnimeEdit #AnimeReels #Tamil #Reels #Trending #Otaku"
    )


def generate(video: Path, part: int, audio: str, filename: str, start: float, length: float) -> str:
    if not AI_API_KEY:
        return _fallback(part, audio, filename)

    frames = []
    for sec in (0.5, max(0.5, length / 2), max(0.5, length - 0.5)):
        img = _frame(video, start + min(sec, max(0.1, length - 0.1)))
        if img:
            frames.append(img)
    if not frames:
        return _fallback(part, audio, filename)

    content = [{
        "type": "text",
        "text": (
            "You create Instagram Reels metadata for anime/movie clips. "
            "Return ONLY valid JSON with keys title, summary, caption, hashtags. "
            "ALL text must be natural Tamil except the exact audio name and hashtag tokens. "
            "The FIRST words of caption MUST be exactly 'Link in Bio'. "
            "Title must be short, catchy and suitable for a trending Reel. "
            "Do not claim facts that cannot be seen. Do not mention copyrighted lyrics. "
            f"This is Part {part}. Selected audio track name exactly as stored: {audio!r}. "
            f"Original filename: {filename!r}. Part duration: {length:.1f}s."
        )
    }]
    for img in frames:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}})

    payload = {
        "model": AI_MODEL,
        "temperature": 0.7,
        "max_tokens": 700,
        "messages": [{"role": "user", "content": content}],
    }
    try:
        r = requests.post(
            AI_URL,
            headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        text = data["choices"][0]["message"]["content"].strip()
        obj = json.loads(text)
        title = str(obj.get("title", "")).strip()
        summary = str(obj.get("summary", "")).strip()
        caption = str(obj.get("caption", "")).strip()
        hashtags = str(obj.get("hashtags", "")).strip()
        if not title or not summary or not caption:
            raise ValueError("Incomplete AI response")
        if not caption.lower().startswith("link in bio"):
            caption = "Link in Bio 🔗\n\n" + caption
        return (
            f"Link in Bio 🔗\n\n"
            f"🎬 {title} — Part {part}\n\n"
            f"📝 {summary}\n\n"
            f"{caption.removeprefix('Link in Bio').strip()}\n\n"
            f"🎵 Song: {audio}\n\n"
            f"{hashtags}"
        )
    except Exception as e:
        print(f"Caption generation fallback: {type(e).__name__}: {e}")
        return _fallback(part, audio, filename)
