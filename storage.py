"""
storage.py — persistent per-channel conversation memory.

Design
------
* One JSON file on disk (`data/history.json`) holds everything:

    {
      "channels": {
        "123456789012345678": {                 # Discord channel id (string)
          "system_prompt": "You are a pirate.", # or null
          "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello!"}
          ]
        }
      }
    }

* A full in-memory mirror is loaded once at startup; every read/write hits
  memory only, so Discord event handlers are never blocked by disk I/O.
* Every mutation schedules a *debounced* background save (default 2 s).
  Rapid bursts of messages therefore cause one write instead of dozens, and
  a crash can lose at most the last couple of seconds of chat. `flush()`
  forces an immediate write (used on graceful shutdown).
* Writes are atomic (temp file + os.replace), so the file is never left
  half-written even if the process dies mid-save.
* Each channel keeps at most `max_messages` entries — the oldest are dropped
  (rolling window). This bounds the token cost of every AI request.
* A per-channel asyncio.Lock serialises mutations from concurrent command
  handlers. The file itself is only touched by the single save task, so no
  extra locking is needed for I/O.
* DM channels work automatically: a DMChannel has its own id, so its
  conversation is stored independently of any server channel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("aibot.storage")

Role = str                       # "user" | "assistant"
Message = dict                   # {"role": Role, "content": str}


class ConversationStore:
    """In-memory mirror + debounced atomic persistence of channel memories."""

    def __init__(self, path: Path, max_messages: int = 40, save_delay: float = 2.0) -> None:
        self._path = path
        self._max_messages = max(2, max_messages)
        self._save_delay = save_delay
        self._data: dict = {"channels": {}}
        self._locks: dict[int, asyncio.Lock] = {}
        self._save_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Load the JSON file into memory. A corrupt file is moved to .bak."""
        if not self._path.exists():
            log.info("No history file at %s — starting fresh.", self._path)
            return
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("channels"), dict):
                raise ValueError("unexpected file shape")
            self._data = loaded
            log.info("Loaded history for %d channel(s).", len(self._data["channels"]))
        except (OSError, ValueError) as exc:
            backup = self._path.with_name(self._path.name + ".bak")
            log.error("History file is corrupt (%s). Renaming it to %s and starting fresh.", exc, backup)
            try:
                self._path.replace(backup)
            except OSError:
                pass
            self._data = {"channels": {}}

    async def flush(self) -> None:
        """Force an immediate save (used on graceful shutdown / in tests)."""
        if self._save_task is not None and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = None
        await asyncio.to_thread(self._write_file)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[channel_id] = lock
        return lock

    def _bucket(self, channel_id: int, create: bool = True) -> dict | None:
        """Return (and optionally create) the storage bucket for a channel."""
        bucket = self._data["channels"].get(str(channel_id))
        if bucket is None and create:
            bucket = {"system_prompt": None, "messages": []}
            self._data["channels"][str(channel_id)] = bucket
        if bucket is not None:
            # Defensive defaults in case the file was hand-edited.
            bucket.setdefault("system_prompt", None)
            bucket.setdefault("messages", [])
            bucket.setdefault("free_chat", True)
        return bucket

    def _schedule_save(self) -> None:
        """(Re)start the debounced save timer. Called after every mutation."""
        if self._save_task is not None and not self._save_task.done():
            self._save_task.cancel()
        self._save_task = asyncio.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self._save_delay)
            await asyncio.to_thread(self._write_file)
        except asyncio.CancelledError:
            pass  # replaced by a newer save, or flushed explicitly
        finally:
            # Only clear the pointer if a newer task has not already replaced it.
            if self._save_task is task:
                self._save_task = None

    def _write_file(self) -> None:
        """Atomic write: dump to a temp file, then os.replace over the target."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("Failed to save history file: %s", exc)

    # ------------------------------------------------------------------
    # Public API (all async — safe to call from any command handler)
    # ------------------------------------------------------------------
    async def get_messages(self, channel_id: int) -> list[Message]:
        """Return a *copy* of the channel's message list (oldest first)."""
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            return list(bucket["messages"])

    async def get_system_prompt(self, channel_id: int) -> str | None:
        """Return the channel's system prompt, or None if never set."""
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            return bucket["system_prompt"]

    async def append(self, channel_id: int, role: Role, content: str) -> None:
        """
        Append one conversation turn and trim the history to the configured
        maximum (rolling window — the oldest messages fall off first).
        """
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            bucket["messages"].append({"role": role, "content": content})
            overflow = len(bucket["messages"]) - self._max_messages
            if overflow > 0:
                del bucket["messages"][:overflow]
            self._schedule_save()

    async def set_system_prompt(self, channel_id: int, prompt: str | None) -> None:
        """
        Set (or clear, with None) the channel's system prompt.

        This is THE mechanism behind dynamic persona switching: `.promt`,
        `.promt-fast` and `.promt-brain` all funnel into this method, and the
        stored prompt is injected into every subsequent AI request.
        """
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            bucket["system_prompt"] = prompt
            self._schedule_save()

    async def get_free_chat(self, channel_id: int) -> bool:
        """
        Free-chat mode: plain messages without any prefix trigger the AI.
        Enabled by default in every channel (missing flag == enabled, so old
        history files upgrade transparently). DMs/mentions bypass this flag.

        Read-only on purpose: never creates a bucket, so channels where
        nobody has ever used the bot do not accumulate empty entries.
        """
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id, create=False)
            if bucket is None:
                return True
            return bool(bucket["free_chat"])

    async def set_free_chat(self, channel_id: int, enabled: bool) -> None:
        """Enable/disable free-chat mode for a channel (persisted, `.chat`)."""
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            bucket["free_chat"] = bool(enabled)
            self._schedule_save()

    async def clear(self, channel_id: int) -> None:
        """Wipe BOTH the history and the system prompt for the channel."""
        async with self._lock(channel_id):
            self._data["channels"].pop(str(channel_id), None)
            self._schedule_save()

    async def stats(self, channel_id: int) -> tuple[int, str | None]:
        """(message_count, system_prompt) — used by `.clear` and `.promt`."""
        async with self._lock(channel_id):
            bucket = self._bucket(channel_id)
            return len(bucket["messages"]), bucket["system_prompt"]

    def channel_stats_sync(self) -> list[tuple[int, int]]:
        """
        [(channel_id, message_count)] — synchronous, read-only snapshot used
        by the bot's console `stats` command (console code must not await).
        """
        return [
            (int(cid), len(bucket.get("messages", [])))
            for cid, bucket in self._data["channels"].items()
            if bucket.get("messages")
        ]
