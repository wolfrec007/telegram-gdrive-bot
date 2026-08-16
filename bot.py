import logging
import time
import uuid
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from flask import Flask, request, jsonify
import threading

import config
import drive

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

user_state: dict[int, str] = {}
user_parent: dict[int, str] = {}
active_jobs: dict[str, dict] = {}  # job_key -> {user_id, file_name, stage, started, size}
pending_uploads: dict[str, dict] = {}  # short job_id -> conflict resolution state
pending_files: dict[str, dict] = {}  # short job_id -> file awaiting a destination-folder choice

flask_app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _jobs_text(user_id: int) -> str:
    jobs = [j for j in active_jobs.values() if j["user_id"] == user_id]
    if not jobs:
        return "No active jobs right now."

    now = time.time()
    lines = ["Active jobs:\n"]
    stage_labels = {
        "downloading": "⬇️ Downloading",
        "uploading": "⬆️ Uploading to Drive",
        "waiting_decision": "⚠️ Waiting for your decision",
    }
    for j in jobs:
        elapsed = int(now - j["started"])
        mins, secs = divmod(elapsed, 60)
        stage_label = stage_labels.get(j["stage"], j["stage"])
        size_info = f" ({j['size'] / 1024 / 1024:.1f} MB)" if j.get("size") else ""
        lines.append(f"{stage_label}: `{j['file_name']}`{size_info} — {mins}m{secs:02d}s")
    return "\n".join(lines)


def _next_available_name(file_name: str, folder_id: str) -> str:
    p = Path(file_name)
    stem, suffix = p.stem, p.suffix
    candidate = file_name
    n = 1
    while drive.find_file_by_name(candidate, folder_id) and n <= 50:
        candidate = f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _folder_name(folder_id: str) -> str:
    if folder_id == "root":
        return "My Drive"
    try:
        info = drive.get_folder_info(folder_id)
        return info["name"]
    except Exception:
        return folder_id


def _folder_keyboard(folders, parent_id):
    buttons = []
    for f in folders:
        buttons.append([InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"browse:{f['id']}")])
    buttons.append([InlineKeyboardButton("✅ Use this folder", callback_data=f"select:{parent_id}")])
    buttons.append([InlineKeyboardButton("➕ Create new folder here", callback_data=f"create:{parent_id}")])
    nav = []
    if parent_id != "root":
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"back:{parent_id}"))
    nav.append(InlineKeyboardButton("🏠 Root", callback_data="browse:root"))
    nav.append(InlineKeyboardButton("📋 Menu", callback_data="back_to_menu"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def _dest_keyboard(folders, folder_id, job_id):
    buttons = []
    for f in folders:
        buttons.append([InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"dest:browse:{f['id']}:{job_id}")])
    buttons.append([InlineKeyboardButton("✅ Save in this folder", callback_data=f"dest:save:{folder_id}:{job_id}")])
    nav = []
    if folder_id != "root":
        nav.append(InlineKeyboardButton("⬅️ Back", callback_data=f"dest:up:{folder_id}:{job_id}"))
    nav.append(InlineKeyboardButton("🏠 Root", callback_data=f"dest:browse:root:{job_id}"))
    nav.append(InlineKeyboardButton("❌ Cancel", callback_data=f"dest:cancel:{job_id}"))
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# ── Commands ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Pick folder", callback_data="pick_folder")],
        [InlineKeyboardButton("📊 Status", callback_data="show_status")],
        [InlineKeyboardButton("🟢 Active Jobs", callback_data="show_jobs")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")],
    ])
    await update.message.reply_text(
        "Welcome! Send me a video or document and I'll ask where to save it "
        "(your current default folder, or browse into any subfolder).\n\n"
        "Use the buttons below or these commands:\n"
        "/setfolder — browse and pick a Drive folder\n"
        "/status — show current config\n"
        "/jobs — show active downloads/uploads\n"
        "/cancel — cancel current action",
        reply_markup=kb,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use:\n\n"
        "1. Send a video or document to this bot\n"
        "2. It downloads and saves to your Google Drive folder\n"
        "3. You get a link back\n\n"
        "Commands:\n"
        "/setfolder — browse and pick a Drive folder\n"
        "/status — show current config\n"
        "/jobs — show active downloads/uploads\n"
        "/cancel — cancel folder creation"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    cleared = False

    if user_state.pop(user_id, None):
        user_parent.pop(user_id, None)
        cleared = True

    for job_id in [jid for jid, p in pending_files.items() if p["user_id"] == user_id]:
        pending_files.pop(job_id, None)
        cleared = True

    for job_id in [jid for jid, p in pending_uploads.items() if p["user_id"] == user_id]:
        pending = pending_uploads.pop(job_id)
        pending["temp_path"].unlink(missing_ok=True)
        active_jobs.pop(pending["job_key"], None)
        cleared = True

    if cleared:
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    name = _folder_name(config.GOOGLE_DRIVE_FOLDER_ID)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📁 Pick folder", callback_data="pick_folder")]])
    await update.message.reply_text(
        f"Auth mode: {config.AUTH_MODE}\n"
        f"Folder name: {name}\n"
        f"Folder ID: {config.GOOGLE_DRIVE_FOLDER_ID}",
        reply_markup=kb,
    )


async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="show_jobs")]])
    await update.message.reply_text(_jobs_text(user_id), reply_markup=kb)


