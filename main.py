"""
================================================================================
 Current Affairs Telegram Bot (Hindi + English)
================================================================================
Har 1 ghante me verified RSS feeds se current-affairs news fetch karta hai,
strict topic-filter (UPSC/SSC/PCS exam-relevant) apply karta hai, Gemini API
se structured Hindi + English post banwata hai, aur do alag Telegram
channels par bhejta hai.

Deploy: Railway.app (Worker/Background service — no HTTP port needed)
Run:    python main.py

Environment Variables (Railway → Variables tab):
    TELEGRAM_BOT_TOKEN   -> BotFather se mila token
    HINDI_CHANNEL_ID     -> e.g. -100xxxxxxxxxx
    ENGLISH_CHANNEL_ID   -> e.g. -100xxxxxxxxxx
    GEMINI_API_KEY       -> Google AI Studio se mili key

Optional Environment Variables:
    FETCH_INTERVAL_SECONDS  -> default 3600 (1 hour)
    MAX_ITEMS_PER_CYCLE     -> default 5 (kitni news per cycle process hongi)
    LOG_LEVEL               -> default INFO
================================================================================
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import google.generativeai as genai
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ------------------------------------------------------------------------------
# 1. CONFIGURATION
# ------------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("current-affairs-bot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
HINDI_CHANNEL_ID = os.getenv("HINDI_CHANNEL_ID")
ENGLISH_CHANNEL_ID = os.getenv("ENGLISH_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", "3600"))
MAX_ITEMS_PER_CYCLE = int(os.getenv("MAX_ITEMS_PER_CYCLE", "5"))
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

DB_PATH = os.getenv("DB_PATH", "sent_news.db")

REQUIRED_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "HINDI_CHANNEL_ID": HINDI_CHANNEL_ID,
    "ENGLISH_CHANNEL_ID": ENGLISH_CHANNEL_ID,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}

# RSS feeds — verified / reliable sources only.
# NOTE: RSS endpoints change over time. Verify these are live before deploying;
# swap/add feeds here without touching any other logic.
RSS_FEEDS = [
    {"name": "PIB - English Releases", "url": "https://pib.gov.in/rss/lreleng.xml"},
    {"name": "The Hindu - National", "url": "https://www.thehindu.com/news/national/feeder/default.rss"},
    {"name": "The Hindu - Sci-Tech", "url": "https://www.thehindu.com/sci-tech/feeder/default.rss"},
    {"name": "The Hindu - International", "url": "https://www.thehindu.com/news/international/feeder/default.rss"},
    {"name": "PIB - Science & Technology", "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"},
]

# Topics that are STRICTLY allowed (exam-focused, positive/neutral news only).
ALLOWED_KEYWORDS = [
    "isro", "space", "satellite", "chandrayaan", "gaganyaan", "mission",
    "science", "technology", "research", "innovation", "ai ", "artificial intelligence",
    "environment", "climate", "wildlife", "biodiversity", "conservation",
    "government scheme", "yojana", "policy", "ministry", "cabinet approves",
    "economy", "gdp", "rbi", "budget", "inflation", "trade", "export", "import",
    "sports", "olympic", "asian games", "world cup", "medal", "record",
    "appointment", "appointed", "chairman", "committee", "summit", "agreement",
    "mou", "treaty", "united nations", "g20", "un ", "who ", "unesco",
    "award", "rank", "index", "report released", "launch", "inaugurat",
    "infrastructure", "railway", "defence", "defense", "navy", "air force", "army",
    "health", "vaccine", "education", "digital india", "startup",
]

# Topics that must be BLOCKED even if an allowed keyword is also present.
BLOCKED_KEYWORDS = [
    "election", "poll", "vote bank", "campaign rally", "political party",
    "congress party", "bjp slam", "opposition slam", "criticise", "criticize",
    "murder", "rape", "crime", "arrested", "assault", "riot", "clash",
    "protest turns violent", "gossip", "bollywood", "celebrity", "affair",
    "divorce", "scandal", "controversy", "debate over", "row over",
    "accident", "death toll", "terror attack", "shooting", "bomb blast",
]

SYSTEM_INSTRUCTION = """You are an expert UPSC/SSC/PCS current-affairs content writer.
You will receive a raw news headline and summary. Your job is to produce a
factual, exam-oriented social-media post in BOTH English and Hindi.

STRICT RULES:
1. Only cover facts relevant to competitive exams (schemes, science, environment,
   economy, appointments, international relations, sports milestones, etc).
2. Never include personal opinions, political bias, or sensational language.
3. Be factually precise — mention correct ministry / mission / data / numbers
   when available. Do not invent facts not present in the source text.
