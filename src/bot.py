import asyncio
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.events import StopPropagation


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

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
# TELEGRAM CLIENT
# ============================================================

bot = TelegramClient(
    "quick-zip-bot",
    API_ID,
    API_HASH,
).start(
    bot_token=BOT_TOKEN
)


# ============================================================
# USER TASKS
# ============================================================

# user_id -> list of message IDs
tasks = {}

# users currently creating a ZIP
busy_users = set()


# ============================================================
# HELPERS
# ============================================================

def safe_filename(name):
    """
    Make a Telegram filename safe for the filesystem.
    """

    if not name:
        name = "file"

    name = os.path.basename(name)

    name = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        name,
    )

    return name[:240]


def format_mb(value):
    return f"{value / (1024 * 1024):.2f} MB"


def cleanup_user(user_id):

    folder = STORAGE / str(user_id)

    try:
        if folder.exists():
            shutil.rmtree(folder)

    except Exception:
        logger.exception(
            "Failed to clean user directory: %s",
            user_id,
        )


# ============================================================
# /START
# ============================================================

@bot.on(
    events.NewMessage(
        pattern=r"^/start(?:@\w+)?$"
    )
)
async def start_handler(event):

    user_id = event.sender_id

    if user_id in busy_users:

        await event.respond(
            "⏳ <b>I'm still processing your ZIP.</b>\n\n"
            "Please wait until it finishes before using /start.",
            parse_mode="html",
        )

        raise StopPropagation

    await event.respond(
        "👋 <b>Welcome to ZipBot!</b>\n\n"
        "📦 Send me Telegram files and I'll turn them "
        "into a ZIP.\n\n"

        "<b>Commands:</b>\n"
        "➕ /add — start collecting files\n"
        "📦 /zip filename — create ZIP\n"
        "🗑 /del__ID — remove a file\n"
        "❌ /cancel — cancel current task\n"
        "ℹ️ /help — show help\n\n"

        "<b>Example:</b>\n"
        "1. /add\n"
        "2. Send files\n"
        "3. Remove unwanted files with /del__ID\n"
        "4. /zip myfiles",
        parse_mode="html",
    )

    raise StopPropagation


# ============================================================
# /HELP
# ============================================================

@bot.on(
    events.NewMessage(
        pattern=r"^/help(?:@\w+)?$"
    )
)
async def help_handler(event):

    await event.respond(
        "📦 <b>ZipBot Help</b>\n\n"

        "➕ <code>/add</code>\n"
        "Start a new file collection.\n\n"

        "📦 <code>/zip filename</code>\n"
        "Download the selected files, create a ZIP, "
        "and upload it.\n\n"

        "🗑 <code>/del__MESSAGE_ID</code>\n"
        "Remove one file from the current ZIP list.\n\n"

        "❌ <code>/cancel</code>\n"
        "Cancel the current collection.\n\n"

        "📊 During ZIP creation you'll see:\n"
        "• Download percentage\n"
        "• MB downloaded\n"
        "• ZIP processing status\n"
        "• Upload percentage\n"
        "• MB uploaded",
        parse_mode="html",
    )

    raise StopPropagation


# ============================================================
# /ADD
# ============================================================

@bot.on(
    events.NewMessage(
        pattern=r"^/add(?:@\w+)?$"
    )
)
async def add_handler(event):

    user_id = event.sender_id

    if user_id in busy_users:

        await event.respond(
            "⏳ Your previous ZIP is still processing.\n"
            "Please wait for it to finish."
        )

        raise StopPropagation

    # Start fresh task.
    tasks[user_id] = []

    cleanup_user(user_id)

    await event.respond(
        "OK, send me some files.\n\n"
        "I'll give you a delete command for every file."
    )

    logger.info(
        "User %s started a new task",
        user_id,
    )

    raise StopPropagation


# ============================================================
# DELETE FILE
# ============================================================

@bot.on(
    events.NewMessage(
        pattern=r"^/del__(\d+)$"
    )
)
async def delete_handler(event):

    user_id = event.sender_id

    if user_id in busy_users:

        await event.respond(
            "⏳ The ZIP is already being processed.\n"
            "You can't remove files right now."
        )

        raise StopPropagation

    if user_id not in tasks:

        await event.respond(
            "❌ No active file list.\n"
            "Use /add first."
        )

        raise StopPropagation

    message_id = int(
        event.pattern_match.group(1)
    )

    if message_id not in tasks[user_id]:

        await event.respond(
            "❌ That file isn't in your current list."
        )

        raise StopPropagation

    tasks[user_id].remove(message_id)

    remaining = len(
        tasks[user_id]
    )

    await event.respond(
        "🗑 <b>File removed.</b>\n\n"
        f"📁 Remaining: <b>{remaining}</b>",
        parse_mode="html",
    )

    raise StopPropagation


# ============================================================
# FILE COLLECTOR
# ============================================================

