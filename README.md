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
- If Gemini's Google Search grounding tool is unavailable for your API key/
  region, the bot automatically falls back to a plain (non-grounded) model
  call so posting never breaks.
- Quiz explanations are capped at 190 characters to satisfy Telegram's poll
  explanation limit.
