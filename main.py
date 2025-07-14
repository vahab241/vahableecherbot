import os, shutil, asyncio, zipfile, logging, tempfile, gc
import libtorrent as lt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from functools import partial

# ——— تنظیمات ———
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
active_queue = {}  # download queue {id: info}
counter = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger()

# ——— Google Drive Auth ———
def drive_auth():
    base = "/etc/secrets"
    paths = ["credentials.json","token.json"]
    if all(os.path.exists(f"{base}/{p}") for p in paths):
        shutil.copy(f"{base}/credentials.json","/tmp/cred.json")
        shutil.copy(f"{base}/token.json","/tmp/token.json")
    else:
        logger.error("Missing Google Drive auth files.")
        return None
    g = GoogleAuth()
    g.LoadCredentialsFile("/tmp/token.json")
    if not g.credentials or g.access_token_expired:
        g.Refresh()
    drive = GoogleDrive(g)
    logger.info("✅ Google Drive authenticated")
    return drive

DRIVE = drive_auth()

# ——— تورنت دانلود ———
def seed_loop(ses):
    ses.start_dht()
    ses.listen_on(6881, 6891)

async def process_task(tid, tg_id, magnet, dest, context):
    ses = lt.session()
    seed_loop(ses)
    params = {"save_path": DOWNLOAD_DIR}
    handle = lt.add_magnet_uri(ses, magnet, params)
    name = None
    while not handle.has_metadata():
        await asyncio.sleep(1)
    name = handle.name()
    msg = await context.bot.send_message(tg_id, f"⬇️ شروع دانلود: *{name}*", parse_mode="Markdown")
    while not handle.is_seed():
        s = handle.status()
        p = int(s.progress * 100)
        spd = int(s.download_rate/1024)
        bar = "█"*(p//10) + "-"*(10-p//10)
        await msg.edit_text(f"`[{bar}] {p}% @ {spd} KB/s`", parse_mode="Markdown")
        await asyncio.sleep(3)
    path = os.path.join(DOWNLOAD_DIR, name)
    zip_path = os.path.join(DOWNLOAD_DIR, name+".zip")
    zipf = zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED)
    if os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                full = os.path.join(root,f)
                zipf.write(full, os.path.relpath(full, path))
    else:
        zipf.write(path, name)
    zipf.close()
    target = zip_path
    if dest=="telegram":
        await context.bot.send_document(tg_id, open(target,"rb"))
    else:
        if DRIVE:
            f = DRIVE.CreateFile({"title": os.path.basename(target)})
            f.SetContentFile(target)
            f.Upload()
            link = f["webContentLink"]
            await context.bot.send_message(tg_id, f"Drive لینک:\n{link}")
        else:
            await context.bot.send_message(tg_id, "❌ Drive auth failed")
    shutil.rmtree(path, ignore_errors=True)
    os.remove(zip_path)
    await msg.delete()
    active_queue.pop(tid, None)
    gc.collect()

# ——— Handlers ———
async def cmd_start(u,c): await u.message.reply_text("Send magnet link or .torrent file")

async def cmd_queue(u,c):
    lines = [f"{tid}: {info['magnet'][:40]}... -> {info['dest']}" for tid, info in active_queue.items()]
    await c.bot.send_message(u.effective_chat.id, "\n".join(lines) or "Empty queue")

async def handle_text(u,c):
    global counter
    mg = u.message.text.strip()
    if not mg.startswith("magnet:?xt="): return
    counter+=1
    tid = str(counter)
    active_queue[tid] = {"magnet":mg,"dest":None,"chat":u.effective_chat.id}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Telegram", f"dst_tel_{tid}"), InlineKeyboardButton("Drive", f"dst_drv_{tid}")]])
    await u.message.reply_text(f"Choose destination for ID {tid}", reply_markup=kb)

async def handle_cb(u,c):
    cb = u.callback_query
    await cb.answer()
    typ,tid = cb.data.split("_",2)[1:]
    info = active_queue.get(tid)
    if not info: return
    info['dest'] = "telegram" if typ=="tel" else "drive"
    await cb.edit_message_text(f"Queued ID {tid} to {info['dest']}")
    c.application.create_task(process_task(tid, info['chat'], info['magnet'], info['dest'], c))

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("queue", cmd_queue))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
application.add_handler(CallbackQueryHandler(handle_cb))
application.run_polling()
