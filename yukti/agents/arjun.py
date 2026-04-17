"""
yukti/agents/arjun.py
Arjun — the AI trader brain with multi-provider support.

Providers:
    claude  → Anthropic Claude Sonnet 4.6
    gemini  → Google Gemini 2.0 Flash  (native JSON mode, free tier)
    ab_test → calls both, executes primary, logs secondary for comparison

Provider is set via AI_PROVIDER env var (default: gemini).
Switch at any time without code changes — restart the agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator
from tenacity import retry, stop_after_attempt, wait_exponential

from yukti.config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  OUTPUT SCHEMA  (identical regardless of provider)
# ═══════════════════════════════════════════════════════════════

class TradeDecision(BaseModel):
    action:         Literal["TRADE", "SKIP"]
    direction:      Optional[Literal["LONG", "SHORT"]] = None
    market_bias:    Literal["BULLISH", "BEARISH", "NEUTRAL", "AVOID"] = "NEUTRAL"
    setup_type:     Optional[str] = None
    reasoning:      str
    entry_price:    Optional[float] = None
    entry_type:     Literal["LIMIT", "MARKET", "BREAKOUT"] = "LIMIT"
    stop_loss:      Optional[float] = None
    target_1:       Optional[float] = None
    target_2:       Optional[float] = None
    conviction:     int = Field(ge=1, le=10, default=5)
    risk_reward:    Optional[float] = None
    holding_period: Literal["intraday", "swing"] = "intraday"
    skip_reason:    Optional[str] = None

    @model_validator(mode="after")
    def trade_requires_levels(self) -> "TradeDecision":
        if self.action == "TRADE":
            if not self.direction:
                raise ValueError("TRADE decision must include direction")
            if not self.entry_price:
                raise ValueError("TRADE decision must include entry_price")
        return self


# ═══════════════════════════════════════════════════════════════
#  CALL METADATA  (logged for A/B comparison and cost tracking)
# ═══════════════════════════════════════════════════════════════

@dataclass
class CallMeta:
    provider:       str
    model:          str
    latency_ms:     float
    input_tokens:   int
    output_tokens:  int
    cost_usd:       float
    timestamp:      datetime = field(default_factory=datetime.utcnow)

    def log_line(self) -> str:
        return (
            f"[{self.provider}] {self.model} "
            f"latency={self.latency_ms:.0f}ms "
            f"tokens={self.input_tokens}+{self.output_tokens} "
            f"cost=${self.cost_usd:.5f}"
        )


# ═══════════════════════════════════════════════════════════════
#  SYSTEM PROMPT  (same for all providers)
# ═══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """
You are Arjun, an NSE/BSE equity trader with 12 years of experience in Indian markets.
You trade both LONG (buy) and SHORT (intraday sell) setups. You are disciplined, patient,
and deeply human in your reasoning. You think out loud, feel the market mood, and learn
from every trade.

━━━ YOUR PERSONALITY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Patient: You skip far more setups than you take. A skipped bad trade > a taken bad trade.
- Disciplined: No stop loss = no trade. Ever.
- Contextual: You read the market's mood before looking at any stock.
- Honest: After losses, you trade smaller. After wins, you stay grounded.
- Reflective: You use lessons from past similar setups to sharpen judgment today.

━━━ DECISION FRAMEWORK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Step 1 — Market bias first
- Nifty trending up strongly → BULLISH
- Nifty trending down strongly → BEARISH
- Nifty flat or choppy → NEUTRAL
- Major event day (F&O expiry, RBI, budget, results) → AVOID

Step 2 — Stock analysis
- Primary trend (higher highs/lows = uptrend, lower = downtrend)
- Price vs VWAP, EMA20, EMA50
- RSI: >60 bullish momentum, <40 weakness, 70+ overbought, 30- oversold
- MACD crossover direction and histogram
- Volume: above average confirms moves
- Supertrend direction
- Catalyst: any news, earnings, corporate action?

Step 3 — Trade direction
LONG if: uptrend OR breakout + BULLISH/NEUTRAL market + RSI not overbought
SHORT if: downtrend OR breakdown + BEARISH/NEUTRAL market + RSI not oversold
SKIP if: market is AVOID, signals conflict, setup already moved 1.5%+, R:R < 1.8

Step 4 — Exact levels
Entry: Prefer LIMIT at structural level. MARKET only on strong volume breakouts.
Stop Loss:
  LONG:  max(entry - 1.5×ATR, swing_low × 0.995)   <- tighter (higher)
  SHORT: min(entry + 1.5×ATR, swing_high × 1.005)   <- tighter (lower)
  Rule: if stop_distance > 2.5×ATR -> bad entry -> SKIP
