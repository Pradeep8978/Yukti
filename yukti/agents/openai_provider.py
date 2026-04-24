"""OpenAI provider for Arjun — minimal glue to OpenAI ChatCompletion.

This provider mirrors the interface used by other providers in `yukti.agents.arjun`.
It runs the synchronous OpenAI SDK call in a threadpool so the scheduler/event-loop
remains non-blocking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential

from yukti.config import settings
from yukti.agents.arjun import BaseProvider, TradeDecision, CallMeta, SYSTEM_PROMPT

log = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """Simple OpenAI ChatCompletion provider.

    Requires `openai` to be installed and `OPENAI_API_KEY` set in config/env.
    """

    def __init__(self) -> None:
        try:
            import openai as _openai  # imported lazily to avoid hard dependency at module import
        except Exception as exc:  # pragma: no cover - runtime environment may not have SDK
            raise ImportError("openai package not installed. Run: uv add openai") from exc

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is not set")

        _openai.api_key = settings.openai_api_key
        self._client = _openai
        self._model = settings.openai_model
        self._max_tokens = settings.openai_max_tokens
        self._temperature = settings.openai_temperature or settings.ai_temperature

        log.info("OpenAIProvider ready — model=%s", self._model)

    @retry(
        stop=stop_after_attempt(settings.ai_max_retries + 1),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        reraise=True,
    )
    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()

        def _sync_call():
            # Use ChatCompletion.create for widest SDK compatibility
            return self._client.ChatCompletion.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )

        try:
            response = await loop.run_in_executor(None, _sync_call)
        except Exception as exc:
            log.exception("OpenAI API call failed: %s", exc)
            # Safe SKIP on provider failure
            skip = TradeDecision(
                symbol="UNKNOWN",
                action="SKIP",
                reasoning=f"OpenAI provider error: {exc}",
                skip_reason="provider_error",
                conviction=1,
            )
            meta = CallMeta(provider="openai", model=self._model, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0)
            return skip, meta

        latency_ms = (time.monotonic() - t0) * 1000

        # Token accounting (best-effort)
        usage = getattr(response, "usage", None) or {}
        in_tokens = usage.get("prompt_tokens", 0)
        out_tokens = usage.get("completion_tokens", 0)
        cost = 0.0

        # Extract text from response (support different SDK shapes)
        raw = ""
        try:
            choice = response.choices[0]
            # Newer SDKs provide message with .content, older provide .text
            if hasattr(choice, "message") and choice.message is not None:
                raw = getattr(choice.message, "content", "") or choice.message.get("content", "")
            else:
                raw = getattr(choice, "text", "") or ""
        except Exception:
            try:
                raw = str(response)
            except Exception:
                raw = ""

        try:
            data = self._parse_json(raw, "openai")
            symbol = self._extract_symbol(context)
            decision = self._validate(data, "openai", symbol)
        except Exception as exc:
            log.exception("OpenAI response parse/validate failed: %s", exc)
            skip = TradeDecision(
                symbol="UNKNOWN",
                action="SKIP",
                reasoning=f"OpenAI parse/validate failed: {exc}",
                skip_reason="parse_error",
                conviction=1,
            )
            meta = CallMeta(provider="openai", model=self._model, latency_ms=latency_ms, input_tokens=in_tokens, output_tokens=out_tokens, cost_usd=cost)
            return skip, meta

        meta = CallMeta(provider="openai", model=self._model, latency_ms=latency_ms, input_tokens=in_tokens, output_tokens=out_tokens, cost_usd=cost)
        log.debug(meta.log_line())
        return decision, meta
