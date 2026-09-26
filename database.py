"""
database.py
------------
Async PostgreSQL persistence via asyncpg, built around a connection pool
that survives restarts/drops. Two tables are managed here:

    monitored_sources(chat_id BIGINT UNIQUE, title TEXT, chat_type TEXT, added_at TIMESTAMPTZ)
    link_blacklist(url_pattern TEXT UNIQUE, added_at TIMESTAMPTZ)

Everything is exposed through a single `Database` object with an internal
pool. Call `Database.connect()` once at startup and `Database.close()` on
shutdown. Every public method retries transient connection errors with
exponential backoff via `_with_reconnect`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from config import settings

logger = logging.getLogger("relaybot.database")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS monitored_sources (
    chat_id     BIGINT PRIMARY KEY,
    title       TEXT NOT NULL,
    chat_type   TEXT NOT NULL CHECK (chat_type IN ('Channel', 'Group')),
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS link_blacklist (
    url_pattern TEXT PRIMARY KEY,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class MonitoredSource:
    chat_id: int
    title: str
    chat_type: str
    added_at: datetime


class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._connected = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool:
        return self._connected and self._pool is not None

    async def connect(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=8,
                command_timeout=30,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(SCHEMA_SQL)
            self._connected = True
            logger.info("Database connected and schema ensured.")

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
            self._pool = None
            self._connected = False

    async def _reconnect_loop(self) -> None:
        """Keep retrying connect() with exponential backoff until it succeeds."""
        delay = settings.db_reconnect_backoff_seconds
        while True:
            try:
                logger.warning("Attempting database reconnect...")
                async with self._lock:
                    self._pool = None
                    self._connected = False
                await self.connect()
                logger.info("Database reconnected.")
                return
            except Exception as exc:  # noqa: BLE001 - genuinely want to catch everything here
                logger.error("Reconnect attempt failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, settings.db_reconnect_backoff_max_seconds)

    async def _with_reconnect(self, coro_factory):
        """
        Run an operation; on a connection-level failure, mark the pool dead,
        kick off a background reconnect, and re-raise so the caller can
        decide how to degrade gracefully (callers should already expect
        the DB to occasionally be unavailable).
        """
        try:
            if self._pool is None:
                await self.connect()
            return await coro_factory()
        except (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            ConnectionError,
            OSError,
        ) as exc:
            logger.error("Database connection error: %s", exc)
            self._connected = False
            asyncio.create_task(self._reconnect_loop())
            raise

    # ------------------------------------------------------------------ #
    # monitored_sources
    # ------------------------------------------------------------------ #
    async def add_source(self, chat_id: int, title: str, chat_type: str) -> None:
        async def op():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO monitored_sources (chat_id, title, chat_type, added_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (chat_id) DO UPDATE
                        SET title = EXCLUDED.title, chat_type = EXCLUDED.chat_type
                    """,
                    chat_id, title, chat_type, datetime.now(timezone.utc),
                )
        await self._with_reconnect(op)

    async def remove_source(self, chat_id: int) -> bool:
        async def op():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM monitored_sources WHERE chat_id = $1", chat_id
                )
                return result.endswith("1")
        return await self._with_reconnect(op)

    async def list_sources(self) -> list[MonitoredSource]:
        async def op():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT chat_id, title, chat_type, added_at FROM monitored_sources "
                    "ORDER BY added_at ASC"
                )
                return [MonitoredSource(**dict(r)) for r in rows]
        return await self._with_reconnect(op)

    # ------------------------------------------------------------------ #
    # link_blacklist
    # ------------------------------------------------------------------ #
    async def add_blacklisted_link(self, url_pattern: str) -> None:
        async def op():
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO link_blacklist (url_pattern, added_at)
                    VALUES ($1, $2)
                    ON CONFLICT (url_pattern) DO NOTHING
                    """,
                    url_pattern.lower().strip(), datetime.now(timezone.utc),
                )
        await self._with_reconnect(op)

    async def remove_blacklisted_link(self, url_pattern: str) -> bool:
        async def op():
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM link_blacklist WHERE url_pattern = $1",
                    url_pattern.lower().strip(),
                )
                return result.endswith("1")
        return await self._with_reconnect(op)

    async def list_blacklisted_links(self) -> list[str]:
        async def op():
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT url_pattern FROM link_blacklist ORDER BY added_at ASC"
                )
                return [r["url_pattern"] for r in rows]
        return await self._with_reconnect(op)


db = Database(settings.database_url)
