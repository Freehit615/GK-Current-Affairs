"""
bot.py
------
Telegram bot entrypoint. Handles:
  - admin-only access control (silently ignores everyone else)
  - plain-text commands (no inline keyboards / buttons anywhere)
  - automatic detection & registration of raw feed/API links pasted by an admin
  - /start, /status, /del, /repeat_on, /repeat_off, /test

Run with:  python bot.py
"""

import re
import asyncio
from functools import wraps

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import config
import database as db
import scheduler
from config import logger, ADMIN_IDS, BOT_TOKEN

URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Admin-only guard
# --------------------------------------------------------------------------
def admin_only(handler):
    """Decorator: silently drop any update not from a whitelisted admin ID."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if user is None or user.id not in ADMIN_IDS:
            return  # silent ignore, per spec
        return await handler(update, context, *a, **kw)
    return wrapper


async def reply(update: Update, text: str) -> None:
    """Send a short, crisp, professional plain-text reply (no markup/buttons)."""
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
@admin_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(
        update,
        "✅ <b>Authorized</b>\n"
        "Educational Content Bot is online and ready.\n"
        "Send a raw feed/API URL to register it, or use /status.",
    )


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    healthy = await db.db_health_check()
    stats = await db.get_stats()

    db_line = "🟢 Connected" if healthy else "🔴 Unreachable"
    repeat_line = (
        f"🟢 ON (every {stats['repeat_days']}d)"
        if stats["repeat_enabled"] else "🔴 OFF"
    )

    text = (
        "📊 <b>System Status</b>\n"
        f"Database: {db_line}\n"
        f"Active feeds: <b>{stats['active_feeds']}</b>\n"
        f"Hindi channels: <b>{len(config.HINDI_CHANNEL_IDS)}</b>\n"
        f"English channels: <b>{len(config.ENGLISH_CHANNEL_IDS)}</b>\n"
        f"Hindi quiz channels: <b>{len(config.HINDI_QUIZ_CHANNEL_IDS)}</b>\n"
        f"English quiz channels: <b>{len(config.ENGLISH_QUIZ_CHANNEL_IDS)}</b>\n"
        f"Total posts logged: <b>{stats['total_posts']}</b>\n"
        f"Pending revision reposts: <b>{stats['pending_revision']}</b>\n"
        f"Revision loop: {repeat_line}"
    )
    await reply(update, text)

    feeds = await db.get_active_feeds()
    if not feeds:
        return

    lines = ["🔗 <b>Registered Feeds/APIs</b>"]
    lines += [f"{i}. <code>{feed['url']}</code>" for i, feed in enumerate(feeds, start=1)]

    # Telegram caps messages at 4096 chars — chunk the list to stay safely under that.
    chunk, chunk_len = [], 0
    for line in lines:
        if chunk_len + len(line) + 1 > 3500:
            await reply(update, "\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        await reply(update, "\n".join(chunk))
    await reply(update, text)


@admin_only
async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "⚠️ Usage: <code>/del &lt;url&gt;</code>")
        return

    url = context.args[0].strip()
    removed = await db.delete_feed(url)
    if removed:
        await reply(update, f"🗑️ Feed removed:\n<code>{url}</code>")
    else:
        await reply(update, "⚠️ No matching feed found for that URL.")


@admin_only
async def cmd_repeat_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    days = config.DEFAULT_REPEAT_DAYS
    if context.args:
        try:
            days = int(context.args[0])
            if days <= 0:
                raise ValueError
        except ValueError:
            await reply(update, "⚠️ Provide a positive integer, e.g. <code>/repeat_on 15</code>")
            return

    await db.set_repeat_mode(enabled=True, days=days)
    await reply(update, f"✅ Revision loop enabled — repeating content every {days} days.")


@admin_only
async def cmd_repeat_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await db.set_repeat_mode(enabled=False)
    await reply(update, "🛑 Revision loop disabled.")


@admin_only
async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "🔄 Running end-to-end test: fetch → filter → translate → post → quiz…")
    try:
        status, detail = await scheduler.run_test_cycle(context.bot)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Test cycle failed")
        await reply(update, f"❌ Test failed: {exc}")
        return

    if status == "no_feeds":
        await reply(
            update,
            "⚠️ No feeds registered yet. Send a raw feed/API URL to this chat first, "
            "then run /test again.",
        )
    elif status == "fetch_failed":
        await reply(
            update,
            "⚠️ Could not pull any usable content from your feed:\n"
            f"<code>{detail}</code>\n"
            "Check the URL is reachable and returns RSS/Atom XML or JSON with a "
            "recognizable items/articles list.",
        )
    elif status == "filtered":
        await reply(
            update,
            "⚠️ Latest item was rejected by the safety filter, so nothing was posted:\n"
            f"<i>{detail}</i>",
        )
    else:  # "posted"
        await reply(
            update,
            "✅ Test post published to content channels:\n"
            f"<i>{detail}</i>\n"
            f"🧩 Quiz poll scheduled in {config.QUIZ_DELAY_SECONDS // 60} minute(s).",
        )


# --------------------------------------------------------------------------
# Auto link listener (non-command plain text from admins)
# --------------------------------------------------------------------------
@admin_only
async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()

    if not URL_PATTERN.match(text):
        return  # not a URL — ignore silently, per spec (clean text commands only otherwise)

    if await db.feed_exists(text):
        await reply(update, "⚠️ This link/API is already registered.")
        return

    inserted = await db.add_feed(text, added_by=update.effective_user.id)
    if inserted:
        await reply(update, f"✅ Feed registered:\n<code>{text}</code>")
    else:
        await reply(update, "⚠️ This link/API is already registered.")


# --------------------------------------------------------------------------
# Error handler
# --------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update: %s", context.error, exc_info=context.error)


# --------------------------------------------------------------------------
# App bootstrap
# --------------------------------------------------------------------------
BOT_COMMANDS = [
    BotCommand("start", "Authorization check & status message"),
    BotCommand("status", "DB health, feeds, channels, repeat status"),
    BotCommand("del", "Delete a feed/API link — /del <url>"),
    BotCommand("repeat_on", "Enable revision loop — /repeat_on [days]"),
    BotCommand("repeat_off", "Disable revision loop"),
    BotCommand("test", "Run full fetch → post → quiz flow now"),
]


async def post_init(application: Application) -> None:
    await db.init_db()
    scheduler.register_jobs(application)
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Bot initialized: schema ready, scheduler jobs registered, command menu set.")


async def post_shutdown(application: Application) -> None:
    await config.close_pool()


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("del", cmd_del))
    application.add_handler(CommandHandler("repeat_on", cmd_repeat_on))
    application.add_handler(CommandHandler("repeat_off", cmd_repeat_off))
    application.add_handler(CommandHandler("test", cmd_test))

    # Plain-text (non-command) messages from admins — used for link registration.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text)
    )

    application.add_error_handler(on_error)
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting bot polling…")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
