import libtorrent as lt
import asyncio
import threading
import os
import shutil
import logging
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from concurrent.futures import ThreadPoolExecutor
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# --- تنظیمات ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
active_downloads = {}  # دیکشنری برای ذخیره دانلودهای فعال: {شناسه: (handle, thread)}

# --- پیکربندی Google Drive ---
temp_dir = "/tmp/vahab_auth"
os.makedirs(temp_dir, exist_ok=True)
token_path = os.path.join(temp_dir, "token.json")
creds_path = os.path.join(temp_dir, "credentials.json")
try:
    shutil.copyfile("/etc/secrets/token.json", token_path)
    shutil.copyfile("/etc/secrets/credentials.json", creds_path)
except FileNotFoundError as e:
    logging.error(f"فایل احراز هویت پیدا نشد: {e}")
    raise

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
except Exception as e:
    logging.error(f"خطا در احراز هویت Google Drive: {e}")
    raise

shutil.rmtree(temp_dir, ignore_errors=True)

# --- پیکربندی libtorrent ---
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

# --- دانلودکننده تورنت ---
class TorrentDownloader(threading.Thread):
    def __init__(self, download_id, magnet_link, context, loop, chat_id, destination):
        super().__init__()
        self.download_id = download_id
        self.magnet_link = magnet_link
        self.context = context
        self.loop = loop
        self.chat_id = chat_id
        self.destination = destination  # 'telegram' یا 'google_drive'
        self.handle = None
        self.running = True

    def run(self):
        global active_downloads
        params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
        try:
            self.handle = lt.add_magnet_uri(ses, self.magnet_link, params)
            active_downloads[self.download_id] = (self.handle, self)
            self.send_message(f"🔍 در حال دریافت اطلاعات تورنت (ID: {self.download_id})...")
            while not self.handle.has_metadata() and self.running:
                time.sleep(1)

            name = self.handle.name()
            self.send_message(f"⬇️ دانلود {name} (ID: {self.download_id}) آغاز شد")
            while not self.handle.is_seed() and self.running:
                s = self.handle.status()
                percent = int(s.progress * 100)
                speed = int(s.download_rate / 1000)
                progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
                self.send_message(f"📥 {name} (ID: {self.download_id}) - [{progress_bar}] {percent}% ({speed} KB/s)")
                time.sleep(30)

            if self.running and self.download_id in active_downloads:
                self.send_message(f"✅ دانلود کامل شد: {name} (ID: {self.download_id})")
                file_path = os.path.join(DOWNLOAD_DIR, name)
                if self.destination == "telegram":
                    if os.path.exists(file_path):
                        try:
                            with open(file_path, 'rb') as f:
                                asyncio.run_coroutine_threadsafe(
                                    self.context.bot.send_document(chat_id=self.chat_id, document=f),
                                    self.loop
                                )
                            self.send_message(f"📤 فایل {name} (ID: {self.download_id}) به تلگرام ارسال شد.")
                        except Exception as e:
                            self.send_message(f"❌ خطا در ارسال فایل (ID: {self.download_id}): {e}")
                        finally:
                            os.remove(file_path)
                elif self.destination == "google_drive":
                    try:
                        file = drive.CreateFile({'title': name})
                        file.SetContentFile(file_path)
                        file.Upload()
                        file_url = file['webContentLink']
                        self.send_message(f"📤 فایل {name} (ID: {self.download_id}) به گوگل درایو آپلود شد: {file_url}")
                        os.remove(file_path)
                    except Exception as e:
                        self.send_message(f"❌ خطا در آپلود به گوگل درایو (ID: {self.download_id}): {e}")
                del active_downloads[self.download_id]
        except Exception as e:
            self.send_message(f"❌ خطا در دانلود (ID: {self.download_id}): {e}")

    def send_message(self, text):
        asyncio.run_coroutine_threadsafe(self.context.bot.send_message(chat_id=self.chat_id, text=text), self.loop)

    def stop(self):
        self.running = False
        if self.handle:
            ses.remove_torrent(self.handle)
            self.send_message(f"⏹ دانلود (ID: {self.download_id}) متوقف شد.")

# --- فرمان /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت، فایل تورنت یا متنی بفرست. برای لیست دانلودها /list بفرست.")

# --- فرمان /list ---
async def list_downloads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    if not active_downloads:
        await context.bot.send_message(chat_id=OWNER_ID, text="📋 هیچ دانلود فعالی وجود نداره.")
        return
    keyboard = []
    for download_id, (handle, _) in active_downloads.items():
        s = handle.status()
        percent = int(s.progress * 100)
        speed = int(s.download_rate / 1000)
        progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
        row = [
            InlineKeyboardButton(f"ID: {download_id} [{progress_bar}] {percent}%", callback_data=f"status_{download_id}"),
            InlineKeyboardButton("توقف", callback_data=f"stop_{download_id}"),
            InlineKeyboardButton("حذف", callback_data=f"delete_{download_id}")
        ]
        keyboard.append(row)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=OWNER_ID, text="📋 لیست دانلودهای فعال:", reply_markup=reply_markup)

