import re
import subprocess

import selectable_parts


def robust_find_tamil_audio(path):
    """Find Tamil audio reliably from FFmpeg stream labels/metadata."""
    text = selectable_parts.probe(path)
    tracks = []
    current = None

    for raw in text.splitlines():
        line = raw.strip()

        if line.startswith("Stream #"):
            m = re.search(r"Stream #0:(\d+)(?:\[[^\]]+\])?(?:\(([^)]+)\))?.*?:\s*Audio:\s*", line, re.I)
            if m:
                current = {
                    "stream": int(m.group(1)),
                    "lang": m.group(2) or "",
                    "title": "",
                }
                tracks.append(current)
            else:
                current = None
            continue

        if current:
            m = re.match(r"(?:language|title)\s*:\s*(.+)$", line, re.I)
            if m:
                key = line.split(":", 1)[0].strip().lower()
                value = m.group(1).strip()
                if key == "language":
                    current["lang"] = value
                elif key == "title":
                    current["title"] = value

    if not tracks:
        raise RuntimeError("No audio track found")

    tamil_re = re.compile(r"(?:^|[^a-z])(?:tamil|tam|ttam|ta)(?:$|[^a-z])", re.I)
    matches = [
        t for t in tracks
        if tamil_re.search(t["lang"]) or tamil_re.search(t["title"])
    ]
    if not matches:
        # Some files expose the language only in the stream line or title text.
        stream_lines = [
            line.strip() for line in text.splitlines()
            if "Audio:" in line
        ]
        for t, line in zip(tracks, stream_lines):
            if tamil_re.search(line):
                matches.append(t)

    if not matches:
        labels = ", ".join(
            f"stream {t['stream']} lang={t['lang']!r} title={t['title']!r}"
            for t in tracks
        )
        raise RuntimeError(f"Tamil audio track not found; tracks: {labels}")

    chosen = matches[0]
    print(
        f"Tamil audio selected: stream {chosen['stream']} "
        f"lang={chosen['lang']!r} title={chosen['title']!r}"
    )
    return chosen["stream"]


# Override the original detector before the async bot starts processing videos.
selectable_parts.find_tamil_audio = robust_find_tamil_audio


if __name__ == "__main__":
    import asyncio
    asyncio.run(selectable_parts.main())
