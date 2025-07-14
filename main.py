import libtorrent as lt
import asyncio
import os
import shutil
import logging
import time
import gc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, Application,
    CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from filelock import FileLock

# --- تنظیمات اولیه و مسیرها ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
LOCK_FILE = "/tmp/vahab_bot.lock"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- لاگینگ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- احراز هویت گوگل درایو ---
def setup_drive():
    temp_dir = "/tmp/auth"
    os.makedirs(temp_dir, exist_ok=True)
    token_path = os.path.join(temp_dir, "token.json")
    creds_path = os.path.join(temp_dir, "credentials.json")

    try:
        shutil.copy("/etc/secrets/token.json", token_path)
        shutil.copy("/etc/secrets/credentials.json", creds_path)
    except Exception as e:
        logger.error(f"فایل‌های احراز هویت Google Drive پیدا نشدند: {e}")
        return None

    gauth = GoogleAuth()
    try:
        gauth.LoadCredentialsFile(token_path)
        if gauth.credentials is None:
            gauth.LoadClientConfigFile(creds_path)
            gauth.LocalWebserverAuth()
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
        drive = GoogleDrive(gauth)
        logger.info("احراز هویت Google Drive موفق بود.")
        return drive
    except Exception as e:
        logger.error(f"خطا در احراز هویت Google Drive: {e}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

drive = setup_drive()

# --- libtorrent ---
ses = lt.session()
ses.listen_on(6881, 6891)

# --- متغیرهای سراسری ---
active_downloads = {}
errors = []

# --- تابع دانلود ---
async def download_torrent(did, magnet, context, chat_id, dest, msg_id):
    if not drive and dest == "google_drive":
        errors.append("Google Drive آماده نیست.")
        return

    params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
    handle = lt.add_magnet_uri(ses, magnet, params)
    active_downloads[did] = (handle, None, msg_id)

    await context.bot.send_message(chat_id=chat_id, text=f"📥 در حال دریافت اطلاعات تورنت (ID: {did})")
    while not handle.has_metadata():
        await asyncio.sleep(1)

    name = handle.name()
    while not handle.is_seed():
        status = handle.status()
        percent = int(status.progress * 100)
        speed = int(status.download_rate / 1024)
        bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=f"⬇️ {name}\n[{bar}] {percent}% | {speed} KB/s",
        )
        await asyncio.sleep(5)

    file_path = os.path.join(DOWNLOAD_DIR, name)

    if dest == "telegram":
        try:
            with open(file_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f)
        except Exception as e:
            logger.error(f"خطا در ارسال فایل: {e}")
    elif dest == "google_drive":
        try:
            file_drive = drive.CreateFile({"title": name})
            file_drive.SetContentFile(file_path)
            file_drive.Upload()
            link = file_drive["webContentLink"]
            await context.bot.send_message(chat_id=chat_id, text=f"✅ آپلود شد: {link}")
        except Exception as e:
            logger.error(f"خطا در گوگل درایو: {e}")

    os.remove(file_path)
    del active_downloads[did]
    gc.collect()

# --- فرمان‌ها ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت بفرست تا شروع کنم.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    link = update.message.text.strip()
    if not link.startswith("magnet:?xt="):
        await update.message.reply_text("لینک مگنت معتبر نیست.")
        return
    did = str(len(active_downloads) + 1)
    context.user_data["magnet"] = link
    keyboard = [
        [InlineKeyboardButton("تلگرام", callback_data=f"telegram_{did}"),
         InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{did}")]
    ]
    await update.message.reply_text("کجا ذخیره بشه؟", reply_markup=InlineKeyboardMarkup(keyboard))

# --- کلیک روی دکمه‌ها ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, did = query.data.split("_", 1)
    magnet = context.user_data.get("magnet")
    if not magnet:
        await query.edit_message_text("لینک مگنت یافت نشد.")
        return
    await query.edit_message_text(f"دانلود آغاز شد، مقصد: {action}")
    context.application.create_task(download_torrent(did, magnet, context, query.message.chat_id, action, query.message.message_id))

# --- خطاها ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطا: {context.error}")
    if isinstance(context.error, Exception):
        await context.bot.send_message(chat_id=OWNER_ID, text=f"⚠️ خطا: {context.error}")

# --- اجرای ربات ---
def main():
    lock = FileLock(LOCK_FILE, timeout=1)
    with lock:
        logger.info("ربات اجرا شد")
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.add_error_handler(error_handler)
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
