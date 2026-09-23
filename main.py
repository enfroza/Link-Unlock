"""
main.py — Link Unlock Bot & API
================================
A single-file, lightweight FastAPI + python-telegram-bot (v20+) application.

Runs a Telegram bot (button-driven link-creation + edit flow) and a REST API
(serves link configs to the frontend) concurrently in a single asyncio event loop.

Setup:
    pip install -r requirements.txt
    export BOT_TOKEN="123456789:ABC-DEF...your-bot-token..."
    python main.py

Env vars (all optional except BOT_TOKEN):
    BOT_TOKEN           Telegram bot token from @BotFather        (required)
    DB_PATH             SQLite file path                          (default: links.db)
    FRONTEND_BASE_URL   Base URL used to build generated links    (default: https://hornyunlock.netlify.app)
    API_HOST            Host for the REST API                     (default: 0.0.0.0)
    API_PORT            Port for the REST API                     (default: 8000)
"""

from __future__ import annotations

import asyncio
import logging
import os

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("link_unlock")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "links.db")
FRONTEND_BASE_URL = os.environ.get(
    "FRONTEND_BASE_URL", "https://hornyunlock.netlify.app"
).rstrip("/")
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8000"))

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is not set.\n"
        "Get a token from @BotFather on Telegram, then:\n"
        "  export BOT_TOKEN='123456789:your-token-here'"
    )

# Conversation states
ASK_OPEN_URL, ASK_UNLOCK_URL = range(2)
EDIT_CHOOSE_LINK, EDIT_CHOOSE_FIELD, EDIT_NEW_VALUE = range(2, 5)

# --------------------------------------------------------------------------
# Database layer (aiosqlite)
# --------------------------------------------------------------------------


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id  INTEGER NOT NULL,
                open_url     TEXT NOT NULL,
                unlock_url   TEXT NOT NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prefs (
                telegram_id       INTEGER PRIMARY KEY,
                saved_unlock_url  TEXT
            )
            """
        )
        await db.commit()
    logger.info("Database ready at %s", DB_PATH)


async def create_link(telegram_id: int, open_url: str, unlock_url: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO links (telegram_id, open_url, unlock_url) VALUES (?, ?, ?)",
            (telegram_id, open_url, unlock_url),
        )
        await db.commit()
        return cursor.lastrowid


async def get_link(link_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM links WHERE id = ?", (link_id,)) as cur:
            return await cur.fetchone()


async def get_links_for_user(telegram_id: int, limit: int = 10) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM links WHERE telegram_id = ? ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        ) as cur:
            return await cur.fetchall()


async def update_link(link_id: int, telegram_id: int, field: str, value: str) -> bool:
    """Update open_url or unlock_url. Returns True if a row was updated."""
    if field not in ("open_url", "unlock_url"):
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE links SET {field} = ? WHERE id = ? AND telegram_id = ?",
            (value, link_id, telegram_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_saved_unlock_url(telegram_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT saved_unlock_url FROM user_prefs WHERE telegram_id = ?",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return row[0]
        # Fallback: most recent unlock_url from this user's links
        async with db.execute(
            "SELECT unlock_url FROM links WHERE telegram_id = ? ORDER BY created_at DESC LIMIT 1",
            (telegram_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None


async def set_saved_unlock_url(telegram_id: int, unlock_url: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_prefs (telegram_id, saved_unlock_url) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET saved_unlock_url = excluded.saved_unlock_url
            """,
            (telegram_id, unlock_url),
        )
        await db.commit()


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

