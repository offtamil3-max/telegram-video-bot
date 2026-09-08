FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

# Local Bot API may return an absolute file URL; use it directly instead of prefixing it again.
RUN python -c "p='main.py'; s=open(p).read(); old='relative_path = quote(file_path.lstrip(\"/\"), safe=\"/\")\\n    url = f\"{LOCAL_BOT_API_URL}/file/bot{BOT_TOKEN}/{relative_path}\"'; new='url = file_path if file_path.startswith((\"http://\", \"https://\")) else f\"{LOCAL_BOT_API_URL}/file/bot{BOT_TOKEN}/{quote(file_path.lstrip(\"/\"), safe=\"/\")}\"'; s=s.replace(old,new); s=s.replace('tg_file = await context.bot.get_file(file_id)', 'tg_file = await context.bot.get_file(file_id, read_timeout=900, connect_timeout=120, pool_timeout=120)'); open(p,'w').write(s)"

CMD ["python", "main.py"]