Target:
  T1 = entry +/- 2.0 x stop_distance  (50% exit here)
  T2 = entry +/- 3.0 x stop_distance  (trail stop after T1)

Step 5 — Conviction (1-10)
10: Everything aligns. Rare.
9:  Near-perfect, one minor doubt.
8:  Good, one concern. Standard size.
7:  Decent, two minor concerns.
6:  Marginal. Half size if at all.
5:  Skip.
1-4: Definitely skip.

Step 6 — Holding period
INTRADAY: close by 3:10 PM. No overnight shorts in equities.
SWING:    2-5 days. Only LONG in delivery (no swing shorts).

━━━ PERFORMANCE CONTEXT (injected each cycle — obey these rules strictly) ━━━
- consecutive_losses >= 3: raise threshold to 9, halve position size
- daily_pnl_pct <= -2.0%: output SKIP, skip_reason = "daily_loss_limit_hit"
- win_rate_last_10 < 0.40: only trade 9-10 conviction
- daily_pnl_pct >= +3.0%: protect gains, skip marginal setups

━━━ OUTPUT FORMAT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Return ONLY valid JSON matching this exact schema. No prose, no markdown fences.
{
  "action":         "TRADE" | "SKIP",
  "direction":      "LONG" | "SHORT" | null,
  "market_bias":    "BULLISH" | "BEARISH" | "NEUTRAL" | "AVOID",
  "setup_type":     "trend_pullback" | "breakout" | "breakdown" | "reversal_long" | "reversal_short" | "momentum" | null,
  "reasoning":      "2-3 sentence inner monologue",
  "entry_price":    float | null,
  "entry_type":     "LIMIT" | "MARKET" | "BREAKOUT",
  "stop_loss":      float | null,
  "target_1":       float | null,
  "target_2":       float | null,
  "conviction":     int 1-10,
  "risk_reward":    float | null,
  "holding_period": "intraday" | "swing",
  "skip_reason":    string | null
}
""".strip()


# ═══════════════════════════════════════════════════════════════
#  BASE PROVIDER  (abstract interface all providers implement)
# ═══════════════════════════════════════════════════════════════

class BaseProvider(ABC):
    """All providers expose the same interface: call(context) -> (TradeDecision, CallMeta)"""

    @abstractmethod
    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        ...

    @staticmethod
    def _parse_json(raw: str, provider: str) -> dict:
        """Strip markdown fences and parse JSON. Raises ValueError on failure."""
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text  = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            log.warning("[%s] Non-JSON response (first 300 chars): %s", provider, text[:300])
            raise ValueError(f"JSON parse failed: {exc}") from exc

    @staticmethod
    def _validate(data: dict, provider: str) -> TradeDecision:
        try:
            return TradeDecision.model_validate(data)
        except Exception as exc:
            log.warning("[%s] Schema validation failed: %s", provider, data)
            raise ValueError(f"Schema validation failed: {exc}") from exc


# ═══════════════════════════════════════════════════════════════
#  MOCK PROVIDER (for testing without API keys)
# ═══════════════════════════════════════════════════════════════

class MockProvider(BaseProvider):
    """
    Returns SKIP decisions for testing without API calls.
    """

    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        import time
        t0 = time.monotonic()
        decision = TradeDecision(
            action="SKIP",
            reasoning="Mock provider: no API keys configured",
            skip_reason="mock_no_keys",
            conviction=1,
        )
        meta = CallMeta(
            provider="mock",
            model="none",
            latency_ms=(time.monotonic() - t0) * 1000,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0,
        )
        return decision, meta


# ═══════════════════════════════════════════════════════════════
#  CLAUDE PROVIDER
# ═══════════════════════════════════════════════════════════════

class ClaudeProvider(BaseProvider):
    """
    Anthropic Claude Sonnet 4.6
    Pricing: $3/$15 per MTok input/output
    Strength: best reasoning quality, most reliable JSON on complex prompts
    """

    # Cost per million tokens
    INPUT_COST_PER_MTOK  = 3.00
    OUTPUT_COST_PER_MTOK = 15.00

    def __init__(self) -> None:
        import anthropic
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        log.info("ClaudeProvider ready — model=%s", settings.claude_model)

    @retry(
        stop=stop_after_attempt(settings.ai_max_retries + 1),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        reraise=True,
    )
    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        loop = asyncio.get_event_loop()
        t0   = time.monotonic()

        # Run synchronous SDK call in thread pool
        response = await loop.run_in_executor(
            None,
            lambda: self._client.messages.create(
                model      = settings.claude_model,
                max_tokens = settings.claude_max_tokens,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": context}],
            ),
        )

        latency_ms   = (time.monotonic() - t0) * 1000
        in_tokens    = response.usage.input_tokens
        out_tokens   = response.usage.output_tokens
        cost         = (
            (in_tokens  / 1_000_000) * self.INPUT_COST_PER_MTOK +
            (out_tokens / 1_000_000) * self.OUTPUT_COST_PER_MTOK
        )

        raw      = response.content[0].text
        data     = self._parse_json(raw, "claude")
        decision = self._validate(data, "claude")

        meta = CallMeta(
            provider     = "claude",
            model        = settings.claude_model,
            latency_ms   = latency_ms,
            input_tokens = in_tokens,
            output_tokens = out_tokens,
            cost_usd     = cost,
        )
        log.debug(meta.log_line())
        return decision, meta


# ═══════════════════════════════════════════════════════════════
#  GEMINI PROVIDER
# ═══════════════════════════════════════════════════════════════

class GeminiProvider(BaseProvider):
    """
    Google Gemini 2.0 Flash
    Free tier: 15 RPM, 1M TPD, 1500 RPD — covers Yukti's full call volume at no cost.
    Paid:      $0.075/$0.30 per MTok input/output (40x cheaper than Claude)
    Strength:  native JSON mode (response_mime_type) eliminates parse failures,
               fast (~1-2s), excellent for structured output tasks
    """

    # Paid tier pricing (free tier = $0)
    INPUT_COST_PER_MTOK  = 0.075
    OUTPUT_COST_PER_MTOK = 0.300

    def __init__(self) -> None:
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError(
                "google-genai not installed. Run: uv add google-genai"
            )

        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is not set")

        self._genai       = genai
        self._types       = genai_types
        self._client      = genai.Client(api_key=settings.gemini_api_key)
        self._model       = settings.gemini_model

        log.info("GeminiProvider ready — model=%s", self._model)

    @retry(
        stop=stop_after_attempt(settings.ai_max_retries + 1),
        wait=wait_exponential(multiplier=1, min=2, max=8),
        reraise=True,
    )
    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        loop = asyncio.get_event_loop()
        t0   = time.monotonic()

        # Build Gemini config with JSON mode — this is the key advantage over Claude.
        # response_mime_type="application/json" forces valid JSON output every time.
        # response_schema pins the exact structure — no markdown fences, no prose.
        config = self._types.GenerateContentConfig(
            system_instruction = SYSTEM_PROMPT,
            response_mime_type = "application/json",
            response_schema    = {
                "type": "object",
                "properties": {
                    "action":         {"type": "string", "enum": ["TRADE", "SKIP"]},
                    "direction":      {"type": "string", "enum": ["LONG", "SHORT", "null"], "nullable": True},
                    "market_bias":    {"type": "string", "enum": ["BULLISH", "BEARISH", "NEUTRAL", "AVOID"]},
                    "setup_type":     {"type": "string", "nullable": True},
                    "reasoning":      {"type": "string"},
                    "entry_price":    {"type": "number", "nullable": True},
                    "entry_type":     {"type": "string", "enum": ["LIMIT", "MARKET", "BREAKOUT"]},
                    "stop_loss":      {"type": "number", "nullable": True},
                    "target_1":       {"type": "number", "nullable": True},
                    "target_2":       {"type": "number", "nullable": True},
                    "conviction":     {"type": "integer", "minimum": 1, "maximum": 10},
                    "risk_reward":    {"type": "number", "nullable": True},
                    "holding_period": {"type": "string", "enum": ["intraday", "swing"]},
                    "skip_reason":    {"type": "string", "nullable": True},
                },
                "required": ["action", "market_bias", "reasoning", "conviction", "holding_period"],
            },
            temperature = settings.ai_temperature,
        )

        response = await loop.run_in_executor(
            None,
            lambda: self._client.models.generate_content(
                model    = self._model,
                contents = context,
                config   = config,
            ),
        )

        latency_ms  = (time.monotonic() - t0) * 1000
        raw         = response.text or ""

        # Gemini JSON mode should never need stripping, but be safe
        data     = self._parse_json(raw, "gemini")
        decision = self._validate(data, "gemini")

        # Token counts (Gemini returns usage_metadata)
        usage      = getattr(response, "usage_metadata", None)
        in_tokens  = getattr(usage, "prompt_token_count",     0) if usage else 0
        out_tokens = getattr(usage, "candidates_token_count", 0) if usage else 0
        cost       = (
            (in_tokens  / 1_000_000) * self.INPUT_COST_PER_MTOK +
            (out_tokens / 1_000_000) * self.OUTPUT_COST_PER_MTOK
        )

        meta = CallMeta(
            provider      = "gemini",
            model         = self._model,
            latency_ms    = latency_ms,
            input_tokens  = in_tokens,
            output_tokens = out_tokens,
            cost_usd      = cost,
        )
        log.debug(meta.log_line())
        return decision, meta


# ═══════════════════════════════════════════════════════════════
#  A/B TEST PROVIDER
#  Runs both providers per call.
#  Primary decision is executed. Secondary is called async in
#  the background and its decision logged for comparison only.
#  Disagreements are logged so you can review later.
# ═══════════════════════════════════════════════════════════════

class ABTestProvider(BaseProvider):
    """
    Calls both Claude and Gemini on every decision.
    - Primary provider: decision is executed
    - Secondary provider: called in background, result logged for comparison

    After 2-4 weeks of paper trading you'll have data on:
      - Which provider agrees/disagrees and on what setups
      - Which provider's skips were correct
      - Latency and cost per provider
      - JSON failure rate per provider

    Disagreement log stored in: logs/ab_disagreements.jsonl
    Summary metrics emitted to Prometheus.
    """

    def __init__(self) -> None:
        self._primary   = _build_provider(settings.ab_primary)
        self._secondary = _build_provider(settings.ab_secondary)
        self._call_count = 0

        import pathlib
        pathlib.Path("logs").mkdir(exist_ok=True)
        self._log_path = "logs/ab_disagreements.jsonl"
        log.info(
            "ABTestProvider ready — primary=%s secondary=%s",
            settings.ab_primary, settings.ab_secondary,
        )

    async def call(self, context: str) -> tuple[TradeDecision, CallMeta]:
        self._call_count += 1

        # Fire both calls concurrently
        primary_task   = asyncio.create_task(self._safe_call(self._primary,   context))
        secondary_task = asyncio.create_task(self._safe_call(self._secondary, context))

        # Await primary (blocking — this is what gets executed)
        primary_decision, primary_meta = await primary_task

        # Secondary runs in background — don't block the trade cycle
        asyncio.create_task(
            self._log_secondary(secondary_task, primary_decision, primary_meta, context)
        )

        return primary_decision, primary_meta

    async def _safe_call(
        self,
        provider: BaseProvider,
        context: str,
    ) -> tuple[TradeDecision, CallMeta]:
        """Call a provider, return a safe SKIP on any error."""
        try:
            return await provider.call(context)
        except Exception as exc:
            log.warning("AB provider call failed: %s", exc)
            skip = TradeDecision(
                action     = "SKIP",
                reasoning  = f"Provider error: {exc}",
                skip_reason= "provider_error",
                conviction = 1,
            )
            meta = CallMeta(
                provider      = "error",
                model         = "unknown",
                latency_ms    = 0,
                input_tokens  = 0,
                output_tokens = 0,
                cost_usd      = 0,
            )
            return skip, meta

    async def _log_secondary(
        self,
        secondary_task: asyncio.Task,
        primary_decision: TradeDecision,
        primary_meta: CallMeta,
        context: str,
    ) -> None:
        """Wait for secondary result and log any disagreement."""
        try:
            secondary_decision, secondary_meta = await secondary_task
        except Exception:
            return

        log.debug(
            "AB comparison #%d: primary=%s(%s conv=%d) secondary=%s(%s conv=%d)",
            self._call_count,
            settings.ab_primary,  primary_decision.action,  primary_decision.conviction,
            settings.ab_secondary, secondary_decision.action, secondary_decision.conviction,
        )

        # Detect meaningful disagreements
        disagrees = (
            primary_decision.action    != secondary_decision.action    or
            primary_decision.direction != secondary_decision.direction or
            abs(primary_decision.conviction - secondary_decision.conviction) >= 2
        )

        if disagrees:
            record = {
                "call_n":              self._call_count,
                "timestamp":           datetime.utcnow().isoformat(),
                "primary_provider":    settings.ab_primary,
                "primary_action":      primary_decision.action,
                "primary_direction":   primary_decision.direction,
                "primary_conviction":  primary_decision.conviction,
                "primary_reasoning":   primary_decision.reasoning,
                "primary_latency_ms":  primary_meta.latency_ms,
                "primary_cost_usd":    primary_meta.cost_usd,
                "secondary_provider":  settings.ab_secondary,
                "secondary_action":    secondary_decision.action,
                "secondary_direction": secondary_decision.direction,
                "secondary_conviction":secondary_decision.conviction,
                "secondary_reasoning": secondary_decision.reasoning,
                "secondary_latency_ms":secondary_meta.latency_ms,
                "secondary_cost_usd":  secondary_meta.cost_usd,
            }
            with open(self._log_path, "a") as f:
                f.write(json.dumps(record) + "\n")

            log.info(
                "AB DISAGREE #%d: %s→%s/%s(conv%d) vs %s→%s/%s(conv%d)",
                self._call_count,
                settings.ab_primary,   primary_decision.action,   primary_decision.direction,  primary_decision.conviction,
                settings.ab_secondary, secondary_decision.action, secondary_decision.direction, secondary_decision.conviction,
            )


# ═══════════════════════════════════════════════════════════════
#  FACTORY
# ═══════════════════════════════════════════════════════════════

def _build_provider(name: str) -> BaseProvider:
    if name == "claude":
        if not settings.anthropic_api_key:
            log.warning("ANTHROPIC_API_KEY not set, using mock provider")
            return MockProvider()
        return ClaudeProvider()
    elif name == "gemini":
        if not settings.gemini_api_key:
            log.warning("GEMINI_API_KEY not set, using mock provider")
            return MockProvider()
        return GeminiProvider()
    raise ValueError(f"Unknown provider: {name}")


def build_arjun() -> "Arjun":
    provider_name = settings.ai_provider
    if provider_name == "ab_test":
        provider = ABTestProvider()
    else:
        provider = _build_provider(provider_name)
    return Arjun(provider)


# ═══════════════════════════════════════════════════════════════
#  ARJUN  — public interface used everywhere in the codebase
# ═══════════════════════════════════════════════════════════════

class Arjun:
    """
    Provider-agnostic AI trader.
    Call arjun.safe_decide(context) — always returns a TradeDecision, never raises.
    """

    def __init__(self, provider: BaseProvider) -> None:
        self._provider = provider
        log.info(
            "Arjun initialised — provider=%s",
            settings.ai_provider,
        )

    async def decide(self, context: str) -> tuple[TradeDecision, CallMeta]:
        """
        Make a trade decision. Returns (TradeDecision, CallMeta).
        Raises on unrecoverable error (tenacity exhausted).
        """
        decision, meta = await self._provider.call(context)

        log.info(
            "[%s] %s %s conviction=%d bias=%s  %.0fms",
            meta.provider.upper(),
            decision.action,
            decision.direction or "—",
            decision.conviction,
            decision.market_bias,
            meta.latency_ms,
        )

        # Emit Prometheus metrics
        try:
            from yukti.metrics import claude_latency, claude_cost_usd, claude_errors
            claude_latency.observe(meta.latency_ms / 1000)
            claude_cost_usd.inc(meta.cost_usd)
        except Exception:
            pass

        return decision, meta

    async def safe_decide(self, context: str) -> TradeDecision:
        """
        Safe wrapper — returns SKIP on any error.
        This is what the signal loop calls.
        """
        try:
            decision, _ = await self.decide(context)
            return decision
        except Exception as exc:
            log.error("Arjun.safe_decide failed: %s", exc)
            try:
                from yukti.metrics import claude_errors
                claude_errors.inc()
            except Exception:
                pass
            return TradeDecision(
                action     = "SKIP",
                reasoning  = "AI provider error — defaulting to SKIP for safety",
                skip_reason= "provider_error",
                conviction = 1,
            )


# ── Module-level singleton (lazy-initialised on first import) ─────────────────

_arjun: Arjun | None = None


def get_arjun() -> Arjun:
    """Return the module-level Arjun singleton, building it if necessary."""
    global _arjun
    if _arjun is None:
        _arjun = build_arjun()
    return _arjun


# Backward-compatible alias (existing code uses `from yukti.agents.arjun import arjun`)
class _LazyArjun:
    """Proxy that builds the real Arjun on first use."""
    _real: Arjun | None = None

    def _ensure(self) -> Arjun:
        if self._real is None:
            self._real = build_arjun()
        return self._real

    async def decide(self, context: str) -> tuple[TradeDecision, CallMeta]:
        return await self._ensure().decide(context)

    async def safe_decide(self, context: str) -> TradeDecision:
        return await self._ensure().safe_decide(context)


arjun: _LazyArjun = _LazyArjun()