@bot.on(
    events.NewMessage(
        func=lambda event: (
            event.sender_id in tasks
            and event.file is not None
        )
    )
)
async def file_handler(event):

    user_id = event.sender_id

    if user_id in busy_users:
        return

    if user_id not in tasks:
        return

    if event.id not in tasks[user_id]:

        tasks[user_id].append(
            event.id
        )

    filename = (
        getattr(
            event.file,
            "name",
            None,
        )
        or "unnamed file"
    )

    await event.respond(
        f"✅ <b>added</b> {filename}\n\n"
        f"delete using <code>/del__{event.id}</code>",
        parse_mode="html",
    )

    logger.info(
        "Added %s for user %s",
        filename,
        user_id,
    )

    raise StopPropagation


# ============================================================
# /ZIP
# ============================================================

@bot.on(
    events.NewMessage(
        pattern=r"^/zip(?:@\w+)?(?:\s+(.+))?$"
    )
)
async def zip_handler(event):

    user_id = event.sender_id

    # --------------------------------------------------------
    # Prevent duplicate ZIP jobs
    # --------------------------------------------------------

    if user_id in busy_users:

        await event.respond(
            "⏳ <b>A ZIP is already processing.</b>\n\n"
            "Please wait for it to finish.",
            parse_mode="html",
        )

        raise StopPropagation

    # --------------------------------------------------------
    # Check task
    # --------------------------------------------------------

    if user_id not in tasks:

        await event.respond(
            "❌ Use /add first."
        )

        raise StopPropagation

    if not tasks[user_id]:

        await event.respond(
            "❌ No files have been added yet."
        )

        raise StopPropagation

    # --------------------------------------------------------
    # ZIP NAME
    # --------------------------------------------------------

    requested_name = event.pattern_match.group(1)

    if requested_name:

        requested_name = requested_name.strip()

    else:

        requested_name = "archive"

    requested_name = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        requested_name,
    )

    if not requested_name:

        requested_name = "archive"

    # --------------------------------------------------------
    # Mark user busy
    # --------------------------------------------------------

    busy_users.add(user_id)

    root = STORAGE / str(user_id)

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    zip_path = (
        root
        / f"{requested_name}.zip"
    )

    status = None

    try:

        # ====================================================
        # GET TELEGRAM MESSAGES
        # ====================================================

        message_ids = list(
            tasks[user_id]
        )

        messages = await bot.get_messages(
            user_id,
            ids=message_ids,
        )

        messages = [
            message
            for message in messages
            if message is not None
            and message.file is not None
        ]

        if not messages:

            await event.respond(
                "❌ I couldn't find your files."
            )

            return

        # ====================================================
        # TOTAL SIZE
        # ====================================================

        total_bytes = sum(
            message.file.size or 0
            for message in messages
        )

        total_mb = (
            total_bytes
            / (1024 * 1024)
        )

        status = await event.respond(
            "📥 <b>Downloading files...</b>\n\n"
            f"📦 Total: <b>{total_mb:.2f} MB</b>\n"
            "📊 <b>0%</b>\n"
            f"📁 0/{len(messages)} files",
            parse_mode="html",
        )

        # ====================================================
        # DOWNLOAD FILES
        # ====================================================

        downloaded_bytes = 0

        # Used to avoid Telegram message edit spam.
        last_percent = -1

        for index, message in enumerate(
            messages,
            start=1,
        ):

            original_name = (
                getattr(
                    message.file,
                    "name",
                    None,
                )
                or f"file_{message.id}"
            )

            filename = safe_filename(
                original_name
            )

            output_path = (
                root / filename
            )

            # Avoid overwriting duplicate names.
            if output_path.exists():

                stem = output_path.stem
                suffix = output_path.suffix

                counter = 2

                while output_path.exists():

                    output_path = (
                        root
                        / f"{stem}_{counter}"
                        f"{suffix}"
                    )

                    counter += 1

            file_size = (
                message.file.size or 0
            )

            # -----------------------------------------------
            # Download progress callback
            # -----------------------------------------------

            async def download_progress(
                current,
                total,
                message_index=index,
                already_downloaded=downloaded_bytes,
            ):

                nonlocal last_percent

                actual_total = (
                    total_bytes
                    if total_bytes > 0
                    else total
                )

                current_total = (
                    already_downloaded
                    + current
                )

                if actual_total > 0:

                    percent = int(
                        current_total
                        * 100
                        / actual_total
                    )

                    percent = min(
                        percent,
                        100,
                    )

                else:

                    percent = 0

                # Update every 2%.
                if (
                    percent != last_percent
                    and (
                        percent % 2 == 0
                        or percent >= 100
                    )
                ):

                    last_percent = percent

                    current_mb = (
                        current_total
                        / (1024 * 1024)
                    )

                    try:

                        await status.edit(
                            "📥 <b>Downloading files...</b>\n\n"
                            f"📊 <b>{percent}%</b>\n"
                            f"💾 {current_mb:.2f} MB / "
                            f"{total_mb:.2f} MB\n"
                            f"📁 File {message_index}/"
                            f"{len(messages)}\n"
                            f"📄 {filename}",
                            parse_mode="html",
                        )

                    except Exception:
                        pass

            # -----------------------------------------------
            # Actual Telegram download
            # -----------------------------------------------

            logger.info(
                "Downloading %s/%s: %s",
                index,
                len(messages),
                filename,
            )

            await message.download_media(
                file=str(output_path),
                progress_callback=download_progress,
            )

            # -----------------------------------------------
            # Get actual downloaded size
            # -----------------------------------------------

            if output_path.exists():

                actual_size = (
                    output_path.stat().st_size
                )

            else:

                actual_size = file_size

            downloaded_bytes += actual_size

            logger.info(
                "Downloaded %s/%s: %s",
                index,
                len(messages),
                filename,
            )

        # ====================================================
        # ZIP CREATION
        # ====================================================

        await status.edit(
            "📦 <b>Creating ZIP...</b>\n\n"
            f"💾 {downloaded_bytes / (1024 * 1024):.2f} MB "
            f"processed\n"
            f"📁 {len(messages)}/{len(messages)} files",
            parse_mode="html",
        )

        # IMPORTANT:
        # MKV/MP4 files are already compressed.
        # ZIP_DEFLATED wastes CPU and time.
        # ZIP_STORED makes ZIP creation much faster.

        files_to_zip = [
            path
            for path in root.iterdir()
            if path.is_file()
            and path != zip_path
        ]

        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_STORED,
        ) as archive:

            for index, file_path in enumerate(
                files_to_zip,
                start=1,
            ):

                archive.write(
                    file_path,
                    arcname=file_path.name,
                )

                try:

                    await status.edit(
                        "📦 <b>Creating ZIP...</b>\n\n"
                        f"📊 <b>{int(index * 100 / len(files_to_zip))}%</b>\n"
                        f"📁 {index}/{len(files_to_zip)} files\n"
                        f"💾 {file_path.stat().st_size / (1024 * 1024):.2f} MB",
                        parse_mode="html",
                    )

                except Exception:
                    pass

        # ====================================================
        # ZIP SIZE
        # ====================================================

        zip_size = zip_path.stat().st_size

        zip_mb = (
            zip_size
            / (1024 * 1024)
        )

        logger.info(
            "ZIP created: %.2f MB",
            zip_mb,
        )

        # ====================================================
        # UPLOAD
        # ====================================================

        await status.edit(
            "📤 <b>Uploading ZIP...</b>\n\n"
            f"📦 Size: <b>{zip_mb:.2f} MB</b>\n"
            "📊 <b>0%</b>\n"
            "💾 0 MB uploaded",
            parse_mode="html",
        )

        last_upload_percent = -1

        async def upload_progress(
            current,
            total,
        ):

            nonlocal last_upload_percent

            if total <= 0:
                return

            percent = int(
                current
                * 100
                / total
            )

            percent = min(
                percent,
                100,
            )

            # Update every 2%.
            if (
                percent != last_upload_percent
                and (
                    percent % 2 == 0
                    or percent >= 100
                )
            ):

                last_upload_percent = percent

                current_mb = (
                    current
                    / (1024 * 1024)
                )

                total_upload_mb = (
                    total
                    / (1024 * 1024)
                )

                try:

                    await status.edit(
                        "📤 <b>Uploading ZIP...</b>\n\n"
                        f"📊 <b>{percent}%</b>\n"
                        f"💾 {current_mb:.2f} MB / "
                        f"{total_upload_mb:.2f} MB",
                        parse_mode="html",
                    )

                except Exception:
                    pass

        # ----------------------------------------------------
        # ACTUAL UPLOAD
        # ----------------------------------------------------

        await bot.send_file(
            user_id,
            str(zip_path),
            caption=(
                "✅ <b>ZIP ready!</b>\n\n"
                f"📦 Size: <b>{zip_mb:.2f} MB</b>"
            ),
            parse_mode="html",
            progress_callback=upload_progress,
        )

        # ====================================================
        # DONE
        # ====================================================

        try:

            await status.edit(
                "✅ <b>Upload complete!</b>\n\n"
                f"📦 ZIP size: <b>{zip_mb:.2f} MB</b>\n"
                "📊 <b>100%</b>",
                parse_mode="html",
            )

        except Exception:
            pass

        logger.info(
            "ZIP successfully uploaded for user %s",
            user_id,
        )

    except Exception as exc:

        logger.exception(
            "ZIP failed for user %s",
            user_id,
        )

        error_message = (
            "❌ <b>ZIP failed.</b>\n\n"
            f"Error: <code>{type(exc).__name__}</code>\n\n"
            "Check the Render logs for the full error."
        )

        try:

            if status:

                await status.edit(
                    error_message,
                    parse_mode="html",
                )

            else:

                await event.respond(
                    error_message,
                    parse_mode="html",
                )

        except Exception:
            pass

    finally:

        busy_users.discard(
            user_id
        )

        tasks.pop
