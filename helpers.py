"""
helpers.py
----------
Two independent pieces of message-processing logic, kept in one file:

  1. LINK FILTERING — strips only blacklisted links out of a message,
     leaving the rest of the text, formatting entities, and other
     legitimate links untouched. Handles MessageEntityUrl (bare pasted
     links), MessageEntityTextUrl (hyperlinked text), and a regex
     safety-net for untagged URLs. Offset math is done in UTF-16 code
     units to match Telegram's wire format (so emoji/astral chars don't
     desync offsets).

  2. QUIZ SHUFFLING — extracts a poll/quiz from an incoming Telethon
     message, shuffles the answer options, and rebuilds a new
     Poll + InputMediaPoll ready to send, with the correct answer
     remapped to its new position.
"""

from __future__ import annotations

import copy
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from telethon.tl.types import (
    InputMediaPoll,
    Message,
    MessageEntityTextUrl,
    MessageEntityUrl,
    Poll,
    PollAnswer,
    TextWithEntities,
    TypeMessageEntity,
)

logger = logging.getLogger("relaybot.helpers")

# ======================================================================== #
# 1. LINK FILTERING
# ======================================================================== #

_BARE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s]+", re.IGNORECASE)


def _domain_of(url: str) -> str:
    if "://" not in url:
        url = "http://" + url
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0]


def _matches_blacklist(url: str, blacklist: list[str]) -> bool:
    domain = _domain_of(url)
    url_lower = url.lower()
    for pattern in blacklist:
        pattern = pattern.lower().strip()
        if not pattern:
            continue
        # Support both bare-domain patterns ("t.me", "bit.ly") and
        # substring/path patterns ("t.me/joinchat").
        if "/" in pattern or pattern.count(".") == 0:
            if pattern in url_lower:
                return True
        else:
            if domain == pattern or domain.endswith("." + pattern):
                return True
    return False


def _to_utf16(text: str) -> bytes:
    """Encode to UTF-16LE so len()//2 gives Telegram-compatible code-unit offsets."""
    return text.encode("utf-16-le")


@dataclass
class _Removal:
    start: int  # UTF-16 code-unit offset
    end: int    # UTF-16 code-unit offset (exclusive)


def filter_message(
    text: str | None,
    entities: list[TypeMessageEntity] | None,
    blacklist: list[str],
) -> tuple[str, list[TypeMessageEntity]]:
    """
    Return (new_text, new_entities) with every blacklisted link removed.
    Non-matching entities are preserved with offsets shifted to account for
    any text that was removed before them.
    """
    if not text or not blacklist:
        return text or "", list(entities or [])

    utf16 = _to_utf16(text)
    removals: list[_Removal] = []
    surviving_entities: list[TypeMessageEntity] = []

    for ent in entities or []:
        url = None
        if isinstance(ent, MessageEntityTextUrl):
            url = ent.url
        elif isinstance(ent, MessageEntityUrl):
            seg = utf16[ent.offset * 2: (ent.offset + ent.length) * 2]
            url = seg.decode("utf-16-le", errors="ignore")

        if url and _matches_blacklist(url, blacklist):
            if isinstance(ent, MessageEntityUrl):
                # Bare URL entity: remove the literal text.
                removals.append(_Removal(ent.offset, ent.offset + ent.length))
            else:
                # Hyperlinked text: drop the hyperlink but keep the visible
                # text in place (so "click here" doesn't vanish), unless the
                # visible text itself looks like the same blacklisted URL.
                seg = utf16[ent.offset * 2: (ent.offset + ent.length) * 2]
                visible = seg.decode("utf-16-le", errors="ignore")
                if _BARE_URL_RE.fullmatch(visible.strip()):
                    removals.append(_Removal(ent.offset, ent.offset + ent.length))
                # else: entity simply dropped below (not re-added), text kept.
            continue

        surviving_entities.append(ent)

    # Safety-net: bare URLs with no entity at all (offsets computed fresh).
    for match in _BARE_URL_RE.finditer(text):
        candidate = match.group(0)
        if _matches_blacklist(candidate, blacklist):
            start_u16 = len(_to_utf16(text[: match.start()])) // 2
            end_u16 = len(_to_utf16(text[: match.end()])) // 2
            if not any(r.start == start_u16 and r.end == end_u16 for r in removals):
                removals.append(_Removal(start_u16, end_u16))

    if not removals:
        return text, surviving_entities

    removals.sort(key=lambda r: r.start)
    merged: list[_Removal] = []
    for r in removals:
        if merged and r.start <= merged[-1].end:
            merged[-1] = _Removal(merged[-1].start, max(merged[-1].end, r.end))
        else:
            merged.append(r)

    # Rebuild text with removed spans cut out (UTF-16 code-unit accurate).
    new_utf16 = bytearray()
    cursor = 0
    for r in merged:
        new_utf16 += utf16[cursor * 2: r.start * 2]
        cursor = r.end
    new_utf16 += utf16[cursor * 2:]
    new_text = bytes(new_utf16).decode("utf-16-le")

    def _shift(offset: int) -> int:
        shift = 0
        for r in merged:
            span = r.end - r.start
            if r.end <= offset:
                shift += span
            elif r.start < offset:
                shift += offset - r.start
        return offset - shift

    new_entities: list[TypeMessageEntity] = []
    for ent in surviving_entities:
        new_offset = _shift(ent.offset)
        new_end = _shift(ent.offset + ent.length)
        new_length = max(0, new_end - new_offset)
        if new_length <= 0:
            continue
        # Telethon entity objects are plain mutable TLObjects; copy.copy()
        # preserves subclass-specific fields (e.g. user_id, language) that
        # vary by entity type, and we only need to adjust offset/length.
        ent_copy = copy.copy(ent)
        ent_copy.offset = new_offset
        ent_copy.length = new_length
        new_entities.append(ent_copy)

    # Collapse accidental double-spaces / leading-trailing whitespace left
    # behind by stripped links, without touching intentional line breaks.
    new_text = re.sub(r"[ \t]{2,}", " ", new_text).strip()

    return new_text, new_entities


