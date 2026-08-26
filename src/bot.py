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
    handlers=[
        logging.StreamHandler()
    ],
)

logger = logging.getLogger("ZipBot")


# ============================================================
# TASK STORAGE
# ============================================================

# user_id -> list of Telegram message IDs
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

    await event.respond(
        "👋 <b>Welcome to ZipBot!</b>\n\n"
        "📦 I can collect your Telegram files "
        "and turn them into a ZIP.\n\n"

        "<b>Commands:</b>\n"
        "➕ /add — start collecting files\n"
        "📦 /zip filename — create the ZIP\n"
        "🗑 /del__ID — remove a file\n"
        "❌ /cancel — cancel the current task\n"
        "ℹ️ /help — show this help\n\n"

        "<b>How to use:</b>\n"
        "1️⃣ Send <code>/add</code>\n"
        "2️⃣ Send your files\n"
        "3️⃣ Use the provided <code>/del__ID</code> "
        "to remove unwanted files\n"
        "4️⃣ Send <code>/zip myfiles</code>\n\n"

        "📊 I'll show ZIP processing and upload progress.",
        parse_mode="html",
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

    # Start a completely new task.
    tasks[user_id] = []

    # Clean any old temporary files.
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
        "I'll give you a delete command for each file."
    )

    logger.info(
        "User %s started a new ZIP task",
        user_id,
    )

    raise StopPropagation


# ============================================================
# DELETE ONE FILE
# ============================================================

