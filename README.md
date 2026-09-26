# Telegram Relay & Quiz Bot (5-file build)

Same behavior as the modular version, consolidated into 5 Python files:

```
config.py     Environment variable loading & validation
database.py    asyncpg pool, schema bootstrap, CRUD for monitored_sources + link_blacklist
helpers.py     Link-blacklist filtering + quiz/poll shuffling logic
userbot.py     Telethon user client: listeners + routing + FloodWait handling
bot.py         Telethon bot client (admin commands) + application entrypoint — run this
```

## Setup

1. Generate a `SESSION_STRING` once, locally:
   ```python
   from telethon.sync import TelegramClient
   from telethon.sessions import StringSession
   with TelegramClient(StringSession(), api_id=API_ID, api_hash=API_HASH) as client:
       print(client.session.save())
   ```
2. Copy `.env.example` to `.env` and fill in every value.
3. `pip install -r requirements.txt`
4. `python bot.py`   ← this boots the database, the userbot, and the bot together.

Schema is created automatically on first connect.

## Admin commands

| Command | Effect |
|---|---|
| `/start` | Welcome panel + help |
| `/add <id>` or a bare numeric ID | Detects channel/group, saves, starts monitoring |
| `/del <id>` | Stops monitoring, removes from DB |
| `/dellink <url>` | Blacklists a link (per spec: removes it from future posts) |
| `/addlink <url>` | Un-blacklists a link (per spec: restores normal forwarding) |
| `/stats` | `[Type] Title — (ID)` for every source |
| `/status` | Userbot/bot/DB health + latency + source count |

## Notes

- The **userbot** sends the final messages to `POST_CHANNEL_ID`/`QUIZ_CHANNEL_ID` — make sure that account has posting rights in both.
- Quiz-correctness extraction needs the source poll to be genuine quiz-mode (Telegram only attaches `correct` flags then).
- Blacklist entries can be bare domains (`t.me`) or path fragments (`t.me/joinchat`).
- `DATABASE_URL` validation accepts `mongodb://`, but `database.py` is implemented for PostgreSQL — swap its internals for Motor/PyMongo if you need Mongo; `userbot.py`/`bot.py` only depend on the `Database` interface.
