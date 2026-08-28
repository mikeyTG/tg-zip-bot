import asyncio
import logging
import os
import re
import time
from pathlib import Path
from shutil import rmtree
from zipfile import ZIP_STORED, ZipFile

from dotenv import load_dotenv
from hydrogram import Client, filters
from hydrogram.types import Message

try:
    from web import start
    start()
except Exception:
    # The bot must still start if the optional Render health server is unavailable.
    logging.getLogger("MikeyZipBot").exception("Web server could not start")

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

CONC_MAX = max(1, int(os.environ.get("CONC_MAX", "6")))
PROGRESS_EDIT_INTERVAL = float(
    os.environ.get("PROGRESS_EDIT_INTERVAL", "2.0")
)
MAX_SIZE = 2 * 1024 * 1024 * 1024
STORAGE = Path("./files")
STORAGE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="[%(levelname)s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MikeyZipBot")

# User -> messages collected for the next ZIP.
tasks = {}
busy = set()


app = Client(
    "quick-zip-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


def clean(uid):
    path = STORAGE / str(uid)
    if path.exists():
        try:
            rmtree(path)
        except Exception:
            logger.exception("Cleanup failed for %s", uid)


def safe_name(name):
    name = os.path.basename(str(name or "file"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return (name or "file")[:240]


def size_of(message):
    obj = (
        message.document
        or message.video
        or message.audio
        or message.photo
        or message.animation
    )
    return int(getattr(obj, "file_size", 0) or 0)


def file_name_of(message):
    obj = (
        message.document
        or message.video
        or message.audio
        or message.photo
        or message.animation
    )
    return safe_name(getattr(obj, "file_name", None) or f"file_{message.id}")


def progress_bar(percent, length=12):
    percent = max(0, min(100, int(percent)))
    n = int(length * percent / 100)
    return "█" * n + "░" * (length - n)


async def edit_progress(message, text, last, force=False):
    now = time.monotonic()
    if not force and now - last[0] < PROGRESS_EDIT_INTERVAL:
        return last[0]
    try:
        await message.edit_text(text, parse_mode="html")
        last[0] = now
    except Exception:
        pass
    return last[0]


@app.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    await message.reply_text(
        "👋 <b>Welcome to Mikey ZIP Bot!</b>\n\n"
        "📦 Collect Telegram files and pack them into a ZIP.\n\n"
        "➕ <code>/add</code> — start collecting\n"
        "📦 <code>/zip filename</code> — create ZIP\n"
        "🗑 <code>/del__ID</code> — remove a file\n"
        "❌ <code>/cancel</code> — cancel",
        parse_mode="html",
    )


@app.on_message(filters.command("help") & filters.private)
async def help_handler(client, message):
    await start_handler(client, message)


@app.on_message(filters.command("add") & filters.private)
async def add_handler(client, message):
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
async def delete_handler(client, message):
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


@app.on_message(
    (filters.document
     | filters.video
     | filters.audio
     | filters.photo
     | filters.animation)
    & filters.private
)
async def file_handler(client, message):
    uid = message.from_user.id
    if uid not in tasks or uid in busy:
        return

    tasks[uid].append(message)
    name = file_name_of(message)
    size = size_of(message)

    await message.reply_text(
        f"✅ <b>added</b> {name}\n\n"
        f"📦 {size / (1024 * 1024):.2f} MB\n\n"
        f"delete using <code>/del__{message.id}</code>",
        parse_mode="html",
    )


async def download_one(message, path, status, progress_state, index, total_files, total_bytes):
    async def callback(current, total, *args):
        if not total:
            return
        # Hydrogram supplies current/total bytes to the callback.
        now = time.monotonic()
        if now - progress_state["time"] < PROGRESS_EDIT_INTERVAL and current != total:
            return
        progress_state["time"] = now

        done = progress_state["completed_bytes"] + current
        percent = int(done * 100 / total_bytes) if total_bytes else 0
        try:
            await status.edit_text(
                "📥 <b>Downloading files...</b>\n\n"
                f"📊 <b>{percent}%</b>\n{progress_bar(percent)}\n"
                f"💾 {done / (1024*1024):.2f} MB / "
                f"{total_bytes / (1024*1024):.2f} MB\n"
                f"📁 Files: {progress_state['completed_files']}/{total_files}",
                parse_mode="html",
            )
        except Exception:
            pass

    await message.download(
        file_name=str(path),
        progress=callback,
    )

    progress_state["completed_bytes"] += path.stat().st_size
    progress_state["completed_files"] += 1
    return path


@app.on_message(filters.regex(r"^/zip(?:@\w+)?\s+([\w.-]+)$") & filters.private)
async def zip_handler(client, message):
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

    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", message.matches[0].group(1))
    name = name or "archive"

    busy.add(uid)
    root = STORAGE / str(uid)
    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / f"{name}.zip"

    try:
        messages = list(tasks[uid])
        total_bytes = sum(size_of(m) for m in messages)

        if total_bytes > MAX_SIZE:
            await message.reply_text("❌ The total file size is larger than 2 GB.")
            return

        status = await message.reply_text(
            "📥 <b>Downloading files...</b>\n\n"
            f"📊 <b>0%</b>\n{progress_bar(0)}\n"
            f"💾 0.00 MB / {total_bytes/(1024*1024):.2f} MB\n"
            f"📁 Files: 0/{len(messages)}",
            parse_mode="html",
        )

        state = {
            "completed_bytes": 0,
            "completed_files": 0,
            "time": 0.0,
        }

        semaphore = asyncio.Semaphore(CONC_MAX)

        async def worker(i, msg):
            async with semaphore:
                filename = file_name_of(msg)
                path = root / filename
                if path.exists():
                    path = root / f"{path.stem}_{i}{path.suffix}"
                return await download_one(
                    msg, path, status, state, i, len(messages), total_bytes
                )

        files = await asyncio.gather(
            *(worker(i, msg) for i, msg in enumerate(messages, 1))
        )

        await status.edit_text(
            "📦 <b>Creating ZIP...</b>\n\n"
            f"📊 <b>0%</b>\n{progress_bar(0)}\n"
            f"📁 Files: 0/{len(files)}\n"
            "⚡ Zero compression",
            parse_mode="html",
        )

        # Do not recompress MKV/MP4/etc.; this saves CPU and makes packaging fast.
        with ZipFile(zip_path, "w", compression=ZIP_STORED) as z:
            for i, path in enumerate(files, 1):
                z.write(path, arcname=path.name)
                percent = int(i * 100 / len(files))
                try:
                    await status.edit_text(
                        "📦 <b>Creating ZIP...</b>\n\n"
                        f"📊 <b>{percent}%</b>\n{progress_bar(percent)}\n"
                        f"📁 Files: <b>{i}/{len(files)}</b>\n"
                        "⚡ Zero compression",
                        parse_mode="html",
                    )
                except Exception:
                    pass

        upload_size = zip_path.stat().st_size
        upload_state = {"time": 0.0}

        async def upload_callback(current, total, *args):
            if not total:
                return
            now = time.monotonic()
            percent = int(current * 100 / total)
            if percent != 100 and now - upload_state["time"] < PROGRESS_EDIT_INTERVAL:
                return
            upload_state["time"] = now
            try:
                await status.edit_text(
                    "📤 <b>Uploading ZIP...</b>\n\n"
                    f"📊 <b>{percent}%</b>\n{progress_bar(percent)}\n"
                    f"💾 {current/(1024*1024):.2f} MB / "
                    f"{total/(1024*1024):.2f} MB",
                    parse_mode="html",
                )
            except Exception:
                pass

        await client.send_document(
            uid,
            str(zip_path),
            caption=(
                "✅ <b>ZIP ready!</b>\n\n"
                f"📦 {zip_path.name}\n"
                f"💾 {upload_size/(1024*1024):.2f} MB"
            ),
            progress=upload_callback,
        )

        try:
            await status.edit_text(
                "✅ <b>Upload complete!</b>\n\n"
                f"📦 {zip_path.name}\n"
                f"💾 {upload_size/(1024*1024):.2f} MB",
                parse_mode="html",
            )
        except Exception:
            pass

    except Exception as exc:
        logger.exception("ZIP operation failed for %s", uid)
        try:
            await message.reply_text(
                f"❌ <b>ZIP failed.</b>\n\n"
                f"<code>{type(exc).__name__}: {exc}</code>",
                parse_mode="html",
            )
        except Exception:
            pass
    finally:
        busy.discard(uid)
        tasks.pop(uid, None)
        clean(uid)


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_handler(client, message):
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
