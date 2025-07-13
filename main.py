import libtorrent as lt
import asyncio
import threading
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
from concurrent.futures import ThreadPoolExecutor
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
import fcntl
import socket

# --- تنظیمات ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN is not set in environment variables.")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"  # تغییر به مسیر موقت برای Render
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
active_downloads = {}  # {download_id: (handle, thread)}
errors = []
LOCK_FILE = "/tmp/vahab_bot.lock"
PORT = int(os.environ.get("PORT", 8080))  # پورت صوری برای Render

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
        # استفاده از مسیر استاندارد اسرار توی Render
        if os.path.exists("/etc/secrets/token.json") and os.path.exists("/etc/secrets/credentials.json"):
            shutil.copyfile("/etc/secrets/token.json", token_path)
            shutil.copyfile("/etc/secrets/credentials.json", creds_path)
        else:
            logger.error("فایل‌های احراز هویت Google Drive توی /etc/secrets/ پیدا نشد. آپلود به گوگل درایو غیرفعال است.")
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
class TorrentDownloader(threading.Thread):
    def __init__(self, download_id: str, magnet_link: str, context: ContextTypes.DEFAULT_TYPE, loop: asyncio.AbstractEventLoop, chat_id: int, destination: str):
        super().__init__(daemon=True)
        self.download_id = download_id
        self.magnet_link = magnet_link
        self.context = context
        self.loop = loop
        self.chat_id = chat_id
        self.destination = destination
        self.handle = None
        self.running = True
        self.completed = False

    def run(self):
        global active_downloads, errors
        if not drive and self.destination == "google_drive":
            errors.append(f"احراز هویت Google Drive برای دانلود (ID: {self.download_id}) انجام نشده.")
            return
        params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
        try:
            self.handle = lt.add_magnet_uri(ses, self.magnet_link, params)
            active_downloads[self.download_id] = (self.handle, self)
            self._send_message(f"<b>🔍 در حال دریافت اطلاعات تورنت</b> (ID: {self.download_id})...")
            while not self.handle.has_metadata() and self.running:
                time.sleep(1)

            name = self.handle.name()
            self._send_message(f"<b>⬇️ دانلود آغاز شد</b>: *{name}* (ID: {self.download_id})")
            while not self.handle.is_seed() and self.running:
                s = self.handle.status()
                percent = int(s.progress * 100)
                speed = int(s.download_rate / 1000)
                progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
                self._send_message(
                    f"<b>📥 در حال دانلود</b> *{name}* (ID: {self.download_id})\n[{progress_bar}] <i>{percent}% ({speed} KB/s)</i>"
                )
                time.sleep(5)

            if self.running and self.download_id in active_downloads:
                self.completed = True
                self._send_message(f"<b>✅ دانلود کامل شد</b>: *{name}* (ID: {self.download_id})")
                file_path = os.path.join(DOWNLOAD_DIR, name)
                if os.path.exists(file_path):
                    if self.destination == "telegram":
                        try:
                            with open(file_path, "rb") as f:
                                msg = asyncio.run_coroutine_threadsafe(
                                    self.context.bot.send_document(chat_id=self.chat_id, document=f), self.loop
                                ).result(timeout=60)
                            self._send_completion_message(name, msg.message_id)
                        except Exception as e:
                            errors.append(f"خطا در ارسال فایل (ID: {self.download_id}): {e}")
                        finally:
                            os.remove(file_path)
                    elif self.destination == "google_drive":
                        try:
                            file_drive = drive.CreateFile({"title": name})
                            file_drive.SetContentFile(file_path)
                            file_drive.Upload()
                            file_url = file_drive["webContentLink"]
                            self._send_completion_message(name, file_url)
                            os.remove(file_path)
                        except Exception as e:
                            errors.append(f"خطا در آپلود به گوگل درایو (ID: {self.download_id}): {e}")
                del active_downloads[self.download_id]
                gc.collect()
        except Exception as e:
            errors.append(f"خطا در دانلود (ID: {self.download_id}): {e}")

    def _send_message(self, text: str):
        asyncio.run_coroutine_threadsafe(
            self.context.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="HTML"), self.loop
        ).result(timeout=30)

    def _send_completion_message(self, name: str, link_or_message_id: str | int):
        keyboard = [[InlineKeyboardButton("مشاهده", callback_data=f"view_{self.download_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"<b>📤 فایل آماده است</b>: *{name}*" + (
            f"\nلینک: {link_or_message_id}" if isinstance(link_or_message_id, str) else ""
        )
        asyncio.run_coroutine_threadsafe(
            self.context.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup
            ),
            self.loop,
        ).result(timeout=30)

    def stop(self):
        self.running = False
        if self.handle:
            ses.remove_torrent(self.handle)
            self._send_message(f"<b>⏹ دانلود متوقف شد</b> (ID: {self.download_id})")

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
        for did, (handle, _) in active_downloads.items()
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
        _, thread = active_downloads[download_id]
        thread.stop()
        del active_downloads[download_id]
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>⏹ دانلود متوقف شد</b> (ID: {download_id}).", parse_mode="HTML")
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"<b>❌ دانلود با ID {download_id} پیدا نشد</b>.", parse_mode="HTML")

