import libtorrent as lt
import asyncio
import os
import shutil
import logging
import time
import gc
import threading
import socket  # اضافه کردن ماژول socket
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
PORT = int(os.environ.get("PORT", 8080))

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
ses = lt.session({"listen_interfaces": "0.0.0.0:6881"})

# --- دانلودکننده تورنت ---
async def download_torrent(download_id: str, magnet_link: str, context: ContextTypes.DEFAULT_TYPE, chat_id: int, destination: str, message_id: int):
    global active_downloads, errors
    if not drive and destination == "google_drive":
        errors.append(f"احراز هویت Google Drive برای دانلود (ID: {download_id}) انجام نشده.")
        return
    params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
    handle = lt.add_magnet_uri(ses, magnet_link, params)
    active_downloads[download_id] = (handle, None, message_id)
    await context.bot.send_message(chat_id=chat_id, text=f"<b>🔍 در حال دریافت اطلاعات تورنت</b> (ID: {download_id})...", parse_mode="HTML")
    while not handle.has_metadata():
        await asyncio.sleep(1)

    name = handle.name()
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=f"<b>⬇️ دانلود آغاز شد</b>: *{name}* (ID: {download_id})",
        parse_mode="HTML",
    )
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
                with open(file_path, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f)
                keyboard = [[InlineKeyboardButton("مشاهده", callback_data=f"view_{download_id}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>📤 فایل آماده است</b>: *{name}*",
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                errors.append(f"خطا در ارسال فایل (ID: {download_id}): {e}")
            finally:
                os.remove(file_path)
        elif destination == "google_drive":
            try:
                file_drive = drive.CreateFile({"title": name})
                file_drive.SetContentFile(file_path)
                file_drive.Upload()
                file_url = file_drive["webContentLink"]
                keyboard = [[InlineKeyboardButton("مشاهده", callback_data=f"view_{download_id}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"<b>📤 فایل آماده است</b>: *{name}*\nلینک: {file_url}",
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                errors.append(f"خطا در آپلود به گوگل درایو (ID: {download_id}): {e}")
            finally:
                os.remove(file_path)
    del active_downloads[download_id]
    gc.collect()

# --- تابع اعلان خطاها ---
async def check_errors(context: ContextTypes.DEFAULT_TYPE):
    global errors
    if errors:
        error_msg = "\n".join(errors)
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>❌ خطاها:</b>\n{error_msg}", parse_mode="HTML")
        errors.clear()

# --- فرمان‌ها ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>سلام!</b> لینک مگنت، فایل تورنت یا متنی بفرست. برای لیست دانلودها /list بفرست.", parse_mode="HTML"
    )

async def list_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    if not active_downloads:
        await context.bot.send_message(chat_id=OWNER_ID, text="<b>📋 هیچ دانلود فعالی وجود نداره.</b>", parse_mode="HTML")
        return
    keyboard = [
        [
            InlineKeyboardButton(f"ID: {did} [{progress_bar}] {percent}%", callback_data=f"status_{did}"),
            InlineKeyboardButton("توقف", callback_data=f"stop_{did}"),
            InlineKeyboardButton("حذف", callback_data=f"delete_{did}"),
        ]
        for did, (handle, _, _) in active_downloads.items()
        if (s := handle.status()) and (percent := int(s.progress * 100)) and (speed := int(s.download_rate / 1000)) and (
            progress_bar := "█" * (percent // 10) + "-" * (10 - percent // 10)
        )
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=OWNER_ID, text="<b>📋 لیست دانلودهای فعال:</b>", parse_mode="HTML", reply_markup=reply_markup)

async def stop_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    if not context.args:
        await context.bot.send_message(chat_id=OWNER_ID, text="<b>❗ لطفاً شناسه دانلود رو وارد کن</b> (مثال: /stop 1).", parse_mode="HTML")
        return
    download_id = context.args[0]
    if download_id in active_downloads:
        handle, _, _ = active_downloads[download_id]
        ses.remove_torrent(handle)
        del active_downloads[download_id]
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>⏹ دانلود متوقف شد</b> (ID: {download_id}).", parse_mode="HTML")
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>❌ دانلود با ID {download_id} پیدا نشد</b>.", parse_mode="HTML")

# --- مدیریت کلیک دکمه‌ها ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, download_id = query.data.split("_", 1)  # جدا کردن فقط با اولین _
    logger.info(f"دکمه کلیک شد: action={action}, download_id={download_id}")

    if action == "status":
        handle, _, message_id = active_downloads.get(download_id, (None, None, None))
        if handle:
            s = handle.status()
            percent = int(s.progress * 100)
            speed = int(s.download_rate / 1000)
            progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=message_id,
                text=f"<b>📊 وضعیت</b> (ID: {download_id})\n[{progress_bar}] <i>{percent}% ({speed} KB/s)</i>",
                parse_mode="HTML",
            )
    elif action == "stop":
        if download_id in active_downloads:
            handle, _, message_id = active_downloads[download_id]
            ses.remove_torrent(handle)
            del active_downloads[download_id]
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=message_id,
                text=f"<b>⏹ دانلود متوقف شد</b> (ID: {download_id}).",
                parse_mode="HTML",
            )
    elif action == "delete":
        if download_id in active_downloads:
            handle, _, message_id = active_downloads[download_id]
            ses.remove_torrent(handle)
            del active_downloads[download_id]
            await context.bot.edit_message_text(
                chat_id=query.message.chat_id,
                message_id=message_id,
                text=f"<b>🗑 دانلود حذف شد</b> (ID: {download_id}).",
                parse_mode="HTML",
            )
    elif action == "view" and download_id in active_downloads:
        handle, _, _ = active_downloads[download_id]
        name = handle.name()
        file_path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                await query.message.reply_document(document=f)
        else:
            await query.edit_message_text(f"<b>❌ فایل {name} (ID: {download_id}) پیدا نشد</b>.", parse_mode="HTML")
    elif action in ("telegram", "google_drive"):
        magnet_link = context.user_data.get("magnet_link")
        if not magnet_link:
            await query.edit_message_text("<b>❌ خطا: لینک مگنت پیدا نشد</b>.", parse_mode="HTML")
            return
        await query.edit_message_text(f"<b>📥 دانلود شروع شد</b> (ID: {download_id}) و فایل در *{action}* ذخیره می‌شه.", parse_mode="HTML")
        task = asyncio.create_task(download_torrent(download_id, magnet_link, context, query.message.chat_id, action, query.message.message_id))
        active_downloads[download_id] = (None, task, query.message.message_id)

# --- پیام متنی ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    link = update.message.text.strip()
    if link.startswith("magnet:?xt="):
        download_id = str(len(active_downloads) + 1)
        context.user_data["magnet_link"] = link
        keyboard = [[InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"), InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"<b>فایل کجا ذخیره بشه؟</b> (ID: {download_id})", parse_mode="HTML", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text="<b>❗ لطفاً فقط لینک مگنت معتبر ارسال کنید</b>.", parse_mode="HTML")

# --- دریافت فایل تورنت یا متنی ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    file_path = "/tmp/temp_file"
    try:
        file = await update.message.document.get_file()
        await file.download_to_drive(file_path)
        if file.file_name.endswith((".torrent", ".txt")):
            with open(file_path, "r" if file.file_name.endswith(".txt") else "rb") as f:
                if file.file_name.endswith(".txt"):
                    for line in f:
                        line = line.strip()
                        if line.startswith("magnet:?xt="):
                            download_id = str(len(active_downloads) + 1)
                            context.user_data["magnet_link"] = line
                            keyboard = [
                                [InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"), InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await update.message.reply_text(
                                f"<b>فایل {line} کجا ذخیره بشه?</b> (ID: {download_id})", parse_mode="HTML", reply_markup=reply_markup
                            )
                else:
                    download_id = str(len(active_downloads) + 1)
                    context.user_data["magnet_link"] = file_path
                    keyboard = [
                        [InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"), InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(
                        f"<b>فایل تورنت کجا ذخیره بشه?</b> (ID: {download_id})", parse_mode="HTML", reply_markup=reply_markup
                    )
        else:
            await context.bot.send_message(chat_id=OWNER_ID, text="<b>❗ فقط فایل‌های تورنت (.torrent) یا متنی (.txt) پشتیبانی می‌شوند</b>.", parse_mode="HTML")
    except Exception as e:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>❌ خطا در پردازش فایل</b>: {e}", parse_mode="HTML")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# --- اجرای ربات ---
def run_dummy_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PORT))
    sock.listen(1)
    logger.info(f"پورت صوری {PORT} باز شد.")
    while True:
        conn, addr = sock.accept()
        conn.close()

def main():
    lock = FileLock(LOCK_FILE, timeout=1)
    try:
        with lock:
            logger.info("قفل با موفقیت گرفته شد. اجرای ربات شروع شد.")
            # اجرای سرور صوری توی ترد جدا
            dummy_thread = threading.Thread(target=run_dummy_server, daemon=True)
            dummy_thread.start()

            # ساخت اپلیکیشن
            application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

            # تنظیم تسک دوره‌ای
            application.job_queue.run_repeating(check_errors, interval=300)

            # اضافه کردن هندلرها
            application.add_handler(CommandHandler("start", start))
            application.add_handler(CommandHandler("list", list_downloads))
            application.add_handler(CommandHandler("stop", stop_download))
            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
            application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
            application.add_handler(CallbackQueryHandler(handle_callback))

            # اجرای ربات
            application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"خطا در اجرای ربات: {e}", exc_info=True)
    finally:
        if "dummy_thread" in locals():
            dummy_thread.join(timeout=5)

if __name__ == "__main__":
    main()
