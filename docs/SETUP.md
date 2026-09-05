# Connecting a research engine

ARGUS needs one API key before its AI research features turn on. Quant
metrics, charts, price history, news, earnings, and the Brent gate all work
with **no key at all** — this is only for the "run research on a ticker"
features.

There are three ways to add a key. All three end up in the same place — the
server's `.env` file — they just differ in how you get there.

---

## Option A — from the browser (recommended — no file editing, ever)

1. **Start the server** — this is the one step that always needs a terminal,
   the same way starting any local app does:
   ```bash
   uvicorn api:app --workers 1 --no-proxy-headers
   ```
   The very first time you run this with no `.env` yet, the console prints a
   freshly generated `AGENT_SECRET` — something like:
   ```
   First run — generated your AGENT_SECRET (saved to .env):
       a1b2c3d4...
   Paste it into "I HAVE A KEY" in the browser to unlock.
   ```
   Copy that value. It's saved to `.env` and won't be shown again.
2. Open `http://localhost:8000`, click **I HAVE A KEY**, and paste it in.
   You're now unlocked as the owner.
3. Open **◈ CONFIG** (top of the terminal). Under **AI Research**, paste a
   Groq or Perplexity key into either field and press **SAVE**. It's live
   immediately — no restart, no file to touch.

That's the whole flow. Nothing after step 1 ever needs a terminal again —
adding, changing, or removing a key later is always just **◈ CONFIG**.

### Getting a free Groq key (about a minute, no card needed)

1. Go to [console.groq.com/keys](https://console.groq.com/keys)
2. Sign up (email or Google/GitHub)
3. Click **Create API Key**, give it any name
4. Copy the key — it starts with `gsk_`
5. Paste it into the Groq field in **◈ CONFIG** and press **SAVE**

That's it. Research is now on.

---

## Option B — the guided CLI wizard

Prefer a terminal, or scripting an install? This does the same thing as
Option A end to end, including generating `AGENT_SECRET` for you:

```bash
python main.py setup
```

It asks which provider you want, gives you the sign-up link, asks you to
paste the key in, and saves everything — you never have to open or edit a
file by hand. **◈ CONFIG** in the browser remains available afterward for any
future changes.

---

## Option C — edit `.env` yourself

If you'd rather do it directly:

1. Copy the example file:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in any text editor.
3. Paste your key into **one** of these two lines:
   ```
   PERPLEXITY_API_KEY=pplx-...
   GROQ_API_KEY=gsk_...
   ```
4. Set `AGENT_SECRET` to a password of your choosing (or generate one:
   `python -c "import secrets; print(secrets.token_hex(32))"`).
5. Save the file, then (re)start the server for the change to take effect.

---

## Which key should I get?

| | Groq | Perplexity |
|---|---|---|
| Cost | **Free** | Paid — roughly $0.04–$0.21 per research run |
| Sign-up | ~1 minute, no card | Card required |
| Live web search | **No** — answers come from the model's own training, which can be months out of date | **Yes** — reads current filings and news while it answers |
| Every research run says which one produced it | Yes | Yes |

**Start with Groq.** It's free and gets AI research working immediately. Add
a Perplexity key later, any time, for higher-quality, current-data research —
you can have both keys set at once; ARGUS always prefers Perplexity when it's
available.

---

## A word on security

- Your key lives in your own `.env` file, on your own machine (or your own
  server, if you deploy this) — never anywhere else, never with anyone else.
- Whichever option you use, the raw key is **never sent back to any browser**
  once saved, never logged, and never shown to you again in full — Option A's
  CONFIG modal only ever shows *whether* a provider is configured, the same
  way `AGENT_SECRET` itself is never re-displayed either.
- `.env` is already excluded from git (see `.gitignore`) — a normal
  `git add .` / `git push` will never include it. The CLI wizard
  double-checks this and warns you if something has gone wrong.
- Nobody else can spend your key's credits through the app without your
  `AGENT_SECRET` — that's what it's for. Adding a provider key from **◈
  CONFIG** is itself owner-only: a guest session gets refused outright.

If you ever want to stop paying for a key or switch providers, open **◈
CONFIG** and save a new one — or leave the field blank and press **SAVE** to
disconnect a provider entirely.