# --- فرمان /stop ---
async def stop_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    if not context.args:
        await context.bot.send_message(chat_id=OWNER_ID, text="❗ لطفاً شناسه دانلود رو وارد کن (مثال: /stop 1).")
        return
    download_id = context.args[0]
    if download_id in active_downloads:
        _, thread = active_downloads[download_id]
        thread.stop()
        del active_downloads[download_id]
        await context.bot.send_message(chat_id=OWNER_ID, text=f"⏹ دانلود (ID: {download_id}) متوقف شد.")
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"❌ دانلود با ID {download_id} پیدا نشد.")

# --- مدیریت کلیک دکمه‌ها ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    action = data[0]
    download_id = data[1]

    if action == "status":
        handle, _ = active_downloads.get(download_id, (None, None))
        if handle:
            s = handle.status()
            percent = int(s.progress * 100)
            speed = int(s.download_rate / 1000)
            progress_bar = "█" * (percent // 10) + "-" * (10 - percent // 10)
            await query.edit_message_text(f"📊 وضعیت (ID: {download_id}) - [{progress_bar}] {percent}% ({speed} KB/s)")
    elif action == "stop":
        if download_id in active_downloads:
            _, thread = active_downloads[download_id]
            thread.stop()
            del active_downloads[download_id]
            await query.edit_message_text(f"⏹ دانلود (ID: {download_id}) متوقف شد.")
    elif action == "delete":
        if download_id in active_downloads:
            ses.remove_torrent(active_downloads[download_id][0])
            del active_downloads[download_id]
            await query.edit_message_text(f"🗑 دانلود (ID: {download_id}) حذف شد.")

# --- پیام متنی ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    link = update.message.text.strip()
    if link.startswith("magnet:?xt="):
        download_id = str(len(active_downloads) + 1)
        context.user_data['magnet_link'] = link
        keyboard = [
            [
                InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"),
                InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(f"فایل کجا ذخیره بشه؟ (ID: {download_id})", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text="❗ لطفاً فقط لینک مگنت معتبر ارسال کنید.")

# --- مدیریت انتخاب مقصد ---
async def handle_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    destination = data[0]
    download_id = data[1]
    magnet_link = context.user_data.get('magnet_link')
    if not magnet_link:
        await query.message.reply_text("❌ خطا: لینک مگنت پیدا نشد.")
        return
    loop = asyncio.get_running_loop()
    t = TorrentDownloader(download_id, magnet_link, context, loop, query.message.chat_id, destination)
    executor.submit(t.run)
    await query.message.reply_text(f"📥 دانلود شروع شد (ID: {download_id}) و فایل در {destination} ذخیره می‌شه.")

# --- دریافت فایل تورنت یا متنی ---
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    file_path = "temp_file"
    try:
        file = await update.message.document.get_file()
        await file.download_to_drive(file_path)
        if file.file_name.endswith(('.torrent', '.txt')):
            with open(file_path, "r" if file.file_name.endswith('.txt') else "rb") as f:
                if file.file_name.endswith('.txt'):
                    for line in f:
                        line = line.strip()
                        if line.startswith("magnet:?xt="):
                            download_id = str(len(active_downloads) + 1)
                            context.user_data['magnet_link'] = line
                            keyboard = [
                                [
                                    InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"),
                                    InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")
                                ]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await update.message.reply_text(f"فایل {line} کجا ذخیره بشه? (ID: {download_id})", reply_markup=reply_markup)
                else:
                    download_id = str(len(active_downloads) + 1)
                    context.user_data['magnet_link'] = file_path
                    keyboard = [
                        [
                            InlineKeyboardButton("تلگرام", callback_data=f"telegram_{download_id}"),
                            InlineKeyboardButton("گوگل درایو", callback_data=f"google_drive_{download_id}")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(f"فایل تورنت کجا ذخیره بشه? (ID: {download_id})", reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=OWNER_ID, text="❗ فقط فایل‌های تورنت (.torrent) یا متنی (.txt) پشتیبانی می‌شوند.")
    except Exception as e:
        await context.bot.send_message(chat_id=OWNER_ID, text=f"❌ خطا در پردازش فایل: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# --- اجرای ربات ---
executor = ThreadPoolExecutor(max_workers=5)

def main():
    logging.basicConfig(level=logging.INFO)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_downloads))
    application.add_handler(CommandHandler("stop", stop_download))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(CallbackQueryHandler(handle_callback))

    application.run_polling()

if __name__ == "__main__":
    main()
