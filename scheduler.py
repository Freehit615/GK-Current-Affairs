"""
scheduler.py
------------
Background pipeline:
  1. Fetch content from every registered feed/API (RSS or generic JSON).
  2. Reject unsafe/inappropriate items (educational safety filter).
  3. Translate into Hindi + English.
  4. Post to the configured content channels.
  5. Two minutes later, post a native Telegram Quiz Poll to the quiz channels.
  6. Separately, a 15-day (configurable) revision loop reposts old content
     to the same channels for student revision.

All network/CPU-bound sync calls (feedparser, deep_translator) are pushed
to a thread executor so the event loop is never blocked.
"""

import asyncio
import json
import re
import time
from typing import Optional, List, Dict, Any, Tuple

import aiohttp
import feedparser
from deep_translator import GoogleTranslator
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes
from telegram.error import TelegramError

import config
import database as db
from config import (
    logger,
    BLOCKED_KEYWORDS,
    HINDI_CHANNEL_IDS,
    ENGLISH_CHANNEL_IDS,
    HINDI_QUIZ_CHANNEL_IDS,
    ENGLISH_QUIZ_CHANNEL_IDS,
    QUIZ_DELAY_SECONDS,
    FEED_POLL_INTERVAL_SECONDS,
    REPEAT_CHECK_INTERVAL_SECONDS,
)

MAX_NEW_ITEMS_PER_FEED_PER_RUN = 3  # avoid flooding channels on first-ever fetch


# --------------------------------------------------------------------------
# Fetching (RSS / Atom / generic JSON API)
# --------------------------------------------------------------------------
async def _http_get(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()


def _parse_rss(raw_text: str) -> List[Dict[str, str]]:
    parsed = feedparser.parse(raw_text)
    items = []
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        title = entry.get("title", "").strip()
        summary = (entry.get("summary") or entry.get("description") or "").strip()
        summary = re.sub("<[^<]+?>", "", summary)  # strip HTML tags
        if guid and title:
            items.append({"guid": guid, "title": title, "text": summary})
    return items


def _parse_json_api(raw_text: str) -> List[Dict[str, str]]:
    data = json.loads(raw_text)
    raw_items = (
        data if isinstance(data, list)
        else data.get("articles") or data.get("items") or data.get("results") or []
    )
    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("headline") or "").strip()
        summary = str(
            entry.get("description") or entry.get("summary") or entry.get("content") or ""
        ).strip()
        guid = str(entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("url") or title)
        if guid and title:
            items.append({"guid": guid, "title": title, "text": summary})
    return items


async def fetch_feed_with_diagnostics(url: str) -> Tuple[List[Dict[str, str]], bool, str]:
    """
    Fetch + parse a feed/API URL. Returns (items, is_working, message).

    is_working is True when the endpoint is reachable and returned a
    recognizable structure (RSS/Atom, or JSON with an articles/items/results
    list) -- even if there happen to be zero new items right now.
    is_working is False when the request failed outright, the response
    could not be parsed at all, or the payload looks like an API error
    (e.g. an invalid NewsAPI key returns valid JSON but no article list).
    """
    try:
        raw_text = await _http_get(url)
    except Exception as exc:  # noqa: BLE001
        return [], False, f"Unreachable: {exc}"

    loop = asyncio.get_running_loop()

    try:
        rss_items = await loop.run_in_executor(None, _parse_rss, raw_text)
    except Exception:  # noqa: BLE001
        rss_items = []
    if rss_items:
        return rss_items, True, "OK (RSS/Atom)"

    try:
        data = json.loads(raw_text)
    except Exception:  # noqa: BLE001
        return [], False, "Unrecognized response (not valid RSS/Atom or JSON)"

    # Common "API key invalid / quota exceeded" style error payloads (e.g. NewsAPI).
    if isinstance(data, dict) and str(data.get("status", "")).lower() == "error":
        return [], False, str(data.get("message") or "API returned an error response")

    raw_list = data if isinstance(data, list) else None
    if raw_list is None and isinstance(data, dict):
        raw_list = data.get("articles") or data.get("items") or data.get("results")

    if raw_list is None:
        return [], False, "Unrecognized JSON structure (no articles/items/results list)"

    try:
        json_items = await loop.run_in_executor(None, _parse_json_api, raw_text)
    except Exception as exc:  # noqa: BLE001
        return [], False, f"JSON parse error: {exc}"

    return json_items, True, "OK (JSON API)"


async def fetch_feed_items(url: str) -> List[Dict[str, str]]:
    """Backward-compatible wrapper around fetch_feed_with_diagnostics (items only)."""
    items, _, _ = await fetch_feed_with_diagnostics(url)
    return items


