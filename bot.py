import os
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

# per-user state for folder creation flow
user_state: dict[int, str] = {}
user_parent: dict[int, str] = {}

flask_app = Flask(__name__)


# ── Telegram Bot ──────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a video or document and I'll save it to your Google Drive folder.\n\n"
        "Commands:\n"
        "/setfolder — browse and pick a Drive folder\n"
        "/status — show current config"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Auth mode: {config.AUTH_MODE}\n"
        f"Drive folder ID: {config.GOOGLE_DRIVE_FOLDER_ID}"
    )


def _folder_keyboard(folders, parent_id):
    buttons = []
    for f in folders:
        buttons.append([InlineKeyboardButton(f"📁 {f['name']}", callback_data=f"browse:{f['id']}")])
    buttons.append([InlineKeyboardButton("✅ Use this folder", callback_data=f"select:{parent_id}")])
    buttons.append([InlineKeyboardButton("➕ Create new folder here", callback_data=f"create:{parent_id}")])
    if parent_id != "root":
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"back:{parent_id}")])
    return InlineKeyboardMarkup(buttons)


async def cmd_setfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parent_id = config.GOOGLE_DRIVE_FOLDER_ID or "root"
    try:
        folders = drive.list_folders(parent_id)
    except Exception as e:
        await update.message.reply_text(f"Failed to list folders: {e}")
        return

    if parent_id != "root":
        try:
            info = drive.get_folder_info(parent_id)
            text = f"**{info['name']}** — choose a subfolder:"
        except Exception:
            text = "Choose a folder:"
    else:
        text = "My Drive — choose a folder:"

    await update.message.reply_text(text, reply_markup=_folder_keyboard(folders, parent_id))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    action, folder_id = data.split(":", 1)
    user_id = query.from_user.id

    if action == "browse":
        try:
            folders = drive.list_folders(folder_id)
            info = drive.get_folder_info(folder_id)
            text = f"**{info['name']}** — choose a subfolder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, folder_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "select":
        config.GOOGLE_DRIVE_FOLDER_ID = folder_id
        try:
            info = drive.get_folder_info(folder_id)
            name = info["name"]
        except Exception:
            name = folder_id
        await query.edit_message_text(f"✅ Target folder set to **{name}**")

    elif action == "back":
        try:
            parent_id = drive.get_parent_id(folder_id) or "root"
            folders = drive.list_folders(parent_id)
            if parent_id == "root":
                text = "My Drive — choose a folder:"
            else:
                info = drive.get_folder_info(parent_id)
                text = f"**{info['name']}** — choose a subfolder:"
            await query.edit_message_text(text, reply_markup=_folder_keyboard(folders, parent_id))
        except Exception as e:
            await query.edit_message_text(f"Failed: {e}")

    elif action == "create":
        user_state[user_id] = "awaiting_folder_name"
        user_parent[user_id] = folder_id
        if folder_id == "root":
            parent_name = "My Drive"
        else:
            try:
                info = drive.get_folder_info(folder_id)
                parent_name = info["name"]
            except Exception:
                parent_name = folder_id
        await query.edit_message_text(f"Send me the name for the new folder inside **{parent_name}**:")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_state.get(user_id) == "awaiting_folder_name":
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
            await update.message.reply_text(f"✅ Created and selected folder **{folder['name']}**")
        except Exception as e:
            await update.message.reply_text(f"Failed to create folder: {e}")


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, update.message.video)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await handle_file(update, update.message.document)


async def handle_file(update: Update, file_obj):
    msg = await update.message.reply_text("Downloading...")

    file_id = file_obj.file_id
    file_name = getattr(file_obj, "file_name", None) or f"file_{file_id[:8]}"
    mime_type = getattr(file_obj, "mime_type", "application/octet-stream")
    file_size = getattr(file_obj, "file_size", None)

    temp_path = DOWNLOADS_DIR / file_name

    try:
        tg_file = await update.message.bot.get_file(file_id)
        await tg_file.download_to_drive(temp_path.as_posix())

        size_info = f" ({file_size / 1024 / 1024:.1f} MB)" if file_size else ""
        await msg.edit_text("Uploading to Drive...")

        link = drive.upload_file(temp_path.as_posix(), file_name, mime_type)
        await msg.edit_text(f"Done! Saved as `{file_name}`{size_info}\n[Open in Drive]({link})")

    except Exception as e:
        await msg.edit_text(f"Failed: {e}")
    finally:
        temp_path.unlink(missing_ok=True)


# ── Flask (health + OAuth callback) ───────────────────────────────────────────


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
        return "<h3>✅ Authenticated!</h3><p>You can close this tab. Restart the bot to use Drive.</p>"


def run_flask():
    flask_app.run(host="0.0.0.0", port=config.PORT)


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"Health check: http://localhost:{config.PORT}/health")

    app = Application.builder().token(config.BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("setfolder", cmd_setfolder))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.VIDEO, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
