"""
bot.py — entry point for the AI Discord bot.

A prefix-command chat bot (default prefix ".") with per-channel conversation
memory persisted to disk and a swappable AI backend (OpenAI, Gemini, or any
OpenAI-compatible service like B.AI/GLM).

Module map
----------
    config.py    loads + validates environment variables
    prompts.py   system-prompt presets (fast / brain / default)
    storage.py   per-channel history + system prompt, persisted to JSON
    ai_client.py provider-agnostic async AI facade with retry
    bot.py       discord.py bot: commands, embeds, error handling  <- you are here

IMPORTANT (discord.py specifics)
--------------------------------
Prefix commands are defined in `AICommands(commands.Cog)` below, NOT as
methods of the Bot subclass. In discord.py (verified on 2.7.x), methods
decorated with @commands.command() on a Bot subclass are NOT auto-registered,
while methods inside a Cog ARE (collected by CogMeta). Defining them in a
Cog is the canonical pattern; event handlers (on_message/on_command_error)
stay on the Bot, which is fully supported.

Run from the project root:
    python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Coroutine

import discord
from discord.ext import commands

from ai_client import (
    AIProvider,
    AIProviderError,
    AIPermanentError,
    build_provider,
    chat_with_retries,
)
from config import BASE_DIR, Config, ConfigError, load_config
from prompts import BRAIN_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT, FAST_SYSTEM_PROMPT
from storage import ConversationStore

log = logging.getLogger("aibot")

EMBED_COLOR = 0x5865F2   # Discord "blurple"
OK_COLOR = 0x57F287      # green
WARN_COLOR = 0xFEE75C    # yellow

CHAT_COOLDOWN_SECONDS = 3.0    # per-user minimum delay between AI requests
DISCORD_MESSAGE_LIMIT = 2000   # hard platform limit per normal message


def chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """
    Split `text` into Discord-safe pieces of at most `limit` characters.

    Break points are preferred in this order: newline -> space -> hard cut,
    so words and code lines are not split in half wherever possible.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 4:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 4:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


