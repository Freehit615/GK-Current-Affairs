"""
config.py
---------
Centralised configuration loader. Every setting the bot needs comes from
environment variables (12-factor style), so this is the single place that
touches `os.environ`. Import `config` (the module-level singleton at the
bottom) everywhere else instead of re-reading env vars.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("relaybot.config")


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}") from exc


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"ADMINS entry {chunk!r} is not a valid Telegram user id") from exc
    if not ids:
        raise ConfigError("ADMINS must contain at least one numeric Telegram user id")
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    post_channel_id: int
    quiz_channel_id: int
    admins: frozenset[int]
    default_source_id: int | None
    database_url: str

    # Derived / static settings
    flood_wait_max_retries: int = field(default=5)
    db_reconnect_backoff_seconds: float = field(default=2.0)
    db_reconnect_backoff_max_seconds: float = field(default=60.0)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def uses_postgres(self) -> bool:
        return self.database_url.startswith(("postgres://", "postgresql://"))

    def uses_mongo(self) -> bool:
        return self.database_url.startswith("mongodb")


def load_config() -> Config:
    try:
        api_id = int(_require("API_ID"))
    except ValueError as exc:
        raise ConfigError("API_ID must be an integer") from exc

    cfg = Config(
        api_id=api_id,
        api_hash=_require("API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        session_string=_require("SESSION_STRING"),
        post_channel_id=int(_require("POST_CHANNEL_ID")),
        quiz_channel_id=int(_require("QUIZ_CHANNEL_ID")),
        admins=_parse_admin_ids(_require("ADMINS")),
        default_source_id=_optional_int("DEFAULT_SOURCE_ID", default=None),
        database_url=_require("DATABASE_URL"),
    )

    if not cfg.uses_postgres() and not cfg.uses_mongo():
        raise ConfigError(
            "DATABASE_URL must start with postgres://, postgresql:// or mongodb"
        )

    logger.info(
        "Config loaded: %d admin(s), post_channel=%s, quiz_channel=%s, backend=%s",
        len(cfg.admins),
        cfg.post_channel_id,
        cfg.quiz_channel_id,
        "postgres" if cfg.uses_postgres() else "mongo",
    )
    return cfg


# Import-and-use singleton. Any module can `from config import settings`.
settings = load_config()
