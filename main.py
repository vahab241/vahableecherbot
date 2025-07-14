# mail.py
import libtorrent as lt
import asyncio
import os
import shutil
import logging
import time
import gc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, Application
)
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from filelock import FileLock

# --- تنظیمات ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in environment variables.")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
active_downloads = {}
errors = []
LOCK_FILE = "/tmp/vahab_bot.lock"

# --- لاگ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- احراز هویت Google Drive ---
def setup_drive_auth():
    temp_dir = "/tmp/vahab_auth"
    os.makedirs(temp_dir, exist_ok=True)
    token_path = os.path.join(temp_dir, "token.json")
    creds_path = os.path.join(temp_dir, "credentials.json")
    try:
        if os.path.exists("/etc/secrets/token.json") and os.path.exists("/etc/secrets/credentials.json"):
            shutil.copyfile("/etc/secrets/token.json", token_path)
            shutil.copyfile("/etc/secrets/credentials.json", creds_path)
        else:
            logger.error("Google Drive auth files not found in /etc/secrets.")
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
        logger.info("Google Drive auth success.")
        return drive
    except Exception as e:
        logger.error(f"Drive auth error: {e}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

drive = setup_drive_auth()

# --- تنظیمات libtorrent ---
ses = lt.session({
    'listen_interfaces': '0.0.0.0:6881',
    'connections_limit': 200,
    'download_rate_limit': 0,
})

# --- دانلود ---
async def download_torrent(download_id, magnet_link, context, chat_id, destination, message_id):
    if not drive and destination == "google_drive":
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                            text="❌ احراز هویت Google Drive انجام نشده.", parse_mode="HTML")
        return

    handle = lt.add_magnet_uri(ses, magnet_link, {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)})
    active_downloads[download_id] = (handle, None, message_id)
    await context.bot.send_message(chat_id=chat_id, text=f"🔍 دریافت اطلاعات تورنت... (ID: {download_id})", parse_mode="HTML")

    while not handle.has_metadata():
        await asyncio.sleep(1)

    name = handle.name()
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=f"⬇️ دانلود شروع شد: *{name}* (ID: {download_id})", parse_mode="HTML")

    while not handle.is_seed():
        s = handle.status()
        percent = int(s.progress * 100)
        speed = int(s.download_rate / 1000)
        bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                            text=f"📥 *{name}* [{bar}] {percent}% ({speed} KB/s)", parse_mode="HTML")
        await asyncio.sleep(5)

    file_path = os.path.join(DOWNLOAD_DIR, name)
    if os.path.exists(file_path):
        if destination == "telegram":
            await context.bot.send_document(chat_id=chat_id, document=open(file_path, "rb"))
        elif destination == "google_drive":
            file_drive = drive.CreateFile({"title": name})
            file_drive.SetContentFile(file_path)
            file_drive.Upload()
            url = file_drive["webContentLink"]
            await context.bot.send_message(chat_id=chat_id, text=f"📤 آپلود شد: {url}")
        os.remove(file_path)
    del active_downloads[download_id]
    gc.collect()

# --- بررسی خطاها ---
async def check_errors(context):
    if errors:
        await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(errors))
        errors.clear()

# --- دستورات ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت رو بفرست.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    if link.startswith("magnet:?xt="):
        did = str(len(active_downloads) + 1)
        context.user_data["magnet_link"] = link
        kb = [[InlineKeyboardButton("تلگرام", callback_data=f"telegram_{did}"),
               InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{did}")]]
        await update.message.reply_text(f"کجا ذخیره بشه؟ (ID: {did})", reply_markup=InlineKeyboardMarkup(kb))

# --- کال‌بک ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, did = query.data.split("_", 1)
    magnet = context.user_data.get("magnet_link")
    if not magnet:
        await query.edit_message_text("❌ لینک مگنت پیدا نشد.")
        return
    await query.edit_message_text(f"📥 شروع دانلود (ID: {did}) به مقصد {action}.")
    context.application.create_task(
        download_torrent(did, magnet, context, query.message.chat_id, action, query.message.message_id)
    )

# --- اجرا ---
def main():
    lock = FileLock(LOCK_FILE, timeout=1)
    with lock:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.job_queue.run_repeating(check_errors, interval=300)
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.run_polling()

if __name__ == "__main__":
    main()