class BotConsole:
    """
    Interactive management console running in the terminal window where the
    bot was started. Typing there controls the live bot without Discord:

        help                              — list console commands
        nick <name>                       — change the bot's nickname (all servers)
        activity playing|listening|watching|competing <text>
                                          — set the bot's activity
        activity clear                    — remove the activity
        status online|idle|dnd|invisible  — set the presence status
        stats                             — servers / channels / message counts
        stop | exit | quit                — save everything and shut down

    Implementation notes:
      * stdin has no async API, so a dedicated daemon thread blocks on
        readline(); daemon=True means the thread can never hang shutdown.
      * Discord mutations must happen on the bot's event loop — done with
        asyncio.run_coroutine_threadsafe(), the only thread-safe bridge.
    """

    HELP = (
        "\n=== Console commands (type here, press Enter) ===\n"
        "  help                              show this list\n"
        "  nick <name>                       change the bot's nickname\n"
        "  activity playing <text>           'Playing <text>'\n"
        "  activity listening <text>         'Listening to <text>'\n"
        "  activity watching <text>          'Watching <text>'\n"
        "  activity competing <text>         'Competing in <text>'\n"
        "  activity clear                    remove the activity\n"
        "  status online|idle|dnd|invisible  set presence status\n"
        "  stats                             servers / channels / message counts\n"
        "  stop  (or exit / quit)            save everything and shut down\n"
        "=================================================="
    )

    _ACTIVITY_TYPES = {
        "playing": (discord.ActivityType.playing, "Playing {}"),
        "listening": (discord.ActivityType.listening, "Listening to {}"),
        "watching": (discord.ActivityType.watching, "Watching {}"),
        "competing": (discord.ActivityType.competing, "Competing in {}"),
    }
    _STATUSES = {
        "online": discord.Status.online,
        "idle": discord.Status.idle,
        "dnd": discord.Status.dnd,
        "invisible": discord.Status.invisible,
        "offline": discord.Status.invisible,   # alias
    }

    def __init__(self, bot: "AIBot") -> None:
        self._bot = bot
        self._loop = asyncio.get_running_loop()
        self._activity: discord.Activity | None = None   # last applied activity
        self._thread = threading.Thread(
            target=self._run, name="bot-console", daemon=True
        )

    def start(self) -> None:
        """Start the console thread (called from setup_hook)."""
        self._thread.start()

    # ------------------------------------------------------------------
    # Thread side (blocking stdin)
    # ------------------------------------------------------------------
    def _run(self) -> None:
        print(self.HELP)
        while True:
            try:
                line = input("console> ").strip()
            except (EOFError, KeyboardInterrupt):
                # stdin closed (e.g. process piped) — stop reading quietly.
                return
            if not line:
                continue
            try:
                if not self._dispatch(line):
                    return  # 'stop' requested
            except Exception:  # noqa: BLE001 — console must never kill the bot
                log.exception("Console command failed: %s", line)

    def _dispatch(self, line: str) -> bool:
        """Parse one console line. Returns False when the bot should stop."""
        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("stop", "exit", "quit"):
            print("Shutting down — flushing history to disk…")
            self._run_coro(self._bot.close(), timeout=15.0)
            os._exit(0)  # guaranteed exit even if something hangs on close

        if cmd in ("help", "?"):
            print(self.HELP)
        elif cmd == "nick":
            if arg:
                self._run_coro(self._set_nick(arg))
            else:
                print("Usage: nick <new name>")
        elif cmd == "activity":
            self._activity_cmd(arg)
        elif cmd == "status":
            self._status_cmd(arg.lower())
        elif cmd == "stats":
            self._run_coro(self._stats())
        else:
            print(f"Unknown console command {cmd!r} — type 'help'.")
        return True

    def _run_coro(self, coro: Coroutine[Any, Any, Any], timeout: float = 10.0) -> None:
        """Schedule a coroutine on the bot's loop and surface its errors."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            log.error("Console command timed out after %.0fs.", timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"Command failed: {exc}")

    # ------------------------------------------------------------------
    # Command implementations (coroutines run on the bot's event loop)
    # ------------------------------------------------------------------
    async def _set_nick(self, name: str) -> None:
        """
        Nicknames are per-guild in Discord, so apply the name in every server
        the bot is in. Needs the 'Change Nickname' permission there.
        """
        if not self._bot.guilds:
            print("The bot is not in any server yet.")
            return
        ok, failed = 0, 0
        for guild in self._bot.guilds:
            try:
                await guild.me.edit(nick=name)
                ok += 1
            except discord.Forbidden:
                failed += 1
                log.warning("No 'Change Nickname' permission in %s.", guild.name)
            except discord.HTTPException as exc:
                failed += 1
                log.warning("Nickname change failed in %s: %s", guild.name, exc)
        print(f"Nickname set to {name!r} in {ok} server(s)"
              + (f", failed in {failed}" if failed else "") + ".")

    def _activity_cmd(self, arg: str) -> None:
        """`activity <kind> <text>` or `activity clear`."""
        if not arg or arg.lower() == "clear":
            self._activity = None
            self._run_coro(self._apply_presence())
            return
        parts = arg.split(maxsplit=1)
        kind = parts[0].lower()
        text = parts[1].strip() if len(parts) > 1 else ""
        entry = self._ACTIVITY_TYPES.get(kind)
        if entry is None or not text:
            print("Usage: activity <playing|listening|watching|competing> <text>"
                  "  |  activity clear")
            return
        activity_type, template = entry
        self._activity = discord.Activity(
            type=activity_type, name=template.format(text)
        )
        self._run_coro(self._apply_presence())

    def _status_cmd(self, arg: str) -> None:
        """`status online|idle|dnd|invisible` (keeps the current activity)."""
        status = self._STATUSES.get(arg)
        if status is None:
            print("Usage: status <online|idle|dnd|invisible>")
            return
        self._run_coro(self._apply_presence(status=status))

    async def _apply_presence(self, status: discord.Status | None = None) -> None:
        # change_presence needs the gateway; wrap in case it is not ready yet.
        try:
            await self._bot.change_presence(activity=self._activity, status=status)
            print("Presence updated."
                  if self._activity or status else "Activity cleared.")
        except discord.HTTPException as exc:
            print(f"Discord rejected the presence change: {exc}")

    async def _stats(self) -> None:
        rows = self._bot.store.channel_stats_sync()
        total = sum(count for _, count in rows)
        print(
            f"Servers: {len(self._bot.guilds)} | latency: "
            f"{self._bot.latency * 1000:.0f} ms | channels with history: "
            f"{len(rows)} | stored messages: {total}"
        )
        for cid, count in sorted(rows, key=lambda r: r[1], reverse=True)[:10]:
            print(f"  channel {cid}: {count} message(s)")


class AIBot(commands.Bot):
    """
    The bot object.

    Holds three collaborators (see each module's docstring for details):
      * self.store  — ConversationStore: per-channel history + system prompt
      * self.ai     — AIProvider adapter (OpenAI / Gemini / custom)
      * self.config — validated configuration from .env

    Prefix commands live in the AICommands Cog (see module docstring for
    why); this class owns event routing, the free-chat pipeline and errors.
    """

    def __init__(self, config: Config) -> None:
        # The MESSAGE CONTENT intent is required to read prefix commands and
        # normal chat. It must ALSO be switched on in the Discord Developer
        # Portal (Bot -> Privileged Gateway Intents), otherwise login fails
        # with discord.errors.PrivilegedIntentsRequired.
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=config.command_prefix,
            help_command=None,          # replaced by our own `.help` command
            intents=intents,
            case_insensitive=True,      # `.PROMT` works like `.promt`
        )

        self.config = config
        # The store keeps everything in memory and persists to disk with a
        # debounce — see storage.py for the full design explanation.
        self.store = ConversationStore(
            config.history_file, max_messages=config.max_history_messages
        )
        self.ai: AIProvider = build_provider(config)

        # One semaphore per channel: AI calls for the same channel are
        # serialised so two users chatting simultaneously never interleave
        # their request/response pairs.
        self._channel_locks: dict[int, asyncio.Semaphore] = {}
        # Per-user chat cooldown — {user_id: monotonic timestamp of last use}.
        self._last_chat: dict[int, float] = {}
        # Created in setup_hook (needs a running loop); see BotConsole.
        self._console: BotConsole | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def setup_hook(self) -> None:
        # Called once before the gateway connects: load the saved history so
        # the bot "remembers" previous sessions from the very first message,
        # then register the prefix commands (the Cog) and the console UI.
        self.store.load()
        await self.add_cog(AICommands(self))
        # Terminal interface: while the bot runs, you can type commands
        # (nick, activity, status, stats, stop…) straight into the console.
        self._console = BotConsole(self)
        self._console.start()

    async def close(self) -> None:
        # Flush pending debounced writes before the event loop stops.
        await self.store.flush()
        await super().close()

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Logged in as %s (id=%s) — serving %d guild(s).",
            user, getattr(user, "id", "?"), len(self.guilds),
        )
        # Defensive visibility: if this list is ever empty, commands are not
        # registered and the bot would silently ignore the prefix.
        log.info("Registered commands: %s", ", ".join(sorted(c.name for c in self.commands)))
        await self.change_presence(
            activity=discord.Game(name=f"{self.config.command_prefix}help — chat with me")
        )

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        """
        Route one incoming message.

        1. Bot/webhook authors are ignored (prevents reply loops).
        2. Messages starting with the prefix go to the command system.
        3. Plain text in DMs or direct @mentions of the bot is treated as a
           chat request — a convenience path that reuses `.ask`'s logic.
        """
        if message.author.bot or message.webhook_id:
            return

        content = message.content or ""

        # ---- Free chat / DM / mention path --------------------------------
        # A message triggers the AI without any prefix when:
        #   * it is a DM, or
        #   * it directly @mentions the bot, or
        #   * the channel has free-chat mode enabled (`.chat on`, default).
        if not content.startswith(self.config.command_prefix):
            is_dm = message.guild is None
            mentioned = self.user is not None and self.user in message.mentions
            free_chat = (
                is_dm or mentioned
                or await self.store.get_free_chat(message.channel.id)
            )
            if free_chat and content.strip():
                question = self._strip_mention(content)
                if question:
                    try:
                        await self._handle_chat(message, question)
                    except Exception:  # noqa: BLE001 — never let an event die loudly
                        log.exception("Unexpected failure handling chat in channel %s",
                                      message.channel.id)
                        try:
                            await message.reply("💥 Something went wrong. Details are in the bot log.")
                        except discord.HTTPException:
                            pass
                    return

        # Normal prefix-command processing (.promt, .clear, .help, .ask…).
        await self.process_commands(message)

    def _strip_mention(self, content: str) -> str:
        """Remove the bot's own mention tokens, leaving the user's text."""
        if self.user is None:
            return content.strip()
        for token in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            content = content.replace(token, "")
        return content.strip()

    # ------------------------------------------------------------------
    # Rate limiting helpers
    # ------------------------------------------------------------------
    def _chat_rate_limited(self, user_id: int) -> float:
        """Seconds remaining on this user's chat cooldown (0.0 = allowed)."""
        now = time.monotonic()
        remaining = CHAT_COOLDOWN_SECONDS - (now - self._last_chat.get(user_id, 0.0))
        return max(0.0, remaining)

    def _mark_chat(self, user_id: int) -> None:
        self._last_chat[user_id] = time.monotonic()

    # ------------------------------------------------------------------
    # Core chat handler (used by `.ask`, DMs and @mentions)
    # ------------------------------------------------------------------
    async def _handle_chat(self, message: discord.Message, question: str) -> None:
        """
        The single code path through which every AI conversation flows.

        History strategy (see storage.py for the persistence side):
          1. Snapshot the channel history and append the user's question
             *in memory only* — that snapshot is what the AI sees.
          2. Only when the AI call succeeds are BOTH turns persisted, so a
             failed request never leaves a dangling unanswered question in
             the context.
          3. The channel's system prompt (persona) is injected on every
             request; DEFAULT_SYSTEM_PROMPT applies when none was set.
        """
        cfg = self.config
        channel_id = message.channel.id

        if len(question) > cfg.max_input_chars:
            await message.reply(
                f"⚠️ Message too long ({len(question)} chars, max {cfg.max_input_chars})."
            )
            return

        remaining = self._chat_rate_limited(message.author.id)
        if remaining > 0:
            await message.reply(f"⏳ One request at a time — try again in {remaining:.0f}s.")
            return
        self._mark_chat(message.author.id)

        semaphore = self._channel_locks.setdefault(channel_id, asyncio.Semaphore(1))

        async with message.channel.typing():          # "Bot is typing…" indicator
            async with semaphore:
                history = await self.store.get_messages(channel_id)
                request_history = history + [{"role": "user", "content": question}]
                system_prompt = (
                    await self.store.get_system_prompt(channel_id) or DEFAULT_SYSTEM_PROMPT
                )

                try:
                    answer = await chat_with_retries(
                        self.ai, system_prompt, request_history, timeout=cfg.request_timeout,
                    )
                except AIPermanentError as exc:
                    log.error("AI rejected request in channel %s: %s", channel_id, exc)
                    await message.reply(
                        "🛑 The AI backend rejected the request. This usually means a wrong "
                        "API key or model name — check the bot's configuration and logs."
                    )
                    return
                except AIProviderError as exc:  # transient, retries exhausted
                    log.error("AI unavailable after retries in channel %s: %s", channel_id, exc)
                    await message.reply(
                        "🟠 The AI service seems down or overloaded right now. "
                        "Please try again in a minute."
                    )
                    return

                # Persist the completed exchange (user turn + assistant turn).
                await self.store.append(channel_id, "user", question)
                await self.store.append(channel_id, "assistant", answer)

            # Deliver outside the channel lock so the next queued user is not
            # blocked by our network sends.
            try:
                chunks = chunk_message(answer)
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)
            except discord.HTTPException as exc:
                # Covers Discord rate limits (429) and permission problems.
                log.warning("Could not deliver AI answer in channel %s: %s", channel_id, exc)

    # ------------------------------------------------------------------
    # Command error handling
    # ------------------------------------------------------------------
    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Friendly handling for every command-level failure."""
        # CommandInvokeError wraps whatever the command body raised — unwrap
        # it so we can react to the real exception.
        if isinstance(error, commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, commands.CommandNotFound):
            # Silently ignore unknown commands — no spam in busy servers.
            log.debug("Unknown command: %s", ctx.message.content)
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"⚠️ Missing argument `{error.param.name}`. "
                f"See `{self.config.command_prefix}help` for usage."
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ This command is on cooldown — retry in {error.retry_after:.1f}s.")
            return
        if isinstance(error, commands.CheckFailure):
            await ctx.send("🚫 You can't use this command here.")
            return

        # Anything unexpected: log the full traceback, show a generic reply.
        channel_name = getattr(ctx.channel, "name", "DM")
        log.error("Unhandled command error in #%s", channel_name, exc_info=error)
        try:
            await ctx.send("💥 Something went wrong while running that command. "
                           "The details are in the bot's log.")
        except discord.HTTPException:
            pass


class AICommands(commands.Cog):
    """
    All prefix commands, grouped into logical sections in `.help`.

    A Cog (not methods on the Bot subclass) is used because discord.py only
    auto-registers @commands.command() methods inside Cogs — see the module
    docstring. The Cog reaches the shared services via `self.bot`.
    """

    def __init__(self, bot: AIBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Helpers shared by several commands
    # ------------------------------------------------------------------
    @staticmethod
    def _channel_id(ctx: commands.Context) -> int:
        # Works for text channels, threads and DMs alike: every one of them
        # has a unique channel id, so memories never bleed across channels.
        return ctx.channel.id

    async def _maybe_send_long(self, ctx: commands.Context, text: str) -> None:
        """Split replies at 2000 chars (Discord's hard limit), respecting fences."""
        LIMIT = 2000
        if len(text) <= LIMIT:
            await ctx.send(text)
            return
        # Naive but fence-aware split: never cut inside a ``` block if we can
        # avoid it, because an unclosed fence would swallow the rest of the page.
        parts: list[str] = []
        current = ""
        in_fence = False
        for line in text.splitlines(keepends=True):
            if len(current) + len(line) > LIMIT - (6 if in_fence else 0):
                if in_fence:
                    current += "```\n"
                parts.append(current)
                current = "```\n" if in_fence else ""
            current += line
            if line.strip().startswith("```"):
                in_fence = not in_fence
        if current.strip():
            parts.append(current)
        for part in parts[:10]:  # absolute cap: 10 pages per reply
            await ctx.send(part)

    # ------------------------------------------------------------------
    # Commands — AI Mode Configuration
    # ------------------------------------------------------------------
    @commands.command(name="promt")
    async def promt(self, ctx: commands.Context, *, text: str | None = None) -> None:
        """
        `.promt <text>` — set a custom system prompt (persona/rules) for this
        channel. `.promt` with no argument shows the currently active one.

        Setting a prompt REPLACES whatever was active (custom or preset):
        there is exactly one prompt per channel, stored via
        ConversationStore.set_system_prompt() and injected into every
        subsequent AI request. Conversation history is deliberately kept —
        the new persona governs answers from now on; `.clear` resets both.
        """
        channel_id = self._channel_id(ctx)

        if text is None or not text.strip():
            active = await self.bot.store.get_system_prompt(channel_id)
            embed = discord.Embed(
                title="💬 Current system prompt",
                description=(active or "_None set — the default persona is active._")[:2000],
                color=EMBED_COLOR if active else discord.Color.dark_grey(),
            )
            embed.set_footer(text=f"Set a new one with {self.bot.config.command_prefix}promt <text>")
            await ctx.send(embed=embed)
            return

        text = text.strip()
        if len(text) > self.bot.config.max_prompt_chars:
            await ctx.send(
                f"⚠️ Prompt too long ({len(text)} chars, max {self.bot.config.max_prompt_chars})."
            )
            return

        await self.bot.store.set_system_prompt(channel_id, text)
        log.info("Channel %s: custom prompt set (%d chars).", channel_id, len(text))
        embed = discord.Embed(
            title="✅ System prompt updated for this channel",
            description=text[:2000],
            color=OK_COLOR,
        )
        embed.set_footer(
            text=f"Applies to the next message. Use {self.bot.config.command_prefix}clear to wipe it and the history."
        )
        await ctx.send(embed=embed)

    @commands.command(name="promt-fast")
    async def promt_fast(self, ctx: commands.Context) -> None:
        """
        `.promt-fast` — switch this channel to instant mode.

        Implementation: store the FAST_SYSTEM_PROMPT constant through the
        same set_system_prompt() path as a custom `.promt`. The prompt is
        persisted, so the chosen mode survives bot restarts.
        """
        await self.bot.store.set_system_prompt(self._channel_id(ctx), FAST_SYSTEM_PROMPT)
        log.info("Channel %s: mode -> FAST.", self._channel_id(ctx))
        embed = discord.Embed(
            title="⚡ Fast mode enabled",
            description=FAST_SYSTEM_PROMPT,
            color=WARN_COLOR,
        )
        embed.set_footer(
            text=f"History kept. Use {self.bot.config.command_prefix}clear to also wipe the conversation."
        )
        await ctx.send(embed=embed)

    @commands.command(name="promt-brain")
    async def promt_brain(self, ctx: commands.Context) -> None:
        """`.promt-brain` — switch this channel to deep-thinking mode."""
        await self.bot.store.set_system_prompt(self._channel_id(ctx), BRAIN_SYSTEM_PROMPT)
        log.info("Channel %s: mode -> BRAIN.", self._channel_id(ctx))
        embed = discord.Embed(
            title="🧠 Deep-thinking mode enabled",
            description=BRAIN_SYSTEM_PROMPT,
            color=0xEB459E,
        )
        embed.set_footer(
            text=f"History kept. Use {self.bot.config.command_prefix}clear to also wipe the conversation."
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Commands — Memory & Utility
    # ------------------------------------------------------------------
    @commands.command(name="chat")
    async def chat_mode(self, ctx: commands.Context, mode: str | None = None) -> None:
        """
        `.chat` / `.chat status` — show whether free-chat is on in this channel;
        `.chat on` / `.chat off` — toggle replying to plain (prefix-less) messages.

        DMs and @mentions always reach the AI regardless of this flag.
        """
        channel_id = self._channel_id(ctx)
        p = self.bot.config.command_prefix

        if mode is None or mode.lower() == "status":
            enabled = await self.bot.store.get_free_chat(channel_id)
            embed = discord.Embed(
                title="💬 Free chat mode",
                description=(
                    f"**Enabled** — every plain message here goes to the AI.\n"
                    f"Turn off with `{p}chat off`."
                    if enabled else
                    f"**Disabled** — the AI answers only via `{p}ask <text>`, "
                    f"DMs or @mentions.\nTurn on with `{p}chat on`."
                ),
                color=OK_COLOR if enabled else discord.Color.dark_grey(),
            )
            await ctx.send(embed=embed)
            return

        mode = mode.lower().strip()
        if mode in ("on", "enable", "вкл"):
            await self.bot.store.set_free_chat(channel_id, True)
            log.info("Channel %s: free chat -> ON.", channel_id)
            await ctx.send(embed=discord.Embed(
                title="💬 Free chat: ON",
                description="Now every plain message in this channel goes to the AI. "
                            f"Commands still work normally; `{p}chat off` disables this.",
                color=OK_COLOR,
            ))
        elif mode in ("off", "disable", "выкл"):
            await self.bot.store.set_free_chat(channel_id, False)
            log.info("Channel %s: free chat -> OFF.", channel_id)
            await ctx.send(embed=discord.Embed(
                title="💬 Free chat: OFF",
                description=f"The AI now answers only via `{p}ask <text>`, DMs or @mentions.",
                color=WARN_COLOR,
            ))
        else:
            await ctx.send(f"Usage: `{p}chat` | `{p}chat on` | `{p}chat off`")

    @commands.command(name="clear")
    async def clear_cmd(self, ctx: commands.Context) -> None:
        """
        `.clear` — forget everything about this channel.

        Wipes BOTH pieces of stored state (message history and system
        prompt) in one atomic call; the channel falls back to the default
        persona on its next message.
        """
        channel_id = self._channel_id(ctx)
        count, prompt = await self.bot.store.stats(channel_id)
        await self.bot.store.clear(channel_id)
        log.info("Channel %s: cleared (%d messages, prompt %s).",
                 channel_id, count, "removed" if prompt else "was default")

        if count == 0 and prompt is None:
            await ctx.send("🧹 Nothing to clear — this channel was already clean.")
            return
        embed = discord.Embed(
            title="🧹 Channel memory cleared",
            description=(
                f"Removed **{count}** stored message(s) and the active system prompt "
                f"for this channel."
            ),
            color=OK_COLOR,
        )
        await ctx.send(embed=embed)

    @commands.command(name="ask", aliases=["a"])
    async def ask(self, ctx: commands.Context, *, question: str) -> None:
        """`.ask <text>` — chat with the AI using this channel's memory."""
        await self.bot._handle_chat(ctx.message, question.strip())

    @commands.command(name="help")
    async def help_cmd(self, ctx: commands.Context) -> None:
        """`.help` — show the formatted command overview."""
        p = self.bot.config.command_prefix
        embed = discord.Embed(
            title="🤖 AI Assistant — Command Overview",
            description=(
                "A chat bot with per-channel memory and switchable personas.\n"
                f"Ask in any channel with `{p}ask <text>`, by mentioning me, "
                "or by sending me a DM."
            ),
            color=EMBED_COLOR,
        )
        embed.add_field(
            name="💬 AI Chat",
            value=(
                f"`{p}ask <text>` — talk to the AI (alias: `{p}a`)\n"
                f"`just write <text>` — free chat without a command (on by default)\n"
                f"`{p}chat` — show / toggle free chat for this channel (`on`/`off`)\n"
                f"`@mention me` / DM — same as `{p}ask`\n"
                f"The AI remembers the last {self.bot.config.max_history_messages} messages per channel."
            ),
            inline=False,
        )
        embed.add_field(
            name="🧠 AI Mode Configuration",
            value=(
                f"`{p}promt <text>` — set a custom system prompt for this channel\n"
                f"`{p}promt` — show the current system prompt\n"
                f"`{p}promt-fast` — ⚡ instant, strict, concise answers\n"
                f"`{p}promt-brain` — 🧠 deep, step-by-step analysis\n"
                f"`{p}clear` — remove the active prompt and the history"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧹 Utility",
            value=(
                f"`{p}clear` — wipe history + system prompt for this channel\n"
                f"`{p}help` — show this overview"
            ),
            inline=False,
        )
        embed.set_footer(
            text=f"Provider: {self.bot.config.ai_provider} · model: {self.bot.config.ai_model}"
        )
        await ctx.send(embed=embed)


def setup_logging(log_dir: Path) -> None:
    """
    Console + rotating file logging (logs/bot.log, 2 MB x 3 backups) for both
    our code and discord.py itself (whose rate-limit warnings are useful).
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), file_handler],
    )


def main() -> int:
    setup_logging(BASE_DIR / "logs")

    try:
        config = load_config()
    except ConfigError as exc:
        log.error("%s", exc)
        log.error("Fix the issues above and run the bot again.")
        return 1

    log.info(
        "Config OK — provider=%s model=%s prefix=%r history=%s",
        config.ai_provider, config.ai_model, config.command_prefix, config.history_file,
    )

    try:
        bot = AIBot(config)
    except ConfigError as exc:  # e.g. the selected provider's SDK is missing
        log.error("%s", exc)
        return 1

    try:
        # log_handler=None -> discord.py logs flow through our basicConfig.
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the token. Regenerate it in the Developer Portal "
            "(Bot -> Reset Token) and update DISCORD_TOKEN in .env."
        )
        return 1
    except discord.PrivilegedIntentsRequired:
        log.error(
            "The 'Message Content Intent' is not enabled. Open the Developer Portal "
            "-> your app -> Bot -> Privileged Gateway Intents, enable 'MESSAGE CONTENT "
            "INTENT', then restart the bot."
        )
        return 1
    except KeyboardInterrupt:
        log.info("Interrupted by user (Ctrl+C) — goodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