# --------------------------------------------------------------------------
# Educational/study safety filter
# --------------------------------------------------------------------------
def is_content_safe(title: str, text: str) -> bool:
    combined = f"{title} {text}".lower()
    return not any(keyword in combined for keyword in BLOCKED_KEYWORDS)


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------
def _translate_sync(text: str, target: str) -> str:
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception as exc:  # noqa: BLE001
        logger.error("Translation to '%s' failed: %s", target, exc)
        return text  # graceful fallback: original text is better than nothing


async def translate_text(text: str, target: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _translate_sync, text, target)


def _format_post(title: str, body: str) -> str:
    title = title.strip()
    body = body.strip()
    if body:
        return f"📘 <b>{title}</b>\n\n{body}"
    return f"📘 <b>{title}</b>"


async def build_bilingual_post(title: str, text: str) -> Tuple[str, str]:
    """Translate raw source content into ready-to-post Hindi and English strings."""
    hindi_title, english_title = await asyncio.gather(
        translate_text(title, "hi"), translate_text(title, "en"),
    )
    hindi_body, english_body = await asyncio.gather(
        translate_text(text, "hi"), translate_text(text, "en"),
    )
    return _format_post(hindi_title, hindi_body), _format_post(english_title, english_body)


# --------------------------------------------------------------------------
# Multi-channel posting
# --------------------------------------------------------------------------
async def post_to_channels(bot: Bot, channel_ids: List, text: str) -> None:
    for chat_id in channel_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            logger.error("Failed to post to channel %s: %s", chat_id, exc)


# --------------------------------------------------------------------------
# Native Telegram Quiz Poll generation
# --------------------------------------------------------------------------
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "and", "to", "is", "are", "was", "were",
    "for", "with", "at", "by", "from", "this", "that", "it", "as", "be", "has",
    "have", "had", "which", "will", "its", "into", "about",
}


def _key_terms(text: str, limit: int = 6) -> List[str]:
    words = re.findall(r"[A-Za-z]{4,}", text)
    seen, terms = set(), []
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        terms.append(w)
        if len(terms) >= limit:
            break
    return terms


def generate_quiz(title: str, text: str) -> Optional[Tuple[str, List[str], int]]:
    """
    Heuristic quiz generator: builds a fill-in-the-blank style question from the
    most prominent keyword in the content, with plausible distractors drawn from
    other keywords in the same item.

    Returns (question, options[4], correct_option_index) or None if not enough
    material to build a fair question.

    NOTE: This is a lightweight, dependency-free heuristic. For higher-quality
    quizzes, swap this function's body for a call to an LLM/quiz-generation API —
    the rest of the pipeline (posting, scheduling) does not need to change.
    """
    terms = _key_terms(f"{title}. {text}")
    if len(terms) < 4:
        return None

    correct = terms[0]
    distractors = terms[1:4]
    options = [correct] + distractors
    # Simple deterministic shuffle so the correct answer isn't always first.
    correct_index = int(time.time()) % 4
    options[0], options[correct_index] = options[correct_index], options[0]

    blanked = re.sub(re.escape(correct), "_____", title, count=1, flags=re.IGNORECASE)
    if blanked == title:
        blanked = f"Which term relates to: {title[:150]}?"
    question = blanked[:290]  # Telegram poll question limit is 300 chars

    return question, options, correct_index


async def post_quiz(bot: Bot, channel_ids: List, question: str, options: List[str], correct_index: int) -> None:
    for chat_id in channel_ids:
        try:
            await bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=options,
                type="quiz",
                correct_option_id=correct_index,
                is_anonymous=True,  # required by Telegram for channel polls
            )
        except TelegramError as exc:
            logger.error("Failed to post quiz poll to channel %s: %s", chat_id, exc)


async def _delayed_quiz(bot: Bot, title: str, hindi_text: str, english_text: str, delay: int) -> None:
    await asyncio.sleep(delay)

    hi_quiz = generate_quiz(title, hindi_text)
    en_quiz = generate_quiz(title, english_text)

    if hi_quiz:
        await post_quiz(bot, HINDI_QUIZ_CHANNEL_IDS, *hi_quiz)
    if en_quiz:
        await post_quiz(bot, ENGLISH_QUIZ_CHANNEL_IDS, *en_quiz)


# --------------------------------------------------------------------------
# Core pipeline: one feed item -> filtered -> translated -> posted -> quiz scheduled
# --------------------------------------------------------------------------
async def process_item(bot: Bot, feed_id: Optional[int], item: Dict[str, str], force: bool = False) -> str:
    """
    Run one content item through the full pipeline.
    Returns one of: "posted", "duplicate", "filtered".
    """
    title, text, guid = item["title"], item.get("text", ""), item["guid"]

    if not force and feed_id is not None and await db.item_already_posted(feed_id, guid):
        return "duplicate"

    if not is_content_safe(title, text):
        logger.info("Item rejected by safety filter: %s", title[:80])
        return "filtered"

    hindi_post, english_post = await build_bilingual_post(title, text)

    await post_to_channels(bot, HINDI_CHANNEL_IDS, hindi_post)
    await post_to_channels(bot, ENGLISH_CHANNEL_IDS, english_post)

    if feed_id is not None:
        await db.record_post(feed_id, guid, title, hindi_post, english_post)

    # Fire-and-forget: quiz poll 2 minutes later, without blocking the caller.
    asyncio.create_task(_delayed_quiz(bot, title, hindi_post, english_post, QUIZ_DELAY_SECONDS))
    return "posted"


