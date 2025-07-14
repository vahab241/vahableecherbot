import libtorrent as lt
import asyncio
import os
import shutil
import logging
import time
import gc
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in environment variables.")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
active_downloads = {}  # {download_id: (handle, task, message_id)}
errors = []
LOCK_FILE = "/tmp/vahab_bot.lock"

# --- پیکربندی لاگینگ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# --- پیکربندی Google Drive ---
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
            logger.error("فایل‌های احراز هویت Google Drive توی /etc/secrets/ پیدا نشد.")
            return None
    except Exception as e:
        logger.error(f"خطا در کپی فایل‌های احراز هویت: {e}")
        return None

    gauth = GoogleAuth()
    try:
        gauth.LoadCredentialsFile(token_path)
        if gauth.credentials is None:
            gauth.LoadClientConfigFile(creds_path)
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
        drive = GoogleDrive(gauth)
        logger.info("احراز هویت Google Drive با موفقیت انجام شد.")
        return drive
    except Exception as e:
        logger.error(f"خطا در احراز هویت Google Drive: {e}")
        return None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

drive = setup_drive_auth()

# --- پیکربندی libtorrent ---
settings = {
    'listen_interfaces': '0.0.0.0:6881',
    'connections_limit': 200,
    'download_rate_limit': 0,
}
ses = lt.session(settings)

# --- دانلودکننده تورنت ---
async def download_torrent(download_id: str, magnet_link: str, context: ContextTypes.DEFAULT_TYPE, chat_id: int, destination: str, message_id: int):
    global active_downloads, errors
    if not drive and destination == "google_drive":
        errors.append(f"احراز هویت Google Drive برای دانلود (ID: {download_id}) انجام نشده.")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"<b>❌ خطا</b>: احراز هویت Google Drive انجام نشده (ID: {download_id}).",
            parse_mode="HTML",
        )
        return

    logger.info(f"شروع دانلود: ID={download_id}")
    params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
    handle = lt.add_magnet_uri(ses, magnet_link, params)

    for _ in range(60):
        if handle.has_metadata():
            break
        await asyncio.sleep(1)
    else:
        errors.append(f"دریافت متادیتای تورنت ناموفق بود (ID: {download_id})")
        return

    name = handle.name()
    active_downloads[download_id] = (handle, None, message_id)

    await context.bot.send_message(chat_id=chat_id, text=f"<b>⬇️ دانلود آغاز شد</b>: *{name}* (ID: {download_id})", parse_mode="HTML")
    while not handle.is_seed():
        s = handle.status()
        percent = int(s.progress * 100)
        speed = int(s.download_rate / 1000)
        progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"<b>📥 در حال دانلود</b> *{name}* (ID: {download_id})\n[{progress_bar}] <i>{percent}% ({speed} KB/s)</i>",
            parse_mode="HTML",
        )
        await asyncio.sleep(5)

    file_path = os.path.join(DOWNLOAD_DIR, name)
    if os.path.exists(file_path):
        if destination == "telegram":
            try:
                logger.info("در حال ارسال به تلگرام...")
                with open(file_path, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f)
            except Exception as e:
                errors.append(f"خطا در ارسال فایل (ID: {download_id}): {e}")
            finally:
                os.remove(file_path)
        elif destination == "google_drive":
            try:
                logger.info("در حال آپلود به گوگل درایو...")
                file_drive = drive.CreateFile({"title": name})
                file_drive.SetContentFile(file_path)
                file_drive.Upload()
                file_url = file_drive["webContentLink"]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>📤 فایل آپلود شد</b>: {file_url}",
                    parse_mode="HTML",
                )
            except Exception as e:
                errors.append(f"خطا در آپلود به گوگل درایو (ID: {download_id}): {e}")
            finally:
                os.remove(file_path)

    del active_downloads[download_id]
    gc.collect()

# --- اصلاح بخش اجرای وظیفه دانلود در handle_callback ---
# فقط این تکه کد رو جایگزین بخش دکمه‌های telegram و google_drive در handle_callback کن:

# elif action in ("telegram", "google_drive"):
#     magnet_link = context.user_data.get("magnet_link")
#     if not magnet_link:
#         await query.edit_message_text("<b>❌ خطا: لینک مگنت پیدا نشد</b>.", parse_mode="HTML")
#         return
#     await query.edit_message_text(f"<b>📥 دانلود شروع شد</b> (ID: {download_id}) و فایل در *{action}* ذخیره می‌شه.", parse_mode="HTML")
#     context.application.create_task(
#         download_torrent(download_id, magnet_link, context, query.message.chat_id, action, query.message.message_id)
#     )
