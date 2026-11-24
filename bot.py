import asyncio
import logging
import os
import shutil
import time
import uuid

from pyrogram import Client, filters, idle
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import web as dashboard
import aiohttp
from storage import E2, friendly_error
from state import START_TIME, STATS, ErrorCapture, add_error, human_size, human_time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger().addHandler(ErrorCapture())
log = logging.getLogger("bot")

USER_PREFIX = {}  # user_id -> folder (key prefix) inside the bucket
e2 = E2()
sem = asyncio.Semaphore(config.MAX_PARALLEL)

app = Client(
    "e2_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=".",
)


# ---------- helpers ----------
def bar(done, total, width=20):
    pct = min(done / total, 1) if total else 0
    filled = int(width * pct)
    return f"{'█' * filled}{'░' * (width - filled)} {pct * 100:.1f}%"


def fmt_dur(seconds):
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class Meter:
    """Builds the progress text: bar, sizes, smoothed speed, ETA and elapsed time."""

    def __init__(self, title):
        self.title = title
        self.start = time.time()
        self.last_t, self.last_done, self.speed = self.start, 0, 0.0

    def text(self, done, total):
        now = time.time()
        dt = now - self.last_t
        if dt >= 0.5:
            inst = max(done - self.last_done, 0) / dt
            self.speed = inst if self.speed == 0 else 0.7 * self.speed + 0.3 * inst
            self.last_t, self.last_done = now, done
        eta = (total - done) / self.speed if self.speed > 0 and total else None
        lines = [
            f"**{self.title}**",
            "",
            f"`{bar(done, total)}`",
            "",
            f"📦 **Size:** {human_size(done)} / {human_size(total) if total else '?'}",
            f"⚡ **Speed:** {human_size(self.speed)}/s",
            f"⏳ **ETA:** {fmt_dur(eta) if eta is not None else 'calculating...'}",
            f"⏱ **Elapsed:** {fmt_dur(now - self.start)}",
        ]
        return "\n".join(lines)


class Progress:
    """Throttled async progress callback (for Telegram/aiohttp transfers)."""

    def __init__(self, msg, title):
        self.msg = msg
        self.meter = Meter(title)
        self.last = 0

    async def __call__(self, done, total):
        now = time.time()
        if now - self.last < 3 and done != total:
            return
        self.last = now
        try:
            await self.msg.edit_text(self.meter.text(done, total))
        except Exception:
            pass


def allowed(message: Message):
    if not config.ALLOWED_USERS:
        return True
    return message.from_user and message.from_user.id in config.ALLOWED_USERS


def get_media(message: Message):
    for kind in ("document", "video", "audio", "voice", "animation", "video_note"):
        media = getattr(message, kind, None)
        if media:
            return media
    if message.photo:
        return message.photo
    return None


# ---------- commands ----------
def display_name(m: Message):
    u = m.from_user
    return (u.first_name or u.username or "there") if u else "there"


@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    await m.reply_text(
        f"👋 **Hello, {display_name(m)}!**\n"
        f"Welcome to **{config.BOT_NAME}**\n\n"
        "• Send me any file (up to 4 GB) → I upload it to IDrive e2 and give you "
        "a share link and a download link.\n"
        "• Send me a direct `http(s)://` link → I download it and send the file here.\n"
        "• `/get <file key>` → send a file from your bucket to this chat.\n\n"
        "📁 `/folder` – show current folder, `/folder reset` to clear\n"
        "📊 `/status` – bot status and uptime\n"
        "❓ `/help` – full details and commands"
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, m: Message):
    await m.reply_text(
        f"📖 **{config.BOT_NAME} – Help**\n\n"
        "**What I do**\n"
        "Upload files to your IDrive e2 bucket and give you links, or fetch files and "
        "send them to Telegram.\n\n"
        "**Upload a file**\n"
        "Send any document, video, audio, voice note or photo (up to 4 GB; Telegram "
        "limits apply). You get a live progress card with percent, size, speed, ETA and "
        "elapsed time, then buttons for the **Share link** and **Download link**.\n\n"
        "**Download from a link**\n"
        "Send a direct `http(s)://` link. I download it and send the file here "
        "(up to 2 GB).\n\n"
        "**Commands**\n"
        "`/start` – welcome message\n"
        "`/help` – this message\n"
        "`/get <file key>` – send a bucket file to this chat (the key is shown after each upload)\n"
        "`/folder <name>` – upload into a folder of your bucket\n"
        "`/folder` – show the current folder\n"
        "`/folder reset` – back to the default folder\n"
        "`/status` – running time, active jobs and counters\n\n"
        "**Good to know**\n"
        f"• Links expire after {config.LINK_EXPIRY_DAYS} day(s); the file stays in your bucket.\n"
        f"• Up to {config.MAX_PARALLEL} transfers run at the same time; others wait in the queue.\n"
        "• If a transfer fails, the message shows the reason."
    )