async def cmd_setfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    parent_id = config.GOOGLE_DRIVE_FOLDER_ID or "root"
    try:
        folders = drive.list_folders(parent_id)
    except Exception as e:
        await update.message.reply_text(f"Failed to list folders: {e}")
        return

    name = _folder_name(parent_id)
    if parent_id != "root":
        text = f"**{name}** — choose a subfolder:"
    else:
        text = "My Drive — choose a folder:"

    await update.message.reply_text(text, reply_markup=_folder_keyboard(folders, parent_id))


# ── Callbacks (inline keyboard) ──────────────────────────────────────────────


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    # Menu-only callbacks (no folder_id)
    if data in ("pick_folder", "show_status", "show_help", "show_jobs", "back_to_menu"):
        await query.answer()
        if data == "pick_folder":
            parent_id = config.GOOGLE_DRIVE_FOLDER_ID or "root"
            try:
                folders = drive.list_folders(parent_id)
                name = _folder_name(parent_id)
                text = f"**{name}** — choose a subfolder:" if parent_id != "root" else "My Drive — choose a folder:"
                await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, parent_id))
            except Exception as e:
                await query.edit_message_text(f"Failed to list folders: {e}")
        elif data == "show_status":
            name = _folder_name(config.GOOGLE_DRIVE_FOLDER_ID)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]])
            await query.edit_message_text(
                f"Auth mode: {config.AUTH_MODE}\nFolder name: {name}\nFolder ID: `{config.GOOGLE_DRIVE_FOLDER_ID}`",
                reply_markup=kb,
            )
        elif data == "show_jobs":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="show_jobs")],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")],
            ])
            await query.edit_message_text(_jobs_text(user_id), reply_markup=kb)
        elif data == "show_help":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]])
            await query.edit_message_text(
                "How to use:\n\n"
                "1. Send a video or document\n"
                "2. It saves to your Google Drive folder\n"
                "3. You get a link back\n\n"
                "Use /setfolder to change the target folder.",
                reply_markup=kb,
            )
        elif data == "back_to_menu":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📁 Pick folder", callback_data="pick_folder")],
                [InlineKeyboardButton("📊 Status", callback_data="show_status")],
                [InlineKeyboardButton("🟢 Active Jobs", callback_data="show_jobs")],
                [InlineKeyboardButton("❓ Help", callback_data="show_help")],
            ])
            await query.edit_message_text("Main menu:", reply_markup=kb)
        return

    # Folder callbacks (have folder_id after colon)
    await query.answer()
    action, folder_id = data.split(":", 1)

    if action == "browse":
        try:
            folders = drive.list_folders(folder_id)
            name = _folder_name(folder_id)
            text = f"**{name}** — choose a subfolder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, folder_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "select":
        config.GOOGLE_DRIVE_FOLDER_ID = folder_id
        name = _folder_name(folder_id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_menu")]])
        await query.edit_message_text(f"✅ Target folder set to **{name}**\nID: `{folder_id}`", reply_markup=kb)

    elif action == "back":
        try:
            parent_id = drive.get_parent_id(folder_id) or "root"
            folders = drive.list_folders(parent_id)
            name = _folder_name(parent_id)
            if parent_id == "root":
                text = "My Drive — choose a folder:"
            else:
                text = f"**{name}** — choose a subfolder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, parent_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "create":
        user_state[user_id] = "awaiting_folder_name"
        user_parent[user_id] = folder_id
        parent_name = _folder_name(folder_id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancelcreate:{folder_id}")]])
        await query.edit_message_text(
            f"Send me the name for the new folder inside **{parent_name}**:", reply_markup=kb
        )

    elif action == "cancelcreate":
        user_state.pop(user_id, None)
        user_parent.pop(user_id, None)
        try:
            folders = drive.list_folders(folder_id)
            name = _folder_name(folder_id)
            text = f"**{name}** — choose a subfolder:" if folder_id != "root" else "My Drive — choose a folder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, folder_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")


# ── Text (folder creation flow) ──────────────────────────────────────────────


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_state.get(user_id) != "awaiting_folder_name":
        return

    folder_name = update.message.text.strip()
    parent_id = user_parent[user_id]
    del user_state[user_id]
    del user_parent[user_id]

    if not folder_name:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_menu")]])
        await update.message.reply_text("Cancelled.", reply_markup=kb)
        return

    try:
        folder = drive.create_folder(folder_name, parent_id)
        config.GOOGLE_DRIVE_FOLDER_ID = folder["id"]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Menu", callback_data="back_to_menu")]])
        await update.message.reply_text(
            f"✅ Created and selected folder **{folder['name']}**\nID: `{folder['id']}`", reply_markup=kb
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to create folder: {e}")


# ── File handling ─────────────────────────────────────────────────────────────


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, ctx, update.message.video)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, ctx, update.message.document)