# --- مدیریت کلیک دکمه‌ها ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, download_id = query.data.split("_")[0], query.data.split("_")[1]

    if action == "status":
        handle, _ = active_downloads.get(download_id, (None, None))
        if handle:
            s = handle.status()
            percent = int(s.progress * 100)
            speed = int(s.download_rate / 1000)
            progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
            await query.edit_message_text(
                f"<b>📊 وضعیت</b> (ID: {download_id})\n[{progress_bar}] <i>{percent}% ({speed} KB/s)</i>", parse_mode="HTML"
            )
    elif action == "stop":
        if download_id in active_downloads:
            _, thread = active_downloads[download_id]
            thread.stop()
            del active_downloads[download_id]
            await query.edit_message_text(f"<b>⏹ دانلود متوقف شد</b> (ID: {download_id}).", parse_mode="HTML")
    elif action == "delete":
        if download_id in active_downloads:
            ses.remove_torrent(active_downloads[download_id][0])
            del active_downloads[download_id]
            await query.edit_message_text(f"<b>🗑 دانلود حذف شد</b> (ID: {download_id}).", parse_mode="HTML")
    elif action == "view" and download_id in active_downloads and active_downloads[download_id][1].completed:
        handle, _ = active_downloads[download_id]
        name = handle.name()
        file_path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                await query.message.reply_document(document=f)
        else:
            await query.edit_message_text(f"<b>❌ فایل {name} (ID: {download_id}) پیدا نشد</b>.", parse_mode="HTML")

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

# --- مدیریت انتخاب مقصد ---
async def handle_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    destination, download_id = data[0], data[1]
    magnet_link = context.user_data.get("magnet_link")
    if not magnet_link:
        await query.message.reply_text("<b>❌ خطا: لینک مگنت پیدا نشد</b>.", parse_mode="HTML")
        return
    loop = asyncio.get_event_loop()
    downloader = TorrentDownloader(download_id, magnet_link, context, loop, query.message.chat_id, destination)
    executor.submit(downloader.run)
    await query.message.edit_text(f"<b>📥 دانلود شروع شد</b> (ID: {download_id}) و فایل در *{destination}* ذخیره می‌شه.", parse_mode="HTML")

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
def acquire_lock():
    try:
        lock_file = open(LOCK_FILE, "w")
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except IOError:
        return None

def release_lock(lock_file):
    if lock_file:
        lock_file.close()

def main():
    lock_file = acquire_lock()
    if not lock_file:
        logger.error("ربات در حال اجرا است. فقط یک نمونه مجاز است.")
        return

    executor = ThreadPoolExecutor(max_workers=3)  # کاهش برای بهینه‌سازی
    try:
        # باز کردن پورت صوری برای Render
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", PORT))
        sock.listen(1)
        logger.info(f"پورت صوری {PORT} باز شد.")

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
        release_lock(lock_file)
        executor.shutdown(wait=False)
        if "sock" in locals():
            sock.close()

if __name__ == "__main__":
    main()
