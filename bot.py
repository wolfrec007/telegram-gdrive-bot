import logging
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

flask_app = Flask(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


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
    buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


# ── Commands ──────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Pick folder", callback_data="pick_folder")],
        [InlineKeyboardButton("📊 Status", callback_data="show_status")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")],
    ])
    await update.message.reply_text(
        "Welcome! Send me a video or document and I'll save it to your Google Drive folder.\n\n"
        "Use the buttons below or these commands:\n"
        "/setfolder — browse and pick a Drive folder\n"
        "/status — show current config\n"
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
        "/cancel — cancel folder creation"
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_state.pop(user_id, None):
        user_parent.pop(user_id, None)
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    name = _folder_name(config.GOOGLE_DRIVE_FOLDER_ID)
    await update.message.reply_text(
        f"Auth mode: {config.AUTH_MODE}\n"
        f"Folder name: {name}\n"
        f"Folder ID: {config.GOOGLE_DRIVE_FOLDER_ID}"
    )


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
    await query.answer()

    data = query.data
    action, folder_id = data.split(":", 1)
    user_id = query.from_user.id

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
        await query.edit_message_text(f"✅ Target folder set to **{name}**\nID: `{folder_id}`")

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
        await query.edit_message_text(f"Send me the name for the new folder inside **{parent_name}**:")

    elif action == "pick_folder":
        await query.answer()
        parent_id = config.GOOGLE_DRIVE_FOLDER_ID or "root"
        try:
            folders = drive.list_folders(parent_id)
            name = _folder_name(parent_id)
            text = f"**{name}** — choose a subfolder:" if parent_id != "root" else "My Drive — choose a folder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, parent_id))
        except Exception as e:
            await query.edit_message_text(f"Failed to list folders: {e}")

    elif action == "show_status":
        await query.answer()
        name = _folder_name(config.GOOGLE_DRIVE_FOLDER_ID)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]])
        await query.edit_message_text(
            f"Auth mode: {config.AUTH_MODE}\n"
            f"Folder name: {name}\n"
            f"Folder ID: {config.GOOGLE_DRIVE_FOLDER_ID}",
            reply_markup=kb,
        )

    elif action == "show_help":
        await query.answer()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back_to_menu")]])
        await query.edit_message_text(
            "How to use:\n\n"
            "1. Send a video or document\n"
            "2. It saves to your Google Drive folder\n"
            "3. You get a link back\n\n"
            "Use /setfolder to change the target folder.",
            reply_markup=kb,
        )

    elif action == "back_to_menu":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📁 Pick folder", callback_data="pick_folder")],
            [InlineKeyboardButton("📊 Status", callback_data="show_status")],
            [InlineKeyboardButton("❓ Help", callback_data="show_help")],
        ])
        await query.edit_message_text("Main menu:", reply_markup=kb)


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
        await update.message.reply_text("Cancelled.")
        return

    try:
        folder = drive.create_folder(folder_name, parent_id)
        config.GOOGLE_DRIVE_FOLDER_ID = folder["id"]
        await update.message.reply_text(
            f"✅ Created and selected folder **{folder['name']}**\nID: `{folder['id']}`"
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to create folder: {e}")


# ── File handling ─────────────────────────────────────────────────────────────


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, ctx, update.message.video)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, ctx, update.message.document)


MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB with Local Bot API


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE, file_obj):
    file_id = file_obj.file_id
    file_name = getattr(file_obj, "file_name", None) or f"file_{file_id[:8]}"
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    file_size = getattr(file_obj, "file_size", None)

    if file_size and file_size > MAX_FILE_SIZE:
        size_gb = file_size / 1024 / 1024 / 1024
        await update.message.reply_text(f"File too large ({size_gb:.1f} GB). Max is 2 GB.")
        return

    msg = await update.message.reply_text("Downloading...")
    temp_path = DOWNLOADS_DIR / file_name

    try:
        tg_file = await ctx.bot.get_file(file_id)
        await tg_file.download(custom_path=temp_path.as_posix())

        size_info = f" ({file_size / 1024 / 1024:.1f} MB)" if file_size else ""
        await msg.edit_text("Uploading to Drive...")

        link = drive.upload_file(temp_path.as_posix(), file_name, mime_type)
        await msg.edit_text(f"Done! Saved as `{file_name}`{size_info}\n[Open in Drive]({link})")

    except Exception as e:
        log.exception("File handling failed")
        await msg.edit_text(f"Failed: {e}")
    finally:
        temp_path.unlink(missing_ok=True)


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
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"Health check: http://localhost:{config.PORT}/health")

    builder = Application.builder().token(config.BOT_TOKEN)

    if config.LOCAL_API_URL:
        builder = builder.base_url(f"{config.LOCAL_API_URL}/bot")
        builder = builder.base_file_url(f"{config.LOCAL_API_URL}/file/bot")
        log.info(f"Using Local Bot API: {config.LOCAL_API_URL}")

    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setfolder", cmd_setfolder))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