MAX_FILE_SIZE_LOCAL_API = 2 * 1024 * 1024 * 1024  # 2GB with Local Bot API
MAX_FILE_SIZE_OFFICIAL_API = 20 * 1024 * 1024  # 20MB hard limit on the official Bot API

USING_LOCAL_API = False  # set at startup in main() based on whether the Local Bot API is reachable


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE, file_obj):
    file_id = file_obj.file_id
    file_name = getattr(file_obj, "file_name", None) or f"file_{file_id[:8]}"
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    file_size = getattr(file_obj, "file_size", None)

    max_size = MAX_FILE_SIZE_LOCAL_API if USING_LOCAL_API else MAX_FILE_SIZE_OFFICIAL_API

    if file_size and file_size > max_size:
        if USING_LOCAL_API:
            size_gb = file_size / 1024 / 1024 / 1024
            await update.message.reply_text(f"File too large ({size_gb:.1f} GB). Max is 2 GB.")
        else:
            size_mb = file_size / 1024 / 1024
            await update.message.reply_text(
                f"File too large ({size_mb:.1f} MB). The official Telegram Bot API caps "
                f"downloads at 20 MB — set up the Local Bot API server to raise this to 2 GB."
            )
        return

    user_id = update.message.from_user.id
    job_id = uuid.uuid4().hex[:10]
    pending_files[job_id] = {
        "user_id": user_id,
        "file_id": file_id,
        "file_name": file_name,
        "mime_type": mime_type,
        "file_size": file_size,
    }

    current_folder_id = config.GOOGLE_DRIVE_FOLDER_ID or "root"
    current_name = _folder_name(current_folder_id)
    size_info = f" ({file_size / 1024 / 1024:.1f} MB)" if file_size else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Save to {current_name}", callback_data=f"dest:save:{current_folder_id}:{job_id}")],
        [InlineKeyboardButton("📁 Choose a different folder", callback_data=f"dest:browse:{current_folder_id}:{job_id}")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"dest:cancel:{job_id}")],
    ])
    await update.message.reply_text(
        f"📥 Received `{file_name}`{size_info}\nWhere should I save it?", reply_markup=kb
    )


