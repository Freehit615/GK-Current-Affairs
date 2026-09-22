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
from datetime import datetime, timedelta
from typing import Optional

import asyncpg
import pytz
from dotenv import load_dotenv

import google.generativeai as genai

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


GEMINI_MODEL_NAME = "gemini-1.5-pro"

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

genai.configure(api_key=Config.GEMINI_API_KEY)


def _get_grounded_model():
    """Model with Google Search grounding enabled, falling back to plain model."""
    try:
        return genai.GenerativeModel(GEMINI_MODEL_NAME, tools="google_search_retrieval")
    except Exception as e:
        logger.warning(f"Grounded model init failed ({e}); falling back to plain model.")
        return genai.GenerativeModel(GEMINI_MODEL_NAME)


def _get_json_model():
    """Model configured to return structured JSON output."""
    return genai.GenerativeModel(
        GEMINI_MODEL_NAME,
        generation_config={"response_mime_type": "application/json"},
    )


def _extract_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


async def generate_current_affairs_hindi(date_str: str) -> str:
    """Returns formatted Hindi current-affairs post body (without header)."""
    prompt = f"""Tum ek expert current affairs editor ho jo competitive exams (UPSC, SSC, Banking, Railway, State PCS) ke liye content banate ho.

Aaj ki tareekh {date_str} ke liye India aur duniya ki sabसे important, verified aur exam-relevant Current Affairs, General Knowledge (GK) aur General Studies (GS) points taiyar karo.

Requirements:
- Sirf aaj ya kal ki verified, factually accurate news use karo (search karke confirm karo).
- 8 se 12 crisp bullet points.
- Har bullet point exam-oriented ho — important facts, names, numbers, dates highlight karo.
- Categories cover karo jahan relevant ho: National, International, Economy, Sports, Science & Tech, Awards, Appointments, Defence.
- Hindi mein likho, clear aur simple bhasha mein.
- Sirf bullet points do, koi extra intro ya outro nahi chahiye.
- Har bullet "•" se start ho.
"""
    model = _get_grounded_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    return response.text.strip()


async def translate_to_english(hindi_text: str) -> str:
    prompt = f"""Translate the following Hindi current affairs bullet points into clear, accurate, exam-oriented English.
Keep the same bullet point structure ("•" for each point). Do not add any extra commentary, intro, or outro.
Preserve all facts, numbers, names and dates exactly.

Hindi content:
{hindi_text}
"""
    model = genai.GenerativeModel(GEMINI_MODEL_NAME)
    response = await asyncio.to_thread(model.generate_content, prompt)
    return response.text.strip()


async def generate_quiz(content_text: str, language: str) -> list:
    """Returns list of {question, options[4], correct_index, explanation}."""
    lang_name = "Hindi" if language == "hindi" else "English"
    prompt = f"""Based ONLY on the following current affairs content, create 3 multiple-choice questions in {lang_name}.

Content:
{content_text}

Return ONLY a JSON array (no markdown, no extra text) in this exact format:
[
  {{
    "question": "string",
    "options": ["option1", "option2", "option3", "option4"],
    "correct_index": 0,
    "explanation": "short one-line explanation, max 190 characters"
  }}
]
Rules:
- Exactly 3 questions, each with exactly 4 options.
- correct_index is 0-based index into options.
- explanation must be under 190 characters.
- Everything in {lang_name}.
"""
    model = _get_json_model()
    response = await asyncio.to_thread(model.generate_content, prompt)
    try:
        data = _extract_json(response.text)
        if isinstance(data, dict) and "questions" in data:
            data = data["questions"]
        return data
    except Exception as e:
        logger.error(f"Failed to parse quiz JSON: {e} | raw: {response.text[:300]}")
        return []


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
        hindi_body = await generate_current_affairs_hindi(date_str)
    except Exception as e:
        logger.error(f"Gemini Hindi generation failed: {e}")
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

    try:
        english_body = await translate_to_english(hindi_body)
    except Exception as e:
        logger.error(f"Gemini translation failed: {e}")
        english_body = None

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
        hindi_quiz = await generate_quiz(hindi_body, "hindi")
        for q in hindi_quiz:
            await _send_quiz_poll(app, Config.HINDI_QUIZ_CHANNEL_ID, q)
        logger.info(f"Posted {len(hindi_quiz)} Hindi quiz questions.")
    except Exception as e:
        logger.error(f"Hindi quiz flow failed: {e}")

    if english_body:
        try:
            english_quiz = await generate_quiz(english_body, "english")
            for q in english_quiz:
                await _send_quiz_poll(app, Config.ENGLISH_QUIZ_CHANNEL_ID, q)
            logger.info(f"Posted {len(english_quiz)} English quiz questions.")
        except Exception as e:
            logger.error(f"English quiz flow failed: {e}")


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
    await update.message.reply_text("⏳ Fetching current affairs and posting now...")
    app = context.application
    asyncio.create_task(run_current_affairs_flow(app))
    await update.message.reply_text("✅ Triggered. Hindi + English posts will go out shortly, quiz follows in 5 min.")


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

    lines = [
        "<b>📊 Bot Status</b>",
        f"Database: {'🟢 Connected' if db_ok else '🔴 Unreachable'}",
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
