"""
database.py
-----------
PostgreSQL (Neon) schema management and all data-access functions:
  - feed registration / duplicate detection / deletion
  - per-item post history (so the same feed entry is never posted twice)
  - 15-day (configurable) revision-repeat tracking
  - bot settings (repeat on/off, repeat interval)
  - stats for /status
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import asyncpg

from config import get_pool, logger, DEFAULT_REPEAT_DAYS


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feeds (
    id          SERIAL PRIMARY KEY,
    url         TEXT UNIQUE NOT NULL,
    added_by    BIGINT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS posts (
    id              SERIAL PRIMARY KEY,
    feed_id         INTEGER REFERENCES feeds(id) ON DELETE SET NULL,
    item_guid       TEXT NOT NULL,           -- unique id/link of the source item (dedupe)
    title           TEXT,
    hindi_text      TEXT NOT NULL,
    english_text    TEXT NOT NULL,
    posted_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    repeated_at     TIMESTAMPTZ,             -- set once the 15-day revision repost fires
    UNIQUE (feed_id, item_guid)
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_repeat_lookup
    ON posts (posted_at) WHERE repeated_at IS NULL;
"""

DEFAULT_SETTINGS = {
    "repeat_enabled": "false",
    "repeat_days": str(DEFAULT_REPEAT_DAYS),
}


async def init_db() -> None:
    """Create tables/indexes if they don't exist yet, and seed default settings."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(SCHEMA_SQL)
            for key, value in DEFAULT_SETTINGS.items():
                await conn.execute(
                    """
                    INSERT INTO settings (key, value) VALUES ($1, $2)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    key, value,
                )
    logger.info("Database schema verified/initialized.")


# --------------------------------------------------------------------------
# Feed management
# --------------------------------------------------------------------------
async def add_feed(url: str, added_by: int) -> bool:
    """
    Register a new feed/API URL.
    Returns True if inserted, False if it already existed (duplicate).
    """
    pool = await get_pool()
    try:
        await pool.execute(
            "INSERT INTO feeds (url, added_by) VALUES ($1, $2)",
            url, added_by,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def feed_exists(url: str) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT 1 FROM feeds WHERE url = $1", url)
    return row is not None


async def delete_feed(url: str) -> bool:
    """Delete a feed by exact URL. Returns True if a row was removed."""
    pool = await get_pool()
    result = await pool.execute("DELETE FROM feeds WHERE url = $1", url)
    # asyncpg returns strings like "DELETE 1"
    return result.split()[-1] != "0"


async def get_active_feeds() -> List[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch("SELECT id, url FROM feeds WHERE is_active = TRUE ORDER BY id")


async def get_feed_count() -> int:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT COUNT(*) AS c FROM feeds WHERE is_active = TRUE")
    return row["c"]


# --------------------------------------------------------------------------
# Post / item history (per-feed-item duplicate protection + revision data)
# --------------------------------------------------------------------------
async def item_already_posted(feed_id: int, item_guid: str) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM posts WHERE feed_id = $1 AND item_guid = $2",
        feed_id, item_guid,
    )
    return row is not None


async def record_post(
    feed_id: Optional[int],
    item_guid: str,
    title: str,
    hindi_text: str,
    english_text: str,
) -> int:
    """Store a successfully-posted item so it can later be picked up by the revision loop."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO posts (feed_id, item_guid, title, hindi_text, english_text)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (feed_id, item_guid) DO NOTHING
        RETURNING id
        """,
        feed_id, item_guid, title, hindi_text, english_text,
    )
    return row["id"] if row else -1


async def get_posts_due_for_repeat(repeat_days: int) -> List[asyncpg.Record]:
    """Posts older than `repeat_days` that have not yet been re-posted for revision."""
    pool = await get_pool()
    cutoff = datetime.now(timezone.utc) - timedelta(days=repeat_days)
    return await pool.fetch(
        """
        SELECT id, title, hindi_text, english_text
        FROM posts
        WHERE repeated_at IS NULL AND posted_at <= $1
        ORDER BY posted_at ASC
        """,
        cutoff,
    )


async def mark_post_repeated(post_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "UPDATE posts SET repeated_at = now() WHERE id = $1", post_id,
    )


# --------------------------------------------------------------------------
# Settings (repeat mode on/off + interval)
# --------------------------------------------------------------------------
async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", key)
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO settings (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        key, value,
    )


async def set_repeat_mode(enabled: bool, days: Optional[int] = None) -> None:
    await set_setting("repeat_enabled", "true" if enabled else "false")
    if days is not None:
        await set_setting("repeat_days", str(days))


async def get_repeat_status() -> Dict[str, Any]:
    enabled = (await get_setting("repeat_enabled", "false")) == "true"
    days = int(await get_setting("repeat_days", str(DEFAULT_REPEAT_DAYS)))
    return {"enabled": enabled, "days": days}


# --------------------------------------------------------------------------
# Stats for /status
# --------------------------------------------------------------------------
async def get_stats() -> Dict[str, Any]:
    pool = await get_pool()
    feed_count = await get_feed_count()
    total_posts_row = await pool.fetchrow("SELECT COUNT(*) AS c FROM posts")
    pending_repeat_row = await pool.fetchrow(
        "SELECT COUNT(*) AS c FROM posts WHERE repeated_at IS NULL"
    )
    repeat_status = await get_repeat_status()
    return {
        "active_feeds": feed_count,
        "total_posts": total_posts_row["c"],
        "pending_revision": pending_repeat_row["c"],
        "repeat_enabled": repeat_status["enabled"],
        "repeat_days": repeat_status["days"],
    }


async def db_health_check() -> bool:
    """Simple connectivity probe used by /status."""
    try:
        pool = await get_pool()
        await pool.fetchval("SELECT 1")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("DB health check failed: %s", exc)
        return False
