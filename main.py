"""
Production-grade Telegram Current Affairs Automation Bot
Stack: python-telegram-bot v20+, Google Generative AI (Gemini), Neon PostgreSQL (asyncpg), APScheduler
Deploy: Railway
"""

import asyncio
import json
import logging
import os
import random
import re
import signal
import time
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import pytz
from dotenv import load_dotenv

from google import genai
from google.genai import types as genai_types

from telegram import Bot, BotCommand, Poll, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

# --------------------------------------------------------------------------
# Config & Logging
# --------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("current_affairs_bot")

IST = pytz.timezone("Asia/Kolkata")


class Config:
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    ADMIN_IDS = {
        int(x.strip())
        for x in os.environ.get("ADMIN_IDS", "").split(",")
        if x.strip().lstrip("-").isdigit()
    }
    HINDI_CHANNEL_ID = os.environ.get("HINDI_CHANNEL_ID", "")
    ENGLISH_CHANNEL_ID = os.environ.get("ENGLISH_CHANNEL_ID", "")
    HINDI_QUIZ_CHANNEL_ID = os.environ.get("HINDI_QUIZ_CHANNEL_ID", "")
    ENGLISH_QUIZ_CHANNEL_ID = os.environ.get("ENGLISH_QUIZ_CHANNEL_ID", "")

    @classmethod
    def validate(cls):
        missing = [
            name
            for name in (
                "TELEGRAM_BOT_TOKEN",
                "GEMINI_API_KEY",
                "DATABASE_URL",
                "HINDI_CHANNEL_ID",
                "ENGLISH_CHANNEL_ID",
                "HINDI_QUIZ_CHANNEL_ID",
                "ENGLISH_QUIZ_CHANNEL_ID",
            )
            if not getattr(cls, name)
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        if not cls.ADMIN_IDS:
            logger.warning("ADMIN_IDS is empty — no one will be able to use admin commands.")


GEMINI_MODEL_NAME = "gemini-flash-latest"  # Google-maintained alias; always points to the current recommended Flash model

# --------------------------------------------------------------------------
# Database Layer
# --------------------------------------------------------------------------

db_pool: Optional[asyncpg.Pool] = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts_history (
    id SERIAL PRIMARY KEY,
    channel_type TEXT NOT NULL,       -- 'hindi' | 'english'
    message_id BIGINT,
    post_date DATE NOT NULL,
    content_snippet TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broadcast_logs (
    id SERIAL PRIMARY KEY,
    target_channel TEXT NOT NULL,
    broadcast_type TEXT NOT NULL,     -- 'manual' | 'automated'
    status TEXT NOT NULL,             -- 'success' | 'failed'
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

DEFAULT_SETTINGS = {
    "broadcast_interval_days": "1",
    "last_broadcast_timestamp": "",
    "gemini_requests_date": "",
    "gemini_requests_count": "0",
}


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(Config.DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        for key, value in DEFAULT_SETTINGS.items():
            await conn.execute(
                """
                INSERT INTO bot_settings (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO NOTHING
                """,
                key,
                value,
            )
    logger.info("Database pool initialized and schema verified.")


async def get_setting(key: str) -> Optional[str]:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM bot_settings WHERE key = $1", key)
        return row["value"] if row else None


async def set_setting(key: str, value: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bot_settings (key, value) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key,
            value,
        )


async def log_post(channel_type: str, message_id: Optional[int], content_snippet: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO posts_history (channel_type, message_id, post_date, content_snippet)
            VALUES ($1, $2, $3, $4)
            """,
            channel_type,
            message_id,
            datetime.now(IST).date(),
            content_snippet[:500],
        )


async def get_latest_post(channel_type: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT * FROM posts_history
            WHERE channel_type = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            channel_type,
        )


async def log_broadcast(target_channel: str, broadcast_type: str, status: str, detail: str = ""):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO broadcast_logs (target_channel, broadcast_type, status, detail)
            VALUES ($1, $2, $3, $4)
            """,
            target_channel,
            broadcast_type,
            status,
            detail[:500],
        )


# --------------------------------------------------------------------------
# Gemini AI Layer
# --------------------------------------------------------------------------

gemini_client = genai.Client(api_key=Config.GEMINI_API_KEY)

GEMINI_MAX_RETRIES = 2
GEMINI_RETRY_BASE_SECONDS = 8

# Free-tier guardrails (Gemini free tier as of this deployment: RPM 5, RPD 20).
# Leave a safety margin below the real RPD cap since other things may share the same key/project.
GEMINI_MAX_REQUESTS_PER_DAY = int(os.environ.get("GEMINI_MAX_REQUESTS_PER_DAY", "16"))
GEMINI_MIN_SECONDS_BETWEEN_CALLS = int(os.environ.get("GEMINI_MIN_SECONDS_BETWEEN_CALLS", "15"))  # 15s -> max 4/min, under RPM 5

_gemini_lock = asyncio.Lock()
_last_gemini_call_monotonic = 0.0


class GeminiBudgetExceeded(Exception):
    """Raised when the daily Gemini free-tier request budget has been used up."""


async def _reserve_gemini_slot():
    """Serializes all Gemini calls: enforces the daily request budget (RPD) and a minimum
    gap between calls (RPM), using bot_settings in Postgres so the budget survives restarts."""
    global _last_gemini_call_monotonic
    async with _gemini_lock:
        today = datetime.now(IST).date().isoformat()
        stored_date = await get_setting("gemini_requests_date")
        if stored_date != today:
            await set_setting("gemini_requests_date", today)
            await set_setting("gemini_requests_count", "0")
            count = 0
        else:
            count = int(await get_setting("gemini_requests_count") or "0")

        if count >= GEMINI_MAX_REQUESTS_PER_DAY:
            raise GeminiBudgetExceeded(
                f"Daily Gemini request budget ({GEMINI_MAX_REQUESTS_PER_DAY}) already used today."
            )

        elapsed = time.monotonic() - _last_gemini_call_monotonic
        if elapsed < GEMINI_MIN_SECONDS_BETWEEN_CALLS:
            await asyncio.sleep(GEMINI_MIN_SECONDS_BETWEEN_CALLS - elapsed)

        _last_gemini_call_monotonic = time.monotonic()
        await set_setting("gemini_requests_count", str(count + 1))


async def get_gemini_usage_today() -> tuple:
    """Returns (used, limit) for today, for /status."""
    today = datetime.now(IST).date().isoformat()
    stored_date = await get_setting("gemini_requests_date")
    used = int(await get_setting("gemini_requests_count") or "0") if stored_date == today else 0
    return used, GEMINI_MAX_REQUESTS_PER_DAY


async def _call_gemini(**kwargs):
    """Reserves a budget/rate slot, then calls generate_content with backoff retries on
    transient errors (503/overload). Does NOT retry 429 quota-exhausted errors."""
    last_exc = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        await _reserve_gemini_slot()
        try:
            return await asyncio.to_thread(gemini_client.models.generate_content, **kwargs)
        except Exception as e:
            last_exc = e
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                raise  # quota issue — no point retrying immediately, and it would burn another slot
            if attempt < GEMINI_MAX_RETRIES:
                wait = GEMINI_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(f"Gemini call failed (attempt {attempt}/{GEMINI_MAX_RETRIES}): {e}. Retrying in {wait}s.")
                await asyncio.sleep(wait)
    raise last_exc


async def _generate_plain(prompt: str) -> str:
    response = await _call_gemini(model=GEMINI_MODEL_NAME, contents=prompt)
    return response.text.strip()


async def _generate_json(prompt: str) -> str:
    response = await _call_gemini(
        model=GEMINI_MODEL_NAME,
        contents=prompt,
        config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text.strip()


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


async def generate_current_affairs_bilingual(date_str: str) -> dict:
    """Single Gemini call that returns BOTH Hindi and English bullet-point bodies,
    to keep daily request count low on the free tier. Returns {"hindi": str, "english": str}."""
    prompt = f"""Tum ek expert current affairs editor ho jo competitive exams (UPSC, SSC, Banking, Railway, State PCS) ke liye content banate ho.

Aaj ki tareekh {date_str} ke liye India aur duniya ki sabसे important, exam-relevant Current Affairs, General Knowledge (GK) aur General Studies (GS) points taiyar karo.

Requirements:
- Apne knowledge ke aadhar par sabसे recent aur exam-relevant events cover karo (note: real-time web search available nahi hai, isliye apne training knowledge tak ki sabसे latest verified information do).
- 8 se 12 crisp bullet points.
- Har bullet point exam-oriented ho — important facts, names, numbers, dates highlight karo.
- Categories cover karo jahan relevant ho: National, International, Economy, Sports, Science & Tech, Awards, Appointments, Defence.
- Har bullet "•" se start ho, koi extra intro/outro nahi.
- Same content do baar do: ek Hindi mein, ek uska accurate English translation (same facts, same bullet structure).

Return ONLY a JSON object, no markdown, in exactly this format:
{{
  "hindi": "• point 1\\n• point 2\\n...",
  "english": "• point 1\\n• point 2\\n..."
}}
"""
    raw_text = await _generate_json(prompt)
    data = _extract_json(raw_text)
    return {"hindi": data["hindi"].strip(), "english": data.get("english", "").strip()}


async def generate_quiz_bilingual(hindi_content: str, english_content: str) -> dict:
    """Single Gemini call that returns quiz questions in BOTH languages.
    Returns {"hindi_quiz": [...], "english_quiz": [...]}, each item:
    {question, options[4], correct_index, explanation}."""
    prompt = f"""Based ONLY on the following current affairs content, create 3 multiple-choice questions.

Hindi content:
{hindi_content}

English content:
{english_content}

Return ONLY a JSON object (no markdown, no extra text) in exactly this format:
{{
  "hindi_quiz": [
    {{"question": "string in Hindi", "options": ["opt1","opt2","opt3","opt4"], "correct_index": 0, "explanation": "short Hindi explanation, max 190 chars"}}
  ],
  "english_quiz": [
    {{"question": "string in English", "options": ["opt1","opt2","opt3","opt4"], "correct_index": 0, "explanation": "short English explanation, max 190 chars"}}
  ]
}}
Rules:
- Exactly 3 questions in each of "hindi_quiz" and "english_quiz" (6 total), covering the same facts.
- Each question has exactly 4 options; correct_index is 0-based.
- explanation under 190 characters.
"""
    raw_text = await _generate_json(prompt)
    try:
        data = _extract_json(raw_text)
        return {
            "hindi_quiz": data.get("hindi_quiz", []),
            "english_quiz": data.get("english_quiz", []),
        }
    except Exception as e:
        logger.error(f"Failed to parse quiz JSON: {e} | raw: {raw_text[:300]}")
        return {"hindi_quiz": [], "english_quiz": []}


# --------------------------------------------------------------------------
# Core Posting Workflow
# --------------------------------------------------------------------------

def build_header(dt: datetime) -> str:
    # Format: DD Mon-YY — Current Affairs, GK & GS  (e.g. 23 Sep-26 — Current Affairs, GK & GS)
    day = dt.strftime("%d")
    mon = dt.strftime("%b")
    yy = dt.strftime("%y")
    return f"{day} {mon}-{yy} — Current Affairs, GK & GS"


async def run_current_affairs_flow(app: Application):
    now_ist = datetime.now(IST)
    date_str = now_ist.strftime("%d %B %Y")
    header = build_header(now_ist)

    try:
        content = await generate_current_affairs_bilingual(date_str)
        hindi_body = content["hindi"]
        english_body = content.get("english") or None
    except GeminiBudgetExceeded as e:
        logger.warning(f"Skipping current affairs post: {e}")
        return
    except Exception as e:
        logger.error(f"Gemini bilingual generation failed: {e}")
        return

    hindi_post = f"<b>{header}</b>\n\n{hindi_body}"
    try:
        msg = await app.bot.send_message(
            chat_id=Config.HINDI_CHANNEL_ID, text=hindi_post, parse_mode=ParseMode.HTML
        )
        await log_post("hindi", msg.message_id, hindi_body)
        logger.info("Posted Hindi current affairs.")
    except Exception as e:
        logger.error(f"Failed to post Hindi current affairs: {e}")
        return

    if english_body:
        english_post = f"<b>{header}</b>\n\n{english_body}"
        try:
            msg = await app.bot.send_message(
                chat_id=Config.ENGLISH_CHANNEL_ID, text=english_post, parse_mode=ParseMode.HTML
            )
            await log_post("english", msg.message_id, english_body)
            logger.info("Posted English current affairs.")
        except Exception as e:
            logger.error(f"Failed to post English current affairs: {e}")

    # Schedule quiz 5 minutes later
    scheduler: AsyncIOScheduler = app.bot_data["scheduler"]
    run_at = datetime.now(IST) + timedelta(minutes=5)
    scheduler.add_job(
        run_quiz_flow,
        trigger="date",
        run_date=run_at,
        args=[app, hindi_body, english_body],
        id=f"quiz_job_{now_ist.strftime('%Y%m%d%H%M%S')}",
        misfire_grace_time=600,
    )
    logger.info(f"Quiz job scheduled for {run_at.isoformat()}")


async def _send_quiz_poll(app: Application, chat_id: str, q: dict):
    options = q["options"]
    await app.bot.send_poll(
        chat_id=chat_id,
        question=q["question"][:300],
        options=[opt[:100] for opt in options],
        type=Poll.QUIZ,
        correct_option_id=int(q["correct_index"]),
        explanation=(q.get("explanation") or "")[:190],
        is_anonymous=True,
    )


async def run_quiz_flow(app: Application, hindi_body: str, english_body: Optional[str]):
    try:
        quiz = await generate_quiz_bilingual(hindi_body, english_body or "")
    except GeminiBudgetExceeded as e:
        logger.warning(f"Skipping quiz: {e}")
        return
    except Exception as e:
        logger.error(f"Quiz generation failed: {e}")
        return

    try:
        for q in quiz.get("hindi_quiz", []):
            await _send_quiz_poll(app, Config.HINDI_QUIZ_CHANNEL_ID, q)
        logger.info(f"Posted {len(quiz.get('hindi_quiz', []))} Hindi quiz questions.")
    except Exception as e:
        logger.error(f"Posting Hindi quiz failed: {e}")

    if english_body:
        try:
            for q in quiz.get("english_quiz", []):
                await _send_quiz_poll(app, Config.ENGLISH_QUIZ_CHANNEL_ID, q)
            logger.info(f"Posted {len(quiz.get('english_quiz', []))} English quiz questions.")
        except Exception as e:
            logger.error(f"Posting English quiz failed: {e}")


# --------------------------------------------------------------------------
# Broadcast Logic
# --------------------------------------------------------------------------

async def hindi_broadcast(app: Application, broadcast_type: str = "manual"):
    row = await get_latest_post("hindi")
    if not row:
        await log_broadcast(Config.ENGLISH_CHANNEL_ID, broadcast_type, "failed", "No Hindi post found")
        return False, "No Hindi post found to broadcast."
    try:
        if row["message_id"]:
            await app.bot.forward_message(
                chat_id=Config.ENGLISH_CHANNEL_ID,
                from_chat_id=Config.HINDI_CHANNEL_ID,
                message_id=row["message_id"],
            )
        else:
            await app.bot.send_message(chat_id=Config.ENGLISH_CHANNEL_ID, text=row["content_snippet"])
        await log_broadcast(Config.ENGLISH_CHANNEL_ID, broadcast_type, "success")
        return True, "Hindi content broadcast to English channel."
    except Exception as e:
        await log_broadcast(Config.ENGLISH_CHANNEL_ID, broadcast_type, "failed", str(e))
        return False, f"Broadcast failed: {e}"


async def english_broadcast(app: Application, broadcast_type: str = "manual"):
    row = await get_latest_post("english")
    if not row:
        await log_broadcast(Config.HINDI_CHANNEL_ID, broadcast_type, "failed", "No English post found")
        return False, "No English post found to broadcast."
    try:
        if row["message_id"]:
            await app.bot.forward_message(
                chat_id=Config.HINDI_CHANNEL_ID,
                from_chat_id=Config.ENGLISH_CHANNEL_ID,
                message_id=row["message_id"],
            )
        else:
            await app.bot.send_message(chat_id=Config.HINDI_CHANNEL_ID, text=row["content_snippet"])
        await log_broadcast(Config.HINDI_CHANNEL_ID, broadcast_type, "success")
        return True, "English content broadcast to Hindi channel."
    except Exception as e:
        await log_broadcast(Config.HINDI_CHANNEL_ID, broadcast_type, "failed", str(e))
        return False, f"Broadcast failed: {e}"


async def automated_broadcast_job(app: Application):
    logger.info("Running automated cross-channel broadcast.")
    await hindi_broadcast(app, broadcast_type="automated")
    await english_broadcast(app, broadcast_type="automated")
    await set_setting("last_broadcast_timestamp", datetime.now(IST).isoformat())


# --------------------------------------------------------------------------
# Scheduler Setup
# --------------------------------------------------------------------------

def schedule_next_daily_job(scheduler: AsyncIOScheduler, app: Application):
    """Self-rescheduling daily job at a random time between 7:00-9:00 AM IST."""
    now_ist = datetime.now(IST)
    next_day = (now_ist + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    random_minutes = random.randint(0, 119)  # 0 to 119 minutes after 7:00 AM
    run_at = next_day.replace(hour=7) + timedelta(minutes=random_minutes)

    async def _job():
        await run_current_affairs_flow(app)
        schedule_next_daily_job(scheduler, app)

    scheduler.add_job(
        _job,
        trigger="date",
        run_date=run_at,
        id="daily_current_affairs_job",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(f"Next daily current affairs job scheduled for {run_at.isoformat()}")


async def schedule_broadcast_job(scheduler: AsyncIOScheduler, app: Application, interval_days: int):
    async def _job():
        await automated_broadcast_job(app)

    scheduler.add_job(
        _job,
        trigger=IntervalTrigger(days=interval_days),
        id="automated_broadcast_job",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(f"Automated broadcast job scheduled every {interval_days} day(s).")


# --------------------------------------------------------------------------
# Admin Auth Decorator
# --------------------------------------------------------------------------

def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in Config.ADMIN_IDS:
            await update.message.reply_text("⛔ Permission denied. You are not authorized to use this bot.")
            logger.warning(f"Unauthorized access attempt by user_id={user_id}")
            return
        return await handler(update, context)

    return wrapper


# --------------------------------------------------------------------------
# Command Handlers
# --------------------------------------------------------------------------

@admin_only
async def cmd_current(update: Update, context: ContextTypes.DEFAULT_TYPE):
    used, limit = await get_gemini_usage_today()
    if used >= limit:
        await update.message.reply_text(
            f"❌ Daily Gemini request budget already used ({used}/{limit}). Try again after midnight IST, "
            f"or raise GEMINI_MAX_REQUESTS_PER_DAY if your actual quota allows more."
        )
        return
    await update.message.reply_text("⏳ Fetching current affairs and posting now...")
    await run_current_affairs_flow(context.application)
    await update.message.reply_text("✅ Done. Hindi + English posts sent (if generation succeeded — check logs), quiz follows in 5 min.")


@admin_only
async def cmd_hindi_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, message = await hindi_broadcast(context.application, broadcast_type="manual")
    await update.message.reply_text(("✅ " if success else "❌ ") + message)


@admin_only
async def cmd_english_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, message = await english_broadcast(context.application, broadcast_type="manual")
    await update.message.reply_text(("✅ " if success else "❌ ") + message)


@admin_only
async def cmd_broadcast_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 1:
        await update.message.reply_text("Usage: /broadcast_timer <days>  (e.g. /broadcast_timer 2)")
        return
    days = int(context.args[0])
    await set_setting("broadcast_interval_days", str(days))
    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    await schedule_broadcast_job(scheduler, context.application, days)
    await update.message.reply_text(f"✅ Automated cross-channel broadcast interval set to every {days} day(s).")


@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scheduler: AsyncIOScheduler = context.application.bot_data["scheduler"]
    interval = await get_setting("broadcast_interval_days") or "1"
    last_broadcast = await get_setting("last_broadcast_timestamp") or "never"

    daily_job = scheduler.get_job("daily_current_affairs_job")
    broadcast_job = scheduler.get_job("automated_broadcast_job")

    db_ok = True
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        db_ok = False

    gemini_used, gemini_limit = await get_gemini_usage_today()

    lines = [
        "<b>📊 Bot Status</b>",
        f"Database: {'🟢 Connected' if db_ok else '🔴 Unreachable'}",
        f"Gemini requests today: {gemini_used}/{gemini_limit}",
        f"Broadcast interval: every {interval} day(s)",
        f"Last automated broadcast: {last_broadcast}",
        f"Next daily post: {daily_job.next_run_time.isoformat() if daily_job and daily_job.next_run_time else 'not scheduled'}",
        f"Next broadcast: {broadcast_job.next_run_time.isoformat() if broadcast_job and broadcast_job.next_run_time else 'not scheduled'}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------
# Lifecycle Hooks
# --------------------------------------------------------------------------

async def post_init(app: Application):
    Config.validate()
    await init_db()

    scheduler = AsyncIOScheduler(timezone=IST)
    app.bot_data["scheduler"] = scheduler

    interval_days = int(await get_setting("broadcast_interval_days") or "1")
    await schedule_broadcast_job(scheduler, app, interval_days)
    schedule_next_daily_job(scheduler, app)

    scheduler.start()

    await app.bot.set_my_commands(
        [
            BotCommand("current", "Manually trigger current affairs + quiz"),
            BotCommand("hindi_broadcast", "Broadcast Hindi content to English channel"),
            BotCommand("english_broadcast", "Broadcast English content to Hindi channel"),
            BotCommand("broadcast_timer", "Set automated broadcast interval in days"),
            BotCommand("status", "Show bot/database/scheduler status"),
        ]
    )
    logger.info("Bot initialized: database ready, scheduler running, commands registered.")


async def post_shutdown(app: Application):
    scheduler: Optional[AsyncIOScheduler] = app.bot_data.get("scheduler")
    if scheduler:
        scheduler.shutdown(wait=False)
    global db_pool
    if db_pool:
        await db_pool.close()
    logger.info("Graceful shutdown complete.")


# --------------------------------------------------------------------------
# Entry Point
# --------------------------------------------------------------------------

def main():
    Config.validate()

    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("current", cmd_current))
    application.add_handler(CommandHandler("hindi_broadcast", cmd_hindi_broadcast))
    application.add_handler(CommandHandler("english_broadcast", cmd_english_broadcast))
    application.add_handler(CommandHandler("broadcast_timer", cmd_broadcast_timer))
    application.add_handler(CommandHandler("status", cmd_status))

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)


if __name__ == "__main__":
    main()
