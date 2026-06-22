import os
import time
import uuid
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

DOWNLOAD_DIR = Path("/tmp/video_downloads")
MAX_FILE_SIZE = 47 * 1024 * 1024  # 47MB (safe under 50MB limit)
executor = ThreadPoolExecutor(max_workers=2)

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

SUPPORTED_DOMAINS = [
    "youtube.com", "youtu.be", "m.youtube.com",
    "tiktok.com", "vm.tiktok.com",
    "instagram.com", "instagr.am",
    "twitter.com", "x.com",
]

FORMAT_QUALITIES = {
    "best": "best[filesize<47M]/best",
    "medium": "best[height<=720][filesize<47M]/best[height<=720]",
    "audio": "bestaudio/best",
}

async def cleanup_file(filepath: Path):
    try:
        if filepath.exists():
            filepath.unlink()
    except Exception as e:
        logger.warning(f"Cleanup failed for {filepath}: {e}")

def is_supported_url(url: str) -> bool:
    return any(domain in url for domain in SUPPORTED_DOMAINS)

def detect_platform(url: str) -> str:
    if "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    elif "tiktok.com" in url:
        return "TikTok"
    elif "instagram.com" in url or "instagr.am" in url:
        return "Instagram"
    elif "twitter.com" in url or "x.com" in url:
        return "X (Twitter)"
    return "Unknown"

def download_with_ytdlp(url: str, output_template: str, quality: str = "best", unique_id: str = "") -> tuple[str | None, str | None]:
    fmt = FORMAT_QUALITIES.get(quality, FORMAT_QUALITIES["best"])
    
    ydl_opts = {
        "format": fmt,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "extract_flat": False,
        "overwrites": True,
    }
    
    if quality == "audio":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }]
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None, "فشل في جلب معلومات الفيديو"
            
            if quality == "audio":
                filepath = str(Path(output_template).with_suffix(".mp3").resolve())
            else:
                filepath = ydl.prepare_filename(info)
            
            p = Path(filepath)
            if not p.exists():
                dl_dir = Path(output_template).parent
                matches = sorted(
                    [f for f in dl_dir.iterdir() if f.stem.startswith(unique_id)],
                    key=lambda f: f.stat().st_mtime, reverse=True,
                )
                if matches:
                    filepath = str(matches[0])
                else:
                    return None, "لم يتم العثور على الفيديو بعد التحميل"
            
            return filepath, None
    except Exception as e:
        error_msg = str(e)
        if "Private video" in error_msg:
            return None, "الفيديو خاص أو مخفي"
        elif "Video unavailable" in error_msg:
            return None, "الفيديو غير متاح"
        elif "Sign in" in error_msg or "login" in error_msg:
            return None, "يتطلب تسجيل دخول. جرب رابط آخر."
        else:
            logger.error(f"yt-dlp error: {error_msg[:200]}")
            return None, f"خطأ: {error_msg[:150]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *مرحباً بك في بوت التحميل!*\n\n"
        "أرسل لي رابط فيديو وسأحمّله لك.\n\n"
        "المنصات المدعومة:\n"
        "📺 يوتيوب (YouTube)\n"
        "🎵 تيك توك (TikTok)\n"
        "📸 إنستقرام (Instagram)\n"
        "🐦 إكس (X / Twitter)\n\n"
        "الأمر /quality لاختيار الجودة.",
        parse_mode="Markdown",
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*طريقة الاستخدام:*\n\n"
        "1️⃣ أرسل رابط الفيديو\n"
        "2️⃣ انتظر حتى يتم التحميل\n"
        "3️⃣ استلم الفيديو واحفظه في ألبوم الصور\n\n"
        "*للحفظ في ألبوم الصور:*\n"
        "- آيفون: اضغط على الفيديو → Save Video\n"
        "- أندرويد: اضغط على الفيديو → حفظ في المعرض\n\n"
        "_حد التحميل: 50MB (حد تيليغرام)_",
        parse_mode="Markdown",
    )

async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("عالية (أفضل جودة)", callback_data="q_best")],
        [InlineKeyboardButton("متوسطة (720p)", callback_data="q_medium")],
        [InlineKeyboardButton("صوت فقط (MP3)", callback_data="q_audio")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "اختر جودة التحميل:", reply_markup=reply_markup
    )

async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    quality_map = {
        "q_best": "best",
        "q_medium": "medium",
        "q_audio": "audio",
    }
    quality = quality_map.get(query.data, "best")
    quality_names = {"best": "عالية", "medium": "متوسطة", "audio": "صوت فقط"}
    
    context.user_data["quality"] = quality
    await query.edit_message_text(
        f"تم اختيار الجودة: {quality_names[quality]} ✅"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ الرابط غير مدعوم.\n"
            "الرجاء إرسال رابط من: يوتيوب، تيك توك، إنستقرام، أو X"
        )
        return
    
    platform = detect_platform(url)
    quality = context.user_data.get("quality", "best")
    
    status_msg = await update.message.reply_text(
        f"🔍 جاري معالجة رابط {platform}..."
    )
    
    unique_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{unique_id}.%(ext)s")
    
    loop = asyncio.get_running_loop()
    filepath, error = await loop.run_in_executor(
        executor, download_with_ytdlp, url, output_template, quality, unique_id
    )
    
    if error:
        await status_msg.edit_text(f"❌ {error}")
        return
    
    if not filepath or not Path(filepath).exists():
        await status_msg.edit_text("❌ فشل التحميل. حاول مرة أخرى.")
        return
    
    if quality != "audio":
        file_size = Path(filepath).stat().st_size
        if file_size > MAX_FILE_SIZE:
            await status_msg.edit_text(
                "❌ الفيديو كبير جداً (أكثر من 50MB - حد تيليغرام).\n"
                "استخدم /quality واختر جودة أقل."
            )
            await cleanup_file(Path(filepath))
            return
    
    await status_msg.edit_text("📤 جاري رفع الفيديو...")
    
    try:
        if quality == "audio":
            await update.message.reply_audio(
                audio=filepath,
                caption="🎵 تم التحميل ✅",
            )
        else:
            await update.message.reply_video(
                video=filepath,
                supports_streaming=True,
                caption=f"✅ تم التحميل من {platform}\n"
                       "اضغط على الفيديو ← Save Video للحفظ في ألبوم الصور",
            )
        
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Upload error: {e}")
        try:
            await status_msg.edit_text(f"❌ خطأ في الرفع: {str(e)[:100]}")
        except:
            pass
    finally:
        await cleanup_file(Path(filepath))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quality", quality_command))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern="^q_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    webhook_url = os.environ.get("WEBHOOK_URL")
    
    if webhook_url:
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
        )
    else:
        app.run_polling(allowed_updates=["message", "callback_query"])

def main():
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    
    while True:
        try:
            run_bot()
        except Exception as e:
            logger.error(f"Bot crashed: {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