4. Output ONLY valid JSON — no markdown fences, no extra commentary — with
   EXACTLY these two keys: "english_post" and "hindi_post".
5. Each post must follow this EXACT structure (keep the emoji and Telegram
   Markdown formatting exactly as shown):

ENGLISH STRUCTURE:
🎯 *[Catchy & Crisp Headline]*

📌 *Key Exam Takeaways:*
• *Point 1:* Deep factual detail with background
• *Point 2:* Why it matters (ministry, treaty, mission, data)
• *Point 3:* Direct exam angle

💡 *Quick Fact:* One-line crisp summary.

🔗 [Source: Read Full Update]({source_url})

HINDI STRUCTURE:
🎯 *[सटीक और आकर्षक शीर्षक]*

📌 *परीक्षा के मुख्य तथ्य:*
• *बिंदु 1:* विस्तृत और प्रामाणिक पृष्ठभूमि
• *बिंदु 2:* संबंधित मंत्रालय, मिशन, या डेटा
• *बिंदु 3:* परीक्षा के लिए सीधा महत्व

💡 *मुख्य बिंदु:* एक लाइन में सार।

🔗 [स्रोत: पूरा पढ़ें]({source_url})

Replace {source_url} with the actual source URL given to you. Keep the '*' bold
markers exactly as shown since the caller sends this as Telegram Markdown.
"""

# ------------------------------------------------------------------------------
# 2. DATA MODELS
# ------------------------------------------------------------------------------


@dataclass
class NewsItem:
    title: str
    summary: str
    link: str
    source: str


# ------------------------------------------------------------------------------
# 3. DEDUPLICATION STORE (SQLite)
# ------------------------------------------------------------------------------


def init_db(db_path: str) -> None:
    """Create the sent_news table if it doesn't already exist."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_news (
                url_hash   TEXT PRIMARY KEY,
                title      TEXT,
                link       TEXT,
                sent_at    TEXT
            )
            """
        )
        conn.commit()
    logger.info("SQLite dedup store ready at %s", db_path)


def hash_url(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


def is_already_sent(db_path: str, url: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.execute(
            "SELECT 1 FROM sent_news WHERE url_hash = ?", (hash_url(url),)
        )
        return cur.fetchone() is not None


def mark_as_sent(db_path: str, item: NewsItem) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_news (url_hash, title, link, sent_at) VALUES (?, ?, ?, ?)",
            (hash_url(item.link), item.title, item.link, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


# ------------------------------------------------------------------------------
# 4. NEWS FETCHING + FILTERING
# ------------------------------------------------------------------------------


def passes_topic_filter(text: str) -> bool:
    """Strict allow/block keyword filter applied on title + summary."""
    lowered = text.lower()

    for blocked in BLOCKED_KEYWORDS:
        if blocked in lowered:
            return False

    for allowed in ALLOWED_KEYWORDS:
        if allowed in lowered:
            return True

    return False  # not explicitly allowed -> skip (strict allow-list behaviour)


def fetch_all_feeds() -> list[NewsItem]:
    """Pull entries from every configured RSS feed. Never raises — logs and
    continues on a per-feed failure so one bad feed doesn't kill the cycle."""
    items: list[NewsItem] = []

    for feed_cfg in RSS_FEEDS:
        name, url = feed_cfg["name"], feed_cfg["url"]
        try:
            parsed = feedparser.parse(url)
            if parsed.bozo and not parsed.entries:
                logger.warning("Feed '%s' could not be parsed cleanly (bozo=%s)", name, parsed.bozo_exception)
                continue

            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip()
                summary = getattr(entry, "summary", getattr(entry, "description", "")).strip()
                link = getattr(entry, "link", "").strip()

                if not title or not link:
                    continue

                # Strip any embedded HTML tags from the summary
                summary = re.sub(r"<[^>]+>", "", summary)

                items.append(NewsItem(title=title, summary=summary, link=link, source=name))

        except Exception:
            logger.exception("Failed to fetch/parse feed '%s' (%s)", name, url)
            continue

    logger.info("Fetched %d raw entries across %d feeds", len(items), len(RSS_FEEDS))
    return items


def filter_and_dedupe(items: list[NewsItem], db_path: str) -> list[NewsItem]:
    accepted: list[NewsItem] = []

    for item in items:
        combined_text = f"{item.title} {item.summary}"

        if not passes_topic_filter(combined_text):
            continue

        if is_already_sent(db_path, item.link):
            continue

        accepted.append(item)

        if len(accepted) >= MAX_ITEMS_PER_CYCLE:
            break

    logger.info("%d item(s) passed topic-filter + dedup check", len(accepted))
    return accepted


