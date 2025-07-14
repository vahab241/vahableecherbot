import os
import asyncio
import logging
import libtorrent as lt
from time import sleep
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

drive = None
def setup_drive():
    global drive
    if os.path.exists("/etc/secrets/credentials.json") and os.path.exists("/etc/secrets/token.json"):
        gauth = GoogleAuth()
        gauth.LoadCredentialsFile("/etc/secrets/token.json")
        if gauth.access_token_expired:
            gauth.Refresh()
        else:
            gauth.Authorize()
        drive = GoogleDrive(gauth)
        logger.info("Google Drive authenticated.")
    else:
        logger.warning("Google Drive credentials not found.")
setup_drive()

ses = lt.session()
ses.listen_on(6881, 6891)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت تورنت بفرست.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("فقط ادمین مجازه.")
        return
    magnet = update.message.text.strip()
    if not magnet.startswith("magnet:?xt="):
        await update.message.reply_text("❌ لینک مگنت معتبر نیست.")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("تلگرام", callback_data=f"tg|{magnet}")],
        [InlineKeyboardButton("گوگل درایو", callback_data=f"gd|{magnet}")],
    ])
    await update.message.reply_text("✅ کجا ذخیره بشه؟", reply_markup=keyboard)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method, magnet = query.data.split("|", 1)
    await query.edit_message_text("در حال دانلود...")
    params = {"save_path": DOWNLOAD_DIR}
    handle = lt.add_magnet_uri(ses, magnet, params)
    while not handle.has_metadata():
        await asyncio.sleep(1)
    name = handle.name()
    await query.edit_message_text(f"دانلود فایل {name} آغاز شد.")
    while not handle.is_seed():
        await asyncio.sleep(5)
    fpath = os.path.join(DOWNLOAD_DIR, name)
    if method == "tg":
        with open(fpath, "rb") as f:
            await query.message.reply_document(document=f)
    elif method == "gd" and drive:
        file_drive = drive.CreateFile({"title": name})
        file_drive.SetContentFile(fpath)
        file_drive.Upload()
        link = file_drive["webContentLink"]
        await query.message.reply_text(f"آپلود شد به گوگل درایو:
{link}")
    os.remove(fpath)

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.run_polling()

if __name__ == "__main__":
    main()