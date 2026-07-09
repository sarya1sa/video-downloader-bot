import os
import uuid
import asyncio
import logging
import threading
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

try:
    subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                   capture_output=True, timeout=120)
except Exception:
    pass

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required")

DOWNLOAD_DIR = Path("/tmp/video_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
executor = ThreadPoolExecutor(max_workers=2)

SUPPORTED = ["youtube.com", "youtu.be", "tiktok.com", "vm.tiktok.com",
             "instagram.com", "instagr.am", "twitter.com", "x.com"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("User sent /start")
    await update.message.reply_text(
        "مرحباً! أرسل لي رابط فيديو من يوتيوب، تيك توك، إنستقرام، أو X وسأحمّله لك."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    logger.info(f"Received message: {url}")

    if not any(d in url for d in SUPPORTED):
        await update.message.reply_text("الرابط غير مدعوم. أرسل رابط من يوتيوب، تيك توك، إنستقرام، أو X")
        return

    status = await update.message.reply_text("جاري التحميل...")

    try:
        unique_id = str(uuid.uuid4())
        out = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")

        opts = {
            "format": "best[filesize<45M]/best",
            "outtmpl": out,
            "quiet": True,
            "no_warnings": True,
        }

        loop = asyncio.get_running_loop()
        filepath = await loop.run_in_executor(executor, _download, url, out, opts, unique_id)

        if not filepath:
            await status.edit_text("فشل التحميل. حاول مرة أخرى.")
            return

        size = Path(filepath).stat().st_size
        if size > 47 * 1024 * 1024:
            await status.edit_text("الفيديو كبير جداً. جرب رابط آخر.")
            Path(filepath).unlink(missing_ok=True)
            return

        with open(filepath, "rb") as f:
            await update.message.reply_video(video=f, caption="تم التحميل")

        await status.delete()
    except Exception as e:
        logger.error(f"Error: {e}")
        await status.edit_text(f"خطأ: {str(e)[:100]}")
    finally:
        for f in DOWNLOAD_DIR.glob(f"{unique_id}*"):
            f.unlink(missing_ok=True)

def _download(url, out, opts, unique_id):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            fname = ydl.prepare_filename(info)
            if Path(fname).exists():
                return fname
            matches = sorted(DOWNLOAD_DIR.glob(f"{unique_id}*"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            return str(matches[0]) if matches else None
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

def main():
    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever(), daemon=True).start()
    logger.info(f"Health server on port {port}")

    while True:
        try:
            logger.info("Bot starting...")
            run_bot()
        except Exception as e:
            logger.error(f"Bot stopped: {e}")
            import time
            time.sleep(10)

if __name__ == "__main__":
    main()