api = FastAPI(title="Link Unlock API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@api.get("/api/config/{config_id}")
async def get_config(config_id: int) -> dict:
    row = await get_link(config_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Link configuration not found")

    return {
        "openLinkUrl": row["open_url"],
        "unlockUrl": row["unlock_url"],
        "branding": {
            "pageTitle": "Link Unlock — Premium Content Gate",
            "cardTitle": "UNLOCK <span>LINK</span>",
            "cardSubtitle": "Complete the actions below to unlock the content",
        },
        "steps": {
            "step1Label": "Step 1 — Visit Link",
            "step2Label": "Step 2 — Unlock Content",
            "openButtonText": "OPEN LINK",
            "unlockButtonText": "Complete Steps to Unlock",
        },
        "timer": {"duration": 30},
        "messages": {
            "successTitle": "Link Unlocked!",
            "successSub": (
                "Your content has been unlocked successfully.<br/>"
                "The link has been opened in a new tab."
            ),
        },
    }


# --------------------------------------------------------------------------
# Telegram bot — keyboards & helpers
# --------------------------------------------------------------------------


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕 Create New Link", callback_data="create_new")],
            [InlineKeyboardButton("📋 My Links", callback_data="my_links")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
    )


def after_create_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Create Another", callback_data="create_new")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]
    )


def is_valid_url(text: str) -> bool:
    text = text.strip()
    return text.startswith("http://") or text.startswith("https://")


async def send_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the triggering callback-query message when possible, else send a new message."""
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            return
        except Exception:
            pass
    await update.effective_chat.send_message(
        text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------------------
# Telegram bot — Create Link handlers
# --------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_chat.send_message(
        "👋 <b>Welcome to Link Unlock</b>\n\nChoose an option below:",
        reply_markup=main_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_or_edit(
        update,
        "👋 <b>Welcome to Link Unlock</b>\n\nChoose an option below:",
        reply_markup=main_menu_keyboard(),
    )


async def start_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "🔗 <b>Step 1/2 — Open Link URL</b>\n\n"
        "Send me the URL you want as the <b>Open Link</b> (shown to visitors as Step 1).",
        reply_markup=cancel_keyboard(),
    )
    return ASK_OPEN_URL


async def receive_open_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_url(text):
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid URL — it must start with http:// or https://.\n"
            "Please send the Open Link URL again.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_OPEN_URL

    context.user_data["open_url"] = text
    telegram_id = update.effective_user.id
    saved = await get_saved_unlock_url(telegram_id)

    if saved:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Use saved unlock URL",
                        callback_data="use_saved_unlock",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]
        )
        await update.message.reply_text(
            "🔓 <b>Step 2/2 — Unlock Link URL</b>\n\n"
            f"You have a saved unlock URL:\n<code>{saved}</code>\n\n"
            "Tap the button to reuse it, or send a new URL.",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "🔓 <b>Step 2/2 — Unlock Link URL</b>\n\n"
            "Now send me the URL you want as the <b>Unlock Link</b> "
            "(revealed to visitors after they complete the steps).\n\n"
            "<i>It will be saved for reuse next time.</i>",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
    return ASK_UNLOCK_URL


async def _finish_create(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    unlock_url: str,
) -> int:
    """Shared logic: create link, save unlock URL as preference, reply success."""
    open_url = context.user_data.get("open_url", "")
    telegram_id = update.effective_user.id

    link_id = await create_link(telegram_id, open_url, unlock_url)
    await set_saved_unlock_url(telegram_id, unlock_url)
    generated_url = f"{FRONTEND_BASE_URL}/?id={link_id}"
    context.user_data.clear()

    text = (
        f"✅ <b>Link created!</b>\n\n"
        f"<b>ID:</b> {link_id}\n"
        f"<b>Open URL:</b> <code>{open_url}</code>\n"
        f"<b>Unlock URL:</b> <code>{unlock_url}</code>\n\n"
        f"<b>Your link:</b>\n{generated_url}\n\n"
        f"<i>Unlock URL saved for next time.</i>"
    )

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=after_create_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            await update.effective_chat.send_message(
                text,
                reply_markup=after_create_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    else:
        await update.message.reply_text(
            text,
            reply_markup=after_create_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    return ConversationHandler.END


async def receive_unlock_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_url(text):
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid URL — it must start with http:// or https://.\n"
            "Please send the Unlock Link URL again.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_UNLOCK_URL

    return await _finish_create(update, context, text)


async def use_saved_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_id = update.effective_user.id
    saved = await get_saved_unlock_url(telegram_id)
    if not saved:
        await update.callback_query.answer("No saved unlock URL found.", show_alert=True)
        await update.callback_query.edit_message_text(
            "🔓 <b>Step 2/2 — Unlock Link URL</b>\n\n"
            "No saved unlock URL. Please send one now.",
            reply_markup=cancel_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return ASK_UNLOCK_URL

    return await _finish_create(update, context, saved)


async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "❌ <b>Cancelled.</b>\n\nChoose an option below:",
        reply_markup=main_menu_keyboard(),
    )
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.", reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Telegram bot — My Links + Edit handlers
# --------------------------------------------------------------------------


async def show_my_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.effective_user.id
    rows = await get_links_for_user(telegram_id, limit=10)

    if not rows:
        text = "📋 <b>My Links</b>\n\nYou haven't created any links yet."
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
        )
    else:
        lines = ["📋 <b>Your Last 10 Links</b>\n"]
        for row in rows:
            final_url = f"{FRONTEND_BASE_URL}/?id={row['id']}"
            lines.append(
                f"<b>ID {row['id']}</b>\n"
                f"🔗 Open: <code>{row['open_url']}</code>\n"
                f"🔓 Unlock: <code>{row['unlock_url']}</code>\n"
                f"🌐 Page: {final_url}\n"
            )
        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Edit a Link", callback_data="edit_link")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]
        )

    await send_or_edit(update, text, reply_markup=keyboard)


async def start_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await send_or_edit(
        update,
        "✏️ <b>Edit Link</b>\n\nSend the <b>ID</b> of the link you want to edit:",
        reply_markup=cancel_keyboard(),
    )
    return EDIT_CHOOSE_LINK


async def edit_choose_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ Please send a valid numeric ID.",
            reply_markup=cancel_keyboard(),
        )
        return EDIT_CHOOSE_LINK

    link_id = int(text)
    row = await get_link(link_id)
    if row is None or row["telegram_id"] != update.effective_user.id:
        await update.message.reply_text(
            "⚠️ Link not found or you don't own it.",
            reply_markup=cancel_keyboard(),
        )
        return EDIT_CHOOSE_LINK

    context.user_data["edit_link_id"] = link_id

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Change Open URL", callback_data="edit_open")],
            [InlineKeyboardButton("🔓 Change Unlock URL", callback_data="edit_unlock")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
        ]
    )
    await update.message.reply_text(
        f"Editing <b>ID {link_id}</b>\n\n"
        f"Current Open: <code>{row['open_url']}</code>\n"
        f"Current Unlock: <code>{row['unlock_url']}</code>\n\n"
        "What do you want to change?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return EDIT_CHOOSE_FIELD


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "edit_open":
        context.user_data["edit_field"] = "open_url"
        field_name = "Open URL"
    else:
        context.user_data["edit_field"] = "unlock_url"
        field_name = "Unlock URL"

    await query.edit_message_text(
        f"Send the new <b>{field_name}</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_keyboard(),
    )
    return EDIT_NEW_VALUE


async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not is_valid_url(text):
        await update.message.reply_text(
            "⚠️ Invalid URL. It must start with http:// or https://",
            reply_markup=cancel_keyboard(),
        )
        return EDIT_NEW_VALUE

    link_id = context.user_data.get("edit_link_id")
    field = context.user_data.get("edit_field")
    telegram_id = update.effective_user.id

    success = await update_link(link_id, telegram_id, field, text)
    if success and field == "unlock_url":
        await set_saved_unlock_url(telegram_id, text)
    context.user_data.clear()

    if success:
        label = "Open URL" if field == "open_url" else "Unlock URL"
        extra = "\n\n<i>Unlock URL also saved for reuse.</i>" if field == "unlock_url" else ""
        await update.message.reply_text(
            f"✅ <b>Updated!</b>\n\n"
            f"Link ID <b>{link_id}</b>\n"
            f"{label}: <code>{text}</code>{extra}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚠️ Failed to update. Link not found.",
            reply_markup=main_menu_keyboard(),
        )
    return ConversationHandler.END


# --------------------------------------------------------------------------
# Telegram bot — application wiring
# --------------------------------------------------------------------------


def build_bot_application() -> Application:
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    create_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_create, pattern="^create_new$")],
        states={
            ASK_OPEN_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_open_url),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            ASK_UNLOCK_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_unlock_url),
                CallbackQueryHandler(use_saved_unlock, pattern="^use_saved_unlock$"),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
        ],
        name="create_link_conversation",
        allow_reentry=True,
        per_message=False,
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_edit, pattern="^edit_link$")],
        states={
            EDIT_CHOOSE_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_choose_link),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            EDIT_CHOOSE_FIELD: [
                CallbackQueryHandler(edit_choose_field, pattern="^edit_(open|unlock)$"),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
            EDIT_NEW_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_value),
                CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_creation, pattern="^cancel$"),
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
        ],
        name="edit_link_conversation",
        allow_reentry=True,
        per_message=False,
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(create_conv)
    application.add_handler(edit_conv)
    application.add_handler(CallbackQueryHandler(show_my_links, pattern="^my_links$"))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern="^main_menu$"))

    return application


# --------------------------------------------------------------------------
# Entrypoint — run FastAPI + Telegram bot concurrently
# --------------------------------------------------------------------------


async def run() -> None:
    await init_db()

    application = build_bot_application()
    uvicorn_config = uvicorn.Config(
        api, host=API_HOST, port=API_PORT, log_level="info"
    )
    server = uvicorn.Server(uvicorn_config)

    async with application:
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot polling started.")
        logger.info("Starting API server on http://%s:%s", API_HOST, API_PORT)
        try:
            await server.serve()
        finally:
            logger.info("Shutting down…")
            await application.updater.stop()
            await application.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted, exiting.")
