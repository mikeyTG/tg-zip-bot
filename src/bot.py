from functools import partial
from asyncio import get_running_loop
from shutil import rmtree
from pathlib import Path
import logging
import os
import re

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.events import NewMessage, StopPropagation
from telethon.tl.custom import Message

from utils import download_files, add_to_zip
from web import start


# ============================================================
# CONFIG
# ============================================================

load_dotenv()
start()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

CONC_MAX = int(os.environ.get("CONC_MAX", 3))
STORAGE = Path("./files")
STORAGE.mkdir(parents=True, exist_ok=True)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="[%(levelname)s/%(asctime)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)

logger = logging.getLogger("ZipBot")


# ============================================================
# TASK STORAGE
# ============================================================

# user_id -> list of Telegram message IDs waiting to be zipped
tasks: dict[int, list[int]] = {}


# ============================================================
# TELEGRAM CLIENT
# ============================================================

bot = TelegramClient(
    "quick-zip-bot",
    api_id=API_ID,
    api_hash=API_HASH,
).start(
    bot_token=BOT_TOKEN
)


# ============================================================
# /START + /HELP
# ============================================================

@bot.on(NewMessage(pattern=r"^/(start|help)(?:@\w+)?$"))
async def start_handler(event):
    await event.respond(
        "👋 <b>Welcome to ZipBot!</b>\n\n"
        "📦 I can collect your Telegram files and turn them into a ZIP.\n\n"
        "<b>Commands:</b>\n"
        "➕ /add — start collecting files\n"
        "📦 /zip filename — create the ZIP\n"
        "🗑 /del__ID — remove one file from the current list\n"
        "❌ /cancel — cancel the current task\n"
        "ℹ️ /help — show this help\n\n"
        "<b>Example:</b>\n"
        "1️⃣ Send <code>/add</code>\n"
        "2️⃣ Send your files\n"
        "3️⃣ If you want to remove a file, use its <code>/del__ID</code>\n"
        "4️⃣ Send <code>/zip myfiles</code>",
        parse_mode="html",
    )

    raise StopPropagation


# ============================================================
# /ADD
# ============================================================

@bot.on(NewMessage(pattern=r"^/add(?:@\w+)?$"))
async def start_task_handler(event):
    user_id = event.sender_id

    # Start a fresh collection.
    tasks[user_id] = []

    user_root = STORAGE / str(user_id)

    try:
        if user_root.exists():
            rmtree(user_root)
    except Exception:
        logger.exception(
            "Failed to clean old files for user %s",
            user_id,
        )

    await event.respond(
        "OK, send me some files.\n\n"
        "I'll tell you how to remove any file you don't want.",
    )

    logger.info("User %s started a new ZIP task", user_id)

    raise StopPropagation


# ============================================================
# DELETE ONE FILE FROM CURRENT TASK
# ============================================================

@bot.on(NewMessage(pattern=r"^/del__(?P<message_id>\d+)$"))
async def delete_file_handler(event):
    user_id = event.sender_id

    if user_id not in tasks:
        await event.respond(
            "❌ You don't have an active file list.\n"
            "Use /add first."
        )
        raise StopPropagation

    message_id = int(event.pattern_match["message_id"])

    if message_id not in tasks[user_id]:
        await event.respond(
            "❌ That file is not in your current ZIP list.\n"
            "It may already have been removed or the ID is invalid."
        )
        raise StopPropagation

    tasks[user_id].remove(message_id)

    remaining = len(tasks[user_id])

    await event.respond(
        f"🗑 <b>File removed.</b>\n\n"
        f"Files remaining: <b>{remaining}</b>",
        parse_mode="html",
    )

    logger.info(
        "User %s removed message %s from ZIP task",
        user_id,
        message_id,
    )

    raise StopPropagation


# ============================================================
# FILE HANDLER
# ============================================================

@bot.on(
    NewMessage(
        func=lambda e: (
            e.sender_id in tasks
            and e.file is not None
        )
    )
)
async def add_file_handler(event):
    user_id = event.sender_id

    if user_id not in tasks:
        return

    # Don't add the same Telegram message twice.
    if event.id not in tasks[user_id]:
        tasks[user_id].append(event.id)

    filename = (
        getattr(event.file, "name", None)
        or "unnamed file"
    )

    await event.respond(
        f"✅ <b>added</b> {filename}\n\n"
        f"delete using <code>/del__{event.id}</code>",
        parse_mode="html",
    )

    logger.info(
        "Added file '%s' (message %s) for user %s",
        filename,
        event.id,
        user_id,
    )

    raise StopPropagation


