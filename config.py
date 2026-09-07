"""
config.py — central configuration for the AI Discord bot.

All runtime settings live in environment variables, loaded from a local
`.env` file via python-dotenv. Secrets never appear in the source code,
which keeps the project safe to commit to version control (the `.env`
file itself is listed in `.gitignore`).

Every mandatory variable is validated up-front in `load_config()` so the
bot fails fast with a human-readable message instead of crashing later
deep inside a Discord event handler.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root — the folder this file lives in.
BASE_DIR = Path(__file__).resolve().parent

# Providers the bot knows how to talk to.
# "custom" = any OpenAI-compatible API (B.AI, Z.ai/GLM, OpenRouter, local
# servers like LM Studio/Ollama…) configured via AI_BASE_URL + AI_MODEL.
SUPPORTED_PROVIDERS = {"openai", "gemini", "custom"}

# Sensible default model per provider (override with AI_MODEL in .env).
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
    "custom": "",  # must be set explicitly — there is no sensible guess
}


class ConfigError(RuntimeError):
    """Raised when mandatory configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the bot's runtime configuration."""

    discord_token: str
    command_prefix: str
    ai_provider: str                 # "openai" | "gemini" | "custom"
    ai_api_key: str
    ai_model: str
    ai_base_url: str | None          # OpenAI-compatible endpoint (custom providers)
    temperature: float
    max_history_messages: int        # per-channel rolling window size
    max_input_chars: int             # max accepted user prompt length
    max_prompt_chars: int            # max accepted `.promt` text length
    request_timeout: float           # hard timeout per AI call, seconds
    data_dir: Path
    log_dir: Path

    @property
    def history_file(self) -> Path:
        """Path of the JSON file that stores per-channel conversations."""
        return self.data_dir / "history.json"


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read an integer env var with a default and a lower bound."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer (got {raw!r}).") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {value}).")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a float env var with a default and an inclusive range."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {value}).")
    return value


def load_config(env_file: Path | None = None) -> Config:
    """
    Read, validate and return the bot configuration.

    Raises:
        ConfigError: with a bullet list of every problem found, so the user
            can fix all issues in one edit instead of one-by-one.
    """
    env_file = env_file or BASE_DIR / ".env"
    if env_file.exists():
        # override=False: real environment variables win over .env values,
        # which is what you want in CI / hosting platforms.
        load_dotenv(env_file, override=False)
    else:
        print(f"[config] Note: no .env file found at {env_file} — relying on real environment variables.")

    problems: list[str] = []

    # ---- Discord ------------------------------------------------------
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not discord_token:
        problems.append(
            "DISCORD_TOKEN is missing. Create a bot at "
            "https://discord.com/developers/applications and copy its token "
            "into .env (see README.md -> 'Getting the Discord bot token')."
        )
    command_prefix = os.getenv("COMMAND_PREFIX", ".").strip() or "."

    # ---- AI provider ----------------------------------------------------
    provider = os.getenv("AI_PROVIDER", "gemini").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        problems.append(
            f"AI_PROVIDER must be one of {sorted(SUPPORTED_PROVIDERS)} (got {provider!r})."
        )
        provider = "gemini"  # safe fallback so the rest of validation can continue

    # "custom" = any OpenAI-compatible API (B.AI, Z.ai/GLM, OpenRouter, …):
    # the same Chat Completions protocol, just at a different base URL.
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    gemini_key = (os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")).strip()
    custom_key = (os.getenv("AI_API_KEY", "") or openai_key).strip()
    if provider == "openai":
        ai_api_key, key_env = openai_key, "OPENAI_API_KEY"
    elif provider == "gemini":
        ai_api_key, key_env = gemini_key, "GEMINI_API_KEY"
    else:  # custom
        ai_api_key, key_env = custom_key, "AI_API_KEY"
    if not ai_api_key:
        problems.append(
            f"No API key found for provider '{provider}'. Set {key_env} in .env "
            "(see README.md -> 'Getting the AI provider API key')."
        )

    ai_model = os.getenv("AI_MODEL", "").strip() or DEFAULT_MODELS.get(provider, "")

    # Base URL override — required for "custom", optional for "openai".
    ai_base_url = (os.getenv("AI_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "")).strip() or None
    if provider == "custom":
        if not ai_model:
            problems.append(
                "AI_MODEL is required when AI_PROVIDER=custom "
                "(e.g. AI_MODEL=glm-5.3-flash)."
            )
        if not ai_base_url:
            problems.append(
                "AI_BASE_URL is required when AI_PROVIDER=custom "
                "(e.g. AI_BASE_URL=https://api.b.ai/v1)."
            )
    if ai_base_url and not ai_base_url.startswith(("http://", "https://")):
        problems.append(
            f"AI_BASE_URL must start with http:// or https:// (got {ai_base_url!r})."
        )

    # ---- Directories ------------------------------------------------------
    data_dir = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
    log_dir = BASE_DIR / "logs"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"Cannot create data/log directories ({exc}).")

    if problems:
        raise ConfigError("Configuration problems found:\n- " + "\n- ".join(problems))

    return Config(
        discord_token=discord_token,
        command_prefix=command_prefix,
        ai_provider=provider,
        ai_api_key=ai_api_key,
        ai_model=ai_model,
        ai_base_url=ai_base_url,
        temperature=_env_float("AI_TEMPERATURE", 0.7, 0.0, 2.0),
        max_history_messages=_env_int("MAX_HISTORY_MESSAGES", 40, 2),
        max_input_chars=_env_int("MAX_INPUT_CHARS", 4000, 100),
        max_prompt_chars=_env_int("MAX_PROMPT_CHARS", 1500, 50),
        request_timeout=_env_float("AI_REQUEST_TIMEOUT", 90.0, 5.0, 600.0),
        data_dir=data_dir,
        log_dir=log_dir,
    )
