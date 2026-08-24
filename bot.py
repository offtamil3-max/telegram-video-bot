import os
import threading
from flask import Flask, Response
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

app = Flask(__name__)

# -------------------------
# Health check
# -------------------------

@app.route("/")
def home():
    return "Telegram Video Bot is running!"


# -------------------------
# Telegram channel handler
# -------------------------

async def channel_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    message = update.channel_post

    if not message:
        return

    video = message.video

    if not video:
        return

    file_id = video.file_id

    # Temporary download link
    try:
        file = await context.bot.get_file(file_id)

        telegram_url = file.file_path

        if telegram_url:
            link = (
                "https://api.telegram.org/file/bot"
                + BOT_TOKEN
                + "/"
                + telegram_url
            )

            print("VIDEO DOWNLOAD LINK:")
            print(link)

    except Exception as e:
        print("Error:", e)


# -------------------------
# Start Telegram bot
# -------------------------

def run_bot():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        MessageHandler(
            filters.VIDEO,
            channel_video
        )
    )

    application.run_polling(
        allowed_updates=["channel_post"]
    )


# -------------------------
# Start web server
# -------------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    threading.Thread(
        target=run_bot,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=port
    )