# ============================================================
# /ZIP
# ============================================================

@bot.on(
    NewMessage(
        pattern=r"^/zip(?:@\w+)?\s+(?P<name>[\w.-]+)$"
    )
)
async def zip_handler(event):
    user_id = event.sender_id

    if user_id not in tasks:
        await event.respond("❌ You must use /add first.")
        raise StopPropagation

    if not tasks[user_id]:
        await event.respond(
            "❌ You haven't added any files yet.\n"
            "Use /add and send some files."
        )
        raise StopPropagation

    zip_name_input = event.pattern_match["name"]
    zip_name_input = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        zip_name_input,
    )

    if not zip_name_input:
        zip_name_input = "archive"

    root = STORAGE / str(user_id)
    zip_name = root / f"{zip_name_input}.zip"
    root.mkdir(parents=True, exist_ok=True)

    status_message = None

    try:
        messages = await bot.get_messages(
            user_id,
            ids=tasks[user_id],
        )

        messages = [
            message
            for message in messages
            if message is not None
            and message.file is not None
        ]

        if not messages:
            await event.respond(
                "❌ I couldn't find the selected files anymore."
            )
            return

        total_size = sum(
            message.file.size or 0
            for message in messages
        )

        max_size = 2 * 1024 * 1024 * 1024

        if total_size > max_size:
            await event.respond(
                "❌ The total file size is larger than 2 GB."
            )
            return

        status_message = await event.respond(
            f"⏳ Downloading {len(messages)} file(s) and creating ZIP..."
        )

        downloaded = 0

        async for file_path in download_files(
            messages,
            CONC_MAX,
            root,
        ):
            if file_path is None:
                continue

            downloaded += 1

            logger.info(
                "Downloaded %s/%s for user %s: %s",
                downloaded,
                len(messages),
                user_id,
                file_path.name,
            )

            await get_running_loop().run_in_executor(
                None,
                partial(
                    add_to_zip,
                    zip_name,
                    file_path,
                ),
            )

        if not zip_name.exists():
            raise RuntimeError("ZIP file was not created.")

        await status_message.edit(
            "📤 Uploading your ZIP..."
        )

        await event.respond(
            "✅ <b>Done!</b>",
            file=zip_name,
            parse_mode="html",
        )

        logger.info(
            "ZIP uploaded successfully for user %s",
            user_id,
        )

    except Exception as exc:
        logger.exception(
            "ZIP operation failed for user %s",
            user_id,
        )

        error_text = (
            "❌ Something went wrong while creating the ZIP.\n\n"
            f"<code>{type(exc).__name__}</code>"
        )

        try:
            if status_message:
                await status_message.edit(
                    error_text,
                    parse_mode="html",
                )
            else:
                await event.respond(
                    error_text,
                    parse_mode="html",
                )
        except Exception:
            logger.exception(
                "Could not send error message to user %s",
                user_id,
            )

    finally:
        try:
            if root.exists():
                rmtree(root)
        except Exception:
            logger.exception(
                "Failed to clean temporary files for user %s",
                user_id,
            )

        tasks.pop(user_id, None)

    raise StopPropagation


# ============================================================
# /CANCEL
# ============================================================

@bot.on(NewMessage(pattern=r"^/cancel(?:@\w+)?$"))
async def cancel_handler(event):
    user_id = event.sender_id

    tasks.pop(user_id, None)

    user_root = STORAGE / str(user_id)

    try:
        if user_root.exists():
            rmtree(user_root)
    except Exception:
        logger.exception(
            "Failed to clean files while cancelling user %s",
            user_id,
        )

    await event.respond(
        "❌ Canceled.\n\n"
        "For a new ZIP, use /add."
    )

    logger.info("User %s cancelled their task", user_id)

    raise StopPropagation


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":
    logger.info("======================================")
    logger.info("Starting ZipBot...")
    logger.info("======================================")

    async def startup_log():
        try:
            account = await bot.get_me()

            logger.info(
                "Logged in as @%s",
                getattr(account, "username", None),
            )

            logger.info(
                "ZipBot is ready and waiting for messages."
            )

        except Exception:
            logger.exception(
                "Could not retrieve bot information."
            )

    try:
        bot.loop.run_until_complete(startup_log())
        bot.run_until_disconnected()

    except KeyboardInterrupt:
        logger.info("ZipBot stopped.")

    except Exception:
        logger.exception(
            "Fatal error while running ZipBot."
        )
        raise
