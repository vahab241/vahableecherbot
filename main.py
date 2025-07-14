import libtorrent as lt
import asyncio
import os
import shutil
import logging
import time
import gc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from filelock import FileLock

# --- تنظیمات ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
LOCK_FILE = "/tmp/vahab_bot.lock"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

active_downloads = {}  # {download_id: (handle, task, message_id)}
errors = []

# --- لاگ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# --- Google Drive Auth ---
def setup_drive_auth():
    temp_dir = "/tmp/vahab_auth"
    os.makedirs(temp_dir, exist_ok=True)
    token_path = os.path.join(temp_dir, "token.json")
    creds_path = os.path.join(temp_dir, "credentials.json")
    try:
        if os.path.exists("/etc/secrets/token.json") and os.path.exists("/etc/secrets/credentials.json"):
            shutil.copy("/etc/secrets/token.json", token_path)
            shutil.copy("/etc/secrets/credentials.json", creds_path)
        else:
            logger.error("فایل‌های گوگل درایو یافت نشد.")
            return None

        gauth = GoogleAuth()
        gauth.LoadCredentialsFile(token_path)
        if gauth.credentials is None:
            gauth.LoadClientConfigFile(creds_path)
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()

        drive = GoogleDrive(gauth)
        logger.info("احراز هویت گوگل درایو موفق بود.")
        return drive
    except Exception as e:
        logger.error(f"Google Drive Auth Error: {e}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

drive = setup_drive_auth()

# --- تنظیم libtorrent ---
ses = lt.session({
    'listen_interfaces': '0.0.0.0:6881',
    'connections_limit': 200,
    'download_rate_limit': 0,
})

# --- تابع دانلود ---
async def download_torrent(download_id, magnet_link, context, chat_id, destination, message_id):
    global active_downloads
    if not drive and destination == "google_drive":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="❌ گوگل درایو احراز نشد.")
        return
    
    handle = lt.add_magnet_uri(ses, magnet_link, {"save_path": DOWNLOAD_DIR})
    active_downloads[download_id] = (handle, None, message_id)

    await context.bot.send_message(chat_id=chat_id, text="🔍 در حال واکشی اطلاعات...")
    while not handle.has_metadata():
        await asyncio.sleep(1)

    name = handle.name()
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"⬇️ دانلود {name} شروع شد")

    while not handle.is_seed():
        s = handle.status()
        percent = int(s.progress * 100)
        speed = int(s.download_rate / 1000)
        bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                            text=f"📥 {name}\n[{bar}] {percent}% ({speed} KB/s)")
        await asyncio.sleep(5)

    file_path = os.path.join(DOWNLOAD_DIR, name)
    if destination == "telegram":
        with open(file_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f)
    elif destination == "google_drive":
        gfile = drive.CreateFile({"title": name})
        gfile.SetContentFile(file_path)
        gfile.Upload()
        link = gfile["webContentLink"]
        await context.bot.send_message(chat_id=chat_id, text=f"✅ آپلود شد: {link}")

    os.remove(file_path)
    del active_downloads[download_id]
    gc.collect()

# --- Callbacks ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, download_id = query.data.split("_", 1)

    if action in ("telegram", "google_drive"):
        magnet_link = context.user_data.get("magnet_link")
        if not magnet_link:
            await query.edit_message_text("❌ لینک مگنت پیدا نشد")
            return
        await query.edit_message_text(f"📥 شروع دانلود (ID: {download_id}) در {action}")
        context.application.create_task(
            download_torrent(download_id, magnet_link, context, query.message.chat_id, action, query.message.message_id)
        )

# --- دستورات ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت بفرست.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text = update.message.text.strip()
    if text.startswith("magnet:?xt="):
        download_id = str(len(active_downloads) + 1)
        context.user_data["magnet_link"] = text
        kb = [[InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"),
               InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")]]
        await update.message.reply_text("کجا ذخیره کنم؟", reply_markup=InlineKeyboardMarkup(kb))

# --- اجرای ربات ---
def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN مشخص نشده.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