# --------------------------------------------------------------------------
# Periodic job: poll all feeds for new content
# --------------------------------------------------------------------------
async def fetch_all_feeds_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    feeds = await db.get_active_feeds()
    if not feeds:
        return

    working: List[str] = []
    failed: List[Tuple[str, str]] = []

    for feed in feeds:
        items, is_working, message = await fetch_feed_with_diagnostics(feed["url"])

        if is_working:
            working.append(feed["url"])
        else:
            failed.append((feed["url"], message))
            continue  # nothing usable — skip straight to the next feed

        posted_this_feed = 0
        for item in items:
            if posted_this_feed >= MAX_NEW_ITEMS_PER_FEED_PER_RUN:
                break
            result = await process_item(context.bot, feed["id"], item)
            if result == "posted":
                posted_this_feed += 1

    await send_feed_health_report(context.bot, working, failed)


async def send_feed_health_report(bot: Bot, working: List[str], failed: List[Tuple[str, str]]) -> None:
    """Notify every admin how many registered feeds/APIs are currently working vs failing."""
    total = len(working) + len(failed)
    if total == 0:
        return

    lines = [
        "📡 <b>Feed/API Health Report</b>",
        f"Checked: <b>{total}</b>  |  ✅ Working: <b>{len(working)}</b>  |  ❌ Failed: <b>{len(failed)}</b>",
    ]
    if failed:
        lines.append("")
        lines.append("❌ <b>Failing (ignored this run):</b>")
        for url, reason in failed:
            lines.append(f"• <code>{url}</code>\n   ↳ {reason}")

    text = "\n".join(lines)
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode=ParseMode.HTML)
        except TelegramError as exc:
            logger.error("Failed to send feed health report to admin %s: %s", admin_id, exc)


# --------------------------------------------------------------------------
# Periodic job: 15-day (configurable) revision repeat loop
# --------------------------------------------------------------------------
async def repeat_loop_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await db.get_repeat_status()
    if not status["enabled"]:
        return

    due_posts = await db.get_posts_due_for_repeat(status["days"])
    for post in due_posts:
        await post_to_channels(context.bot, HINDI_CHANNEL_IDS, f"🔁 <b>Revision</b>\n\n{post['hindi_text']}")
        await post_to_channels(context.bot, ENGLISH_CHANNEL_IDS, f"🔁 <b>Revision</b>\n\n{post['english_text']}")
        await db.mark_post_repeated(post["id"])

        asyncio.create_task(
            _delayed_quiz(
                context.bot, post["title"] or "", post["hindi_text"], post["english_text"], QUIZ_DELAY_SECONDS
            )
        )


# --------------------------------------------------------------------------
# /test command support
# --------------------------------------------------------------------------
async def run_test_cycle(bot: Bot) -> Tuple[str, str]:
    """
    Pull the latest item from the first active feed and force it through the full
    pipeline (bypassing duplicate-skip so admins can always verify the flow).

    Returns (status, detail):
      status: "no_feeds"    -> nothing is registered in /del list at all
              "fetch_failed" -> feed URL returned no usable RSS/JSON items
              "filtered"     -> an item was found but rejected by the safety filter
              "posted"       -> full pipeline ran and content was posted
      detail: human-readable context (feed URL, or item title) for the reply.
    """
    feeds = await db.get_active_feeds()
    if not feeds:
        return "no_feeds", ""

    feed = feeds[0]
    items, is_working, message = await fetch_feed_with_diagnostics(feed["url"])
    if not is_working or not items:
        return "fetch_failed", f"{feed['url']} — {message}"

    item = items[0]
    result = await process_item(bot, feed["id"], item, force=True)

    if result == "filtered":
        return "filtered", item["title"][:150]

    return "posted", item["title"][:150]


# --------------------------------------------------------------------------
# Job registration
# --------------------------------------------------------------------------
def register_jobs(application: Application) -> None:
    job_queue = application.job_queue
    job_queue.run_repeating(
        fetch_all_feeds_job, interval=FEED_POLL_INTERVAL_SECONDS, first=15, name="fetch_all_feeds"
    )
    job_queue.run_repeating(
        repeat_loop_job, interval=REPEAT_CHECK_INTERVAL_SECONDS, first=30, name="repeat_loop"
    )
    logger.info(
        "Scheduler jobs registered: fetch every %ss, repeat-check every %ss.",
        FEED_POLL_INTERVAL_SECONDS, REPEAT_CHECK_INTERVAL_SECONDS,
    )