async def on_destination(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[1]
    user_id = query.from_user.id

    if action == "cancel":
        job_id = parts[2]
        pending_files.pop(job_id, None)
        await query.edit_message_text("Cancelled.")
        return

    folder_id, job_id = parts[2], parts[3]
    pending = pending_files.get(job_id)
    if not pending:
        await query.edit_message_text("This request has expired.")
        return
    if pending["user_id"] != user_id:
        await query.answer("Not your request.", show_alert=True)
        return

    if action == "browse":
        try:
            folders = drive.list_folders(folder_id)
            name = _folder_name(folder_id)
            text = f"**{name}** — choose a folder to save into:" if folder_id != "root" else "My Drive — choose a folder to save into:"
            await query.edit_message_text(text, reply_markup=_dest_keyboard(folders, folder_id, job_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "up":
        try:
            parent_id = drive.get_parent_id(folder_id) or "root"
            folders = drive.list_folders(parent_id)
            name = _folder_name(parent_id)
            text = "My Drive — choose a folder to save into:" if parent_id == "root" else f"**{name}** — choose a folder to save into:"
            await query.edit_message_text(text, reply_markup=_dest_keyboard(folders, parent_id, job_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "save":
        pending_files.pop(job_id, None)
        await _start_download(query.message, ctx, pending, folder_id)


MAX_FILE_SIZE_LOCAL_API = 2 * 1024 * 1024 * 1024  # 2GB with Local Bot API
MAX_FILE_SIZE_OFFICIAL_API = 20 * 1024 * 1024  # 20MB hard limit on the official Bot API

USING_LOCAL_API = False  # set at startup in main() based on whether the Local Bot API is reachable


async def _start_download(msg, ctx: ContextTypes.DEFAULT_TYPE, file_info: dict, target_folder_id: str):
    user_id = file_info["user_id"]
    file_id = file_info["file_id"]
    file_name = file_info["file_name"]
    mime_type = file_info["mime_type"]
    file_size = file_info["file_size"]

    job_key = f"{user_id}:{file_id}"
    active_jobs[job_key] = {
        "user_id": user_id,
        "file_name": file_name,
        "stage": "downloading",
        "started": time.time(),
        "size": file_size,
    }

    await msg.edit_text(f"Downloading `{file_name}`...")
    temp_path = DOWNLOADS_DIR / file_name

    try:
        tg_file = await ctx.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=temp_path.as_posix())
    except Exception as e:
        log.exception("Download failed")
        await msg.edit_text(f"Failed: {e}")
        temp_path.unlink(missing_ok=True)
        active_jobs.pop(job_key, None)
        return

    try:
        existing = drive.find_file_by_name(file_name, target_folder_id)
    except Exception as e:
        log.warning(f"Conflict check failed, proceeding without it: {e}")
        existing = None

    if existing:
        job_id = uuid.uuid4().hex[:10]
        pending_uploads[job_id] = {
            "user_id": user_id,
            "job_key": job_key,
            "temp_path": temp_path,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size": file_size,
            "existing_file_id": existing["id"],
            "folder_id": target_folder_id,
        }
        active_jobs[job_key]["stage"] = "waiting_decision"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("♻️ Overwrite", callback_data=f"conflict:overwrite:{job_id}")],
            [InlineKeyboardButton("📄 Keep Both", callback_data=f"conflict:keepboth:{job_id}")],
            [InlineKeyboardButton("⏭️ Skip", callback_data=f"conflict:skip:{job_id}")],
        ])
        await msg.edit_text(
            f"⚠️ A file named `{file_name}` already exists in this folder.\nWhat would you like to do?",
            reply_markup=kb,
        )
        return

    await _finish_upload(msg, job_key, temp_path, file_name, mime_type, file_size, folder_id=target_folder_id)


async def _finish_upload(msg, job_key, temp_path, file_name, mime_type, file_size, existing_file_id=None, folder_id=None):
    try:
        active_jobs[job_key]["stage"] = "uploading"
        await msg.edit_text("Uploading to Drive...")

        if existing_file_id:
            link = drive.overwrite_file(existing_file_id, temp_path.as_posix(), mime_type)
        else:
            link = drive.upload_file(temp_path.as_posix(), file_name, mime_type, folder_id=folder_id)

        size_info = f" ({file_size / 1024 / 1024:.1f} MB)" if file_size else ""
        await msg.edit_text(f"Done! Saved as `{file_name}`{size_info}\n[Open in Drive]({link})")

    except Exception as e:
        log.exception("Upload failed")
        await msg.edit_text(f"Failed: {e}")
    finally:
        temp_path.unlink(missing_ok=True)
        active_jobs.pop(job_key, None)


async def on_conflict_resolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, action, job_id = query.data.split(":", 2)

    pending = pending_uploads.pop(job_id, None)
    if not pending:
        await query.edit_message_text("This request has expired.")
        return
    if pending["user_id"] != user_id:
        await query.answer("Not your request.", show_alert=True)
        pending_uploads[job_id] = pending  # put it back, this wasn't for this user
        return

    temp_path = pending["temp_path"]
    file_name = pending["file_name"]
    mime_type = pending["mime_type"]
    file_size = pending["file_size"]
    job_key = pending["job_key"]
    existing_file_id = pending["existing_file_id"]
    folder_id = pending.get("folder_id") or config.GOOGLE_DRIVE_FOLDER_ID

    if action == "skip":
        temp_path.unlink(missing_ok=True)
        active_jobs.pop(job_key, None)
        await query.edit_message_text(f"⏭️ Skipped `{file_name}` — not uploaded.")
    elif action == "overwrite":
        await _finish_upload(query.message, job_key, temp_path, file_name, mime_type, file_size, existing_file_id, folder_id)
    elif action == "keepboth":
        new_name = _next_available_name(file_name, folder_id)
        await _finish_upload(query.message, job_key, temp_path, new_name, mime_type, file_size, folder_id=folder_id)


# ── Error handler ─────────────────────────────────────────────────────────────


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled exception: %s", ctx.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("Something went wrong. Please try again.")


# ── Flask (health + OAuth) ────────────────────────────────────────────────────


@flask_app.route("/health")
def health():
    return jsonify(status="ok", authMode=config.AUTH_MODE)


if config.AUTH_MODE == "oauth":

    @flask_app.route("/auth")
    def auth_start():
        flow = drive.get_oauth_flow()
        flow.redirect_uri = config.OAUTH_REDIRECT_URI
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        return (
            "<h3>Google Drive Auth</h3>"
            f'<p><a href="{url}">Click here to authorize</a></p>'
        )

    @flask_app.route("/auth/callback")
    def auth_callback():
        code = request.args.get("code")
        if not code:
            return "Missing code", 400
        flow = drive.get_oauth_flow()
        flow.redirect_uri = config.OAUTH_REDIRECT_URI
        flow.fetch_token(code=code)
        drive.save_oauth_tokens(flow.credentials)
        return "<h3>Authenticated!</h3><p>You can close this tab. Restart the bot to use Drive.</p>"


def run_flask():
    flask_app.run(host="0.0.0.0", port=config.PORT)


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    global USING_LOCAL_API

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"Health check: http://localhost:{config.PORT}/health")

    builder = Application.builder().token(config.BOT_TOKEN)

    if config.LOCAL_API_URL:
        try:
            import urllib.request
            urllib.request.urlopen(f"{config.LOCAL_API_URL}/bot{config.BOT_TOKEN}/getMe", timeout=5)
            builder = builder.base_url(f"{config.LOCAL_API_URL}/bot")
            builder = builder.base_file_url(f"{config.LOCAL_API_URL}/file/bot")
            USING_LOCAL_API = True
            log.info(f"Using Local Bot API: {config.LOCAL_API_URL}")
        except Exception as e:
            log.warning(f"Local Bot API at {config.LOCAL_API_URL} not ready ({e}), using official Telegram API (20MB file limit)")

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("setfolder", cmd_setfolder))
    app.add_handler(CallbackQueryHandler(on_conflict_resolve, pattern=r"^conflict:"))
    app.add_handler(CallbackQueryHandler(on_destination, pattern=r"^dest:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
