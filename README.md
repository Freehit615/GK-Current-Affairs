# Current Affairs Telegram Bot (Hindi + English)

Har 1 ghante me verified RSS feeds (PIB, The Hindu, etc.) se UPSC/SSC/PCS-relevant
current affairs fetch karta hai, Gemini API se do bhashao (Hindi + English) me
exam-oriented post banwata hai, aur do alag Telegram channels par bhejta hai.
Politics, crime, gossip, elections automatically block ho jaate hain.

---

## 1. Files

| File               | Purpose                                              |
|---------------------|-------------------------------------------------------|
| `main.py`           | Poora bot logic — fetch, filter, Gemini, dispatch    |
| `requirements.txt`  | Python dependencies                                  |
| `README.md`         | Ye file                                              |

Bot apni state ke liye `sent_news.db` (SQLite) khud create karega — usko
repo me manually add karne ki zaroorat nahi.

---

## 2. Prerequisites

1. **Telegram Bot** — [@BotFather](https://t.me/BotFather) se naya bot banao,
   `TELEGRAM_BOT_TOKEN` copy karo.
2. Dono channels (Hindi + English) me **bot ko admin banao** (post karne ke liye
   "Post Messages" permission chahiye).
3. Dono channel IDs nikalo (format: `-100xxxxxxxxxx`). Aasan tarika:
   channel me koi message forward karo [@userinfobot](https://t.me/userinfobot) ko.
4. **Gemini API Key** — [Google AI Studio](https://aistudio.google.com/apikey)
   se free key generate karo.

---

## 3. Local Testing (optional)

```bash
git clone <your-repo-url>
cd <your-repo>
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="123456:ABC-your-token"
export HINDI_CHANNEL_ID="-1001234567890"
export ENGLISH_CHANNEL_ID="-1009876543210"
export GEMINI_API_KEY="your-gemini-key"

python main.py
```

Console me structured logs dikhengi — fetch → filter → Gemini → Telegram send,
har step ke liye.

---

## 4. Deploy on Railway.app

1. Is code ko apne GitHub repo me push karo (`main.py`, `requirements.txt`,
   `README.md`).
2. Railway.app par **New Project → Deploy from GitHub repo** select karo.
3. Repo select karte hi Railway `requirements.txt` detect karke Python
   environment auto-setup kar dega.
4. **Settings → Deploy** me Start Command set karo (agar auto-detect na ho):
   ```
   python main.py
   ```
5. **Variables** tab me ye 4 environment variables add karo:

   | Key                    | Value                          |
   |-------------------------|---------------------------------|
   | `TELEGRAM_BOT_TOKEN`    | BotFather wala token            |
   | `HINDI_CHANNEL_ID`      | e.g. `-1001234567890`           |
   | `ENGLISH_CHANNEL_ID`    | e.g. `-1009876543210`           |
   | `GEMINI_API_KEY`        | Google AI Studio wali key       |

6. **Important:** Ye ek **background worker** hai, web server nahi — isliye
   Railway ke "Service" type ko **Worker** rakho (ya "Web" service me HTTP
   healthcheck **disable** kar do), warna Railway PORT bind na hone par
   service ko unhealthy maan sakta hai.
7. Deploy karo — logs tab me "Bot ready..." message dikhna chahiye, uske
   baad har ghante ek cycle chalega.

### Optional environment variables

| Key                        | Default | Purpose                                  |
|------------------------------|---------|--------------------------------------------|
| `FETCH_INTERVAL_SECONDS`     | `3600`  | Kitne second me ek cycle chale            |
| `MAX_ITEMS_PER_CYCLE`        | `5`     | Ek cycle me max kitni news process ho     |
| `GEMINI_MODEL_NAME`          | `gemini-2.5-flash` | Gemini model override         |
| `LOG_LEVEL`                  | `INFO`  | `DEBUG` / `INFO` / `WARNING` / `ERROR`    |
| `DB_PATH`                    | `sent_news.db` | Dedup SQLite file ka path          |

> ⚠️ Railway ka filesystem **ephemeral** hota hai — redeploy hone par
> `sent_news.db` reset ho sakta hai (kabhi-kabhi purani news dobara post ho
> sakti hai). Agar ye avoid karna ho, Railway ka **Volume** attach karo aur
> `DB_PATH` ko us volume ke andar point karo (e.g. `/data/sent_news.db`).

---

## 5. Kaise kaam karta hai (Architecture)

```
┌─────────────┐    ┌──────────────┐    ┌───────────────┐    ┌──────────────┐
│  RSS Feeds   │ →  │ Topic Filter │ →  │   Gemini API   │ →  │   Telegram   │
│ (PIB, Hindu) │    │ Allow/Block  │    │ EN + HI JSON   │    │  2 Channels  │
└─────────────┘    │ + SQLite     │    └───────────────┘    └──────────────┘
                    │   Dedup      │
                    └──────────────┘
```

1. **Fetch** — `feedparser` se har RSS feed poll hota hai.
2. **Filter** — allow-list keywords (science, ISRO, schemes, economy, sports,
   appointments...) match karna zaroori hai; block-list keywords (election,
   crime, gossip...) mile toh turant reject.
3. **Dedup** — har news URL ka SHA-256 hash `sent_news.db` me check hota hai;
   pehle se bheji gayi news skip ho jaati hai.
4. **Gemini** — `gemini-2.5-flash` ko strict JSON-mode system instruction ke
   saath call kiya jata hai; output `{"english_post": ..., "hindi_post": ...}`.
5. **Dispatch** — dono posts respective channels par `MarkdownV1` formatting
   ke saath bheje jaate hain; agar formatting parse fail ho, bot automatically
   plain-text fallback try karta hai (crash nahi hota).
6. Poora process `while True` loop me `FETCH_INTERVAL_SECONDS` (default 1hr)
   ke gap par repeat hota hai.

---

## 6. Customization Tips

- **Feeds add/remove karna** — `main.py` me `RSS_FEEDS` list edit karo.
- **Topics change karna** — `ALLOWED_KEYWORDS` / `BLOCKED_KEYWORDS` list edit
  karo.
- **Post design change karna** — `SYSTEM_INSTRUCTION` string ke andar wala
  template edit karo (Gemini isi structure ko follow karega).
- **Interval change karna** — `FETCH_INTERVAL_SECONDS` env var set karo, code
  touch karne ki zaroorat nahi.

---

## 7. Troubleshooting

| Problem                                   | Likely Fix                                                        |
|--------------------------------------------|---------------------------------------------------------------------|
| `Missing required environment variable(s)` | Railway Variables tab me sab 4 keys check karo                     |
| Bot message nahi bhej pa raha              | Bot ko channel me admin banao + "Post Messages" permission do      |
| Koi news post nahi ho rahi                 | Feeds ka content allow-list keywords se match nahi ho raha ho sakta — `ALLOWED_KEYWORDS` widen karo, ya logs me `DEBUG` level set karke check karo |
| Gemini invalid JSON error                  | Automatic — item skip hoke agle cycle me retry hota hai            |
| Duplicate news repost ho rahi hai          | Railway volume attach karke `DB_PATH` persistent path par set karo |