@app.on_message(filters.command("folder") & filters.private)
async def folder(_, m: Message):
    uid = m.from_user.id
    if len(m.command) < 2:
        cur = USER_PREFIX.get(uid, config.E2_PREFIX) or "bucket root"
        return await m.reply_text(f"📁 Current folder: `{cur}`")
    arg = m.command[1].strip().strip("/")
    if arg.lower() == "reset":
        USER_PREFIX.pop(uid, None)
        return await m.reply_text("📁 Folder reset to the default.")
    USER_PREFIX[uid] = arg
    await m.reply_text(f"📁 Uploads will now go to folder `{arg}`.")


@app.on_message(filters.command("status") & filters.private)
async def status(_, m: Message):
    await m.reply_text(
        "🟢 **Bot is running**\n"
        f"⏱ Uptime: `{human_time(time.time() - START_TIME)}`\n"
        f"⚙️ Active jobs: `{STATS['active']}`\n"
        f"⬆️ Uploads to IDrive e2: `{STATS['uploads']}`\n"
        f"⬇️ Sent to Telegram: `{STATS['downloads']}`\n"
        f"🗄 IDrive e2: `{'configured: ' + config.E2_BUCKET if config.e2_configured() else 'NOT configured'}`"
    )


class ThreadProgress:
    """boto3 calls back from worker threads with bytes-just-sent. We only
    count bytes here; an async task shows them in the Telegram message."""

    def __init__(self, total):
        self.total, self.done = total, 0

    def __call__(self, n):
        self.done += n


async def show_progress(status_msg, title, tp):
    meter = Meter(title)
    while True:
        await asyncio.sleep(3)
        try:
            await status_msg.edit_text(meter.text(tp.done, tp.total))
        except Exception:
            pass


async def edit_no_preview(msg, text, reply_markup=None):
    """Edit a message without a link preview, on both old and new Kurigram/Pyrogram."""
    try:
        from pyrogram.types import LinkPreviewOptions
        await msg.edit_text(
            text, link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=reply_markup,
        )
    except (ImportError, TypeError):
        await msg.edit_text(text, disable_web_page_preview=True, reply_markup=reply_markup)


def need_e2():
    return None if config.e2_configured() else (
        "❌ IDrive e2 is not configured. Set E2_ENDPOINT, E2_ACCESS_KEY, E2_SECRET_KEY and E2_BUCKET."
    )


# ---------- Telegram -> IDrive e2 ----------
@app.on_message(
    (filters.document | filters.video | filters.audio | filters.voice
     | filters.photo | filters.animation | filters.video_note)
    & filters.private
)
async def tg_to_e2(_, m: Message):
    if not allowed(m):
        return await m.reply_text("⛔ You are not allowed to use this bot.")
    if need_e2():
        return await m.reply_text(need_e2())
    media = get_media(m)
    size = getattr(media, "file_size", 0) or 0
    if size > config.MAX_FILE_SIZE:
        return await m.reply_text(
            f"❌ File too large ({human_size(size)}). Limit: {human_size(config.MAX_FILE_SIZE)}."
        )

    t_start = time.time()
    status_msg = await m.reply_text("⏳ Queued...")
    job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    async with sem:
        STATS["active"] += 1
        try:
            path = await m.download(
                file_name=job_dir + os.sep,
                progress=Progress(status_msg, "⬇️ Step 1/2 · Telegram → Server"),
            )
            total = os.path.getsize(path)
            tp = ThreadProgress(total)
            upd = asyncio.create_task(show_progress(status_msg, "⬆️ Step 2/2 · Server → IDrive e2", tp))
            try:
                res = await e2.upload(path, tp, USER_PREFIX.get(m.from_user.id))
            finally:
                upd.cancel()
            STATS["uploads"] += 1
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Share link", url=res["share_link"])],
                [InlineKeyboardButton("⬇️ Download link", url=res["download_link"])],
            ])
            elapsed = fmt_dur(time.time() - t_start)
            await edit_no_preview(
                status_msg,
                "✅ **Upload complete**\n\n"
                f"📄 **File:** `{res['name']}`\n"
                f"📦 **Size:** {human_size(total)}\n"
                f"⏱ **Total time:** {elapsed}\n"
                f"🔑 **Key:** `{res['key']}`\n"
                f"⏳ Links valid for {config.LINK_EXPIRY_DAYS} day(s)",
                reply_markup=buttons,
            )
        except Exception as e:
            log.exception("upload failed")
            await status_msg.edit_text(f"❌ Failed: `{friendly_error(e)}`")
        finally:
            STATS["active"] -= 1
            shutil.rmtree(job_dir, ignore_errors=True)


