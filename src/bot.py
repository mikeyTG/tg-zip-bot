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
)

logger = logging.getLogger("ZipBot")


# ============================================================
# TASK STORAGE
# ============================================================

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

@bot.on(
    NewMessage(
        pattern=r"^/(start|help)(?:@\w+)?$"
    )
)
async def start_handler(event):
    text = (
        "👋 <b>Welcome to ZipBot!</b>\n\n"
        "📦 I can collect your Telegram files and turn them into a ZIP.\n\n"
        "<b>Commands:</b>\n"
        "➕ /add — start collecting files\n"
        "📦 /zip filename — create the ZIP\n"
        "❌ /cancel — cancel the current task\n"
        "ℹ️ /help — show this help\n\n"
        "<b>Example:</b>\n"
        "1️⃣ Send <code>/add</code>\n"
        "2️⃣ Send your files\n"
        "3️⃣ Send <code>/zip myfiles</code>"
    )

    await event.respond(
        text,
        parse_mode="html"
    )

    raise StopPropagation


# ============================================================
# /ADD
# ============================================================

@bot.on(
    NewMessage(
        pattern=r"^/add(?:@\w+)?$"
    )
)
async def start_task_handler(event):
    user_id = event.sender_id

    tasks[user_id] = []

    user_root = STORAGE / str(user_id)

    try:
        if user_root.exists():
            rmtree(user_root)
    except Exception:
        logger.exception(
            "Failed to clean old files for user %s",
            user_id
        )

    await event.respond(
        "OK, send me some files.\n\n"
        "When you're finished, use:\n"
        "<code>/zip filename</code>",
        parse_mode="html"
    )

    logger.info(
        "User %s started a new ZIP task",
        user_id
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

    tasks[user_id].append(event.id)

    logger.info(
        "Added file message %s for user %s",
        event.id,
        user_id
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
        await event.respond(
            "❌ You must use /add first."
        )
        raise StopPropagation

    if not tasks[user_id]:
        await event.respond(
            "❌ You haven't sent me any files yet."
        )
        raise StopPropagation

    zip_name_input = event.pattern_match["name"]

    zip_name_input = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        zip_name_input
    )

    if not zip_name_input:
        zip_name_input = "archive"

    root = STORAGE / str(user_id)

    zip_name = root / f"{zip_name_input}.zip"

    root.mkdir(
        parents=True,
        exist_ok=True
    )

    status_message = None

    try:

        logger.info(
            "Getting %s files for user %s",
            len(tasks[user_id]),
            user_id
        )

        messages = await bot.get_messages(
            user_id,
            ids=tasks[user_id]
        )

        messages = [
            message
            for message in messages
            if message is not None
            and message.file is not None
        ]

        if not messages:
            await event.respond(
                "❌ I couldn't find the files anymore.\n"
                "Please use /add and send them again."
            )

            tasks.pop(user_id, None)

            raise StopPropagation

        total_size = sum(
            message.file.size or 0
            for message in messages
        )

        max_size = 2 * 1024 * 1024 * 1024

        if total_size > max_size:
            await event.respond(
                "❌ The total file size is larger than 2 GB.\n"
                "Please send fewer or smaller files."
            )

            tasks.pop(user_id, None)

            raise StopPropagation

        status_message = await event.respond(
            "⏳ Downloading files and creating your ZIP..."
        )

        downloaded_count = 0

        async for file_path in download_files(
            messages,
            CONC_MAX,
            root
        ):

            if file_path is None:
                continue

            downloaded_count += 1

            logger.info(
                "Downloaded %s/%s for user %s: %s",
                downloaded_count,
                len(messages),
                user_id,
                file_path.name
            )

            await get_running_loop().run_in_executor(
                None,
                partial(
                    add_to_zip,
                    zip_name,
                    file_path
                )
            )

        if not zip_name.exists():
            raise RuntimeError(
                "ZIP file was not created."
            )

        logger.info(
            "ZIP created for user %s: %s",
            user_id,
            zip_name
        )

        await status_message.edit(
            "📤 Uploading your ZIP..."
        )

        await event.respond(
            "✅ <b>Done!</b>",
            file=zip_name,
            parse_mode="html"
        )

        logger.info(
            "ZIP uploaded successfully for user %s",
            user_id
        )

    except Exception as exc:

        logger.exception(
            "ZIP operation failed for user %s",
            user_id
        )

        try:

            if status_message:
                await status_message.edit(
                    "❌ Something went wrong while creating the ZIP.\n\n"
                    f"<code>{type(exc).__name__}</code>",
                    parse_mode="html"
                )
            else:
                await event.respond(
                    "❌ Something went wrong while creating the ZIP.\n\n"
                    f"<code>{type(exc).__name__}</code>",
                    parse_mode="html"
                )

        except Exception:
            logger.exception(
                "Could not send error message to user %s",
                user_id
            )

    finally:

        try:
            if root.exists():
                rmtree(root)
        except Exception:
            logger.exception(
                "Failed to clean temporary files for user %s",
                user_id
            )

        tasks.pop(user_id, None)

    raise StopPropagation


# ============================================================
# /CANCEL
# ============================================================

@bot.on(
    NewMessage(
        pattern=r"^/cancel(?:@\w+)?$"
    )
)
async def cancel_handler(event):
    user_id = event.sender_id

    tasks.pop(
        user_id,
        None
    )

    user_root = STORAGE / str(user_id)

    try:
        if user_root.exists():
            rmtree(user_root)
    except Exception:
        logger.exception(
            "Failed to clean files while cancelling user %s",
            user_id
        )

    await event.respond(
        "❌ Canceled.\n\n"
        "For a new ZIP, use /add."
    )

    logger.info(
        "User %s cancelled their task",
        user_id
    )

    raise StopPropagation


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":

    logger.info("======================================")
    logger.info("Starting ZipBot...")
    logger.info("======================================")

    try:

        async def startup_log():
            try:
                account = await bot.get_me()

                logger.info(
                    "Logged in as @%s",
                    getattr(account, "username", None)
                )

                logger.info(
                    "ZipBot is ready and waiting for messages."
                )

            except Exception:
                logger.exception(
                    "Could not retrieve bot information."
                )

        bot.loop.run_until_complete(
            startup_log()
        )

        bot.run_until_disconnected()

    except KeyboardInterrupt:

        logger.info(
            "ZipBot stopped."
        )

    except Exception:

        logger.exception(
            "Fatal error while running ZipBot."
        )

        raise
