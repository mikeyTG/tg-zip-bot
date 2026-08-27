from asyncio import Lock, gather
from pathlib import Path
from shutil import rmtree
from zipfile import ZipFile, ZIP_STORED
import logging
import os
import re

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.events import NewMessage, StopPropagation
from web import start

load_dotenv()
start()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

CONC_MAX = int(os.environ.get("CONC_MAX", 3))
STORAGE = Path("./files")
STORAGE.mkdir(parents=True, exist_ok=True)
MAX_SIZE = 2 * 1024 * 1024 * 1024

logging.basicConfig(
    format="[%(levelname)s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("MikeyZipBot")

tasks: dict[int, list[int]] = {}
busy: set[int] = set()
status_locks: dict[int, Lock] = {}

bot = TelegramClient(
    "quick-zip-bot",
    api_id=API_ID,
    api_hash=API_HASH,
).start(bot_token=BOT_TOKEN)


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


@bot.on(NewMessage(pattern=r"^/(start|help)(?:@\w+)?$"))
async def start_handler(event):
    await event.respond(
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
    raise StopPropagation


@bot.on(NewMessage(pattern=r"^/add(?:@\w+)?$"))
async def add_handler(event):
    uid = event.sender_id
    if uid in busy:
        await event.respond("⏳ Your ZIP is processing. Please wait.")
        raise StopPropagation
    tasks[uid] = []
    clean(uid)
    await event.respond(
        "OK, send me your files. 📁\n\n"
        "Each file gets a delete command like <code>/del__123</code>.",
        parse_mode="html",
    )
    raise StopPropagation


@bot.on(NewMessage(pattern=r"^/del__(?P<message_id>\d+)$"))
async def delete_handler(event):
    uid = event.sender_id
    if uid not in tasks:
        await event.respond("❌ Use /add first.")
        raise StopPropagation
    mid = int(event.pattern_match["message_id"])
    if mid not in tasks[uid]:
        await event.respond("❌ That file is not in your current list.")
        raise StopPropagation
    tasks[uid].remove(mid)
    await event.respond(
        f"🗑 <b>File removed.</b> Remaining: <b>{len(tasks[uid])}</b>",
        parse_mode="html",
    )
    raise StopPropagation


@bot.on(NewMessage(func=lambda e: e.sender_id in tasks and e.file is not None))
async def file_handler(event):
    uid = event.sender_id
    if uid in busy or uid not in tasks:
        return
    if event.id not in tasks[uid]:
        tasks[uid].append(event.id)
    name = safe_name(getattr(event.file, "name", None) or f"file_{event.id}")
    size = getattr(event.file, "size", 0) or 0
    await event.respond(
        f"✅ <b>added</b> {name}\n\n"
        f"📦 {fmt_mb(size):.2f} MB\n\n"
        f"delete using <code>/del__{event.id}</code>",
        parse_mode="html",
    )
    raise StopPropagation


@bot.on(NewMessage(pattern=r"^/zip(?:@\w+)?\s+(?P<name>[\w.-]+)$"))
async def zip_handler(event):
    uid = event.sender_id
    if uid in busy:
        await event.respond("⏳ A ZIP is already processing. Please wait.")
        raise StopPropagation
    if uid not in tasks:
        await event.respond("❌ You must use /add first.")
        raise StopPropagation
    if not tasks[uid]:
        await event.respond("❌ You haven't added any files yet.")
        raise StopPropagation

    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", event.pattern_match["name"]) or "archive"
    root = STORAGE / str(uid)
    root.mkdir(parents=True, exist_ok=True)
    zip_path = root / f"{name}.zip"
    status = None
    busy.add(uid)

    try:
        messages = await bot.get_messages(uid, ids=list(tasks[uid]))
        if not isinstance(messages, list):
            messages = [messages]
        messages = [m for m in messages if m is not None and m.file is not None]
        if not messages:
            await event.respond("❌ No valid files found.")
            return

        total = sum(m.file.size or 0 for m in messages)
        if total > MAX_SIZE:
            await event.respond("❌ The total file size is larger than 2 GB.")
            return

        total_mb = fmt_mb(total)
        status = await event.respond(
            "📥 <b>Downloading files...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"💾 0.00 MB / {total_mb:.2f} MB\n"
            f"📁 Files: 0/{len(messages)}",
            parse_mode="html",
        )

        downloaded = 0
        completed = 0
        last_percent = -1
        lock = status_locks.setdefault(uid, Lock())

        async def update_download(current, file_total):
            nonlocal last_percent
            # Aggregate progress: current bytes of this file + sizes of completed files.
            done_bytes = downloaded + current
            percent = int(done_bytes * 100 / total) if total else 0
            percent = max(0, min(100, percent))
            if percent == last_percent and percent != 100:
                return
            if percent % 2 and percent != 100:
                return
            last_percent = percent
            async with lock:
                try:
                    await status.edit(
                        "📥 <b>Downloading files...</b>\n\n"
                        f"📊 <b>{percent}%</b>\n{bar(percent)}\n"
                        f"💾 <b>{fmt_mb(done_bytes):.2f} MB</b> / {total_mb:.2f} MB\n"
                        f"📁 Files completed: <b>{completed}/{len(messages)}</b>",
                        parse_mode="html",
                    )
                except Exception:
                    pass

        async def download_one(index, message):
            nonlocal downloaded, completed
            filename = safe_name(getattr(message.file, "name", None) or f"file_{message.id}")
            path = root / filename
            if path.exists():
                path = root / f"{path.stem}_{index}{path.suffix}"

            await message.download_media(
                file=str(path),
                progress_callback=update_download,
            )
            size = path.stat().st_size if path.exists() else (message.file.size or 0)
            downloaded += size
            completed += 1
            return path

        # Keep concurrent downloads for speed.
        semaphore = __import__("asyncio").Semaphore(max(1, CONC_MAX))

        async def limited(index, message):
            async with semaphore:
                return await download_one(index, message)

        files = await gather(*(limited(i, m) for i, m in enumerate(messages, 1)))
        files = [p for p in files if p and p.exists()]

        await status.edit(
            "📦 <b>Creating ZIP (0% compression)...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"📁 Files: 0/{len(files)}\n"
            "⚡ No compression — direct ZIP packing",
            parse_mode="html",
        )

        # ZERO COMPRESSION: direct ZIP container.
        with ZipFile(zip_path, "w", compression=ZIP_STORED) as z:
            for i, path in enumerate(files, 1):
                z.write(path, arcname=path.name)
                percent = int(i * 100 / len(files))
                try:
                    await status.edit(
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
        last_upload = -1

        await status.edit(
            "📤 <b>Uploading ZIP...</b>\n\n"
            f"📊 <b>0%</b>\n{bar(0)}\n"
            f"💾 0.00 MB / {upload_total_mb:.2f} MB",
            parse_mode="html",
        )

        async def upload_progress(current, total_upload):
            nonlocal last_upload
            if not total_upload:
                return
            percent = int(current * 100 / total_upload)
            if percent == last_upload and percent != 100:
                return
            if percent % 2 and percent != 100:
                return
            last_upload = percent
            try:
                await status.edit(
                    "📤 <b>Uploading ZIP...</b>\n\n"
                    f"📊 <b>{percent}%</b>\n{bar(percent)}\n"
                    f"💾 <b>{fmt_mb(current):.2f} MB</b> / {fmt_mb(total_upload):.2f} MB",
                    parse_mode="html",
                )
            except Exception:
                pass

        await bot.send_file(
            uid,
            str(zip_path),
            caption=(
                "✅ <b>ZIP ready!</b>\n\n"
                f"📦 {zip_path.name}\n"
                f"💾 {upload_total_mb:.2f} MB\n"
                "⚡ Zero compression"
            ),
            parse_mode="html",
            progress_callback=upload_progress,
        )

        await status.edit(
            "✅ <b>Upload complete!</b>\n\n"
            f"📦 {zip_path.name}\n"
            f"💾 {upload_total_mb:.2f} MB\n"
            "📊 <b>100%</b>\n"
            "████████████",
            parse_mode="html",
        )

    except Exception as exc:
        logger.exception("ZIP operation failed for user %s", uid)
        text = f"❌ <b>ZIP failed.</b>\n\n<code>{type(exc).__name__}</code>"
        try:
            if status:
                await status.edit(text, parse_mode="html")
            else:
                await event.respond(text, parse_mode="html")
        except Exception:
            logger.exception("Could not report error")
    finally:
        busy.discard(uid)
        tasks.pop(uid, None)
        status_locks.pop(uid, None)
        clean(uid)

    raise StopPropagation


@bot.on(NewMessage(pattern=r"^/cancel(?:@\w+)?$"))
async def cancel_handler(event):
    uid = event.sender_id
    if uid in busy:
        await event.respond("⏳ ZIP processing is already running.")
        raise StopPropagation
    tasks.pop(uid, None)
    clean(uid)
    await event.respond("❌ Canceled. Use /add to start again.")
    raise StopPropagation


if __name__ == "__main__":
    logger.info("Starting Mikey ZIP Bot...")
    bot.run_until_disconnected()
