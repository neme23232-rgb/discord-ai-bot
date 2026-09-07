"""
ai_client.py — provider-agnostic async access to the AI backend.

Two adapters implement the same async `chat()` interface:
  * OpenAIProvider — https://platform.openai.com  (SDK: `openai`, AsyncOpenAI)
  * GeminiProvider — https://aistudio.google.com  (SDK: `google-genai`)

The bot only ever talks to `build_provider(config)`, so switching provider
is a one-line .env change (AI_PROVIDER=openai|gemini|custom). "custom"
reuses the OpenAI adapter with a different base_url, which covers every
OpenAI-compatible service (B.AI, Z.ai/GLM, OpenRouter, LM Studio, …).

Error model
-----------
All SDK failures are normalised into two exceptions the caller can react to:
  * AITransientError  — network hiccups, timeouts, rate limits, 5xx.
                        Safe to retry with backoff.
  * AIPermanentError  — bad API key, unknown model, malformed request.
                        Retrying will not help; surface to the user.
`chat_with_retries()` implements the retry policy (3 attempts, exponential
backoff, hard asyncio timeout) in one provider-agnostic place.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from config import Config, ConfigError

log = logging.getLogger("aibot.ai")

# History messages are plain chat dicts: {"role": "user"|"assistant", "content": str}
Message = dict[str, str]


class AIProviderError(RuntimeError):
    """Base class for AI backend failures."""


class AITransientError(AIProviderError):
    """Temporary failure (network, timeout, rate limit, server 5xx)."""


class AIPermanentError(AIProviderError):
    """Permanent failure (bad key, unknown model, invalid request)."""


class AIProvider(Protocol):
    """Structural interface every provider adapter implements."""

    async def chat(self, system_prompt: str | None, history: list[Message]) -> str:
        """Return the assistant's reply for the given conversation."""
        ...


# ---------------------------------------------------------------------------
# OpenAI adapter
# ---------------------------------------------------------------------------
class OpenAIProvider:
    """
    Adapter for the OpenAI Chat Completions API (AsyncOpenAI client).

    Also speaks to ANY OpenAI-compatible backend — B.AI (chat.b.ai),
    Z.ai/GLM, OpenRouter, LM Studio, etc. — by pointing `base_url` at that
    service: they all implement the same /chat/completions protocol.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float,
        timeout: float,
        base_url: str | None = None,
    ) -> None:
        from openai import AsyncOpenAI  # imported lazily so the other provider's SDK is optional

        # max_retries=0 -> the retry policy lives in chat_with_retries(), not the SDK.
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=0, base_url=base_url)
        self._model = model
        self._temperature = temperature

    async def chat(self, system_prompt: str | None, history: list[Message]) -> str:
        from openai import (
            APIConnectionError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            InternalServerError,
            RateLimitError,
        )

        # The system prompt (persona) is the first message; the per-channel
        # history follows in chronological order.
        messages: list[Message] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=self._temperature,
            )
        except (APIConnectionError, APITimeoutError) as exc:
            raise AITransientError(f"OpenAI connection problem: {exc}") from exc
        except RateLimitError as exc:
            raise AITransientError(f"OpenAI rate limit / quota: {exc}") from exc
        except InternalServerError as exc:
            raise AITransientError(f"OpenAI server error: {exc}") from exc
        except AuthenticationError as exc:
            raise AIPermanentError("OpenAI rejected the API key (AuthenticationError).") from exc
        except BadRequestError as exc:
            # Most commonly an unknown model name or a malformed payload.
            raise AIPermanentError(f"OpenAI rejected the request: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (IndexError, AttributeError) as exc:
            raise AITransientError("OpenAI returned an unexpected response shape.") from exc
        if not content or not content.strip():
            raise AITransientError("OpenAI returned an empty message.")
        return content.strip()


# ---------------------------------------------------------------------------
# Gemini adapter
# ---------------------------------------------------------------------------
class GeminiProvider:
    """Adapter for the Gemini API via the official google-genai SDK."""

    def __init__(self, api_key: str, model: str, temperature: float) -> None:
        from google import genai  # lazy import, same reason as OpenAI's

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._temperature = temperature

    async def chat(self, system_prompt: str | None, history: list[Message]) -> str:
        from google.genai import errors as genai_errors
        from google.genai import types

        # Gemini roles are "user" and "model" — map our "assistant" across.
        contents = [
            types.Content(
                role="model" if msg["role"] == "assistant" else "user",
                parts=[types.Part(text=msg["content"])],
            )
            for msg in history
        ]
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,  # None -> no system instruction
            temperature=self._temperature,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", None)
            if status == 429 or (status or 0) >= 500:
                raise AITransientError(f"Gemini API error {status}: {exc}") from exc
            # 400 (bad model name / malformed), 401/403 (key) -> permanent.
            raise AIPermanentError(f"Gemini API error {status}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — SDK version differences, network layer
            raise AITransientError(f"Gemini request failed: {exc}") from exc

        # `.text` raises ValueError when the response has no text parts
        # (e.g. blocked by safety filters) — normalise to a transient error.
        try:
            text = (response.text or "").strip()
        except (ValueError, AttributeError):
            text = ""
        if not text:
            raise AITransientError(
                "Gemini returned an empty response (possibly blocked by safety filters)."
            )
        return text


# ---------------------------------------------------------------------------
# Factory + retry policy
# ---------------------------------------------------------------------------
def build_provider(config: Config) -> AIProvider:
    """Create the provider adapter selected by config.ai_provider."""
    try:
        if config.ai_provider in ("openai", "custom"):
            where = f" @ {config.ai_base_url}" if config.ai_base_url else ""
            log.info("Using OpenAI-compatible provider%s (model=%s).", where, config.ai_model)
            return OpenAIProvider(
                config.ai_api_key,
                config.ai_model,
                config.temperature,
                config.request_timeout,
                base_url=config.ai_base_url,
            )
        log.info("Using Gemini provider (model=%s).", config.ai_model)
        return GeminiProvider(config.ai_api_key, config.ai_model, config.temperature)
    except ImportError as exc:
        raise ConfigError(
            f"The SDK for provider '{config.ai_provider}' is not installed ({exc}). "
            "Run: pip install -r requirements.txt"
        ) from exc


async def chat_with_retries(
    provider: AIProvider,
    system_prompt: str | None,
    history: list[Message],
    *,
    attempts: int = 3,
    timeout: float = 90.0,
) -> str:
    """
    Call provider.chat() with a hard timeout and exponential backoff.

    Retry schedule: attempt 1 immediately, attempt 2 after 1 s, attempt 3
    after 3 s. Only AITransientError is retried; permanent errors bubble up
    immediately so the user gets a precise, actionable message.
    """
    last_error: AITransientError | None = None
    for attempt in range(1, attempts + 1):
        try:
            # wait_for is the hard ceiling: even if an SDK hangs internally,
            # this coroutine is cancelled and the command handler moves on.
            return await asyncio.wait_for(
                provider.chat(system_prompt, history), timeout=timeout
            )
        except asyncio.TimeoutError:
            last_error = AITransientError(f"AI request timed out after {timeout:.0f}s.")
            log.warning("AI attempt %d/%d timed out.", attempt, attempts)
        except AITransientError as exc:
            last_error = exc
            log.warning("AI attempt %d/%d failed: %s", attempt, attempts, exc)
        if attempt < attempts:
            await asyncio.sleep(1.0 * 2 ** (attempt - 1))
    assert last_error is not None
    raise last_error
