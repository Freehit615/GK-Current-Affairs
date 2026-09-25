"""
config.py
---------
Centralized configuration: environment variables, database connection pool,
and shared constants used across bot.py, database.py, and scheduler.py.
"""

import os
import logging
from typing import List

import asyncpg

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("edubot")


# --------------------------------------------------------------------------
# Helpers to parse environment variables
# --------------------------------------------------------------------------
def _parse_int_list(raw: str) -> List[int]:
    """Parse a comma-separated string of numeric Telegram IDs into a list of ints."""
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_str_list(raw: str) -> List[str]:
    """Parse a comma-separated string of channel identifiers (IDs or @usernames)."""
    if not raw:
        return []
    result = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # Telegram channel IDs are numeric (often negative, e.g. -100xxxxxxxxxx).
        # Usernames start with '@'. Keep both forms usable in send_message(chat_id=...).
        if item.lstrip("-").isdigit():
            result.append(int(item))
        else:
            result.append(item if item.startswith("@") else f"@{item}")
    return result


# --------------------------------------------------------------------------
# Required / optional environment variables
# --------------------------------------------------------------------------
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

ADMIN_IDS: List[int] = _parse_int_list(os.environ.get("ADMIN_IDS", ""))

HINDI_CHANNEL_IDS: List = _parse_str_list(os.environ.get("HINDI_CHANNEL_IDS", ""))
ENGLISH_CHANNEL_IDS: List = _parse_str_list(os.environ.get("ENGLISH_CHANNEL_IDS", ""))
HINDI_QUIZ_CHANNEL_IDS: List = _parse_str_list(os.environ.get("HINDI_QUIZ_CHANNEL_IDS", ""))
ENGLISH_QUIZ_CHANNEL_IDS: List = _parse_str_list(os.environ.get("ENGLISH_QUIZ_CHANNEL_IDS", ""))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is missing.")
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS is empty — no one will be able to control this bot.")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
DEFAULT_REPEAT_DAYS: int = 15          # default revision-repeat interval
QUIZ_DELAY_SECONDS: int = 120          # 2 minutes after main post
FEED_POLL_INTERVAL_SECONDS: int = 3600  # how often scheduler checks feeds for new content
REPEAT_CHECK_INTERVAL_SECONDS: int = 6 * 3600  # how often revision-repeat loop is checked

# Educational/study-safety content filter.
# Any item whose text contains one of these (case-insensitive) is rejected outright.
BLOCKED_KEYWORDS: List[str] = [
    "porn", "sex", "nude", "nsfw", "gambling", "bet now", "suicide", "self-harm",
    "kill", "murder", "rape", "abuse", "explosive", "bomb", "terrorist",
    "drug deal", "cocaine", "heroin", "gore", "violence", "hate speech",
    "escort", "onlyfans", "xxx",
]


# --------------------------------------------------------------------------
# Database connection pool
# --------------------------------------------------------------------------
_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create (once) and return the shared asyncpg connection pool to Neon Postgres."""
    global _pool
    if _pool is None:
        # Neon requires SSL; sslmode is usually already encoded in DATABASE_URL,
        # but we force ssl='require' as a safety net for pools that strip it.
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=30,
            ssl="require",
        )
        logger.info("Database connection pool created.")
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Return the existing pool, creating it if necessary."""
    if _pool is None:
        return await create_pool()
    return _pool


async def close_pool() -> None:
    """Gracefully close the database pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection pool closed.")