# ------------------------------------------------------------------------------
# 5. GEMINI INTEGRATION
# ------------------------------------------------------------------------------


def init_gemini() -> "genai.GenerativeModel":
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL_NAME,
        system_instruction=SYSTEM_INSTRUCTION,
        generation_config={
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    )
    return model


def generate_posts(model: "genai.GenerativeModel", item: NewsItem) -> Optional[dict]:
    """Calls Gemini and returns a dict with 'english_post' and 'hindi_post',
    or None on failure."""
    user_prompt = (
        f"Source: {item.source}\n"
        f"Title: {item.title}\n"
        f"Summary: {item.summary}\n"
        f"Source URL: {item.link}\n\n"
        "Generate the JSON now."
    )

    try:
        response = model.generate_content(user_prompt)
        raw_text = response.text.strip()

        # Safety net in case the model wraps JSON in markdown fences anyway.
        raw_text = re.sub(r"^```(?:json)?|```$", "", raw_text, flags=re.MULTILINE).strip()

        data = json.loads(raw_text)

        if "english_post" not in data or "hindi_post" not in data:
            logger.error("Gemini response missing required keys for '%s'", item.title)
            return None

        return data

    except json.JSONDecodeError:
        logger.exception("Gemini returned invalid JSON for '%s'", item.title)
        return None
    except Exception:
        logger.exception("Gemini API call failed for '%s'", item.title)
        return None


# ------------------------------------------------------------------------------
# 6. TELEGRAM DISPATCH
# ------------------------------------------------------------------------------


def _strip_markdown(text: str) -> str:
    """Fallback: strip common Markdown markers so plain-text send never crashes."""
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", text)
    return text


async def safe_send(bot: Bot, chat_id: str, text: str, label: str) -> bool:
    """Sends a message with Markdown formatting; on parse failure, retries as
    plain text so a single bad post never breaks the whole cycle."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        logger.info("Sent %s post to channel %s", label, chat_id)
        return True

    except TelegramError as e:
        logger.warning("Markdown send failed for %s (%s) — retrying as plain text", label, e)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=_strip_markdown(text),
                disable_web_page_preview=False,
            )
            logger.info("Sent %s post to channel %s (plain-text fallback)", label, chat_id)
            return True
        except TelegramError:
            logger.exception("Plain-text fallback also failed for %s -> %s", label, chat_id)
            return False


# ------------------------------------------------------------------------------
# 7. MAIN CYCLE
# ------------------------------------------------------------------------------


async def run_cycle(bot: Bot, model: "genai.GenerativeModel", db_path: str) -> None:
    logger.info("=== Starting fetch cycle ===")

    raw_items = fetch_all_feeds()
    candidates = filter_and_dedupe(raw_items, db_path)

    if not candidates:
        logger.info("No new exam-relevant news found this cycle.")
        return

    for item in candidates:
        logger.info("Processing: %s", item.title)

        posts = generate_posts(model, item)
        if posts is None:
            # Do not mark as sent -> will be retried next cycle since the
            # generation failed, not the content itself.
            continue

        english_ok = await safe_send(bot, ENGLISH_CHANNEL_ID, posts["english_post"], "English")
        hindi_ok = await safe_send(bot, HINDI_CHANNEL_ID, posts["hindi_post"], "Hindi")

        if english_ok or hindi_ok:
            # Mark sent even on partial success to avoid duplicate reposting;
            # failures are logged above for manual follow-up.
            mark_as_sent(db_path, item)

        await asyncio.sleep(2)  # gentle pacing between Telegram API calls

    logger.info("=== Cycle complete ===")


def validate_env() -> None:
    missing = [name for name, value in REQUIRED_ENV_VARS.items() if not value]
    if missing:
        logger.critical("Missing required environment variable(s): %s", ", ".join(missing))
        sys.exit(1)


async def main() -> None:
    logger.info("Booting Current Affairs Telegram Bot...")
    validate_env()

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True) if Path(DB_PATH).parent != Path("") else None
    init_db(DB_PATH)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    model = init_gemini()

    logger.info(
        "Bot ready. Interval=%ss | Model=%s | Max items/cycle=%s",
        FETCH_INTERVAL_SECONDS, GEMINI_MODEL_NAME, MAX_ITEMS_PER_CYCLE,
    )

    while True:
        try:
            await run_cycle(bot, model, DB_PATH)
        except Exception:
            # Top-level safety net: a single cycle's failure must never kill
            # the whole worker process.
            logger.exception("Unhandled error during fetch cycle")

        logger.info("Sleeping for %s seconds until next cycle...", FETCH_INTERVAL_SECONDS)
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt).")
