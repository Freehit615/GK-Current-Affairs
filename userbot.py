"""
userbot.py
----------
The Telethon *user* client (logged in via SESSION_STRING). It:

  1. Loads the monitored source list (DEFAULT_SOURCE_ID + DB rows) at startup.
  2. Listens for new messages across all of them via a single NewMessage
     handler that filters by an in-memory set (kept in sync by bot commands
     instead of re-registering event handlers on every /add or /del).
  3. Routes: polls -> shuffle -> QUIZ_CHANNEL_ID, everything else ->
     link-filter -> POST_CHANNEL_ID.
  4. Retries automatically on FloodWaitError.

`monitored_sources` (the in-memory set) is intentionally exposed at module
level so bot.py's admin commands can mutate it directly after touching the
database, keeping both clients in sync without extra IPC.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

from config import settings
from helpers import NotAQuizPoll, build_input_media_poll, extract_and_shuffle, filter_message
from database import db

logger = logging.getLogger("relaybot.userbot")

client = TelegramClient(
    session=StringSession(settings.session_string),
    api_id=settings.api_id,
    api_hash=settings.api_hash,
)

# In-memory mirror of monitored_sources, kept authoritative alongside the DB.
monitored_sources: set[int] = set()
_blacklist_cache: list[str] = []
_blacklist_lock = asyncio.Lock()


async def refresh_blacklist_cache() -> None:
    async with _blacklist_lock:
        try:
            _blacklist_cache[:] = await db.list_blacklisted_links()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to refresh link blacklist cache; keeping previous values.")


async def load_initial_sources() -> None:
    """Populate `monitored_sources` from DEFAULT_SOURCE_ID + the database."""
    monitored_sources.clear()
    if settings.default_source_id is not None:
        monitored_sources.add(settings.default_source_id)

    try:
        rows = await db.list_sources()
        for row in rows:
            monitored_sources.add(row.chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not load sources from database at startup; "
                          "continuing with DEFAULT_SOURCE_ID only if set.")

    await refresh_blacklist_cache()
    logger.info("Monitoring %d source(s): %s", len(monitored_sources), sorted(monitored_sources))


async def resolve_chat_type_and_title(chat_id: int) -> tuple[str, str]:
    """Fetch an entity via the userbot and classify it as Channel or Group."""
    entity = await client.get_entity(chat_id)
    if isinstance(entity, Channel):
        chat_type = "Channel" if not entity.megagroup else "Group"
        title = entity.title
    elif isinstance(entity, Chat):
        chat_type = "Group"
        title = entity.title
    elif isinstance(entity, User):
        raise ValueError("That ID belongs to a user, not a channel or group.")
    else:
        raise ValueError(f"Unsupported entity type: {type(entity).__name__}")
    return chat_type, title


async def _send_with_flood_retry(coro_factory, *, max_retries: int | None = None):
    max_retries = max_retries if max_retries is not None else settings.flood_wait_max_retries
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except FloodWaitError as exc:
            attempt += 1
            if attempt > max_retries:
                logger.error("FloodWait exceeded max retries (%ds wait). Giving up on this send.", exc.seconds)
                raise
            logger.warning("FloodWait: sleeping %ds (attempt %d/%d)", exc.seconds, attempt, max_retries)
            await asyncio.sleep(exc.seconds + 1)


async def _handle_quiz(event: events.NewMessage.Event) -> None:
    try:
        shuffled = extract_and_shuffle(event.message)
    except NotAQuizPoll:
        return

    media = build_input_media_poll(shuffled)

    async def _send():
        return await client.send_message(settings.quiz_channel_id, file=media)

    try:
        await _send_with_flood_retry(_send)
        logger.info("Routed quiz %r (%d options) to QUIZ_CHANNEL_ID", shuffled.question[:60], len(shuffled.options))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send shuffled quiz to QUIZ_CHANNEL_ID")


async def _handle_post(event: events.NewMessage.Event) -> None:
    message = event.message
    text = message.raw_text or ""
    entities = message.entities or []

    async with _blacklist_lock:
        blacklist = list(_blacklist_cache)

    new_text, new_entities = filter_message(text, entities, blacklist)

    async def _send():
        if message.media and not message.poll:
            return await client.send_file(
                settings.post_channel_id,
                file=message.media,
                caption=new_text,
                formatting_entities=new_entities or None,
            )
        return await client.send_message(
            settings.post_channel_id,
            new_text,
            formatting_entities=new_entities or None,
            link_preview=bool(new_text),
        )

    try:
        await _send_with_flood_retry(_send)
        logger.info("Routed post from chat %s to POST_CHANNEL_ID", event.chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to relay post to POST_CHANNEL_ID")


@client.on(events.NewMessage())
async def _on_new_message(event: events.NewMessage.Event) -> None:
    if event.chat_id not in monitored_sources:
        return

    message = event.message
    try:
        if message.poll is not None:
            await _handle_quiz(event)
        else:
            await _handle_post(event)
    except Exception:  # noqa: BLE001
        logger.exception("Unhandled error while routing message %s from chat %s", message.id, event.chat_id)


async def start_userbot() -> None:
    await client.start()
    me = await client.get_me()
    logger.info("Userbot connected as %s (id=%s)", getattr(me, "username", None) or me.first_name, me.id)
    await load_initial_sources()
