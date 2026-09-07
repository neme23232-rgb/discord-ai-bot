# AI Discord Bot (discord.py + OpenAI/Gemini)

A production-ready Discord chat bot with **per-channel conversation memory** and
**switchable personas** (custom system prompt, ⚡ fast mode, 🧠 deep-thinking mode).
Works with **OpenAI**, **Google Gemini**, or **any OpenAI-compatible API**
(GLM/B.AI, OpenRouter, local servers) — switch via one `.env` variable.

> 📘 **Just want to run it?** The beginner-friendly guide (keys, setup, usage,
> troubleshooting) lives in [USAGE.md](USAGE.md). This README covers the
> architecture and development details.

No API keys or secrets are stored in the repository: all credentials live in a
local `.env` file that git never commits (see `.gitignore`), and runtime data
(`data/`, `logs/`) is excluded as well.

```
config.py     loads + validates all environment variables (fails fast, lists ALL problems)
prompts.py    system-prompt presets (default / fast / brain)
storage.py    per-channel history + system prompt, persisted to data/history.json
ai_client.py  provider-agnostic async AI facade (OpenAI & Gemini adapters, retry + backoff)
bot.py        discord.py bot: commands, embeds, routing, error handling
tests/        dependency-free smoke tests for the storage layer
```

---

## 1. Prerequisites

* **Python 3.11+** (`python --version` to check)
* A Discord account with permission to add bots to a server you control
* An API key from OpenAI **or** Google AI Studio (both are free to start; OpenAI
  requires a small credit balance for API use)

---

## 2. Getting the API keys

### 2a. Discord bot token

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application** → give it a name → **Create**.
2. Open the **Bot** tab in the left sidebar.
3. Click **Reset Token** → **Copy**. This is your `DISCORD_TOKEN`.
   ⚠️ It is shown **only once** — store it in `.env` immediately and never commit it.
4. On the same page, scroll to **Privileged Gateway Intents** and enable
   **MESSAGE CONTENT INTENT**. *This is mandatory* — the bot reads normal chat
   messages, and Discord rejects the login without it.
5. Invite the bot to your server: **OAuth2 → URL Generator** → check
   `bot` → permissions: *Send Messages*, *Read Message History*, *Read
   Messages/View Channels* → copy the generated URL → open it → pick your server.

### 2b. AI provider key

