import os
import shutil
import asyncio
import logging
import gc
import libtorrent as lt

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

if not TELEGRAM_TOKEN or not OWNER_ID:
    raise RuntimeError("توکن یا آیدی مالک تنظیم نشده.")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

active = {}
errors = []

def setup_drive():
    try:
        shutil.copyfile("/etc/secrets/credentials.json", "/tmp/credentials.json")
        shutil.copyfile("/etc/secrets/token.json", "/tmp/token.json")

        gauth = GoogleAuth()
        gauth.LoadCredentialsFile("/tmp/token.json")
        if gauth.credentials is None:
            gauth.LoadClientConfigFile("/tmp/credentials.json")
        elif gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()

        drive = GoogleDrive(gauth)
        logger.info("احراز هویت Google Drive موفق بود.")
        return drive
    except Exception as e:
        logger.error(f"خطا در احراز هویت گوگل: {e}")
        return None

drive = setup_drive()
ses = lt.session()
ses.listen_on(6881, 6891)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت یا فایل تورنت رو بفرست.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    msg = update.message.text.strip()
    if not msg.startswith("magnet:?xt="):
        return
    download_id = str(len(active) + 1)
    context.user_data["magnet"] = msg
    keyboard = [[
        InlineKeyboardButton("📥 تلگرام", callback_data=f"tg_{download_id}"),
        InlineKeyboardButton("☁️ گوگل درایو", callback_data=f"gd_{download_id}")
    ]]
    await update.message.reply_text("کجا ذخیره بشه؟", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if '_' not in query.data:
        return
    dest, download_id = query.data.split("_", 1)
    magnet = context.user_data.get("magnet")
    if not magnet:
        await query.edit_message_text("❌ لینک یافت نشد.")
        return
    await query.edit_message_text("⏳ شروع دانلود...")
    context.application.create_task(download(download_id, magnet, dest, query.message.chat_id, context))

async def download(id, magnet, destination, chat_id, context):
    params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
    handle = lt.add_magnet_uri(ses, magnet, params)
    while not handle.has_metadata():
        await asyncio.sleep(1)

    name = handle.name()
    await context.bot.send_message(chat_id=chat_id, text=f"⬇️ در حال دانلود: {name}")

    while not handle.is_seed():
        await asyncio.sleep(5)

    file_path = os.path.join(DOWNLOAD_DIR, name)
    if not os.path.exists(file_path):
        await context.bot.send_message(chat_id=chat_id, text="❌ فایل یافت نشد.")
        return

    try:
        if destination == "tg":
            await context.bot.send_document(chat_id=chat_id, document=open(file_path, "rb"))
        elif destination == "gd" and drive:
            gfile = drive.CreateFile({"title": name})
            gfile.SetContentFile(file_path)
            gfile.Upload()
            url = gfile["webContentLink"]
            await context.bot.send_message(chat_id=chat_id, text=f"✅ آپلود شد:\n{url}")
    except Exception as e:
        errors.append(str(e))
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        gc.collect()

async def report_errors(context: ContextTypes.DEFAULT_TYPE):
    if errors:
        await context.bot.send_message(chat_id=OWNER_ID, text="\n".join(errors))
        errors.clear()

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(report_errors(app.bot)), 'interval', seconds=300)
    scheduler.start()

    logger.info("ربات اجرا شد")
    app.run_polling()

if __name__ == "__main__":
    main()
