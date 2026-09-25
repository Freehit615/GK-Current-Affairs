# Current Affairs Telegram Automation Bot

Production-grade bot that auto-generates and posts daily Current Affairs / GK / GS
content (Hindi + English) with quiz polls, plus admin-controlled cross-channel
broadcasting. Built with `python-telegram-bot`, Gemini (Google Generative AI),
Neon PostgreSQL (`asyncpg`), and `APScheduler`. Deploys on Railway.

## Files

- `main.py` — full bot logic (single file)
- `requirements.txt` — pinned dependencies
- `README.md` — this file

## How it works

- **Daily post (7:00–9:00 AM IST, random time each day):** Gemini generates
  fresh Hindi current affairs → posted to `HINDI_CHANNEL_ID` → translated to
  English → posted to `ENGLISH_CHANNEL_ID`.
- **Quiz (5 min later):** 2–3 MCQs per language, posted as native Telegram quiz
  polls to `HINDI_QUIZ_CHANNEL_ID` / `ENGLISH_QUIZ_CHANNEL_ID`.
- **Cross-broadcast:** the latest Hindi post is forwarded into the English
  channel and vice versa, either manually via commands or automatically on an
  admin-configurable interval (stored in Postgres, survives restarts).

## Environment Variables

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `GEMINI_API_KEY` | Google Generative AI (Gemini) API key |
| `DATABASE_URL` | Neon Postgres connection string (`postgresql://user:pass@host/db?sslmode=require`) |
| `ADMIN_IDS` | Comma-separated Telegram numeric user IDs, e.g. `111111,222222` |
| `HINDI_CHANNEL_ID` | Chat ID of the Hindi current-affairs channel |
| `ENGLISH_CHANNEL_ID` | Chat ID of the English current-affairs channel |
| `HINDI_QUIZ_CHANNEL_ID` | Chat ID for Hindi quiz polls |
| `ENGLISH_QUIZ_CHANNEL_ID` | Chat ID for English quiz polls |
| `GEMINI_MAX_REQUESTS_PER_DAY` | Optional. Daily Gemini call budget (default `16`) |
| `GEMINI_MIN_SECONDS_BETWEEN_CALLS` | Optional. Min gap between Gemini calls in seconds (default `15`) |
| `GEMINI_MODELS` | Optional. Comma-separated model fallback order (default `gemini-flash-latest,gemini-3.6-flash`) |

Channel IDs are usually negative numbers like `-1001234567890`. The bot must be
an **admin** in every channel it posts to (post + poll permissions).

## Local Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # create this and fill in the variables above
python main.py
```

`.env.example`:
```
TELEGRAM_BOT_TOKEN=
GEMINI_API_KEY=
DATABASE_URL=
ADMIN_IDS=
HINDI_CHANNEL_ID=
ENGLISH_CHANNEL_ID=
HINDI_QUIZ_CHANNEL_ID=
ENGLISH_QUIZ_CHANNEL_ID=
GEMINI_MAX_REQUESTS_PER_DAY=16
GEMINI_MIN_SECONDS_BETWEEN_CALLS=15
```

## Railway Deployment

1. Push these 3 files to a GitHub repo.
2. In Railway, **New Project → Deploy from GitHub repo**, select the repo.
3. Railway auto-detects Python. Set the **Start Command** to:
   ```
   python main.py
   ```
4. Go to **Variables** and add all env vars listed above (values from
   BotFather, Google AI Studio, and your Neon dashboard).
5. Deploy. Check **Deployments → Logs** — you should see:
   `Bot initialized: database ready, scheduler running, commands registered.`
6. The bot is now polling; no webhook or public URL is required.

### Neon PostgreSQL

- Create a free project at neon.tech, copy the pooled connection string into
  `DATABASE_URL`.
- Tables (`bot_settings`, `posts_history`, `broadcast_logs`) are created
  automatically on first startup — no manual migration needed.

## Commands (admin-only, registered in Telegram's menu button)

| Command | Description |
|---|---|
| `/current` | Manually trigger current affairs post + quiz (5 min later) |
| `/hindi_broadcast` | Forward latest Hindi post into the English channel now |
| `/english_broadcast` | Forward latest English post into the Hindi channel now |
| `/broadcast_timer <days>` | Set automated cross-broadcast interval, e.g. `/broadcast_timer 2` |
| `/status` | Show DB connection, broadcast interval, next scheduled runs |

All commands reply "⛔ Permission denied" for any user ID not in `ADMIN_IDS`.

## Notes

- The daily job time is randomized (0–119 min after 7:00 AM IST) and
  re-randomizes itself for the next day after each run, so it never becomes
  predictable.
- Broadcast interval changes take effect immediately — no restart needed.
- Uses the `google-genai` SDK with the `gemini-flash-latest` model alias,
  which Google keeps pointed at its current recommended Flash model — no
  manual updates needed when a specific model version is retired.
- **Google Search grounding is enabled** for the current-affairs generation
  call, so daily posts reflect actual recent events rather than just Gemini's
  training data. Grounding can't be combined with JSON mode (a hard API
  limitation), so that one call uses a plain-text `===HINDI===` / `===ENGLISH===`
  format that's parsed on our side; the quiz call stays JSON since it's based
  on the already-generated content, not live search.
- If grounding fails for **any** reason (quota/entitlement `429`, or a
  transient error), the bot automatically retries the same request without
  grounding — the post still goes out, just without live search that cycle.
  This costs at most **1 extra call**; when grounding succeeds (the normal
  case), there's no extra cost at all.
- Transient Gemini `503` (server overload) errors on non-grounded calls
  trigger an automatic fallback to the next model in `GEMINI_MODELS` (default:
  `gemini-flash-latest,gemini-3.6-flash`). Set `GEMINI_MODELS` (comma-separated)
  to customize.
- **Free-tier request budgeting:** the bot is built to run well within a tight
  free-tier quota (as of this deployment: 5 requests/minute, 20 requests/day).
  Each daily cycle uses **2 Gemini calls in the best case** (1 grounded
  current-affairs call + 1 quiz call), **up to 5 in the worst case** (grounding
  fails +1, then the non-grounded retry hits overload and falls back across
  2 models +1, plus the quiz call's own model fallback +1). A daily counter is
  kept in Postgres (`bot_settings`) and capped at `GEMINI_MAX_REQUESTS_PER_DAY`
  (default `16`, leaving headroom under a 20/day quota); once hit, further
  Gemini calls are skipped for the rest of the day rather than erroring out
  with 429s. Calls are also spaced at least `GEMINI_MIN_SECONDS_BETWEEN_CALLS`
  seconds apart (default `15s`, i.e. max ~4/min) to stay under a 5/min limit.
  Both are configurable via env vars if your quota differs. `/status` shows
  today's usage (`Gemini requests today: X/Y`).
- Quiz explanations are capped at 190 characters to satisfy Telegram's poll
  explanation limit.