# ---------- IDrive e2 key -> Telegram ----------
@app.on_message(filters.command("get") & filters.private)
async def e2_to_tg(_, m: Message):
    if not allowed(m):
        return await m.reply_text("⛔ You are not allowed to use this bot.")
    if need_e2():
        return await m.reply_text(need_e2())
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/get <file key>` (the key is shown after each upload)")
    key = m.text.split(None, 1)[1].strip()
    status_msg = await m.reply_text("🔎 Looking for the file...")
    job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex)
    async with sem:
        STATS["active"] += 1
        try:
            total = await e2.size(key)
            tp = ThreadProgress(total)
            upd = asyncio.create_task(show_progress(status_msg, "⬇️ Step 1/2 · IDrive e2 → Server", tp))
            try:
                path = await e2.download(key, os.path.join(job_dir, os.path.basename(key)), tp)
            finally:
                upd.cancel()
            await status_msg.edit_text("⬆️ Sending to Telegram...")
            await m.reply_document(
                path, caption=f"`{os.path.basename(key)}`",
                progress=Progress(status_msg, "⬆️ Step 2/2 · Server → Telegram"),
            )
            STATS["downloads"] += 1
            await status_msg.delete()
        except Exception as e:
            log.exception("e2 get failed")
            await status_msg.edit_text(f"❌ Failed: `{friendly_error(e)}`")
        finally:
            STATS["active"] -= 1
            shutil.rmtree(job_dir, ignore_errors=True)


# ---------- direct link -> Telegram ----------
@app.on_message(filters.text & filters.regex(r"^https?://\S+$") & filters.private)
async def url_to_tg(_, m: Message):
    if not allowed(m):
        return await m.reply_text("⛔ You are not allowed to use this bot.")
    url = m.text.strip()
    status_msg = await m.reply_text("🔎 Checking link...")
    job_dir = os.path.join(config.DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    async with sem:
        STATS["active"] += 1
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length", 0))
                    if total > config.MAX_FILE_SIZE:
                        raise RuntimeError(f"File too large ({human_size(total)})")
                    name = os.path.basename(r.url.path) or "file"
                    cd = r.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        name = cd.split("filename=")[-1].strip('" ;')
                    name = E2.clean_name(name)
                    path = os.path.join(job_dir, name)
                    prog = Progress(status_msg, f"⬇️ Step 1/2 · Link → Server · {name}")
                    done = 0
                    with open(path, "wb") as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
                            done += len(chunk)
                            await prog(done, total or done)
            await status_msg.edit_text(f"⬆️ Sending {name} to Telegram...")
            await m.reply_document(
                path, caption=f"`{name}`",
                progress=Progress(status_msg, f"⬆️ Step 2/2 · Server → Telegram · {name}"),
            )
            STATS["downloads"] += 1
            await status_msg.delete()
        except Exception as e:
            log.exception("url download failed")
            await status_msg.edit_text(f"❌ Failed: `{e}`")
        finally:
            STATS["active"] -= 1
            shutil.rmtree(job_dir, ignore_errors=True)


async def main():
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    runner = await dashboard.start_web(app, e2)  # dashboard starts first (Render needs the port)
    while True:  # keep trying so the bot never stays down
        try:
            await app.start()
            break
        except Exception as e:
            log.error("Bot failed to start: %r (retrying in 15s)", e)
            await asyncio.sleep(15)
    log.info("Bot started")
    try:
        await app.set_bot_commands([
            BotCommand("start", "Welcome message"),
            BotCommand("help", "Help and full details"),
            BotCommand("status", "Bot status and uptime"),
            BotCommand("folder", "Show or set upload folder"),
            BotCommand("get", "Send a bucket file to this chat"),
        ])
    except Exception as e:
        log.warning("Could not set bot commands: %r", e)
    await idle()
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
