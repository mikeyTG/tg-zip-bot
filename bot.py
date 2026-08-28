import asyncio
from asyncio import Lock
from pathlib import Path
from shutil import rmtree
from zipfile import ZipFile, ZIP_STORED
import logging
import os
import re
import time

from dotenv import load_dotenv
from hydrogram import Client, filters
from hydrogram.types import Message
from web import start

load_dotenv()
start()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Hydrogram + tgcrypto is used for Telegram transfers.
# More concurrent downloads = faster total processing on a suitable Render instance.
CONC_MAX = int(os.environ.get('CONC_MAX', 6))
PROGRESS_EDIT_INTERVAL = float(os.environ.get('PROGRESS_EDIT_INTERVAL', 2.0))

STORAGE = Path("./files")
STORAGE.mkdir(parents=True, exist_ok=True)
MAX_SIZE = 2 * 1024 * 1024 * 1024

logging.basicConfig(
    format="[%(levelname)s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("MikeyZipBot")

# {user_id: [Message]}
tasks: dict[int, list[Message]] = {}
busy: set[int] = set()
status_locks: dict[int, Lock] = {}
last_status_update: dict[int, float] = {}

app = Client(
    "quick-zip-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


def bar(percent, length=12):
    percent = max(0, min(100, percent))
    n = int(length * percent / 100)
    return "█" * n + "░" * (length - n)


def fmt_mb(n):
    return n / (1024 * 1024)


def safe_name(name):
    name = os.path.basename(str(name or "file"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return (name or "file")[:240]


def clean(user_id):
    root = STORAGE / str(user_id)
    try:
        if root.exists():
            rmtree(root)
    except Exception:
        logger.exception("Could not clean files for %s", user_id)


async def edit_status(status, text, uid=None, force=False):
    """Throttle Telegram status edits so progress updates don't become a bottleneck."""
    if uid is not None and not force:
        now = time.monotonic()
        if now - last_status_update.get(uid, 0) < PROGRESS_EDIT_INTERVAL:
            return
        last_status_update[uid] = now

    lock = status_locks.setdefault(uid or 0, Lock())
    async with lock:
        try:
            await status.edit_text(text, parse_mode="html")
        except Exception:
            pass


@app.on_message(filters.command(["start", "help"]) & filters.private)
async def start_handler(client: Client, message: Message):
    await message.reply_text(
        "👋 <b>Welcome to Mikey ZIP Bot!</b>\n\n"
        "📦 Collect Telegram files and pack them into a ZIP.\n\n"
        "➕ <code>/add</code> — start collecting\n"
        "📦 <code>/zip filename</code> — create ZIP\n"
        "🗑 <code>/del__ID</code> — remove a file\n"
        "❌ <code>/cancel</code> — cancel\n\n"
        "⚡ ZIP uses <b>ZERO compression</b>, so files are packed directly.\n"
        "📊 Download, ZIP processing and upload progress are shown.",
        parse_mode="html",
    )


@app.on_message(filters.command("add") & filters.private)
async def add_handler(client: Client, message: Message):
    uid = message.from_user.id
    if uid in busy:
        await message.reply_text("⏳ Your ZIP is processing. Please wait.")
        return

    tasks[uid] = []
    clean(uid)
    await message.reply_text(
        "OK, send me your files. 📁\n\n"
        "Each file gets a delete command like <code>/del__123</code>.",
        parse_mode="html",
    )


@app.on_message(filters.regex(r"^/del__(\d+)$") & filters.private)
async def delete_handler(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in tasks:
        await message.reply_text("❌ Use /add first.")
        return

    mid = int(message.matches[0].group(1))
    found = next((m for m in tasks[uid] if m.id == mid), None)
    if found is None:
        await message.reply_text("❌ That file is not in your current list.")
        return

    tasks[uid].remove(found)
    await message.reply_text(
        f"🗑 <b>File removed.</b> Remaining: <b>{len(tasks[uid])}</b>",
        parse_mode="html",
    )


def has_supported_file(message: Message):
    return bool(
        message.document or message.video or message.audio or
        message.photo or message.animation
    )


@app.on_message(
    (filters.document | filters.video | filters.audio | filters.photo | filters.animation)
    & filters.private
)
async def file_handler(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in tasks or uid in busy:
        return

    tasks[uid].append(message)

    file_obj = (
        message.document or message.video or message.audio or
        message.photo or message.animation
    )
    name = safe_name(
        getattr(file_obj, "file_name", None)
        or getattr(file_obj, "file_name", None)
        or f"file_{message.id}"
    )
    size = getattr(file_obj, "file_size", None) or 0

    await message.reply_text(
        f"✅ <b>added</b> {name}\n\n"
        f"📦 {fmt_mb(size):.2f} MB\n\n"
        f"delete using <code>/del__{message.id}</code>",
        parse_mode="html",
    )


@app.on_message(filters.regex(r"^/zip(?:@\w+)?\s+([\w.-]+)$") & filters.private)
async def zip_handler(client: Client, message: Message):
    uid = message.from_user.id

    if uid in busy:
        await message.reply_text("⏳ A ZIP is already processing. Please wait.")
        return
    if uid not in tasks:
        await message.reply_text("❌ You must use /add first.")
        return
    if not tasks[uid]:
        await message.reply_text("❌ You haven't added any files yet.")
        return

    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", message.matches[0].group(1)) or "archive"
    root = STORAGE / str(uid)
    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / f"{name}.zip"

    status = None
    busy.add(uid)

    try:
        messages = [m for m in tasks[uid] if m is not None and has_supported_file(m)]
        if not messages:
            await message.reply_text("❌ No valid files found.")
            return

        def get_size(m):
            obj = m.document or m.video or m.audio or m.photo or m.animation
            return getattr(obj, "file_size", 0) or 0

        total = sum(get_size(m) for m in messages)
        if total > MAX_SIZE:
            await message.reply_text("❌ The total file size is larger than 2 GB.")
            return

        total_mb = fmt_mb(total)
        status = await message.reply_text(
            "📥 <b>Downloading files...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"💾 0.00 MB / {total_mb:.2f} MB\n"
            f"📁 Files: 0/{len(messages)}",
            parse_mode="html",
        )

        completed_bytes = 0
        completed_files = 0
        progress_lock = asyncio.Lock()
        progress_state = {"last_percent": -1, "last_edit": 0.0}

        async def progress_callback(current, file_total, status_message, action_text, start_time):
            # Hydrogram calls this while the transfer is running.
            # Keep the UI lightweight; Telegram message edits themselves can
            # become the bottleneck if done too frequently.
            now = time.monotonic()
            async with progress_lock:
                done_bytes = completed_bytes + current
                percent = int(done_bytes * 100 / total) if total else 0

                if (
                    percent != 100
                    and percent == progress_state["last_percent"]
                ):
                    return
                if percent != 100 and now - progress_state["last_edit"] < 1.5:
                    return

                progress_state["last_percent"] = percent
                progress_state["last_edit"] = now

            elapsed = max(time.time() - start_time, 0.001)
            speed = current / elapsed

            try:
                await status_message.edit_text(
                    "📥 <b>Downloading files...</b>\n\n"
                    f"📊 <b>{percent}%</b>\n{bar(percent)}\n"
                    f"💾 <b>{fmt_mb(done_bytes):.2f} MB</b> / {total_mb:.2f} MB\n"
                    f"📁 Files completed: <b>{completed_files}/{len(messages)}</b>\n"
                    f"⚡ Current speed: <b>{fmt_mb(speed):.2f} MB/s</b>",
                    parse_mode="html",
                )
            except Exception:
                pass

        semaphore = asyncio.Semaphore(max(1, CONC_MAX))

        async def download_one(index, msg):
            nonlocal completed_bytes, completed_files

            file_obj = msg.document or msg.video or msg.audio or msg.photo or msg.animation
            filename = safe_name(
                getattr(file_obj, "file_name", None) or f"file_{msg.id}"
            )
            path = root / filename

            if path.exists():
                path = root / f"{path.stem}_{index}{path.suffix}"

            async with semaphore:
                start_time = time.time()
                await msg.download(
                    file_name=str(path),
                    progress=progress_callback,
                    progress_args=(
                        status,
                        f"📥 Downloading {filename}",
                        start_time,
                    ),
                )

            size = path.stat().st_size if path.exists() else get_size(msg)
            async with progress_lock:
                completed_bytes += size
                completed_files += 1

            return path

        # Concurrent Telegram downloads are the main speed improvement.
        files = await asyncio.gather(
            *(download_one(i, msg) for i, msg in enumerate(messages, 1))
        )
        files = [p for p in files if p and p.exists()]

        await status.edit_text(
            "📦 <b>Creating ZIP (0% compression)...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"📁 Files: 0/{len(files)}\n"
            "⚡ No compression — direct ZIP packing",
            parse_mode="html",
        )

        # MKV/MP4 are already compressed. ZIP_STORED avoids CPU-heavy recompression.
        with ZipFile(zip_path, "w", compression=ZIP_STORED) as z:
            for i, path in enumerate(files, 1):
                z.write(path, arcname=path.name)
                percent = int(i * 100 / len(files))
                try:
                    await status.edit_text(
                        "📦 <b>Creating ZIP...</b>\n\n"
                        f"📊 <b>{percent}%</b>\n{bar(percent)}\n"
                        f"📁 Files: <b>{i}/{len(files)}</b>\n"
                        f"📄 {path.name}\n"
                        "⚡ No compression",
                        parse_mode="html",
                    )
                except Exception:
                    pass

        zip_size = zip_path.stat().st_size
        upload_total_mb = fmt_mb(zip_size)
        upload_start = time.time()
        upload_last = {"percent": -1, "time": 0.0}

        await status.edit_text(
            "📤 <b>Uploading ZIP...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"💾 0.00 MB / {upload_total_mb:.2f} MB",
            parse_mode="html",
        )

        async def upload_progress(current, total_upload, status_message, action_text, start_time):
            if not total_upload:
                return

            now = time.monotonic()
            percent = int(current * 100 / total_upload)

            if percent != 100 and now - upload_last["time"] < 1.5:
                return
            if percent == upload_last["percent"] and percent != 100:
                return

            upload_last["percent"] = percent
            upload_last["time"] = now

            elapsed = max(time.time() - start_time, 0.001)
            speed = current / elapsed

            try:
                await status_message.edit_text(
                    "📤 <b>Uploading ZIP...</b>\n\n"
                    f"📊 <b>{percent}%</b>\n{bar(percent)}\n"
                    f"💾 <b>{fmt_mb(current):.2f} MB</b> / {fmt_mb(total_upload):.2f} MB\n"
                    f"⚡ Speed: <b>{fmt_mb(speed):.2f} MB/s</b>",
                    parse_mode="html",
                )
            except Exception:
                pass

        await client.send_document(
            chat_id=uid,
            document=str(zip_path),
            caption=(
                "✅ <b>ZIP ready!</b>\n\n"
                f"📦 {zip_path.name}\n"
                f"💾 {upload_total_mb:.2f} MB\n"
                "⚡ Zero compression"
            ),
            progress=upload_progress,
            progress_args=(status, "📤 Uploading ZIP...", upload_start),
        )

        await status.edit_text(
            "✅ <b>Upload complete!</b>\n\n"
            f"📦 {zip_path.name}\n"
            f"💾 {upload_total_mb:.2f} MB\n"
            "📊 <b>100%</b>\n"
            "████████████",
            parse_mode="html",
        )

    except Exception as exc:
        logger.exception("ZIP operation failed for user %s", uid)
        text = f"❌ <b>ZIP failed.</b>\n\n<code>{type(exc).__name__}: {exc}</code>"
        try:
            if status:
                await status.edit_text(text, parse_mode="html")
            else:
                await message.reply_text(text, parse_mode="html")
        except Exception:
            logger.exception("Could not report error")
    finally:
        busy.discard(uid)
        tasks.pop(uid, None)
        status_locks.pop(uid, None)
        last_status_update.pop(uid, None)
        clean(uid)


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client: Client, message: Message):
    uid = message.from_user.id
    if uid in busy:
        await message.reply_text("⏳ ZIP processing is already running.")
        return

    tasks.pop(uid, None)
    clean(uid)
    await message.reply_text("❌ Canceled. Use /add to start again.")


if __name__ == "__main__":
    logger.info("Starting Mikey ZIP Bot with Hydrogram + tgcrypto...")
    app.run()