| Provider | Where to get it | Env variable |
|---|---|---|
| **Gemini** (recommended to start) | [Google AI Studio](https://aistudio.google.com/apikey) → *Create API key*. Free tier available, no credit card. | `GEMINI_API_KEY` |
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → *Create new secret key*. Requires billing enabled on the project. | `OPENAI_API_KEY` |
| **B.AI / GLM & other OpenAI-compatible APIs** | Any service exposing the OpenAI Chat Completions protocol — e.g. B.AI (`chat.b.ai` → API key), Z.ai/GLM, OpenRouter. | `AI_API_KEY` + `AI_BASE_URL` + `AI_MODEL` |

For OpenAI-compatible services set three variables instead (works with **no
VPN**, from any region the service itself serves):

```ini
AI_PROVIDER=custom
AI_BASE_URL=https://api.b.ai/v1   # the service's base URL
AI_API_KEY=your-key               # key from that service
AI_MODEL=glm-5.3-flash            # exact model id (check the service's docs / /v1/models)
```

Keep both keys secret: anyone with them can spend your quota. They live only in
`.env`, which is excluded from git via `.gitignore`.

---

## 3. Installation & configuration

```bash
# 1. from the project folder
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. dependencies
pip install -r requirements.txt

# 3. configuration
copy .env.example .env      # (macOS/Linux: cp .env.example .env)
```

Open `.env` and fill in:

```ini
DISCORD_TOKEN=your-discord-token-here
AI_PROVIDER=gemini            # or openai, or custom (OpenAI-compatible, see section 2b)
GEMINI_API_KEY=your-gemini-key
# OPENAI_API_KEY=your-openai-key   (only when AI_PROVIDER=openai)
# AI_MODEL=gemini-2.5-flash        (optional override)
```

---

## 4. Step-by-step local testing (safe procedure)

**Step 0 — syntax check (no keys needed).**
```bash
python -m compileall -q .
python tests/test_storage.py
```
The second command runs the storage smoke tests (persistence, rolling window,
channel isolation, `.clear` behaviour, corrupt-file recovery) and should print
`All storage smoke tests passed ✅`.

**Step 1 — dry-run the config.**
```bash
python -c "from config import load_config; load_config(); print('config OK')"
```
With an incomplete `.env` this deliberately fails, listing *every* missing item
at once — e.g. `DISCORD_TOKEN is missing…`, `No API key found for provider…`.
Fix until it prints `config OK`. If the chosen provider's SDK is missing you'll
get a clear `pip install …` hint.

**Step 2 — verify the AI key alone (no Discord involved).**
```bash
python -c "import asyncio; from config import load_config; from ai_client import build_provider, chat_with_retries; c=load_config(); p=build_provider(c); print(asyncio.run(chat_with_retries(p, 'Answer in one word.', [{'role':'user','content':'Say OK.'}], timeout=30)))"
```
Printing `OK` proves the provider + key + model all work, so any later problem
is on the Discord side. If you see `AIPermanentError`, the key/model is wrong;
`AITransientError` means rate limit or outage — wait and retry.

**Step 3 — start the bot.**
```bash
python bot.py
```
Expected console output:
```
INFO aibot: Config OK — provider=gemini model=gemini-2.5-flash prefix='.' history=...\data\history.json
INFO aibot.ai: Using Gemini provider (model=gemini-2.5-flash).
INFO discord.client: logged in as ...
INFO aibot: Logged in as YourBot#1234 (id=…) — serving 1 guild(s).
```
The bot's Discord status should change to *“Playing .help — chat with me”*.

**Step 4 — test in Discord (private server recommended).**

| # | Action | Expected result |
|---|---|---|
| 1 | `.help` | Embed with 💬 AI Chat / 🧠 AI Mode Configuration / 🧹 Utility sections |
| 2 | `.ask What is 2+2?` | Reply `4`; "Bot is typing…" while waiting |
| 3 | `.ask What did I just ask?` | It remembers — the history works |
| 4 | `.promt You are a pirate. Always answer like a pirate.` | ✅ confirmation embed |
| 5 | `.ask hello` | Pirate-flavoured answer |
| 6 | `.promt` | Shows the pirate prompt currently active |
| 7 | `.promt-fast` then `.ask Explain quantum computing` | ⚡ short, blunt answer |
| 8 | `.promt-brain` then the same question | 🧠 structured step-by-step breakdown |
| 9 | `.clear` | 🧹 cleared; then `.ask What did I ask before?` → no memory |
| 10 | **Restart the bot**, then `.ask What did I ask before?` (asked something again first) | history survived the restart → persistence works |
| 11 | Type `hello` — **without any prefix** | The AI answers: free chat is on by default (`.chat off` disables it in that channel) |

**Step 5 — kill-safety check.** While the bot is running, delete
`data/history.json.tmp` if you see one (it exists only for a split second —
that's the atomic-write pattern), then Ctrl+C the bot. You should see a clean
shutdown and `data/history.json` intact.

### Testing etiquette / safety

* Test in **your own server** with the bot's only audience being you — AI replies
  are not moderated content.
* Expect Discord **rate limits** if you spam: the bot handles 429s internally
  (discord.py queues sends), and chat requests have a 3 s per-user cooldown.
* Free-tier keys have low rate limits (Gemini ≈ 10–15 req/min) — bursts will
  produce the 🟠 "service seems down" message; that's the retry policy being
  honest, wait ~30 s.

---

## 5. How the pieces work

### History management (`storage.py`)

* One JSON file (`data/history.json`) holds **every** channel:
  `{"channels": {"<channel_id>": {"system_prompt": "...|null", "messages": [...]}}}`.
* A full **in-memory mirror** is loaded once at startup — reads never touch disk.
* Every mutation schedules a **debounced save** (2 s): message bursts cause one
  write, and a crash loses at most the last couple of seconds. `flush()` forces
  a write on graceful shutdown (Ctrl+C).
* Writes are **atomic** (temp file + `os.replace`) — the file is never
  half-written, and a corrupt file is quarantined to `.bak` instead of crashing.
* Each channel keeps the **last N messages** (default 40) — a rolling window
  that bounds the token cost of every request.
* Channel **IDs are the keys**, so servers, threads and DMs all get independent
  memories that never bleed into each other.
* A per-channel `asyncio.Lock` serialises mutations; a per-channel
  `asyncio.Semaphore` in `bot.py` additionally serialises AI calls so two users
  chatting at once never interleave request/response pairs.

### Dynamic system prompts (`prompts.py` + `bot.py`)

There is **exactly one mechanism**: writing a string into the channel's storage
bucket via `ConversationStore.set_system_prompt()`.

1. `.promt <text>` stores your text (replaces anything previously active).
2. `.promt-fast` / `.promt-brain` store the matching constant from `prompts.py`
   through the *same* method — so presets and custom prompts are one code path.
3. Every AI request loads `get_system_prompt(channel_id) or DEFAULT_SYSTEM_PROMPT`
   and sends it as the persona (OpenAI: first `system` message; Gemini:
   `system_instruction`).
4. Because the prompt is persisted, the chosen persona **survives restarts**;
   `.clear` wipes prompt + history together, returning the channel to default.

### AI access & error handling (`ai_client.py`)

* `AIProvider` is a small protocol; `OpenAIProvider` and `GeminiProvider`
  implement it with their official SDKs (`openai`, `google-genai`), both fully
  async. Switching provider = one `.env` line, zero code changes.
* All SDK exceptions are normalised into two bot-level errors:
  **transient** (network, timeout, 429, 5xx → retried with exponential backoff:
  3 attempts, 1 s then 3 s, wrapped in a hard `asyncio.wait_for` ceiling) and
  **permanent** (bad key/model → surfaced immediately with an actionable message).
* Discord-side failures (rate limits, missing permissions on a channel) are
  caught per-message and logged — one bad channel can never crash the loop.
* Missing/misconfigured keys are detected **before** login by `config.py`,
  which reports all problems in a single bullet list.

### Free chat & terminal console

* **Free chat** — plain messages (no prefix) go to the AI in every channel by
  default. Narrow it per channel with `.chat off` / `.chat on` (`.chat` shows
  the current state); DMs and @mentions always work regardless of the flag.
* **Console** — the terminal running the bot accepts management commands
  while it works:

  | Console command | Effect |
  |---|---|
  | `nick <name>` | Change the bot's nickname on all servers |
  | `activity playing/listening/watching/competing <text>` | Set the activity |
  | `activity clear` | Remove the activity |
  | `status online\|idle\|dnd\|invisible` | Set the presence status |
  | `stats` | Servers, latency, channels with history, message counts |
  | `flush` | Force-save history to disk right now |
  | `stop` / `exit` / `quit` | Save everything and shut down |
  | `help` | List these commands |

  Activity/status changes are not persisted — they reset on restart.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PrivilegedIntentsRequired` on login | Enable **Message Content Intent** in the Developer Portal (Bot tab), restart. |
| `Discord rejected the token` | Token reset or copied wrong — Bot tab → Reset Token → update `.env`. |
| 🛑 "AI backend rejected the request" | Wrong API key or model name. Check `AI_PROVIDER`, the matching key, and `AI_MODEL`; details in `logs/bot.log`. |
| 🟠 "service seems down" | Rate limit / quota / outage. Free tiers are tight — wait, or switch `AI_PROVIDER`. |
| Bot online but silent in chat | It was probably not invited with *Send Messages* permission in that channel, or you're messaging in a channel where it lacks *View Channel*. |
| History gone after restart | You deleted `data/` or ran the bot from a different working directory (history path is relative to `config.py`). |
| Want a fresh start everywhere | Stop the bot, delete `data/history.json`, start again. |

Logs rotate in `logs/bot.log` (2 MB × 3 backups) — always check there first.
