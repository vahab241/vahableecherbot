import libtorrent as lt
import asyncio
import threading
import os
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

# --- پیکربندی Google Drive ---
gauth = GoogleAuth()
gauth.LoadCredentialsFile("token.json")
if gauth.credentials is None:
    gauth.LoadClientConfigFile("credentials.json")
    gauth.LocalWebserverAuth()  # برای احراز هویت اولیه (فقط توی Codespace)
elif gauth.access_token_expired:
    gauth.Refresh()
else:
    gauth.Authorize()
gauth.SaveCredentialsFile("token.json")
drive = GoogleDrive(gauth)

# --- پیکربندی libtorrent ---
ses = lt.session({'listen_interfaces': '0.0.0.0:6881'})

# --- دانلودکننده تورنت ---
class TorrentDownloader(threading.Thread):
    def __init__(self, magnet_link, context, loop, chat_id, destination):
        super().__init__()
        self.magnet_link = magnet_link
        self.context = context
        self.loop = loop
        self.chat_id = chat_id
        self.destination = destination  # 'telegram' یا 'google_drive'

    def run(self):
        params = {"save_path": DOWNLOAD_DIR, "storage_mode": lt.storage_mode_t(2)}
        try:
            handle = lt.add_magnet_uri(ses, self.magnet_link, params)
        except Exception as e:
            self.send_message(f"❌ خطا در افزودن لینک مگنت: {e}")
            return

        self.send_message("🔍 در حال دریافت اطلاعات تورنت...")
        while not handle.has_metadata():
            time.sleep(1)

        name = handle.name()
        self.send_message(f"⬇️ دانلود {name} آغاز شد")
        while not handle.is_seed():
            s = handle.status()
            percent = int(s.progress * 100)
            speed = int(s.download_rate / 1000)
            self.send_message(f"📥 {name} - {percent}% ({speed} KB/s)")
            time.sleep(30)

        self.send_message(f"✅ دانلود کامل شد: {name}")
        file_path = os.path.join(DOWNLOAD_DIR, name)
        
        if self.destination == "telegram":
            if os.path.exists(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        asyncio.run_coroutine_threadsafe(
                            self.context.bot.send_document(chat_id=self.chat_id, document=f),
                            self.loop
                        )
                    self.send_message("📤 فایل به تلگرام ارسال شد.")
                except Exception as e:
                    self.send_message(f"❌ خطا در ارسال فایل به تلگرام: {e}")
                finally:
                    os.remove(file_path)  # حذف فایل بعد از ارسال
        elif self.destination == "google_drive":
            try:
                file = drive.CreateFile({'title': name})
                file.SetContentFile(file_path)
                file.Upload()
                file_url = file['webContentLink']
                self.send_message(f"📤 فایل به گوگل درایو آپلود شد: {file_url}")
                os.remove(file_path)  # حذف فایل بعد از آپلود
            except Exception as e:
                self.send_message(f"❌ خطا در آپلود به گوگل درایو: {e}")

    def send_message(self, text):
        asyncio.run_coroutine_threadsafe(self.context.bot.send_message(chat_id=self.chat_id, text=text), self.loop)

# --- فرمان /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام! لینک مگنت، فایل تورنت یا متنی بفرست.")

# --- پیام متنی ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await context.bot.send_message(chat_id=update.effective_user.id, text="❗ فقط مالک ربات می‌تونه ازش استفاده کنه.")
        return
    link = update.message.text.strip()
    if link.startswith("magnet:?xt="):
        context.user_data['magnet_link'] = link
        keyboard = [
            [
                InlineKeyboardButton("تلگرام", callback_data="telegram"),
                InlineKeyboardButton("گوگل درایو", callback_data="google_drive")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("فایل کجا ذخیره بشه؟", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=OWNER_ID, text="❗ لطفاً فقط لینک مگنت معتبر ارسال کنید.")

# --- مدیریت انتخاب مقصد ---
async def handle_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    destination = query.data  # 'telegram' یا 'google_drive'
    magnet_link = context.user_data.get('magnet_link')
    if not magnet_link:
        await query.message.reply_text("❌ خطا: لینک مگنت پیدا نشد.")
        return
    loop = asyncio.get_running_loop()
    t = TorrentDownloader(magnet_link, context, loop, query.message.chat_id, destination)
    executor.submit(t.run)
    await query.message.reply_text(f"📥 دانلود شروع شد و فایل در {destination} ذخیره می‌شه.")

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
                            context.user_data['magnet_link'] = line
                            keyboard = [
                                [
                                    InlineKeyboardButton("تلگرام", callback_data="telegram"),
                                    InlineKeyboardButton("گوگل درایو", callback_data="google_drive")
                                ]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            await update.message.reply_text(f"فایل {line} کجا ذخیره بشه؟", reply_markup=reply_markup)
                else:  # فایل تورنت
                    context.user_data['magnet_link'] = file_path  # مسیر فایل تورنت
                    keyboard = [
                        [
                            InlineKeyboardButton("تلگرام", callback_data="telegram"),
                            InlineKeyboardButton("گوگل درایو", callback_data="google_drive")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(f"فایل تورنت کجا ذخیره بشه؟", reply_markup=reply_markup)
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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    application.add_handler(CallbackQueryHandler(handle_destination))

    application.run_polling()

if __name__ == "__main__":
    main()
