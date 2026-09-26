"""
bot.py
------
The Telethon *bot* client (BOT_TOKEN) — every admin command — PLUS the
application entrypoint that boots the database, both Telegram clients,
and runs until interrupted.

Run with:  python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("relaybot.bot")

from telethon import TelegramClient, events
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

import userbot
from config import settings
from database import db

bot = TelegramClient("bot_session", api_id=settings.api_id, api_hash=settings.api_hash)

_START_TIME = time.time()

COMMANDS = [
    ("start", "Show the welcome panel and quick help"),
    ("add", "Add a source: /add <chat_id>"),
    ("del", "Remove a source: /del <chat_id>"),
    ("addlink", "Blacklist a link: /addlink <url>"),
    ("dellink", "Un-blacklist a link: /dellink <url>"),
    ("stats", "List all monitored sources"),
    ("status", "Live diagnostic status check"),
]


def _admin_only(handler):
    async def wrapped(event: events.NewMessage.Event):
        sender_id = event.sender_id
        if sender_id is None or not settings.is_admin(sender_id):
            await event.reply("🚫 This bot is restricted to admins only.")
            return
        return await handler(event)
    return wrapped


async def register_bot_commands() -> None:
    await bot(
        SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="en",
            commands=[BotCommand(command=c, description=d) for c, d in COMMANDS],
        )
    )
    logger.info("Bot command menu registered (%d commands).", len(COMMANDS))


# ---------------------------------------------------------------------- #
# /start
# ---------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/start$"))
@_admin_only
async def cmd_start(event: events.NewMessage.Event) -> None:
    text = (
        "**🤖 Telegram Relay & Quiz Bot**\n\n"
        "I monitor source channels/groups, forward regular posts (with "
        "blacklisted links stripped) to the post channel, and shuffle "
        "quiz polls before posting them to the quiz channel.\n\n"
        "**Quick commands**\n"
        "• `/add <chat_id>` — start monitoring a chat\n"
        "• `/del <chat_id>` — stop monitoring a chat\n"
        "• `/addlink <url>` — blacklist a link/domain\n"
        "• `/dellink <url>` — un-blacklist a link/domain\n"
        "• `/stats` — list monitored sources\n"
        "• `/status` — live diagnostics\n\n"
        "You can also just send a bare numeric ID with no command to add it."
    )
    await event.reply(text, parse_mode="markdown")


# ---------------------------------------------------------------------- #
# /add and bare-ID shortcut
# ---------------------------------------------------------------------- #
async def _add_source(event: events.NewMessage.Event, raw_id: str) -> None:
    try:
        chat_id = int(raw_id.strip())
    except ValueError:
        await event.reply("⚠️ That doesn't look like a numeric chat ID.")
        return

    try:
        chat_type, title = await userbot.resolve_chat_type_and_title(chat_id)
    except Exception as exc:  # noqa: BLE001
        await event.reply(f"❌ Couldn't resolve that ID via the userbot: `{exc}`", parse_mode="markdown")
        return

    try:
        await db.add_source(chat_id, title, chat_type)
    except Exception:  # noqa: BLE001
        logger.exception("DB error while adding source %s", chat_id)
        await event.reply("⚠️ Saved to memory, but the database write failed — it may not survive a restart.")

    userbot.monitored_sources.add(chat_id)
    await event.reply(f"✅ Now monitoring **[{chat_type}] {title}** (`{chat_id}`).", parse_mode="markdown")


@bot.on(events.NewMessage(pattern=r"^/add\s+(-?\d+)$"))
@_admin_only
async def cmd_add(event: events.NewMessage.Event) -> None:
    await _add_source(event, event.pattern_match.group(1))


@bot.on(events.NewMessage(pattern=r"^(-?\d{6,})$"))
@_admin_only
async def cmd_add_bare_id(event: events.NewMessage.Event) -> None:
    await _add_source(event, event.pattern_match.group(1))


# ---------------------------------------------------------------------- #
# /del
# ---------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/del\s+(-?\d+)$"))
@_admin_only
async def cmd_del(event: events.NewMessage.Event) -> None:
    chat_id = int(event.pattern_match.group(1))
    userbot.monitored_sources.discard(chat_id)
    try:
        removed = await db.remove_source(chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("DB error while removing source %s", chat_id)
        await event.reply("⚠️ Removed from memory, but the database delete failed.")
        return

    if removed:
        await event.reply(f"🗑️ Stopped monitoring `{chat_id}`.", parse_mode="markdown")
    else:
        await event.reply(f"ℹ️ `{chat_id}` wasn't in the database (removed from live monitoring anyway).", parse_mode="markdown")


# ---------------------------------------------------------------------- #
# /addlink and /dellink
# ---------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/dellink\s+(\S+)$"))
@_admin_only
async def cmd_dellink(event: events.NewMessage.Event) -> None:
    """Per the spec: /dellink adds a URL to the blacklist (removes it from future posts)."""
    url = event.pattern_match.group(1)
    try:
        await db.add_blacklisted_link(url)
    except Exception:  # noqa: BLE001
        logger.exception("DB error while blacklisting %s", url)
        await event.reply("⚠️ Database write failed — blacklist not persisted.")
        return
    await userbot.refresh_blacklist_cache()
    await event.reply(f"🔗🚫 Blacklisted: `{url}` — it will be stripped from future posts.", parse_mode="markdown")


@bot.on(events.NewMessage(pattern=r"^/addlink\s+(\S+)$"))
@_admin_only
async def cmd_addlink(event: events.NewMessage.Event) -> None:
    """Per the spec: /addlink removes a URL from the blacklist (restores normal forwarding)."""
    url = event.pattern_match.group(1)
    try:
        removed = await db.remove_blacklisted_link(url)
    except Exception:  # noqa: BLE001
        logger.exception("DB error while un-blacklisting %s", url)
        await event.reply("⚠️ Database write failed.")
        return
    if removed:
        await userbot.refresh_blacklist_cache()
        await event.reply(f"🔗✅ Un-blacklisted: `{url}` — normal forwarding restored.", parse_mode="markdown")
    else:
        await event.reply(f"ℹ️ `{url}` wasn't on the blacklist.", parse_mode="markdown")


# ---------------------------------------------------------------------- #
# /stats
# ---------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/stats$"))
@_admin_only
async def cmd_stats(event: events.NewMessage.Event) -> None:
    try:
        sources = await db.list_sources()
    except Exception:  # noqa: BLE001
        logger.exception("DB error while listing sources")
        await event.reply("⚠️ Couldn't reach the database to list sources.")
        return

    if not sources:
        await event.reply("No sources are currently monitored.")
        return

    channels = [s for s in sources if s.chat_type == "Channel"]
    groups = [s for s in sources if s.chat_type == "Group"]

    lines = [f"[{s.chat_type}] {s.title} — ({s.chat_id})" for s in sources]
    header = f"**📊 Monitored Sources** ({len(channels)} channels, {len(groups)} groups)\n\n"
    body = "\n".join(lines)
    await event.reply(header + "```\n" + body + "\n```", parse_mode="markdown")


# ---------------------------------------------------------------------- #
# /status
# ---------------------------------------------------------------------- #
@bot.on(events.NewMessage(pattern=r"^/status$"))
@_admin_only
async def cmd_status(event: events.NewMessage.Event) -> None:
    userbot_ok = userbot.client.is_connected()
    try:
        userbot_ok = userbot_ok and await userbot.client.is_user_authorized()
    except Exception:  # noqa: BLE001
        userbot_ok = False

    t0 = time.monotonic()
    await bot.get_me()
    latency_ms = (time.monotonic() - t0) * 1000

    db_ok = db.connected
    uptime_s = int(time.time() - _START_TIME)

    text = (
        "**🩺 Status Report**\n\n"
        f"{'🟢' if userbot_ok else '🔴'} **Userbot:** {'Active & Connected' if userbot_ok else 'Disconnected'}\n"
        f"🤖 **Bot latency:** {latency_ms:.0f} ms\n"
        f"📡 **Active sources:** {len(userbot.monitored_sources)}\n"
        f"{'🟢' if db_ok else '🔴'} **Database:** {'Connected' if db_ok else 'Disconnected'}\n"
        f"⏱️ **Uptime:** {uptime_s // 3600}h {(uptime_s % 3600) // 60}m {uptime_s % 60}s"
    )
    await event.reply(text, parse_mode="markdown")


async def start_bot() -> None:
    await bot.start(bot_token=settings.bot_token)
    await register_bot_commands()
    me = await bot.get_me()
    logger.info("Bot connected as @%s", me.username)


# ---------------------------------------------------------------------- #
# Entrypoint
# ---------------------------------------------------------------------- #
async def _run() -> None:
    logger.info("Starting up...")
    await db.connect()

    await userbot.start_userbot()
    await start_bot()

    stop_event = asyncio.Event()

    def _handle_signal(sig_name: str) -> None:
        logger.info("Received %s, shutting down gracefully...", sig_name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except NotImplementedError:
            # add_signal_handler isn't available on some platforms (e.g. Windows).
            pass

    logger.info("Both clients are running. Waiting for messages / shutdown signal.")
    await stop_event.wait()

    await userbot.client.disconnect()
    await bot.disconnect()
    await db.close()
    logger.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Interrupted.")
    except Exception:  # noqa: BLE001
        logger.exception("Fatal error during startup/runtime.")
        sys.exit(1)


if __name__ == "__main__":
    main()