# ======================================================================== #
# 2. QUIZ / POLL SHUFFLING
# ======================================================================== #

class NotAQuizPoll(Exception):
    """Raised when a message doesn't contain a poll we can process."""


def _text_of(value) -> str:
    """Poll.question / PollAnswer.text may be a plain str (older Telethon)
    or a TextWithEntities wrapper (newer Telethon / layer). Normalise."""
    if isinstance(value, TextWithEntities):
        return value.text
    return value or ""


@dataclass
class ShuffledQuiz:
    question: str
    options: list[str]          # shuffled, display order
    correct_index: int | None   # index into `options`, or None if unknown
    explanation: str | None
    is_quiz: bool
    multiple_choice: bool


def extract_and_shuffle(message: Message) -> ShuffledQuiz:
    """Pull the poll out of a Telethon message and return a shuffled version."""
    media = getattr(message, "media", None)
    poll = getattr(media, "poll", None)
    if poll is None:
        raise NotAQuizPoll("Message has no poll/quiz media.")

    results = getattr(media, "results", None)
    correct_options: set[bytes] = set()
    explanation: str | None = None
    if results and getattr(results, "results", None):
        for voters in results.results:
            if getattr(voters, "correct", False):
                correct_options.add(voters.option)
        solution = getattr(results, "solution", None)
        if solution:
            explanation = _text_of(solution) if isinstance(solution, TextWithEntities) else solution

    answers: list[PollAnswer] = list(poll.answers)
    indexed = list(enumerate(answers))
    random.shuffle(indexed)

    shuffled_texts: list[str] = []
    correct_index: int | None = None
    for new_idx, (_, answer) in enumerate(indexed):
        shuffled_texts.append(_text_of(answer.text))
        if answer.option in correct_options:
            correct_index = new_idx

    if getattr(poll, "quiz", False) and correct_index is None:
        logger.warning(
            "Quiz poll %r had no matched correct option (Telethon layer/version "
            "mismatch?) — forwarding without a guaranteed correct answer.",
            _text_of(poll.question),
        )

    return ShuffledQuiz(
        question=_text_of(poll.question),
        options=shuffled_texts,
        correct_index=correct_index,
        explanation=explanation,
        is_quiz=bool(getattr(poll, "quiz", False)),
        multiple_choice=bool(getattr(poll, "multiple_choice", False)),
    )


def build_input_media_poll(shuffled: ShuffledQuiz) -> InputMediaPoll:
    """Turn a ShuffledQuiz back into something Telethon can send."""
    answers = [
        PollAnswer(text=TextWithEntities(text=opt, entities=[]), option=bytes([i]))
        for i, opt in enumerate(shuffled.options)
    ]

    new_poll = Poll(
        id=0,
        question=TextWithEntities(text=shuffled.question, entities=[]),
        answers=answers,
        closed=False,
        public_voters=False,
        multiple_choice=shuffled.multiple_choice,
        quiz=shuffled.is_quiz,
    )

    correct_answers = None
    if shuffled.is_quiz and shuffled.correct_index is not None:
        correct_answers = [bytes([shuffled.correct_index])]

    solution_entities: list = []
    return InputMediaPoll(
        poll=new_poll,
        correct_answers=correct_answers,
        solution=shuffled.explanation or None,
        solution_entities=solution_entities if shuffled.explanation else None,
    )
