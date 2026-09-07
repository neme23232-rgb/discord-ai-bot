"""
Quick smoke tests for ConversationStore — no external dependencies needed
(only the standard library), so they run before any pip install.

Run directly:   python tests/test_storage.py
Or with pytest: pytest tests/test_storage.py -q
"""
import asyncio
import shutil
import sys
from pathlib import Path

# Make the project root importable when run from any working directory.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage import ConversationStore  # noqa: E402

# Windows consoles often default to a legacy codepage (e.g. cp1251) that
# cannot render emoji/unicode — force UTF-8 output so the summary always prints.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Workspace-local scratch dir (kept out of version control by .gitignore).
TMP_DIR = ROOT / "data" / "_test_tmp"


async def main() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TMP_DIR / "history.json"

    # --- 1. append + prompt + persistence -----------------------------
    store = ConversationStore(path, max_messages=4, save_delay=0.05)
    store.load()

    await store.append(100, "user", "hello")
    await store.append(100, "assistant", "hi there")
    await store.set_system_prompt(100, "You are a pirate.")
    await store.append(100, "user", "again")

    msgs = await store.get_messages(100)
    assert msgs == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "again"},
    ], msgs
    assert await store.get_system_prompt(100) == "You are a pirate."

    await store.flush()
    assert path.exists(), "history file should exist after flush"

    # --- 2. reload from disk (simulates a bot restart) -----------------
    store2 = ConversationStore(path, max_messages=4)
    store2.load()
    assert await store2.get_system_prompt(100) == "You are a pirate."
    assert len(await store2.get_messages(100)) == 3

    # --- 3. rolling window trims the oldest messages --------------------
    for i in range(10):
        await store2.append(100, "user", f"m{i}")
    await store2.flush()
    msgs = await store2.get_messages(100)
    assert len(msgs) == 4, msgs
    assert msgs[-1]["content"] == "m9", msgs

    # --- 4. channels are independent ------------------------------------
    await store2.append(200, "user", "different channel")
    assert len(await store2.get_messages(100)) == 4
    assert len(await store2.get_messages(200)) == 1
    assert await store2.get_system_prompt(200) is None

    # --- 5. clear() wipes prompt AND history ----------------------------
    await store2.clear(100)
    await store2.flush()
    store3 = ConversationStore(path, max_messages=4)
    store3.load()
    assert await store3.get_messages(100) == []
    assert await store3.get_system_prompt(100) is None
    # other channel untouched
    assert len(await store3.get_messages(200)) == 1

    # --- 6. corrupt file is quarantined, not fatal ----------------------
    path.write_text("{ not valid json", encoding="utf-8")
    store4 = ConversationStore(path, max_messages=4)
    store4.load()  # must not raise
    assert await store4.get_messages(999) == []

    # --- 7. free-chat flag: default ON, persists, reset by clear --------
    assert await store3.get_free_chat(200) is True            # default
    await store3.set_free_chat(200, False)
    assert await store3.get_free_chat(200) is False
    await store3.flush()
    store5 = ConversationStore(path, max_messages=4)
    store5.load()
    assert await store5.get_free_chat(200) is False           # persisted
    # read-only getter must not create buckets for unknown channels
    assert await store5.get_free_chat(424242) is True
    assert all(cid != 424242 for cid, _ in store5.channel_stats_sync())
    await store3.clear(200)
    assert await store3.get_free_chat(200) is True            # reset by clear()

    print("All storage smoke tests passed ✅")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Leave no scratch files behind.
        shutil.rmtree(TMP_DIR, ignore_errors=True)