@bot.on(
    NewMessage(
        pattern=r"^/del__(?P<message_id>\d+)$"
    )
)
async def delete_file_handler(event):

    user_id = event.sender_id

    # No active task.
    if user_id not in tasks:

        await event.respond(
            "❌ You don't have an active file list.\n"
            "Use /add first."
        )

        raise StopPropagation

    message_id = int(
        event.pattern_match["message_id"]
    )

    # Check whether the message is in the ZIP queue.
    if message_id not in tasks[user_id]:

        await event.respond(
            "❌ That file isn't in your current ZIP list.\n"
            "It may already have been removed."
        )

        raise StopPropagation

    # Remove the message ID from the queue.
    tasks[user_id].remove(message_id)

    remaining = len(tasks[user_id])

    await event.respond(
        "🗑 <b>File removed.</b>\n\n"
        f"📁 Files remaining: <b>{remaining}</b>",
        parse_mode="html",
    )

    logger.info(
        "User %s removed message %s",
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

    # Don't add the same message twice.
    if event.id not in tasks[user_id]:

        tasks[user_id].append(event.id)

    # Get filename.
    filename = (
        getattr(event.file, "name", None)
        or "unnamed file"
    )

    # Tell user how to delete this file.
    await event.respond(
        f"✅ <b>added</b> {filename}\n\n"
        f"delete using <code>/del__{event.id}</code>",
        parse_mode="html",
    )

    logger.info(
        "Added file '%s' "
        "(message %s) for user %s",
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

    # --------------------------------------------------------
    # Check active task
    # --------------------------------------------------------

    if user_id not in tasks:

        await event.respond(
            "❌ You must use /add first."
        )

        raise StopPropagation

    if not tasks[user_id]:

        await event.respond(
            "❌ You haven't added any files yet.\n"
            "Use /add and send some files."
        )

        raise StopPropagation

    # --------------------------------------------------------
    # Get ZIP name
    # --------------------------------------------------------

    zip_name_input = event.pattern_match["name"]

    # Keep the ZIP filename safe.
    zip_name_input = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        zip_name_input,
    )

    if not zip_name_input:
        zip_name_input = "archive"

    root = STORAGE / str(user_id)

    zip_name = root / f"{zip_name_input}.zip"

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    status_message = None

    try:

        # ====================================================
        # GET SELECTED TELEGRAM MESSAGES
        # ====================================================

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

        # ====================================================
        # CALCULATE TOTAL SIZE
        # ====================================================

        total_size = sum(
            message.file.size or 0
            for message in messages
        )

        max_size = 2 * 1024 * 1024 * 1024

        if total_size > max_size:

            total_gb = (
                total_size
                / (1024 * 1024 * 1024)
            )

            await event.respond(
                "❌ The selected files are too large.\n\n"
                f"📦 Total: <b>{total_gb:.2f} GB</b>\n"
                "📦 Maximum: <b>2 GB</b>\n\n"
                "Remove some files using /del__ID."
                ,
                parse_mode="html",
            )

            return

        total_mb = (
            total_size
            / (1024 * 1024)
        )

        # ====================================================
        # INITIAL STATUS
        # ====================================================

        status_message = await event.respond(
            "📥 <b>Preparing files...</b>\n\n"
            f"📦 Total: <b>{total_mb:.2f} MB</b>\n"
            "📊 Progress: <b>0%</b>\n"
            f"📁 Files: <b>0/{len(messages)}</b>",
            parse_mode="html",
        )

        # ====================================================
        # PROCESS FILES
        # ====================================================

        processed_bytes = 0
        processed_files = 0

        async for file_path in download_files(
            messages,
            CONC_MAX,
            root,
        ):

            if file_path is None:
                continue

            # -----------------------------------------------
            # Get actual downloaded file size
            # -----------------------------------------------

            try:

                file_size = file_path.stat().st_size

            except Exception:

                file_size = 0

            processed_bytes += file_size
            processed_files += 1

            processed_mb = (
                processed_bytes
                / (1024 * 1024)
            )

            # -----------------------------------------------
            # Calculate percentage
            # -----------------------------------------------

            if total_size > 0:

                percent = int(
                    processed_bytes
                    * 100
                    / total_size
                )

                percent = min(
                    percent,
                    100,
                )

            else:

                percent = 100

            # -----------------------------------------------
            # Log progress
            # -----------------------------------------------

            logger.info(
                "Processing user %s: "
                "%s/%s files, "
                "%.2f MB / %.2f MB (%s%%)",
                user_id,
                processed_files,
                len(messages),
                processed_mb,
                total_mb,
                percent,
            )

            # -----------------------------------------------
            # Add file to ZIP
            # -----------------------------------------------

            await get_running_loop().run_in_executor(
                None,
                partial(
                    add_to_zip,
                    zip_name,
                    file_path,
                ),
            )

            # -----------------------------------------------
            # Update Telegram status
            # -----------------------------------------------

            try:

                await status_message.edit(
                    "📦 <b>Processing ZIP...</b>\n\n"
                    f"📊 <b>{percent}%</b>\n"
                    f"💾 {processed_mb:.2f} MB / "
                    f"{total_mb:.2f} MB\n"
                    f"📁 {processed_files}/"
                    f"{len(messages)} files",
                    parse_mode="html",
                )

            except Exception:

                # Ignore Telegram edit errors.
                pass

        # ====================================================
        # VERIFY ZIP
        # ====================================================

        if not zip_name.exists():

            raise RuntimeError(
                "ZIP file was not created."
            )

        zip_size = zip_name.stat().st_size

        zip_mb = (
            zip_size
            / (1024 * 1024)
        )

        logger.info(
            "ZIP created for user %s: %.2f MB",
            user_id,
            zip_mb,
        )

        # ====================================================
        # UPLOAD PROGRESS
        # ====================================================

        await status_message.edit(
            "📤 <b>Uploading ZIP...</b>\n\n"
            f"💾 {zip_mb:.2f} MB\n"
            "📊 <b>0%</b>",
            parse_mode="html",
        )

        last_percent = -1

        async def upload_progress(
            current,
            total,
        ):

            nonlocal last_percent

            if total <= 0:
                return

            # Current uploaded MB.
            current_mb = (
                current
                / (1024 * 1024)
            )

            # Total upload MB.
            upload_total_mb = (
                total
                / (1024 * 1024)
            )

            # Real percentage.
            percent = int(
                current
                * 100
                / total
            )

            percent = min(
                percent,
                100,
            )

            # Only edit when percentage changes.
            # This prevents Telegram flood limits.
            if (
                percent != last_percent
                or percent == 100
            ):

                last_percent = percent

                try:

                    await status_message.edit(
                        "📤 <b>Uploading ZIP...</b>\n\n"
                        f"📊 <b>{percent}%</b>\n"
                        f"💾 {current_mb:.2f} MB / "
                        f"{upload_total_mb:.2f} MB",
                        parse_mode="html",
                    )

                except Exception:

                    pass

        # ====================================================
        # UPLOAD ZIP
        # ====================================================

        await bot.send_file(
            user_id,
            zip_name,
            caption=(
                "✅ <b>ZIP ready!</b>\n\n"
                f"📦 Size: <b>{zip_mb:.2f} MB</b>\n"
                "📊 Upload: <b>100%</b>"
            ),
            parse_mode="html",
            progress_callback=upload_progress,
        )

        # ====================================================
        # COMPLETE
        # ====================================================

        try:

            await status_message.edit(
                "✅ <b>Upload complete!</b>\n\n"
                f"📦 ZIP size: <b>{zip_mb:.2f} MB</b>\n"
                "📊 <b>100%</b>",
                parse_mode="html",
            )

        except Exception:

            pass

        logger.info(
            "ZIP uploaded successfully "
            "for user %s",
            user_id,
        )

    # ========================================================
    # ERROR HANDLING
    # ========================================================

    except Exception as exc:

        logger.exception(
            "ZIP operation failed "
            "for user %s",
            user_id,
        )

        error_text = (
            "❌ <b>Something went wrong.</b>\n\n"
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
                "Could not send error message "
                "to user %s",
                user_id,
            )

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        try:

            if root.exists():
                rmtree(root)

        except Exception:

            logger.exception(
                "Failed to clean temporary files "
                "for user %s",
                user_id,
            )

        # Clear selected files.
        tasks.pop(
            user_id,
            None,
        )

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

    # Clear task.
    tasks.pop(
        user_id,
        None,
    )

    # Remove temporary files.
    user_root = STORAGE / str(user_id)

    try:

        if user_root.exists():
            rmtree(user_root)

    except Exception:

        logger.exception(
            "Failed to clean files "
            "while cancelling user %s",
            user_id,
        )

    await event.respond(
        "❌ Canceled.\n\n"
        "For a new ZIP, use /add."
    )

    logger.info(
        "User %s cancelled their task",
        user_id,
    )

    raise StopPropagation


# ============================================================
# START BOT
# ============================================================

if __name__ == "__main__":

    logger.info(
        "======================================"
    )

    logger.info(
        "Starting ZipBot..."
    )

    logger.info(
        "======================================"
    )

    async def startup_log():

        try:

            account = await bot.get_me()

            logger.info(
                "Logged in as @%s",
                getattr(
                    account,
                    "username",
                    None,
                ),
            )

            logger.info(
                "ZipBot is ready and waiting "
                "for messages."
            )

        except Exception:

            logger.exception(
                "Could not retrieve bot information."
            )

    try:

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
